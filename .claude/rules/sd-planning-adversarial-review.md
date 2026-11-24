---
paths:
  - "docs/work/**"
  - "WORKFLOW.md"
---

# SD planning adversarial review

Adversarial review runs at four points, each with a cap on automatic passes.
When the cap is spent with a blocking finding open, run one class pass, as the pack's
[`WORKFLOW.md` § Parallel work](../../WORKFLOW.md#parallel-work) describes, then review again.
The integrator decides further rounds and records each reason in `sd task note`.
The artifact moves on once every blocking finding is fixed or rebutted with evidence on the item.
A rebuttal needs no approval: record the finding, the reason and the evidence a reader can check.
Non-blocking findings hold nothing.

| Flow | Point | What it checks | Cap |
|---|---|---|---|
| Research | After the brief and decisions | Claims against sources, gaps, wrong calls | 2 |
| Research | Final product, before the send box | The piece, page or ticket as a reader sees it | 1 |
| Development | prd and design | Scope, missing requirements, wrong assumptions | 5 |
| Development | Code, in a build run | Defects a second reader finds in the builder's branch | 3 reviews, then it parks |
| Development | Code, before merge | Automatic local reviews `sd-ship` runs on one item's branch | 15 `sd-ship` passes |

The two code rows count different things: a build run's own reviews, and every automatic `sd-ship` pass on the
item's branch, the lane's prepares included. Neither is the lead's limit: after round 3, counting builder and
lane rounds together, the lead records fix, file or reject for each blocker (system sd:10204).

This file holds the only copy of that table. A skill that runs a review names its point here and reads its cap from
that row; no skill carries a cap of its own.

This file is also the one place the planning review rule is stated. When the
current run creates or materially updates an active work item's `design.md`
(or a legacy `prd.md`) under `docs/work/`, capture the pre-edit
existence and content hashes for those files. At the planning
convergence boundary, before requesting implementation approval or moving the
item to `in_progress`, read and follow
[`../sd-ai-command-pack/planning-adversarial-review.md`](../sd-ai-command-pack/planning-adversarial-review.md).

Apply that contract once per coherent planning edit batch. Do not claim
approval from a review lane that was skipped or failed, and do not proceed past
an unresolved blocking concern.
