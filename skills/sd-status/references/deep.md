# Deep analysis

Run as `/sd-status deep`: a deep analysis of the repo in the current directory.
Review only: change no code.
Budget: about 120 minutes. Threat model: the threat-model line in the repo's `CLAUDE.local.md`.
Follow `references/decide-ask-record.md` to decide, ask, record and report; this run's base tag is `deep-review` and its tag `deep-review:<repo>`.
`<repo>` is the primary repo's short name in lowercase ASCII letters, digits and hyphens.

## 0. Scope

- Primary: the repo in the working directory, at origin/main.
- Connected repos: each one this repo calls, is called by, ships to or reads data from.
  Name the API, data format, executable, deployment step or user workflow that links them.
  Mark any you could not read.
- Previous runs: report what their items became, as that file says, by the base tag.
  A `deep-review:<other repo>:<date>:<slug>` ref is another repo's run: count it in the place that file gives another host's.
  Note which of this repo's findings shipped and which are still open.

## 1. Fan out

Fan out read-only agents, one per lens, 30 minutes each.
Each returns findings as: repo, file:line, trigger, consequence, evidence, smallest fix, label (reproduced | code-supported risk | design judgment).

a. Architecture: what the system does, who uses it, which component owns each decision and piece of state.
   Trace one representative workflow from the first user action to its result, and one failure through retry or recovery.
b. Implementation and operations: correctness, data integrity, reliability, security, performance, tests, deployment, observability.
   Look for duplicated rules and steps operators repeat by hand.
   For new or unneeded mechanisms, read the newest item of `sd store items --grep 'KISS audit and grill' --json`, the weekly audit, and cite its rows for this repo instead of recounting them.
c. Planned work: open sd items, decision notes, open PRs (and issues where `sd-db repo list` shows them on).
   Check each claimed fix against shipped code.
   Group proposals by the problem they solve; mark each keep, simplify, defer or drop, with the reason.
d. User experience: walk the same workflow through the real interface.
   Can a first-time user start, see progress, recognize success, and recover from a failure?
   For a web UI, use rendered screens (dark and light, desktop and phone).
   For a CLI or API, run the documented entry point and read the real output and errors.
e. Cross-repo agreement: for each link from step 0, check that both sides agree on names, formats, versions and failure handling.

## 2. Verify

- Send each candidate top-20 finding to a verifier that defaults to refuting it.
  Keep only findings that survive with a reachable trigger.
- A design judgment needs a named cost (time, failures, lines) to rank.

## 3. Rank

- Order by user impact times how often it hits, over the cost to fix.
- Prefer delete, reuse or simplify over a new mechanism; a fix that adds a mechanism names what it replaces.

## 4. File

- Dedupe each surviving finding against open items; note the existing item instead of filing a copy.
- File the top 20 as items in their own repo with that file's filing command and `--ref deep-review:<repo>:<date>:<slug>`: `--priority` 2 for a reproduced defect, 3 for the rest.
  After the ref line, the body gives file:line, trigger, consequence, smallest fix and done test.
  Leave them planning, so they show in Suggested.
- Past the top 20, also file every finding that is high priority or high value: a reproduced defect, a data-loss, security or wrong-result risk, or a fix that removes a recurring manual step.
  The cap of 20 never drops one of these.
- Put the remaining findings, up to rank 40, in the report only.

## 5. Report

Write `docs/dashboard/deep-review-<date>-<HHMM>.md` in the primary repo, the local start time, so a same-day rerun keeps the earlier report.
Lead with the top 20 table: rank, repo, finding, label, fix, item.
Then: the scope table; what shipped since the last run; the workflow trace; planned-work rulings (keep, simplify, defer, drop); what works well enough to keep; what could not be verified, and what evidence would settle it.

## 6. Rule the top 20, in rank order

- Rule and ask as that file says. The options: build now, keep planned, defer to a date or item, or drop with a reason.
- A routine ruling never builds: it keeps the item planned, defers it or drops it. Build now is the operator's answer alone, since a build spends a builder and a lane round.
- A Retry requeues every member of the item's bundle: the assignments with the same `build_run` in `sd assignments list --json` as the item's newest one. Before each `sd assignments build N`, read that list; if any member's item has an open question note or a Decisions row, press none of them until the operator answers, and note on the item which member it waits on.
- On a build-now answer: `sd task status N ready --if-revision <revision>`, then `sd assignments build N`; an item whose build failed is only retried with `sd assignments build N`, never set ready.
  Write the status before the decision note: the note moves the item's revision.
  `<revision>` is the `revision:` line of the `sd task show N` read when you asked: a refusal means the item changed while the question waited, so read it again.
  Filter on no list of builds first: the press refuses an item it cannot build, such as one already building, and writes nothing; report that as its state, and note the refusal on the item, so a ready item with no build says why.

End with the report path, a table (rank, item, ruling, state), the veto table and the line `RESULT: filed <n>, built <n>, asked <n>`.
