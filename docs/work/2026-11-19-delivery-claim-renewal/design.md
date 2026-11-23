---
title: A hand prepare renews a delivery claim the item outgrew
created: 2026-11-19
item: sd:9268
branch: build/sd-1636
---

# Design — delivery-claim-renewal

## Problem

A delivery claim records the item's scope (title, body, path) and the
acceptance evidence. When the item changed after the claim, merge
refused, and no command renewed the claim. A lane prepare passes the
enqueue's evidence on every retry, so it must never renew one.

## Approach

A prepare by hand with `--deliver` renews the claim: it saves the new
scope and evidence. A lane prepare never renews (sd:9335). Once a pull
request exists, prepare saves its title, body, claim and closing set as
`pending`; `ready_to_send` publishes them.

The review class: what counts as the evidence a delivery stands on,
and when a stored claim, body or flag goes stale. Two rules answer it.

- Evidence is a row's evidence, result and waiver (`evidence_of`),
  with text folded (NFKC, spacing). A comment, a waiving note's body
  or a criterion's name proves nothing. A renewal whose rows prove
  nothing new refuses.
- A body's basis is the words it was made from, recorded, never
  inferred. A lane prepare appends a Screenshots section per run, so
  the entry's body is read from its queue row (`body_basis`), and the
  receipt maps each published text to its basis (`body_bases`).
- Equal content is no record. A body file's words are its text, even
  when an earlier prepare published that text. A row is a prepare's
  own when the row's holder, the runner, is its parent.
- A missing record is never answered by a guess. A text no prepare
  recorded has no basis; the entry then proceeds only when an ended
  lane entry of the same words lists the PNGs that make exactly the
  published body (`drawn_by_lane`). A lane prepare no row vouches for
  records no words.
- The published closing set is bound to the head it was read at.
  Reconcile refuses to deliver or close a merge of another head.
- A `pending` set means the prepare did not finish. Merge refuses
  `prepare_required` while one waits, in every mode (`merge_scope`).
  The Copilot request is outside the set, so an equal set merges only
  with `--abandon-copilot-review`, which needs a request receipt.
- A refusal's printed way out must clear it, and must not pass its own
  guard by another door. An explicit Copilot request stays owed on the
  receipt (`copilot_review_owed`) until a request is recorded or
  `--copilot-review skip` withdraws it, so the retry merge advises asks
  again. A hand delivery at the merge commit answers a reconcile of an
  unbound closing set: it binds an empty set to the merged head, and
  reconcile closes no item it could not vouch for.

Rejected: check the closing head for `--no-item` merges. It covers one
mode; a pending body can wait in any mode.

## Non-goals

- The sticky `renewed` mark (pack sd:9580). The operator ruled it
  ships after this item merges (note #64736). Its old patch no longer
  applies; sd:9580 rebuilds on this.
- Renewal from phase `merge_dispatch`: prepare routes it to reconcile.

## Failure table

Each row names a write or read of the claim, the evidence, the body or
the `renewed` mark in `bin/sd-ship`. Tests are in
`tests/test_sd_ship.py` unless named otherwise.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| first prepare save | title, body, claim, closing set, active | lint, review, push or Copilot fails | no `pull_request` saved, so merge refuses; prepare again | `test_a_first_prepare_whose_copilot_request_fails_records_no_pull_request` |
| reprepare save | the same fields as `pending` | a later step fails | merge refuses `prepare_required`; the next prepare overwrites `pending` | `test_a_failed_renewal_never_squashes_its_unpublished_body` |
| renewal claim | new scope and evidence in `pending` | the evidence is the old claim's, a comment apart | refuses before any save | `test_a_renewal_whose_rows_differ_only_outside_the_evidence_refuses` |
| renewal by the lane | none | a retry carries pre-change evidence | the lane never renews; `assert_scope` refuses | `test_a_lane_prepare_never_renews_a_claim_the_item_outgrew`, `test_an_orphaned_lane_prepare_never_renews` |
| rebase closing set | `closing.head` moves to the rebased head | a later step fails | in `pending` while a PR exists; merge refuses a stale head | `test_a_closing_set_read_at_another_head_refuses_the_merge`, `test_a_reconcile_after_a_failed_renewal_reads_the_published_closing_set` |
| guest body strip | body without `Work:` in `pending` | a later step fails | guest mode has no merge authority | `test_written_guest_mode_has_no_merge_authority` |
| push | active `head`, remote branch | Copilot or a later step fails after the push | merge refuses `prepare_required`, `--no-item` too | `test_a_reprepare_that_does_not_publish_its_body_merges_nothing` (`tests/test_sd_ship_no_item_publication.py`) |
| create pull request | `create_dispatched`, then the PR | the answer is lost | the retry reuses the one open PR | `test_lost_create_response_finds_one_matching_pr` |
| publish | `ready_to_send` promotes `pending` in one save | the process stops before it | as the push row | `test_a_failed_copilot_request_never_publishes_a_renewal` |
| merge dispatch | `merge_message` from the published body | the answer is lost | reconcile reads the PR; no second merge | `test_lost_merge_response_reconciles_without_a_second_merge` |
| merge read of scope | none | the item changed after the claim | refuses; nothing merged; renew by hand | `test_a_scope_change_before_the_merge_says_nothing_merged` |
| reconcile read of scope | `merged`, the closing set | the item changed after the merge | refuses; the advice delivers by hand | `test_a_scope_change_after_the_merge_gives_the_merged_advice` |
| renewal evidence text | none | old evidence with a trailing space, line break or renamed criterion | folded the same; refuses before any save | `test_a_renewal_whose_evidence_differs_only_in_spacing_or_its_criterion_name_refuses` |
| reconcile of another head | `merged` recorded | a reprepare pushed a tip, then refused; GitHub merged that tip | refuses `closing_set_stale`; nothing delivered or closed; deliver by hand | `test_a_reconcile_of_a_head_the_published_closing_set_was_not_read_at_refuses` |
| reconcile after that hand delivery | `closing` bound to the merged head, no items | the refusal had no hand-delivery escape and no prepare runs after a merge, so it never cleared: no owed close paid, no cleanup | the claimed item delivered at the commit is the answer; reconcile closes no other item and settles | `test_a_hand_delivery_settles_a_reconcile_of_an_unbound_closing_set` |
| hand prepare under a lane row | none | a running row with no lane mark counts it as the lane's | refuses `lane_entry_running`, naming the row; the entry ends, or `sd-ship lane cancel` releases one whose runner is gone; then the prepare by hand renews | `test_a_hand_prepare_held_by_a_running_lane_row_names_that_row` |
| explicit Copilot request | `copilot_review_owed` set before any step that can fail | GitHub refuses the request; the retry merge advises ran without the flag, chose `not_selected` and published without the review, on a first prepare too | the retry asks again; a recorded request pays it and lifts the suppression it was to lift; `--copilot-review skip` withdraws it | `test_a_plain_retry_after_a_failed_explicit_copilot_request_still_owes_it`, `test_a_first_prepare_whose_explicit_copilot_request_failed_still_owes_it`, `test_an_owed_copilot_request_outlasts_the_suppression_it_was_to_lift`, `test_an_explicit_skip_withdraws_an_owed_copilot_request` |
| scope refusal before a merge | none | the printed renewal was never followed in a test | the printed command, run by hand with fresh evidence, renews; merge lands | `test_a_scope_change_before_the_merge_says_nothing_merged` |
| body refusal, the entry's body by hand | `body_bases` maps the entry's body to itself | that way out was never followed in a test | a prepare by hand with the entry's body, then a retry of the entry, proceeds | `test_a_lane_retry_with_the_body_a_renewal_replaced_refuses` |
| untied lane prepare, the retry | none | the retry it names was never followed in a test | once the running entry ended, a retried entry proceeds | `test_a_lane_prepare_whose_body_is_not_its_running_entrys_refuses` |
| lane retry after a renewal | none | old items, evidence or body | refuses `delivery_claim_renewed` before any save | `test_a_lane_retry_of_an_entry_older_than_a_renewal_refuses`, `test_a_lane_retry_with_the_items_a_renewal_dropped_refuses`, `test_a_lane_retry_with_the_body_a_renewal_replaced_refuses` |
| lane prepare on a renewed claim | none | the published body mixes the entry's words with the lane's Screenshots section, drawn per run, and the pull request keeps the body it was opened with | the entry's body is read from its running queue row and tied to this prepare's body; `body_bases` records the words each published text was made from; no text is read to guess | `test_a_stale_entry_cannot_restore_the_screenshots_a_renewal_removed`, `test_a_retry_of_the_renewals_body_proceeds_whatever_the_lane_draws`, `test_a_renewal_of_a_body_the_lane_published_keeps_the_entrys_basis`, `test_a_lane_body_differs_from_a_renewals_in_any_byte_but_the_lanes_own_section` |
| lane prepare that outlived its runner | none | it reads a newer entry's row as its own | a row another runner holds, or an untied body, refuses `lane_unknown` before any save | `test_a_lane_prepare_whose_body_is_not_its_running_entrys_refuses`, `test_a_lane_prepare_its_runner_does_not_hold_the_entry_of_refuses` |
| renewal with a body file of a published text | `body_bases` maps the text to itself | the text took the earlier publication's basis, so the entry that drew it retried and removed the section | a body file is its own words; the entry refuses `delivery_claim_renewed` | `test_an_explicit_renewal_of_a_published_text_holds_against_the_entry_that_drew_it` |
| renewal that takes a body no prepare recorded | `body_bases` maps the text to no basis | a receipt older than `body_bases` refused its own unchanged entry; a section edited on GitHub read as the lane's | the entry proceeds only when a lane row's `shots` make exactly the published body; any other refuses `delivery_claim_renewed` | `test_an_unchanged_retry_proceeds_on_a_receipt_older_than_the_body_record`, `test_a_section_edited_on_the_pull_request_is_not_the_lanes` |
| lane prepare no row vouches for | `body_bases` maps its text to no basis | its body file, the lane's section in it, was recorded as its own words | nothing is recorded; the lane's rows answer later | `test_a_lane_body_no_row_vouched_for_is_recorded_as_nobodys_words` |
| enqueue of the published body | none | the published text's basis is the first entry's words, so the copy compared unequal | an entry whose body is the published body proceeds | `test_an_entry_enqueued_with_the_published_body_proceeds` |
| merge after a failed Copilot request | none | the pending set equals the published one, and no request was recorded | merge refuses `prepare_required`; `--abandon-copilot-review` needs a request receipt | `test_a_merge_after_a_failed_explicit_copilot_request_refuses`, `test_later_head_can_abandon_the_latest_request_after_dispatch_failure` |
| `renewed` mark | kept on every later claim | a new enqueue with new evidence refuses too | sd:9580, next PR; no fix here (ruling #64736) | `test_a_lane_retry_of_an_entry_older_than_a_renewal_refuses` |

## Acceptance criteria

- [ ] `tests.test_sd_ship` renewal, scope, closing and Copilot groups
  pass with 0 failures.
- [ ] `tests.test_sd_ship_no_item_publication` passes with 0 failures.

## Rejected findings

- Round 7, two findings on `refuse_other_body`: a receipt renewed
  before the upgrade holds a basis the new code reads wrongly. No such
  receipt exists. Only this branch writes the `renewed` mark and
  `body_bases`:
  `git grep -n "renewed\|body_bases" origin/main -- bin/sd-ship`
  prints one unrelated help line. The branch was never pushed, and the
  serving checkout is pinned to main. The second case also has the same
  recorded bytes as a body the lane published, which must retry.

## Ceilings

- A hand delivery is the only answer to a merged pull request whose
  claim reconcile cannot vouch for (scope changed, base advanced, or
  another head). An operator who finds the claimed item undelivered has
  no command that settles the receipt; this holds on main for the first
  two. A settle-without-delivery verb would be a new mechanism, so it
  is not built here.

## Risks

- A merge after any failed reprepare now refuses until a prepare
  publishes. That is the intent: the published body may not describe
  the pushed head.
- The evidence tie is the row content, not a timestamp: lane entry
  times come from more than one machine's clock.
