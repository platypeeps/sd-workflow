# Research repo conventions

The standard every research repo follows. Written 2026-08-27, when six repos were
aligned onto it; moved into this pack on 2026-09-03, so the standard and the
tooling that enforces it are versioned in one repository rather than two. Each
repo's `CLAUDE.md` points here; where a repo needs something this document does
not cover, it says so in its own `CLAUDE.md` rather than inventing a private
variant.

**Which repos follow it, and where each publishes, is not recorded here.** Those
are per-repo bindings and they live in each repo's own `research.conf.py` — a
table in a shared document has to be edited in a seventh place every time a
seventh repo appears, and it is wrong from the moment one is renamed.

Fan-out and file-tool rules are not here either: they are properties of the
harness rather than of this standard, and live in
`references/subagent-dispatch.md`.

## The repo's own `CLAUDE.md`

Every research repo carries a `CLAUDE.md` — the short form of this standard, for
an agent working in the repo with no pack checkout to read. It comes from
`skills/sd-research-repo/templates/CLAUDE.md`, and until 2026-09-12 it came from
there by no written route at all: five repos carried a copy, nothing said to
install one, and nothing re-read them. One divergence sat unnoticed for six
weeks and was reported three separate times as three findings.

**Laying the first copy is a supported operation, and re-laying it is not.**

```bash
sd-research-kit init-claude-md     # from inside a repo that has none
```

That verb writes `CLAUDE.md` and refuses outright if one is already there. The
refusal is the design, not a gap: a research repo legitimately states parts of
the template differently — a repo whose Notion folder is a team space replaces
the template's personal `file:///` Source header, because the page is read by
people who have no such checkout — and a writer that merged the template back
over an existing copy would undo that silently, putting an absolute path on a
team page. There is no re-sync verb and there should not be one.

So after the first copy, the file is managed by being **checked** rather than by
being rewritten. `sd-research-kit review` compares this repo's `CLAUDE.md`
against the template and reports what the repo no longer says. The comparison is
one-directional: the template is a floor, not a ceiling. Anything the repo adds
— a whole local section, an extra paragraph inside a shared one — is the repo
doing its job and is counted, never reported.

The template is short on purpose. It keeps what an agent in the repo needs
every session: the layout, the main-document name, the provenance block, the
`render` and `review` commands, the local-by-default rule, the fan-out limits,
and the style bullets. The rest lives here, and the template points here.
The checklist and the second-reader command live in `sd-research-kit review`.

A repo block that says a template block and more is not drift. `review` ignores
case, markup and punctuation for that match. So a repo that still carries the
longer template text of 2026-09-23 passes, except `## Parallel work`, which no
repo carried. A repo can shorten its copy to the current template at any time.

### Declare intentional differences

Declare each intentional replacement under this heading:

```markdown
## Local overrides of the shared template

- `the opening`: use STE-Concise sentences without changing the template instructions.
- `## Publishing — local by default, mirrors on request`: omit
  personal checkout paths from shared pages.
```

Use the exact template heading, including `##`, inside backticks.
Use `the opening` for content before the first H2 heading, including the document title.
Place actual declarations outside code fences.
The parser ignores examples inside triple-backtick fences.

- State a reason for each entry. Missing reasons fail review and leave drift findings active.
- Name an existing template section. Unknown sections fail review.
- Keep overrides narrow. An opening override does not cover H2 sections.
- Read each overridden section again when the template changes. Later changes within that section receive no comparison.

`review` prints each accepted override, its reason, and the count of suppressed drift findings.
The opening declaration follows the same rules as section declarations.
The parser and filtering logic live in `source:bin/sd_research_review.py::declared_overrides` and `source:bin/sd_research_review.py::apply_overrides`.

A repo with no `CLAUDE.md` at all, and a pack install whose `skills/` is missing
from beside its `bin/`, both fail the review rather than passing quietly: a gate
that cannot run has not passed.

## Layout

| Path | Holds |
|---|---|
| `00-overview/` | `understanding.md`, `open-questions.md`, `next-research.md` — fixed names |
| `05-architecture/` | `architecture.md` — one overview: an Archify diagram of the system or domain, its parts, and which stage covers each |
| `10-sources/` | `registry.md` (what was read, when, status), `references.md` (link list) |
| `20-map/` | `MAP-*.md` — structured ledgers, crosswalks, comparisons |
| `30-brief/` | `BRIEF-*` argued positions · `REVIEW-*` assessments · `SUMMARY-*` condensations |
| `40-docs/` | `PRD-` `DESIGN-` `PLAN-` `SPIKE-` `SURVEY-` `DISCOVERY-` `BENCHMARK-` `DECK-` `OUTREACH-` |
| `90-scratch/` | Throwaway and superseded. Never cited. |
| `assets/` | Images the docs reference |
| `docs/dashboard/` | Rendered HTML, served by the dashboard. Generated — never hand-edit. Gitignored. |
| `vendor/` | Third-party clones you only read. Gitignored. |

Use only the directories the repo needs; do not invent new ones. The numbers are
reading order, not a workflow — `00` is where a newcomer starts, `90` is where
nothing is cited from.

An official repo you change is not vendored. Clone it into the official tree
(`~/repos/<org>/<name>`), register it, and file its code work there.
This repo keeps the research, with a private remote of your own.

Type prefixes carry meaning:

- `SURVEY-` mirrors an **external** system — a repo, a service, a running app.
  Record what it was read at (commit, or the date for a live surface) in the
  provenance block, and refresh when that system changes materially.
- `SPIKE-` is a measurement plan or a measurement result, with its gates declared
  up front.
- `REVIEW-` assesses something someone else produced; `BRIEF-` argues a position
  of our own.

### The architecture overview

`05-architecture/architecture.md` is one document per project: the system or
domain under study, its parts, how they connect, and which later stage covers
each part. A newcomer reads it before the sources, to place every later
document. Copy it from `skills/sd-research-repo/templates/05-architecture/architecture.md`.

- Draw the diagram with Archify when it is available. Export it as SVG and
  paste the `<svg>` into the template's `<div class="diagram">` block: a served
  page loads no external image (see Publishing).
  `render` scopes the export's page-wide `:root`, `svg` and `[data-theme]`
  rules to `.archify-diagram`, so they follow the page's theme and restyle
  nothing else, and drops the export's trailing whitespace from the page.
  Strip it from the pasted source too (`sed -i '' 's/[[:space:]]*$//'`): the
  `@keyframes` lines end in a space, which fails a whitespace check.
- Without Archify, say so in Status and keep only the plain diagram in the
  fenced `text` block. Keep that block for a document designated for Drive too:
  the mirror imports the Markdown, not the rendered page.
- A Notion mirror shows the Archify diagram as an image and drops the `text`
  block right after it (sd:6066): `render` draws the SVG as a PNG with Chrome
  and the drain uploads it. Without Chrome the Notion request is not queued,
  and `render` says why.
- Add it to `research.conf.py` like any other document, for example
  `dict(src="05-architecture/architecture.md", out="architecture", title="Architecture — <project>")`.

`sd-research-kit review` warns when the overview is missing or no `DOCS` entry
renders it. It **does not fail**: every research repo predates the stage
(pack sd:5660).

### Maps: stable row IDs and a gaps section

Every ledger numbers its rows with stable IDs: `C01`, `C02`, and so on. Later
cards, decisions and gaps cite those IDs, in prose, in a ticket or in a meeting.
Never renumber a row; retire its ID and say so.

Every map closes its ledger with a gaps section, under a heading that carries
the word `Gaps`. Bucket every ID, then name the leftovers: rows with no owner,
no card or no decision. A map that names no gaps claims a completeness it has
not checked.

`sd-research-kit review` checks each `20-map/*.md`: at least one table whose
first column is all stable IDs, and a gaps heading. Which table is the ledger
is not mechanical, so one ID-keyed table satisfies it. A miss **warns and does
not fail**. The rulings of 2026-09-30 (sd:1835, sd:1836) make both required but
do not say they fail the review, and every research repo predates them.

### The source registry carries a status

`10-sources/registry.md` gives every source a `Status` column: `approved`,
`draft`, `restricted` or `superseded`, or the repo's own documented set. A
source someone else owns starts as `draft` until its owner approves it. Re-read
the handling notes of every cited source before publishing. A document that
cites a source not `approved` does not publish until the status changes or the
citation goes. This is guidance; no check reads the column (ruling of
2026-09-30, sd:1838).

`registry.md` also keeps a `## Corrections on record` section: the claims that
reading the real source overturned, each one kept, not removed (sd:1841).

## Main document — START HERE

Every research project has exactly one main document for readers entering the document set.
Use the title `START HERE — <descriptive project or decision title>`.
Choose the document that explains scope, current conclusions, decisions, and where supporting evidence lives.
For multiple research tracks, use an overview that directs readers to each track.

- Apply this convention during project setup and the next update to an existing project.
- Use the same title in the Markdown H1, `research.conf.py` `title` and `h1`, and the title of every mirror.
- Put a prominent **Start here** link near the top of the README.
  Link a mirror when the document has one; otherwise, link the Markdown source.
- Identify the main document in the README document table and in each mirror's parent container.
- Keep the existing source filename, type prefix, mirror page or file id, URL, icon, and parent.
  The title identifies the document's role.
- If another document takes this role, move the `START HERE — ` title marker to the
  new one, strike it from the old one, and update all entry links.
  Rename no file and re-parent no mirror: a role change edits titles and
  links only, never a filename, a type prefix, or a mirror's identity.
  Exactly one document carries the `START HERE — ` marker.

`sd-research-kit review` enumerates the configured documents and checks the two
surfaces that are inside the checkout: each document's H1, and the `title` and
`h1` the config renders it under. Missing, duplicate and mismatched all fail it.
Its scope is the configured documents, not the tree — a superseded draft in
`90-scratch/` still carrying an old marker is neither cited nor mirrored, and is
not a duplicate.

The README entry link and each mirror's title and parent are in no file the kit
reads, so reading those back after publication stays manual. The
review's `ok   main document` line names the surfaces it did check, so that a
pass is not read as a claim about the two it did not.

## Status brief — an experiment's fixed sections

A research repo that runs an experiment keeps one status brief, `30-brief/BRIEF-status.md`.
It is the START HERE document for the experiment, and the one input to every other status surface:
the experiment row, the status deck and the dashboard's Experiments view all read it, and none is edited on its own.
Piloted in mcp-research (sd:5499); the template is `skills/sd-research-repo/templates/BRIEF-status.md`.

Keep these six sections, numbered and in this order:

1. **Status at a glance**: a two-column table with these rows, in this order.
   - `State`: one of `Running`, `Waiting on <who or what>`, `Blocked on <what>`, `Decided` or `Done`; the first word is the state.
   - `Updated`: the day the brief was last made true, `YYYY-MM-DD`. Over 7 days old, the dashboard calls it stale.
   - `In one line`: the experiment's state in one or two sentences.
   - `Experiment`: a link to the experiment's row, and its iteration.
   - `Hypothesis`: this iteration's hypothesis, as one sentence.
   - `Deck`: a link to the current status deck, once one is imported. Optional.
2. **What we learned**: one finding per bullet, with its source and what was not probed.
3. **What shipped**: a table of the changes, each linked, merged or open.
4. **Asks**: a `Who | What` table. Asks are actions someone else must take now; decisions that wait go in Next.
5. **Next**: a numbered list, then the tracker's tickets when the work is tracked.
6. **Status**: verified, not verified, the second reader's pass, and each update as a dated entry.

Use no emoji in the State cell: the deck and the mirrors are read outside the team, and the word carries the state.

### The deck beside the brief

`30-brief/deck.json` holds the brief condensed to at most three slides.
Update it in the same change as the brief, so the two never disagree.
`sd-research-kit deck` lays it out into `docs/dashboard/deck.pptx`: a title page, then the slides, with one call to
action on the last slide.
The example and the schema in use are the JSON block in `skills/sd-research-repo/templates/deck.md`; the builder refuses an unknown key,
an em dash, more than six cards (three beside a track) and text past its bound, naming the value's path.

- `name`, `tagline`, `date`: the deck's title page.
- `slides`: each has an `eyebrow` and a `title`, an optional `accent` (the title's tail, in the accent colour),
  an optional `track` of 2 to 5 stages (`done`, `now`, `next`), `cards` (`big`, `head`, `text`, `pill`, `tone`),
  a `foot` with an optional bold `foot_label`, and a `link` for the brief.
- `theme`: overrides the builder's colours and font key by key, and can name a PNG `logo` (relative to the deck.json); without one the builder uses the pack's Mezmo logo, `bin/fonts/mezmo-logo.png`
  and the one `cta` (`text`, `url`).
  A deck for an audience with its own brand kit takes its colours, font, logo and call to action from that kit.

Building writes a file and nothing else: importing it into Slides is the operator's step.
Update the existing deck and pages in place, so the brief's `Deck` row and every shared link stay current; create a new one only when none exists.

## Documents

Every document opens with an H1, a provenance paragraph, then `---`:

```markdown
# Title

Compiled 2026-08-27 from `owner/repo` @ `abc1234`, plus <other sources>.

---

## 1. First section
```

The renderer strips everything above `---` and builds the masthead from
`research.conf.py`, so that block is written for readers of the markdown.

- `##` headings become the rail nav. Number them when order matters.
- State what is **verified** and what is **not verified** in a closing Status
  section. A number with its validation status beats an adjective.
- Corrections stay visible — say what changed and why; never silently overwrite a
  claim someone may have already acted on.
- One claim, one source. From code, cite `file:line`; from a repo survey, cite
  the commit; from a tracker, cite the ticket.
- Supersession is stated in the document, not encoded in its filename.

### Grade every number

End the main document with one number-grade table. List every number the
document states, with its grade and what would settle it:

| Number | Grade | Source | What would settle it |
|---|---|---|---|
| 42% of traces carry a status | measured | the map's row C07 | — |
| $0.12 per GB stored | published | vendor price page, 2026-09-01 | an invoice |
| 3× ingest growth | modelled | the spike plan | a 30-day data pull |

Use four grades: `measured` (we verified it), `published` (a source states it),
`modelled` (we derived it) and `assumed`. The table tells a reader which claims
can carry a decision, and which data pulls would settle the most. This is
guidance; no check reads the table (ruling of 2026-09-30, sd:1837).

## Rendering

```bash
sd-research-kit render
```

Every verb acts on the repository the caller is standing in and takes no path
argument, so run it from inside the repo. The kit took `render [repo_dir]` before
it moved into this pack; R10-D6 says a command resolves its repository from the
current working directory and nowhere else.

Each repo supplies a `research.conf.py` naming `PROJECT` and a `DOCS` list;
per-doc keys are `src out title h1 eyebrow stand meta vtitle figs legend footer
skip links sibling notion drive`. The visual identity is the shared design
foundation (OKLCH tokens, IBM Plex, a Dark · Light · System control) and lives
in the renderer, so every repo renders the same way.

One form is written per document. `docs/dashboard/<name>.html` is standalone,
opens with `file://`, and is the form the dashboard's Documents tab lists and
serves — the published form, not merely a local one. The content-only
`build/artifact/` form is gone: nothing consumed it, and the dashboard folder's
contract is that everything in it is published. Add or edit pages in
`research.conf.py`, never by editing generated files.

Each render also writes `docs/dashboard/research.json`: `PROJECT`, the sha256 of
the `research.conf.py` it executed, each document's string fields, and a
`notion` and a `drive` flag saying whether the document designates that
destination. The dashboard never executes the config, so it reads a computed
one from there, designations set in a loop included.

Rendering ends by writing each designated document's Markdown into the Obsidian vault,
registering `docs/dashboard/` with the dashboard, and queueing a sync for every
document that designates an outward destination. Every step is idempotent. See
Publishing, and `references/publication-contract.md`.

Verify links before publishing:

```bash
sd-research-kit checklinks
```

It resolves markdown links relative to the containing file, and backticked paths
carrying a `NN-dir/` segment against the repo root. A backticked bare filename is
prose, not a link — docs legitimately name files that live in another repo or do
not exist yet. The hook `sd-research-kit init-hook` installs runs it after every
render and reports broken links (sd:1840).

Check what the documents pin:

```bash
sd-research-kit pins
```

For every `source` @ `sha` the markdown asserts, it says whether upstream has
moved past it. The list is read out of the documents each run, never maintained.

## Adversarial review before publishing

Nothing is published — to a mirror or anywhere else — until it has been reviewed
against itself. Two halves, both required: the **information** the document rests
on, and the **product** a reader will actually receive. The mechanical half is a
command:

```bash
sd-research-kit review
```

It checks what a script can decide — every document has a provenance block,
closes with a Status section that separates verified from not verified, and is
not newer than its build; each map has stable row IDs and a gaps section, which
only warns — and then prints the checklist for the half no script can do. Exit 1
means fix it first.

**The information.** Take the review adversarially: the job is to refute the
document, not to confirm it.

1. List the load-bearing claims. A claim is load-bearing if removing it changes
   the conclusion; anything else is context and does not need this treatment.
2. Open the cited source again and read it. Default to refuted — a claim stands
   only if the source *says* it, not merely that it is consistent with it.
   Second-hand support ("the summary says the paper found") is not support.
3. Numbers: check the unit, the date and the denominator, not the digits. A rate
   without its base has not been checked.
4. A claim no source supports is cut, or moved into Status as explicitly
   unverified. It never stays in the body, where a reader assumes it was checked.
5. Say what you could not check and why. A stated gap is useful; a silent one is
   a defect.

**The product.** Read what the reader gets, not what you meant.

6. Read the rendered page as someone who has not seen the source material. Does
   the conclusion follow from what is on the page, or only from what you happen
   to know?
7. Find the load-bearing assumption the document never states. There is usually
   one.
8. After mirroring, check the published page against the source, and that
   handling restrictions survived the mirror — a document that may not be shared
   externally may not become a shared page.

The outcome goes in the Status section: what was verified and how, what was not,
what was cut. A review that found nothing says so, and says what it checked —
"reviewed" without a record of what was examined is indistinguishable from not
reviewing.

Reviewing your own work is the weak form; it is the one that ships most often, so
it is the one to be disciplined about. Where the document carries a decision
someone will act on, get a second reader who was not involved in writing it.

**The second reader is the pack's review lane, `sd-review`, with the
`research-brief` lens.** It takes the reviewer from the provider registry's
reviewer order, not from one named CLI: this repo's consent, vendor
independence and fallback apply, so a reviewer that fails or is disabled hands
the pass to the next enabled one. The lens is what keeps the framing: the
prompt says the subject is a markdown brief, not code, and attacks the
argument. Its axes and caveats come from
`local-adversarial-gate/lenses/research-brief.md` and `core.md` in the `system`
repo, shared with `sd-writing`; the lane carries them in its findings
schema, so each finding states what would have to be true and a confidence tag
inside its summary.

This pass is the research flow's first review point, *after the brief and
decisions*. Its cap is the one on that row in the sd-workflow checkout's
`.claude/rules/sd-planning-adversarial-review.md`. The pass over the final
product, before the send box, is the second row and has its own cap. Read the
caps there — in the pack, which is where that file lives; a research repo does
not carry it. This file states none.

**The exact invocation is not retyped here** — the kit's own checklist is the
one place it is written, so nothing here can go stale against it:

```bash
sd-research-kit review
```

Under *The second reader* it prints the pass for both cases, and the fallback.
`CHECKLIST` in `bin/sd_research_review.py` owns that text. Do not copy it into
a repository guide, and do not name a provider there either: the registry
chooses.

Three things to get right:

- **Pick the scope that matches the commit state.** `--scope worktree` reviews
  the uncommitted change; `--scope branch` reviews what is committed on the
  branch.
- **Keep the lens.** Without `--lens research-brief` the lane's framing is a
  code review, and a change of only Markdown is routed to no reviewer at all.
- **The registry-independent fallback is for a lane that cannot run** — not
  installed, or a usage or configuration error. A reviewer that fails is not
  that case: the chain has already moved on. The checklist says what to do
  then, and Status says the registry was bypassed.

**What it cannot do.** The second reader sees the repository, not the sources.
It cannot discharge step 2 — opening the citation and reading it is yours, and
no second reader substitutes for it. What it does catch is the claim with no
citation behind it, the rate without its base, the assumption doing load-bearing
work off the page, and the conclusion that only follows if you already know the
material.

Record the pass in Status like any other check: which reviewer the chain picked
and on `<date>`, what it raised, what was changed and what was rejected with
the reason. If no reviewer completed — the lane's output says which were tried
and why — say *that* in Status. "No independent pass" is a
stated gap; self-review that quietly presents itself as review is the defect
this section exists to prevent.

## Publishing — the dashboard by default, outward mirrors on designation

`references/publication-contract.md` governs this. It applies to every skill in
the pack, not only to research repos; what follows is how a research repo meets
it.

**Superseded 2026-09-20.** Before this date the standard said every overview,
map, brief, report and survey had a Notion page. That is now the exception
rather than the rule: an outward destination is outward-facing, so a document
reaches one only when the user designates it. The designation alone is enough,
because each destination has a default container; naming one overrides that
default.

### Obsidian and the dashboard are the defaults

`sd-research-kit render` ends by writing the Markdown of each document that
designates `obsidian=dict()` into `$OBSIDIAN_VAULT/Briefs/<repo>/<name>.md`. The repo's channels setting may name
another vault folder, or turn Obsidian off; see the publication contract's
"A repo's channels setting". The document is the Markdown in the repo; the vault
copy and everything else are duplicates of it in another format. The note carries
frontmatter naming the source repo, path and revision, so a reader knows which
checkout owns it and that the note is not the place to edit. The revision is the
commit whose bytes the note holds; a file no commit holds says `uncommitted`.

`render` also registers `docs/dashboard/` with the local dashboard's Documents
tab, and the tab lists and serves **everything it finds there**. That folder is
gitignored — it is regenerated whenever git moves the tree — and `render` adds
the ignore entry itself. The default location needs no line in the
dashboard's `documents.conf`: the dashboard enumerates `docs/dashboard/` from
disk and takes its name from the workflow database (sd:3986), so a row would
be the default written down a second time. `render` writes a
`root|<key>|<label>|<directory>` line only for a directory outside it. The file
is `project-dashboard/documents.conf` under `$SYSTEM_TOOLS_CONFIG`, which
defaults to `$XDG_CONFIG_HOME/system`, else `~/.config/system`. The dashboard
reads that file and never writes it, so the repo keeps owning its own output.

The Documents tab serves under `default-src 'none'; style-src 'unsafe-inline';
img-src data:; font-src data:`. The renderer already meets it — fonts are
embedded as data: URIs by `bin/sd_research_fonts.py`, styles are inline, and
the one script, the foundation's theme control, degrades to the reader's OS
scheme where it is blocked. Do not add a `<link>`, another `<script>` or a
remote image to a rendered page: it will not load, and the page will render
degraded rather than fail visibly.

`90-scratch/` is not published. Neither is working state — ledgers, receipts,
handoff packets. Publish what a reader is meant to read.

### Outward destinations are per document

The user designates a document. Each destination has a default container, so
the designation alone is enough; the user names a container only to override
the default. Record the designation in that document's `DOCS` entry, where the
document is already described:

```python
notion=dict()                    # private briefs folder, <repo> page
notion=dict(team=True)           # team briefs folder, <repo> page
drive=dict()                     # My Drive, Briefs/<repo> folder
drive=dict(folder="Research deliverables", file="https://docs.google.com/document/d/...")
```

| Destination | Key | Container | Existing page or file (optional) |
| --- | --- | --- | --- |
| Notion | `notion=` | a configured page id per scope, `<folder>/<repo>`; `space=` overrides it | `page=` |
| Google Drive | `drive=` | defaults to `Briefs/<repo>`; `folder=` overrides | `file=` |

Both keys on one entry are two mirrors, not a choice. Until one of these keys
exists, the document publishes locally and nowhere else. Do not infer a target
from a title, a folder or a neighbouring document.

The vault copy is designated the same way, with `obsidian=dict()` on the entry:
without it, a repository other than the vault's own writes no vault copy
(system sd:5500). The vault's own repository writes every entry.

`notion=True` is the same designation as `notion=dict()`, written shorter, and
a write-back amends it into `notion=dict(page="<id>")`. `None` and `False` are
the off position: they designate nothing, enqueue nothing, and never receive a
written-back id.

**A Notion mirror is private unless it asks for the team.** `notion=dict()`
goes to the folder the `sd.notion_private_folder` setting names;
`notion=dict(team=True)` goes to the one `sd.notion_team_folder` names.
Private is the default because the two mistakes are not symmetric: a
brief the team cannot see is repaired by adding `team=True` and draining again,
and a private brief in a shared team space has already been read. `space=`
overrides the folder, never the scope.

**Each default folder is a configured page id.** Set it per operator, as a
workflow database row:

```sh
sd config set sd.notion_private_folder <notion page id>
sd config set sd.notion_team_folder <notion page id>
```

While the database holds no row for one, its old variable answers
(`SD_NOTION_PRIVATE_FOLDER`, `SD_NOTION_TEAM_FOLDER`).

A page id and not a folder name, because a name lookup that finds nothing
returns an empty result rather than an error, so a rename would move every
default mirror to nowhere in silence. Configured and not shipped, because a
page id belongs to one Notion account. A page URL works in place of a bare id.
An unset setting, or one holding a name rather than an id, refuses that mirror
and names the setting; the local copies are written either way.

**A default lands in the repo's own page under that folder**, the way a Drive
default lands in `Briefs/<repo>`. The request names the repo in `subfolder`,
and the drain creates that page when it is missing.

A `space=` override written as a page id or page URL is pinned by id too, and
it replaces the whole container, so no `subfolder` is appended to it. Written
as a plain name it cannot be pinned, and the request says which by carrying
`resolve: "id"` with `space_id` or `resolve: "name"` with the name in `space`.

**A Drive mirror lands beside its siblings.** `drive=dict()` goes to
`Briefs/<repo>` in My Drive, the same shape the vault uses, so the two copies
agree on where a brief lives. The drain resolves the path and creates the repo
folder when it is missing. `folder=` overrides it for a document that belongs
somewhere a reader already looks.

The folder `sdw.drive_publishing_folder` names is not this default. That one is
the primary blog's gate, and a brief placed in it would read as cleared for
publication.

The page or file is optional everywhere. Without it the drain creates the page
or file and writes the id it got back into the designation, before it deletes
the request. With it the drain updates that one, which is what stops a re-render
leaving a second copy behind. `references/publication-contract.md` holds the
drain's step order, and this page states no second version of it.

Recording it in `research.conf.py` is what makes the mirror machine-readable.
The README's **Notion pages** table stays, for the human reader, but it is no
longer the only place the target is written; a Drive mirror is listed there too.

### How a mirror is updated

A render does not call Notion or Drive. It runs in a git hook and in CI, neither
of which reaches an MCP server, and the pack holds no credential for either.
Instead it writes a sync request per designated document per destination under
`~/.claude/pending-mirror-syncs/`, naming the destination, the document, its
container, the page or file to update and the source revision.

An agent session drains that queue through the connector the request names, in
that destination's native format: the Notion connector for
`destination: notion`, as Notion blocks; the Google Workspace connector for
`destination: drive`, as a native Google Doc. A Notion request also carries a
`scope`, `private` or `team`, and a `private` request never reaches the team
space whatever its folder is called. It carries `resolve` too: with `id`, write
under the page `space_id` names and never search by name; with `name`, a lookup
that finds nothing is a failure to report, not a folder to create. Where it
names a `subfolder`, the document goes in that page under the container, which
the drain creates when it is missing. The source for every mirror is the
request's `content`, the Markdown as it stood when the request was queued,
each soft-wrapped paragraph joined onto one line (sd:5872):
never the rendered HTML, and never the file the request's path names, which
may have changed since. One request file per document per destination, so a
re-render replaces the pending request rather than queueing a second one; a
request that is never drained stays on disk. A document whose Markdown, title
and container have not changed since it was last queued is not queued again,
whether that request is still pending or the drain has recorded it as
delivered: `render` keeps a receipt beside the queue, the drain's last step
(`sd-research-kit delivered`) writes the delivered half of it, and
`SD_MIRROR_REQUEUE=1` queues every designated document regardless. A render
from a linked worktree writes that worktree's `docs/dashboard/` and nothing
else -- no vault copy, no request -- unless `SD_PUBLISH_FROM_WORKTREE=1` is on
the invocation; the refusal names the main checkout to run in. The main
checkout queues no request off its default branch, under the same switch.
`SD_SKIP_MIRROR=<reason>` renders and queues nothing; the hook sets it for a
branch checkout behind its upstream, so the pull that follows queues instead.
`references/publication-contract.md` says how the status report surfaces one.

Mirror shape — the full document minus its H1, opening with a pointer back to
the file so a reader who lands in the mirror knows where to edit. The pointer
links the request's `source_url` verbatim, the GitHub URL the render read from
`origin`; where that is `null`, it names the `source` path instead. The drain
never composes a GitHub URL itself:

```markdown
*Source: **[`<repo-relative path>`](<source_url>)** — edit there, then update this page.*

<the document's provenance line>

---

## 1. First section
```

Without a `source_url`, the first line is
`` *Source: **`<absolute path to the markdown>`** — edit there, then update this page.* ``

**Notion.** It round-trips tables, fenced code blocks (ASCII diagrams
included), blockquotes, and nested lists faithfully; write an empty table cell
as a single space. A document too large for one `notion-create-pages` call is
created with its first half, then extended with `notion-update-page` /
`insert_content` at `position: end`. Give each page an icon and keep it stable
across updates — a changed icon reads as a different page.

**Google Drive.** Mirror as a Google Doc, not as an uploaded `.md` or `.html`
file: the point of the mirror is that someone can read and comment on it in
place. Create it with the Workspace connector's Markdown import so headings,
tables and code blocks survive, then record the resulting file id in `file=`.
Update that file in place on later drains. Keep the document's name equal to its
title, since Drive search is by name and a renamed copy reads as a second
document. Resolve the folder to an id once and prefer the id thereafter
— two folders may share a name, and a mirror written into the wrong one is a
disclosure rather than a mistake. `Briefs/<repo>` resolves under My Drive's
root, and the repo folder is created when it is missing; a `Briefs` folder
found anywhere else is a different folder and not this one.

Handling restrictions survive the mirror, at every destination: a document that
may not be shared externally may not be mirrored to a shared Notion space or a
shared Drive folder either, and designating one does not lift the restriction.

### Artifacts

Research is still not published as artifacts — the hosted single-page surface
the renderer's retired `build/artifact/` form was written for. Pages published
as artifacts before 2026-08-27 stay where they are as historical record.

## Style

- Absolute paths when pointing at a local file: `file:///Users/...`, never a bare
  path — a bare path gets linkified into a URL that cannot resolve.
- Every repo is a git repo, and every restructure moves files with `git mv` so
  history follows.
