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

- A run's file names its tag. A run about one machine adds `<host>`, the short host name in lowercase (`hostname -s`), as `health:<host>`: two machines' runs then never share a ref.
- File each item with a ref, and the same ref as the body's first line: `--ref <tag>:<date>:<slug> --body 'ref: <tag>:<date>:<slug>'`, with any further body text after it. The lookup below finds the item by that line.
- `<date>` is the run's start date in the machine's local zone, as `YYYY-MM-DD`, kept for the whole run even past midnight.
- `<slug>` names the finding in lowercase ASCII letters, digits and hyphens, at most 40 characters: no space, colon or newline.
- A rerun files the same finding under the same ref. `sd task add` then refuses and names the item filed before: use that item; do not file it again.

## Open with the last run

Before the first step, report what the previous run's items became:

1. Run `sd store items --grep '<tag>:' --json` and `sd store items --parked --grep '<tag>:' --json`, with the run's tag. Both read done items of any age; the second adds the parked items a plain list hides.
2. Keep the items whose `ref` field starts with the tag and a colon, exactly; `--grep` matches text anywhere, so drop every other hit.
3. The previous run is the newest `<date>` among them before today's; items under today's date are this run's own, from an interrupted start.
4. Give each of its items one row: item, title, state: `done`, `open`, or `stale` (open, and `updated_at` more than 7 days ago, read as UTC).
5. Say the run has no previous run only when both reads succeed and step 2 keeps no item. A read that fails is reported as failed, not as no previous run.

## Report

End with the run's own table, the veto table, then one line, the last of the reply:

`RESULT: filed <n>, built <n>, asked <n>`

`filed` counts items this run added (not a refused rerun), `built` the builds it queued, `asked` the questions it put to the operator or filed for the dashboard.
