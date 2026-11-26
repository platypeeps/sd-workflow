# Decide, ask, record

The one copy of the rules every operator run follows (pack sd:10800).
A run's own file names its tag, its time budget and its steps; this file names how it decides, asks, records and reports.

## Decide or ask

- A question is routine when its options do not differ in money, risk, a standing ruling or scope.
- Rule each routine question yourself and record it at once: `sd task note N "<ruling and reason>" --kind decision`.
- Ask the operator every other question. File it first if no note holds it: `sd task note N $'<question>\nOption: <a> (Recommended)\nOption: <b>' --kind question`.
- Ask one question per `AskUserQuestion` call: one or two lines of context, your recommended option first, marked "(Recommended)".
- Record each answer at once: `sd task note N "<ruling>" --kind decision --answers <note>`. A bare decision leaves the question waiting.
- With no operator present, file each question and skip the asking; it waits on the dashboard.
- End the run with a veto table of the routine rulings: item, ruling, reason. The operator vetoes a row by saying its item.

## Tag what the run files

- File each item with a ref: `--ref <tag>:<date>:<slug>`. `<date>` is the run's start date in the machine's local zone, as `YYYY-MM-DD`, kept for the whole run even past midnight.
- `<slug>` names the finding in lowercase ASCII letters, digits and hyphens, at most 40 characters: no space, colon or newline.
- A rerun files the same finding under the same ref. `sd task add` then refuses and names the item filed before: use that item; do not file it again.

## Open with the last run

Before the first step, report what the previous run's items became:

1. Run `sd store items --since 40d --json` and keep the items whose `ref` starts with the run's tag and a colon, as `health:`.
2. The previous run is the newest `<date>` among them before today's; items under today's date are this run's own, from an interrupted start.
3. Give each of its items one row: item, title, state: `done`, `open`, or `stale` (open, and `updated_at` more than 7 days ago, read as UTC).
4. With no such item, say the run has no previous run, and go on.

## Report

End with the run's own table, the veto table, then one line, the last of the reply:

`RESULT: filed <n>, built <n>, asked <n>`

`filed` counts items this run added (not a refused rerun), `built` the builds it queued, `asked` the questions it put to the operator or filed for the dashboard.
