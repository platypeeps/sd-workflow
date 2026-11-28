# Triage

Run when the operator says "triage" or "park review".
Triage every stalled or suggested item in this repo and its companion repos, then queue a build for each one that ends up ready.
Budget: about 45 minutes. Follow `references/decide-ask-record.md` to decide, ask, record and report; this run's tag is `triage`.

0. Report what the previous triage's items became, as that file says.
   The press, not this run, picks what a build queues: a Retry takes a whole bundle of the item's repository, and not always the bundle of the item's last build. So name no bundle: step 1 reads the open question notes of every planning, ready, blocked or parked item of each repository, and one open question on any of them holds every press in that repository until the operator answers it. A repository whose read failed is held too. This holds for every press below, park review included.

1. Collect
   - every planning, ready or blocked item, each with its open question notes;
   - every suggested item, the dashboard Queue's Suggested rows: in `sd today --here --all`, the `Queues` rows whose Queue is `ready`, the items due today or before, `Planned` and `Unscheduled`;
   - every parked item, with its open question notes: `sd store items --parked --belongs-to <repo>` for each repo, plus blocked items whose reason says Parked;
   - every assignment in `sd assignments list` for these repos that is blocked, cancelled, or running with no recent beat;
   - every open review advisory: the weekly "Open review advisories" items and any advisory finding a review report names but no item holds yet;
   - everything that waits on the operator: open question notes, the Decisions rows in `sd today --here`, and any question this run raises.
     Count the review questions and scores in its `+N review questions wait` line for the report, and leave them to the dashboard's Review page: a review batch's questions are labelled there, not answered by this run.

2. Decide, for each one. Read why it stopped and what it waits on.
   A repository with an open question waits for step 4: queue no build in it until the operator answers,
   then decide its items here as below: an answered item that failed review or a build is still retried, never set ready.
   - Nothing it needs is missing (the item it waits on is done, the date has passed, the measurement now passes): unpark it if parked (`sd task edit N --unpark`), and set it ready (`sd task status N ready`).
     Add `--if-revision <revision>` to the item's first write, the `revision:` line of the `sd task show N` you decided from:
     a refusal means the item changed since, so read it again and decide again.
   - A suggested planning item that waits on nothing (no unfinished waits-on item, no future date, no failed review or build): set it ready (`sd task status N ready`), with `--if-revision` as above, so step 3 builds it.
     A suggested item is work the operator wants built; leave it planning only with a note naming what it still lacks.
   - It failed review or a build: fix it or split it, then retry it with `sd assignments build N`, the dashboard's Retry; do not set it ready first, which would start a fresh build.
     Past round 3, record fix, file or reject for each blocker with `sd task note N "<fix, file or reject: reason>" --kind decision`.
   - A stale assignment: retry it with `sd assignments build N` if its item is still wanted; if the item is done or superseded, cancel it with `sd assignments cancel <assignment> --if-revision <revision>` and put the reason in `sd task note N "<reason>"`.
     `<revision>` is the assignment's `revision` in the `sd assignments list --json` you decided from: a refusal means it changed since, so read it again.
     Cancel only an assignment whose run has ended or lost its beat: cancelling one member of a running bundle stops and parks the whole run.
     A member of a live bundle waits for its run to end, with a note naming that run.
   - An advisory: file it as an item for the repo's next PR, with that file's filing command and `--ref triage:<date>:<slug>`, or close it with a reason a reader can check. Never treat one as a blocker.
   - It waits on the operator: add it to the questions for step 4.
   - Otherwise: leave it as it is, with a note naming what it still waits on: an item, a date, a measurement, or the operator.

3. Build
   Run `sd assignments build N` for every ready item, including items that were ready before this run.
   Press nothing in a repository that step 0 holds: step 4 asks first, then builds there.
   Filter on no list of builds first: the press refuses an item it cannot build, such as one already building, and writes nothing.
   Report a refusal as that item's state.

4. Ask
   Ask the open question notes and Decisions rows, oldest first, as that file says; the review questions stay on the dashboard's Review page.
   When the answers leave a repository no open question, decide its waiting items again as in step 2, so a failed item is retried and never set ready, then build them as in step 3.
   Before that, read its open question notes again: the read before the asking is older than the answers, and only a question whose answer is recorded, as that file says, stops holding.
   A repository with a question still open, or filed for the dashboard with no operator present, gets no build in this run: report each of its ready items as waiting on that question.

5. Report
   End with one table: item, kind (item, parked, assignment, advisory, question), old state, park reason, new state, build, reason.
   Under it, put the veto table and the line `RESULT: filed <n>, built <n>, asked <n>`.

## Park review

"park review" runs only the parked part of the triage above, for this repo and its companion repos, with the same tag, budget and shared file.
List them with `sd store items --parked --belongs-to <repo>` for each repo, plus blocked items whose reason says Parked.
Before any press, read the open question notes of every planning, ready, blocked or parked item of each repository, as step 1 does: step 0's hold needs them, parked or not.
For each, read why it was parked and what it waits on.

- If that item is done, that date has passed, or that measurement now passes: unpark it (`sd task edit N --unpark`), set it ready (`sd task status N ready`), and run `sd assignments build N`; the press refuses an item already building, as in step 3.
  Bind the first write with `--if-revision <revision>`, as in step 2.
- If it was parked by a failed review or build: fix or split it, then retry its build with `sd assignments build N`, the dashboard's Retry.
- If it waits on the operator: ask, as that file says, and build it only after the answer, decided again as in step 2.
- A repository that step 0 holds is asked first in every case, then built in. Before that, read its open question notes again: the read before the asking is older than the answers, and only a question whose answer is recorded, as that file says, stops holding.
- Otherwise: leave it parked, with a note naming what it still waits on.

End with a table: item, park reason, new state, build, reason; then the veto table and the line `RESULT: filed <n>, built <n>, asked <n>`.
