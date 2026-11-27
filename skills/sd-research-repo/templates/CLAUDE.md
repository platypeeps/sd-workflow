# Research repo conventions

Applies to this repo. Full standard: run `sd-research-kit conventions` for its path,
or read it in the pack at `skills/sd-research-repo/references/conventions.md`.

This file was laid once by `sd-research-kit init-claude-md` and is never re-laid.
`sd-research-kit review` reports where it has fallen behind the pack's template; a
section this repo states differently on purpose goes under `## Local overrides of the
shared template`, with the reason.

## Layout

| Path | Holds |
|---|---|
| `00-overview/` | `understanding.md`, `open-questions.md`, `next-research.md` — fixed names |
| `05-architecture/` | `architecture.md` — one overview: an Archify diagram of the system or domain, its parts, and which stage covers each |
| `10-sources/` | `registry.md` (what was read, when, status), `references.md` (link list) |
| `20-map/` | `MAP-*.md` — structured ledgers, crosswalks, comparisons |
| `30-brief/` | `BRIEF-*` argued positions · `REVIEW-*` assessments · `SUMMARY-*` condensations |
| `40-docs/` | `PRD-` `DESIGN-` `PLAN-` `SPIKE-` `DISCOVERY-` `BENCHMARK-` `DECK-` `OUTREACH-` |
| `90-scratch/` | Throwaway and superseded. Never cited. |
| `docs/dashboard/` | Rendered HTML, served by the dashboard. Generated — never hand-edit. Gitignored. |
| `vendor/` | Third-party clones you only read. Gitignored. |

Use only the directories this repo needs; do not invent new ones.

An official repo you change is not vendored. Clone it into the official tree
(`~/repos/<org>/<name>`), register it, and file its code work there.
This repo keeps the research, with a private remote of your own.

## Main document — START HERE

Choose exactly one main document titled `START HERE — <descriptive project or decision title>`.
Apply this during setup and the next update to an existing project.
Match its Markdown H1 to the `research.conf.py` `title` and `h1`.

Follow the full standard's **Main document — START HERE** section.

## Documents

Every document opens with an H1, a provenance paragraph, then `---`:

```markdown
# Title

Compiled 2026-08-27 from `owner/repo` @ `abc1234`, plus <other sources>.

---

## 1. First section
```

- `##` headings become the rail nav. Number them when order matters.
- State what is **verified** and what is **not verified** in a closing Status section.
- Corrections stay visible — say what changed and why, do not silently overwrite.

## Rendering

```bash
sd-research-kit render
```

Add or edit pages in `research.conf.py`, never by editing generated files.

## Adversarial review before publishing

```bash
sd-research-kit review
```

It checks provenance blocks, Status sections and build freshness, then prints the checklist
for the half no script can do. Exit 1 means fix it first.

The second reader is `sd-review --lens research-brief`, as the checklist prints it. It takes
the reviewer from the provider registry's reviewer order, not from one named CLI, and moves to
the next enabled reviewer when one fails. The lens keeps the brief attacked as an argument.

Record the outcome in the Status section: what was verified and how, what was not, what was
cut, and the second reader's pass — which reader, date, what it raised, what changed, what
was rejected and why. A review that found nothing says what it checked.

## Publishing — local by default, mirrors on request

`sd-research-kit render` publishes every overview, map, brief, report, and survey as HTML
on the dashboard's Documents tab. A document whose `research.conf.py` entry carries
`obsidian=dict()` also gets a Markdown copy in the Obsidian vault; the vault's own repo
writes every entry. Both copies are local. `90-scratch/` is not published.

**Do not publish research as an artifact.**

An outward mirror is opt-in, per document. The user designates a document. Record the
designation as a `notion=` or `drive=` key in that document's `research.conf.py` entry.

By default a designated document mirrors into this repo's own page or folder at that
destination — put its URL here when the repo designates one.

## Parallel work

Reading fans out to read-only subagents; writing stays in one lane, one writer per
checkout. Give each subagent a budget and a deadline, and treat no report by the
deadline as a failure. The rules are the pack's `WORKFLOW.md` section **Parallel work**
and `skills/_shared/references/subagent-dispatch.md`.

Subagents return findings as text. Inside a subagent, Claude Code's `Write` tool refuses
a basename matching `/^(REPORT|SUMMARY|FINDINGS|ANALYSIS).*\.md$/i`. Name scratch files
for their subject, such as `source-notes.md`. The main thread writes `30-brief/SUMMARY-*`.

## Style

- Absolute paths when pointing at a local file: `file:///Users/...`, not a bare path.
- Prefer a number with its validation status over an adjective.
- One claim, one source. If it came from Jira, cite the ticket; from code, cite `file:line`.
