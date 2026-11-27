# Recorded rebuttals

Read this before recording dispositions for blocking findings.
Use it only for a complete review of the exact clean head whose deterministic checks passed or did not run.
A blocking review runs no gate, so its `check.status` is `not_run` (sd:2605).
It records rebuttals and parked risks; neither needs operator acceptance.
It waives no missing depth, incomplete transport, failed check, or source change.
A fix needs verification on its new head.
Record a rejection that survives one review pass here before the next fix is pushed (`skills/sd-ship/SKILL.md`, Recorded rebuttals).
For itemless records, replace `--item ID` below with `--no-item --review-id ID`.

1. Run `sd-ship adjudicate --item ID --expected-head SHA --json`.
   Save the returned `proposal` outside the checkout.
   Preserve its bindings and every indexed raw finding.
2. Fill each blocking finding's `response_disposition`, `reason` and `test`.
   The template holds `rebutted|parked`: replace it with one of the two.
   A finding names a defect class, not only a line: before you fix or rebut it, check every sibling site of the same shape.
   A fix at the reported line alone leaves the class live at its siblings.
   Use `rebutted` for a rejection, with a reason a reader can check.
   For a code finding, first write a test that would fail on this head if the finding held (sd:10893).
   If it fails, the finding reproduces: fix it fail-first instead of rebutting it.
   If it passes, set the row's `test` to its argv and rebut.
   The test must run at the reviewed head: a test already committed there (`["python3", "-m", "pytest", "tests/test_x.py::test_y"]`) or an inline script (`["python3", "-c", "..."]`).
   A new commit moves the head, so adjudicate at the head the next review sees.
   Recording and each prepare run that test at the clean head, with no shell, for up to 300 s.
   Merge, publication and `verify-review` only read the record; they run no test.
   A test that fails, cannot start, overruns or dirties the checkout refuses: the blocker stays open.
   `test` is `[]` when there is none, such as a cosmetic or a parked finding.
   File the rebutted finding as an item for the next PR; it is advisory now.
   Use `parked` for an accepted risk, with a reason that says when to revisit it.
   A cosmetic finding is `rebutted` with a reason that starts `cosmetic:` and says why no behaviour changes.
   Cosmetic means the fix changes no behaviour and no action a reader takes; contract text is never cosmetic.
   Nobody fixes a cosmetic finding, and it gets no follow-up row.
   Identical findings keep separate indices and decisions.
3. Record it with `sd-ship adjudicate --item ID --expected-head SHA --dispositions-file FILE --json`.
   This validates the file and records it at once; it calls no provider.
   The record replaces any record made since step 1 without a check: dispositions recorded between the two calls are lost.
   That window is accepted (sd:9999): one operator, about a second between the calls; no flag binds the prior record.
4. Resume `sd-ship prepare --item ID --json`.
   A recorded disposition reuses completed coverage without another review reservation.
   When the report's gate did not run, this prepare runs it at the head before clearance and records `adjudicated_gate`.
   A failing gate refuses clearance and keeps the spent pass; fix it and prepare again, which reviews the fix.
   For itemless records, `sd-ship review --no-item --review-id ID` runs it; `verify-review` only reads the record.

The record binds repository, branch, review identity, head, complete history, raw report, and finding indices.
It separately binds review tools/policy and disposition tools/policy.
Prepare and merge revalidate these identities; a later recording needs a fresh prepare.
Blank, unresolved, or `addressed` responses cannot substitute for fix verification.

Raw reports, severities, exit codes, and spent reservations remain unchanged.
The checkpoint reports `review_clearance.kind: adjudicated`, not a clean raw review.
CI, ownership, protection, and separate merge authority remain required.
