---
name: sd-publish-drive
description: Use when the user says a ready piece in a writing content repository should ship, to run its final gates, attach a tip, and push it to the Drive publishing folder; or when the live URL comes back.
disable-model-invocation: true
---

# sd-publish-drive

Ship one `ready` piece: final gates, one approved tip, one confirmed push to the Drive publishing folder, then the stage `published`.
Run from the root of the writing content repository. `pack` below means `python3 scripts/pack.py`.
Read `references/publication.md` in full before a claim or push: it owns the claim, the connector actions, the readback and the recovery.
The repository's `CLAUDE.md` names the primary blog that reads the publishing folder, and its `--target` name, written `<primary>` below.
A second, shorter version for a personal blog is separate work, after the live URL exists.

It has no `bin/` command: the steps below are the procedure, and `scripts/pack.py` dispatches each connector action.

## Workflow

1. `sd writing get --piece <year>/<slug> --json`. Refuse unless `writing.stage` is `ready`; send it back to `sd-draft` and say why.
2. `sd writing readiness --piece <year>/<slug> --json` must show current passing fact-check and adversarial records. Build the preview with `pack pieces preflight --piece <year>/<slug> --out <scratch dir>/preview.html --json`.
3. Read `research.md`, `profile/brand-voice/VOICE.md` and `profile/personality-profile/PROFILE.md`.
4. Reuse a current passing fact-check record. Re-run it when the draft, research or report changed, the way `sd-draft` step 7 does, and record it with `sd writing gate`. A contradicted or unverified claim blocks until fixed or accepted by the user.
5. Then spawn three blind sub-agents in one message, each returning a verdict, line-tied findings and fixes:
   - **Style**: the draft, `VOICE.md`, and the `sd-humanizer` and `no-ai-slop` (Detect mode) pattern lists. Audit only.
   - **Appeal**: the draft only. Hook, pacing, where a reader leaves, the ending.
   - **Relevance**: the draft, the thesis in `research.md`, and `PROFILE.md`. On thesis, and inside the profile's boundaries.
6. A real problem stops the run before any external write. Report it and suggest `sd-draft`, or ask whether to publish with the findings accepted.
7. Check `## Draft` for `TODO` or placeholder text.
8. **Attach one tip.** `sd store list sdw.tip --status approved`; only `approved` counts. Skip a tip that `writing.metadata.tip` on another pushed piece already holds. Pick the best topic fit, not the top score, and say why. `pack tips attach --piece <year>/<slug> --title "<tip title>"` re-checks the status. Render the preview again. With no approved tip, publish without one and say so; do not fall back to `inbox` or write a tip inline.
9. **Confirm with the user**: piece, word count, tip, account, publishing folder, and the final preview. Approval for that exact result stands.
10. Read `sdw.google_account`, `sdw.drive_writing_folder` and `sdw.drive_publishing_folder` with `sd config get`. Claim, import once, verify native content, move the same document ID into the publishing folder, and reconcile, all per `references/publication.md`. A lost response needs readback, not a blind retry.
11. If the piece has `obsidian_source`, add `- Published copy: <Drive URL>` to the idea note's `Drive docs` section with `sd store set sdw.blog-idea "<idea title>" --section-file 'Drive docs=<file>'`, keeping its old lines.

Report: title, word count, date, the gate findings, no open TODOs, the Drive URL, which tip and why, and that the live URL, the idea note and the tip still wait on the live URL.

## When the live URL comes back

The primary blog publishes from the Drive doc on its own schedule. When the user hands over the live URL, in one pass:

1. `pack pieces set-published-url --piece <year>/<slug> --target <primary> --url "<live URL>"`.
2. With a tip: `sd store set sdw.tip "<tip title>" --field status=published --field url="<live URL>" --field used-by=<year>/<slug>`.
3. With `obsidian_source`: `sd store set sdw.blog-idea "<idea title>" --field status=published --field url="<live URL>"`. The idea must be at `drafting`. Set the note's `url` to the live primary URL, not the Drive doc.
4. Only now may a separate step write a personal-blog version that links to it.

Never guess or build the live URL from a title or slug. A plausible 404 reads as done; a null reads as not live yet.

## If a published piece has to come back

`sd writing stage --piece <year>/<slug> --stage <earlier> --correct --reason "<why>"` records the correction with its reason. It does not reopen the drift gate. Then say which of `published_urls`, the idea note and the tip still point at the retracted piece.

## Safety rules

- The only external writes are the one confirmed native import and the same-ID move into the publishing folder. No deletes, no sharing changes, no direct import into the publishing folder, no other destination.
- Never run `sd-research-kit highlight` on this copy: the primary blog publishes from it, so a review change mark would ship.
- A tip is used once. A tip at `inbox` never ships.
- Nothing is sent to a personal blog, a schedule, social media or email.
