## Say this to a session

- "what next" or "what do you need from me": the status in five tables, then the waiting questions one at a time. Reports only; it records your answers.
- "next here": the same, for the repository the session stands in.
- "grill": the weekly grill questions, one at a time, when Today shows a Grill ready row.
- `/sd-next <phrase>` runs any phrase in the table above as the skill, by name.
- `! <cmd>` runs a command yourself, in the session's shell; the session sees its output.

## Status of the fixes

- A session reports each item by its state. See it yourself on [Today](/today) (the Queue's lane rows and builds), [Activity](/activity) and [Decide](/decide).
- The states, in order: queued, building, reviewing, fixing round N, blocked (round cap: the lead rules each blocker), lane pending, running, then merged or failed.
- A lane item takes about 25 to 35 minutes. It takes longer while the load is above the limit below.

## Why an item went back

- The lane's catch-up merge of main voids a ruling made on the old head: the lead rules again.
- A merged change to the review tool, or a change to CLAUDE.local.md, voids a review: the item is reviewed again.
- A semantic conflict with main returns the item to its builder.

## What blocks a review, what is advisory

- This repository's threat model, read now from CLAUDE.local.md: {live:threat_model}
- Data loss or a leaked secret always blocks.
- Past round 3 the lead rules each blocker FIX, REJECT or FILE, with a decision note.
- An advisory gets a one-line note, a filed item (`--ref advisory:sd:N`), or the Sunday advisory sweep. The Queue on [Today](/today) has a Show advisories toggle.

## Load and capacity

- Builder cap (`sd.machine_cap`, `sd.repo_cap`): {live:machine_cap}.
- No new builder starts above this load: {live:load_cap}. The load now: {live:load5}.
- Gate slots (`sd gate status`): {live:gate_slots}.
- The usual heavy hitters: gates, builds, and the Docker VM (an end-to-end test database). See [Health](/fleet-health).

## Hub and satellites

- A hub refresh publishes the hub-pin tag. The pin now: {live:hub_pin}.
- Each satellite runs repo-sync-follow every 5 minutes and moves to the pin.
- machine-setup runs nightly at 03:15 local time. It rewrites the prompt files and the CLAUDE.local.md keys. A refresh request runs it now.
- To check: `agent-prompt.sh status`, and each satellite's last follow run on [Reports](/reports) (a failed follow files a report).

## Pauses and exemptions

- The system and pack build pause: {item:10848}. The pause ends when that item is done.
- Say "exempt N" to let one item build during the pause.
- Say "queue after the batch" to make an item wait on the open batch (blocked-by).

## Where answers live

- `sd task show N`: every note on an item, its rulings and questions too. On the dashboard: [Tasks](/tasks).
- [Decide](/decide): the questions that wait on you.
- [Reports](/reports): the job reports and alerts.
