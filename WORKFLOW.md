# Workflow

How the pack expects to be used. One person does most of the work. Other
people see pull requests and merged commits, and nothing else the pack makes.
Every default below serves that person. Anything that would show a personal
process to someone else is off unless this file says otherwise.

For this maintainer's reviewer and report preferences, read the
sd-workflow checkout's [.claude/rules/sd-operator-defaults.md](.claude/rules/sd-operator-defaults.md).
Those instructions do not change executable gates or another operator's permissions.

## Available controls

`sd task` and the dashboard create and update ordinary tasks directly in the
database, without a Git checkout, PRD or GitHub issue. `sd today` and `sd store
items` use the same queries as Today and Backlog. Completion of an ordinary
task is independent of code delivery. `sd work deliver` verifies delivery
evidence; `sd work cancel --reason` records a cancellation without waiting for
another merge, and `sd task cancel --reason` does the same for a task or
followup. Artifact relinking preserves the item's identity and history.

Writing uses shared stage and review-evidence checks through `sd writing` and
the Writing screen. After the verified one-time cutover, stages, parking and
publication metadata belong to the database; drafts and research stay in Git.
The supported operational controls inspect existing launchd jobs and database
assignments. Running an arbitrary agent command or terminating an unowned
process is not a dashboard action.

The automation policies below describe the intended development loop. They do
not mean an autonomous queue runner, send box, one-click revert, or repository
settings editor is installed. Consult the current command help and dashboard
controls for executable operations.

## Two flows, one spine

The spine is the **item**: one row in the local database and one screen on the
dashboard, with optional files in Git. A small code change can proceed without
creating an item or placeholder planning artifact.

**Research.** Sources, understanding, brief, decisions, handoff or publish. The
research kit lays out the repository; the item tracks what is open. You sit at
the end: external publish or filing is the one gate, and it is yours.

**Development.** Plan when warranted, then implement, test, review, push, and merge when authorized.
Merging needs `runner_merge: auto` on the repository row, for the runner and the assistant alike, and a fresh sole-operator check before each merge.
Without it, or an operator request for that merge, stop at pull-request-ready.

The loop proceeds within the approved scope and existing permissions.
Missing authority, unresolved scope, failed checks, or exhausted review allowance stops the affected operation.
Record the blocker and next supported action.
Do not replace required approval with a proposal that the user can veto afterward.

## Defaults

These run without being asked.

- No pack command runs in a checkout whose `repo.managed` is no (sd:1620, sd:2566).
  `sd-ship`, `sd-check`, `sd-status` and `sd-ship lane enqueue` refuse there, dry runs included.
  `sd-review` runs there, review only: it reads a diff, and its gate check does not run (sd:5953).
  The refusal names the remedy: `sd fleet standard` in that checkout, which sets managed last (sd:7710).
  The Build press and auto-pickup still take a task there, with a GitHub remote (sd:7868).
  Its run reviews, runs the repository's own checks, pushes the branch and opens a draft pull request.
  No lane, merge or pack file reaches the repository; the owners merge, and the merge closes the task.
  A checkout with no row proceeds; only a row marked unmanaged refuses.
- `sd-status` reports. It never writes.
- An item reference a person reads names its repository: `system sd:5103`, or `fleet sd:5103` for an item in none (sd:5124).
  A table with a Repo column may keep its ID cell bare; trailers, `--ref` keys, `--json` and refusals keep the bare `sd:<n>`.
- `sd-review --scope branch --challenge` runs on the machine before a push.
  Blocking findings are fixed or recorded before the branch leaves.
  Under `repo.ci = local`, see **Gate first, then review the same head** under [Parallel work](#parallel-work).
- The required check runs on the exact head: `sd/local-gate` under `repo.ci = local`, CI otherwise.
  Preserve review, ownership, protection, and authorization gates.
- `sd-ship` commits enumerated paths, pushes, opens the pull request, waits
  for CI once in the background, merges with an explicit title and body
  whose trailer names the item, and runs `git fetch -p`. The repository
  setting `delete_branch_on_merge` removes
  the remote branch.
  After each confirmed in-scope merge, follow `skills/sd-ship/references/post-merge-closeout.md`.
- Expected branch protection requires pull requests, current CI, and up-to-date branches, with no required approvals.
  Configure it deliberately in GitHub; installation does not grant a protection exception.
  `sd-status` reports gaps; executable merge stops when required protection is absent.
  Protection is read from both of GitHub's mechanisms: the classic object first, and when that is absent, the branch's active rulesets.
  A ruleset with a `pull_request` or `required_status_checks` rule is the protection, held to the same guards; one that only forbids deletion or force-push is not.
  One accepted gap changes that gate: an `unprotected` entry in the repo row's `status` declaration, read at merge time (sd:5109).
  Under it, `sd-ship merge` requires every check run, every status, and a `pull_request` run of every workflow at the head to pass.
  The receipt records `declared_gap: unprotected`. Any other gap, and a `.github/sd-status.json` file, do not change the gate.
- `make check` runs `sd-docs-lint` against the checkout's own `docs/work/`, `docs/spec/` and `docs/decisions/`.
  `sd-ship` runs it again at delivery time. A consumer that wants the gate
  checks the pack out in its own workflow and runs `<pack>/bin/sd-docs-lint`
  from there; the machine-scope installer puts no `bin/` in a consumer,
  and nothing runs the lint there otherwise. A runner has no database, so
  rule 2 reads statuses from git there, with full history, and checks
  fewer items than the machine with the rows; each run prints which source
  it read. `make check` passes `--no-history`: rule 2 then fetches nothing
  and reads no `git log`, and an item only git could answer reads `unknown`.
  The delivery question stays with the lint `sd-ship` runs.
- A commit to the pack, the system repository or the writing repository names
  what needed it: `Needed-by: <item id>` or `Needed-by: cost | efficiency |
  visibility`. `sd-ship` warns when the trailer is missing and ships anyway.

## Opt-in

These run only when asked by name.

- A work item is a row: `sd task add`. Its status, notes and progress live on the row.
  Write `docs/work/<date>-<slug>/design.md` only for a change that moves state in steps (pack sd:6358).
  The design names its row with `item: sd:<n>`; there is no `prd.md` or `implement.md` to write.
  An older item keeps its `prd.md`; an active one carries no `status:` line,
  and `sd-docs-lint` fails one that does. Every checkout reads status from the
  item's row. A checkout or CI runner with no database asks git whether the
  item is delivered, by the merge trailers, and nothing else.
  `ready_to_send` marks a finished artifact waiting on you.
- Update `docs/spec/` on the same branch when a change alters behaviour it documents.
- A review pass beyond the table below. Ask for it by name; the item records
  that you did.
- `sd-handoff`. Write a packet when you stop mid-task. Followups, decisions and
  open questions are already on the item; the packet carries only what is not.

## No-CI mode

A repository whose `repo.ci` row says `local` runs no GitHub Actions (sd:1843).
`github` is the default. Every reader treats a missing column, row or library as `github`.

`sd fleet standard`, run from a checkout, brings that repository to the whole managed standard (system sd:5610).
That covers the sd-db row, GitHub merge and protection settings, Actions as `sd ci local` sets them, and the untracked fleet files.
It previews an unmanaged row too, and sets `managed` and `runner_merge` last, after every other step held.
It writes no ruleset, never turns Dependabot alerts on (off is a kept choice), and changes no GitHub setting outside `sd.fleet_owners`.

Switch a repository with `sd ci local`, run from its checkout (sd:1914).
It is a dry run; `--apply` makes the changes, and a second run finds none.
It refuses a repository the sd database does not mark managed (`repo.managed = no`, sd:1620).
It needs `admin` on the repository and makes three changes, each only where it does not hold yet:

- It sets `repo.ci` to `local`, as `bin/sd-db repo ci <path> local` does.
- It makes `sd/local-gate` the one required status check, `strict` on, in whichever mechanism the default branch uses.
  Classic protection has its required checks replaced.
  A repository ruleset keeps every other rule; only its status-check rule changes, or is added.
  The dropped contexts are named: their workflows no longer run, and a required context that never reports blocks every merge.
  A branch protected classically with no required checks refuses; add the check in the branch settings.
  A required check it cannot rewrite stops the run before any write: an organization ruleset's, or another protected branch's.
- A private repository has Actions disabled outright.
  A public one keeps Actions on for its dynamic workflows (CodeQL, Dependabot, Copilot).
  Each workflow its files declare is disabled.

After the switch:

- `sd-ship merge` checks the reviewed head out into a clean, detached `git worktree`.
  It runs `sd-check` there, not in your checkout, and removes the worktree after.
- The child drops `PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV`, `CONDA_PREFIX` and `PATH` entries inside your checkout.
  It also drops any `PATH` entry whose parent holds `pyvenv.cfg`: a virtualenv's `bin`, wherever it lives.
- A Rust repository (any tracked `Cargo.toml`) builds into a warm folder the gate owns, not the worktree's `target/` (sd:2493).
  Each repository keeps two under `${XDG_CACHE_HOME:-~/.cache}/sd/gate/`, one gate at a time in each; `CARGO_TARGET_DIR` names it.
  A warm run gets `CARGO_INCREMENTAL=0`. When both are held, the run builds cold in its worktree.
  Your own `CARGO_TARGET_DIR` never reaches the check.
  `SD_GATE_CARGO_TARGETS` sets the count, `0` switches the cache off, and `SD_GATE_CACHE_DIR` moves it.
  Cargo prunes nothing there, so the gate bounds the cache at `sd.gate_cache_gb` (sd:2598).
  Past it, the gate removes the least recently used free folders, its own last, and names each on stderr.
- This is a self-hosted runner, not a hermetic build.
  The gate guarantees a clean tree at the exact head, a scrubbed Python environment and no virtualenv on `PATH`.
  The rest of `PATH` and the system tools are this machine's image.
  The repository's own `check` entrypoint owns a hermetic environment if it needs one.
- It posts the result to that exact commit as the `sd/local-gate` status, `success` or `failure`.
  It refuses to post for any commit other than the one the worktree held.
- The description carries `inputs <digest>` as provenance: the head, the copied `CLAUDE.local.md` and the pack's `bin/` files.
  A repository whose `gate_reuse` declaration adds `"pack": "sd-check"` declares that its check runs no other pack command.
  Its inputs then hash only what `sd-check` and the gate's `sd_gate_run` import, so a pack landing elsewhere voids no receipt (sd:2722).
- Every merge attempt posts a fresh status. `sd-ship prepare` runs this same gate, through `sd-review --gate-check`.
  Prepare's pass leaves a receipt; the merge gate at the same head and binding, within 30 minutes, reads it instead of running `sd-check` again.
  The status then says `(reused)`. The merge gate never writes a receipt.
  Prepare reads one too: a pass that `sd gate check` or an earlier prepare left at the same head and binding (sd:1912).
  A repository whose row carries a `gate_reuse` declaration keys receipts by tree and merge base instead of head, for 6 hours (sd:1912, sd:5109).
  The merge base binds by its tree, not its commit (sd:2586).
  The pack's own declaration adds `"tool": "tree"`: its gate runs the gated tree's own `bin/sd-check` and binds that tree, not the checkout's `bin/` (sd:2613).
  A pack landing between a builder's gate and the lane's prepare then keeps the pack item's receipt.
  The field counts only when the running pack belongs to the gated repository; any other repository's gate keeps the checkout binding.
  A pass whose binding moved during the run leaves no receipt, and the result's `receipt_skipped` names what moved (sd:2612).
  Inputs outside the repository are not bound; `bin/sd_gate_receipts.py` names the binding and this trust boundary.
- Given the base branch, the gate passes `sd-check --base`: a repository's declared docs-only scope applies (sd:2072).
- `sd-ship merge --watch` starts no remote watch: no remote check is coming, and the gate runs to completion in the merge (sd:1875).
- The merge then requires that status as `success` at the head, posted by the authenticated account.
  Missing, failed, pending, naming another commit, or from another account: each refuses.
- Under a declared gap, the status replaces the `pull_request` workflow runs `every_check` asks for.
  Under protection, the status is required beside the protection's own contexts.
- Protection for such a repository should require `sd/local-gate`; `sd ci local` sets that.
  A merge path of the repository's own (a Dependabot merge, a script that merges) gets no status from `sd-ship merge`.
  Have it run `sd gate post --head SHA` first, or its required check never reports (sd:1989).
  After a switch, grep the repository for such automation, such as a script that polls check runs.
  `sd-status` reports it as the one produced context, so a required workflow context shows as not produced.
- Under a declared gap, a head that already carries a failed check run still refuses: a workflow that ran before the switch, or a billing-blocked one.
  GitHub reports the pull request `unstable`, not `clean`, and `every_check` requires every check run to pass.
  Under protection, `unstable` merges: only the required checks and the local gate are read (sd:2075).
  Push a fresh commit to the branch; an empty one will do. Nothing runs on it but the local gate.
- `sd fleet stamp` and `sd-review setup-github` lay no workflow and say why.
  Absence is not drift: `setup-github --check` prints `absent` and exits 0.
  A tracked route workflow never runs; `--check` names it `REMOVE`, and the stamp names it too.
  Remove it with `sd-review setup-github --remove`, which also lifts its Dependabot guard; `--dry-run` previews.
- Routing needs no workflow. `sd-ship prepare` routes in its local review pass and records the plan in the receipt.
  The route workflow only printed that plan to a job summary; nothing reads it.

## Reviews

Adversarial review runs at four points, each with a cap on automatic passes.
The points, the caps and the stop rule are in
[.claude/rules/sd-planning-adversarial-review.md](.claude/rules/sd-planning-adversarial-review.md); this page does not copy them.

The code pass reads a head. Under `repo.ci = local` a gate pass at that head
comes first; see [Parallel work](#parallel-work). A fix that changes the head
gets a further verification pass over the diff since the reviewed head, up
to the cap; `sd-ship` pushes only the reviewed head or a verified fix
of it, and merges naming that head with
`gh pr merge --squash --match-head-commit <the reviewed sha>` —
equivalently `PUT /repos/{owner}/{repo}/pulls/{n}/merge`
with `sha=` — so a head that moved after the review is refused at GitHub with
a 405. The flag is what does the refusing: a merge that carries only a title
and a body refuses nothing, whatever head moved under it.

The reviewer order gives independence: Codex reviews first by config, and
commits carry no attribution trailer (sd:3014). When Codex is down, a Claude
reviewer may review Claude's code. Skills name the roles `author` and
`reviewer`; the provider registry below maps them.

No post-merge external review runs (sd:777).

## Advisory

- Copilot is an optional second review after local review; `sd.copilot_review` under
  [Standing authorization](#standing-authorization) decides when `sd-ship` requests one.
  `sd-ship` requests at most three per pull request. Read and disposition every posted finding.

## Never in a shared repository

A shared repository resolves to `guest` mode. Detection never produces
`minimal`; only an operator writes it, and the refusals below that name
`guest` do not apply to it.
`full` requires administration, a non-fork remote, and exclusive push access.
In a shared repository:

- No `Work:` line in a pull request body unless the pull request resolves a
  work item that lives in that repository.
- No `docs/work/`, `docs/spec/`, or `docs/decisions/` commits. `mode: guest`
  already carries this: planning artifacts stay out of the repository, on
  every branch and every remote, a fork's included; no integration branch
  holds them.
  Two machines enforce that refusal. `sd-review --scope planning` calls
  `sd_lib.guest_artifact_refusal`, which resolves the mode and names refused
  paths. `sd-ship` checks the
  same list, `sd_lib.guest_refused_dirs`, before a guest push.
  A `guest_allow: docs/decisions` line in the local block takes decision
  records out of both checks for that repository (sd:2168).
  `docs/work/` and `docs/spec/` stay refused; naming either is an error.
  Nothing yet refuses a `docs/spec/` or
  `docs/decisions/` write at the moment it happens; the push gate is where
  those are caught.
- No labels, review comments, reviewer requests, or bot posts from any pack
  surface. `sd-review` never posts.
- No workflow files or repository settings unless the owner of that
  repository asked for them.
- No merge without `runner_merge: auto` on the row or an operator request for that merge;
  see [Standing authorization](#standing-authorization). No permission covers a merge that skips
  the review lane. Ownership and protection gates still decide whether `sd-ship` can execute it;
  a refusal is a stop.
- No issue filed. `sd suggest add` writes a row everywhere; `sd suggest publish`
  files it as an sd item when you run it, in the checkout you name with
  `--belongs-to`. No pack surface files a GitHub issue.

## The path for a change

Small change: branch, commit, local review, push, pull request, CI, merge.
Use the ship workflow without inventing a planning artifact.
After review, take a newer default branch with `git merge origin/<base>`, never a rebase:
a rebase rewrites the reviewed commits, and the next push no longer fast-forwards
the branch `sd-ship` pushed. `sd-ship prepare --catch-up` makes that merge.

Change that earns a work item: `sd task add` adds the row; write `design.md`
from the pack's `templates/design.md` only for a change that moves state in steps. Then the small-change
path runs with the applicable review points. An item can span several pull
requests. `Item: <item>` associates a merge without closing the item;
`Delivers: <item>` declares the delivering merge. Changes without an associated
item omit those trailers and create no placeholder record. `sd-ship` writes the
trailers as the last paragraph of the squash message, since git reads trailers
only from the final paragraph.

A criterion only the operator can observe, such as a command running unprompted
on their machine, is not a checklist box. Record it in the item's `## Log` with
its date when it is observed; the delivering pull request never ticks it in
advance (sd:1933).

`sd-ship` owns the lines `sd_lib.OWNED_TRAILERS` names: `Item:`, `Work:`, `Delivers:` and `Closes:`.
`prepare` writes `Work:` into the body it publishes; `merge` writes `Item:`, `Delivers:` and any owed
`Closes:` (sd:1600) into the squash message. Each later `--item` on the enqueue names an item the
merge co-delivers; the merge adds `Delivers:` for each. A body `Closes:` line is refused, and the title and
commit `Delivers:` lines close nothing (sd:3704). Co-delivery needs `--deliver`: an `--associate-only` claim closes nothing (sd:3365). `prepare` strips any other owned line from the
supplied body and lists it in the result's `normalized`. `Refs:` is not owned; its items stay open.
Without `--body-file`, `prepare` reads the open pull request's live body; `body_source` names the source.
`sd-ship body --item <item> [--body-file <file>] [--pr <n>]` prints the body `prepare` would publish
and runs the body lint on it. A merge made without `sd-ship` writes `Item:` or `Delivers:` by hand.

After the remote confirms the delivering merge, `sd work deliver <row-id>
<full-commit-sha>` verifies the commit, default branch and delivery trailer. It
records completion and the shipment time together. Repeating that operation
preserves the original receipt. A missing trailer or unavailable remote leaves
the claim unverified and reports the missing evidence; a bare merged branch or
stale issue status cannot close the item.

A whole-item merge prepared without `--deliver` carries `Item:` and no
`Delivers:`, so `sd work deliver` refuses it. `sd work deliver <row-id>
<full-commit-sha> --associated --reason TEXT` closes that row. It runs the same
reachability check, accepts the `Item:` trailer for the row instead, and records
the trailer and the reason on the receipt. A task or followup has no receipt:
its move to done records the delivery sentence and the reason. It refuses a
missing reason, and `sd-ship prepare --deliver` on such a record names it.

A `--deliver` squash GitHub lands on a base its review did not cover is held:
reconcile records the merge and not the delivery, and no git reader takes its
`Delivers:`. Once the combined tree passes and the operator closes the row by
hand, reconcile clears the hold and records `closing_owed` on the receipt. The
next `sd-ship merge` in the same repository then writes `Closes: <item>` into
its squash's trailer block, at most 10 per squash, and its result names each
in `carried_closes`. Its reconcile marks them paid only if that squash is not
held itself. Accepted gap (sd:1600): until that merge lands, a reader with no
database still sees the item open; `closes_left` names debts past the cap.

`sd work cancel <row-id> --reason TEXT` records cancellation immediately,
without a status-file change or another pull request. It does not claim the
work shipped. A later associated merge may carry `Closes: <item>` for context;
that merge is not a prerequisite for database completion. Readers with no
database can use explicit `Delivers:` or `Closes:` evidence to see that work is
closed, while only `Delivers:` says it shipped. A shallow clone that cannot
establish the evidence reports uncertainty.

`sd task cancel <row-id> --reason TEXT` closes a task or followup nobody will
do. It writes the same `done` status and `cancelled` receipt, through the same
library call with the task guard (sd:1005). `sd task status <row-id> done`
writes no receipt, so the row reads as finished work.
The guard refuses a recurring task and a row with an active assignment.
An `sd_db` older than the guard refuses the verb by name.

The item directory stays in place. Use `sd work relink <row-id> <path>` when an
artifact moves: it preserves the row, notes and original source identity. No
command automatically deletes or archives a completed item directory.

## Mutation checks

A mutation check edits the code a test covers, runs the test, and restores the
edit; a test that passes on the mutation has not earned its place.
Run a Python mutation check with `PYTHONDONTWRITEBYTECODE=1`, and delete the
`__pycache__` folders first (sd:1790). Python reuses a `.pyc` whose recorded
source size and whole-second mtime still match, so a same-size edit restored
within a second runs stale bytecode: a mutation reads as killed or survived by
timing, and a restored file can fail its own test. The variable stops writes,
not reads, so a cache left from an earlier run still answers.

## Parallel work

The harness fans work out only when a `CLAUDE.md` or a skill asks for it.
These rules say when to ask. They hold wherever a pack skill runs.
A skill that dispatches workers cites this section and restates nothing.
The pack ships a writer for multi-file code: the `sd-slice-builder` agent, which the installer places in `~/.claude/agents`.

This section holds rules. The mechanism is in the tools: `sd-ship lane --help`, `sd gate --help`, `sd-check --help`,
[docs/sd-db.md](docs/sd-db.md) and `skills/sd-ship/references/recovery.md`.

Four roles share the work:

- **The lead plans and lands.** One lead session per machine plans, starts builders, runs the merge lane and records state in `sd task note`.
- **A builder writes code in its own worktree.** It runs only the suites its change touches, never `make check`,
  and ends with commits, a pull-request body file and a report. The lane's prepare runs the one full gate (system sd:5800).
- **A reader is read-only and unlimited.** Investigation, review, planning and audits fan out across readers; a reader needs no worktree.
- **An integrator is a builder that merges several builders' work** on one branch, so the lane lands one pull request, not several that conflict.

Workers:

- **Run at most 6 code-writing builders per machine, and one build bundle building per repository at a time.** More starve the gates and reviews they wait on,
  and one serial lane lands about 3 at a time: the rest only queue and go stale against main (sd:5617).
  A repository's later items wait, and the next bundle takes them (sd:8158).
  A bundle in the lane frees its repository's slot: the lane stays serial, so the next bundle builds while it lands (sd:8277).
- **Start no builder while load5 is above 40.**
- **A writer runs alone in its checkout.** Any agent or session that changes files works in its own git worktree or clone.
  Never start a second writer in a checkout that already has one.
- **Make worktrees outside the repositories folder**, such as `~/worktrees/<repository>-<branch>`:
  a tool that reads every folder there as a checkout takes a worktree for one.
- **No worker fails silently.** Every worker gets a budget and reports the moment its last step ends, pass or fail.
  No report by the deadline is a failure, and so is a worker gone idle without one. Do not poll; do not assume success.
  Cancel a silent writer and confirm it stopped before starting a replacement; if you cannot confirm, escalate.
  Then write one `sd task note` on each of its items: what stopped, its head and what is left (pack sd:5963).
  Only a read-only worker may be replaced on the deadline alone.
- **Register each in-session builder so Today lists it** (pack sd:7221): on spawn run
  `sd assignments beat <items> --start --worktree <path> --branch <branch> --budget <minutes>`; on its final report, `--end` before the lane enqueue, which refuses a held item. Only `--end` ends a run: silence and a done item do not.
- **Fan out only when the targets are independent, share no mutable state, and are cheap to verify.**
- **Keep one open bundle branch per repository**, one commit per row; rows that change the same files always group.
  Open a second pull request only for a change that moves state in steps or when the bundle is already in the lane (pack sd:7737).
  `--start` refuses a second open branch of the repository, naming the open one; `--into <branch>` starts a side branch that merges into it, and `--reason state-move|in-lane` opens a second one and notes why.
  Name every grouped row in the pull request. Where goldens exist, bless them once per bundle.
  Add a bundle's shadow columns in one schema migration, not one per feature (pack sd:6358).
  Bundling comes before parallel lanes: revisit them only when Metrics' Lane times median wait (pack sd:6006) passes 30 min (pack sd:7412).
- **Repository work uses the existing `ready` status (pack sd:6000).** A second status would split one queue in two.
  A ruling with a clear spec moves its item to `ready`: nothing is left to ask before a build.
  The lane brief and "pick up open items" take `ready` items first: a builder can finish them without a question.
  Rank `ready` items by value per hour: one developer's hours are the budget.
  No intake cap: the ranking orders the work, and a cap would only hide rows (grill Q45; Q1 ruled no rate limit).
- **Large uncommitted data goes under `sd.bulk_storage_root`**, as `<root>/<repository>/`. Build output and owner-sensitive data stay on the system disk.
- **Test one version per language, the latest stable (Python 3.15, Node 26); no version matrices.**

Gates and reviews:

- **Shift lane failures left: review and test before enqueue (sd:5617).** 17 of 40 prepares on 2026-10-20 and 21 were redone:
  10 review blocks, 3 gate failures after review and 3 conflicts with main, all visible before enqueue.
  The builder merges `origin/main` and reports, with no review of its own (the `sd-slice-builder` flow, step 6).
  The lead runs `sd-review -C <worktree> --scope branch` in the background, with no time cap (system sd:5825):
  a builder may run only foreground commands under 10 minutes, and design reviews overran that cap.
  The lead sends blocking findings to the idle builder, which fixes them fail-first; the lead reviews again.
  `sd-review` records no pass the lane reuses, so the lane reviews the branch once more at prepare.
  For a UI change the lead also looks at the builder's page shots, dark and light at desktop and phone width (sd:6007):
  misaligned dropdowns, missing help icons, a missing Park and counts in labels passed review and tests.
  Then the lead runs the tests the change touches
  (`make check CHANGED=<paths>`, or a repo's `make test SUITE=<name>`) and enqueues on a pass; the lane's prepare gate is
  the one full run (system sd:5800). A pre-enqueue pass is reused only when main has not moved, so it doubled gate load;
  3 of the 17 redone prepares were gate failures. A failure goes back to the builder, not into the lane.

- **Iterate on the fast path; gate once before the push.** `make check CHANGED="<paths>"` runs the tests those paths need.
  Only the full gate counts as evidence.
- **Gate first, then review the same head (sd:2603).** Run `sd gate check --base main`, then `sd-review --scope branch --gate-check main` within 30 minutes.
  Run both with the same environment, since the receipt binds it. A fix commit moves the head, so it needs a new gate pass.
  Never run plain `sd-review --scope branch` under `repo.ci = local`: it runs a second full check and reuses no pass.
- **A change that moves state in steps carries a failure table before review round 1:**
  each step, the state it moved, the failure, the recovery and the test.
- **A repeating finding starts a class pass.** The trigger is two findings of one class in different rounds, or three blocking rounds in a row.
  Name the class, list every instance from the code rather than the findings, add each to the failure table,
  and fix each row that lacks a recovery or a test, fail-first. Record the trigger in `sd task note`. Split the pull request if the table shows it does too much.
- **Gates share the machine through slots** (`sd.gate_slots`). Every `sd-check` run and every gate `sd-ship` runs queues in one machine-wide order.
  Wrap any other gate as `sd gate run -- make check`. Use no per-repository `lockf` and do not wait on the load average. `sd gate status` shows holders and waiters.
- **Gates start in the order they queued** (sd:5409). Every `sd-check` run is a lane gate; a builder's `sd gate run` waits its turn beside it.
  No later lane gate overtakes a waiting run, and a run that holds a slot keeps it. `--max-wait` bounds the wait: exit 75, the command not run.
- **A repository's `precheck` target runs before the slot wait.** A failure stops the run there.
- **Reviews take their own slots** (`sd.review_slots`) before the first reviewer starts.

The merge lane:

- **The lead ships only through the lane.** `sd-ship lane enqueue` adds an item; `sd-ship lane run` drains the queue in order.
  A hand-run prepare-and-merge chain jumps the queue and leaves no log. Verbs: `enqueue|list|retry|cancel|move|hold|release|run|watch|prune`.
- **`lane retry <item>` queues a failed, skipped or prepared entry again** at its head; a cancelled one needs `--expected-head`.
  It refuses `head_moved` for a branch that moved past the head, as enqueue does (pack sd:6057): enqueue the new head instead.
- **`lane cancel <item>` clears a prepared entry merged by hand** (sd:5743), once a `merged` ship receipt of the item holds its head.
  It also clears the item's newest failed or skipped entries, back to its last merged or prepared one (pack sd:5838): their work landed in another bundle.
- **Keep one `sd-ship lane watch --item A --item B ...` naming every item the lead has in that lane.**
  Restart it after each enqueue, after an sd outage and after any exit whose output lacks `ok: true`:
  a bare `lane watch` never exits, and a hub build mismatch ends a watch with `ok: false` (`hub_unavailable`).
- **Stop an item's queued ship before its builder merges main.** Run `sd-ship lane cancel <item>`: the ship merges in the same worktree, and its abort can discard the builder's merge.
- **Stop a running ship chain by its process group** (`kill -- -<pgid>`). A killed shell alone leaves its children holding a gate slot.
- **Metadata that orders landings** (a session number, a ledger entry, a changelog position) is allocated when the work lands, never when the branch is cut.
  An item id is not that kind of metadata. Run `sd-ship prepare` and `merge` back to back for one item (sd:2339).
- **After a lane merge, clean up once the builder has stopped.** The runner deletes the remote branch and each local branch proven landed (sd:5700), and notes the item with the command that removes the worktree.
  A branch is proven landed when no worktree holds it and merging it into origin's default branch, or into a merged lane head from where it forked, changes nothing: content, not history, since a bundle may revert a commit before its squash.
  It never removes a worktree: a live builder holds it open, and a removal loses its writes. Remove the clean worktree and its build output by hand; keep unpushed unmerged work.
  `sd-ship lane prune [--dry-run] [--json]` deletes the older proven-landed branches of one repository and names each kept branch with its reason.
- **Each lane runs on its lane host** (`repo.lane_host`; NULL is the hub, sd:3003). Off that host the ship verbs refuse `lane_elsewhere`.
  [docs/sd-db.md § Lane host](docs/sd-db.md#lane-host) covers the move.
- **One hub holds the workflow database; a satellite reaches it over the network.**
  A verb that needs a lock or a directory beside the database refuses with `HubOnly` off the hub: run it on the hub.
  The list and the refusals are in [docs/sd-db.md § Hub-only verbs](docs/sd-db.md#hub-only-verbs).

A session writes on its own branch in its own worktree, one writer per worktree: `git worktree add` makes one.
Before writing a fix, look on the remote for a branch or pull request that already claims it, by the item id or by the changed path.

## Modes

`CLAUDE.local.md` carries one `mode:` line per repository. The installer writes
the block; the file is untracked by construction.

| Mode | Where planning artifacts go | What ships |
|---|---|---|
| `full` | `docs/work/` in the repository | everything above; merge only with `runner_merge: auto` on the row |
| `minimal` | nowhere, by convention only; nothing enforces it | the small-change path only |
| `guest` | nowhere in the repository; both checks below refuse them | the small-change path to pull-request-ready; no posts, no labels |

`minimal` holds no work items by agreement, not by a check.
`sd-docs-lint` skips its `docs/work` rules there: a local folder and the pages that name it are not linted.
`sd_lib.guest_artifact_refusal` and `sd-ship`'s push check refuse
`docs/work/`, `docs/spec/` and `docs/decisions/` in `guest` only, so a
`minimal` repository can commit and push them unrefused. `sd-ship` also adds
the `Work: sd:<id>` line in `minimal`, as it does in `full`. Only `guest`
refuses the review routing lane (R10-D5): `minimal` is written by hand and
never detected, so it names the operator's own quiet repository, and may
install the lane (sd:1292).

Access decides where artifacts go, whichever namespace holds the
repository. Without a `mode:` line, the pack asks three questions of the
remote: can you administer it, is it not a fork, can anyone else push. Admin,
not a fork, nobody else: `full`, in your namespace or an organisation's.
Anything else, including no answer: `guest`. A root with no remote, **or
no git at all**, is `full`; there is no one to expose anything to. The
no-git case is decided on its own rather than falling into whichever
exception branch the remote lookup raises. The questions are asked
again before every artifact write and every push, and a `no` makes the
run `guest` whatever the line says: a `mode: full` you wrote is a ceiling,
never a floor: detection lowers it and never raises it, so a repository
that gains a collaborator stops
receiving your planning artifacts before the next push, not after the
next merge. The push check reads what the push adds over `origin/<base>`
and refuses a guest push that carries planning paths, to an upstream or to
your own fork alike; no fork integration branch exists. A lowered run leaves a note on the
item saying which answer lowered it, once per item and remote however many
runs it takes, and `sd-status` names the planning artifacts the shared tree
was already carrying, which are yours to move. Mode never decides merging.
`runner_merge: auto` is a per-repository policy you set once with
`bin/sd-db repo runner-merge <path> auto`, off by default, and nothing derives it.
Every merge asks the same three questions again, one function for both gates.
Two of the three answers are final: a repository you do not administer, and a
fork, are refusals no setting reaches, and so is a question the remote could
not answer at all. The third -- "nobody else may push" -- is the one the row
speaks for, because co-ownership is exactly what you decided about when you
set the row. `auto` answers it and a merge proceeds; `manual`, no row, or a
database that cannot be read suspends it with the reason shown. A merge the
row let through says so on its receipt, naming the repository, the setting and
who else may push, so an ownership merge and a row-authorized one are not one
sentence.

## Providers

The provider registry maps roles to providers.
Reusable skill procedures name roles; operator policy owns preferred entries.

Cheap, standard, and deep changes ask two independent local reviews (sd:7519).
With one eligible reader, the review asks one and its result names the shortfall (`review_shortfall`).
The first two readers on the reviewer order run at once: with the shipped order, codex and Claude.
A reader that fails gives its slot to the next entry; the receipt keeps the registry order.
Claude's reader also gets the ranked addition: trace callers and config readers, rank by production impact.
Skip requires none; planning, lensed and challenged reviews keep a floor of two, so a research brief gets both.
Tier selection still follows repository policy; the tier never adds a third reader.
Complete local review before any Copilot request; [Standing authorization](#standing-authorization) holds the policy.
A completed Copilot review must cover the merge head, or an ancestor whose diff to it touches only skippable `docs/` paths.

    bills:
      anthropic: { cost: subscription }
      openai:    { cost: subscription }
      moonshot:  { cost: prepaid }
      minimax:   { cost: plan, meter: "https://www.minimax.io/v1/token_plan/remains", meter_env: MINIMAX_API_KEY }
      baseten:   { cost: company, cap_usd_month: 50 }
      local:     { cost: local }
    providers:
      claude:  { start: "claude -p",  vendor: anthropic, bill: anthropic, roles: [author, reviewer], reader: claude-json }
      codex:   { start: "codex exec", vendor: openai,    bill: openai,    roles: [author, reviewer], reader: codex-json }
      opencode: { start: "opencode run", model: openai/gpt-5.5, vendor: openai, bill: openai,
                  roles: [reviewer], reader: opencode-json }
      kimi:    { url: "https://api.moonshot.ai/v1", model: kimi-k3, vendor: moonshot, bill: moonshot,
                 roles: [reviewer], max_tokens: 65536, price: { in: 3.00, out: 15.00 } }
      minimax: { url: "https://api.minimax.io/v1", model: MiniMax-M3, vendor: minimax, bill: minimax,
                 roles: [reviewer], max_tokens: 65536, price: { in: 0, out: 0 } }
      baseten: { url: "https://inference.baseten.co/v1", model: deepseek-ai/DeepSeek-V4-Pro-0813, vendor: deepseek,
                 bill: baseten, roles: [reviewer], max_tokens: 65536, price: { in: 1.32, out: 3.96 } }
    roles:
      author:   [claude, codex]
      reviewer: [codex, claude, opencode]

A capped bill takes `url` entries only, because the library makes those
calls and can refuse one before it is sent; a `start` entry on a capped
bill is refused when the file is read.

Each entry also carries `env`, the list of variables the entry's process
receives beside `PATH`, `HOME`, `LANG`, `TERM` and `TMPDIR`, `env:
[OPENAI_API_KEY]` for `codex`; left out of the example above for width. A
session inherits the variables its own entry names and no other entry's.
That is inheritance, not isolation: the session runs as you, in your
`HOME`, and can read the file the keys live in.

Model pins, meters, token ceilings, reasoning controls and `response_format`: [docs/providers.md](docs/providers.md).
Change a pin by editing the registry file, never by a page.
A `url` answer that fails the findings schema retries once on the same entry only when its `price` is zero in and out;
a priced entry falls through to the next reviewer.

Adding a provider is an entry; adding money is a bill. Both role lines are
read in order. `author` is picked when an assignment starts and never switched
mid-item. `reviewer` is the first entry that is enabled, is on no bill at its
cap this month, and answers its preflight (the cap check is below).
The registry refuses a file whose first `author` and first `reviewer`
are the same entry; no check reads which vendor authored a change. A rate limit,
a missing binary, a failed run or a timeout falls through to the next, and the
run says which one reviewed and why the earlier ones did not. With none left,
the review refuses by name rather than reading its own work.
Only entries on the reviewer order participate in automatic fallback.
Enabled reviewer-capable entries outside that order require an explicit `--provider` selection.
With a database, the order in force is its rows: `sd providers configure` sets it, `sd providers list` shows it, and `sd-review --explain` names it on its `order from` line; the file's `roles:` lists only seed those rows. The shipped seed contains Codex, then Claude, then opencode; other providers remain explicit-only until an order names them.
Existing provider files and database orders remain unchanged until the operator migrates them.
The chain continues until the required count completes or eligible entries run out; the
required readers run at once, and a failed one's slot takes the next entry. A completed
review with findings counts; it does not trigger a replacement. Consent, author
exclusions and spending limits apply to every fallback, and earlier findings remain.
`vendor` is the
maker of the model, not the tool; `bill` is whose money. A bill with
`cap_usd_month` is held per call, and a `url` entry on a capped bill is
held to it before its request is sent. Every `url` call `sd-review` makes
goes through the library's `sd_db.calls.call` (`source:bin/sd-review::charged_call`):
the call's bound, the prompt's estimated tokens at `price.in` plus
`max_tokens` at `price.out`, is reserved against the bill's month; the
request is claimed, sent once, and the row is settled at the usage the
answer carries or bound at the reservation when it cannot be costed. A
bound that would pass the cap is refused by the ledger and the fallthrough
passes the entry over. Before the chain is built, `review` sweeps
reservations whose process is gone and asks the ledger which capped bills
are at their cap this month (`source:bin/sd-review::capped_bills`); those
reach `reviewer_chain` and `pick` in `bin/sd_registry.py` as bill name to
the month's total, so the fallthrough skips every entry on such a bill and
`--provider` refuses one by name, saying what the month spent or holds.
Once the registry read succeeds, a fault at the ledger -- no library to
reach with no state file beside the registry, a library older than
`sd_db.calls`, or a reservation the database will not take -- refuses an
entry on a capped bill naming the fault and dispatches an entry on an
uncapped bill as before: unknown is not uncapped. A state file that exists
but cannot be read, or exists while the library cannot be imported, is
refused at registry read (`source:bin/sd_registry.py::read_runtime`), and
nothing is dispatched. A `url` entry on a capped bill without a usable
`price.in`, `price.out` and `max_tokens` is refused at registry read, on
the file and on the merged rows, since a cap the ledger cannot reserve
against is not a cap. Entries with `url` share one OpenAI-compatible client
and one reader. This file is identity and seed; enabled, order and caps
are rows the dashboard edits, and the library merges file and rows on
every read.

`sd-review --provider <name>` picks one entry for one run. The dashboard's
item screen offers the same list, with vendor, cost and reason beside each
name. Copilot and Greptile are not entries: they post on the pull request, and
this lane never posts.

This file is the only list of provider identities. `sd-review` reads it through the
library. The repo row's review declaration carries repository policy, paths and severity;
it names no provider chain. Effective authorization restricts the registry's
reviewer chain before vendor, transport, availability and spending gates run.

## Standing authorization

Core settings use `sd config get|set|unset|list` and the existing atomic machine configuration writer.
The file is `~/.config/sd-ai-command-pack/config.json`, honoring `XDG_CONFIG_HOME`.
These keys serve every machine and live in the workflow database as `setting` rows: `sd.fleet_owners`, `sd.copilot_review` (sd:3989), `sd.hub_name` (sd:4044), `sd.status_companions` (sd:5098) and the `sd.notion_*` publish ids (sd:5141).
Every plugin key (`hoa.*`, `sdw.*`, `songs.*`) is a `setting` row too (sd:5109).
`sd config set` and `unset` write the row; with a database the row alone answers, the file's line for that key is not read, and `get` names it on stderr.
A machine with no database and no `hub.json` keeps them in the file. A database that cannot be read is an error, never the file's value.
`sd config list sd --json` gives each key's `source`: `database`, `file` or `default`.
Merge permission is one setting, `repo.runner_merge`: a column on the repository row in the workflow database, not an `sd` key.
Set it with `bin/sd-db repo runner-merge <path> auto|manual`; `bin/sd-db repo list` shows it as each row's last field.
`auto` lets the runner and the assistant merge active, in-scope work without asking, only through `sd-ship prepare` then `sd-ship merge`.
No value permits a merge that skips the review lane, such as a raw `gh pr merge` or a web squash; a refusal from `sd-ship` is a stop.
`manual`, no row, or an unreadable database means ask the operator first. An explicit instruction to wait wins.
`sd.assistant_merge` is retired: `repo.runner_merge` is the assistant's setting too.

The reserved `sd` namespace declares these settings:

- `sd.external_reviews`: `configured` permits private code and scoped review context to eligible configured providers.
  It includes future registry entries; registry configuration chooses capability, while this explicit operator grant authorizes transmission.
  `deny` vetoes all local allowances. Absence supplies no standing grant.
- `sd.copilot_review`: when `sd-ship` requests a Copilot review by itself. `deep` requests one on deep-tier changes only,
  `always` on every reviewing tier, `never` on none. Absence reads `deep`, so a repository with no
  review declaration gets Copilot on deep changes and on nothing else.
  A review declaration that names `copilot_review.automatic_deep` overrides `deep` and `always`; one that does not inherits.
  `never` wins over the declaration.
  `sd-review` reports the effective policy, its source and the repository's say under `remote_reviews.copilot`.
  `sd-ship` resolves the decision again at dispatch, from the setting as it stands then and the tiers the
  retained passes recorded, so a setting changed after the review takes effect without another review.
- `sd.gate_slots`: how many gates (`sd-check` runs, `sd gate run`, the pack's `make test`) may run at once on
  this machine; `0` is no cap. Absence reads a quarter of the cores. `SD_GATE_SLOTS` overrides it
  for one run. It grants nothing;
  see [Parallel work](#parallel-work).
- `sd.review_slots`: how many reviews may run their reviewers at once on this machine; `0` is no cap.
  Absence reads 2. `SD_REVIEW_SLOTS` overrides it for one run. It grants nothing.
- `sd.gate_load_max`: the gate queue starts a gate only while load1 is below this; `0` is no load condition.
  Absence is no load condition (sd:2607). `SD_GATE_LOAD_MAX` overrides it for one run. It grants nothing.
- `sd.gate_settle_seconds`: seconds between two gate starts, and of low load1 while load5 is high; `0` is none.
  Absence reads 45. `SD_GATE_SETTLE_SECONDS` overrides it for one run. It grants nothing.
- `sd.fleet_owners`: comma-separated GitHub logins whose repositories `sd fleet stamp` treats as the operator's own.
  Absence reads the deprecated `fleet.owners` list in the machine config, then the pack's default, `platypeeps`. It grants nothing.
- `sd.hub_name`: the hub's tailnet name, its MagicDNS name or bare machine name. A machine with its own database serves it
  on the tailnet and runs lanes from it only while its tailnet name matches; `bin/sd-db fence` answers.
  Absence is no fence. It grants nothing; [docs/sd-db.md](docs/sd-db.md) has the answers and callers.
- `sd.status_companions`: the repositories `sd-status` lists beside the one it runs in, labelled by repository,
  as `<repo>=<other>[,<other>]` with `;` between repositories, each spelled as `bin/sd-db repo list` names it.
  Absence lists none. A machine with no database refuses `set` (sd:5109). It grants nothing.
- `sd.jira_recent_days`: the days `sd-status` looks back for Jira rows; it lists the assigned or filed rows Jira
  updated inside them and counts the rest. A whole number from 1; absence reads 30. It grants nothing.
- `sd.gate_cache_gb`: the most gigabytes the local gate's warm Rust build folders may hold; `0` is no bound.
  Absence reads 40. `SD_GATE_CACHE_GB` overrides it for one run. It grants nothing.
- `sd.lane_root`: the folder that holds each repository's `sd-ship lane` logs and runner lock, as `<root>/<repository>/lane/`.
  The queue is in the hub database; a `queue/queue.json` an earlier version left there is imported once and kept as `queue.json.imported-<stamp>`.
  Absence reads `$XDG_STATE_HOME/sd/lanes`. `SD_LANE_ROOT` overrides it. It grants nothing.
- `sd.bulk_storage_root`: the folder for large uncommitted data, as `<root>/<repository>/`; see
  [Parallel work](#parallel-work). Absence is no bulk root. It grants nothing.
- `sd.privacy_patterns`: the privacy-pattern file `sd-docs-lint --pr-body` checks a pull request body against, one
  extended regular expression per line. Absence reads `privacy-patterns` in `$SYSTEM_TOOLS_CONFIG`, else in
  `${XDG_CONFIG_HOME:-~/.config}/system`. No file skips the check with a note. It grants nothing.
- `sd.notion_private_folder` and `sd.notion_team_folder`: the Notion page id, or page URL, of the folder a `notion=` mirror goes to;
  `notion=dict(team=True)` takes the team one. `sd.notion_experiments` and `sd.notion_experiment_tasks`: the data source ids
  `sd-research-kit project` writes item rows to. While the database holds no row for one, its old variable answers
  (`SD_NOTION_PRIVATE_FOLDER`, `SD_NOTION_TEAM_FOLDER`, `SD_NOTION_EXPERIMENTS`, `SD_NOTION_EXPERIMENT_TASKS`).
  A machine with no database refuses `set`. They grant nothing.

Installation supplies no grant. A new operator must state their own policy; never copy another user's personal permission.
These settings start no background work, enable no runner policy, and bypass no ownership, review, CI, or protection gate.
Review depth, author exclusions, spending limits, and the review table's automatic pass caps remain unchanged.
The upstream pull-request exception in `AGENTS.md` still requires permission for that specific PR.

Review resolution is ordered: machine deny; present local restriction; configured standing policy; otherwise refusal.
A local named list restricts recipients. An explicit empty list denies all; malformed local consent refuses.
An absent local key inherits standing policy. `--explain` and review receipts identify the authorization source.
Linked worktrees share the main checkout's local block; installer output and review receipts name that canonical path.
Unreadable existing configuration paths refuse, including directories and dangling links.
Ship receipts bind the external-review policy source and value; changing it invalidates the recorded review.
Unrelated machine settings do not invalidate reviews.

## Overrides

The `CLAUDE.local.md` block carries these keys, and the pack reads no others.

    mode: full | minimal | guest
    check: <the command that verifies this repo>
    test: <optional, when the repo spells its tests separately>
    lint: <optional, same>
    reviewers: <entry@recipient pairs that may receive this repository's diff, e.g. claude@claude+3f9a1c2e, baseten@inference.baseten.co>
    guest_allow: docs/decisions   (optional; the only tree a guest repository can take out of the refusal)

`check`, `test` and `lint` run in that order and are optional; one combined command can use `check` alone.
`reviewers` restricts the effective authorization described above. Each local entry binds its destination:
a URL host, or an executable with a fingerprint of its command and declared environment names.
A changed destination or fingerprint needs a changed local allowance. Standing `configured` policy follows the current registry instead.
Transport restrictions and the operator's responsibility for tool configuration remain unchanged.

The installer preserves an existing answer, including empty denial. Malformed existing blocks and unknown answers refuse without rewriting consent.
Without local consent it inherits an explicit machine policy, or offers enabled recipients with no default grant.
An explicit empty answer writes a durable empty restriction. Earlier installers erased some empty answers;
missing historical keys cannot distinguish those answers from repositories never configured. Inspect them before granting standing policy.

Everything under **Opt-in** above is asked for by name, in the moment, rather
than switched on in a file. Naming it is already the whole cost, and a key that
turns a lane on permanently is a default in disguise: it stops being a decision
you make about this change and becomes one you made about this repository,
months ago, for reasons the file does not record.
