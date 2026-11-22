---
title: Land a ready train in one push of main, one gate run
created: 2026-11-22
item: sd:9923
branch: build/sd-1543
---

# Design — train-landing

## Problem

The train of sd:9899 gates the top of up to 3 queued entries once, but
each member still lands alone through GitHub's merge API. Every tree
that becomes main must pass its own gate, so the train saves no gate
run. A saving needs the members to land together: one push of main,
one gate run for the joined tree (operator 2026-11-21).

## Approach

The runner lands a train of N ready members, 2 to `sd.lane_train`, in
these steps. Each step reuses a reader or writer that exists today.

1. Prepare: each member runs `sd-ship prepare` as today: its own
   review, its own pull request, its own receipt. Only the top's
   prepare gates; the members below skip their gate (open question 3).
2. Join: in a scratch worktree on the fetched `origin/<base>` F, merge
   each member's reviewed head `H_i` onto the landing before it with
   `git merge --no-ff`. The merge commit `M_i` carries the message
   `sd-ship merge` composes today: the title with `(#N)`, the body,
   `Item:`, `Delivers:` and `Reviewed-base:`. `demoted_trailers`
   checks each message. A member that conflicts drops out, as in
   sd:9899, and rides the next train.
3. Gate: the local gate runs once, on the tree of `M_N`.
4. Scratch push: `M_N` goes to `refs/heads/sd-train/<run>`, so GitHub
   holds the commits before any status names them.
5. Status: `sd/local-gate` success is posted on each `H_i` and on
   `M_N`, from the one gate run. Branch protection reads the status
   on the pushed tip.
6. Land: `git push origin M_N:refs/heads/<base>`, never forced. GitHub
   refuses it when main moved (non-fast-forward) or protection forbids
   the push; then nothing landed and each member ships alone.
7. Reconcile: GitHub marks a pull request merged once its head is
   reachable from the base. Each member then runs `sd-ship merge`,
   which reads `merged: true` and runs the existing `reconcile`:
   `Delivers:` on `M_i` closes the item, cleanup runs as today.
8. Cleanup: the scratch ref is deleted. A missed delete leaves a
   stale ref, which the next train's step 4 overwrites by name.

The train lands only where every precondition holds, read through
`sd_protection` before step 4. An unknown answer counts as no.

- No firm `required_pull_request_reviews`: a direct push to a branch
  that requires a pull request is refused for every non-bypass actor.
- No `required_linear_history`: the train lands merge commits.
- Each required status check is `sd/local-gate`, which step 5 posts.
- `repo.runner_merge` is `auto`, and every member passes
  `merge_refusal`.

Otherwise the members ship one at a time, as after sd:9899.

## Facts to measure before code

Each is a GitHub behaviour a builder cannot test: it needs a push to a
scratch repository, which only the hub session may make.

1. A pull request whose head a push makes reachable from the base
   reads `merged: true`; which commit its `merge_commit_sha` names.
   `reconcile` needs `M_i` there, or `require_merge_on_base` refuses.
2. A direct push to a protected main with a required `sd/local-gate`
   check passes when the status sits on the pushed tip only, or needs
   it on every new commit.

## Open questions

1. Shape: merge commits mark each pull request merged and reuse
   `reconcile`, but a `merge=squash` repository then holds merge
   commits. A squash chain keeps history linear, but GitHub shows each
   pull request closed, not merged, and `reconcile` needs a train path.
2. Hold: `M_i` for i > 1 lands on `M_(i-1)`, a base its review did not
   cover, so `held_at_merge` holds its delivery (sd:1089, #1179). Today
   a hand delivery after the combined tree passed releases the hold.
   Does the train's joined-tree gate release it automatically?
3. Prepare without a gate for the members below the top needs a new
   prepare flag, a new mechanism.

## Acceptance criteria

- A train of 3 green members lands in one push of main and one gate
  run; each pull request reads merged and each item closes by its own
  `Delivers:`.
- A push GitHub refuses lands nothing; each member ships alone.
- A member that conflicts drops out and stays pending.
- A protection the train cannot satisfy, or cannot read, starts no
  train landing.
- A run stopped after step 6 reconciles each member on the next run.

## Failure table

```text
| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 1 prepare | each member's PR and receipt | a member's review blocks | it leaves the train; the rest join | train members |
| 2 join | scratch commits, unreferenced | a member conflicts | it drops, stays pending | join conflict |
| 2 join | none | a message demotes a trailer | the member drops; it ships alone | join trailers |
| 3 gate | the gate's receipt on M_N | the gate fails | no push; each member ships alone | gate fails |
| 3 gate | none | TERM or deadline | no push; scratch tree removed | gate cancelled |
| 4 scratch push | sd-train/<run> ref | push fails | no status, no landing; ref absent | scratch push |
| 5 status | statuses on H_i, M_N | a post fails | no landing; a status on an unlanded sha moves nothing | status post |
| 6 land | main at M_N | main moved: non-fast-forward | nothing landed; ship alone | land refused |
| 6 land | main at M_N | protection refuses the push | nothing landed; ship alone | land refused |
| 6 land | main at M_N, maybe | answer lost after GitHub took it | step 7 reads main; reconcile per member | land lost |
| 6 land | none | run twice | second push is up to date or refused | land twice |
| 7 reconcile | item done, PR merged | stopped midway | next run's sd-ship merge reconciles | reconcile resumes |
| 7 reconcile | none | merge_commit_sha is not M_i | reconcile refuses; item stays open | fact 1 |
| 7 reconcile | none | held at merge (question 2) | hold note; hand delivery | hold |
| 8 cleanup | ref deleted | delete fails | stale ref; next train overwrites it | cleanup |
```

## Not in this change

- One combined review: each member keeps its own (sd:9899).
- Landing across repositories: a train is one repository's queue.
