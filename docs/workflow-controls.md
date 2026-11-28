# Workflow controls and local rollout

The installed-state details below record earlier rollouts on 2026-09-08.
They do not establish the currently loaded build or complete the coordinated closeout.
Verify installation, process identity and live acceptance after delivering matching commits across all three repositories.

The workflow dashboard is installed at **http://127.0.0.1:8767** and privately at
**https://hub.example.test:8443**. Remote access requires the Tailscale login
`operator@example.test`. The command pack, system repository and writing pack
share the installed `sd_db` library and `~/.local/share/sd/sd.db`. Ordinary work
progress belongs to the database.

## Daily use

| Need | Control |
| --- | --- |
| Capture work without a PRD | Today, or `sd task add "Title"` — it takes the registered checkout enclosing cwd; `--belongs-to PATH` files it in another registered checkout, as `edit` moves one; `--no-repo` files one that belongs to none |
| Prioritize, schedule, add details | Item screen, or `sd task edit ID` |
| Move a task or followup to the right checkout | `sd task edit ID --belongs-to PATH` — `.` names the checkout enclosing cwd; `--no-repo` leaves it belonging to none, as it does on `add`. An unregistered path is refused, and the move is recorded as an item note. The move prints the checkout it landed in; ordinary output names a row's checkout only when it is not the one you are standing in |
| Clear a stale branch name | `sd task edit ID --clear-branch` — sets the row's branch to none and records an "Updated branch" note. An in_progress item is refused, because `sd-docs-lint` wants it to record a branch; use `--branch` |
| Record or replace an item's branch | `sd task edit ID --branch NAME` — sets the row's branch and records an "Updated branch" note; the build records one when it claims the item. A row with an active assignment or a retained runner lease is refused (sd:7659) |
| Park an item by hand | `sd task edit ID --park` — stamps the park now, as the nightly prune does for an untouched P4 item, and records a "Parked" note; `--reason TEXT` adds the reason to that note as `Parked by WHO: TEXT`, so a job can find its own parks. A done item is refused; an item already parked keeps its first stamp. A writing piece parks with `sd writing park` |
| Bring back a parked item | The dashboard's Unpark control, or `sd task edit ID --unpark` — clears the park and records an "Unparked" note. A writing piece revives with `sd writing park --revive` |
| Reclassify an item filed as the wrong kind | A task's item screen, or `sd task edit ID --kind KIND` — the kinds are `sd_db.workflow.HAND_KINDS`, the same four `sd task add --kind` offers. A move to `work-idea` or `personal-idea` carries no repository, so a row that has one needs `--no-repo` in the same command. A `followup` keeps its repository (sd:809). `edit_item` refuses a produced row (a contribution or skill-review task), an `idea` that is a writing piece, and a row with a queued, running or ending assignment, and records every change as a "Changed kind" note naming who made it |
| Record what an item waits on | `sd task edit ID --blocked-by M`, repeated for several, sets the set and replaces the old one; `--clear-blocked-by` drops it (pack sd:9570). A done item, the item itself, a missing item or a cycle among open items is refused. No build takes the item while one it names is not done: the press refuses, auto-pickup skips it, and a queued build whose dependency reopened ends blocked at its start. A done writes nothing; the next auto-pickup tick takes a ready item. `sd task show` prints `waits on:`; the item's Details list it and its Queues row says `waits on sd:M` |
| Change task status | Item screen, or `sd task status ID STATUS` |
| Close or reopen an idea | Item screen, or `sd task status ID done --reason TEXT` and `sd task status ID planning` — for a `work-idea`, a `personal-idea` and an `idea` that is no writing piece and no vault row (`sd_db.workflow.hand_idea`); the reason is optional |
| Close a task or followup nobody will do | `sd task cancel ID --reason TEXT` — `done` with a `cancelled` receipt, as `sd work cancel` writes; a finding carried to the row reopens |
| Record and resolve a followup | Item notes, or `sd task note ID TEXT --kind followup` / `sd note resolve NOTE_ID` — a note on a done item prints an advisory naming its delivery, and still lands |
| Answer a question | The Decisions buttons, or `sd task note ID TEXT --kind decision --answers NOTE_ID` — records `Ruling on note #NOTE_ID: TEXT` and resolves the question in one write; it refuses a note that is not an open question on that item, and a review batch's question |
| See the same inventory as the dashboard | `sd today` — a one-line summary, then ten tables in one order: attention, operations on this machine (each open report line that names this host, then the gate line), decisions (each with the note id `--answers` takes), landing now (position and the time each entry started or was queued), delivered in the last 24 hours by a merged PR or verified delivery (PR, merge commit, landed time, then a `+N closed by hand` line), planned (due after today), blocked, unscheduled, followups, each capped at five rows (`--all` lifts the cap), then all due and active work. An empty table prints its heading with `(0)`. `--only NAME`, repeatable, keeps one repository's rows by short name, or `fleet` for items in none, in every section, count and `--json`. `sd today --json` and `sd store items --json` give the work rows; `sd_db.today` gives every section as plain rows; the `sd-next` skill turns it into the operator's status |
| Relink a moved work artifact | Item screen, or `sd work relink ID PATH` |
| Cancel repository work | Item screen, or `sd work cancel ID --reason TEXT` |
| Record verified code delivery | `sd work deliver ID FULL_COMMIT_SHA` |
| Close a work item, task or followup whose merge carried `Item:` without `Delivers:` | `sd work deliver ID FULL_COMMIT_SHA --associated --reason TEXT` |
| Promote an idea to a registered writing piece | `sd writing promote ID [--slug SLUG]` — the row keeps its id and becomes piece `YEAR/slug`, scaffolded from the repository's `templates/piece-template.md`; the writing checkout it runs from is the target, and elsewhere the one repository that registers pieces is |
| Move a writing piece through its stages | Writing screen, or `sd writing stage --piece YEAR/slug --stage STAGE` |
| Inspect current writing evidence | Item screen, or `sd writing readiness --piece YEAR/slug` |
| Run the adversarial gate on a draft | `sd writing adversarial --piece YEAR/slug` — renders the shared `writing-draft` lens, runs codex read-only on the checkout, and writes `adversarial.md` stamped with the draft it read; `sd writing gate` records the verdict |
| Mark a review companion current after a revision | `sd writing reconcile --piece YEAR/slug --artifact adversarial\|research --note-file PATH` — refuses an adversarial reconcile once more than 25 % of the draft is prose the last run did not read |
| Park or revive a piece | Item screen, or `sd writing park --piece YEAR/slug [--revive]` |
| Observe jobs and assignments | Operations → Jobs, `sd jobs list`, `sd assignments list`, `sd assignments get ID [ID ...]` |
| Wait until builds end | `sd assignments get ID [ID ...] --wait [--timeout MIN] [--until merged]`; polls until each has left queued, claimed and running; `--until merged` also waits out a done build's lane entry and exits 0 only when each merged; exit 1 at the timeout (default 240 minutes) |
| Inspect launchd services | Operations → Services, or `sd services list` |
| Start, stop or restart an eligible user service | Operations → Services, or `sd services start/stop/restart LABEL` |
| Inspect configured ports and observed TCP listeners | Operations → Ports |
| Find items by age in status | Operations → Progress; a bar opens matching Backlog items |
| See weekly numbers, cost and providers | Operations → Usage |
| Request a supported retry or stop | Operations, `sd jobs retry NAME`, `sd jobs cancel NAME` |
| Start a build: the one verb (sd:9063) | `sd assignments build ID [ID ...]`; a planning item is set ready first; an item whose bundle ended blocked or cancelled is retried, worktree kept; one whose lane entry failed is Fix, the lane's reason its finding; `--finding TEXT` is Send to builder, on a fresh build too, refused where it would start a design run; one refusal refuses the selection |
| Take one item out of an ended bundle, lead included (pack sd:9709) | `sd assignments build ID --drop`, on the host that built it: the kept items' commits are cherry-picked onto a fresh branch and worktree from the base under a kept lead, the item builds alone, and its old commits and files stay in the old branch and worktree; a commit that cites both sides, or one that cites no item, refuses the drop |
| Run one build-dispatch tick: reap, lane watch, claim, spawn | `sd assignments dispatch`; the hub's job runs it |
| Supervise one build run, or list the live sessions | `sd assignments dispatch --supervise RUN`, `sd assignments dispatch --sessions` |
| Cancel a queued assignment or a running build | Operations, or `sd assignments cancel ID`; a queued or running build's items are parked, so auto-pickup skips them until Unpark (sd:9222); a running build's answer names the pending request in `cancel_requested`, and a repeat refuses until the run stops (pack sd:9375); a blocked row's cancel parks nothing; a queued lead takes its queued members with it, and the answer lists each member's row under `members` (pack sd:9266); a member cancelled on its own leaves its bundle, and once the bundle ran, its next run holds the branch before a builder starts if the branch carries earlier work, as after a subset Retry (pack sd:9709); a lane entry on the bundle's branch drops a citation of it and lists it under `## Left open`, and refuses it as the entry's item, an `--item` or a retried saved close (`member_cancelled_alone`); the run checks again before prepare and before the merge, and fails the entry with that code; `sd-ship merge` enforces it right before it publishes, on every path |

Capture defaults to Task. Followup, Comment, Question, Decision and Proposal
require a Related item; the short inline CLI hint follows that selection.
`sd task note` defaults to Comment and accepts all five note kinds through
`--kind`. Use `--kind followup` for an action that should appear in Today.

Two of those five words are also item kinds, and Capture's list is the note
side of both. `sd task note <item> --kind followup` hangs a followup off an
item and resolves against it; `sd task add --kind followup` files one that
has no parent item and is worked in its own right, and it needs no Related
item because there is no parent. The test of which half you want is whether
the followup should survive the item it was filed against being deleted: the
note does not, the item does. The item half is otherwise a task -- it takes
the registered checkout enclosing cwd (`--no-repo` files it against none), and
`sd task edit` changes its title, body, priority, due date and checkout.
`proposal` is the same collision with only one live half: the
note exists, the item kind is reserved and nothing creates it, so neither
`sd task add --kind` nor `sd task edit --kind` offers it. The other three --
Comment, Question, Decision -- are notes and nothing else. [docs/sd-db.md](sd-db.md#the-item-kinds-and-the-two-words-called-followup)
holds the full table of both closed lists.

The `sd` launcher is installed at `~/bin/common/sd` and is also managed by the
system repository's bin-links installer.

Run writing commands from the root of a content repository: the checkout that
holds `content/`, `profile/` and `templates/`. JSON item responses carry a revision;
`--if-revision` requires the state you inspected. Browser forms always carry it.
Parked pieces and their followups stay out of Today, Backlog and resumed-session
followups. The Writing view can show parked pieces explicitly and revive them.

## Contributions

Contribution metadata belongs to a Task in the shared database. It can exist
before a pull request. Filing the contribution preserves that item's ID,
notes, local evidence, and workflow status.

```bash
sd task contribution add "Upstream contribution" --file contribution.json --json
sd task contribution edit 42 --file changes.json --if-revision ITEM_REVISION --json
sd task contribution list --json
sd task contribution show item:42 --json
sd task contribution ack item:42 --event EVENT_ID --if-revision CHECKPOINT_REVISION --json
sd task contribution dismiss github:https://github.com/OWNER/REPO/pull/123 --if-revision CHECKPOINT_REVISION --json
```

Use `dismiss` for an observed row with no item that will never settle, such as
a pull request whose repository was re-created and now 404s. It acknowledges
the row's active events and moves it to `closed`. New attention shows it again
until acknowledged; a lifecycle change (open, closed, merged) ends the dismissal.
Nothing dismisses a row automatically. A row with an item closes through its task.

Use `edit` to attach metadata to an existing Task. Use `sd store item 42 --json`
for its current item revision. A changes file is a UTF-8 JSON object, limited
to 64 KiB. Unknown fields, invalid dependencies, stale revisions, and invalid
local evidence refuse the operation. Duplicate JSON keys also refuse.

A contribution row carries exactly one identity: a `pull_url`, an `issue_url`,
a local clone plus branch, or an issue draft. A row carrying both `pull_url`
and `issue_url` is refused.

Unfiled *branch* work requires the local clone and branch. Supply the tested
commit and evidence group to record verified test results. Missing evidence
remains explicitly unverified. A filed contribution supplies `pull_url` or
`issue_url` instead of the clone and branch, and never both of them.

```json
{
  "local_clone": "/absolute/path/to/clone",
  "local_branch": "contribution-branch",
  "tested_commit": "FULL_COMMIT_SHA",
  "evidence": [{
    "argv": ["python", "-m", "unittest"],
    "cwd": "/absolute/path/to/clone",
    "exit_code": 0,
    "commit": "FULL_COMMIT_SHA",
    "artifact": "/absolute/path/to/test.log",
    "sha256": "LOG_SHA256"
  }],
  "blocked_on": "Waiting for the dependency release",
  "depends_on": [{"kind": "item", "item": 43}],
  "blocking_labels": ["blocked"]
}
```

Replace placeholders with actual evidence. Registration checks the branch
commit and evidence file hashes. Stored command arguments are data; registration
never executes them. Nonzero test results remain recorded as failures.

Filing needs a changes file such as
`{"pull_url":"https://github.com/OWNER/REPO/pull/123"}`.

An upstream issue is the same row with `issue_url` instead:
`{"issue_url":"https://github.com/OWNER/REPO/issues/123"}`. An issue you have
written up but not filed anywhere supplies `target_repo` (`owner/repo`),
`draft_title`, and `draft_path` -- an absolute path to the draft body with its
SHA-256 digest, re-checked on read the way evidence artifacts are, so the draft
that was reviewed is the draft that gets filed. All three are required
together. Filing it later preserves the item ID, its notes and its status,
exactly as filing a branch does. The word for that state is `draft`: "unfiled"
already means a contribution with no `pull_url`, which demands a clone and a
branch an issue draft does not have.

`depends_on` also accepts a merge dependency with `kind` and the exact PR `url`.
A release dependency uses `kind: "release"`, `repo: "OWNER/REPO"`, `tag`,
and `contains_pull`. An optional `package` object binds its `name` and `version`.
Merge alone does not prove release inclusion or package publication.
An unknown release requirement remains unresolved.
An issue dependency uses `kind: "issue"` and the exact issue `url`; it resolves
when that issue is closed as completed, or when a referencing pull request
merges. A close as not planned resolves nothing, because not planned is a
refusal rather than an answer. All dependencies must resolve
before the contribution becomes newly unblocked.

The ordered list uses the shared dashboard projection. It keeps unknown and
stale observations visible. Local task completion remains an explicit task
operation; an upstream merge does not complete the task, and neither does an
upstream issue closing. Any row whose last observation reads closed takes the
terminal `closed` lane, which sorts after every open row: a closed issue, and a
pull request closed without merging, which awaits no one. A later observation
that reads open returns that pull request to `awaiting_them`; a merged pull
request stays in `merged`. Active attention still wins: a row with an
unacknowledged event, such as `Closed without merge`, stays in `awaiting_you`
until that event is acknowledged.

Each row's `attention_sources` identifies its checkpoint key, revision, and
event IDs. PR attention uses `github:https://github.com/OWNER/REPO/pull/123`;
issue attention uses `issue:https://github.com/OWNER/REPO/issues/123`;
dependency attention uses `item:42`. An issue draft has no second key: an
unfiled row lives under `item:ID` alone until it is filed. These checkpoints
have separate revisions.
Inspect the matching key with `show`, then acknowledge its explicit event IDs.
Repeat `--event` for multiple events from that checkpoint. A stale checkpoint
revision refuses acknowledgement. Acknowledgement does not erase source history
or claim that a notification was delivered.

## Authority and review

Task completion requires no commit, pull request or GitHub issue. Repository
work has separate cancellation and delivery operations. Delivery checks the
full commit, delivery trailer and current default branch before recording its
receipt and shipment time together. An outdated external issue or branch name
cannot authorize completion or hold ordinary tasks open.

Suggestions default to local database rows. Publishing one as an sd item of
its own is an explicit action; no pack surface files a GitHub issue. External tracker snapshots carry their sync freshness
and remain context; they do not become the local progress authority.

Planning artifacts remain optional for small changes. Local code review stays
bounded by the existing review policy; status-only commits and bookkeeping
pull requests are not required to finish ordinary work. A stale review or a
blocking finding still requires evidence or a recorded disposition.

Writing readiness binds research and review evidence to the current draft and
review generation. Corrections invalidate prior readiness. An unstamped report
requires the digest actually reviewed; recording a verdict cannot silently
bless a changed draft. Publication is not a generic dashboard action.

## Installed state and recovery

The schema 2-to-3 migration preserved all 10 existing items exactly. The live
writing cutover imported 22 indexes, including 7 parked pieces, and verified
22 rows with zero differences. It removed only frontmatter status/publication
bookkeeping. Draft text, research, reports, article moves, concurrent commits
and Git history were preserved. Routine writing progress now changes rows,
not tracked content files.

Recovery material remains at:

- `/private/tmp/sd-workflow-implementation-kl1o4lht/backups`: original source
  patches/files, consistent database snapshots, and previous installed library.
- `~/.local/share/sd/writing-cutover-pmpw73si`: the original 22 writing indexes.
- `~/.local/share/sd/runtime-backups/`: prior LaunchAgents and exact installation
  plans. Each replacement keeps its own directory.
- `/private/tmp/sd-workflow-implementation-kl1o4lht/acceptance`: rehearsal
  results, apply manifests, CLI/HTTP acceptance tests and verification reports.

`sd writing recover` reconciles an interrupted cutover journal. It restores
only files still matching this operation's output and preserves concurrent
edits for explicit reconciliation. Database restore stages and validates the
snapshot before replacing a live database; it does not unlink live WAL files.
An older backup can lack post-cutover progress. `sd restore reimport REPOSITORY`
now previews recovery from verified historical source evidence. Applying that
preview requires its `--if-fingerprint` value. Missing or unproven sources
refuse recovery; the command cannot invent progress absent from the surviving
evidence. Retain backups and reconcile authority before resuming dispatch.

`sd shadow sync --strict` fails when any *configured* tracker's interval
remains incomplete; a tracker whose variables are unset is reported as not
collected and does not fail the run. It retains collected rows and holds the
cursor for a later retry. The recovery flags bound GitHub only: given either,
every other tracker prints `skipped (recovery window is GitHub's)`.
Bound recovery with `--since` and `--until`, using timezone-aware ISO timestamps
such as `2026-09-06T10:00:00Z`. Bounds are inclusive UTC seconds; fractional
seconds round down. The end cannot exceed the current time. Equal bounds
request one second. Complete historical intervals retain a later cursor.
Complete intervals separated from the cursor also retain it, preserving the
uncovered gap. Both cases report successful coverage and cursor retention.

`--max-requests` accepts a positive integer. `--max-seconds` accepts a positive
finite number. Omitted controls use the library defaults. Invalid controls
refuse before database access. If coverage exceeds these limits, retry a
smaller interval or increase the limits; strict mode continues to report
failure until coverage is complete.

Two refusals print one `sd:` line on stderr and collect nothing more.
Exit 3 means a satellite ran the verb; run it on the hub.
Exit 4 means another shadow sync holds the lock; retry when it finishes.

The dashboard launcher uses the command pack's installed library in an isolated
interpreter. Health verifies schema, loaded process identity and hashes of the
running build; changing installed code requires a restart. Provisioning refuses
to replace a newer schema library with an older committed system version.
Deliver matching committed versions of all three repositories and verify a
fresh installation before activating the coordinated rollout. A source commit
or merged pull request alone does not establish the installed runtime.

## Verification on 2026-09-08

- `bin/sd-check --json`: `"status": "pass"`, 1,893 tests, 100% installer
  coverage, clean Ruff and mypy, and no security findings (4 existing workflow
  suppressions). The parked-followup query initially failed the security scan;
  static queries fixed it, and the complete check passed on the final source.
- Shared database suite: `Ran 445 tests in 83.292s`, `OK`. Final dashboard
  suite: `Ran 207 tests in 31.849s`, `OK`. Writing CLI/HTTP acceptance:
  9 tests passed.
- Operations acceptance: 5 tests passed using a controlled launchd fixture.
  No live job was retried or stopped. Daily workflow CLI/HTTP acceptance:
  8 tests passed.
- Live health: `ok=true`, `code_changed=false`, schema 3. Database integrity:
  `ok`; inventory: 32 items, 22 writing pieces, 7 parked.
- Live Today returns the same 16 item IDs in the same order through CLI and
  HTTP, with zero parked items. Browser inspection covered the 820px tablet
  layout; the temporary viewport was reset afterward.
- `git diff --check` passed in all three repositories. Source commits remain
  unchanged by this rollout; the user's concurrent writing commits were kept.

The final pack report and full unittest log are in the acceptance directory as
`operations-tabs-pack-check.json` and `operations-tabs-pack-unittest.log`.
The dashboard output is `operations-tabs-dashboard-tests.log`. The temporary
launchd lifecycle acceptance is `live-service-controls.json`: start observed
running, restart observed a new PID, stale stop refused, and final stop observed
unloaded. The fixture was cleaned up; no existing user service was controlled. Live CLI/HTTP
comparison and build identity are in `live-final-verification.json`.

## Private access rollout

Private access was explicitly approved and enabled on 2026-09-08. The new
`~/.config/sd/dashboard-private.json` is mode 0600 and names the exact HTTPS
origin and operator above. The LaunchAgent passes this configuration explicitly;
the previous local-only `dashboard.json` remains unchanged for rollback.
The installation backup is
`~/.local/share/sd/runtime-backups/dashboard-1guqtxwl`.

Every remote request revalidates the node, operator and private Serve route.
Sessions bind the origin and operator; HTTPS cookies are Secure, HttpOnly and
SameSite=Strict, and write requests also require the exact origin and CSRF token.
The authentication/runtime security scan passed. Live HTTPS checks returned 200
for Today, Writing, Operations and health; invalid identities and forwarded
local-host requests returned 403. Serve replaced a client-supplied false identity
with the authenticated identity. A valid remote session passed the POST security
checks and reached action validation; the deliberately empty action returned 400
before opening a database writer. No live job was invoked.

Port 8443 proxies only to `127.0.0.1:8767` and has Funnel disabled. The complete
remaining Serve configuration, including public 443 to port 8766 and its Funnel
setting, matched the saved pre-install snapshot exactly. The HTTPS UI was opened
and inspected in Chrome. Physical iPad access has not been tested; connect
Tailscale on the iPad under the approved login, then open the private URL.

The acceptance directory contains `private-access-dashboard-tests.log`,
`private-access-preflight.json`, `private-access-install.json`,
`private-access-live.json`, and `private-access-serve-after.json`. Source and
configuration backups are under `backups/private-access-20260908`.

## Operations layout rollout

The Operations update is installed on the same local and private URLs. The
latest runtime backup is
`~/.local/share/sd/runtime-backups/dashboard-xuqj6_qm`. The 18 promoted source
files match the reviewed manifest; the installed library matches all 38 files
in the tested wheel. No schema change was needed.

Live acceptance returned `"ok": true`: all five Operations tabs returned 200,
Today and Backlog show the exact inline CLI hint with no separate CLI details,
Backlog no longer contains the age chart, and all three populated Progress
links opened the matching active-item scope. The HTTP Services inventory
matched all 32 scoped IDs from `sd services list --json`. Ports showed the
running dashboard process on `127.0.0.1:8767`. Browser inspection confirmed the
capture hint, Services controls, Ports, Progress and Usage layouts; a disposable
browser fixture exercised Start, Restart and Stop. Existing user services were
only inspected. Physical iPad access remains untested.

The complete Tailscale Serve configuration matched the pre-install snapshot.
Health returned `ok=true`, `code_changed=false`, schema 3, PID 78555. Evidence
is in `operations-tabs-live.json`, `operations-tabs-install.json`,
`operations-tabs-serve-after.json` and `operations-tabs-source-verification.json`
in the acceptance directory above.

## Capture types rollout

Installed on 2026-09-08. Capture distinguishes standalone tasks from attached
notes. Related choices carry the stable ID, repository and current status;
completed and parked parents remain available without being reopened. Selection
loads a revision through the authenticated context endpoint. Stale or missing
parents retain the draft and require a deliberate refresh. An uncertain save
outcome retains the draft and requires checking the result before retrying.

The 13 promoted system files and installed 38-file library match the reviewed
build. Pack checks passed; the shared database suite passed 445 tests, and the
dashboard suite passed 207 tests. Repository guard failures in the initial
scratch checkout were resolved by restoring its missing Git context, without
weakening the checks. Browser tests created a standalone task and attached a
followup to the intended item, and confirmed draft retention after stale
selection and type changes. JavaScript checks covered out-of-order responses
and interrupted-save retry handling. These writes used only a disposable DB.

Live read-only acceptance verified all six types, all 32 related items, the
context endpoint and healthy schema 3 with unchanged Serve configuration.
Evidence and source/library backups are under
`/private/tmp/sd-capture-types-fn5icxdj/acceptance` and `backups` respectively.
The runtime rollback snapshot is
`~/.local/share/sd/runtime-backups/dashboard-fumx279q`.

## Limits

Job actions apply only to recognized installed launchd jobs. Unknown runtime
output disables controls. An accepted request is not a completed job; the UI
shows the observed state separately. Repeated requests are suppressed until
the observation changes. Running assignments without an owned cancellation
backend cannot be stopped through the dashboard. The system now includes an
owned queue runner and finite command execution. Their implementation does not
activate the service: verify its configured work volume, installed build and
observed runtime before dispatch. The pack has no runner command;
`sd-ship` records reviewed delivery and verified remote merges.

The refreshed UI contains Today, Backlog, Writing, Operations and item details.
Operations has Jobs, Services, Ports, Progress and Usage subtabs. Services lists
user LaunchAgents and third-party system LaunchDaemons. Controls apply to owned,
recognized, long-running user services; system daemons, scheduled/startup jobs
and the dashboard/Tailscale access services show why they are read-only. Stop
unloads for this login without deleting the plist or changing persistent
enablement. Start/restart honor restore, revision and identity guards.

Ports merges configured Docker-candidate ports with the TCP listeners visible
to the dashboard user. It retains all observed owners and addresses, and does
not infer process ownership from a configured port number. Failed observations
remain unknown; no listener observed does not prove another interface or
privileged process is absent. Other legacy plugin views remain CLI-only.
Metered-cost redesign is deferred; existing figures moved to Usage.

See `system/local-project-dashboard/RUNTIME.md` for the fingerprinted installer,
health checks and rollback limits. The earlier rollout evidence above records
its own source and live operations; use the coordinated delivery receipts for
subsequent commits, merges, installation and acceptance.
