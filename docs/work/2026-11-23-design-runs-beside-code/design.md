---
title: Design runs beside code, then ride the next code bundle
created: 2026-11-23
item: sd:10096
branch: build/sd-1739
---

# Design — design-runs-beside-code

## Problem

A design-only item built as a bundle took the repository's one bundle
slot (`REPO_CAP=1`). The code bundle of that repository waited behind
it, and each design cost its own PR.

## Approach

An item with `fields.work_kind = design` (operator ruling, note #74534)
moves in two steps. No new state: each step reuses a row kind the
dispatcher already runs.

1. Design run. Build or auto-pickup queues a draft-scope run
   (`draft.queue_design`) while the item has no written design. It is
   read-only, makes no branch and counts against the machine cap only.
   The tick claims it beside a code bundle. It writes a `Design:`
   comment and its questions, if any.
2. Ride. With no question, the design run's write queues one ordinary
   build row in the same transaction (`build.queue_row`). With a
   question, a ruling then Build queues it. A fresh design row never
   leads a bundle (`_rides`) and takes no slot (`_slots`). It joins the
   repository's next fresh code bundle. The brief tells the builder to
   commit the design, with its rulings applied, as
   `docs/work/<date>-<slug>/design.md` in the first commit, with a
   `Closes:` trailer. The lane closes it with the rest: one PR.

One reader answers whether a design's question still waits
(`build.design_waits`) and one lists its rulings
(`build.design_rulings`). Both read the standing ruling
(`needs_sweep.standing_ruling`): the newest one that neither Decide's
reversal nor a review withdrawal took back. The press, auto-pickup,
Retry, the claim, delivery and the brief ask them.

Once it rides, the design item is an ordinary bundle member. Retry,
drop, park, delivery and the merge take it as they take any member.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Design run claim | draft run row, item stays ready | an item moved off ready before the claim | `draft._refusal` cancels the row with a note | `test_a_design_item_and_a_code_bundle_in_one_repo_both_claim_in_one_tick` |
| Design write | `Design:` note, ride row, one transaction | the run was reaped first | the compare-and-set writes nothing; the retry runs again | existing `write` tests |
| Ride claim | the ride row joins a code bundle | no code bundle queued | the row waits queued; alone it claims nothing | `test_a_written_design_rides_the_next_code_bundle_and_never_leads_one` |
| Handoff when the code is skipped | none | the design is the delivery's only item; the lane refuses a branch of design records, and Fix and send-back expect a code item | the run blocks before the enqueue; Retry builds the code on the same branch | `test_a_design_is_not_delivered_without_code` |
| Ruling taken back, at the press | none | Build, auto-pickup or Retry would queue a design whose question asks again | `design_waits` refuses in `_press_refusal`; it reads the standing ruling, so Decide's reversal and a review withdrawal both hold | `test_the_press_refuses_a_design_whose_ruling_decide_reversed` |
| Ruling taken back, ride queued | none | the claim would take the ride into a fresh bundle | `_bundle` leaves the row out; it stays queued until a new ruling | `test_the_claim_leaves_out_a_ride_whose_ruling_decide_reversed` |
| Ruling taken back, bundle retried | none | a bundle that ran before claims whole, past the fresh check | the claim holds the bundle as a reopened dependency does: refused and parked, Retry resumes it | `test_a_retried_bundle_holds_while_its_design_ruling_is_reversed` |
| Ruling taken back, after the claim | the design is committed | the lane would close the design's item | delivery leaves the item out (`waits_on_operator`): a `Refs:` line keeps it open and blocked; a new ruling then Build amends `design.md` | `test_a_design_whose_ruling_is_reversed_after_the_claim_is_not_delivered` |
| Ruling replaced, after the brief | the design is committed | a new ruling stands, but the brief carried the one it replaced | the brief records the rulings it carries (`note_brief`, note ids); delivery holds a standing ruling not among them, with a `Refs:` line; a ruling in the claim's second is no replacement | `test_a_design_whose_ruling_is_replaced_after_the_claim_is_not_delivered`, `test_a_ruling_in_the_claims_second_is_no_replacement` |
| Ruling taken back, after the enqueue | the lane holds the branch | the merge closes the design | `sd-ship merge` refuses right before it publishes (`refuse_design_waiting`); rule, then deliver again | `test_the_merge_refuses_a_design_whose_ruling_was_reversed_after_prepare` |
| Ruling replaced, after the enqueue | the lane holds the branch | no question waits, so the merge closes the design under the superseded ruling | the merge asks the rule delivery asks (`design_waits(delivered=True)`): the standing rulings are exactly the brief's; Build again, so the brief carries the new one | `test_the_merge_refuses_a_design_whose_ruling_was_replaced_after_prepare` |
| Carried ruling stands no more, question not reopened | the design is committed | delivery held only a ruling the brief lacked | the compare runs both ways: a carried ruling that no longer stands holds too | `test_delivery_holds_unless_the_standing_rulings_are_the_briefs` |
| P1 ride claim | the ride row joins a code bundle | a P1 row joined only a P1 lead of its area, and a ride never leads, so it waited while code shipped | a ride joins any fresh bundle of its repository: it takes no slot | `test_a_p1_design_rides_an_ordinary_code_bundle` |
| Design commit | `design.md` on the branch | two designs with one title closed from one file that kept only one | the enqueue precheck (`undesigned`) refuses before review until each closed design has a `design.md` whose front matter's `item:` line names it | `test_two_designs_in_one_file_do_not_reach_review` |
| Brief | none | a reversed, withdrawn or replaced ruling reads as an instruction | `design_rulings` lists the standing ruling of each question only | `test_the_brief_rulings_are_the_standing_ones` |
| Design write queues the ride | ride row | the press would refuse the item (a dependency, a moved item) | `queue_row` runs the press's check before its write; a note says why, Build queues it later | `test_a_design_run_queues_no_ride_the_press_would_refuse` |
| A park after the landing message is composed | the member is parked | the message names a parked member | ordinary member park: the item stays parked, its commit stays on the branch for Retry | existing park tests |
| Press, pickup, one press of a code item that waits on its design | assignment row | each waits on the other forever | a design aboard the bundle is no dependency of it (`aboard`, `_riding`) | `test_a_code_item_that_waits_on_its_design_builds_with_its_ride` |
| Claim of a retried bundle | run row | a retried design row is no fresh ride, so the lead read as waiting | `aboard` counts every design member, fresh or retried | `test_a_retry_keeps_a_code_item_with_the_design_it_waits_on` |
| Retry, Fix and send-back split (`_inside`, `_waiting`, `_retry_takes`, `_queue_members`) | member rows | the design left to build alone, which it never does; the lead stayed ended | the split and the press check skip a design aboard | `test_a_retry_keeps_a_code_item_with_the_design_it_waits_on` |
| Build by item of the code item | member rows | the same split through `queue_again` | as Retry | `test_build_by_item_retries_a_code_item_with_the_design_it_waits_on` |
| Delivery, design left out for its ruling | none | the code item shipped without its design | `design_left` blocks the run; rule, then Retry | `test_a_code_item_is_not_delivered_without_the_design_it_waits_on` |
| Delivery, design with no commit | none | the code item shipped without its design | `design_left` blocks the run; commit the design, then Retry | `test_a_code_item_is_not_delivered_when_its_design_has_no_commit` |
| Enqueue precheck, owner read | none | a fenced `item:` example owned a design, also after empty front matter | the owner is the `item:` line inside the leading front matter, which ends at its first closing line | `test_a_fenced_example_owns_no_design`, `test_an_example_after_empty_front_matter_owns_no_design` |

## Acceptance criteria

- A design item and a code bundle in one repository both claim in one
  dispatch tick.
- A written design with no question rides the next code bundle, takes
  no slot and claims nothing alone.
- An open design question holds the ride, also after a ruling is
  withdrawn.
