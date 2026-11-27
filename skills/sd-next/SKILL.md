---
name: sd-next
description: 'Use when the operator asks for status, "what next", recommendations, followups or "what do you need from me" across all their work: done recently, landing soon, open items, numbered next steps, then the waiting questions one at a time; when the operator says "grill", to walk the weekly grill questions; and for the runs "triage", "park review", "health check", "open threads" or "collect followups".'
---

# sd-next

`sd-next` answers "what is done, what lands next, what is open, what do I do now, what do you need from me" across every repository.
It is the one home of the operator's status format; other skills and docs point here.
`sd-status` is a different question: where the one repository you stand in stands.

## Steps

1. Run `sd today | head -120`. It is read-only and derives every section from the shared database, the lane and the gate queue.
   For "next here" or "what next in this repo", run `sd today --here | head -120`: this repo, its companions and their open issues; the format below is unchanged.
2. Collect what only this session knows:
   - the builders and agents running now, from `ListAgents` where the harness has it, with each one's item and budget;
   - this session's open threads: recommendations not acted on, followups and questions no `sd task note` holds yet, and commits or worktrees not landed (`git status`, `git log origin/main..`, `git worktree list`).
3. Answer in the format below.
4. Walk the waiting questions, then stop.

## Format

Use the tables `sd today` prints: a heading `<Section> (<count>)`, a header row, a rule, aligned columns, at most five rows, then `… N more`.
Copy rows from `sd today`; do not rewrite them as prose. Give five sections, in this order:

1. **Done recently**: the `Delivered` table and its `+N closed by hand` line. Its When column is the local time each change landed.
2. **Landing soon**: the `Landing now` table, with the running builders added as rows.
   For what is ready, building or landing, copy the `Queues` table (pack sd:6946). Position and the time each entry started or was queued come from the table. The lane records no ETA; do not invent one.
3. **Open items and followups**: the `Attention`, `Operations, this machine`, `Decisions`, `Blocked` and `Followups` tables, with the gate line under `Operations, this machine` and the `+N review questions wait` line under `Decisions` when `sd today` prints one.
4. **This session**: one table of the threads from step 2, with the columns ID (the item, or `-`), Repo, State (`unfiled`, `filed`, `unlanded` or `running`), Title and When.
5. **Next steps**: a numbered list the operator picks from by number. Put the recommended step first and mark it. Each step names its item with its repo (`system sd:5103`, not a bare `sd:5103`; `fleet sd:5103` for an item in no repository) and one action, and a duration ("about 20 minutes") or a delivery time ("by 15:00 MDT"). Draw the steps from decisions waiting, failed lane entries, this machine's findings (`Operations, this machine`), `Planned` (soonest first), `Unscheduled` (highest priority first) and this session's threads.
   For work to pick up, offer `ready` items first, ranked by value per hour (pack sd:6000).

Write each time in the machine's local zone with its label; convert UTC first.
Keep each sentence short and plain, in simple tenses. Add no preamble and no closing summary.

## Waiting questions

After the answer, take the operator through the waiting questions, oldest first: the `Decisions` rows, then this session's unfiled questions.
Skip review questions: `sd today` leaves a review batch's questions out of `Decisions` and counts them in its `+N review questions wait` line, as the operator labels them on the dashboard's Review page.
Skip the grill's questions too: `sd today` leaves them out of `Decisions` and names them in its `Grill ready` line, as the operator answers them in a session of their own (below).

1. File an unfiled question first: `sd task note N $'<question>\nOption: <a> (Recommended)\nOption: <b>' --kind question`.
2. Ask one question per `AskUserQuestion` call. Give its context in one or two lines, and put your recommended option first, marked "(Recommended)".
3. Record each answer at once: `sd task note N "<ruling>" --kind decision --answers <note>`.
4. Stop when the operator says stop, or when no question waits.

In a run with no operator present, skip this walk; the questions wait on the dashboard.

## Grill

The weekly KISS audit files about ten seed questions on one item, filed under the ref `grill:<date>`: new mechanisms, drift, slow lanes and stale rulings (pack sd:6022).
`sd today` then prints `Grill ready: N questions on sd:X; say 'grill' in a fresh session`, and the dashboard's Today shows the same row.
When the operator says "grill":

1. Run `sd today | grep '^Grill ready'`. With no such line, say no grill waits, and stop.
2. Run `sd task show N` for each item the line names, and read its open questions, oldest first.
3. Walk them as the waiting questions above: one per `AskUserQuestion` call, the recommended option first, and each answer recorded at once with `--answers`.
4. Stop when the operator says stop, or when no question waits. A question left open stays on the item for the next grill.
5. Once every question on an item has a ruling, close it: `sd task status N done --reason 'grill answered'`.

## Operator runs

Each run below lives in its own file; read it in full when its trigger comes, and follow it in place of the steps above.
Every run decides, asks, records and reports as `references/decide-ask-record.md` says.

- `references/triage.md`: "triage" (every stalled item, then builds) and "park review" (its parked items alone).
- `references/health.md`: "health check" (this machine's health and open findings).
- `references/open-threads.md`: "open threads" or "collect followups" (this session's unfiled threads, filed and ruled).

## Never

- Never say "none" for a section `sd today` printed as `unknown`; say it is unknown.
- Never list more than five rows in a section; the rest stay in `sd today` and the dashboard.
- Never invent an item number, a PR number, a commit or a note id; copy them from the output.
- Never write anything but the question and decision notes of the walks above, and a grill item's close; an operator run records what its reference says.
