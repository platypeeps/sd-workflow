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
| Handoff when the lead is skipped | the design member is delivered beside a skipped lead | the lead delivers nothing | ordinary member handling; a design row that ran leads its Retry, as `_rides` holds only fresh rows | existing skipped-lead tests |
| Ruling taken back, at the press | none | Build, auto-pickup or Retry would queue a design whose question asks again | `design_waits` refuses in `_press_refusal`; it reads the standing ruling, so Decide's reversal and a review withdrawal both hold | `test_the_press_refuses_a_design_whose_ruling_decide_reversed` |
| Ruling taken back, ride queued | none | the claim would take the ride into a fresh bundle | `_bundle` leaves the row out; it stays queued until a new ruling | `test_the_claim_leaves_out_a_ride_whose_ruling_decide_reversed` |
| Ruling taken back, bundle retried | none | a bundle that ran before claims whole, past the fresh check | the claim holds the bundle as a reopened dependency does: refused and parked, Retry resumes it | `test_a_retried_bundle_holds_while_its_design_ruling_is_reversed` |
| Ruling taken back, after the claim | the design is committed | the lane would close the design's item | delivery leaves the item out (`waits_on_operator`): a `Refs:` line keeps it open and blocked; a new ruling then Build amends `design.md` | `test_a_design_whose_ruling_is_reversed_after_the_claim_is_not_delivered` |
| Ruling replaced, after the claim | the design is committed | a new ruling stands, but the brief carried the one it replaced | `design_waits(since)` also holds a standing ruling written since the claim; delivery leaves the item out with a `Refs:` line; Build queues a ride whose brief carries the new ruling | `test_a_design_whose_ruling_is_replaced_after_the_claim_is_not_delivered` |
| P1 ride claim | the ride row joins a code bundle | a P1 row joined only a P1 lead of its area, and a ride never leads, so it waited while code shipped | a ride joins any fresh bundle of its repository: it takes no slot | `test_a_p1_design_rides_an_ordinary_code_bundle` |
| Design commit | `design.md` on the branch | two designs with one title closed from one file that kept only one | the enqueue precheck (`undesigned`) refuses before review until each closed design has a `design.md` whose first `item:` line names it | `test_two_designs_in_one_file_do_not_reach_review` |
| Brief | none | a reversed, withdrawn or replaced ruling reads as an instruction | `design_rulings` lists the standing ruling of each question only | `test_the_brief_rulings_are_the_standing_ones` |
| Design write queues the ride | ride row | the press would refuse the item (a dependency, a moved item) | `queue_row` runs the press's check before its write; a note says why, Build queues it later | `test_a_design_run_queues_no_ride_the_press_would_refuse` |
| A park after the landing message is composed | the member is parked | the message names a parked member | ordinary member park: the item stays parked, its commit stays on the branch for Retry | existing park tests |

## Acceptance criteria

- A design item and a code bundle in one repository both claim in one
  dispatch tick.
- A written design with no question rides the next code bundle, takes
  no slot and claims nothing alone.
- An open design question holds the ride, also after a ruling is
  withdrawn.
