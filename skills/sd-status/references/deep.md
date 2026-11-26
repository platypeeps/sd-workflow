# Deep analysis

Run as `/sd-status deep`: a deep analysis of the repo in the current directory.
Review only: change no code.
Budget about 2 hours. Threat model: the threat-model line in the repo's `CLAUDE.local.md`.

## 0. Scope

- Primary: the repo in the working directory, at origin/main.
- Connected repos: each one this repo calls, is called by, ships to or reads data from.
  Name the API, data format, executable, deployment step or user workflow that links them.
  Mark any you could not read.
- Previous runs: items with ref `deep-review:<repo>:*`.
  Note which of their findings shipped and which are still open.

## 1. Fan out

Fan out read-only agents, one per lens, 30 minutes each.
Each returns findings as: repo, file:line, trigger, consequence, evidence, smallest fix, label (reproduced | code-supported risk | design judgment).

a. Architecture: what the system does, who uses it, which component owns each decision and piece of state.
   Trace one representative workflow from the first user action to its result, and one failure through retry or recovery.
b. Implementation and operations: correctness, data integrity, reliability, security, performance, tests, deployment, observability.
   Look for duplicated rules, unneeded mechanisms, and steps operators repeat by hand.
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
- File the top 20 as items in their own repo: priority 2 for a reproduced defect, 3 for the rest, ref `deep-review:<repo>:<date>:<rank>` (a ref names one item, so each finding takes its own), body: file:line, trigger, consequence, smallest fix, done test.
  Leave them planning, so they show in Suggested.
- Past the top 20, also file every finding that is high priority or high value: a reproduced defect, a data-loss, security or wrong-result risk, or a fix that removes a recurring manual step.
  The cap of 20 never drops one of these.
- Put the remaining findings, up to rank 40, in the report only.

## 5. Report

Write `docs/dashboard/deep-review-<date>.md` in the primary repo.
Lead with the top 20 table: rank, repo, finding, label, fix, item.
Then: the scope table; what shipped since the last run; the workflow trace; planned-work rulings (keep, simplify, defer, drop); what works well enough to keep; what could not be verified, and what evidence would settle it.

## 6. Rule the top 20, in rank order

- Decide a routine one yourself: the options do not differ in money, risk, a standing ruling, or scope.
  Record the ruling and its reason as a decision note on its item, and act on it.
- Ask the operator only about the rest, one per question (`AskUserQuestion`): one or two lines on the finding and its evidence, then the options, recommended first: build now (set ready, queue a build), keep planned, defer to a date or item, or drop with a reason.
  Record each answer at once as a decision note, and act on it.

End with the report path, a table (rank, item, ruling, state), and under it the veto table of the rulings made without asking.
