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

- A run's file names its base tag, as `health`. A run about one machine adds `<host>`, the short host name in lowercase (`hostname -s`), so its tag is `health:<host>`: two machines' runs then never share a ref. Any other run's tag is its base tag.
- File each item with its ref, and the same ref as the body's first line:
  `sd task add "<title>" --priority <1-4> --belongs-to <path> --ref <tag>:<date>:<slug> --body 'ref: <tag>:<date>:<slug>'`.
  Put any further body text after that line, and keep the line in every later body edit: it is how a done item is found after a week.
- `<date>` is the run's start date in the machine's local zone, as `YYYY-MM-DD`, kept for the whole run even past midnight.
- `<slug>` names the finding in lowercase ASCII letters, digits and hyphens, at most 40 characters: no space, colon or newline.
- A rerun files the same finding under the same ref. The add then refuses and names the item filed before: use that item, and do not file it again.

## Open with the last run

Before the first step, report what the previous run's items became:

1. Run all four reads with the run's base tag and merge their rows by item id:
   `sd store items --json`, `sd store items --parked --json`, `sd store items --grep '<base>:' --json` and `sd store items --parked --grep '<base>:' --json`.
   The first two list every open item, whatever its body holds; the two `--grep` reads add the done items of any age that carry the ref line.
2. Keep every item whose `ref` field starts with the base tag and a colon, exactly. Read the ref, never the matched text. Place each kept item; drop none:
   - `<tag>:<date>:<slug>`: this run's own history.
   - `<base>:<date>:<slug>` in a run about one machine: filed before the host rule. Count it as this machine's, and mark its row `host unknown`.
   - `<base>:<other host>:<date>:<slug>`: another host's. Leave it out of the rows, and say how many there are.
   - any other shape: one row marked `unreadable ref`, with the ref as written.
3. The previous run is the newest `<date>` before today's among the first two kinds; items under today's date are this run's own, from an interrupted start.
4. Give each of its items one row: item, title, state: `done`, `open`, or `stale` (open, and `updated_at` more than 7 days ago, read as UTC).
5. A read that fails is reported as failed, not as no previous run. When all four succeed and step 3 finds no earlier date, say: no tagged item found. Name the limit with it: a done item older than a week that lacks the ref line is in none of these reads.

## Report

End with the run's own table, the veto table, then one line, the last of the reply:

`RESULT: filed <n>, built <n>, asked <n>`

`filed` counts items this run added (not a refused rerun), `built` the builds it queued, `asked` the questions it put to the operator or filed for the dashboard.
