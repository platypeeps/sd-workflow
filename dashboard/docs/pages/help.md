# Help

Help is at `/help` (sd:10917), the last entry of the rail's Fleet group (`g f`).
It answers two questions: what to say to a session, and how the fleet works.
`GET /api/help` (`help_screen.document`) reads files and settings and writes
nothing.

The phrase table comes from the "Operator runs" list in
`skills/sd-next/SKILL.md`. Each row is one quoted phrase, its description in
parentheses, the `Budget:` line of the reference file the list names, and
whether the run queues work. A new run in that list shows with no page edit.

The rest of the page is `sd_dashboard/v2/help.md`: `## ` sections of
paragraphs and `- ` bullets, with `code` and links to dashboard views (a path
that starts with `/`). Two tokens put live values in the text:

- `{live:<name>}` reads one of `help_screen.LIVE` now: `machine_cap`,
  `load_cap`, `load5`, `gate_slots`, `threat_model` (the `threat-model` key of
  the pack checkout's `CLAUDE.local.md`) and `hub_pin` (the system
  checkout's `hub-pin` tag).
- `{item:<n>}` reads that item's status and title, such as the pause item.

A value that does not read shows "unknown" with the reason, and the head
names it. An unknown token, a link off the server, or text before the first
section is a problem the page names; the test suite fails on each.

Gap from the item: each satellite's last follow run is not read; the page
links Reports, where a failed follow files a report.
