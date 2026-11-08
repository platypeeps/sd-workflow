# Writing

Writing is at `/writing` (sd:2125): the design's pipeline board, one column
per stage with four gate lamps per piece, a focused editor and the strip of
gate counts. `GET /api/writing` (`writing_screen.document`) gives every
piece, parked ones too: its stage, gate problems, next and correction stages
and publication claims from `writing.piece_state`, each file's size from
`writing.piece_files`, and its draft up to `DRAFT_CHARS`, cut at a paragraph
and marked `draft_cut` past that. The repository goes out by its folder name
only. Show draft (pack sd:7242), on each piece's row menu, the palette and
Details, selects the piece and opens its draft in the read-only editor.
Stage asks first and posts
`POST /api/items/<id>/stage` with the piece's revision; it has no Undo,
because going back is a correction. Correct asks for a reason and posts the
same route with `correct`. Park and Revive post `/park` and `/revive` and
undo each other. Publish and Capture are copy only. Refresh review posts
`/api/adversarial` with kind `piece`: codex reads the draft in the
background and rewrites `adversarial.md` (pack sd:6699). Once that file's
digest stamp names the current draft (pack sd:6977; file times do not
count), Apply review asks first and posts `/fix` with
`apply: adversarial`: claude edits the draft for each finding or rejects it
with a reason, records each with `sd writing reconcile`, and leaves the edit
uncommitted. It publishes nothing. The page reads through
the shell's reader, `read.js`. The old list was retired in sd:3402. The views are Personal, then one per
`dashboard.realms` realm (sd:5064); there is no All view (sd:7213). A piece
sits in its realm's view, else in Personal, which also takes a piece whose
realm was not read or has no view; an old `#view=all` link opens Personal.

Grill (pack sd:6700) asks first, then posts Fix with `{"grill": "ask"}`:
claude in herdr reads the draft and writes 5 to 10 question notes on the
piece. `/api/writing` gives each piece `grill`, its open questions and the
count of its rulings, and Details walks the first open question with a
button per option or a box for words; each answer posts
`/api/decisions/answer`, as Decide does. Apply answers, once every question
is answered, posts Fix with `{"grill": "apply"}`: claude edits the draft to
follow each ruling and leaves the edit uncommitted.

Bulk Decline (sd:8014) sits on the Ideas and Tips columns. Its
button names what the defaults move: "Decline N below score 6 older
than 30 days". It asks first, takes another score and age, and posts the
rows it names, with their revisions, to `/api/triage/decline`
(`triage.decline`). Only the press declines; no job does. The Declined
filter, off until pressed, lists each declined row with when and by
which control. Declined rows have no Restore yet: the vault rules give
declined no move out.

The Ideas column (pack sd:7244) is `ideas` in the same read: the
`sdw.blog-idea` rows at stage inbox, read with `records.list_rows`, highest
score first. It shows `IDEAS` of them with the inbox count in its header and
the strip, and links a title only to an http address. A failed read leaves
the column unknown with its reason; the pieces still read.

Each gate row in Details has a refresh and a view icon (pack sd:7242).
Draft's refresh reads the pieces again and its view is Show draft.
Adversarial's refresh is Refresh review. No route runs research or a
fact-check, so their refresh copies the line that does, to run in the
content repository. A report's view opens it in the editor's evidence
pane, read only from `GET /api/writing/<id>/<gate>`
(`writing_screen.report`) and cut at `REPORT_CHARS`; a missing report's
view is off and says so.

Not read yet, so drawn as a gap: a draft save. The editor is read only,
since no route writes a draft.
