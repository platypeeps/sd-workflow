# Research fleet review

Run when the operator says "review my research repos".
Review every research repo in one run: is the research solid, and is it moving forward?
End with suggested items that fix or advance each repo.
Read in parallel; write one repo at a time.
Budget: about 90 minutes. Follow `references/decide-ask-record.md` to decide, ask, record and report; this run's tag is `research-review`.

## 0. Scope

- Repos: the rows of `sd-db repo list` whose checkout is a research repo (it carries a `research.conf.py`).
  `<name>` below is a repo's short name, as `sd today` prints it; `<repo>` is its checkout path.
- Report what the previous run's items became, as that file says.
- Read only until every agent reports. Start no experiment, paid model run or upload, and publish no mirror, without the operator's answer.
- Fan out one read-only agent per repo, 20 minutes each.
  Each agent returns the step-1 facts, step-2 verdicts, step-3 proposed rulings and step-4 candidate items for its repo.
  The agents write nothing; the lead makes every write from step 3 on, after all agents report.
- Every write in this run, and every question it asks, is on an item of a reviewed repo; leave any other repo's item untouched.
- The press, not this run, picks what a build queues: a Retry takes a whole bundle of the item's repository, and not always the bundle of the item's last build. So name no bundle: step 1 reads the open question notes of every planning, ready, blocked or parked item of each repository, and one open question on any of them holds every press in that repository until the operator answers it. A repository whose read failed is held too. This holds for every press below.

## 1. Collect, per repo

- Activity: last commit on origin/main, open branches and worktrees, and unpushed commits.
- Items: every planning, ready, in_progress, blocked or parked item, its newest note, its open question notes, and any build or lane entry.
  Use `sd store items --parked --belongs-to <repo>` and `sd assignments list`.
- Questions: open question notes on the repo's items, and the Decisions rows of `sd today --only <name>`, which holds that repo's rows alone.
  Count review questions and scores for the report only: a review batch's questions are labelled on the dashboard's Review page, not answered by this run.
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
- For each stalled item, decide:
  - unblock it and set it ready (`sd task status N ready --if-revision <revision>`, the `revision:` line of the `sd task show N` you decided from) if nothing it needs is missing, its build never failed and its repository has no open question (step 0 holds it);
    a refusal means the item changed since: read it again;
  - fix it or split it if it failed review or a build, then retry it with `sd assignments build N`, never set ready, with a decision note for each blocker past round 3;
  - otherwise leave a note naming what it waits on: an item, a date, a measurement, or the operator.

## 4. Suggest items, per repo (required)

- Turn every real finding and every forward gap into a candidate:
  - Fix: a contradicted, outdated or unsourced claim, a failing kit check, a moved pin, or an inconsistency.
  - Forward: the top 1-2 open research questions, a missing next step, or a stale Status, each scoped to one PR's work.
- Before filing, drop a candidate that an open item already covers. Note the existing item instead.
- File each surviving one as its own item, 1 to 5 per repo, ranked, with that file's filing command:
  `--priority` 2 for a fix of a wrong claim and 3 for the rest, `--belongs-to <repo>`, `--ref research-review:<date>:<slug>`, and the title `<name>: <action verb> <what>`.
  `<slug>` starts with the repo's short name, as `<name>-<finding>`, so two repos' findings never share a ref.
  After the ref line, the body says why, with file:line or source, and the done test: what a reader can check when it is finished.
- Leave them planning, unclaimed, so they show in Today's Suggested list.
  Set ready and build only a fix, and only in a repository that step 0 does not hold.
- A candidate needing spend, a new experiment or a scope call goes on the item as a question note, not a build.
- A repo with no candidate gets one line in the report saying why.
  If it is also stalled, it gets a question: continue, pause or close it.

## 5. Build

- Run `sd assignments build N` for each item this run set ready in step 3 or 4, in the reviewed repos only. Respect the load limit.
  Press nothing in a repository that step 0 holds: step 6 asks first, then builds there.
  Filter on no list of builds first: the press refuses an item it cannot build, such as one already building, and writes nothing.
- Note on each item touched what you did.

## 6. Ask

Ask the questions step 1 collected and this run raised, on items of the reviewed repos only, oldest first, as that file says.
Review questions stay on the dashboard's Review page.
When the answers leave a repository no open question, decide its waiting items again as in step 3, so a failed item is retried and never set ready, then build them as in step 5.
Before that, read its open question notes again: the read before the asking is older than the answers, and only a question whose answer is recorded, as that file says, stops holding.
A repository with a question still open, or filed for the dashboard with no operator present, gets no build in this run.
Whether a repo continues is never routine: ask it.

## 7. Report

- Table 1, one row per repo: repo, last commit, solid (claims n/5 supported, kit checks pass/fail), moving (yes, stalled or paused), suggested items (sd:N list), action (fixed, built #N, or needs the operator).
- Table 2, the suggested items: item, repo, kind (fix or forward), priority, title, done test, state (planning, ready, built #N).
- Table 3: each existing item touched: item, old state, new state, build, reason.

End with the veto table and the line `RESULT: filed <n>, built <n>, asked <n>`.
