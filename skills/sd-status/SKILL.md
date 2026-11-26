---
name: sd-status
description: Read-only derived status for the repository you are standing in — is anything wrong, what is pending, what is next — with branch-protection enforcement gaps named.
disable-model-invocation: true
---

# sd-status

`bin/sd-status` answers "where does this repository actually stand" without
writing anything — not the repo, not the state directory, not a cache. Every
section derives its answer at run time from the filesystem, git, GitHub, or the shared database.

**There is no repo-path argument and no fleet walk (R10-D6).** The repository
is the one enclosing cwd, full stop. The old fleet-walking sd-status is
dropped; the dashboard provides the cross-repo view of repositories. Two
sections are not scoped to the checkout. The `jira` section: a Jira ticket
belongs to no repository, so it is read from the shared database whole. And
`Operations, this machine`: a finding about the machine belongs to no
repository, so every repository shows it.

The report leads with the judgement. `Abnormalities`, `Pending` and `Next`
answer "is anything wrong", "what is waiting" and "what do I do now" before a
single inventory section appears, because those are the questions a reader
came with; the eight older sections keep their order underneath them so
nobody's muscle memory breaks.

When the deliverable is a finished document, apply
`references/publication-contract.md`: it publishes to the Obsidian
vault and the dashboard's Documents tab by default, and reaches an
outward destination — Notion, Google Drive — only where the user designated that
document for it.

## The seventeen sections, in output order

Above them all sits the banner — two lines, `sd-status: <repo path>` and the
`pack:` line naming the checkout these tools came from with its branch and
head; a detached checkout prints `pinned`, its intended state. It is a header rather than a section: it names where the report ran and
what ran it, and nothing under it is a finding. Count it and the report
opens with eighteen top-level lines; the seventeen below are the sections.

| Section | What it shows |
|---|---|
| `Abnormalities` | every abnormal class in the ranking table, one row each, whether or not it fired: `clear` with 0, `findings` with its count, or `unchecked` with `-` and its reason on a line under the table. A second table lists at most three findings per class, and a line names how many each class held back |
| `Pending` | at most ten actionable rows by rank, with the line under them stating the denominator — `10 of 123, by rank`. A class with a cap shows at most that many rows, and a line under the list says how many of its rows the list does not show |
| `Next` | one row: the top-ranked id, its check and its `suggest`. Not a menu, not three options |
| `Work across the shared database` | a Repo row for this repository and one for each companion the `sd.status_companions` setting row lists for it, then `sd today`'s Landing now, Delivered, Planned, Blocked, Unscheduled and Work tables for them, in `sd today`'s layout, indented two spaces. A database that does not read is a `!` reason, never empty tables |
| `Operations, this machine` | `sd today`'s section of that name, the same in every repository and uncapped: one row per line of an open attention report's current findings, every repository's, that names this machine as `<job> on <host>: <finding>`, `<host>` being the lower-cased `hostname -s`: item, job (the report's external id before its `:`) and finding. A line that only heads the next ones is dropped. A report whose fields do not parse is a row saying it may name this machine. Under the table, `sd today`'s gate line: slots holding and waiting from `sd gate status --json`'s source, then the load against `sd.gate_load_max`. A database or gate read that fails is a reason, never 0 |
| `Open threads` | a row per `source` with its count, then the exclusions named in full, so the counts are never read as a total of everything that exists |
| `Work items` | a row per live item with its status from the item's row; findings and the counts line under the table, archived ones counted and not listed |
| `Contributions, this repo, shared database order` | a row per contribution: state (its lane), reference, item, when it merged or closed in local time, title. Each lane shows its first five rows, then `… N more <lane>`; a line points at the dashboard's Contributions page for every row. Every other field is `sd task contribution show KEY --json`'s and `--json`'s |
| `Open pull requests` | open pull requests, via the same code path as `sd-pr-state` |
| `Merge lane, this repo, shared database` | each item's newest lane entry that has not merged or been cancelled, with its state; `sd-ship lane list` is the full view |
| `Detected setup` | a Field and Value table: mode, lowering reason, shared-tree paths to move, and detected check entrypoints |
| `Issues, this repo, shared database` | open GitHub issues for this repository from the shared database, one row each with the needs-you rows first, as context; no row in `pending` comes from them |
| `Jira, shared database, all repositories` | the operator's Jira involvement from the shared database, across every repository: one row per ticket assigned to or filed by the operator that Jira updated in the last `sd.jira_recent_days` days (unset reads 30), open or closed, open rows first: key, state, that reason and title; at most `JIRA_ROWS` rows, then `… N more`, then one line counting the older or undated rows it hides. `--json` keeps every row, a watched one too, with its `reason` and `updated_at`. Not scoped to the checkout |
| `Protection` | branch-protection **enforcement**, a row per gap, acceptance, merge-settings flag and fleet-baseline flag |
| `Resumable handoffs` | the pending local packet for this directory as a row (**read, never consumed**) |
| `Backends` | the review lanes the provider registry declares, plus `copilot`, which no entry names — names and states only, enumerated at runtime |
| `Legacy residue` | a row per legacy leftover, each with the exact command that removes it on a line under the table |

Every section prints as `sd today` prints one (sd:5098, sd:5357): a heading `<Section> (<count>)`, a header row, a dash rule, aligned columns, the title cut to the terminal width.
The count is the rows the section holds; a section whose read failed counts `unknown`, never 0.
An empty section is its heading alone; plain lines (notes, cap lines, reasons, `until` lines) follow the table.
A finding, reason, path or suggestion the table cuts prints whole on a line under it, as `<first cell> <column>: <text>`.
The `Merge lane` table carries a Repo column, and a failed lane row's title names its item as `<repo> sd:<n>`; relay an item the same way (sd:5124).

The contribution section filters the shared projection by the current checkout path and its GitHub repository name.
Its order is newly unblocked, awaiting you, awaiting them, merged, then closed.
`closed` is terminal and holds any row whose last observation reads closed: an upstream issue, or a pull request closed without merging; an unacknowledged event keeps either in awaiting you until it is acknowledged.
It also holds a row with no item that the operator dismissed, until a lifecycle change.
The dashboard uses the same projection and order across repositories.
A row is a pull request, an upstream issue, or local work that is neither filed yet.
An issue row carries its issue URL; an issue you have written up but not filed carries its target repository and draft instead, and filing it keeps the item ID.
Read every field, event IDs and their revision included, with `sd task contribution show KEY --json`.
Use `sd task contribution ack KEY --event EVENT --if-revision REVISION` to acknowledge an event explicitly.
Use `sd task contribution dismiss KEY --if-revision REVISION` to close a row with no item that will not settle; only the operator dismisses.
Acknowledgement records attention separately from notification delivery and local task completion.

**A class that could not be asked prints `unchecked` and is not counted
clear.** The summary line is assembled so the word `clear` cannot appear while
any class is blind, because a banner that says clear over a class it never read
asserts health it did not establish.

## One table enumerates the checks

`CLASSES` in `bin/sd-status` is the single answer to "which checks exist". The
renderer iterates it, the ranking reads `rank` off it, the banner reads
`abnormal` off it, and `open threads` reads `source` off it. **Adding a check
means adding a row there and a producer — never editing a renderer, a sort, or
a list.** That is how those drift apart.

Rows are ordered `(rank, -age_days, id)`: the class first, the oldest of a
class next, the id last so the order is total and reproducible. A reader can
predict `pending` from this table before running the command.

| Rank | Check | Id prefix | In the banner | Source | What it means |
|---|---|---|---|---|---|
| 10 | `branch-already-merged` | `w` | yes | `work item + git` | an open item with delivery evidence in the default branch |
| 10 | `in-progress-without-branch` | `w` | yes | `work item` | status: in_progress with no branch: field to work on |
| 10 | `branch-unresolvable` | `w` | yes | `work item + git` | a non-done item's branch: field naming a ref no local or remote head carries |
| 10 | `status-unreadable` | `w` | yes | `work item` | an item whose authoritative source will not yield a status |
| 20 | `unresolved-concern` | `c` | yes | `## Review ledger` | a review concern left open by its own ledger row |
| 20 | `unreadable-concern-row` | `c` | yes | `ledger row nothing can classify` | a C- row whose disposition this reader does not recognise |
| 25 | `lane-entry-failed` | `l` | yes | `merge lane queue` | an item whose newest lane entry failed or was skipped short of a merge |
| 30 | `pr-check-failing` | `p` | yes | `open pull requests` | an open pull request with a failing check |
| 30 | `pr-check-missing` | `p` | yes | `open PRs + protection` | an open pull request reporting no check the branch requires |
| 30 | `dirty-tree-with-open-pr` | `p` | yes | `git + open PRs` | uncommitted work on a branch that already has a pull request |
| 40 | `protection-gap` | `g` | yes | `protection` | an enforcement leg missing on the default branch |
| 45 | `accepted-gap-standing` | `g` | no | `repo row status declaration accepted_gaps[]` | a written acceptance whose until condition nothing re-reads |
| 60 | `pr-needs-action` | `p` | no | `open pull requests` | an open pull request waiting on a review or a merge |
| 75 | `mirror-sync-pending` | `n` | no | `~/.claude/pending-mirror-syncs/` | a published document whose designated mirror is not written yet |
| 80 | `unmerged-branch` | `b` | no | `origin heads` | a branch on origin with no open pull request carrying it |
| 90 | `parked-concern` | `c` | no | `## Review ledger` | a concern parked behind a trigger nobody is watching |
| 100 | `idle-planning` | `w` | yes | `item activity + idle threshold` | an item idle in planning past the 45-day threshold |
| 100 | `undated-planning` | `w` | yes | `work item` | a planning item with no date to age it by |
| 120 | `source-marker` | `t` | no | `marker scan over the index` | a marker left in tracked source |
| 125 | `undisclosed-tool` | `k` | no | `skill claims bin/<tool>` | a skill disclosing a tool that is not built |

The `idle-planning` row is rule R10-D1, a registry row. `bin/sd-status` cites
R10-D1 in the comments beside the `CLASSES` row and the reason text, never
inside either string, so no string in the tool is a second copy of the
registry.

## Ids: `<letter><4 hex digits, 8 on collision>`, from the data alone

Every actionable row carries an id like `w0a35` — short enough to type, and a
pure function of the data, so "do `w0a35`" in a later session still names the
same row. Nothing reads a clock, a sequence number or a ledger, and **no id
ledger is written anywhere**; the same data yields the same ids every run.

**The hash is over `(check name, natural key)`, never over the class letter.**
Two checks in one class routinely fire on one object — a pull request that is
both `pr-check-failing` and `dirty-tree-with-open-pr`, an item that is both
`branch-already-merged` and `idle-planning`. Keyed on the letter, each such
pair hashes identically at four digits *and* at eight, so widening could never
separate them and one id would name two actions. The letter is a display
prefix; identity is `(check, key)`.

**Colliding ids widen to eight hex digits, both of them, and record that they
did.** The widened rows carry `"widened": true` in `--json`; the text report
shows the wider id and names the widened ids in a line under the rows, so
the change is visible rather than silent. So a consumer matching ids wants
`^[a-z][0-9a-f]{4,}$` — `{4}` is wrong, and this checkout already
carries two eight-digit ids that a fixed-width pattern misses.

## Picking a row through `AskUserQuestion`

The harness component allows **at most four options per question and at most
four questions**. Ten pending rows do not fit, and spreading them over three
questions abuses a control meant for distinct dimensions — the user would
answer three questions to express one choice.

**The resolution: ids are the primary interface and the component is a
shortcut.** Render exactly **one** question, `multiSelect: true`, whose four
options are pending rows 1 to 4, each labelled by its id. Rows 5 to 10 are
addressable by typing the id, which the component's own free-text answer
already supports, and `--actions` lists every row, the ones `pending` leaves
out included. Take the options from the text report's `pending`, not from
`actions` in `--json`: `pending` is the first ten of it.

Four rules on that question:

- **Never renumber the options.** The label is the id the report printed.
- **Never substitute your own ordering.** The order is `pending`'s, which is
  the ranking table's, which the user can predict.
- **Never invent an option that is not an inventory row.** An option the report
  did not produce has no id, and nothing acts on it.
- **Never offer a waiting question.** `sd-status` walks no questions: `sd-next`
  walks the Decisions rows, and review questions wait on the dashboard's
  Review page.

## The protection section is the one that matters

The doctrine is that merge authority is GitHub branch protection *wherever
protection is actually enforcing*. Protection is read from both of GitHub's
mechanisms: the classic object, and when that answers 404, the branch's active
rulesets -- a `pull_request` or `required_status_checks` rule there is
protection and gets the same gap analysis; `deletion` alone is not, and the
`unprotected` finding then names the rules it saw.
Rules that could not be read leave protection unknown, with the read error as the reason (sd:1000).
Protection that exempts admins is prose, not
authority: it stops collaborators and leaves the one account that does the
merging entirely ungated. So this section reports enforcement state, and each
missing leg prints as a named gap:

- `enforce_admins` off — every rule below it stops at the admin who merges;
  on a ruleset, an `OrganizationAdmin` bypass, named per ruleset with the
  rules that ruleset carries, then the rulesets still binding administrators
  and the ones not known either way — GitHub layers rulesets, and a bypass
  on the review ruleset reaches nothing the checks ruleset requires. The
  same id says unknown, and never enforced, for a bypass list GitHub
  withheld and for a repository-role bypass, whose numeric id nothing here
  resolves to admin or not
- `bypass` — a ruleset an app, a team, a deploy key or a user can walk past,
  named with the rules it carries; that actor is not subject to them, and
  the rulesets administrators stay subject to are named
- no required status checks — a red PR still merges
- `strict` off — a green check run against a base that moved still merges
- `required_not_produced` — required contexts no workflow here produces; each
  blocks every PR until an external app reports it
- `produced_not_required` — checks this repo runs but does not require; they
  can go red without blocking a merge
- `reviews` — no PR review required, or zero required approvals
- the two r7 merge-settings flags: squash title/message source (a `wip:`
  subject reaching main) and whether rebase-merge is allowed
- the two fleet-baseline flags, for a repository whose owner is in
  `sd.fleet_owners`, else `platypeeps` as on the dashboard (sd:1807): `protection_source`, raised unless classic
  protection alone protects the branch, with no active ruleset beside it or a rules read that failed (sd:5406),
  and `required_check`, raised unless `ci` is required
  (`sd/local-gate` under `repo.ci = local`). The dashboard shows the same ids.
  A classic read that failed carries neither, as the system files it unknown.

**A finding is a report, not a failure.** The command exits 0 whether or not it
found gaps or abnormalities. Exit 2 is reserved for an invocation or
configuration fault, printed as a sentence, never a traceback.

## Accepted states are not silenced states

A repository can record, in its repo row's `status` declaration
(`sd-db repo declare PATH status`), a protection state it has looked at and decided to keep. Those findings print as
an `accepted` row, `<date>: <reason>`, with `<id> until: <condition>` on a line
under the table, and move from `protection.gaps` to `protection.accepted` in `--json` —
so a consumer counting gaps counts open ones only, and never mistakes an
accepted finding for an absent one.

What makes it an acknowledgement rather than a suppression:

- **Keyed on the observed protection state, never on the gap id.** `reviews`
  is emitted for two opposite states — the review object being absent, so no
  pull request is required at all, and the object existing while asking for
  zero approvals. An entry pins the facts it was accepted under, and stops
  applying the moment any of them changes; the gap then prints as a gap,
  carrying a line saying an acknowledgement exists and no longer matches.
- **`because` and `until` are both required, and both print every run.**
  `until` is the deletion criterion: when it comes true the entry is deleted
  and the gap returns on its own.
- **A malformed declaration accepts nothing and says so** — each fault prints as its
  own gap. It fails closed and loudly, never silently.

`sd-ship merge` reads two kinds of acceptance itself, from the row at merge time:
`bypass`, for a bypass by `DeployKey` actors only, and `strict`. Each must pin
its own fact, and every fact it pins must equal the live state. No other
actor type and no other gap can be accepted that way.

An acceptance nobody re-reads is itself a row: `accepted-gap-standing` at rank
45 surfaces a written acceptance whose `until` condition nothing is watching.
An entry whose `until` is `never` has no condition to watch, so it lists no
row; Protection still prints it as accepted.

## Flags

`--json` (one machine-readable object) · `--actions` (every actionable row,
uncapped, one per line, count first) · `--limit N` (most PRs to list).

**`--json` wins when both `--json` and `--actions` are given.** `pending` caps
at ten because a report is read whole; `--actions` is the list a caller pipes,
so capping it would make the cap the interface.

The `--json` schema is version **3**. Beyond the section keys it carries
`inventory` (`rows` plus the `unchecked` map),
`abnormalities`, `actions` — the uncapped inventory, of which `pending` is the
first ten — and `next`. It
has no top-level `pending` key, and the two nested ones are something else
again: `handoff.packet.pending` is a boolean about the handoff packet, and
`pull_requests.pull_requests[].checks.pending` an integer count of that pull
request's checks that have not finished. `_BUCKETS` in `bin/sd-pr-state` folds
six states into it: `PENDING`, `QUEUED`, `IN_PROGRESS`, `WAITING`, `REQUESTED`
and `EXPECTED`. `QUEUED`, `IN_PROGRESS`, `WAITING` and `REQUESTED` are a check
run's `status`, which `_outcome` reads only while the run has no `conclusion`;
`EXPECTED` is a commit status GitHub expects and has not received; `PENDING`
is either. `rollup_buckets` writes only the buckets it saw, so with no such
check the key is absent, not 0. Neither is this list.
**`next` is an object with `id`, `check` and `suggest`, not a bare string**,
because a caller acting on the suggestion needs the id it belongs to in the
same breath.

## Never

- **Never present the absence of gaps you did not read as safety.** If the
  protection section could not be fetched, say the enforcement state is
  unknown; do not say the repo is protected.
- **Never call a class clear when it printed `unchecked`.** A class that could
  not be asked was not looked at, and the two words are not interchangeable.
- **Never report an accepted finding as an absent one.** "No open gaps" is a
  different sentence from "protection is fully enforcing", and only the second
  is a claim about the branch. Read `protection.accepted` before saying either.
- **Never claim a guarantee the config does not provide.** In a repo with no
  protection, the honest statement is that nothing enforces merge authority
  there.
- **Never read `pending` as the whole list.** It is capped at ten and says so;
  the total is in its heading and on the line under the rows, and `--actions`
  and `--json` carry the rest.
- **Never invent an id, and never renumber one.** Ids come from the run you are
  looking at; an id you composed names nothing.
- **Never consume the handoff packet from here.** The `resumable handoffs`
  section reads it; only `sd-handoff-restore` or `sd-handoff --show` consumes
  it. Reading status must never eat a pending packet.
- **Never write, anywhere.** No cache, no index row, no state file, no id
  ledger. If you want a fact recorded, that is a different command.
- **Never point it at another repository**, and never loop it over checkouts to
  rebuild the fleet view that was deliberately removed.
