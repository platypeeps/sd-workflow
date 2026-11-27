# Open pull requests

Run on `/sd-ship open-prs`, or when the operator says "check open PRs" (sd-next routes it): every open PR in this repo and its companion repos.
Budget: about 30 minutes. Follow `skills/_shared/references/decide-ask-record.md` in the sd-workflow checkout to decide, ask, record and report; this run's tag is `open-prs`.

1. List them with `mcp__github__list_pull_requests`, and read each one's state with `pull_request_read` `get`.
   Keep each PR's head branch, full head commit and base branch (`base.ref`) from that read; every later step binds to them.
   A PR whose head repo (`head.repo.full_name`) is not its base repo, or is absent, is a fork PR: report it and act on nothing.
   A branch name matches only inside one repo, so a fork's branch never names a lane entry, item or worktree here.
2. For each PR, find what holds it: a failed `sd/local-gate`, open review findings, a merge conflict, or a lane entry.
   Its item is the one item whose note names the PR's full URL as a whole word:
   search with `sd store items --belongs-to <checkout> --grep "<PR URL>"`, then read each hit with `sd task show N`.
   No item, or more than one: report the PR and act on nothing there.
   A lane entry never names the item: it holds a branch and a head, no PR number, and the lane keeps old entries,
   so a PR that replaced another on the same branch and commit would take the old one's item.
   Its lane entries are every entry in `sd-ship -C <checkout> lane list` whose `branch` equals the PR's head branch exactly, at any `expected_head` and in any `status`:
   prepare's catch-up push moves the PR's head past its own entry's, so a head match would hide a failed entry.
   One of them may be an older PR's, so any one stops the enqueue, and none permits a write.
   Every step-4 command names the item, not the PR, so the item must stand for this PR alone:
   when two or more open PRs in the step-1 list resolve to one item: report each and act on none.
   Step 4 acts only where no other open PR of the item is in that list.
3. Skip, and only report, a PR in either case:
   - its builder is still running, or an entry on its branch or of its item is `pending` or `running`: touching it races that writer;
     this read can go stale, so enqueue checks again in its own write, and its refusal is a skip too;
   - its repo is one the operator does not manage (no row in `sd-db repo list`): report it, and act on nothing there.
4. Act on the rest:
   - A finding blocks only when it names a concrete failing input, command or interruption that a real caller reaches.
     File every other finding as an sd item for the repo's next PR, as that file's filing command says, with `--ref open-prs:<date>:<slug>`.
   - The sweep sends no finding to a builder and retries no lane entry: both commands name the item, not the branch,
     and pick the item's newest build or entry when they write, so one queued since the sweep's read takes the write.
     Record a blocking finding with `sd task note N "<PR URL>: <finding>"`, and report the PR for the operator's Fix.
     Never push by hand to a branch the lane holds.
   - Past round 3, counting builder and lane rounds, record fix, file or reject for each blocker with `sd task note N "<PR URL: fix, file or reject, and the reason>" --kind decision`.
   - Rebut a finding that doesn't hold, with a reason a reader can check.
   - Report a PR whose branch has a lane entry, and queue nothing: give the newest entry's `item`, `status` and `expected_head` beside the PR's item and head, and let the operator decide whether it is this PR's.
     Name it for the operator's Retry only when that entry failed and its cause lay outside the branch and is gone (an sd outage, a gate that never ran).
     A review blocker is fixed or rejected, never retried.
   - Enqueue a PR that passes only when the lane has no entry on its branch and no entry of item N but `merged` ones, from a worktree checked out on its head branch.
     Report a stacked PR and queue nothing: another open PR's base is this PR's head branch, or its base is not the repo's default branch.
     The lane squash-merges into the default branch and deletes the head branch, which strands each PR stacked on it.
     Find it with `git -C <checkout> worktree list --porcelain`: the entry whose `branch` is `refs/heads/<head branch>` exactly.
     No such worktree, or one whose `HEAD` is not the head step 1 read: report the PR and queue nothing.
     Then run `sd-ship -C <worktree> lane enqueue --item N --deliver|--associate-only --title "<title>" --body-file <body> --expected-head <head>`.
     The first enqueue of an item refuses without its claim: `--deliver` when this PR is the item's last, `--associate-only` when a later PR finishes it.
     `<title>` is the PR's title and `<body>` a file holding its live body.
     Enqueue names the item, and reads the branch from the worktree itself, so the entry it queues is this PR's branch; it refuses, queuing nothing, an item already queued or a head that moved since step 1:
     read the PR again before any other try.
5. Ask the operator first, one question at a time, before you close a PR, merge one by hand, or request a person's review.

End with a table: PR, item, what held it, action, new state; then the veto table and the line `RESULT: filed <n>, built <n>, asked <n>`.
