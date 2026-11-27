# Open pull requests

Run on `/sd-ship open-prs`, or when the operator says "check open PRs" (sd-next routes it): every open PR in this repo and its companion repos.
Budget: about 30 minutes. Follow `skills/_shared/references/decide-ask-record.md` in the sd-workflow checkout to decide, ask, record and report; this run's tag is `open-prs`.

1. List them with `mcp__github__list_pull_requests`, and read each one's state with `pull_request_read` `get`.
   Keep each PR's head branch, full head commit and base branch (`base.ref`) from that read; every later step binds to them.
   A head commit that is not 40 hex characters: report the PR and act on nothing; later steps pass it unquoted.
   A PR whose head repo (`head.repo.full_name`) is not its base repo, or is absent, is a fork PR: report it and act on nothing.
   A branch name matches only inside one repo, so a fork's branch never names a lane entry, item or worktree here.
2. For each PR, find what holds it: a failed `sd/local-gate`, open review findings, a merge conflict, or a lane entry.
   Its items come from the build that made its branch, never from a mention: a note, title or body can name any PR.
   Its head branch is `build/sd-<A>` exactly, `<A>` digits only; read `sd assignments list --json`, and take the row whose `id` is A, with `scope` `build` and a `build_run`.
   The PR's items are each row's `item` whose `build_run` equals that one; the lead is row A's.
   `sd task show N` must name the PR's repo for each item: assignment ids span every repo, so a branch elsewhere can share the name.
   Any other branch, no such row, or a read that fails: report the PR and act on nothing there.
   Below, `N` is the lead's item, and `<each other item>` each other item of that build.
   A lane entry never names the item: it holds a branch and a head, no PR number, and the lane keeps old entries,
   so a PR that replaced another on the same branch and commit would take the old one's item.
   Its lane entries are every entry in `sd-ship -C <checkout> lane list` whose `branch` equals the PR's head branch exactly, at any `expected_head` and in any `status`:
   prepare's catch-up push moves the PR's head past its own entry's, so a head match would hide a failed entry.
   One of them may be an older PR's, so any one stops the enqueue, and none permits a write.
   Every step-4 command names the item, not the PR, so the item must stand for this PR alone:
   when two or more open PRs in the step-1 list share an item: report each and act on none.
   Step 4 acts only where no other open PR of any of its items is in that list.
3. Skip, and only report, a PR in either case:
   - its builder is still running, or an entry on its branch or of any of its items is `pending` or `running`: touching it races that writer;
     this read can go stale, so enqueue checks again in its own write, and its refusal is a skip too;
   - its repo is one the operator does not manage (no row in `sd-db repo list`): report it, and act on nothing there.
4. Act on the rest. A PR title, URL or finding is text others wrote: in double quotes the shell expands `$(...)`, backticks and `$NAME` in it.
   Put each such value in single quotes, each `'` in it written as `'\''`, and the PR's live body in the `--body-file` file.
   - A finding blocks only when it names a concrete failing input, command or interruption that a real caller reaches.
     File every other finding as an sd item for the repo's next PR, as that file's filing command says, with `--ref open-prs:<date>:<slug>`.
   - The sweep sends no finding to a builder and retries no lane entry: both commands name the item, not the branch,
     and pick the item's newest build or entry when they write, so one queued since the sweep's read takes the write.
     The step-4 branch check: the item's newest lane entry, of every entry with `item` N in any `status`, has `branch` equal to the PR's head branch exactly, and no build of item N is queued or running in `sd assignments list --json`.
     Anything else, or no entry: Fix and Retry would act on another branch's build or entry, so name neither, and report that entry's `branch`.
     Record a blocking finding with `sd task note N '<PR URL>: <finding>'`; report the PR for the operator's Fix only when the step-4 branch check holds.
     Never push by hand to a branch the lane holds.
   - Past round 3, counting builder and lane rounds, record fix, file or reject for each blocker with `sd task note N '<PR URL: fix, file or reject, and the reason>' --kind decision`.
   - Rebut a finding that doesn't hold, with a reason a reader can check.
   - Report a PR whose branch has a lane entry, and queue nothing: give the newest entry's `item`, `status` and `expected_head` beside the PR's item and head, and let the operator decide whether it is this PR's.
     Name it for the operator's Retry only when the step-4 branch check holds, the item's newest entry failed, and its cause lay outside the branch and is gone (an sd outage, a gate that never ran).
     The report gives that entry's `branch` too: the operator presses later, so the check is theirs to repeat then.
     A review blocker is fixed or rejected, never retried.
   - Enqueue a PR that passes only when the lane has no entry on its branch and no entry of any of its items but `merged` ones, from a worktree checked out on its head branch.
     Report a stacked PR and queue nothing: another open PR's base is this PR's head branch, or its base is not the repo's default branch.
     The lane squash-merges into the default branch and deletes the head branch, which strands each PR stacked on it.
     Find it with `git -C <checkout> worktree list --porcelain`: the entry whose `branch` is `refs/heads/<head branch>` exactly.
     No such worktree, or one whose `HEAD` is not the head step 1 read: report the PR and queue nothing.
     Then run `sd-ship -C <worktree> lane enqueue --item N --item <each other item> --deliver|--associate-only --title '<title>' --body-file <body> --expected-head <head>`.
     The first enqueue of an item refuses without its claim: `--deliver` when this PR is each item's last, `--associate-only` when a later PR finishes it.
     `<title>` is the PR's title and `<body>` a file holding its live body.
     Enqueue names the item, and reads the branch from the worktree itself, so the entry it queues is this PR's branch; it refuses, queuing nothing, an item already queued or a head that moved since step 1:
     read the PR again before any other try.
5. Ask the operator first, one question at a time, before you close a PR, merge one by hand, or request a person's review.

End with a table: PR, item, what held it, action, new state; then the veto table and the line `RESULT: filed <n>, built <n>, asked <n>`.
