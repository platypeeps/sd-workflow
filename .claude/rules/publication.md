---
paths:
  - "skills/**"
  - "bin/sd_research*.py"
---

# Where a finished document goes

- `skills/_shared/references/publication-contract.md` is the policy for this pack and everything installed from it; read it before you decide where a document goes.
- Publish every finished document by default as HTML on the dashboard Documents tab from `docs/dashboard/`; Markdown goes to the vault under `Briefs/<repo>/` when its entry designates `obsidian=dict()`, or for the vault's own repo.
- A skill that ends by naming a path in a build directory has not finished: a document nobody can find was not delivered.
- Working state (ledgers, receipts, handoff packets, monitor state) is not a finished document and does not publish.
- The vault copy is the document; each other copy is a duplicate in its destination's native format: HTML, Notion blocks or a native Google Doc.
- Notion and Google Drive are opt-in mirrors, per document, in its `notion=` or `drive=` key in `research.conf.py`, or for a routine brief in its item's `fields.publish`; nothing infers a target, and both keys mean two mirrors.
- A render enqueues each sync under `~/.claude/pending-mirror-syncs/` and an agent session drains it: git hooks and CI cannot reach an MCP server.
- A Notion mirror goes to `sd.notion_private_folder` unless `notion=dict(team=True)` sends it to `sd.notion_team_folder`: a private brief is fixed by draining again, a leaked one is already read.
- Both folder settings are workflow database rows holding a Notion page id, so a rename moves nothing; an unset one refuses the mirror; the document lands in the repo's own page there.
- A published page carries its own resources: the Documents tab serves `default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:`, so a link, script or remote image fails silently.
- Embed fonts as the renderer does (`bin/sd_research_fonts.py`); the theme control is the one inline script, and blocked, the page follows the OS scheme and hides it.
- A new document-producing skill cites `references/publication-contract.md` in its `SKILL.md`: the contract reaches an installed skill only that way, and the installer warns only about a citation it cannot resolve.
