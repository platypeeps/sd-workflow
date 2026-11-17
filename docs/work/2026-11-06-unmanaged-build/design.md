---
title: Build an unmanaged repository's item to a draft pull request
created: 2026-11-06
item: sd:7868
branch: feat/sd-7868-unmanaged-build
---

# Design — unmanaged-build

## Problem

The Build press and auto-pickup refuse a ready item whose repository is
registered with `repo.managed = 0`: `build._repo_refusal` says "is not
managed". Such a repository belongs to someone else (memory-experiment
sd:7832 is one), so no lane, merge or pack file may reach it. The operator
ruled (sd:7868 note #46131) that a build there still runs: the same builder,
`sd-review --scope branch` and the repository's own tests, then a pushed
branch and a draft pull request that the owners merge.

## Approach

Keep the run's steps and their compare-and-sets; change what two steps do
when the run's repository reads unmanaged.

- Press and pickup: `_repo_refusal` drops the managed rule. An unmanaged
  repository instead needs a GitHub remote, and each item must be a
  `task`: its pull request is tracked as the task's contribution
  `pull_url`, which `contributions.configure` writes for tasks only.
- `reviewing`: `sd-review` already runs review-only there (its gate
  skips an unmanaged checkout, sd:5953). After a clean or advisory review
  the supervisor runs the repository's own checks, as `sd-check` detects
  them (`sd_lib.detect_entrypoints`: the local block, Makefile, Taskfile,
  package.json, Cargo.toml, pyproject.toml), each through `sd gate run`
  so it queues for a machine gate slot. A failure goes back to the
  builder as a blocker, as `gate_failed` does. So does a check that
  exits 0 but changes a tracked file (`git status --porcelain
  --untracked-files=no` not empty): it tested a tree the head does not
  hold, so publish would push an untested commit. No entrypoint found is
  noted on the lead item and is no failure.
- `enqueuing`: the same compare-and-set, delivery read, title check and
  `sd-docs-lint --pr-body`. Then, instead of `sd-ship lane enqueue`:
  `git push origin <head>:refs/heads/<branch>`, find the open pull
  request for that head branch with `gh pr list`, or open one with
  `gh pr create --draft`, then link it on the lead task with
  `contributions.configure(pull_url=...)`. The run's `detail` names the
  draft, and `build.enqueued` ends the members `done` with the lease
  released, as for the lane.
- Title and body: the builder writes both, as for the lane; the same
  line check and leak guard (`enqueue_refusals`) run before the review.
  That guard runs only where a pre-push hook names it, which an
  unmanaged repository lacks, so `publish` runs `leak-guard.sh scan`
  (`Tools.guard`) itself before the push: over the title, the body, and
  each commit's message, paths and added lines in base..head. A match,
  or a scan that cannot check (no pattern file is exit 3), blocks the
  run with nothing pushed. The scan runs only when the repository is
  public, the rule machine-setup uses for the pre-push guard (operator
  ruling): the owners of a private one already see work names. `gh repo
  view --json isPrivate` reads it; a visibility that did not read counts
  as public, so the scan runs. `sd-docs-lint` gets `--body-only` there, so a
  foreign docs tree cannot block the run.
- Reviewed head: no lane gate re-checks an unmanaged head, so
  `enqueuing` publishes only the head this supervisor's review and
  checks passed. A run resumed or retried at `enqueuing` goes back to
  `reviewing` first.
- Checks' processes: `sd gate run` starts its command in a process group
  of its own. Every stop reads, live from `ps`, the groups that the
  recorded group's descendants run in, and ends them with it: the
  supervisor's `end_group` (TERM, the grace, then KILL) and the reap's
  `kill_group` (KILL). A live read names only groups this run's processes
  started, so a recorded number a reused pid holds is never signalled.
  The supervisor also records each group with its leader's start, to end
  one that outlived its parent; it signals one only while that leader, or
  no leader, runs in it.
- `enqueued`: the tick's `watch` reads a run whose `detail` names a draft
  pull request from the contribution observation (`contribution_sync`
  observes every task `pull_url`), never the lane. Merged: the delivered
  tasks still `in_progress` move to `done`, and the run goes to
  `cleaning` (never `checking`: a merge in someone else's repository
  deploys nothing here). Closed without merge: the run ends
  `lane_failed`, its items left for the operator. Open: nothing.
- Item status: `in_progress` until the merge, as a lane-held item is.
  With sd:7859, Queue shows it as waiting on the pull request. A draft
  pull request the operator wrote, linked to an item, reads `awaiting_you`
  with the reason "Draft pull request: mark it ready for review", so the
  operator sees it until it is marked ready (note #46134). The Builds
  panel's Open PR link reads the lead task's `pull_url` when the run's
  repository is unmanaged.
- Dashboard (note #46134): Tasks Details gains a "Pull request" section
  for an item with a contribution `pull_url`: a link that opens it in a
  new tab, its observed state and lane, and a "Needs you" line with the
  projection's reasons while `needs_you` holds. Queue's row and its Open
  PR link come from sd:7859 ("blocked: PR · on you"), which reads the
  same lane; its DOM test for a draft follows once sd:7859 lands.
- The needs-you signal is the contribution lane `awaiting_you`, chosen
  over a new pr-attention report or a Queue-only state: Today's Needs
  you `prs` rows, the Contributions page and sd:7859's Queue "on you"
  all read that lane already, so one rule in `_projection_row` flags the
  draft everywhere with no new report kind or state.

Rejected: letting `sd-check` or `sd-ship` run in an unmanaged checkout.
Both refuse there on purpose (sd:2566), and `sd-ship` writes receipts and
merges.
Correction (2026-11-16): the operator's ruling, note #58478 on
sd:9100, later accepted `sd-ship`, `sd-check` and `sd-status` in an
unmanaged repo, for one surface for unmanaged repos. `sd-ship`
`prepare` and `merge` go through GitHub alone, a plain `sd-check`
records no receipt, and `--record-receipt` and the lane still refuse.
Rejected: a new run column for the mode. The run's `detail` is
unset on an open run until it ends, so it carries the draft's address at
`enqueued` with no schema change.

No new command, flag, setting or table. `Tools` gains two argv seams,
`gh` and `gate`, which tests fill with fakes.

## Non-goals

- No merge, no `sd/local-gate`, no lane entry, no ship receipt.
- No pack file written into the repository: the title and body live in
  the run's log folder; the worktree holds only the builder's commits.
- No `work` or `report` items in an unmanaged repository.
- No marking ready for review: the operator does that on GitHub.

## Failure table

```text
Step     | State moved            | Failure              | Recovery                        | Test
-------- | ---------------------- | -------------------- | ------------------------------- | -----------------------------
claim    | assignment running,    | repo turned managed  | _refused_repo blocks a queued   | test_build: press and pickup
         | item in_progress, run  | or lost its GitHub   | build whose repo now refuses;   | take an unmanaged task;
         | and lease              | remote after press   | item left as it is              | refuse a non-task, no GitHub
worktree | git worktree, branch   | fetch or add fails   | run ends blocked; reap retries  | unchanged (managed tests)
         |                        |                      | a lost run once at `worktree`   |
build    | builder commits        | builder fails        | as managed: blocked or retried  | unchanged
review   | review_round           | review blocks        | fix round, at most 3            | unchanged
tests    | review_round on fail   | a check exits non-0  | blocker to the builder, rerun   | test_build_run: unmanaged
         |                        |                      | review and tests on the fix     | tests fail, fix, pass
tests    | none                   | supervisor lost      | reap retries at `reviewing`,    | resume at reviewing reruns
         |                        | mid-test             | which runs review and tests     | the tests
tests    | none                   | a check exits 0 but  | blocker naming the paths, no    | a check that edits a tracked
         |                        | edits tracked files  | pass noted                      | file is a failure
tests    | none                   | entrypoints do not   | run ends blocked naming the     | entrypoints that do not read
         |                        | read (config fault)  | fault; no fix round             | end the run
tests    | none                   | git gave no answer   | Unread: the reap retries once   | a git that gave no answer is
         |                        | to the detection     |                                 | unread
push     | remote branch at head  | push fails           | run ends blocked, nothing       | test_build_run: push refused
         |                        |                      | opened; Retry pushes again      | blocks, no PR call
push     | remote branch at head  | lost after push      | retry at `enqueuing` pushes the | lost after the push: the
         |                        |                      | same head again (a no-op)       | retry opens one PR
draft PR | an open draft PR       | gh refuses           | run ends blocked with gh's text | gh create refused
draft PR | an open draft PR       | lost after create    | retry finds the open PR for the | lost after the create:
         |                        |                      | branch with gh pr list, opens   | retry lists and creates none
         |                        |                      | no second one                   |
draft PR | none                   | a fork's PR uses the | not this run's: ignored; the    | a fork's pull request is
         |                        | branch name          | run opens and links its own     | never linked
draft PR | none                   | the branch's PR      | run ends blocked naming the     | another base is not linked
         |                        | targets another base | base; nothing linked            |
draft PR | none                   | the PR reads another | Unread (GitHub moves the head   | another head is unread and
         |                        | head than the push   | after a push); reap retries     | not linked
link     | lead task's pull_url   | configure refuses    | run ends blocked naming the URL | link refused blocks
end      | detail, members done,  | lost before enqueued | retry redoes push, list, link:  | covered by the two above
         | lease released         |                      | each finds its state done       |
merge    | tasks done, run at     | observation unread   | nothing moves; next tick reads  | watch: open, merged, closed,
         | cleaning               | or stale             | again                           | unread
merge    | run lane_failed        | closed unmerged      | operator reopens or cancels;    | watch: closed ends lane_failed
         |                        |                      | worktree kept                   |
review   | none                   | retry at enqueuing   | back to reviewing: review and   | test_build_run: a retry at
         |                        | with no reviewed head| checks before the publish       | enqueuing reviews and tests
push     | none                   | a private value in   | blocked before the push; the    | test_build_run: private
         |                        | title, body, commits | scan reads all three            | commit, title; no patterns
push     | none                   | visibility unread    | read as public: the scan runs;  | test_build_run: private repo
         |                        | or repo private      | private skips it, noted         | publishes; unread scans
scan     | none                   | added `++x` reads as | header only between diff --git  | test_build_run: reads an
         |                        | `+++x`, the header   | and @@; hunk `+` lines all kept | added line starting ++
scan     | none                   | a file or body not   | read as bytes; patterns match   | not utf8 file; body not
         |                        | UTF-8 crashed        | ASCII; body refused, not crash  | utf8 blocks, no crash
scan     | none                   | CR or form feed cut  | split on LF alone, as grep -n   | keeps a line whole across
         |                        | a line in two        | numbers lines                   | a CR
scan     | none                   | message line `diff   | messages read by their own      | message line that looks
         |                        | --git` ended message | git log --format=%B             | like a diff header
scan     | none                   | binary file: no +++  | paths from --name-only -z;      | binary file's path; binary
         |                        | line, no text        | content unread, as leak-guard   | content not scanned
scan     | none                   | `-` line, `\ No      | not added text: skipped, as     | none needed: no text the
         |                        | newline`, mode line  | leak-guard check skips them     | push adds
scan     | none                   | CRLF line            | LF split keeps `x\r`; grep     | none needed: the CR stays
         |                        |                      | matches inside it               | inside the scanned line
push     | none                   | foreign docs tree    | --body-only: the body alone     | test_build_run: body lint
         |                        | breaks the tree lint | is linted                       | skips a foreign docs tree
flag     | none (a read)          | draft not marked     | lane awaiting_you with DRAFTED  | test_contributions: draft
         |                        | ready by operator    | until observed ready            | reads awaiting_you; Details
         |                        |                      |                                 | flags it (DOM test)
link UI  | none (a read)          | operator cannot      | Details links pull_url in a new | test_v2_tasks: Details link
         |                        | find the draft       | tab; Queue via sd:7859          | opens it (DOM test)
```

Each git read the scan makes, by commit kind (review of 5a5adca64).
All three run `git log base..head` with `--no-renames`,
`--diff-merges=first-parent` and `log.showRoot=true`:

```text
Read      | Commit kind | What it reads                    | Test
--------- | ----------- | -------------------------------- | ------------------------------
messages  | merge       | its message (%B lists merges)    | covered by the merge test's
          |             |                                  | message read; no own test
messages  | root        | its message                      | none needed: same read
messages  | rename      | its message                      | none needed: same read
paths     | merge       | paths changed vs first parent;   | a value only a conflict
          |             | before: none                     | resolution adds
paths     | root        | every path it adds               | not alone: base..head holds a
          |             |                                  | root only for a branch with no
          |             |                                  | shared history, which the
          |             |                                  | worktree step never makes
paths     | rename      | old and new path (delete, add)   | a renamed file's new path
patches   | merge       | lines added vs first parent,     | a value only a conflict
          |             | resolution included; before:     | resolution adds; one side
          |             | none. Main's lines too: a match  | taken passes
          |             | there blocks, never leaks        |
patches   | root        | every line, showRoot forced on   | as paths, root
patches   | rename      | the whole file as added          | a renamed file's new path
```

How a run's check ends, one row per path. "Groups below" means the
groups that the recorded group's descendants run in, read live; the
check runs in one, under the gate. Each test is in test_build_run.

```text
End path            | Who stops the check's groups      | Test
------------------- | --------------------------------- | ------------------------------
normal exit         | the gate waits for the check;     | a check that exits leaves no
                    | end_group ends what it left in    | process of its group
                    | its group (recorded, leaderless)  |
check failure       | as normal exit; then a blocker    | tests fail, fix, pass
timeout             | end_group: groups below, TERM,    | a check that ignores SIGTERM
                    | grace, KILL                       | ends with the run
cancel              | the beat's end_group, as timeout  | a cancel during a check ends
                    |                                   | the check's own group
lost run (beat      | the beat's end_group, as timeout, | shares end_group with the two
refused)            | then Lost                         | rows above
supervisor SIGKILL, | the reap's kill_group: builder    | a supervisor killed during a
then reap           | group and groups below, KILL      | check leaves no check
heartbeat loss,     | kill_group on the supervisor's    | a supervisor whose beat
then reap           | group, then the builder's         | stopped leaves no check
gate refusal (125)  | none started; the gate's group    | a gate that starts no check
                    | reads gone                        | leaves no group
reused pid          | a group is signalled only if read | a group the check started and
                    | live below, or its recorded       | that ended is not signalled;
                    | leader (or none) still leads it   | one led by another process is
                    |                                   | left alone
```

Each `ps` or group read the supervisor makes (review of b34af431e).
One rule: an unread read signals nothing it did not prove the run's,
and never fails a run alone; a known group reading ESRCH is gone.

```text
Read                     | Unread means                     | Test
------------------------ | -------------------------------- | ------------------------------
register start (run)     | start None recorded; the reap    | unchanged (build reap tests)
                         | reads a live pid unsure, not ours|
builder_start (child)    | start None recorded, as above    | unchanged
beat descendant_groups   | no group recorded this beat; the | a supervisor killed during a
                         | next beat reads again            | check leaves no check
end_group widen          | no group added; known groups are | an unread ps does not fail an
                         | signalled, their ESRCH is True   | end once the group is gone
                         | (was a sticky flag: Survivor)    |
end_group gone (group)   | not gone: KILL, then Survivor    | a group of zombies reads gone
ours (group, alive,      | not ours: never signalled        | a group led by another process
started)                 |                                  | is left alone
kill_group (build.py)    | recorded group only; ESRCH True  | test_build: a kill whose
                         |                                  | descendants did not read
```

Not covered: a check that leaves a daemon in a new group and exits
before a reap. Nothing then ties it to the run; the lane's gate has
the same reach.

## Acceptance criteria

- [ ] A press on a ready task in an unmanaged repository with a GitHub
  remote queues; `make test SUITE=sd_db` style focused runs of
  `tests/sd_db/test_build.py` pass.
- [ ] A supervised unmanaged run ends `enqueued` with a draft pull
  request, no `sd-ship` call, the branch on the fixture origin, and the
  lead task's `pull_url` set; `tests/sd_db/test_build_run.py` passes.
- [ ] Every managed-path test in both files passes unchanged.
- [ ] Tasks Details for a task with a draft pull request links it in
  a new tab and flags it as needing the operator; `test_v2_tasks`
  passes.

## Risks

- A push to a third-party remote publishes. It happens only in a build
  the operator pressed or auto-pickup they turned on; tests push to a
  fixture bare repository and fake `gh`.
- Commits carry `Closes: sd:N` trailers into the owners' history. The
  delivery read needs them; they name an item number, nothing private.
- The merge closes the items only once `contribution_sync` observes it,
  which runs on its own schedule.
- The draft's "needs you" reason waits for the first observation.
- A check's own exit 125 is a failure; the gate's refusal (125) ends
  its stderr with `NOT_STARTED` and reads unknown (pack sd:7899).
  A 127 still reads as a check failure.
- Evidence: the watch ties a merge to the run by the pull request URL
  the run recorded and the observation keyed by that URL; the tests'
  pass is tied to the head the review read.
