# The workflow database: hub, lane host and judgments

`bin/sd-db` runs the workflow database's verbs from this checkout's `lib/`.
This page covers the hub and its satellites, the refusals, the lane host, `judgments`, the review queue, the item kinds, board votes, and renaming a repository by hand.
The module docstrings under `lib/sd_db/` hold the detail.

## One hub, many satellites: `serve` and `hub.json`

The hub is the one machine that holds `~/.local/share/sd/sd.db`.
A satellite holds no database.
It sends every open of the default database to the hub, one TCP session per connection.

### `serve` on the hub

    bin/sd-db serve              # the tailnet listener, port 8769
    bin/sd-db serve --loopback   # 127.0.0.1 behind a token: the suite over the wire

- `serve` opens the database on the hub's own disk, never through `hub.json`.
- It never creates a database. A missing file exits 1 and names the path it looked for.
- One server per file: it takes `<database>.serve.lock` beside the file.
  A second server exits non-zero and names the lock.
- It loads its build at start. After the pack's `lib/` changes, restart it.
  Until then it refuses each session with `HubRestartNeeded`.
- The first frame carries the client's protocol, package version, schema and build digest.
  The hub refuses any difference with `BuildMismatch`, which names the side to upgrade, and opens nothing.
- A launchd job on the hub runs the tailnet listener; the system repository's `local-machine-setup` installs it.

| | `--loopback` | Tailnet (no flag) |
| --- | --- | --- |
| Binds | `127.0.0.1` | this node's Tailscale IPv4 address |
| Admits | a session that sends the token from `<database>.serve.token` (mode 0600) | an untagged node of this node's owner |
| Token | fresh at each start, removed at exit | none |
| Opens | the file the session names, or the served one | the served database only |
| Used by | `python -m sd_db.testing.wire HOST:PORT TOKEN_FILE` | satellites |

Loopback is not a user boundary: every local account reaches `127.0.0.1`, so the token checks the account.

### The fence: a returning old hub

After a failover the old hub comes back with its own database and no `hub.json`, so it would act as the hub at once.
The fence stops it (sd:4044): a machine with its own database holds the hub role only while its tailnet name matches `sd.hub_name`.

    sd config set sd.hub_name hub.example.test   # the hub's MagicDNS name, or its bare machine name
    bin/sd-db fence                             # sd-db: fence: holder: this node is hub.example.test, ...

| Answer | When | `serve` on the tailnet, a lane on this machine's database | A write to this machine's database | `bin/sd-db fence` |
| --- | --- | --- | --- | --- |
| `holder` | this node's MagicDNS name matches | go on | go on | exit 0 |
| `off` | `sd.hub_name` is unset | go on, as before the fence | go on | exit 0 |
| `elsewhere` | another name, or a satellite | refuse, naming the holder | refuse with `Fenced`, naming the demotion | exit 1 |
| `unknown` | the setting does not read, or Tailscale does not name this node | refuse | go on | exit 1 |

The setting is read first, so `off` asks Tailscale nothing. Each check reads the name again; a failed read refuses again for 30 seconds without asking.
`sd config set sd.hub_name` refuses on a machine with no database: the fence reads the row alone.
`serve --loopback` is not fenced: it leaves the machine for nobody.
Scripts that write off the machine (the NAS copy, the `hub-pin` push) run `bin/sd-db fence` first.

Every library write to this machine's own database asks the fence too (system sd:5986): each write statement, and its commit.
A commit more than 5 seconds after the console renames the node, or after a sleep, rolls back.
Only `elsewhere` refuses: a Tailscale outage must not stop a healthy hub, and nothing it writes then leaves the machine.
Reads stay allowed, so the old hub's dashboard still shows its stale records.
A write and every boundary that makes it persist (COMMIT, the outer savepoint's RELEASE) use an answer held 5 seconds per process (`FENCE_SECONDS`).
The transaction's opening asks again once that answer is older than half of it, before SQLite's write lock, so a commit holds no other writer for a Tailscale call (sd:7042).
A write outside a transaction (`execute`, `executemany`, a cursor's) runs in its own `BEGIN IMMEDIATE` ... `COMMIT`, so its commit is one that asks; a batch commits whole or not at all.
A script's write outside a transaction refuses (sqlite3 commits before a script, so nothing can hold it): put `BEGIN IMMEDIATE` ... `COMMIT` inside the script. A writable `blobopen` needs a transaction.
Another file, such as a test fixture or a backup target, is not the record and is not fenced.
`sd-db restore` writes past it: promote restores before the machine takes the hub's name.

Demote a returning old hub in this order, the order the refusal names:

1. Copy `~/.config/sd/hub.json` from any satellite. Until step 2, every `sd` command refuses with `HubConflict`.
2. Move `~/.local/share/sd/sd.db` aside as `sd.db.demoted-<date>`. `sd` commands then reach the hub.

The other order leaves, after a cut between the steps, no `hub.json` and no database, and a fresh database reads `off` and writes.

### Peer identity on the tailnet

The listener takes the peer from the TCP socket, never from a frame.
`sd_db.tailnet` asks `tailscale whois --json --proto=tcp` for the node and its owner.
The dashboard's direct listener reads the same rules.
Before anything opens, the listener refuses:

- a tagged node, an expired node, or a node that does not own the address;
- a login other than this node's owner;
- this node's own Tailscale addresses;
- a session that names a database path.

Each refusal goes to the log before any SQL runs.
`serve` does not start on a tagged node or while Tailscale is not running.

`whois` names a node's owner, not the local account that dialed.
So every process on an admitted satellite is trusted, service accounts included.
The refusal of the hub's own addresses keeps a second account on the hub out.

### `hub.json` on a satellite

`~/.config/sd/hub.json` makes a machine a satellite:

    {"hub": "hub.example.test", "port": 8769}

`python -I -m sd_db.satellite --hub HOST --apply` writes it; the system repository's machine setup runs that stage.

- No file: `connect` opens the local database.
- The file and no answer from the hub: `HubUnreachable`, which names the hub. Nothing is created.
- The file beside a local `sd.db`: `HubConflict`. A machine is a hub or a satellite, not both.
- A file that names no usable hub: `HubConfigError`, never a fall back to a local file.
- An explicit path equal to the default is the default.
  `sd-review --database ~/.local/share/sd/sd.db` opens the hub, though no file exists.
- An explicit path other than the default opens locally, such as a backup target.
- `init` and `migrate` refuse with `HubOnly`.
- `token_file` is for a loopback hub only; a satellite's file names none.

### A satellite on another build

The hub refuses a satellite whose `sd_db` build differs, with `BuildMismatch`.
Nothing installs a build: the pack's `lib/` is the one copy (sd:3280).
The satellite stage reports the build as `DIFFERS`, with both sides' values.
It names the `repo-sync.sh follow` verb that moves the pack to the hub's commit.

### A lost `COMMIT`: `request_outcome`

A write transaction over the wire carries a request id R.
R is a ULID the client makes at `BEGIN IMMEDIATE`.
The session owns R before any statement runs, and the hub refuses an R it has seen.
At `COMMIT` the hub inserts the `request_outcome` row on the same connection, then commits.
The row and the writes land together or not at all.
A transaction that wrote nothing gets no row.

When the answer to `COMMIT` is lost, the client asks the hub `outcome(R)`:

| Answer | Meaning | The client |
| --- | --- | --- |
| `recorded` | the transaction committed | returns, and runs nothing again |
| `in_flight` | a session still owns R | asks again, with a backoff |
| `absent` | the transaction did not commit | raises `TransactionLost(R)`; run the verb again, under a new id |

When the outcome is still unknown after 60 seconds (`OUTCOME_BOUND`), the client raises `UnknownOutcome(R)`.
Do not run the verb again until the hub answers for R.

- A plain `BEGIN` opens a read transaction under `query_only`: no R and no row.
- A session silent for 4 seconds inside an open transaction is closed, which rolls it back.
  That is below the 5-second `BUSY_TIMEOUT`, so a hub writer behind an abandoned satellite gets the lock.
- No `request_outcome` row is ever deleted.
- A local connection ignores R.

## Refusals

### What a session may not run

SQLite's authorizer on each session's connection refuses these statements:

- `ATTACH`, and every `VACUUM`, which SQLite authorizes as an `ATTACH`;
- `load_extension`;
- a `PRAGMA` not in `PRAGMAS` of `sd_db/serve.py`, and one that sets a value not in `PRAGMAS_WITH_ARGUMENT`;
- `BEGIN`, `COMMIT` and `ROLLBACK` that the hub does not route, whatever the text;
- a `query_only` setting inside a transaction the hub holds.

The client gets `StatementRefused`, which is also a `sqlite3.DatabaseError`.
The refused statement does not run, and the session continues.

### Hub-only verbs

A file lock or a directory beside the database exists on the hub only.
So off the hub, each verb that needs one raises `HubOnly` before it locks or writes.
The message names the verb and the hub.
`sd_db.database.refuse_hub_only` is the one guard; its callers are the list:

- the control gate of service controls and restore;
- `backup`, `restore`, `init` and `migrate`;
- runner controls and the runner's executions directory;
- `repo remove` and `item remove`;
- the retention and executions prunes;
- the publication journal and the `sd writing` cutover;
- shadow sync;
- `repo lane-host`, which holds the hub's ship lock while it moves a lane (sd:3075);
- `records render`, the HTML page of each base (next section).
- `sd reports brief render`, the vault copies of routine briefs.

`ledger.reserve` raises `LedgerRefused` with scope `hub`: provider calls are charged on the hub.
`ledger.release_orphans` sweeps nothing off the hub, because it asks the local kernel about owner pids.
The `sd-ship` repository lock follows the lane host instead; see the next section.

## Lane host

`repo.lane_host` names the machine that runs a repository's merge lane (sd:3075, sd:3003).
The value is that machine's `hostname -s`, lower-cased.
NULL, shown as `hub`, means the hub; every row starts there.

    bin/sd-db repo lane-host PATH build-2   # move the lane; prints before and after
    bin/sd-db repo lane-host PATH hub       # NULL again; refused while another machine hosts it

The dashboard does the same: Management, the repository's page, `lane_host`, Move lane.
Both refuse an unregistered path and a name outside `[a-z0-9-]+`.
A move sets every clone row of the remote, so clones never disagree.
The queue lives in the hub database, so pending entries follow a move and nothing is copied.
Today's Queue view on the dashboard lists each managed lane with its host; Release there ends a stuck claim.

A move holds the ship lock of the machine that hosts the lane, from before its read to after its commit.
Every ship takes that lock, so a held lock proves no ship runs.
A busy lock refuses the move and names its holder.
A ship that waited through the move reads the new host once it gets the lock, and refuses.
A move runs only on the hub, and only while the hub hosts the lane.
A lane on another machine refuses with "run the move on <host> (sd:3003 part 2)".

`sd_db.ship.hosts_lane` answers whether this machine hosts a repository.
`repository_lock` asks it before the flock and again once it holds it, so a move during a wait refuses.
On any other machine it raises `LaneElsewhere` (`lane_elsewhere`) and locks nothing.
A read fault, a database that cannot be opened, or clones that disagree raise `LaneUnknown` (`lane_unknown`).
Uncertain ownership never grants the hub.
A missing row reads as the hub.
A lane this machine would run on its own database passes the fence too: `elsewhere` raises `LaneElsewhere` naming the holder, `unknown` raises `LaneUnknown`.
The hub's lock file sits beside the database; a satellite host's sits under `$XDG_STATE_HOME/sd/ship-locks/`.

The machine that hosts a repository gates and merges it, and no request crosses a machine.
Schedule `sd-ship lane run --hosted` on every machine: it runs each lane the machine hosts.
Each lane runs in its own child; up to three run at once while the gate has a free slot and load is under its limit.
A lane that gets work while the run lasts starts then, on the same terms, not at the next scheduled run.
[WORKFLOW.md § Parallel work](../WORKFLOW.md#parallel-work) covers the lane from the ship side.

## Channels

`repo.channels` names the publish channels a repository uses, and the folder each one writes to (sd:3462).
It is a JSON object; NULL, shown as `none`, means no setting, and every row starts there.
The design is `docs/work/2026-10-13-channels/design.md`.

    bin/sd-db repo channels PATH                                         # print: canonical JSON or none
    bin/sd-db repo channels PATH obsidian=Briefs/x notion=Example drive= # set; prints before -> after
    bin/sd-db repo channels PATH none                                    # clear

The keys are `obsidian`, `notion` and `drive`; `drive=` names the default folder, and a channel left out is off.
`feed=URL` adds the repo's one RSS/Atom subscription (sd:3980), an http or https URL with no credentials; `sd-watchlist` reads it.
The feed is inbound, not a channel: a setting naming only a feed publishes as no setting, and `channels.outbound` drops it for every publisher.
`jira=KEY` and `github=OWNER/NAME` name the repo's issue sources (sd:3461), inbound like the feed: the `repo` value that tracker's `shadow` rows carry, a Jira project key or a GitHub `owner/name`.
`repos.issue_sources` reads them for Today and Tasks; a managed repo refuses `github` and reads one set before as none, since managed repos use no GitHub issues.
`email=LABEL,LABEL` names the mail aliases the repo owns (sd:4191), inbound too: one to eight labels of `alias|<label>|<address>` lines in `local-mail-intake`'s conf, lower-case names, stored sorted. The address stays in that file, so an `@` refuses. `repos.mail_aliases` reads them for Today. Before rolling the pack back past this key, or running `sd-db` from an older checkout, clear it from each repo that names it: a build from before it refuses the whole setting, and its next set replaces the whole setting, every other key with it. This build refuses such a set instead: over a stored value it cannot read, a set with no `before` exits 1, and `none` then a set is the way through. Design: `docs/work/2026-10-14-email-slack-channels/design.md`.
`repos.mine_issues` reads each repo's open issues whose `shadow.reason` is `assigned` or `filed` (sd:4636); both collectors write the reason, `watched` rows stay synced and hidden, and a row synced before migration 34 reads unknown until a collect sees it again.
A set naming any channel replaces the channels; one naming none keeps them, so the dashboard form never drops a source. Each inbound key (feed, jira, github, email) is set on its own: a set that leaves one out keeps it. `none` clears all of it; to drop one source, clear and set the rest again. The verb prints `before -> after`, as stored.
A folder is a name, never an id or a URL: no leading `/`, no `.` or `..` segment, no control character.
A Notion folder is one page name, with no `/`.
`sd_db.channels.validate` checks every write; `repos.set_channels` is the one writer, `repos.repo_channels` the reader.
The verb refuses an unregistered path when it sets, and prints `none` with a note when it reads one.
A stored value the validator refuses, or a database not yet migrated, exits 1 and prints no value.
So does a read while a `restore` is unresolved, for every reader: the row may be older than the operator's last edit. A set still works; it is the restore's recovery.
A change takes effect at the next `sd-research-kit render` in the checkout, or the next commit.
The render reads the setting once; one it cannot read holds the vault copy and the queue, and a render by hand exits 1.
What each channel's folder replaces, and drain step 2's refusal: `skills/_shared/references/publication-contract.md`.

## Clone profiles and Dependabot

`repo.clone_profiles` names the machine kinds `repo-sync` clones a repository on: `personal`, `work` and `terra` (sd:4043).
NULL, shown as `none`, means none, and every row starts there.
A set has one spelling, its words in that order, so the column's CHECK is a closed list.
`repo.dependabot` is 1 for a repository outside the owner rule whose Dependabot pull requests the daily sweep may take.
The design is slice P2 of the system repository's config-to-DB design (sd:3983).

    bin/sd-db repo clones PATH personal,work    # set; prints before -> after
    bin/sd-db repo clones PATH +terra           # add one profile; -terra takes one away; none clears
    bin/sd-db repo dependabot PATH yes          # or no; prints before -> after
    bin/sd-db repo seed --clones personal       # add personal to each row repo-sync's lists give it
    bin/sd-db repo list --clones personal       # `<subdir> <owner/repo>`, the lines repo-sync reads

`repo clones` reads and writes in one transaction, so a `+P` beside a `-Q` on the same row loses neither.
`seed --clones P` reads `repos.common.conf` and `repos.P.conf` in the `repo-sync` config folder; terra reads its own list alone.
The profile is the argument, never `REPO_SYNC_PROFILE`.
A line with no row is registered as `repo seed` would, when its checkout is here.
A line with neither a checkout nor a row is named, and the run exits 1 after the other lines.
A line names a repository; its path only says where it sits.
The checkout's origin and the row's remote must each name the line's repository, or that line writes nothing, is named with what the path holds, and the run exits 1.
No origin counts as a mismatch here; plain `repo seed` refuses a differing origin too, and keeps the conf's slug for a checkout with none.
It only adds, one transaction per line, so a rerun finishes a stopped run.
`list --clones P` prints one line per row cloned on P, `.` for a checkout straight under `~/repos` (`SD_REPO_ROOT`).
A row that cannot be such a line exits 1, names every such row, and prints nothing on stdout.
Such a line needs a row one or two folders deep, a GitHub remote, and a folder named as the repository.
A database not yet migrated exits 1 too; neither case answers an empty list.

## GitHub issues

The `policy` declaration's `issues` key says whether a repository takes GitHub issues from outside reporters (sd:4827, pack sd:6849).
Migration 45 moved the `repo.github_issues` column into it: a row that was on reads `ask`, and the column is gone.
No key reads `off`: agents use no GitHub issues there and file work as sd items.
`read`: an agent maps each open issue to one sd item and writes nothing on GitHub.
`ask`: also, it comments for the reporter and closes the issue when the item is done, each write approved by the operator in the harness prompt (`sd-issue-guard` asks); an unattended run drafts it as a `--kind question` note instead.
There is no `auto` (note #34749).

    bin/sd-db repo github-issues PATH ask       # or read, off; prints before -> after

`repo list` shows it between `ci` and `lane_host`; `runner_merge` stays the last field.
`repos.issues_policy` reads it for code: no row and no key read `off`.
`sd shadow sync` stores every open issue of a `read` or `ask` repository with a github.com remote, not only the operator's (sd:4880): an unwindowed `is:issue is:open` sweep lists them, five repositories per search, whatever the window.
Absence from a search does not close a row, since GitHub's index can omit an open issue: after a complete sweep, each stored open issue of its repositories that neither it nor this run's buckets saw is read directly (`GET repos/{owner}/{repo}/issues/{n}`, on the sync's budget).
Only the read's own answer closes it: state `closed`, a `pull_request` key, another issue number, or HTTP 410, which GitHub gives for a deleted issue and for a repository whose Issues are disabled (pack sd:5415); its url spelling decides nothing, so an issue that still answers open under its number stays open, renamed or transferred. Any other failed, refused or over-budget read, a 404 too, leaves it open and prints `intake sweep incomplete`.
A failed or capped sweep reads and closes nothing, holds no cursor and prints the same line.
A stored open issue of any other repository that no search has seen for a week is read the same way, oldest first, after a run whose buckets completed (pack sd:5415); an open answer counts as a sighting.
Each GitHub row also keeps `merged_at` and `closed_at` (migration 38, sd:5303), read from the same search page and the direct read: no extra request.
A reopened row loses both; a date that does not parse keeps the stored one. Rows older than the nightly window get theirs from one `sd shadow sync --since <date>`.
A pull request's row also keeps `branch` and `linked_issues`, the urls of the issues it closes, at most ten (migration 38, sd:5315), from the same page.
`shadow_sync.jira_links(connection, title, branch)` finds Jira keys in the texts and joins them to the `jira` rows; a key no row holds links nothing, and no Jira call is made.
`contributions.projection`, the dashboard's `/api/contributions` read, gives each row `merged_at`, `closed_at`, `branch` and `linked`: each `{url, kind, key}`, kind `github` for an issue it closes or `jira` for a key it names.
`repos.intake_issues` reads, per `on` repository, its open issues whose author is not the operator (the author of the `filed` rows) that no item carries as `--ref github:<owner>/<repo>#N`, for Today's intake.
The policy's `issues_scope` key says which of them intake lists (pack sd:7672): `all`, no key, lists them all; `mine` only those whose `shadow.reason` is `assigned`, `watched` (a mention or a review request) or `filed` (`repos.SCOPE_MINE`). The windowed buckets never see an issue last updated before their window, so `sd shadow sync` also searches each `mine` repository unwindowed, `assignee:@me` and `mentions:@me`, five repositories per search, and when both complete restates the reason of each of its stored open issues from them (`writes.restate_reasons`): one that no longer holds is taken back, and `filed` stays. A failed or capped search restates nothing and prints `intake sweep incomplete`. Until the first sync after the switch, an older issue reads no reason and stays out. The scope governs no write: `issues` alone does.

    bin/sd-db repo issues-scope PATH mine      # or all; prints before -> after

Agents close an issue with `mcp__github__issue_write` `update`. `bin/sd-issue-guard` lets that through when the call names a managed `on` repository; filing an issue there stays denied. A call aimed at a repository the operator does not manage gets `ask`, so the operator decides each one (sd:5721).

## Jira project

`repo.jira_project` names the Jira project a repository files issues in, or NULL for none (sd:5103).
Every row starts NULL, off. It is a filing target: the system dashboard's File Jira issue action files a sanitized issue there after the operator confirms its preview.
It is not the inbound issue source `repo channels PATH jira=KEY` names, and setting one leaves the other alone.

    bin/sd-db repo jira-project PATH EXAMPLE    # or off; prints before -> after

A key is `A-Z`, then 1 to 254 of `A-Z`, `0-9` or `_`; the column's CHECK holds the same spelling.
`repo list` does not show it, so no reader that counts its fields moves.
`repos.jira_project` reads it for code: no row reads None, and a database not yet migrated raises `SchemaTooOld`.

## Declarations

`repo.declarations` holds a repository's pack declarations as one JSON object, or NULL for none (sd:5109).
Each key is a kind; each value is the object the old `.github/` file held, or for `experiment` the research link.

| Kind | Reader | Old file |
| --- | --- | --- |
| `review` | `sd-review` routing policy, its binding in `sd-ship` | `.github/sd-review.json` |
| `gate_reuse` | the gate's receipt key (`sd_gate_receipts`) | `.github/sd-gate-reuse.json` |
| `check_scope` | `sd-check --base` docs-only scope | `.github/sd-check-scope.json` |
| `status` | `sd-status` accepted gaps, `sd-ship merge` declared gap | `.github/sd-status.json` |
| `experiment` | the system dashboard's research project link: `page` and `data_source`, Notion ids, and optional `url` and `title` (sd:5302) | none |
| `policy` | the repository's rules for agents (`bin/sd_repo_policy.py`, sd:6715): `mode`, `commit`, `push`, `pr`, `merge_method`, `issues`, `issues_scope`, `review`, `commit_convention`; an org or external repository never takes `auto`; no kind reads as today's managed defaults. `sd-ship merge` reads it at merge time: it merges by `merge_method` (sd:6790; under rebase, prepare folds `fixup!` commits, writes the trailers on the tip and force-pushes it with a lease), and `review.human` holds the merge until that many humans approve the head; `ci` `{provider: jenkins, url, job}` makes the merge wait for a green Jenkins build of the head (`{pr}` in `job` is the PR number), and an unreachable Jenkins holds it as `ci_unreachable`; `test` `{setup, env, teardown}` is the recipe `sd-check` runs around the checks: unset `env` names or a failed `setup` fail them unrun, and `teardown` runs after them; `release` `{deploy_repo, file, key, verify}` is the chain the lane runs after a merge (`bin/sd_release.py`, sd:6881, sd:7014): it pushes the next semver tag from `commit_convention.release_types` (default `feat`, `fix`; an empty list releases nothing) on origin's main; with `deploy_repo`, `file` and `key`, declared together, it opens a PR in the deploy repository setting `key` in `file` to the version, in the deployed value's form, and merges it with `gh pr merge --match-head-commit` once the deploy repository's own policy gates pass (green checks, `review.human`, a Jenkins `ci`), staying at merge until GitHub reads it `MERGED`; then it runs `verify` in a detached worktree at the merged deploy commit, or at the tagged commit when no deploy is declared, with `SD_RELEASE_TAG` and `SD_RELEASE_VERSION`; each act a policy holds at ask files one question on the item and waits for a ruling that starts with `go`, a freeze window holds the tag and the merge, and a failed `verify` reports and rolls nothing back | none |

    bin/sd-db repo declare PATH                     # every kind, as JSON
    bin/sd-db repo declare PATH review              # one kind
    bin/sd-db repo declare PATH review '<json>'     # set; prints before -> after
    bin/sd-db repo declare PATH review off          # remove

The verb checks a value with its reader's own validator first and drops `$schema`, so the row holds only what a reader accepts.
The library refuses an unknown kind, a value that is not an object, more than 16384 bytes and an unregistered path.
It also checks `experiment` itself (`repos.experiment_problems`), since the dashboard writes that kind directly.
`sd_lib.repo_declaration` reads it through `registered_base`, so a linked worktree, a runner clone and a gate's worktree read their repository's row.
No database on the machine, or a store before 37, reads as nothing declared; a failed read is unknown and each reader refuses or runs in full.
The `.github/` files are no longer read.

    bin/sd-db repo freeze PATH                      # the open window, or not frozen
    bin/sd-db repo freeze PATH UNTIL 'reason'       # freeze until UNTIL, at most 31 days
    bin/sd-db repo freeze PATH off                  # lift it

A freeze window is a `freeze` state row keyed by the repository's GitHub `owner/name`, not a declaration: it is short-lived (sd:6879).
Inside a window the lane claims no entry, so every entry stays pending in order, `sd-ship merge` refuses with `freeze_window`, and the release chain holds its tag and its deploy merge.

## `judgments`: what the judgment models cost, by stage

`bin/sd-db judgments` compares the stages that route a decision through a judgment model.
The system repository's `local-jev` writes a `judgment` row per call.
The mechanism each caller used before writes one too, under the same stage key and in the same table.
A fallback that cannot be counted against a judgment answers nothing.

    bin/sd-db judgments                 # every stage, both arms
    bin/sd-db judgments --since 2026-09 # a month; bounds are compared as text
    bin/sd-db judgments --json          # the same read, for a screen

### What a row holds

The table holds identifiers and counts, and nothing that was submitted.
No prompt, state, path, subject or body reaches it.
The two columns that could carry content are shaped, not only capped: a length cap lets anything short through.
`answer` is a number: a probability, a score, or a position into the caller's own criteria.
`ordering` is positions into the caller's own input.
`sd_db.judgment` refuses a row rather than truncate either.

The names are shaped too.
`caller`, `stage`, `primitive`, `question_id` and `pair` are identifiers: letters, digits and `.`, `_`, `:` or `-`.
They are caller-controlled, and a cap alone would accept a file path or a short subject line.
`provider` and `model` are exempt on purpose: they carry vendor names such as `anthropic/claude-opus-5`.

`location` is where the call came from; `caller` is who asked.
It is the git toplevel of the caller's working directory, else that directory.
`record` stores a path under `$HOME` as `~/` plus the relative path, and any other path absolute.
A bad `location` does not refuse the row.
A value over 255 characters, or one with a control character, is stored as NULL and the row is kept.

More columns record the conditions of a call.
A value that fails its shape is stored as NULL and the row is kept.

| Column | What it holds |
| --- | --- |
| `threshold` | the cut-off the caller applied (`--gate`, `--unsure-below`), from 0 to 1 |
| `run_id` | an identifier that groups the calls of one run, such as one review |
| `prompt_hash` | 16 lowercase hex digits hashing the question definition, never the state |
| `load_avg` | the one-minute load average when the call was made |
| `error_class` | an exception class name such as `ConnectionRefusedError` (sd:2973) |
| `error_detail` | an errno name or HTTP status such as `ECONNREFUSED` or `503`, never message text |

### Batches and gate events

A batched call asks several questions at once (sd:2966).
Its row is the call: tokens, latency, cost, `questions=N`, and no answer.
Given `answers`, one entry per question in order, `record` also writes one child row per question.
A child's `parent` names the batch row; its `question_id` is `<id>:<position>`.
A child carries the question's type as `primitive`, and its own answer and confidence.
It carries no tokens, latency or cost, so nothing is counted twice.
Every report counts calls from batch rows only; agreement and labels read the children.
A child whose field fails its shape is kept as `invalid` with that field NULL.

A gate event is not a decision.
A caller that declines at the gate, then records its own mechanism, writes two rows for one decision.
So the comparison leaves the gate row out of the calls and the declines, and counts it on its own line.

### Cost

A judgment row is not a `cost` row.
`cost` is money against a `bill`, and its `provider` is a foreign key into the registry; a judgment model is on neither.
A row's `usd` is the cost its caller reports.
Without one, `record` prices the tokens at the `price` (`in` and `out`, dollars per million) in `providers.yaml`.
An entry that names a `model` prices that model only.
With no usable price, `usd` stays NULL and the token counts are the record.
The report names those rows per arm as `unpriced`, with their providers.
`local-jev` rows name provider `typesafe`; its price is the operator's to enter.
An entry with `roles: []` is never chosen for a role.

A local model is priced by machine time instead (sd:2967).
An entry with `price: { hour: N }` prices a `jev`, `kev` or `haiku` row at N dollars per hour.
The time is `server_ms`, or `duration_ms` when the server reported none.
The rate is the machine's, so it applies whatever the entry's `model`.
A baseline row is not priced by time. With no `hour` price, a `local` row stays NULL, never 0.0.
A rate prices the rows written after it is entered; old rows are never repriced.

The operator measures the rate.
Take the machine's hourly cost, or measure its power under load and multiply by the electricity price:

    sudo powermetrics --samplers cpu_power,gpu_power -n 1   # watts, while the model answers
    # hour = watts / 1000 * dollars per kWh; 40 W at $0.15/kWh is 0.006

### Labels: whether a judgment was right

Reported confidence is not accuracy.
A row can carry a label: `override` is the answer an authoritative later source says it should have been.
It is a number in the shape of `answer`.
`override_source` names the rule that produced it, and `override_at` says when.
A row is right when the label equals the answer, compared exactly as decimals.
A row with no answer, such as a failed call or a batch row, cannot be labelled; a batch's child rows can.
The report counts labelled and right rows per arm, and per arm by reported confidence in tenths.
It prints each rule's known blind spot beside the stage it labelled.

`judgments label` is the one write path; a labeller uses these verbs and never opens the database itself.
The same label twice changes nothing.
A different label is refused unless `--replace` says to overwrite it.
A `kev` or `haiku` comparison row is refused: a pair's label lives on its Jev row.
A choice is labelled with a whole position from 1.

    bin/sd-db judgments unlabelled --stage JEV_SD_REVIEW --prefix sd-review-tier: --json
    bin/sd-db judgments label --row 42 --override 3 --source outcome.example

No labeller writes this table yet.
A label needs independent evidence of the right answer, never one derived from the prediction it scores (sd:2107).

### The comparison arms

`jev` can ask a local Kev and a Haiku model the same request beside each live call, once each arm is on (sd:2366).
Each answer is its own row, arm `kev` or `haiku`, with the Jev row's pair id.
It carries the latency the model reported in `server_ms`, and the distribution in `probabilities`.
The distribution is numbers in option order, never the keys.
The report above reads only `jev` and `baseline`; this one reads the three model arms:

    bin/sd-db judgments compare [--stage NAME] [--since STAMP] [--until STAMP] [--json]

It reports per stage, and per arm and provider:

- calls, and declines by cause;
- p50 and p95 latency, by nearest rank, so every printed number is one a call took;
- tokens and cost;
- agreement with the Jev row of the same pair;
- accuracy and Brier score on pairs whose Jev row carries a label.

A noul agrees when both sit on the same side of 0.5; the mean `|Δp|` is printed beside it.
A score agrees when the rounded scores are equal; its distribution feeds the Brier score only.
A batch is compared question by question: each child row against the Jev child with the same pair and `question_id`.
Its calls, latency, tokens and cost come from the batch row alone.

Each arm, Jev included, is also read against the old mechanism (sd:2761).
That is the baseline row a shadow call writes under the same pair id.
The line gives agreements over comparable pairs, the percent, and the five most common disagreements.
Each disagreement reads `old→arm`: 1 or 0 for a noul, a position for a choice, a rounded level for a score.
A pair whose old answer is not a number is counted and not compared.
Examples are a `--fallback` marker, or a noul asked under a `--gate` other than 0.5.
A stage with no such pair says `no paired samples with the old mechanism`.

## `telemetry report`: the monthly lane report

`sd-db telemetry report [--month YYYY-MM] [--out FILE]` writes the monthly lane report draft for the writing pipeline (pack sd:6947), last month by default, to stdout or `FILE`.
It reads every ended build run from the store: merged PRs with cost and lead time, failure classes against the month before (`telemetry.CLASSES`), review rounds and the repeat-blocker rate, denied tool calls, database-lock refusals, and the run id of the first run of each failure class and of the three costliest merges.
It carries item ids, PR numbers, run ids, counts and cost, never a title, note body or `detail`.
sd-db sends no traces: the genai-traces collector is the one sink (pack sd:7365).
A build's spawn env appends `sd.build_run` and `sd.items` to `OTEL_RESOURCE_ATTRIBUTES`, so Claude Code's own OTel names the run.

## `review`: one queue of small questions

`bin/sd-db review` is the plumbing behind one review queue that any repository or experiment files into (sd:3635).
A producer writes one batch file and files it; the operator answers each question with one option; the producer exports the labels.
The design is the review-queue design in the system repository; `lib/sd_db/review.py` holds the note formats.

    bin/sd-db review file batch.json                      # file a batch; the same file again is a no-op
    bin/sd-db review export 4242 --out labels.jsonl       # labels with provenance, as JSONL
    bin/sd-db review export 4242 --key key.csv --out labels.jsonl  # sealed: closes the batch, then joins the key
    bin/sd-db review apply nightly                        # approvals to tasks; close or expire each batch
    bin/sd-db review file --kev labels.jsonl --batch aura --blind   # a Kev label file as one blind batch
    bin/sd-db review export 4242 --out reviews.jsonl --format reviews  # labels in Kev's reviews.jsonl shape

A batch is one task under the ref `review:<queue>:<batch>`, so a second filing finds the first.
Its first comment is the header; each question is a `question` note in the Decisions format, with one `Subject:` line.
A label is the Decisions ruling, `Ruling on note #N: <option>`; an undo is a `Withdrawn: ruling #M on note #N` decision.
A sealed export refuses a key whose SHA-256 is not the header's seal, and refuses while a question is open.
It commits the batch's close before it parses the key.
A batch once closed refuses every ruling and withdrawal.
Labels are read from the note history up to the first close, never from a question's resolution; a note written after the close by another path is exported as `late` and moves nothing.
The key is CSV; its first column names the subject.
`apply` acts only on questions that offer `Approve` and `Reject`; each approval files one task under `review-approved:<note>`.
Its repo is the batch's, else the registered checkout whose last path part is the name in the header's `Source: <name>@<commit>`; none or two leave it unset.
Approve through `rule`, which the dashboard's Review page calls, files that task in the ruling's transaction (sd:4924).
The ruling then reads `Ruling on note #N: Approve -> sd:<task>`, and `apply` finds the ref and files nothing more.
If the ref already names a task, as after a withdrawn approval, the ruling names it instead; a withdrawal leaves the task and ends `; sd:<task> stays filed`.
It resolves a batch's open questions as `Expired, not ruled` after 7 days.

A batch file may carry `context` (`purpose`, `rubric` as a list of lines, `feeds`: what the labels feed), which the header holds as `Purpose:`, `Rubric:` and `Feeds:` lines.
A question may carry `recommendation` (`option`, one of its options, and a one-line `reason`), which its note holds as two evidence lines, `Recommended:` and `Recommended because:`.
The header then holds `Recommendations: <n>`, and only such a batch reads those lines as one; in it, an evidence line of the producer's own may not start with either.
A batch that recommends nothing keeps such evidence as evidence, as before.
The dashboard shows a blind or sealed batch's recommendation only once its question is ruled (system sd:5759).
The export's lines carry `recommendation`, `agreed` and `ruled_blind`; a label is ruled blind in a blind or sealed batch when it is its question's first ruling.
The manifest carries `context` and `agreement`: how many labels had a recommendation, agreed with it, were ruled blind, and agreed blind.

`file --kev` reads the JSONL Kev's label review reads (sd:4789) and files it under queue `kev`, title `Label: <batch>`.
Each line is one question: its `id` is the subject, and its options are the option keys, then `(drop)`.
A proposed label and each judge become evidence lines, which the dashboard hides in a blind batch.
A Kev file files whole or not at all: a bad line refuses it, naming the line, and no 90-day suppression drops a line.
`--format reviews` writes one line per labelled question: `id`, `verdict` (`accept`, `relabel` or `drop`), `label`, `proposed_label`, `note` and `reviewed_at`.
A blind batch never reads `accept`, because its labeller saw no proposal.

## The state kinds

`state.kind` has no CHECK since migration 031 (sd:3506): `writes.STATE_KINDS` is its one list, and `writes.record_state` refuses a kind outside it.
A list the library validates gets no CHECK: SQLite cannot alter one, so each new kind used to rebuild the whole table.

## The item kinds, and the two words called `followup`

`item.kind` is a closed list, guarded by a CHECK. Seven values:

| Kind | What it is |
|---|---|
| `work` | a change to a repository, planned under `docs/work/` |
| `task` | a scoped piece of work in a repository |
| `report` | a run report a scheduled job wrote |
| `idea` | **a writing piece on the article ladder, and nothing else** |
| `work-idea` | an idea about a repository or product, not yet planned work |
| `personal-idea` | an idea about life, which does not enter the writing pipeline |
| `followup` | something to come back to, filed as an item of its own |

`idea` looks like the generic word and is not.
`writing.list_pieces` reads `kind = 'idea'` with a `piece`, and the dashboard renders writing controls on it.
So a personal or a work idea has its own kind, not a stage on `idea`.

`note.kind` is its own closed list of seven: `followup`, `decision`, `proposal`, `question`, `comment`, `exec` and `status_change`.
Only `followup` is in both lists. Migration 032 (sd:3510) dropped the `proposal` and `dep` item kinds: neither had a producer or a row.
Migration 046 (sd:7004) moved every `personal` and `skill-review` row to `task` and dropped both kinds.
A to-do is a task in a repository; the catalogue files a skill review as a `task` whose `fields.skill_review.action` is `review` (`skills_catalog.is_review`).

- A `followup` note hangs off an item and is resolved against it (`sd task note <item> --kind followup`).
  A `followup` item stands on its own (`sd task add --kind followup`).
  If it makes no sense with the parent deleted, it is a note.
- A `proposal` is a note: `sd task note <item> --kind proposal` records one, and `reads.brief_notes` carries it into a brief.

`task` and `followup` items (`workflow.TASK_STATUS_KINDS`) share the five `TASK_STATUSES`:
`planning`, `ready`, `in_progress`, `blocked` and `done`.
`work` keeps its delivery rules; `idea`, `work-idea`, `personal-idea` and `report` keep their own workflows.

`workflow.edit_item` is the verb a person uses to change a row's kind.
The target must be in the store's CHECK (`workflow.schema_kinds`) and in `workflow.HAND_KINDS`, the four `sd task add --kind` files.
A `work` or `report` row keeps its kind, because its producer owns it.
So does an `idea` with a `piece`, and a row whose `fields` carry `contribution` or `skill_review` (`workflow.PRODUCED_FIELDS`).
The edit writes a `comment` note, `Changed kind <old> -> <new> by <who>`, in the same transaction.
A vault base row's kind is refused: `records` lists it by item kind `idea`.

`work-idea` and `personal-idea` (`workflow.REPO_LESS_KINDS`) are filed with no repository.
Read them as a group with `reads.backlog_items(connection, repo=reads.NO_REPO)` or the dashboard's `/tasks?repo=no%20repo`.
That is filing behaviour, not a constraint: `item.repo` has no per-kind CHECK, and `writes.create_item` takes a `repo` for any kind.

## `records render`: the HTML page of each base

Once a plugin's `store.driver` is `db`, its vault base rows live in `item` and `sd_db.records` writes them.
`records render [KIND ...]` writes one read-only HTML page per base to `<vault>/docs/dashboard/base-<kind>.html`, for the dashboard's Documents tab; with no kind it takes every kind a `db` plugin declares.
It writes no note into the vault: the note mirror was retired (sd:5133).
It refuses off the hub, for a kind whose plugin list still reports the removed `vault` driver, and when the vault root is not a directory.

Each committed `records` write renders its kind, or every kind after a failed render. A write never fails for its render: it prints `mirror not updated: <reason>`.
Every render records the `heartbeat` state row `records-mirror`, body `{at, ok, kinds, written, reason}`.
`records.mirror_state(connection)` reads the last one, `(timestamp, body)` or None; the dashboard's Health shows it.
The verb exits 1 on a render that stopped.

`records documents` moves learning rows to their Documents view (pack sd:6262), so they leave Tasks, Today and the counts.

- With no `--apply` it is the dry run. It opens the database read-only, prints each row it would move, and ends with the plan digest.
- Videos match by their title, `YYYY-MM-DD - YouTube - <channel> - <title>`. Books and articles match only by the list the operator gives, `--book ID` each.
- `--apply PLAN`, with the same `--book` list, moves that list in one transaction. A list that changed since the dry run refuses and moves nothing.
- A moved row gets `fields.document-type` (`books` or `videos`) and a `comment` note. Its id, notes, stage and status stay.
- `--undo ID ...` takes the field off and moves the rows back. It refuses them all when one was never moved.
- `reads.NOT_A_DOCUMENT` is the rule every Tasks, Today and count read applies, through `reads._unparked_clause`. Open notes stay in Decide.

## `sd reports brief`: routine briefs

A routine brief is one `item` row of kind `report`, source `brief`, keyed by its stem, `<YYYY-MM-DD> - <Series>` (sd:3459).
It is filed `done` and stays `done`: `sd reports list` skips it, and `acknowledge` and `reopen` refuse it.
`sd_db.routine_briefs` holds the rows; `sd_db.brief` is the session brief, a different thing.

- `add <file> --belongs-to <checkout>` files one brief. A file the rules refuse exits 2 before any write.
- The same stem with the same bytes prints `unchanged`. Other bytes are a conflict, exit 1, unless `--extend` (the row's text is a strict prefix of the file) or `--replace` (the operator's flag) allows the file.
- `list [--series S] [--since DAY] [--json]` and `show <stem>` read the rows.
- `import <folder> --belongs-to <checkout> [--replace <stem>]...` adds each missing stem and strips a `mirror-hash` line first. A row that differs is a conflict, never replaced unless its stem is named; the import prints `verified <n>` when every row it took reads back with the note's sha256.

The row is the source of truth: no counter or time decides a difference, because a restore rolls those back with the rows.

`render` writes each row's copy, `<vault>/<folder>/<stem>.md`, with a `mirror-hash` line as its last frontmatter line (sd:4041).

- The folder is the owning repository's `channels` obsidian folder; no setting or `obsidian: {}` is `Briefs/<repo>`, and a setting without `obsidian` writes no copy.
- A setting it cannot read writes no copy and names the cause.
- A copy is overwritten only when its `mirror-hash` matches its content, or it has none and holds the row's text. Any other copy is a hand edit. It and a brief-shaped note no row holds are kept and refused; the verb then exits 1.
- It refuses off the hub and while a `restore` is unresolved. A lock beside the database serializes renders, and each reads the rows only once it holds the lock.
- When the folder moves, it names the copies left in the previous folder and deletes none.
- Every render records the `heartbeat` state row `brief-copies`; `routine_briefs.copies_state` reads the last one.
- A hub `add` renders after its commit. A failed render prints `vault copy not written: <reason>`, and `add` still exits 0.

## The newest restorable snapshot

`sd_db.backup.newest_restorable(roots, report=None)` names the snapshot `restore --newest` would restore, and restores nothing (pack sd:6323).
`restore_newest` calls the same walk, so a readiness check and the restore cannot disagree.

- It takes the dated folders under `roots`, newest checkpoint first, and judges a local copy of each.
  The checks run in order: the files against the manifest's hashes, the schema stop, `_check_backup`, then `check_restorable`.
- It returns a `Restorable`: `snapshot` and `taken_at` of the first that passes, and `refused`, why each newer one failed.
  `refused` also names an unreadable root and each run stopped before its manifest.
- No snapshot passing is `snapshot=None`, not an error.
- A snapshot of a schema newer than this library stops the walk, as it stops the restore: `newer` holds its path and version.
- `released` lists the runs whose open leases a restore of that snapshot would release.
- It writes only its temporary copies, so it runs off the hub; `restore_newest` still refuses there.

## `sd votes`: board votes

`sd votes add|edit|list|show|import` keeps a board's votes in the `vote` table, one row per vote, keyed by its checkout and a key such as `CI-1` (pack sd:8050).
Run it from the checkout, or name one with `--belongs-to PATH`; `list`, `show` and the writes take `--json`.
`import FILE` loads a CSV with `votes.csv`'s columns in one transaction: a key the checkout holds is updated, and a second run changes nothing.
It refuses the whole file for a header that lacks a column or names one twice, and for a row with fewer cells than the header: either would blank stored text.
`votes` keeps the cell as written; `tally` is each director's answer read from it, or none when the cell does not read whole.
An outcome starting `pending` makes a vote open unless `--state` says otherwise; `edit --if-revision N` refuses a vote that changed.
The HOA page's Votes view reads them, open votes first.

## Renaming a repository

There is no rename verb (sd:3172).
A rename by hand changes the keyed rows and the runner journal together.
Otherwise `bin/sd-db backup` stops with "runner row ... differs from the backup journal".

1. Stop the lanes, the dashboard and `sd-serve`, then run `bin/sd-db backup`.
2. In one transaction, change the old key (`~/repos/old`) to the new one in `repo.path` and every `repo` column:
   `item`, `cost`, `repo_protection`, `runner_lease`, `runner_run` (its `detached_from` too), `shadow` and `vote`.
   `SELECT m.name, p.name FROM sqlite_master m, pragma_table_info(m.name) p
   WHERE m.type = 'table' AND p.name LIKE '%repo%'` lists the columns.
3. Rewrite each file in `runner-journal/` beside `sd.db` whose `record.repo` names the old repository, by its key or its absolute path.
   Do not edit the text: `runner_journal.read` checks the envelope's sha256.
   Recompute it as `runner_journal.persist` writes it, with sorted keys and compact separators:

       import hashlib, json, pathlib
       path = pathlib.Path("~/.local/share/sd/runner-journal/<id>.json").expanduser()
       record = {**json.loads(path.read_text())["record"], "repo": "~/repos/new"}
       def compact(value):
           return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
       envelope = {"record": record, "sha256": hashlib.sha256(compact(record)).hexdigest()}
       path.write_bytes(compact(envelope) + b"\n")

4. Run `bin/sd-db backup` again; it passes when rows and journal agree.
