# Research fleet review

Run when the operator says "review my research repos".
Review every research repo in one run: is the research solid, and is it moving forward?
End with suggested items that fix or advance each repo.
Read in parallel; write one repo at a time.

## 0. Scope

- Repos: the rows of `sd-db repo list` whose checkout is a research repo (it carries a `research.conf.py`).
- Read only until step 4. Start no experiment, paid model run or upload, and publish no mirror, without the operator's answer.
- Fan out one read-only agent per repo, 20 minutes each.
  Each agent returns the step-1 facts, step-2 verdicts and step-3 candidate items for its repo.

## 1. Collect, per repo

- Activity: last commit on origin/main, open branches and worktrees, and unpushed commits.
- Items: every planning, blocked, parked or ready item, its newest note, and any build or lane entry.
  Use `sd store items --parked` and `sd assignments list`.
- Questions: open question notes and research rows in the Decisions list of `sd today --here`.
  Count review questions and scores for the report only: a review batch's questions are labelled on the dashboard's Review page, not answered with `--answers`.
- Kit checks, run from a worktree: `sd-research-kit review`, `checklinks` and `pins`. Quote each one's failing lines.
- The Status section of each top document: its date, and whether it names a next step.
- Open research questions: `00-overview/next-research.md` or its equivalent.

## 2. Judge "solid", per repo

- Claims: pick the 5 load-bearing claims in the brief (`30-brief/` or the top summary).
  Check each against its cited source or data with `sd-fact-check`.
  Label each supported, partial, unverified, contradicted or outdated.
- Pins: a pinned source that moved upstream is outdated until someone rereads it.
- Consistency: a number or ruling that two documents state differently is a finding.
- A finding is real only if a reader would act on it wrongly: a wrong number, a stale ruling, a dead link in a step, or a claim with no source.
  Cosmetic issues go in one advisory line.

## 3. Judge "moving forward", per repo

- Stalled: no commit in 14 days and no in_progress item, or the Status names no next step.
- For each stalled item, decide as in the sd-next triage run (`skills/sd-next/references/triage.md`):
  - unblock it and set it ready if nothing it needs is missing;
  - fix it or split it if it failed review, with a decision note for each blocker past round 3;
  - otherwise leave a note naming what it waits on: an item, a date, a measurement, or the operator.

## 4. Suggest items, per repo (required)

- Turn every real finding and every forward gap into a candidate:
  - Fix: a contradicted, outdated or unsourced claim, a failing kit check, a moved pin, or an inconsistency.
  - Forward: the top 1-2 open research questions, a missing next step, or a stale Status, each scoped to one PR's work.
- Before filing, drop a candidate that an open item already covers. Note the existing item instead.
- File each surviving one as its own item, 1 to 5 per repo, ranked; `<n>` is its rank, since a ref names one item:

  ```bash
  sd task add --priority <2 fix of a wrong claim | 3 the rest> \
    --belongs-to <repo> --ref research-review:<repo>:<date>:<n> \
    --body "<why, with file:line or source; the done test: what a reader can check when it is finished>" \
    "<repo>: <action verb> <what>"
  ```

- Leave them planning, unclaimed, so they show in Today's Suggested list.
  Set ready and build only a fix with no open question.
- A candidate needing spend, a new experiment or a scope call goes on the item as a question note, not a build.
- A repo with no candidate gets one line in the report saying why.
  If it is also stalled, it gets a question: continue, pause or close it.

## 5. Build

- Run `sd assignments build` for each ready item with no queued or running build, including step-4 fixes. Respect the load limit.
- Note on each item touched what you did.

## 6. Ask

Walk the open question notes and Decisions rows oldest first, one per question (review questions stay on the dashboard's Review page): one or two lines of context, the recommended option first.
Record each answer at once with `sd task note N "<ruling>" --kind decision --answers <note>`.
If an answer unblocks an item, set it ready and build it.
Decide routine ones yourself; ask only where the options differ in money, risk, a standing ruling, or whether a repo continues.

## 7. Report

- Table 1, one row per repo: repo, last commit, solid (claims n/5 supported, kit checks pass/fail), moving (yes, stalled or paused), suggested items (sd:N list), action (fixed, built #N, or needs the operator).
- Table 2, the suggested items: item, repo, kind (fix or forward), priority, title, done test, state (planning, ready, built #N).
- Table 3: each existing item touched, as in the triage report.

Under them, put the veto table of decisions made without asking.
