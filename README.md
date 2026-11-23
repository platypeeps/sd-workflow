# SD AI Command Pack

[![Python](https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-unittest-2E7D32)](#verify)
[![License: MIT](https://img.shields.io/github/license/platypeeps/sd-workflow)](LICENSE)
[![Source](https://img.shields.io/badge/source-GitHub-181717?logo=github)](https://github.com/platypeeps/sd-workflow)

## Overview

One repository, one prefix, one machine-scope install. The pack renders its
`sd-*` surfaces into the AI tools installed on a machine and gets out of the
way. `sd --help` and `sd skill list` enumerate the installed commands and skills. It
does not install anything into the repositories you work in.

That last sentence is the design, not a summary of it. The previous version of
this pack copied roughly 56,000 lines of payload into every consuming
repository, kept those copies in sync through a release train, and needed a
plugin marketplace, a fleet registry, and a receipt protocol to do it. All of
that existed to solve a problem the pack had created for itself. The
replacement renders from `skills/` at install time, so there are no copies to
keep in sync, no versions to roll out, and nothing tracked in a repository that
the framework owns.

What renders is what a path names. `skills/paths.json` describes three
pipelines — **research**, sources to brief to handoff; **development**, plan to
build to ship; **act**, brief to draft to send — and the installer writes
exactly the skills those paths name. Every other skill lives in `contrib/`: in
this repository, versioned, documented, and not installed. `sd skill try
<name>` installs one for thirty days and writes down when it started; if
nothing used it by then, the next install run removes it and says so.

The split is not a ranking. A path says which skills are steps in one
sequence somebody actually runs, and that is what these three say.
`sd skill list` prints the date of each skill's last use, and `--usage`
adds its recorded uses; the weekly KISS audit lists the unused ones. A
skill in `contrib/` is one command away, and use is what moves it.

`sd skill intake <GitHub URL>` judges an outside skill against the installed
set: its risk, its overlap with installed skills and its value, then a verdict
of install, fold or decline. It installs nothing until `--approve` names the
revision the report printed; the skill then lands in `contrib/` and records its
source URL and commit. `sd skill intake <name>` fetches that source again,
shows the diff and updates only on the same kind of approval.
`sd skill intake --track <repo URL>` adds a source repository to the tracked
list, one workflow database setting row (`skills.intake_repos`). Until that
row is first written, the list is read from the installed skills' Provenance,
the `skills` CLI's `~/.agents/.skill-lock.json`, and the GitHub origin of any
git checkout a user skill links into. `--refresh <repo URL>` judges each skill
there and lists the new and changed ones, each with its approval line; it
installs nothing. With `--apply` it also installs the new revision of each
installed skill whose verdict is install, and prints each one's rollback. `--untrack` stops tracking, and installed skills stay.
An install keeps the copy it replaced. `sd skill intake <name> --rollback`
shows the swap back to that copy, and `--approve` makes it; a second
rollback undoes the first. It refuses a locally edited copy, and a pack
skill git already holds: revert that commit through the lane. The intake
report and the rollback report list the pack skills whose Lineage or
Provenance names the same repository, with the commits that added it.

A repository can extend the framework from the other side. `sd plugin add`
registers a **domain pack** — its own manifest, its own `<prefix>-*` skills, its
own rows — and three exist today. What a manifest may declare, and what the
other halves are, is in [domain packs](docs/domain-packs.md).

**What it writes on a machine:**

- `~/.claude/skills/sd-*/SKILL.md`
- `~/.codex/skills/sd-*/SKILL.md`
- `~/.config/opencode/commands/sd-*.md`
- `~/.claude/agents/sd-*.md` — and `--user` removes a hand-placed agent that
  one of them replaces, such as `slice-builder.md` for `sd-slice-builder`,
  when its digest is a known copy; an edited copy stays and is reported.
  `AGENT_PREDECESSORS` in `bin/sd_install.py` is the list. `sd-slice-builder`'s
  frontmatter runs the `sd-builder-guard` hook by name, before Bash and at
  Stop, through the link below.
- `~/.local/bin/sd` and `~/.local/bin/sd-*` — one symlink per executable in
  `bin/`, so the commands resolve from any directory; `--bin-dir DIR` puts them
  elsewhere. The installer never edits `PATH`: `--user` warns when the link
  directory is not on it, and `--status` says how many commands resolve.
- seven hook entries in `~/.claude/settings.json` — `SessionStart` for
  `sd-handoff-restore` (`startup`, `clear`) and `sd-session-trail`
  (`compact`, which prints the items, decisions and questions the session
  wrote, read back by its `CLAUDE_CODE_SESSION_ID`), `PreToolUse` and
  `UserPromptSubmit` for `sd-skill-use`, and `PreToolUse` on `Bash` and `mcp__github__issue_write`
  for `sd-issue-guard`, which decides by the target an `issue_write` call names: it
  denies an issue in a managed repository and points to `sd task add`, and
  asks the operator about an issue anywhere else; with `policy.issues`
  ask, it asks about an update of a managed repository's own issue too;
  `PreToolUse` on `Bash`, `Edit`, `Write`, `NotebookEdit`, `Read`,
  `Grep` and `Glob`, and
  `SessionEnd`, for `sd-session-beat`, which keeps the session's heartbeat
  row and the worktrees it holds (`sd assignments dispatch --sessions`), and puts a
  teammate's `agent_id` in front of each of its Bash calls that runs `sd`,
  so its notes leave its lead's trail.
  `bin/sd_install.py`'s `HOOK_SPECS` is the one list; this
  line describes it and does not govern it.
- one line — `CLAUDE.local.md` — in the global git excludes

**What it writes in a repository:** nothing at machine-scope install. Everything
below is written by something you invoke against that repository, and nothing
else is. Its executables write these paths, and no others:

- `CLAUDE.local.md` — per-repo configuration, from `python3 bin/sd_install.py --repo`.
  Untracked by way of that one excludes line, and that command refuses outright
  if `CLAUDE.local.md` turns out to be tracked, rather than edit a file under
  version control.
- `.github/workflows/sd-review-route.yml` — the routing lane, from `sd-review
  setup-github`. **Tracked.** A `repo.ci = local` repository gets none; see
  [WORKFLOW.md § No-CI mode](WORKFLOW.md#no-ci-mode). With
  `--remove-legacy` it also deletes the three files the old `sd-github-review`
  installer left. `--remove` deletes the workflow and its Dependabot guard.
- The fleet stamp, from `sd fleet stamp`, into the checkout you stand in, which
  must be a checkout of a managed `runner_merge=auto` repository (sd:1620): the routing lane and
  its Dependabot guard as `setup-github` writes them,
  `.github/workflows/sd-check.yml` where no other workflow runs on
  `pull_request` (created only: an existing one is kept as written), and a `docs/dashboard/` line in
  `.gitignore`. **Tracked**, and written only on a feature branch.
  Where the remote says nobody else may push, GitHub reports the default branch unprotected by both
  classic protection and rulesets, and the repo row's `status` declaration accepts no `unprotected` gap,
  the plan prints the `sd-db repo declare` command that adds it; the stamp writes no row.
  A protection read that fails is reported `unknown` and prints nothing.
  A repository whose `status` declaration or `CLAUDE.md` rule forbids CI gets no workflow.
  A repository whose `repo.ci` row says `local` gets no workflow either, from
  the stamp or from `setup-github`; see [WORKFLOW.md § No-CI mode](WORKFLOW.md#no-ci-mode).
  A repository declines any of these files once, in a tracked
  `.github/sd-fleet.json` holding `{"exempt": ["<path>", ...]}`; the stamp
  names each exempt path and never proposes it, and a file that does not read
  refuses the repository.
  It lays the Claude Code settings baseline too (sd:1661): `permissions.deny`
  rules that stop Claude Code's file tools reading secret files, listed in
  `SECRET_READ_DENY` in `bin/sd_fleet.py`. An owned or co-owned repository
  carries them in a tracked `.claude/settings.json`. A guest repository takes
  no tracked file of ours, so its plan proposes the untracked
  `.claude/settings.local.json` instead; a guest plan carries a refusal, so a
  write lays nothing there and the operator copies the file by hand.
  The file-name rules start `Read(//**/`, anchored at the filesystem root, so
  they cover the project and secrets outside it such as `~/.netrc`; a single
  `/` would anchor at the project only (sd:2982).
  Missing rules are added. None is removed, except that a project-anchored
  rule an older stamp laid becomes its `//` replacement in place.
  A settings file that does not read refuses.
  It also adds the template's new lines to that checkout's `CLAUDE.local.md`
  block, removing none, and creates its untracked `docs/dashboard/`. `--dry-run` prints every auto repository's diff against
  its `origin/HEAD` and writes nothing. A repository is the operator's own
  when its owner is in `sd.fleet_owners`, comma-separated GitHub logins
  (unset: the deprecated `fleet.owners` list in the machine config, then
  `DEFAULT_OWNERS` in `bin/sd_fleet.py`); any other owner's protection stands.
- `build/` — HTML from `sd-research-kit render`, into the research repository you
  are standing in. Gitignored.
- `CLAUDE.md` — the research-repo standard in short form, from `sd-research-kit
  init-claude-md`, into a research repository that has none. **Tracked.** Written
  once: the verb refuses if a copy is already there, and there is no re-sync
  verb, because a repo may state parts of the template differently on purpose.
  From then on `sd-research-kit review` reports where the copy and
  `skills/sd-research-repo/templates/CLAUDE.md` disagree, and the repo's own
  `## Local overrides of the shared template` section records the disagreements
  that are deliberate.

Two `contrib/` skills add paths of their own, both tracked, once `sd skill try`
brings one back (pack sd:7368 retired both). Invoking either is the approval
to write, and neither writes anywhere else:

- `sd-plan` — a work item is a row (`sd task add`); it writes
  `docs/work/<YYYY-MM-DD>-<slug>/design.md` only for a change that moves state in steps.
  `--work-dir` selects a root other than `docs/work`; `--decision`
  writes `docs/decisions/<YYYY-MM-DD>-<slug>.md` instead of a work item.
- `sd-spec` — `docs/spec/**`, rewritten in place on the branch so the
  correction lands with the change, plus the learnings page under `--retro`.

Every other skill either edits documents that are already yours or reads only.

Everything else the pack writes lands outside the repository entirely.

That `CLAUDE.local.md` block carries a `mode:` line with one of three values,
and the workflow each selects is stated in [WORKFLOW.md](WORKFLOW.md):

- `full` — planning artifacts live in `docs/work/` in the repository, and the
  whole path runs; an unattended merge additionally needs `runner_merge: auto`
  on the repository row, set with `bin/sd-db repo runner-merge <path> auto`.
- `minimal` — no work items anywhere, and the small-change path only.
- `guest` — planning artifacts stay out of the repository, a fork's branches
  included; the loop stops at pull-request-ready, and nothing is posted upstream.

Without a `mode:` line the mode is detected, and detection only ever lowers a
line you wrote.

Agents render to Claude only. Codex agent TOML translation remains outside this installer's scope.
The Codex skill metadata adapter does not change that boundary.
The limit is a test, not
a note: `tests/test_sd_agents.py` asserts nothing lands in `~/.codex/agents`.
The pack neither renders there nor removes what it finds there; what the test
guarantees is that the installer writes nothing, not that the directory is
absent. The skills that name an agent make the delegation optional,
so a Codex session runs those passes inline rather than losing them.

Antigravity is deliberately **not** rendered. Its skill format is byte-identical
to Claude's, but which of three candidate roots `agy` actually loads is
unresolved, and rendering into the wrong one would produce surfaces that appear
installed and never load — worse than absent, because nothing would report them
missing. Zero or all, never partial, is the rule; what the test asserts today
is the zero half of it — no `sd-*` under any of the three candidate roots —
because P1 has not passed and there is no all half to check yet.

## Standing operator authorization

The pack supports standing permission across consuming repositories. Installation grants no permission for a new user.
After that operator explicitly grants permission, record it:

```sh
sd config set sd.external_reviews configured
bin/sd-db repo runner-merge <path> auto
```

Merge permission is one setting, `repo.runner_merge` on the repository row; the runner and the assistant both read it.
`sd.assistant_merge` is retired.

These values live in `~/.config/sd-ai-command-pack/config.json`; `XDG_CONFIG_HOME` overrides the configuration root.
`sd config get`, `list`, and `unset` inspect or remove settings. No personal grant ships in this repository.
`sd.gate_slots` is load control, not a grant: how many gates may run at once on the machine, across every repository (unset: a quarter of the cores).
`sd.gate_workers` is load control too: how many test workers one local run of the pack's tests starts (unset: the cores over the gate cap).
`sd.gate_load_max` and `sd.gate_settle_seconds` are load control too: the gate queue starts its head 45 s after the last start by default, and only below a load1 limit where one is set (unset: none, since macOS counts disk waits in the load average).
`sd.fleet_owners` names the GitHub logins whose repositories `sd fleet stamp` treats as the operator's own, comma-separated; it grants nothing.
`sd.hub_name` names the hub by its tailnet name; a machine with its own database serves it and runs lanes from it only while its tailnet name matches, and writes to it unless another node holds the name (unset: no fence; `bin/sd-db fence` answers). It grants nothing.
`sd.status_companions` names the repositories `sd-status` lists beside the one it runs in, as `<repo>=<other>[,<other>]` with `;` between repositories; it is a workflow database row, and unset lists none. It grants nothing.
`sd.jira_recent_days` is how many days back `sd-status` lists the Jira rows Jira updated (unset: 30); it grants nothing.
`sd.gate_cache_gb` bounds the local gate's warm Rust build folders (unset: 40 GB); past it the gate removes the least recently used free folder.
`sd.bulk_storage_root` names the folder for large uncommitted data, as `<root>/<repository>/` (unset: none); build output stays on the system disk. It grants nothing.
`sd.privacy_patterns` names the privacy-pattern file `sd-docs-lint --pr-body` checks a pull request body against, one extended regular expression per line (unset: `privacy-patterns` in `$SYSTEM_TOOLS_CONFIG`, else `~/.config/system`); with no file the check is skipped with a note. It grants nothing.
`sd.notion_private_folder` and `sd.notion_team_folder` name the Notion folders a `notion=` mirror goes to, and `sd.notion_experiments` and `sd.notion_experiment_tasks` the databases `sd-research-kit project` writes rows to; each is a workflow database row, and until one exists its old `SD_NOTION_*` variable answers. They grant nothing.
`sd-ship lane enqueue|list|retry|cancel|move|hold|release|run|watch|prune` keeps a serial prepare-and-merge queue per repository in the hub database, with its logs under `sd.lane_root` (unset: `$XDG_STATE_HOME/sd/lanes`), so a queued chain outlives the session that filled it. `sd-ship lane --help` and each verb's `--help` hold the options and refusals; [Parallel work](WORKFLOW.md#parallel-work) holds the rules.
`sd gate run -- make check` queues any command the same way; `sd gate status` shows the queue.
`sd gate run --max-wait SECONDS -- <cmd>` gives up after that long in the queue and exits 75, the command not run, naming its place in the queue, the holders and how long it waited; it does not bound the command itself.
Every waiter starts in the order it queued, a lane gate (any `sd-check` run) or not: no later lane gate overtakes a waiting `sd gate run`, and a run that holds a slot keeps it.
`sd gate status --json` lists the lane gates in `lane_gates`.
Wrap a plain `make check` in any repository that way, and drop a per-repository `lockf` from lane scripts: the pool orders gates across every repository.
A waiting gate names who holds each slot and since when.
`sd.review_slots` is load control too: how many reviews may run their reviewers at once on the machine (unset: 2).
`sd.lane_parallel` is load control too: how many lanes one hosted lane run runs at once (unset: 5, 1 to 8).
`sd.lane_train` is load control too: how many queued entries of one repository one lane run gates at once as a stack and lands as one train pull request (unset: 3, 1 to 9).
`sd.machine_cap`, `sd.repo_cap` and `sd.bundle_max` are load control too: how many builders run at once on this machine (unset: 6), how many bundles build at once in one repository (unset: 1), and how many items one build bundle takes when this machine claims it (unset: 3); the claim gives a build child 120 minutes per 3 slots it took, never less.
`sd.build_model` names the model every build bundle this machine claims runs its builder on (unset: opus, and sonnet for an all-mechanical bundle).
`sd gate post --head SHA` runs the merge gate at SHA and posts `sd/local-gate`, for a merge path that is not `sd-ship merge`.
`sd gate check` runs the same check at `HEAD` and records a pass that `sd-ship prepare` and the merge gate reuse at that head; it posts nothing.

`configured` allows private code and scoped review context to the operator's eligible configured providers, including future entries.
A local `reviewers` list restricts recipients; an explicit empty value denies review.
Machine `sd.external_reviews deny` vetoes local consent. Missing machine policy requires explicit local consent.
The installer preserves restrictions and refuses malformed answers. It cannot recover historical empty answers whose keys were erased.

`sd.copilot_review` says when `sd-ship` requests a GitHub Copilot review by itself: `deep` on deep-tier changes only, `always` on every reviewing tier, `never` on none.
Unset reads `deep`, so every repository gets Copilot on deep changes without a per-repository file.
A repository's review declaration (`sd-db repo declare PATH review`) `copilot_review.automatic_deep` overrides `deep` and `always` when it names the key; one that does not name it inherits.
A machine `never` wins over the declaration.
`sd-review --explain` reports the effective policy and its source (`repository`, `machine config`, `machine default`) under `remote_reviews.copilot`.
`sd-ship` applies the setting as it stands at dispatch to the tiers the retained passes recorded, so a change takes effect without another review.
`sd-ship prepare --copilot-review request|skip` still overrides both for one prepare.
See [the workflow policy](WORKFLOW.md#standing-authorization) for resolution and limits.

## Daily workflow

The dashboard and CLI use the same `sd_db` operations from this pack's `lib/`.
Capture a task without a checkout or planning document:

```bash
sd task add "Investigate the delayed pipeline" --priority 1
sd today
sd task status 42 in_progress
sd task note 42 "Reproduced with the staging input"
sd task status 42 done
```

A task a commit delivered closes with that commit named:
`sd task status 42 done --delivered-by <full-sha>`. The SHA is verified the
way `sd work deliver` verifies one — reachable from the checkout's default
branch and carrying a `Delivers: sd:42` trailer in the block
`git interpret-trailers` reads — and is recorded on the transition, so the
history answers "what delivered this" for a task and for a work item alike.
A fix that landed in another registered checkout adds
`--delivered-in <path>`: the commit is verified there, the transition names
that checkout, and the task keeps the checkout it was filed in.
A commit that states the trailer outside that block is refused by name rather
than reported as carrying none. An item that belongs to no checkout has no
default branch to verify a commit against, so `--delivered-by` on a
`followup` filed off every checkout is refused for
that reason and the item closes without it. A `followup` filed in a
registered checkout carries that checkout since sd:809, but only a task's
move to done records a delivering commit, so the flag is refused there too,
on that second reason, and the item closes without it just the same.
A row worked on its own branch, as the build records it,
does not close plainly while no merge of that branch is recorded (sd:1990).
Name the merge with `--delivered-by`, or say why no pull request is needed
with `--reason`, which the transition records. A merge `sd-ship` recorded,
or a row on `main` or `master`, closes as before.

A task that repeats carries a rule:
`sd task add "File the weekly report" --due 2026-01-01 --recur FREQ=WEEKLY`.
The rule is an RRULE subset (`FREQ`, `INTERVAL`, `BYMONTH`, `BYMONTHDAY`, and
`BYDAY` weekdays on `WEEKLY`) and needs a due date. `--recur-anchor schedule`, the default, dates the next
occurrence from the last due date; `completion` dates it from the day the task
was done. Completing the task creates the next row and moves the rule to it,
and `sd task status` prints `next occurrence: #N · due D`, or `recurrence
ended:` with the reason when no next occurrence exists. The next row stays
out of `sd today` until its due date; `--recur-lead DAYS` brings it back that
many days earlier. Until then `sd store items` prints `opens D` on it, and its
read rows carry `scheduled` and `opens_on`. `sd task edit` takes the same three flags, and `--clear-recur`
stops the series. `sd_db` owns every refusal: the grammar, the anchor, the
lead, the due date and the kinds that may recur.

A row another source raises carries a reference to the occurrence that raised it:
`sd task add "repo-sync failed" --kind followup --ref job:repo-sync:42`.
A second add with the same reference refuses, names the row and its status,
and leaves the row as it was, done or open; `sd task edit` changes a row, and
the next run's failure names a new reference and files a new row (sd:3703).
`sd task show` prints the reference as `ref:`, and
`sd today --json` and every `--json` row carry it as `ref`. Only a task or a
followup takes `--ref`, and not with `--recur`.

`sd store items --open` lists the backlog, and `--here` narrows it to this registered checkout; `sd store item 42 --json` includes
history and a revision that edits can require with `--if-revision`.
Each `sd store items --json` row carries `status_reason`, the reason its latest status change gave, or null.
`sd task show 42` is an alias that prints the same thing.
`sd task cancel 42 --reason TEXT` closes a task or followup nobody will do,
with the `cancelled` receipt `sd work cancel` writes. Notes,
priorities, due dates and task status save directly to the database. GitHub
issues are optional external references, with their last successful sync shown
separately from local progress.

Repository work uses `sd work relink`, `sd work cancel` and
`sd work deliver`. A planning folder names its row with `item: sd:<id>`
in its `design.md`; nothing files a `work` row any more (pack sd:7064).
Delivery verifies a full commit and its delivery trailer against the default
branch before recording completion. Cancelling work requires a reason and
completes immediately in the database. Neither operation writes a status file
or creates a bookkeeping pull request.

Parallel sessions and workers follow [Parallel work](WORKFLOW.md#parallel-work):
one writer per checkout, read-only fan-out, one merge lane, and a budget on
every worker.

From the writing checkout, `sd writing list`, `sd writing readiness --piece
YEAR/slug`, and `sd writing stage` share the dashboard's writing controls.
Import and cutover have separate preview and verification commands. `list`,
`import` and `verify` refuse a checkout with no `content/` folder.
`sd writing promote ID` turns an idea row into a registered piece in the
current writing checkout, from its piece template. `sd writing adversarial`
runs the shared adversarial gate on a draft, and `sd writing reconcile` marks
`adversarial.md` or `research.md` current after a small revision. Once the
repository uses rows, routine stage, parking and metadata changes leave content
files untouched. See the writing pack's `.claude/reference/database-workflow.md`
for review evidence and recovery commands.

`sd jobs list` and `sd assignments list` show operational state. Job retry and
cancellation require a recognized installed job and a fresh state check;
unsupported controls explain why they are unavailable. Retry also starts an idle
job, such as one with no calendar; its `label` reads "Run now" then. Cancelling a queued
assignment does not claim to terminate an independently running process.

The current dashboard and its installation instructions live in
`system/local-project-dashboard`. It binds to loopback; remote access requires
an explicitly configured private HTTPS front door and operator identity.

## Install

```bash
git clone https://github.com/platypeeps/sd-workflow
cd sd-workflow
python3 bin/sd_install.py --user
```

The checkout you install from is the serving checkout: every rendered surface is
a copy of what is in it at that moment. Keep it on a clean `main` and update
with `--pull`, which fast-forwards and re-renders in one step and refuses to run
off `main` or over uncommitted changes.

### A dedicated serving tree

Work in one checkout and serve from another (sd:3009). The serving tree is a
plain clone on a detached `HEAD` that nobody works in, at
`${XDG_DATA_HOME:-~/.local/share}/sd-ai-command-pack/serving`.

- **Update:** `make setup` in your working checkout. The tree serves that
  checkout's `HEAD` (sd:3111), so a pinned checkout pins the commands too.
  Its last step, `bin/sd_install.py --serve`, clones `origin` into the tree
  the first time. It then fetches the working checkout's `HEAD` into the tree,
  which reaches a commit `origin` lacks, detaches the tree there and runs the
  tree's `make setup`. That builds the tree's own `.venv` and renders from it.
- **A linked worktree:** `make setup` there builds the worktree's `.venv` and
  serves nothing, since its `HEAD` is unmerged work (sd:5551). It says so on
  stderr. `SERVE=yes` serves it anyway; a checkout where git cannot tell is
  treated the same way.
- **A running lane:** a lane runs `sd-ship` from the tree, so `--serve` holds
  every lane's runner lock while it moves the tree (sd:3273). When a lane is
  running, it leaves the tree where it is, says so and exits 0; run
  `make setup` again once the lane is idle. repo-sync's refresh and follow
  hold the locks themselves and set `REPO_SYNC_LANES_HELD=1`, so their
  `make setup` moves the tree.
- **Roll back:** in the tree, `git checkout --detach <commit> && make setup
  SERVE=no && .venv/bin/python bin/sd_install.py --user`. It works for any
  commit: `SERVE=no` skips the `--serve` step, which a commit from before
  sd:3009 refuses in the tree and one from before sd:1118 does not have.
  `git reflog` there lists the commits it served. `make setup` in the working
  checkout brings the tree back to that checkout's `HEAD`.
- **A failure** stops where it happened and says so. A failed fetch or
  checkout moves nothing. A failed build leaves the tree at its new commit
  with `.venv` marked mid-provision, so served commands load no `sd_db` from
  it; a failed render puts its files back. Fix the cause and run the same
  command again.
- **From the A/B slots:** a tree from before sd:3009 moves over on its first
  `make setup`. It replaces the `.venv` link with a directory, then removes
  `.venv-a`, `.venv-b` and `serving.lock`.
- **From links into the working checkout:** an install from before sd:3009
  linked the commands in `~/.local/bin` to the working checkout's `bin/`.
  The tree's render moves each link the receipt records to the tree's `bin/`
  (sd:3141). It still refuses a regular file, or a link the receipt does not
  record. `--user` in any other checkout moves no link.
- **Verify:** `--verify --json` is as strict as in any checkout. Planning
  drafts or a `HEAD` moved without a render fail the source check, which is why
  nobody works in the serving tree.

| Command | What it does |
|---|---|
| `python3 bin/sd_install.py --user` | Render skills and link commands into `~/.local/bin`; use `--bin-dir DIR` for another directory |
| `python3 bin/sd_install.py --status` | Report installed source, drift, legacy residue, and remaining predecessor agents without failing on drift |
| `python3 bin/sd_install.py --verify --json` | Read-only: fail on receipt, source, rendered-file, command-resolution, or bounded help-probe errors |
| `python3 bin/sd_install.py --pull` | Fast-forward the clean serving checkout on `main`, then render |
| `python3 bin/sd_install.py --serve` | In the serving tree, render it; elsewhere, clone the tree if it is missing, detach it at this checkout's `HEAD` and run its `make setup`; `make setup` runs this |
| `python3 bin/sd_install.py --uninstall` | Remove receipt-owned renders, hooks, and command links; preserve modified files and retargeted links |
| `python3 bin/sd_install.py --adopt-legacy` | Delete the pre-3e fleet installer's successor-less renders |
| `python3 bin/sd_install.py --repo [PATH]` | Write the marked block into `PATH/CLAUDE.local.md` |

`--verify` runs no provider, meter, database migration, or installation operation.
It resolves every executable through the current `PATH` and rejects commands from another checkout.
Relative or empty `PATH` entries fail verification before help probes run.
It runs only `sd --help`, `sd-review --help`, and `sd-ship --help`, with five-second limits.
Other commands receive interpreter and resolution checks, but remain explicitly unsmoked.
Verification requires the receipt's clean source commit; staged source changes are not an installed verification pass.

### Codex invocation metadata

Use `$sd-review`, `$sd-ship`, or another installed skill name in Codex.
Codex CLI also provides `/skills`; installed skills are not individual `/sd-*` commands.
Restart Codex if updated skills do not appear.
See [OpenAI skill documentation](https://developers.openai.com/es-419/docs/build-skills).

The installer preserves canonical Claude command markers.
For Codex skills, it translates `disable-model-invocation: true` into `policy.allow_implicit_invocation: false` in `agents/openai.yaml`.
An explicit `false` marker becomes `true`; absent markers add no policy.
The Markdown body and other source metadata remain unchanged.
Generated policy files belong to the installation receipt and retain drift protection during removal.
Failed installations restore every unchanged render, generated policies included, to its prior state; concurrent changes remain untouched.
Conflicting or unsupported invocation metadata refuses installation before rendering.
The adapter accepts plain block-mapping keys and lowercase booleans; other metadata sections remain opaque.
Invocation booleans cannot have indented continuation lines.
Claude agents and OpenCode rendering remain unchanged.

`--dry-run` prints what any of them would do and writes nothing. `--home DIR`
installs into a scratch directory instead of `$HOME`, which is how the tests
drive it. `--bin-dir DIR` links the commands somewhere other than
`~/.local/bin`, and the receipt remembers the directory, so a later `--user`
or `--pull` without the flag links there again; a link already pointing into
this checkout is kept as it is, and anything else at a link's path makes
`--user` refuse by name and write nothing.

If you commit to this checkout, `make hooks` arms the pre-commit tier: it
links `.git/hooks/pre-commit` to the tracked `hooks/pre-commit`, which runs
Ruff over the staged Python and the two whole-tree test passes
(`tests.test_code_health`, `tests.test_doc_citations`) in about five seconds
and prints its own wall time against the budget its header states.
`SD_SKIP_HOOKS=1 git commit` skips it with a notice. The target also links
`.git/hooks/commit-msg` to `hooks/commit-msg`, which refuses a `Delivers:`,
`Closes:`, `Item:` or `Needed-by:` line that git will not read as a trailer;
`SD_SKIP_HOOKS` does not skip it (sd:1931, sd:3014). The hook is one per
clone: the link sits in the clone's common `.git/hooks`, its target is the
relative `../../hooks/pre-commit`, so it reads the main checkout's tracked
file and every linked worktree shares it, whichever worktree ran `make
hooks`; the target replaces a dangling link and refuses anything else already at that path; it never
sets `core.hooksPath` and refuses to run while one is set, since git would
then place the link under that directory, and the directory is not
`.githooks/`, because `sd-status` reports both of those as the retired gate
stack's residue. This is
a setting of the clone, not a render, so `--user` does not make it and
`--uninstall` does not remove it; `bin/sd_install.py` is unchanged, and folding
the hook into `--user` is the owner's call.

### What it owns, and what it will not touch

The receipt at `~/.local/state/sd-ai-command-pack/installed.json` records every
path the installer owns: written by it, or an existing link to this checkout it
adopted. A render row carries the digest of what it wrote, and a link row the
target the link points at. That single fact is what makes the
rest safe:

- A surface you rename or retire in `skills/` — or move to `contrib/` —
  disappears from every platform on the next `--user`, because the receipt
  knows the old path was ours.
- A rendered file you have since edited by hand is **kept** and reported, never
  silently deleted.
- A stale path it could not remove stays in the receipt, so the next `--user`
  tries again.
- `--uninstall` removes those paths and nothing else. A recorded link that no
  longer points into this checkout is left and reported, a link the receipt
  never named is never touched, and the link directory stays. The global
  excludes line and any `CLAUDE.local.md` blocks are left alone; they are yours.
- If the receipt will not parse, it grants no delete authority at all — the
  installer converges forward and removes nothing it cannot account for.

## Commands

There are eight named surfaces, all commands.
All platforms preserve Markdown bodies.
Codex invocation metadata uses the [adapter](#codex-invocation-metadata).
Each surface has one `SKILL.md` file under `skills/` or `contrib/`.
That file is both the installed artifact and its documentation.

`skills/` and `contrib/` also hold the skills these commands draw on —
knowledge and procedure with no standing side-effect authority, loaded when
relevant rather than invoked. They are not listed here: `sd skill list`
reads both roots, which are the only two inventories that cannot go stale.

`sd-plan`, `sd-spec` and `sd-skill-adopt` are the named surfaces in `contrib/`.
They install with `sd skill try` instead of the default installer.
They remain commands because they authorize side effects.
Each of the eight commands sets `disable-model-invocation`.
Every other surface omits that marker.

**Runs as** identifies the execution form.
`bin/` means the pack ships an executable entry point.
**prose** means an agent follows the skill without a runner.
For prose surfaces, the skill is the implementation.
Two of the eight are prose.
Each prose skill has a "State of the tooling" section.
`tests/test_skill_frontmatter.py` checks each claim against the filesystem.

| Command | Runs as | Purpose |
|---|---|---|
| `sd-plan` | prose | Interview into a work item under `docs/work/`, review it, open its branch |
| `sd-check` | `bin/` | Deterministic runner over the repo's own entrypoints |
| `sd-review` | `bin/` | Local review on the exact diff; findings dispositioned locally, never posted |
| `sd-ship` | `bin/` | Review committed work, prepare its PR, verify merge authority, and reconcile delivery |
| `sd-spec` | prose | Update `docs/spec/**` on the PR branch |
| `sd-status` | `bin/` | Read-only: derived status, open PRs, branch-protection gaps and the states this repo accepts (its `status` declaration) |
| `sd-skill-adopt` | `bin/` | Safety pre-screen, lint, and canonical transform for an incoming skill |
| `sd-handoff` | `bin/` | Write the local session packet for this directory; `/clear` restores it |

## Maintaining

### Verify

```bash
make setup   # once; outside a linked worktree it also refreshes the serving tree
make check   # test + lint + audit + docs-lint
make precheck   # lint + the always-run test modules, about a minute
```

This repository has `repo.ci = local`: it carries no GitHub Actions workflow.
`sd-ship merge` runs `sd-check` (here `make precheck`, then `make check`) in a fresh worktree and
posts the result as the `sd/local-gate` status on the head commit. The gate
reads `sd_db` from the worktree's own `lib/`.
`sd-ship prepare` checks the public pull request body against the privacy patterns with `sd-docs-lint --body-only`.
WORKFLOW.md "No-CI mode" describes the gate.

`main` is protected by a repository ruleset named `main`, which targets the default
branch and has no bypass actors. Its rules block deletion and non-fast-forward
pushes, require a pull request with no required approvals, and require the strict
`sd/local-gate` status check.
The pack's `status` declaration accepts one gap, `reviews`: the approval count is 0
because the sole maintainer cannot approve their own pull request. `sd-ship merge`
reads the protection object before it reads the pull request's checks and
refuses a missing or weaker one (`bin/sd_ship_remote.py`, `gate()`). The
object is the classic one or, when classic answers 404, the branch's active
rulesets reduced to the same shape (`bin/sd_protection.py`); this repository
answers with the ruleset. An `unprotected`
entry in that file is the documented way to accept a branch without
protection; `WORKFLOW.md` describes how the merge gate honours it.

**Installer coverage is gated at 100% line and branch.** The gate enumerates its
subject from git rather than matching a glob, and declares a statement floor, so
neither adding an unmeasured module nor gutting the measured one can report
green. See `.github/scripts/check-installer-coverage.sh`, whose comments explain
why the floor moved from files to statements at step 3e.

```bash
PYTHON_BIN=python bash .github/scripts/check-installer-coverage.sh
```

The bash 3.2 syntax gate runs in `make lint` and in no CI job. It existed
because the pack shipped shell that ran on whatever bash a consumer's macOS
provided, which is 3.2. Nothing is shipped now; what it still protects is this
repo's own three scripts under `.github/scripts/`, which `make check` runs
through `/bin/bash` on macOS. The CI job that built bash 3.2 from source to
run the same gate on Linux was cut for that reason (sd:10, criterion 17).

### Where the work happens

This repository dogfoods its own artifacts.
The [current architecture](docs/current-architecture.md) describes the resulting design.

## License

MIT. See [LICENSE](LICENSE).
