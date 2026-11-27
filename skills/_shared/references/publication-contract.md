# Publication contract

Read this before deciding where a finished document goes.

This contract applies to the pack and to every consumer of it. It states two
default destinations and a set of outward ones, each opt-in per document. It
does not decide whether a document is finished; that is the producing skill's
judgment.

| Destination | When | Format | Written by |
| --- | --- | --- | --- |
| Obsidian | designated, or every document of the vault's own repo | Markdown | the render, directly |
| Dashboard | always | HTML | the render, directly |
| Notion | designated | Notion blocks | an agent session, from the queue |
| Google Drive | designated | native Google Doc | an agent session, from the queue |

A document is the Markdown in its repo. Obsidian is one channel, designated per
document like Notion and Drive, and a repo may turn it off (see
[A repo's channels setting](#a-repos-channels-setting)).
The others are duplicates of it, each in the
format its destination reads natively — a Markdown file pasted into Drive, or an
HTML blob pasted into Obsidian, is a copy nobody can use where it landed.

## Obsidian is the local copy

A finished document whose DOCS entry carries `obsidian=` (any value but `None`
or `False`; the dashboard writes `obsidian=dict()`) is written into the vault as
Markdown, under `Briefs/<repo>/<name>.md`, or under the folder the repo's
channels setting names. The vault's own repository, the checkout
`$OBSIDIAN_VAULT` names, mirrors the whole vault, so every one of its entries is
written without the key (system sd:5500). `$OBSIDIAN_VAULT` names the vault; a
machine with none still publishes everywhere else and reports the missing copy
by name.

A render never deletes a vault copy: an entry that loses `obsidian=` keeps its
old copy until the dashboard's untick moves it, by `trash_obsidian`, into the
vault's `.trash`, where Obsidian restores it. Only this checkout's copy moves,
by the ownership rule the write uses.

Markdown, because the vault's whole value is that a note is editable, linkable
and searchable in place. The file carries a frontmatter block naming the source
repo, the source path and the revision, so a reader who finds the note knows
which checkout owns it — and knows the note is not the place to edit. The body
is that revision's bytes, not a later working-tree edit; a file no commit holds
is written from disk with `revision: uncommitted`.

This is a local filesystem write, so a render performs it directly. It needs no
connector, no credential and no drain, which is what lets the primary copy be
the one that never waits.

## The dashboard is the other default

A finished document is also published to the local dashboard's **Documents**
tab, as HTML. Publication is the default, not a step the user requests. A
document nobody can find was not delivered.

The HTML is written to `docs/dashboard/` in the producing repository, and the
dashboard serves **everything it finds there**. That folder is gitignored: it is
regenerated whenever git moves the tree, and tracking it would put a generated
diff in front of a reader on every render.

The dashboard reads the directory and never writes to it, so the producing
repository owns its own output. A report belongs to the repository that
generates it.

Both default destinations are local. Writing a note into your own vault and
serving a page on loopback are not disclosure, and this contract grants no
authority to send anything outside the machine. Every outward destination below
is opt-in per document, and none is reached by default.

## What publishes, and what does not

Publish the work products a reader is meant to read: reports, briefs, proposals,
reviews, specs, plans, status summaries, research documents.

Do not publish working state. Adjudication ledgers, check receipts, handoff
packets, monitor state, profile artifacts and scratch output stay where they
are. They have readers, but the reader is the next skill, not a person browsing
a list. A Documents tab that lists everything lists nothing.

When a skill is unsure, it publishes. An unwanted page is one line removed from
a conf file. An unpublished finding is a finding nobody read.

## A published page carries its own resources

Served documents run under `default-src 'none'; style-src 'unsafe-inline';
img-src data:; font-src data:`. An external stylesheet, script, font or image is
blocked, and the page renders degraded rather than failing loudly.

So a published page embeds what it needs. Fonts are data: URIs, images are data:
URIs, styles are inline, and there is no script but one. The same page then
opens identically over `file://` with no network, which is the other way these
pages are read.

`bin/sd_research_fonts.py` holds the vendored faces for the research renderer.
The one script is the design foundation's theme control, inline and verbatim
(`THEME_JS` in `bin/sd_research_tokens.py`). Where a policy blocks it, the page
follows the reader's OS scheme and hides the control, so blocking it costs the
choice and nothing else.
A skill that emits its own HTML meets the same rule by itself.

## Registration

The dashboard finds `docs/dashboard/` by itself: every checkout under its
`REPO_ROOT` holding that directory is a document root, enumerated from disk.
So a repository publishing to the default location enters the Documents tab
by having the directory. Its name is the workflow database's `view` row, set
on the Documents page, and the dashboard reads no `label|` line (sd:3986), so
the default location needs no row in `documents.conf` at all.

`root|<key>|<label>|<directory>` stays for a directory the dashboard cannot
find — somewhere outside `docs/dashboard/`, as `hoa` publishes into `reports`.
It is the one form that can say something the disk does not, so it is the one
form that should carry a path. **Do not write a `root|` row for the default
location.** It is the default written down a second time, and it goes stale the
moment the repository moves while still looking authoritative — which is the
drift the enumeration removed.

For a research repo, `sd-research-kit render` checks the file at the end of
every run, and `sd-research-kit publish` does the same without rebuilding. It
writes no row for `docs/dashboard/`, and a `label|` row an earlier renderer
wrote stays where it is: the Documents page names it, and Rename replaces it.
A `root|` row for a directory outside the default is written once and is
idempotent, and a key already naming another directory is reported rather than
overwritten.

One exception to "reported rather than overwritten": a row for this
repository's key naming a directory *inside this repository* is this
repository's row, and is rewritten in place rather than duplicated. That
retires a `build/` row — the published directory before `docs/dashboard/` —
and it retires a `root|` row for the default location that an earlier renderer
wrote, because that row outranks the directory the dashboard finds. A
row naming *another* repository's directory is still a collision, and is still
only reported.

It does not belong in the dashboard's source: a fleet dashboard carrying one
repository's path would be wrong in a way that is awkward to undo, which is why
the conf file exists. It lives in the machine's config directory, not the
dashboard checkout: `$SYSTEM_TOOLS_CONFIG/project-dashboard/documents.conf`,
with `$SYSTEM_TOOLS_CONFIG` defaulting to `$XDG_CONFIG_HOME/system`, else
`~/.config/system`.
A machine without that file still renders its documents. The registration is
reported as skipped, naming the path it looked for, and the render succeeds.

`documents.conf` is shared across machines. A root that is absent on this
machine is reported by name rather than hidden, because a silently missing
section looks the same as a report that was never generated.

## Staying current

Rendered output goes stale the moment its source changes, and a stale page is
worse than a missing one because it looks current.

A repository that publishes re-renders when git changes a document. For a
research repo, `sd-research-kit init-hook` installs the hook that does it, as
post-commit, post-merge and post-checkout, so a pull or a branch switch renders
too. After the render it runs `sd-research-kit checklinks` and reports broken
links. The hook never fails the git command, because the change has already been
made when it runs.
A render executes `research.conf.py`, so every trigger of the hook, a commit
included, renders only a config a render by hand has executed there before.
Otherwise it names the file and asks for `sd-research-kit render` once it has
been read. Only that render by hand records a config as read; a render the hook
or git runs checks the record again and writes nothing.
The dashboard's freshness indicator is then a statement about the render, not
about whether anyone remembered. A verification-only mode that fails on stale
output is acceptable where a write hook is not wanted; silence is not.

## Outward destinations are per document, and opt-in

An outward mirror is the durable shareable copy. Outward destinations are
outward-facing by definition, so no document reaches one by default and no skill
infers a target from a title, a path or a neighbour.

The user designates a document at that time. The designation is recorded where
the document is already described — a destination key on that document's entry
in `research.conf.py`:

| Destination | Key | Container | Existing page or file (optional) |
| --- | --- | --- | --- |
| Notion | `notion=` | a configured page id per scope, `<folder>/<repo>`; `space=` overrides it | `page=` |
| Google Drive | `drive=` | defaults to `Briefs/<repo>`; `folder=` overrides | `file=` |

    notion=dict()                    # private briefs folder, <repo> page
    notion=dict(team=True)           # team briefs folder, <repo> page
    drive=dict()                     # My Drive, Briefs/<repo> folder
    drive=dict(folder="<name or id>", file="<file id or url>")

Both keys on one entry are two mirrors, not a choice. Until one of these keys
exists, the document publishes locally and nowhere else.

`item=<number>` on the entry names the sd item the document belongs to. Each
request carries it, and step 6 records the mirror's URL on that item, under
`fields.published` with one `Published <channel> <document>: <url>` note, so
the item shows where its document was published. The render records the vault
copy's `obsidian://` link the same way, for a copy it wrote from the main
checkout's default branch; a link that does not land is reported, never fails
the render, and the next render records it. The item is part of the
fingerprint: adding or changing it queues the document once, so the next
drain records the URL on the new item.

### A routine brief is designated on its item

A routine brief (`sd reports brief`) is a database row with no `research.conf.py`
entry, so its item holds the designation: `fields.publish`, with the same
`notion` and `drive` keys and values, as `{"notion": {}, "drive": {"folder":
"<name>"}}` (sd:5102). Each brief render queues every designated brief into the
same queue, with the same request, fingerprint and receipt. The request carries
the row's text as `content`, the brief's item as `item`, and `sd:<item>` as its
`source`, which the pointer line names. Step 5 writes nothing for a brief: step
6 records the URL on its item and, with the container it went to, on the
receipt. The next render carries that URL as `page=` or `file=` only while the
request names the same container, as step 4's carried id; another scope, space
or folder publishes afresh there, and a page or file the designation names
wins.

### A writing piece mirrors from review on

A writing piece (`sd writing`) has no `research.conf.py` entry and no
designation: its stage decides (pack sd:7243). A piece that is not parked, at
`review` or `ready`, mirrors to the Drive folder `Writing/Drafts`; at
`published`, to `Writing/Published`. Both sit under My Drive, in each account,
and the drain creates one that is missing. Any other stage queues nothing, and a
copy already made stays where it is.

Each file that exists is its own document: the draft `index.md`, `research.md`,
`fact-check.md` and `adversarial.md`, as `<year>-<slug>-<stem>` with stem
`draft`, `research`, `fact-check` or `adversarial`, titled
`<piece title> · <stem>`. A name past 160 bytes keeps its head, a digest of
the whole and the stem (`writing.piece_out`), so its queue file fits. The request carries the file's text, read from the
registered checkout and never a linked worktree's, as `content`, the piece's
item as `item`, and `sd:<item>/<file>` as its `source`. It names no vault copy.

**The realm picks the account.** The request names `account`, the Google
address of the piece's realm: the work realm's `google.accounts.work.email`,
any other realm's `google.accounts.personal.email`. Write it with that
account, as `user_google_email`, and never another. A realm list not read
whole, a realm or an account that cannot be read holds the piece, named.

**A published piece moves.** The request says `moves: true`, and the file id
delivered to Drafts is carried to the Published request. Where the request
names a `file` whose parent is not `folder`, move it there first with
`update_drive_file` (`add_parents` the folder, `remove_parents` the old one),
then update it as below. A stop after the move leaves the request; the next
drain finds the file in place and only updates it. A copy in the other account
is never carried: a piece whose realm changed gets a new copy there.

A piece has no render, so the `sd writing` verbs that write one (`promote`,
`register`, `stage`, `metadata`, `gate`, `park`, `adversarial`, `reconcile`,
`publication-reconcile`) queue it after their write, and so do the dashboard's
Stage and Park. The dashboard service also sweeps every piece each 5 minutes,
so a hand edit reaches the queue with no verb after it. Only a file whose text
changed is queued again. The queueing never fails the write. Step 5 and the carried URL work as for a brief. A piece
never mirrors into the folder `sdw.drive_publishing_folder` names: that is the
blog's publishing gate.

### A repo's channels setting

A repo may name the channels it uses, and a folder for each, on its `repo` row
(`bin/sd-db repo channels`, `docs/sd-db.md` § Channels). The setting says where,
never whether: an outward mirror still needs its key on the document.

- **No setting, no change.** A NULL value, no row, or a machine with no
  workflow database publishes as described here, byte for byte.
- **A folder replaces the default.** Obsidian: the folder under the vault, for
  `Briefs/<repo>`. Notion: the page under the scope folder, for `<repo>` in
  `subfolder`. Drive: the path under My Drive, for `Briefs/<repo>`. `{}` keeps
  the default.
- **A named folder is the operator's.** Nothing there that this tool did not
  write is replaced. The render leaves a vault note that names no checkout and
  says so; a drain adopts a page or file, there or anywhere, only under step 3's
  pointer rule.
- **Precedence.** A document's `space=` or `folder=` wins over the setting, and
  the setting wins over the default. A pinned `page=` or `file=` wins over both;
  where the setting names a folder, the render names each pinned document, and
  deleting the pin creates the page under the new folder.
- **A channel left out is off.** Only `obsidian`, `notion` and `drive` are
  channels. The inbound `feed` key (sd:3980) is not one: a setting naming only
  a feed is no setting here. A document designated for a channel left out is
  refused for that channel by name, and its other channels still publish. Without
  `obsidian` the render writes no vault copy and says so. The dashboard copy is
  not a channel and always publishes.
- **A pending request for a channel left out stays.** The render names it with
  its path on every run, and drain step 2 refuses it. Delete it by hand, with no
  drain running. Re-enabling the channel takes one render with
  `SD_MIRROR_REQUEUE=1`.
- **A moved folder.** An unpinned document's request names the new container,
  so a carried page or file id is dropped and the drain creates it there; the
  old one stays. The render names each vault copy this checkout left in
  `Briefs/<repo>`, and deletes none.
- **Unknown is not "no setting".** A setting the render cannot read (the hub
  does not answer, a build mismatch, a database older than the column, a value
  the validator refuses, an unresolved restore) holds the vault copy and the
  queue for that render, names the cause, and leaves earlier requests as they
  are. The dashboard copy is still written. A render by hand exits 1; the git
  hook's exits 0.

The render reads the setting once, when it publishes. A change takes effect at
the next render.

### A Notion mirror is private unless it asks for the team

`notion=dict()` mirrors to the folder the `sd.notion_private_folder` setting names.
`notion=dict(team=True)` mirrors to the one `sd.notion_team_folder` names.

The default is private because the two mistakes are not symmetric. A brief the
team cannot see is repaired by adding `team=True` and draining again. A private
brief in a shared team space has already been read by the team, and deleting it
does not undo that. So the direction that happens by accident is the recoverable
one.

`space=` overrides the folder, never the scope: naming a folder says where
inside a space, not which space. A document that must reach the team says so
with `team=True` and nothing else does it.

**Each default folder is a configured Notion page id, not a folder name.** A
name lookup that finds nothing returns an empty result rather than an error, so
a rename would move every default mirror to nowhere and tell no one. A page id
belongs to one Notion account, so no id ships with the pack either. Each
operator sets their own, as workflow database rows (sd:5141):

    sd config set sd.notion_private_folder <notion page id>
    sd config set sd.notion_team_folder <notion page id>

A page URL is accepted in place of a bare id. While the database holds no row
for a setting, its old variable answers (`SD_NOTION_PRIVATE_FOLDER`,
`SD_NOTION_TEAM_FOLDER`); a cleared row reads unset. An unset setting, or one
holding a folder name rather than an id, refuses that mirror and names the
setting. A setting that cannot be read refuses it too and names the cause,
never the variable's value. The render still writes the local copies; only the
outward request is withheld. Nothing falls back to a folder name or to a page
some other account owns.

**A default lands in the repo's own page under that folder**, the way a Drive
default lands in `Briefs/<repo>`. The request carries the repo in `subfolder`,
and the drain creates that page under `space_id` when it is missing. Without
it, every repository's briefs pile into the folder itself, beside the per-repo
pages already there.

A `space=` override is pinned by id too when it is written as a page id or a
page URL, and it replaces the whole container — no `subfolder` is appended to
what the user named. Written as a plain name it cannot be pinned, so the
request says which of the two it is: `resolve: "id"` with the folder in
`space_id`, or `resolve: "name"` with `space_id` empty and the name in `space`.

### A Drive mirror lands beside its siblings

`drive=dict()` mirrors to **`Briefs/<repo>` in My Drive** — the same shape the
vault uses, so the two copies agree on where a brief lives. A reader who knows
where one brief is knows where all of them are, whichever copy they found first.

The drain resolves that path and creates the repo folder when it is missing, so
a new repo publishes without anyone provisioning a folder first. `folder=`
overrides the default with a folder name or id, for a document that belongs
somewhere a reader already looks.

The Drive folder that `sdw.drive_publishing_folder` names is a different thing
and is not this default. That folder is the primary blog's gate: a piece sitting
in it has cleared every publishing check. A brief mirrored into it would read as
approved for publication, which no designation here claims.

The page or file is optional at every destination. Absent, the drain creates it
and writes the id back, which steps 4 and 5 of the drain require. Present, the
drain updates that one rather than leaving a fresh copy behind on every render. The two halves are one mechanism: the write-back is what lets the
next render's request name a page, and naming one is what stops a second copy.

Recording the target makes it machine-readable for the first time. Before this,
the page title and parent lived in no file the kit read, so verifying a mirror
against its source was manual and therefore skipped.

### An item's row in the team experiments databases

An item, not a document, may project as one row in a team experiments database
(sd:3982). The operator designates it with `sd task edit N --experiment yes`
(an experiment) or `--experiment of:M` (a task of experiment M); nothing is
inferred. `row:ID` pins an existing row, `new` drops the pinned one, and `off`
stops the projection and keeps the row. Two settings, beside the Notion folder
ids, name the databases by data source id:

    sd config set sd.notion_experiments <data source id>
    sd config set sd.notion_experiment_tasks <data source id>

They read as the folder settings do, with `SD_NOTION_EXPERIMENTS` and
`SD_NOTION_EXPERIMENT_TASKS` while no row exists. Unset, or holding a name,
`sd-research-kit project` refuses and names the setting. sd writes `Status` on both databases, and `Experiment` and `Due` on a
task, under a guard: a field is written only while its live value is the one
sd last wrote and saw confirmed. A field a person changed is refused, listed in
`yielded`, and named in one note on the item, with the value that hands it
back. The title, `Stage` and `Priority` are written once, at create, with one
body line, `Projected from sd:<n>. sd keeps Status current; edit anything else.`
Everything else on the row is the team's. A property the database lacks, but
the title, is not written, and the plan names it in `skipped`.

`sd-research-kit project` queues one `notion-row` request per designated item
whose row would change, as `sd-item-<n>.notion-row.json` in the same queue. Its
`--plan` form is the guard, in code; the drain below calls it and writes only
what it prints.

## How an outward mirror is updated

A render does not call any outward destination. Rendering runs in a git hook and
in CI, neither of which can reach an MCP server, and the pack holds no
credential for any of them.

So a render enqueues. It writes one sync request per document per destination,
naming the document, its container, the page or file to update, the source
revision, the Markdown itself as `content` and a `fingerprint` -- the digest
of that content and of what this generation of the request would write. The
content travels with the request because the paths the request also names are
mutable: a drain that read the file at `source` published whatever it held by
then and acknowledged a fingerprint of something else. An agent session drains the queue through that
destination's connector and records the result. The request is durable: an
enqueue that is never drained stays visible rather than expiring.

A render queues a document only when that fingerprint differs from the one a
drain last recorded as delivered, and from the one a still-pending request
carries. A receipt beside the queue holds both; `SD_MIRROR_REQUEUE=1` on the
invocation queues every designated document regardless. The queue is read
before the receipt: a pending request that asks for a different generation is
overwritten whatever the receipt says was delivered, because the queue says
what the next drain will write and the source has moved on. A render and the
drain's last step take one lock on the queue, `pending-mirror-syncs.lock`
beside it, so neither can land between the other's read and its write.

A render from a linked worktree queues nothing and writes no vault copy. Its
`docs/dashboard/` is rendered, and the dashboard row it would register is the
repository's own, but publication is the main checkout's: an unmerged branch
that queued under the repository's name would replace the pending request,
inherit the page id a drain recorded for it, and become the canonical mirror.
The refusal names the checkout to run in. `SD_PUBLISH_FROM_WORKTREE=1` on the
invocation is the operator saying, in words, that this branch's content is the
canonical copy.

The main checkout queues a mirror from its default branch only (sd:2019). The
re-render hook fires on a branch switch, so a feature branch or a detached HEAD
would otherwise replace the pending request with unmerged text. Its vault copy
and `docs/dashboard/` are still written, since the next render replaces both.
The default branch is `origin`'s (`origin/HEAD`, then `origin/main` or
`origin/master`; local `main` or `master` without `origin`), never the one
HEAD's tracking remote names, so a branch that tracks a fork is held too.
When no default branch resolves, nothing is queued.
`SD_PUBLISH_FROM_WORKTREE=1` lifts this hold too.

A render with `SD_SKIP_MIRROR` set queues nothing and leaves a pending request
as it was; the value is the reason, and the render reports it. The re-render
hook sets it for a branch checkout that lands behind its upstream, read off the
local tracking ref without a fetch. That tree is about to be replaced by a pull,
and a request queued from it is stale text a drain could publish before the
pull's own render queues the current one.

A render with `SD_MIRROR_DRY_RUN=1` prints each request it would queue, as
`mirror: would queue ...`, and writes no request and no receipt. It writes
no vault copy either, and prints `obsidian: would write ...` for each
(sd:5897). Only the render's own `docs/dashboard/` output is written.

One queue carries every destination, and `bin/sd-status` reports a pending
request as `mirror-sync-pending`. A document designated for two places is two
requests, and either can drain while the other waits.

That queue was once `~/.claude/pending-notion-syncs`, before one queue carried
every destination. A render moves anything left there into the current queue,
and the status report covers both names, so the rename costs no request its
row either side of the move. Neither directory is created by looking, an
emptied one is left standing rather than swept, and a request moves verbatim
rather than being rewritten to a schema it was not written under.

This makes the mirror reliable without giving a hook a long-lived write
credential to a shared space. It also keeps the outward-facing step in a place
where a person is present.

Draining is six steps per request, and the order matters:

1. Read the request. It names the destination, the document, its `content`
   -- the Markdown as it stood when the request was queued, and what step 3
   mirrors -- the paths its source, rendered page and vault copy sit at, its
   `source_url`, the GitHub URL of the source read from the repository's
   `origin` or `null` where origin names no GitHub repository, its
   container, the page or file to update, the revision the render was made
   from, the `item` its entry names if any, and its `fingerprint`, which
   step 6 hands back. A Notion request
   also names its `scope`,
   `private` or `team`, how its container was resolved — `resolve: "id"` with
   the folder's page id in `space_id`, or `resolve: "name"` with the folder's
   name in `space` — and `subfolder`, the page under that container the
   document belongs in. A Notion request whose document holds an Archify
   diagram also names its `image`: the PNG to upload in place of the
   `{{archify-image}}` line its `content` holds.
2. Check the document's handling restrictions first. One that may not be shared
   externally is not mirrored to a shared destination, and designating it did
   not lift that. Report it and leave the request in place. Then read the
   channels setting of the `repo` the request names, with
   `bin/sd-db repo channels <repo>`. Read only its `obsidian`, `notion` and
   `drive` keys; a setting naming none of them, such as one holding only a
   `feed`, is no setting. A setting that leaves out the request's
   destination refuses it: report it and leave it in place. A destination name
   is not a channel name: a `notion-row` request reads the channel `notion`. A read that exits
   non-zero, an unresolved restore among the causes, leaves every request of
   that repo unwritten, with or without `item`: unknown is not "no
   restriction". `none` is no setting, and so is a
   machine with no workflow database (no `~/.local/share/sd/sd.db` and no
   `~/.config/sd/hub.json`), where the read is skipped.
3. Mirror the document to the named container, in that destination's **native
   format**, updating the page or file the request names rather than creating a
   second one:

   - `notion` — the Notion connector, as Notion blocks. With `resolve: "id"`,
     write under the page `space_id` names and never search for that folder by
     name. With `resolve: "name"`, look the folder up by the name in `space`,
     and treat a lookup that finds nothing as a failure to report rather than
     an empty result — do not create a folder to make the name resolve. Where
     the request names a `subfolder`, the document goes in that page under the
     container and not in the container itself; create it when it is missing,
     the way a Drive drain creates `Briefs/<repo>`. Honour `scope` either way:
     a `private` request goes to your private space and never to the team
     space, whatever the folder is called. A request with an `image` places
     it as "A Notion request with an image" says, below.
   - `drive` — the Google Workspace connector, as a native Google Doc. Not an
     uploaded `.md` or `.html` file: the point of the mirror is that someone can
     read and comment on it in place. Import the Markdown so headings, tables
     and code blocks survive. A `folder` of the form `Briefs/<repo>` resolves
     under My Drive's root; create the repo folder when it is missing, and do
     not adopt a `Briefs` folder found somewhere else.

     **An update marks what changed (sd:7252).** Where the request names a
     `file` that is not in the trash, the reader has seen that copy, so mark
     the paragraphs that differ from it. Read the Doc with
     `get_doc_as_markdown` (`comment_mode: "none"`) into a file, write
     `content` to a `.md` file, and run
     `sd-research-kit highlight --previous <read-back.md> --since "<time>" <content.md> <out.html>`.
     `<time>` is the file's last-modified time as Drive reports it, in local
     time with its zone. Then replace the body with `update_drive_file`,
     `file_path` the `<out.html>` and `source_format: "html"`: an HTML import
     keeps headings, tables and code blocks as the Markdown one does. Each
     changed paragraph, list item, heading, table cell and code block gets a
     yellow background. One note opens the Doc: the count, how many earlier
     paragraphs no longer appear, and their sections, or `No changes since`.
     The read-back holds a reader's own edits, so they count as seen. A read
     that fails, or a `highlight` that exits non-zero, leaves the request: an
     unknown baseline would mark nothing or everything. Three writes carry
     no mark. A create has nothing to compare against. A Notion mirror is out
     of scope for now. And a Drive publishing folder is not a
     mirror: a blog publishes from it, so a review mark there would ship.
     The headless Publish now drain gets `get_doc_as_markdown`,
     `update_drive_file`, `highlight` and Write to `sd-<run id>-*` names
     at the top of the workspace-mcp attachments folder only for a run
     that holds such an update, and its prompt names those paths.

   - `notion-row` -- the Notion connector, one database row. Run
     `sd-research-kit project` first, at the start of the drain, and drain
     experiments before tasks: a task's plan refuses while its experiment has
     no row. For each request, fetch the database named by `database` (its
     data source id) and, where the request names a `row`, that page. Write
     what you fetched to a file as JSON: `database`, the data source id;
     `schema`, each property's `type` and, for a select, its `options`; and
     `row`, the page's `id`, `parent` (its data source id) and `properties`.
     Hand each property whole, exactly as Notion holds it: a select's option
     name, untrimmed, or null; a date's whole value, never its start alone,
     as `{"start", "end", "time_zone"}` with all three keys, or null; a
     relation's whole value, never a page of it, as `{"ids", "has_more"}`
     with every page id and `has_more` false, even when empty. A property
     left out or cut short is refused, never read as empty. Then run
     `sd-research-kit project --plan <request> --live <file>`. A pinned row
     that is not found or is in the trash takes `--missing` instead of
     `--live`; a fetch that fails any other way stops this request and calls
     no plan, because a failed read is unknown, not missing. A plan that
     answers `search:` found an earlier create that may have landed: search
     the database for pages whose body opens with the pointer
     `Projected from sd:<n>.`, whatever their title, and plan again with each
     as a `candidates` entry shaped like `row`, an empty list for none. One
     match is adopted; none or two ask, since a create may have landed and
     search can lag it, and `--experiment new` is the answer that none did.
     The plan prints JSON: `action` `create` with `properties` and the
     `body` line, or `update` with the `row` and the `properties` to write,
     `null` clearing one. A create is written only after its mark:
     `sd-research-kit project --writing <request>` records on the item that
     the write follows, and the create call comes straight after it, in one
     call; never create without the mark, and never mark without creating
     right after. A plan that printed a create and was not marked left the
     item as nothing written, so a dry run leaves no trace to recover from,
     and the next plan prints the create again; a mark left behind is what
     the next plan searches for (sd:4742). Write exactly what the plan
     printed, in one call, straight after the plan; anything that comes
     between them means fetch and plan again. A plan repeated under one
     request is the same answer, however often: before the mark, the same
     create; after it, `search:`. The plan the write follows is the one that
     counts, not the first. An
     update with no properties writes nothing and still goes to step 6. A
     plan that refuses exits 1 and records nothing: report it and leave the
     request. A refusal that ends with `Option:` lines is a question: file it
     on the item with `sd task note <n> '<text>' --kind question`, the
     pick marked ` (Recommended)` (the note refuses one with none), unless an
     open question already says the same, and leave the request. Never
     create a row the plan did not print, and never edit the schema or add
     a select option.

   Never convert a document into a format its destination does not read
   natively. The source for every mirror is the request's `content`: the
   Markdown as it stood when the request was queued, and what its
   `fingerprint` describes. Not the rendered HTML, and not the file at
   `source`, which may have moved on since -- a drain that reads the path
   publishes what nobody queued and acknowledges a fingerprint of something
   else. `source` and `markdown` say where the text came from and where the
   vault copy sits. The render puts each soft-wrapped paragraph and list
   item of `content` on one line, since a Notion write turns a single
   newline into a `<br />` (sd:5872); write the lines as they stand and
   do not re-wrap them.

   **The pointer line links `source_url`, verbatim.** Where it is `null` or
   absent, the pointer names the `source` path. Never compose a GitHub URL
   from the repository name, a remembered owner or a template: the render
   read the owner from `origin`, and a drain that composed its own pointed
   a moved repository's pages at the wrong owner for six days (sd:1999). A
   request written before `source_url` existed gets it on the next render,
   because the field is part of the `fingerprint`. A request written before `content`
   existed names only its paths; the next render rewrites it with the field,
   and a drain that reaches one first mirrors the file at `source`, as the
   contract then said.

   **A relative link has one rendering (sd:2243).** A link to a target that
   is mirrored to the same destination uses that target's recorded page URL.
   A link to an unmirrored target becomes its repo-relative path in plain
   text. Never write a `file:///` URL or an absolute path: no other reader
   can open one, and drains that improvised gave one page set two link shapes.

   **Look before creating.** Where the request names no page or file, search
   the named container for one already carrying this document's title, and
   adopt the match instead of creating a second. A drain that creates
   unconditionally makes a duplicate out of every interrupted earlier drain.
   Two matches is an ambiguity, not a choice: report it and leave the request
   in place. Adopt a match only when its pointer line names this document's
   `source_url` or `source`. A container can hold the operator's own pages,
   and whether the channels setting named it can change after the request
   was queued, so this holds in every container. Any other match is not
   this tool's: report it and leave the request in place.
   Steps 4 and 5 do not apply to a row: the plan records `creating` on the
   item before the create, and step 6 records the row id there. A request
   never holds a row id.
4. Write the id into the request file, the moment the destination returns it.
   The request is the durable record, because it is the one file the drain is
   certain it can write and the one the next drain is certain to read. A
   request that already named a page or file created nothing, so steps 4 and 5
   have nothing to record.

   This step exists for the gap between creating a page and recording it. A
   created page whose id is written nowhere is the duplicate this procedure
   exists to prevent: the next render enqueues a create again, and the next
   drain makes a second copy. Both copies carry the same title and the same
   content, so nothing reads as wrong and no reader can tell which is stale.

   **A re-render carries a recorded id forward rather than overwriting it.**
   A render rewrites the request for every designated document, and the
   re-render hook renders on every commit, merge and checkout that touches a
   document — so without this, any of the three landing between step 4 and
   step 5 erased the only record of the created page, and the gap step 4
   exists to close reopened by the one route neither step watches. An id is
   carried only while the request still names the same document, in the same
   repository, in the same container. A page id belongs to the folder it was
   created under, so a designation moved to another folder, or to the other
   Notion scope, drops it and the drain resolves the new container afresh. The
   document and the repository are compared because the queue filename says
   neither: it is keyed on a repository's basename, and two checkouts of that
   name may designate a document of the same name into the same default
   folder. How the container was *spelled* is not compared — a folder written
   as a page URL and the same folder written as its bare id are one container
   — and neither is the document's title, since a renamed document keeps its
   page.
5. Write the id into the document's designation, as `page=` for Notion and
   `file=` for Drive. In a research repo that designation is a key on the
   document's `research.conf.py` entry, and it is what the *next render* reads
   — step 4 only carries the id as far as the current request. A routine
   brief's request (`source` `sd:<item>`) and a writing piece's file's
   (`source` `sd:<item>/<file>`) skip this step: step 6 records the
   URL and its container, and the next render carries it into that container.

   A designation written as the bare `True` shorthand is amended into the dict
   that records the id: `notion=True` becomes `notion=dict(page="<id>")`, which
   is the same designation with the same private scope. A designation written
   `None` or `False` is switched off, so it enqueues nothing, and no drain ever
   reaches it. Never add a key to a document that designates no mirror.

   If this write fails — an unwritable file, a config the drain cannot amend —
   report it and stop, leaving the request in place with its id. The queue then
   still says work is owed, and the next drain updates the page it names rather
   than creating one.
6. Run `sd-research-kit delivered <request file name> <fingerprint>` with
   `--url <url>`, giving the fingerprint read in step 1 and the URL of the
   page or file step 3 wrote, once steps 4 and 5 have both succeeded. A
   request that names an `item` needs `--url`; without it the command writes
   nothing and exits 2. For the generation it was given, it records the URL
   on the item first, then that fingerprint and URL beside the queue, and
   removes the request only while the file is still that generation. A newer
   generation's item gets nothing: the next drain records with it. Any step
   that does not finish -- the item record refused (a restore open, an
   unknown item, no database reachable), the receipt write, the removal --
   leaves the request, prints the reason and exits 1; run step 6 again once
   the cause is fixed, and an equal URL adds no second note. A render that queued
   newer content while steps 2 to 5 ran leaves its request in place, and the
   next drain writes it. Do not delete the file by hand: that acknowledged
   whatever the file held at that moment, which after such a render was
   content the drain had not written -- and the next render, reading the
   empty slot as delivered, then never queued it again. A request left behind
   says the sync still owes work, which is the safe thing for it to say if
   any earlier step half-finished.

   For a `notion-row` request, `--url` is the row's page URL and step 6
   confirms the plan of the generation it is given: it records the row id
   and moves the planned values to `written`, refusing a plan the item's
   designation has moved past since. A re-run after a failed receipt answers
   `unchanged`. A field a person holds keeps the item queued: each
   `project` queues it again, so the next drain sees a hand-back.

**A Notion request with an image (sd:6066).** The page shows the Archify
diagram as an image, and no other diagram: the render put the
`{{archify-image}}` line where the document's `diagram` div was, dropped the
plain `text` block after it, and drew the SVG as the PNG `image.path` names.
Drive and the served page keep the document as written. On every write of
the page, a redrain included, upload the image again: call
`notion-create-file-upload` with a `.png` filename, then run
`sd-research-kit upload <request file name> <upload_url> <authorization>`
with the `upload_url` and the `authorization` header it returned. It sends
the request's own PNG to that address only, and prints the line to write,
`<image source="file-upload://<id>">caption</image>`. Write `content` with
that line in place of the `{{archify-image}}` line. An upload that exits
non-zero is a failure, not a missing image: leave the request, and never
write the page with the marker in it or the image left out. An upload
Notion never saw attached expires on its own.

**What each failure leaves behind.** This is stated, not implied, because a
procedure whose failure mode is the bug it prevents is not a fix.

| Interrupted after | State | What the next drain does |
| --- | --- | --- |
| an image's upload, before the page write | an unattached upload, which Notion expires | uploads again and writes the page |
| step 3, before step 4 | a created page, its id recorded nowhere | adopts it by title under step 3 and carries on |
| step 4, before step 5 | id in the request, not in the designation | updates that page, retries step 5 |
| step 5, before step 6 | id recorded in both | updates that page, runs step 6 |
| step 6's item record, before its receipt | URL on the item, request in place | updates that page, runs step 6; the record answers `unchanged` |
| a render during steps 2–5 | a newer request under the same name | step 6 leaves it; the next drain writes it |
| a row's plan, before its write | `creating` (`planned`) or `writing` on the item | plans again: the same create, or the update from the row as it is |
| a create's mark, before `delivered` | `creating` marked `writing` | searches by the pointer, adopts one match and asks about none or two |
| a row's write, before step 6 | values Notion took, unconfirmed | refuses those fields as possibly sd's own write, until a person hands them back |
| a trash, before step 6 | a copy in the trash, its URL on the item | finds it in the trash, nothing left to trash, runs step 6 |

Every row survives a render in between, because a render carries a recorded id
forward under step 4 rather than overwriting the request.

One state is not covered: a page created in step 3 whose title then changes
before the next drain runs, *and* whose id reached neither the request nor the
designation. Nothing matches it, so the next drain creates a second page and
the first is orphaned. That is the first row of the table and a rename
together; a rename alone is covered, since a carried id finds the page
whatever it is now called. A destination that creates a page and returns an
error instead of an id lands in the first row and recovers there — by title,
so rename a mirrored document whose drain errored and check its mirror by
hand.

### An unticked destination moves its copy to the trash

Unticking a destination on the dashboard (sd:5489) queues a trash request in the
same slot, then drops the designation (sd:5492). The request carries
`action: "trash"`, the copy's `page` or `file` id and its `url`, as far as the
designation, the pending request, the item and the receipt record them, and no
`content`. Nothing queued and nothing delivered queues nothing. A drain handles
it in place of steps 3 to 5:

- Step 2 is the same: a refusal leaves the request.
- Move the one copy it names to the trash, recoverably: `notion` with
  `notion-delete-pages` and `user_confirmed: true` (the untick asked for that
  page), `drive` with `update_drive_file` and `trashed: true`. With no id, take
  it from `url`; with neither, look in the request's container for the copy
  whose pointer names its `source`, as step 3 does. None found, or a copy
  already in the trash, is nothing left to trash; two is an ambiguity: report
  it and leave the request. A read that fails any other way leaves it too.
- Never trash anything else: no other page, no folder, no parent.
- Step 6 without `--url`: `delivered` takes the URL off the item and the
  receipt, then removes the request while it is that generation.

A re-tick before the drain replaces the trash request with a mirror request
that carries no id from it; that drain adopts the live copy by its pointer.
A page or file a mirror request names that is in the trash is not updated:
step 3 treats it as no page.

Never drain a request into a container it does not resolve to, never create a
page or file outside that container -- the one the document names, or the
configured default for its destination when the document names none -- and
never drain a request to a destination other than the one it names.

## Order

Redaction and source standards run before publication, not after. A document
that fails `source-standards.md` is not published anywhere, including locally.
Publication never launders an unverified claim into a page that looks finished.
