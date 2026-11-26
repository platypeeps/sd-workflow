# Triage

Run when the operator says "triage" or "park review".
Triage every stalled item in this repo and its companion repos, then queue a build for each one that ends up ready.

1. Collect
   - every planning or blocked item;
   - every parked item: `sd store items --parked --belongs-to <repo>` for each repo, plus blocked items whose reason says Parked;
   - every assignment in `sd assignments list` for these repos that is blocked, cancelled, or running with no recent beat;
   - every open review advisory: the weekly "Open review advisories" items and any advisory finding a review report names but no item holds yet;
   - everything that waits on the operator: open question notes, the Decisions rows in `sd today --here`, and any question this run raises.
     Count the review questions and scores in its `+N review questions wait` line for the report, and leave them to the dashboard's Review page: a review batch's questions are labelled there, not answered with `--answers`.

2. Decide, for each one. Read why it stopped and what it waits on.
   An item with an open question note or a Decisions row waits for step 4: queue no build for it until the operator answers.
   - Nothing it needs is missing (the item it waits on is done, the date has passed, the measurement now passes): unpark it if parked (`sd task edit N --unpark`), and set it ready (`sd task status N ready`).
     Add `--if-revision <revision>` to the item's first write, the `revision:` line of the `sd task show N` you decided from:
     a refusal means the item changed since, so read it again and decide again.
   - It failed review or a build: fix it or split it, then retry it with `sd assignments build N`, the dashboard's Retry; do not set it ready first, which would start a fresh build.
     Past round 3, record fix, file or reject for each blocker with `sd task note N "<fix, file or reject: reason>" --kind decision`.
   - A stale assignment: retry it with `sd assignments build N` if its item is still wanted; if the item is done or superseded, cancel it with `sd assignments cancel <assignment> --if-revision <revision>` and put the reason in `sd task note N "<reason>"`.
     `<revision>` is the assignment's `revision` in the `sd assignments list --json` you decided from: a refusal means it changed since, so read it again.
     Cancel only an assignment whose run has ended or lost its beat: cancelling one member of a running bundle stops and parks the whole run.
     A member of a live bundle waits for its run to end, with a note naming that run.
   - An advisory: file it as an item for the repo's next PR, or close it with a reason a reader can check. Never treat one as a blocker.
   - It waits on the operator: add it to the questions for step 4.
   - Otherwise: leave it as it is, with a note naming what it still waits on: an item, a date, a measurement, or the operator.

3. Build
   Run `sd assignments build N` for every ready item, including items that were ready before this run.
   Skip an item with an open question note or a Decisions row: step 4 asks first, then builds it.
   Filter on no list of builds first: the press refuses an item it cannot build, such as one already building, and writes nothing.
   Report a refusal as that item's state.

4. Ask
   Walk every open question note and Decisions row, oldest first, one per question; the review questions stay on the dashboard's Review page.
   Give one or two lines of context and the recommended option first.
   Record each answer at once with `sd task note N "<ruling>" --kind decision --answers <note>`.
   If an answer unblocks an item, set it ready and build it as in step 3.
   Decide routine ones yourself and list them in a veto table; ask only where the options differ in money, risk or a standing ruling.

5. Report
   End with one table: item, kind (item, parked, assignment, advisory, question), old state, park reason, new state, build, reason.
   Under it, put the veto table of decisions made without asking.

## Park review

"park review" runs only the parked part of the triage above, for this repo and its companion repos.
List them with `sd store items --parked --belongs-to <repo>` for each repo, plus blocked items whose reason says Parked.
For each, read why it was parked and what it waits on.

- If that item is done, that date has passed, or that measurement now passes: unpark it (`sd task edit N --unpark`), set it ready (`sd task status N ready`), and run `sd assignments build N`; the press refuses an item already building, as in step 3.
  Bind the first write with `--if-revision <revision>`, as in step 2.
- If it was parked by a failed review or build: fix or split it, then retry its build with `sd assignments build N`, the dashboard's Retry.
- If it waits on the operator: ask, one question at a time, and build it only after the answer.
- An item with an open question note or a Decisions row is asked first in every case, then built.
- Otherwise: leave it parked, with a note naming what it still waits on.

End with a table: item, park reason, new state, build, reason.
