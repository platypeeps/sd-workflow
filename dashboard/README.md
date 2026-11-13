# dashboard

This directory contains the workflow dashboard and the six legacy collector
commands. They are separate surfaces.

The dashboard moved here from the system checkout (sd:5987). It still reads
the operator's config from `<config>/project-dashboard/` (`<config>` is
`$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`), and runs the machine
tools of the system checkout, `$SYSTEM_TOOLS_ROOT`, default `~/repos/system`.

**The workflow dashboard** (`sd_dashboard/`) opens on the new design's Today, and keeps
the classic Contributions, Protection, and
individual Item pages. sd:3402 retired the other classic screens; their controls are on the new pages.
A `/backlog` address opens Tasks with the same filters (sd:2356); the classic
Backlog was deleted in sd:2622. Its production launcher uses the
command pack's provisioned Python interpreter and the pack checkout's `lib/sd_db`;
it refuses any other copy of `sd_db` or a mismatched database schema.
`./dashboard.sh serve` starts it on loopback;
`./dashboard.sh test` runs its tests. Capture an ordinary task, edit its title,
details, priority and due date, change its status, and add or resolve notes.
The CLI and the page call the same `sd_db.workflow` functions. Saving uses a
revision checked inside the write transaction: an old tab gets a conflict
instead of overwriting a newer change. Item forms offer a reload link.
Tasks triages in bulk (sd:3012): pick rows, then set a priority or Close them with one reason (`sd task cancel`).
Each row is written with its own revision, and the toast names the rows refused as stale and the rows that failed.

**Today** opens with **Now**: the fleet's loudest facts, ranked, loudest first —
scheduled jobs that failed or want attention, checkouts with unpushed commits, checkouts with uncommitted files, abandoned
worktrees, and open pull requests waiting on you. The ranking is the pack
dashboard's `dashboard/now.py`, carried across with its rank numbers, in
`sd_dashboard/now_screen.py`; the band a row wears (`broken`, `look`,
`queued`) is derived from the rank alone, never from the kind. The four
sources are the ones Management, Health and GitHub show in full: repos and sessions
through the fleet child, pull requests through `sd_db.shadow` (`needs_you`
rows only; the days a stale row shows count from the sync's first sighting,
because the shadow table carries no `updated_at`), and Jobs through
`sd_db.operations.inventory`. A failed job ranks 1, after a dark collector and
before every other row; it names the exit code or signal, the time its log was
last written, and its retry line, `launchctl kickstart <service>`, the one Management's Retry sends
(a hand-run of `cron-jobs.sh run` does not clear launchd's record).
A failed job's detail ends with a triage class from fixed rules in `sd_dashboard/job_failures.py`:
`transient`, `probably transient`, `unclear` or `needs a person`, and the rule that matched (sd:1166).
The rules read launchd's signal or exit code, then the last run's section of the job's log.
It is a label only: it changes no rank, retry or notification.
Jev is asked the same question in shadow, in the background, once per failed run (sd:2095, sd:2904).
It sees the job name, the outcome and the rule's class, never a log line.
`--shadow` hands back the rule's answer, so Jev's score is recorded in the judgment ledger and never shown.
`JEV_JOB_TRIAGE` switches the shadow off; unset means on.
Each `score` passes `--subject job-triage:<16 hex>`: the first 16 hex of the sha256 of the row id `job:<name>:<exit code or signalN>`, followed by a newline (sd:2953).
An outcome recomputes it with `printf '%s\n' ID | shasum -a 256 | cut -c1-16`.
One shadow pass is one run, `JEV_RUN=job-triage-<UTC yyyymmddThhmmss>-<4 hex>`; the server never reuses an inherited one.
An interrupted, unloaded or unknown job ranks 2, in the look band (sd:2014). An interrupted one carries the same retry line.
An unloaded one names the `cron-jobs.sh install` that loads it; an unknown one names the `launchctl print` that shows its record.
The rows arrive by `/api/now` after
the page is up, so a Today load never waits on the fleet; Refresh reads again.
A collector that goes dark — the child refused, past its budget, exiting
non-zero, a shadow read or a launchd read that fails — is a rank-0 row naming the collector
and the reason, never an empty list. Nothing is stored and nothing on Now
writes; there is no dismiss.

Today is one page with no tabs (sd:5921, the system checkout's `system/docs/work/2026-10-23-today-queue/design.md`):
- The annunciator: Jobs, Lane, Reports, Pulls, Mail and Repos filter Needs you; Sessions and Gate open the Queue view.
- The day track: the last 24 hours as marks (landed, failed jobs, new briefs, sessions), with a key linked to Tasks.
- **Needs you**: every row that asks for an act, ranked broken, look, queued. The work items and briefs fold into group
  rows ("114 items past due" with Triage and Snooze all 1w; "4 new briefs" with Mark all seen). The rail badge counts
  this list's broken rows, else its look rows.
- **Queue** (pack sd:7234): one row per repo that opens to its working, merging, next and blocked rows, and with its
  filters on, its suggested ready items and ended runs. It took Queue's Overview, Today's Queues and In motion.

Decisions are on **Decide** and GitHub on **GitHub**; an old `#view=` address opens its new home with its query.
`sd today` prints the sections the Status tab showed.

**GitHub**, at `/github` in the rail's Work group (sd:5781), has three tabs: Pull requests, Dependabot and Tracked. **Pull requests** lists your open pull requests and issues, and those merged or closed in 7 days (sd:5608).
`/api/today/github` (`today_github.py`) reads only what is stored: `shadow`, the Contributions projection, the
repos with GitHub issues on, sd-ship's delivery notes and items filed under a `github:` ref; it calls no collector.
Rows sort newest GitHub activity first; a row with no stamp says not recorded. **Issues**, at `/issues` after GitHub
(pack sd:7682), holds **Your issues**, the open Jira and GitHub issues assigned to you or filed by you, and **Issue
Intake**, each third-party issue no item carries (sd:4628, sd:4840), both read from `/api/github/issues`.
A pull request row opens its details in the right pane (sd:5633), with Open on GitHub, Open item, Adversarial review
(a codex review of the pull request at its head, in its registered checkout) and Copilot review. Copilot review asks
GitHub once per press through `POST /api/today/github/copilot-review`, which runs `gh api` for one open pull request.
Merge (sd:5668, `pr_merge.py`) copies an open pull request in a registered repository, yours or anyone's, into the lane:
its confirm previews the head, author, worktree and item; then it fetches `pull/<n>/head` into `pr/<n>` and
`~/worktrees/<checkout>-pr-<n>`, files or reuses the item under `github:<owner>/<name>#<n>`, and runs
`sd-ship lane enqueue --deliver`. A second Merge resumes a run that stopped. The original stays open until you close it.
Close original (sd:5669, `pr_close.py`) opens once the lane merged the copy, as its item's delivery note records it:
its confirm shows the exact comment, which links the lane's pull request and merge commit; then it posts that comment
once and closes the original, never merges it. A second press finds an earlier comment by its marker and only closes.
Now and Work rows share one selection (sd:5654): pressing a row in either marks it alone, names it in
`?row=` and shows its details; a Work row's title there opens the item on Tasks.

**Decide**, at `/decide` in the rail's Now group (sd:5781), has two tabs: **Decisions** and **Scoring**, the review
queue that was the Review page (`/review` opens Scoring). Its rail badge counts both. **Decisions** list the questions that wait on the operator, one button per option (sd:3012).
Under them, **Rulings by session** groups the last 7 days of `decision` notes by the session that wrote them, each with its item's state (pack sd:6015); Reverse reopens the question a ruling answered.
Ask one with a `question` note that carries one `Option: <text>` line per choice, at least two:
`sd task note N $'Which store?\nOption: Pack store kind\nOption: Database rows' --kind question`.
Keep each option short; the rest of the body is the question. A button writes the ruling as a `decision` note,
`Ruling on note #<id>: <option>`, and resolves the question, in one transaction under the item's revision, so a
stale page writes nothing and reads the list again. `sd task show N` prints both. A question with fewer than two
`Option:` lines gets a text box instead (sd:5064); its words are the ruling, as `--kind decision --answers <id>`
writes. A prose "Decision needed" comment is not listed (`sd_dashboard/decisions.py`).

**The new design is the default** (sd:2163). It is the redesign from ui-design
`products/system/` (`design.md`, `designs/pages/`), built one page at a time.
`sd_dashboard/v2/` holds the ported shell, tokens and self-hosted IBM Plex
under `static/`, served at `/ui/`; `shell.js` marks each change from the
reference with `build:`. The assets take `/ui/` because the old screens own
`/static/`, which serves exactly `dashboard.js`. Every
v2 script builds markup with the `html` tag in `static/markup.js`, which
escapes each value put in it, and puts it in the page with `put`, which takes
only what `html` made; that file holds the one HTML sink criterion 12's grep
allows in v2. Each page registers itself with one module in
`sd_dashboard/v2/pages/` (sd:2418): its section, routes, API routes and the
old screens it keeps. A page reads its document through the shell's one
reader, `static/read.js`, once its follow-up adopts it; the page's script says
so. What each page reads and what it runs is in
[`docs/pages/`](docs/pages/), one file per page.

Pages link each asset under its content digest, `/ui/<file>?v=<digest>` and
`/static/<file>?v=<digest>`, from a map the server computes when it starts
(sd:2141). That answer is `Cache-Control: private, max-age=31536000, immutable`;
a request without the matching `v` stays `no-store`, as pages and the API are.
So an edited asset reaches the browser after a restart, with no build step.
CSS, script and text go gzipped when the browser accepts it; fonts, pages and
API answers do not. `sd_dashboard/caching.py` says why.

The old screens stayed until their section was ported. A page that took an old
screen's path moved that screen under `/classic/`, and its page module says where.
sd:3402 retired every classic screen whose controls had moved; `/classic/contributions`
is the one left under `/classic/`, and the other kept screens keep their paths.
A `/v2/` address that names a page or an asset answers 301 with its new one (`/v2/today` to
`/today`, `/v2/static/<file>` to `/ui/<file>`); any other `/v2/` path is a 404.
One map, built from the page registry and served as `/ui/sections.js`, says
where each rail section goes: `SECTIONS` for ported pages, `CLASSIC` for a
server-drawn screen a section opens (none since Operations went, pack sd:7517), and
`SCREENS` for old screens without a section, which the palette lists under
Classic screens. `./dashboard.sh pages` prints that map, one line per section
or screen; a section in none of the three, such as Notes or HOA, has no old
screen, and the rail says it is not built yet. The classic screens' own nav links
each ported section to its new page, read from the same registry (sd:2473); the
palette still opens the kept classic screens.

"Why is it like this?" starts in the palette (pack sd:6019). Type a file path
and it lists the items the commits on that path cite (their `Closes:`,
`Delivers:` or `Item:` trailers) or deliver (`Code delivery` notes). Type a
rule's words and it lists the `sd:N` each matching line of a `CLAUDE.md`,
`AGENTS.md` or `.claude/rules/` file cites. Each item opens its page, where
its decisions, design and pull requests are. `/api/why?q=` serves it
(`why_search.py`).

The same script carries each page's rail badge, so every page shows every
badge (sd:3446). The server writes it on each request, unversioned and never
cached, and only for a signed-in page. A page's `count` names the read whose
document carries its `attention`, the one place its rule lives. A page that reads
the database alone (`live`) is read again for the rail; any other page shows what
its own last read carried, with that read's time, so the rail never waits on the
network. A failed read shows no badge, and a restarted server shows none for such
a page until it is read again.

Task controls need no repository, planning document, branch, or GitHub issue.
The shell's Capture, on every new page, files a Task or a Followup item, or a
comment note on the selected row's item (sd:3520). A task or followup takes a
priority, P3 unless changed or typed as `p1` to `p4`, and a repository: the
page's (the Management row, the one repo Tasks filters on or the selected task's,
the Today row's) when the page names one, picked with Alt R from
`GET /api/capture-repos` (sd:3521). The Item page's note form attaches a
Comment, Followup, Question, Decision or Proposal note. The word
`followup` names two things in the store — an item kind and a note kind —
and the forms label them apart: a **Followup item** is an item of kind
`followup`, what `sd task add --kind followup` creates; captured on a row with
an item, the child carries `fields.followup_of`, which the CLI cannot set. A
**Followup note** is a note on the item whose page it is added on. Adding a
note does not reopen or unpark its parent. sd:3402 retired the classic Today's
capture panel, with its related-item picker.
The CLI's note default is Comment, matching the item note form.
Work items keep their delivery rules and their repository's status ownership;
the generic status control cannot complete code delivery or override an active
assignment. Followup and personal items close and reopen through the task
statuses. The details form stays off a personal item's screen because
`workflow.edit_item` refuses its details, so that screen holds the status
control alone. A followup gets both forms since sd:816: `edit_item` has
accepted its title, priority, due date, repository and body since sd:809, and
the panel offers those five. Reclassifying a followup is not among them, so the
`Kind` select stays a task's.
Unbuilt screens and controls are not advertised as working.

Database-owned work can update an artifact link without changing its stable
item identity, or be cancelled with a recorded reason. Cancellation closes the
local work while keeping files and delivery evidence intact. Code delivery is
still a separate CLI operation that verifies its commit. An item's external
reference is shown with its snapshot and sync freshness; an old GitHub status
does not control local task completion.

**Contributions (classic)** at `/classic/contributions` shows upstream activity, unfiled local work, evidence, dependencies, and notification state.
Today previews the first five contributions in the same order.
Newly unblocked work comes first, then work awaiting you, work awaiting others, and merged contributions.
The shared library supplies this order to both the dashboard and `sd-status`.
Each acknowledgement binds exact event IDs and the displayed revision; stale submissions refuse.
Acknowledgement leaves notification delivery history and local task status separate.

**Settings** at `/settings` holds the Fleet keys first (sd:3989): `sd.fleet_owners` and `sd.copilot_review`, read with `sd config list sd --json` and set with `sd config set`.
Then what each browser keeps for the dashboard (sd:3438): the theme and the sections width; the rail's right edge also drags the width (sd:3445), and both stay in browser storage.

Today's **Queue** view shows each managed repository's `sd-ship lane` queue (sd:2585), with the host that runs it.
It was the Queue page until pack sd:7234; `/queue` answers 301 to `/today#view=queue`.
The queue lives in the hub database, so a lane hosted on another machine shows too, folder or not (sd:3284); a repo with no row is not drawn.
The page reads each lane in-process with the pack's own `sd_lane`, the read `lane list` makes, and runs `lane list` only where that read could differ (sd:TBD).
Today's Gate lamp shows the gate slots in use and the load.
Each item is one row: merging (step, holder host, claim age, lease end; a passed lease lights caution), next (order, gate, head), building, blocked (status, step, the reason's first line, who acts), cancelled (when, head), or landed today (pull request, merge commit).
Blocked and cancelled entries show for three days; older ones drop off.
Metrics' Lane times view (pack sd:7233, Queue's Last 7 days until then) shows each lane's median wait (enqueue to claim), median gate time (the speculative gate plus prepare) and slowest entry, enqueue to end, read from the entries' history rows (pack sd:6006).
When a lane's median wait passes an hour, the hosted lane run files the parallel-lanes question on that repository's `Decide: parallel lanes` item, once a week at most.
Building rows are the builder gate logs, `gate-*.log`, in the lane folder `lane list` names, `<lane root>/<checkout name>/lane`; set `sd.lane_root` where builders write them.
Up, down, top, hold, release and cancel run `sd-ship lane move|hold|release|cancel` and nothing else.
Cancel asks first. A cancelled row offers Re-queue (sd:3398): `lane retry <item> --expected-head <head>` queues the cancelled entry again, last, at the head the row showed (pack sd:3389).
It acts only on an item whose newest entry is that cancelled one: the page re-queues a cancel and enqueues nothing new.
The verb refuses a moved branch, a newer entry or a merged head, and writes nothing; the page shows its reason.
Re-queue stays off, with that reason on the row, until `sd-ship lane retry --help` names a cancelled entry.
A running row offers Release, which asks first, then runs `lane cancel <item> --expected-claim <token>:<step>` with the claim the row showed (sd:3337, D3 of pack sd:3174).
At prepare the entry goes back to pending; at merge it fails, and a merge already started may still land.
The pack checks the claim under the queue's lock (pack sd:3330): a step that moved on, a claim taken again or an entry that ended releases nothing, and the page reads again.
`sd-ship` refuses to release a holder alive on this machine.
A blocked row offers Retry, `sd-ship lane retry <item>`: its last entry is queued again at the same head with the body the lane kept.
On a repository whose `runner_merge` is manual it also offers Approve, `lane retry <item> --manual`, which asks first: it grants the merge.
Both pass the head the row showed as `--expected-head`; the verb refuses under the queue's lock an entry that ended at another head since the read.
Both stay hidden until `sd-ship lane retry --help` names `--expected-head`, so a pack before sd:3268 shows neither.
The page sends its revision, which covers the pending order, the holds and each blocked or cancelled entry's head; a queue that changed since the read is refused, and the page reads it again.
Move, hold, release and cancel pass the lane revision `lane list` prints as `--expected-revision`, so the verb checks it again under the queue's lock.
Cancel needs it most: `lane cancel` also releases a running entry, so a Cancel on a row the runner claimed since is refused.
Only a refusal the verb reports as written-nothing says the queue is unchanged (sd:3339).
A hub fault, a timeout or an exit without a refusal may follow a landed write: the page says the outcome is not known and reads the lane again.
The runner reads the queue at each item boundary, so an edit never changes a merge in progress.

**Protection** at `/protection` is one table, one row per registered repository:
what its default branch enforces, gap by gap, with the ids and sentences
`sd-status` prints for one repository — `enforce_admins`, `required_checks`,
`strict`, `required_not_produced`, `produced_not_required`, `reviews` — and
the two merge-settings flags beside them. Two baseline flags follow (sd:1741, sd:5406):
"Classic protection" (`protection_source`), clean when classic protection alone guards the branch, and "Required check" (`required_check`).
A clean "Required check" cell names the check: `ci`, or `sd/local-gate` for a `repo.ci = local` repository.
A repository outside the operator's owners carries neither, and both cells
read as not applicable. Nothing on the page calls GitHub: the
rows are a nightly observation written by the shadow collector (`sd shadow
sync`, through `sd_db.protection`), and the page reads them. Unprotected
repositories sort first, then unknown, then protected. Unknown is not
protected: a repository whose protection could not be read (a 403, a timeout,
an exhausted request budget, or one never observed) carries its own marker and
its reason, and shows no gap cells at all.
A checkout with no row of its own borrows the latest row of a sibling checkout of the same GitHub repository.
Its Observed cell then names the lender: `<time> · borrowed from <path>` (sd:1607).

**Writing** has its own view and stage controls. Its list filters by stage,
saved readiness decision, and active or parked pieces. An item's review panel
checks the current draft and reports exactly what prevents readiness; stage
changes call the same library as `sd writing` and the writing pack. A correction
needs a reason and invalidates the prior readiness decision. Dashboard stage
writes require database ownership and never update piece files. Publication
remains a separate reviewed operation; there is no generic publish button.
Park sets a piece aside without changing its stage or files; revive brings it
back to the active writing list.

The page is `/writing`, the v2 Writing page (sd:2125): a board by stage with
four gate lamps per piece, read from `GET /api/writing`
(`writing_screen.document`). Its Stage, Correct, Park and Revive post the item
routes above; Publish and Capture are copy only. The classic filtered list was
retired in sd:3402; [`docs/pages/writing.md`](docs/pages/writing.md) names
what the page does not read yet.

**Skills** is `/skills`, the v2 Skills page (sd:2123): the command pack's skill
catalog with each skill's use in the last three weeks, read from
`GET /api/skills` (`skills_screen.document`). Try, Review, Promote and Demote
post the routes the classic screen posted; Re-import and Evaluate run
`sd skill intake` and install only on an approved revision (sd:3992), and
Rollback swaps back to the copy the last install replaced (system sd:5779); Run,
Schedule, Adopt and Scan are copy only. The classic screen was retired in sd:3402; [`docs/pages/skills.md`](docs/pages/skills.md) has the detail.

**Operations** is gone (pack sd:7517): each area moved to the page that holds its subject, and the
rail's Fleet group no longer lists it. `/operations?area=<area>` answers 301 to the area's new home
(`operations_screen.MOVED`); the bare `/operations` lists those homes, and `/ui/operations.js` follows
the one an old `#view=<area>` names, since only the browser sees that hash, or Trackers' when there is none.
Services and Sessions moved to Management in pack sd:7517: Deploys lists each service with its
last request in Details, and Sessions lists each worktree, abandoned first, and the sd-* processes.
`/operations?area=services` and `?area=sessions`, and their old `#view=` addresses, open those views.
Usage moved to Metrics' Spend view in pack sd:7524. The filter row's month picker reads any month
(`/metrics?month=YYYY-MM`, `/api/metrics?month=`): Spend draws that month's dollars by bill, each capped
bill's burn with its cap rule and the projection from the month's rate (the day it passes the cap), and
the Usage table its bills, providers and roles; the rail badge stays on this month. The week's numbers,
the provider scorecard and the cap and configure controls were already there.
`/operations?area=usage` answers 301 to `/metrics#view=spend`, with its `month` when it names one.
`/api/usage?month=` still serves the month as `sd-db usage --json` bytes (`usage_screen.py`); without a
month the served dashboard answers its kept reading: one past 45 seconds starts a refresh, one past 300
seconds waits for it. Read-only: the sweep is the verb's, so a dead owner's hold shows until the next
reservation or `sd-db usage` binds it.
Resources' four views moved in pack sd:7527, each read through `/api/<page>/<view>` when shown:
Toolbox is Management's Machine view, Research the Repos filter `?research=yes|no`, Vault a section
on Notes and Queues a folded section on Tasks. `/operations?area=resources&resource=<view>` answers
301 to each, and a bare `?area=resources` or `#view=resources` opens Machine.
Trackers moved to GitHub's Tracked view in pack sd:7525 (`/github#view=tracked`, `/api/github/tracked`,
`trackers_screen.document`): every open pull request and issue `sd shadow sync` keeps, of every tracker,
watched rows and Jira's too, with each tracker's sync health under the table. A row's reference is the
last path segment of its URL: `LOG-23818` for a Jira ticket, `owner/repo#4321` on GitHub. Read-only.
sd:3402 retired the other six, and each one's controls live on a new page:
Jobs on Management and Activity, a queued assignment's cancel on its Item page,
Ports on Health, Progress's age buckets as Tasks' `age` filter
and its observed activity on Metrics, Reports on Reports, Repos on Management,
and Commands on Activity. Any other `area=` is a 404.

Every job or service control rechecks the observed state and revision. The last
request's acceptance is shown separately from the current state; acceptance is
not proof that a job finished or that an application is healthy.
Queued assignments can be cancelled without completing their item, with
`sd assignments cancel`. A blocked assignment can be cancelled, whatever its
item's status, once no runner attempt holds its lease, its own or its bundle
lead's. A restore
blocks starting jobs and starting or restarting services, while supported stop
controls remain available. Stopping a service unloads it for the current login;
its plist stays installed and may load again at the next login. Equivalent
`sd services` commands are in the page's collapsible CLI help.

Ports combines `machine-setup` service candidates with one local `lsof`
listener snapshot. It reads `../local-machine-setup/machine-setup.sh` when that
tool is present beside this folder; without it the inventory is reported
incomplete, never as no ports. Docker state and profile membership stay separate from
listener observations. Observed-only ports are included, multiple visible
owners and addresses are retained, and unavailable or incomplete inspection is
reported. A listening socket does not prove application health or identify the
intended service; inspection is limited to what this user can see and performs
no network scan. A collector runs only when its page reads: Health runs the
listener inspection, a machine or vault view runs its tile, and Management and Health
run the fleet reader. Today's Now runs the fleet reader too, for both
areas, reads the shadow table, and reads the launchd
jobs' last-run state as Management does. None of them
exposes prompts, environment variables, output logs or arbitrary commands.
The server keeps the fleet reader's last answer for up to 45 seconds, refreshes it in the background,
and shows when it was read; a failed refresh keeps it, marked stale with the reason (`snapshot.py`).

The main server binds **local loopback**. Remote access requires an explicit
private HTTPS Tailscale Serve origin and the node owner's exact operator login.
Every remote request revalidates the private route, its loopback target and
operator; missing identity, public Funnel or unavailable inspection is refused.
Local requests remain independent of Tailscale and reject forwarded identity
headers. Configuring a proxy alone does not grant workflow access.
An optional `ip_origin` adds HTTP access on this node's exact Tailscale IPv4 address and port 8768.
That listener authenticates the TCP peer through Tailscale WhoIs and rejects forwarded identity headers.
It preserves the HTTPS route and does not bind a LAN address.

For a local service, put `{"port": 8767}` in
`~/.config/sd/dashboard.json`, then review the installation plan:

```sh
./dashboard.sh preflight --config ~/.config/sd/dashboard.json
./dashboard.sh install --config ~/.config/sd/dashboard.json
# Both commands above are previews. Apply only the fingerprint just reviewed:
./dashboard.sh install --config ~/.config/sd/dashboard.json \
  --apply --expected-fingerprint REPLACE_WITH_REVIEWED_FINGERPRINT
./dashboard.sh health --config ~/.config/sd/dashboard.json
```

The local URL is `http://127.0.0.1:8767`. The installer backs up the existing
LaunchAgent and checks the new process, schema and library before
accepting the replacement. Local installation does not call Tailscale. See
[RUNTIME.md](RUNTIME.md) for configuration, rollback boundaries and the
private front door. Run in the foreground with `./dashboard.sh serve`, or run
the disposable source-development tests with `./dashboard.sh test`.

*No frontend build step, and there will not be one.* No `package.json`, no
`node_modules`, no bundler config under this folder: one hand-written
stylesheet and one hand-written script in `sd_dashboard/static/`, served from
disk. The `pyproject.toml` beside this file builds a wheel of the Python so
that consumers install the package instead of adding this folder to
`PYTHONPATH`; it brings no asset pipeline with it and touches none of the
above. Everything the dashboard did not write is escaped text and Markdown is
an allow-listed subset (`markup.py`); behind both, every response carries a
`Content-Security-Policy` with no inline script and `frame-ancestors 'none'`,
so no page anywhere may frame the dashboard.

`sd_dashboard/v2/static/` holds unchanged copies of ui-design foundation files:
`tokens.css`, `theme.js` and the `<annunciator-strip>` component.
`tests/test_v2_foundation.py` pins each copy by digest to one foundation
commit. Change the foundation first, then copy the file and move the pin.
A top-level page shows its sub-sections through the shell's one view switch, declared in `window.PAGE_VIEWS`; the contract sits at the `view:start` block of `sd_dashboard/v2/static/shell.js`.

Writes are JSON POSTs under `/api/items`, `/api/notes`, `/api/jobs`,
`/api/assignments` and `/api/services`; GET and HEAD never
change workflow rows. The browser supplies its signed, expiring HttpOnly
SameSite=Strict session cookie and matching CSRF token, plus an exact same
Origin. Invalid input, stale revisions, unknown actions and untrusted hosts
are refused. No command or arbitrary file path is accepted by these routes.
`/api/queue/move` accepts a repository only when it is registered, and an action from a fixed list of nine.
`POST /api/notes/quick/add` writes outside the database: it runs `sd store add
sdw.quick-note` from the service's PATH as an argv list, once the text passes
the Notes rules in `notes_screen.quick_text` (sd:2549).
`GET /api/items/ID/capture-context` returns only the selected parent's identity,
status and current revision through the same authenticated boundary. Selection
loads that revision; submitting does not silently refresh it. A stale or deleted
parent requires refreshing the context while retaining the draft. An interrupted
capture response keeps submission disabled until the user checks the outcome
and explicitly enables a retry; a successful save stays disabled while navigating.

Sessions are bound to their origin and operator. HTTPS cookies use the Secure attribute.
The optional IP origin uses HTTP over the encrypted Tailscale connection, with its own origin-bound cookie.
Both remote modes verify the operator and current Tailscale configuration on each request.

The test suite exercises real HTTP capture, readback, completion and note
resolution against a disposable database, along with cross-origin/session
rejection and stale-browser conflict checks. Service tests use disposable
plists and a fake launchctl runner to check exact action sequences and refused
requests; Ports tests cover configured and observed listener evidence.
Browser/iPad usability is a
separate visual check; unit tests do not establish it.

The Backlog list component is `listing.py` — filter, selection and paging, in
that order, with the filter applied before the paging.

**The five legacy collectors** cover the vault, the launchd job table, research
builds and the service port map. Their CLI commands remain available, and the
workflow dashboard renders all five itself: Toolbox, Vault, Research and
Queues through `sd_tile.py` on Management, Notes and Tasks, and Ports in-process on Health. The
manifest no longer declares them as plugin tabs (sd:719 step 3). Retaining the
collectors does not imply feature parity with the old dashboard.

## Documents

**Documents** lists generated HTML reports and serves them whole. Everything
else on this dashboard reads database rows and renders markup; a report does
neither, because a generated report is already a finished page and the useful
thing is to hand it over rather than strip it down.

The page is `/documents`, the v2 Documents page (sd:2114). It reads
`GET /api/documents` (`documents_screen.document`), which lists every file
`/documents/<key>/<file>` serves with its title, h1, stand line, derived kind
(research or report) and render state. Its facets, search and paged ledger come
from the design source. Pin, hide and tag keep the workflow database's `view`
state through `POST /api/documents/view` (sd:3428); render and request are
copy-only lines the dashboard does not run. Its Books and Videos sub tabs read
`GET /api/documents/books` and `GET /api/documents/videos`, the vault's
book, article and video rows (sd:6260, sd:6261).
The classic listing was retired in sd:3402;
the page reads the roots through `documents.py`'s readers, so the rules below hold for it.

A domain repository that publishes reports here follows
`docs/html-reports.md` in this folder: a markdown twin and HTML page built
into its gitignored `docs/dashboard`, inline SVG, and a build stamp on each
page.

**The roots are found, not listed.** Any checkout under `REPO_ROOT` (default
`~/repos`, at the root or one group deep) that holds `docs/dashboard` is a
document root. Onboarding a repository is a directory in it and nothing else:
no line to add, and no list here to fall behind the fleet. `docs/dashboard` is
gitignored in the repositories that have one, because the pages are built
rather than tracked, so the enumeration reads the disk and never asks git.

The dashboard still knows nothing about any particular repository. A report
belongs to whatever generates it, and a fleet dashboard carrying the path to
one association's reports in its source would be wrong in a way that is
awkward to undo.

`documents.conf` remains, for the things a directory cannot say. It lives in
`<config>/project-dashboard/documents.conf` (`<config>` is
`$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`), outside the
checkout, because it names the operator's own repositories and paths; copy
`documents.conf.example` there to start one, and without it every found root is
listed under its directory name:

```
skip|<key>                       a found root the dashboard should not list
skip|<key>|<file>                one file of a root, neither listed nor served
root|<key>|<label>|<directory>   a root that is somewhere else entirely
img|<key>|<host>                 an image host the root's *.app.html pages may load
```

**A name is not a line here.** The derived label is the directory name, and it
reads badly for an abbreviation: `mcp-research` becomes `Mcp Research`. Rename
root on the Documents page sets a better one, and Hide root moves a root to the
page's Hidden roots fold; both are the root key's `view` row in the workflow
database (sd:3986). A `label|` line is no longer read, and the page names one
still in the file so Rename root can replace it.

`root|` is for a root that genuinely lives elsewhere, outside any checkout's
`docs/dashboard`, and it wins over a found root of the same key, its own
label included. The vault is the case it exists for: it is not a checkout
under `REPO_ROOT`, so `root|vault|Vault|~/Documents/My Vault/docs/dashboard`
is the only way its pages are served. Do not reach for it to rename one. A `root|` line
that repeats the path enumeration already found puts the default location back
into this file, where it goes stale the moment the repository moves while
still looking authoritative. That is precisely the drift the enumeration was
written to remove.

**A key with two owners has no owner.** The key is the checkout's directory
name, and two groups may hold a checkout of the same name — `org-a/reports`
and `org-b/reports`. Neither is served, and the listing names the contest and
the directories in it. Picking one would serve one repository's pages at
another's address; picking the first found would decide it by glob order, so a
new checkout sorting earlier would repoint an address that already worked. A
`root|` line naming the one you mean settles it, and a `skip|` line does too.
A name does not: it says what to call the root, not which directory
is the one meant, and that is the question.

**It serves rather than embeds.** Pulling a report's body into a page would run
it through the markup filter, which exists to flatten what a foreign document
brought with it. The filter is right and the report would be ruined: no
stylesheet, no chart. So each report gets its own address, `/documents/<key>/
<name>.html`, and arrives unchanged.

**That needs its own policy.** The shared `Content-Security-Policy` is
`default-src 'self'; script-src 'self'`, and with no `style-src` the fallback
refuses the inline stylesheet a standalone report carries in its head. The page
would render unstyled. A served document replaces that one header with
`default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:`,
which is tighter where it counts: the dashboard permits same-origin script and
this permits none. `_send` takes the policy per response and assigns it every
time, because the handler instance is reused across a kept-alive connection and
a policy left behind would apply to the next page down the same socket.

**A `*.app.html` page may run its own script, in a sandbox (sd:1502).** An
interactive page, such as a map, needs script, and the policy above refuses it.
A file named `<name>.app.html` opts in, and only its name opts it in. It is
served with `sandbox allow-scripts allow-downloads` and without
`allow-same-origin`, so it runs in an opaque origin: the `SameSite=Strict`
session cookie is not sent, storage is unavailable, and `/api/` refuses any
`Origin` other than the dashboard's own, `Origin: null` included. Its script is
`https://cdnjs.cloudflare.com`, which its tags pin with `integrity`, plus its
own inline scripts by SHA-256, measured from the bytes served. A digest rather
than `'unsafe-inline'` keeps an `onerror=` attribute smuggled in through the
page's data from running. Its images are `data:`, `blob:`, the script host, and
the hosts its root's `img|<key>|<host>` lines name; it has no `connect-src`, no
form and no frame. Any other file keeps the no-script policy, whatever it
carries.

**A found root may not reach past its checkout.** A `docs/dashboard` that is a
symlink out of the repository is not a root, because enumeration moved the
decision from a person to a glob: serving a directory outside a checkout is a
thing somebody asks for with a `root|` line, and a symlink is not somebody
asking. `within()` cannot catch it — it resolves the link and trusts the
result, which is the right rule for a declared root and no rule for a found
one. A symlink *inside* the same checkout is fine, and a declared root outside
every checkout, like a vault, is unaffected.

**Three conditions, all of them, before a byte is read.** The key must name a
configured root exactly; the file name must match `[A-Za-z0-9][A-Za-z0-9._-]*\.html`,
which contains no separator, so traversal is unsayable rather than defended
against; and the resolved path's parent must still be the resolved root, which
is what a symlink cannot fake. Every failure is the same 404, because
distinguishing "no such root" from "outside the root" tells a prober which of
the two they reached.

`documents.within()` is that rule, and the listing and the server both call it.
They have to agree: a listing offering a link the server then refuses is worse
than one that omits the file, because the reader believes the first.

The route sits ahead of the database connection in `do_GET`, beside `/static/`,
so a report stays readable when the workflow database is down. The listings do
not: `/api/documents` sits behind it like every other page. That is the right way
round, because the report is the thing somebody needs in front of them during
an outage.

A configured root that does not exist on this machine is reported by name
rather than hidden. `documents.conf` is shared across machines and a root will
often be absent on one of them; a section that silently disappears looks the
same as a report that was never generated. A *found* root cannot be absent, so
this applies to the `root|` lines only.

## Designs

**Designs** lists the HTML mockups in the ui-design checkout and serves them
for review, for example from an iPad over the dashboard's Tailscale address.
The checkout is `DESIGNS_ROOT`, by default `$REPO_ROOT/platypeeps/ui-design`.
The listing shows every `.html` page under `products/`, one section per product.

The page is `/designs`, the v2 Designs page (sd:2126). It reads
`GET /api/designs` (`designs.ledger`): each drawn page's kind, last commit and
screenshot state, and each product with a brief and no page. History, Retake
screenshots and Read brief are copy-only lines the dashboard does not run.
Hide and Unhide (sd:3447) post `/api/designs/view`, the same view state as
Documents' Hide, keyed `designs/<path>` or `designs/product:<name>`; the Hidden
lamp shows the hidden rows, and a hidden row leaves the other lamps and the badge. The
classic listing was retired in sd:3402; [`docs/pages/designs.md`](docs/pages/designs.md) has the detail.

A mockup is served at its path in the tree, under `/designs/`. Its relative
links, such as `../../../foundation/tokens.css`, therefore resolve to files in
the same checkout. `designs.resolve()` is the one rule, and the listing and the
server both call it:

- each path segment starts with a letter or a digit, so `..`, `.git` and dotfiles are refused;
- the extension must be in `designs.TYPES`, which also sets the content type;
- the resolved file must stay inside the resolved checkout, so a symlink out is refused.

Every refusal is the same 404.

A mockup page runs under `designs.POLICY`, which starts with `sandbox allow-scripts`.
Its script runs, so the drawer and palette can be tried, but its origin is
opaque: it reads no dashboard cookie and calls no dashboard endpoint.
`connect-src` and `form-action` are closed too. Because of the opaque origin,
the page's own CSS and images load as cross-origin requests. Asset responses
therefore carry `Cross-Origin-Resource-Policy: cross-origin` through the
handler's `_overrides`; every other header stays the dashboard's.
A font also carries `Access-Control-Allow-Origin: *`, because a browser fetches
fonts in CORS mode. No other file gets it: the data scripts beside the pages
hold real notes and mail, and any site could then read them from this port.

The v2 Designs mockup loads `products/system/designs/pages/data/designs-data.js`.
The tab answers that one path live, with `designs.ledger_script()`, instead of the committed file.
`designs.ledger()` ports ui-design's `tools/collect-designs.mjs` and lists:

- per page: kind, title, bytes, last commit (sha, time, subject) and a dirty flag;
- per page: its screenshots, each one's commit time and PNG size, and which are stale;
- per product: its brief path, title and **Status:** line, including a product with no page;
- `caution`: pages uncommitted, without a screenshot, or with a stale one.

A v2 screenshot is stale when `shots/inputs.json` lacks it or records another inputs hash.
`designs.inputs_hash()` ports `tools/inputs.mjs` byte for byte; `test_designs.py` pins Node's output.
Every page and screenshot the ledger names passes `designs.resolve()`.

## Watches

The page is `/watches`, the v2 Watches page (sd:6264). It reads
`GET /api/watches` (`watches_screen.document`): every `sdw.market` row with
its status and its last refresh date, read from its profile, and the
`market-watch` job's state and last run. Refresh all runs that job now;
Refresh and Add watch run `watch.refresh` and `watch.add` through
`/api/actions/run`. [`docs/pages/watches.md`](docs/pages/watches.md) has
the detail.

## Legacy collector reference

Until 2026-09-01 this directory ran an earlier HTML dashboard on
`127.0.0.1:8767` from login. The parts of that implementation about *serving a
page* — `dashboard.py`, the LaunchAgent, the CSS and icons, `tailscale serve`,
the ack store, the database write path — were deleted rather than ported. What
could not be ported is what stayed: these collectors read a vault and a machine
that only exist here, so they remain system-owned.

The port went to the framework dashboard in `sd-workflow` for the week
that followed, and **came back**: the workflow dashboard at the top of this file
serves `127.0.0.1:8767` today, from this directory, and `dashboard.sh` says so
in its own help. So "the dashboard moved to the pack" describes one week of
2026-09 and nothing since. The plugin contract described below belongs to that
framework dashboard, which is historical as the deleted HTML dashboard above is:
of the three surfaces named here, only the workflow dashboard is served.
`sd-plugin.json` stopped declaring `tile` and `tabs` on 2026-09-13, before the
pack deleted the loader that read them, and its queue actions on 2026-10-18
(sd:5134): it declares only its `prefix`.

## Usage

```sh
./dashboard.sh tile toolbox     # one tab as JSON on stdout
```

The framework dashboard's plugin loader read the six tab names and the tile
command from `../sd-plugin.json` and ran one of these commands per tab per load.

## Files

| File | What it is |
| --- | --- |
| `sd-plugin.json` (repo root) | the manifest: the `sys` prefix and nothing else |
| `dashboard.sh` | collector verbs, workflow server, runtime installer and tests; `<config>/project-dashboard/.env` selects the legacy collector interpreter |
| `sd_tile.py` | one tab per invocation — the part that turns a collector's output into a table |
| `collectors.py` | the collectors themselves. A library with no entry point: everything that started something went with the server |

## Tabs

| Tab | What it shows | Where it comes from |
| --- | --- | --- |
| **Toolbox** | Cron jobs with launchd's own last-exit code **and the next time each one fires**, launch agents, docker containers, and `machine-setup` drift with the age of the measurement | `launchctl list`, `jobs/*.job`, `docker ps`, and the status part of `machine-setup-update-nightly`'s log rather than a fresh `machine-setup.sh status`, which cost most of the five-second budget |
| **Vault** | One card per `* Home` area with its note count, plus Inbox pressure | vault frontmatter |
| **Research** | Every checkout carrying a `research.conf.py`, its documents, and whether the rendered HTML is **fresh**, **stale** or **not built** | the conf itself, plus the built page's mtime in `docs/dashboard/` (or the older `build/`) |
| **Ports** | Each service's effective ports, and which of them clash with another candidate or are already held — read from `machine-setup.sh`'s own conflict lines, which the collector could not see until 6b-9 | `machine-setup.sh candidates service` |

A cron job's last exit code says the last run was fine; it cannot say the job
has quietly stopped firing. The **next run** column is computed from each job's
own crontab expression, so a job that should have fired hours ago and did not
is visible without waiting for the watchdog.

The **Ports** tab exists because `.claude/rules/services.md` says the port list in prose goes
stale and to ask the machine instead. This asks the machine on every load.

## Queues reads; Tasks writes

The Queues view only counts. A base row's stage moves on Tasks, which offers
each row's moves under its kind's rules (`records.rules_for`) and writes them
through `records.edit_row`. The view reads the rows from the workflow database,
their source since pack sd:4627, and reads no vault note (sd:5134).

## How it works

- **One invocation per view.** Each machine or vault view runs `sd_tile.py <name> <since>` as
  a child (`<since>` is the view's start on `CLOCK_MONOTONIC`, so the tile's
  deadline counts from it), under five seconds and 64 KB (`reports_screen.collect`, reading
  through `collectors.Budget`), as the pack's loader once ran
  `dashboard.sh tile <name>` per declared name. Not a style choice: run
  behind one command these collectors take 6.66 s together and would be killed
  on every load, permanently, while each of them fits the budget several times
  over.
- **Exit codes follow the retired plugin contract** — 1 when a collector
  failed, with the reason on stderr, 2 when the tab name is wrong. The view's
  route answers 503 with that reason, and the page shows it in place of the view. Nothing but the payload may
  reach stdout, so `cmd_tile` execs.
- **Data, not markup** (pack sd:7527). A tile prints sections: figures, a
  heading, a table or a listing, each cell text with an optional title, http(s)
  link or numeric flag. `reports_screen.collect` refuses any other shape, and
  the page draws it with `markup.tile`, which escapes every value.
- `run()` gives every child its own process group and kills the group on
  timeout. `subprocess.run()` kills the direct child only, so a grandchild
  holding the inherited stdout pipe — `machine-setup.sh`'s nested `python3`,
  stopped on a TCC prompt — hangs the call long past its timeout.

## macOS TCC, and the two interpreters that read the vault

The vault is in `~/Documents`, so the `python3` that reads it needs the same
one-time Documents grant every LaunchAgent that reads it needs. Three things follow, the
first two learned the hard way on 2026-08-28 when the page sat on
*collecting…* for 27 minutes:

- **Homebrew python, not Xcode's.** Not by preference — by elimination. TCC
  attributes a vault read to the executing binary (verified with a launchd test
  harness: `ls` with no grant fails even under an FDA-granted shell; Homebrew
  python with its grant passes under plain `sh`), and macOS simply ignores user
  FDA grants for Xcode's `/usr/bin/python3`: granted as `Python.app`, as the
  inner `MacOS/Python` binary, toggled off/on, and after
  `tccutil reset SystemPolicyDocumentsFolder com.apple.python3` — every variant
  still gets `Operation not permitted` (2026-08-29). The price of Homebrew is
  that `brew upgrade python` moves the Cellar path and drops the grant.
- **Two interpreters run `sd_tile.py`, so two binaries can need the grant.** A
  machine or vault view runs the tile as a child of the server, under the server's own
  `sys.executable` — `SD_DASHBOARD_PYTHON`, the command pack's
  `.venv/bin/python`, which is Homebrew python@3.13 and execs that framework's
  `Python.app/Contents/MacOS/Python`. `dashboard.sh tile` runs
  it under `DASHBOARD_PYTHON` from `<config>/project-dashboard/.env`, today
  `/opt/homebrew/bin/python3`, which is Homebrew python@3.15 and execs a
  different `Python.app`. `DASHBOARD_PYTHON` therefore does not govern what the
  dashboard's own pages read. The server's path is the one the dashboard
  exercises, and it works: on 2026-09-14 its Briefs view rendered 96 rows from
  the launchd server with no refusal, so the python@3.13 binary holds the
  grant. Whether the python@3.15 binary holds it under launchd has not been
  measured — a shell has Documents access of its own, so running the tile by
  hand cannot answer it. `dashboard.sh grants` is the measurement: it probes
  both interpreter paths for the vault read and prints one line each, naming
  the path that lacks the grant. It also says when its own answers are not
  the binaries' — from a terminal `/bin/ls`, holding no grant, lists the
  vault too, and the verb exits 3 rather than reporting a pass, and 3 again
  when the control gave no conclusive result: it never started, the call on it
  failed, or it came back nonzero saying something that is not an access
  refusal. Run it from launchd, where a child's access is its own, for a
  conclusive exit 0 when both paths hold the grant. One other run reaches 0
  (sd:845): a control still waiting when its timeout ran out is read as one
  kept out, which is what an ungranted read looks like where nobody can answer
  the prompt — it does not fail, it waits. Coming back unsettled and running
  out the clock are not one answer: the first is the 3 above, and only the
  second carries the lines it printed. So a `/bin/ls` wedged for some reason
  of its own passes the interpreters' answers through, and that needs a wedge
  that reaches `/bin/ls` and not the vault — anything wider silences the
  probes too, and a silent probe is a refusal and exit 1 — and the line
  prints the wait it passed on, so the exit is never the only thing said.
- **The LaunchAgent starts the server under `/bin/bash`** (sd:6444). Its
  `ProgramArguments` are `/bin/bash -c '"$0" "$@"; exit $?' dashboard.sh serve …`;
  the trailing `exit` keeps bash from exec'ing python, so Apple's signed bash
  stays the responsible process for disk access, and a `brew upgrade python`
  that drops the python grant does not drop the server's. Check it with a TCC
  `AUTHREQ_ATTRIBUTION` log line for a server child: it names
  `responsible=com.apple.bash`.
- **The vault read is probed in a child first** (`vault_blocked()`, 15 s).
  Nothing spawned by an agent can click a TCC prompt, so an ungranted read does
  not fail — it waits forever. The probe turns that into an error on the tab and
  caps the cost at 15 s instead of 1605.

## Environment

| Variable | Default |
| --- | --- |
| `SYSTEM_TOOLS_CONFIG` | `${XDG_CONFIG_HOME:-~/.config}/system` — config root: `project-dashboard/.env`, `project-dashboard/documents.conf`, and the cron job files Toolbox reads from `cron-jobs/jobs` |
| `CRON_JOBS_EXTRA_DIRS` | none — further job directories Toolbox reads, as `cron-jobs.sh` does |
| `DASHBOARD_PYTHON` | PATH `python3` (set to Homebrew in `<config>/project-dashboard/.env`); the interpreter for `tile`, and one of the two paths `grants` probes |
| `SD_DASHBOARD_PYTHON` | command pack `.venv/bin/python`; runs `serve`, `preflight`, `install`, `health` and `grants`, and so the tiles a machine or vault view renders |
| `OBSIDIAN_VAULT` | `~/Documents/Obsidian Vault` — read by `collectors.py` itself; kept once in `<config>/shared.env` |
| `REPO_ROOT` | `~/repos` — read by `collectors.py` itself; set it in `<config>/project-dashboard/.env` |
| `SYSTEM_TOOLS_LABEL_PREFIX` | `local.system-tools` — launchd label prefix, kept once in `<config>/shared.env`; the dashboard's LaunchAgent is `<prefix>.sd-dashboard`, and Toolbox reads cron jobs as `<prefix>.cron.<job>` |
| `JEV_JOB_TRIAGE` | unset (on) — `0`, `off`, `false`, `no` or `disabled` switches off the failed-job Jev shadow |

`dashboard.sh` exports whichever of `REPO_ROOT`, `SYSTEM_TOOLS_CONFIG` and
`JEV_JOB_TRIAGE` are set after sourcing `<config>/project-dashboard/.env`, plus
`SYSTEM_TOOLS_LABEL_PREFIX` and `OBSIDIAN_VAULT` as `<config>/shared.env` gives
them, and `install` copies them (and `CRON_JOBS_EXTRA_DIRS`) into the
LaunchAgent, so the server reads the same values. The
Vault tile lists the vault's top-level `<Area> Home` folders, found on disk.

The earlier server's knobs went with it: the port, the refresh interval, the
tailnet bind, the allowed hosts, the ack file and the Jira credentials were all
about serving that page. Current workflow runtime settings are documented
above and in [RUNTIME.md](RUNTIME.md). `OBSIDIAN_VAULT` and
`DASHBOARD_REPO_ROOT` are gone too, and were never read on this path — the
tile has always taken the vault and `REPO_ROOT` straight from the environment.
The vault's old name `VAULT` is gone (sd:3990): `OBSIDIAN_VAULT` is the one name.

## What is left behind on disk

`logs/` and `state/` hold no tracked file at all now -- their `.gitkeep`s went
with the writer that needed the directories, and what is in them is ignored by
`**/logs/*` and `**/state/*` in the repository root's `.gitignore`, as it
always was. `state/acks.json` holds what the old **Needs you** tab had been
acknowledged for; nothing reads it any more, and the new dashboard has no ack
store yet, so it is left in place rather than deleted. The same goes for the
old server logs.
