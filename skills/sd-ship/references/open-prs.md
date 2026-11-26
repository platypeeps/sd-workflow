# Open pull requests

Run on `/sd-ship open-prs`, or when the operator says "check open PRs" (sd-next routes it): every open PR in this repo and its companion repos.

1. List them with `mcp__github__list_pull_requests`, and read each one's state with `pull_request_read` `get`.
   Keep each PR's head branch and full head commit from that read; every later step binds to them.
   A PR whose head repo (`head.repo.full_name`) is not its base repo, or is absent, is a fork PR: report it and act on nothing.
   A branch name matches only inside one repo, so a fork's branch never names a lane entry, item or worktree here.
2. For each PR, find what holds it: a failed `sd/local-gate`, open review findings, a merge conflict, a failed lane entry, or no lane entry.
   Its item is the lane entry's `item` in `sd-ship -C <checkout> lane list` whose `branch` equals the PR's head branch exactly.
   With no such entry, it is the one item whose note names the PR's full URL as a whole word:
   search with `sd store items --belongs-to <checkout> --grep "<PR URL>"`, then read each hit with `sd task show N`.
   No item, or more than one: report the PR and act on nothing there.
3. Skip, and only report, a PR in either case:
   - its builder or its lane entry is still running: touching it races that writer;
     this read can go stale, so each step-4 command checks again in its own write, and its refusal is a skip too;
   - its repo is one the operator does not manage (no row in `sd-db repo list`): report it, and act on nothing there.
4. Act on the rest:
   - A finding blocks only when it names a concrete failing input, command or interruption that a real caller reaches.
     File every other finding as an sd item for the repo's next PR.
   - Send a blocking finding with `sd assignments build N --finding "<finding>"`, the dashboard's Fix, and let the command decide:
     never predict from a build's status whether it takes one.
     If it refuses, for any reason, it writes nothing:
     record the finding with `sd task note N "<finding>"` and report the PR with the refusal's text.
     Never push by hand to a branch the lane holds.
     A build that takes the finding enqueues its own fixed head; do not retry or enqueue that PR.
   - Past round 3, counting builder and lane rounds, record fix, file or reject for each blocker with `sd task note N "<fix, file or reject: reason>" --kind decision`.
   - Rebut a finding that doesn't hold, with a reason a reader can check.
   - Retry a failed lane entry only when its cause lay outside the branch and is gone (an sd outage, a gate that never ran).
     A review blocker is fixed or rejected, never retried.
     Two PRs of one item can sit at one commit, so the commit alone does not pick this PR.
     Retry only when the item's newest entry in `sd-ship -C <checkout> lane list` is one whose `branch` equals this PR's head branch and whose `expected_head` is the head step 1 read,
     and no other open PR of the item, in the step-1 list, has that head commit; otherwise report the PR and retry nothing.
     Then run `sd-ship -C <checkout> lane retry N --expected-head <head>`, `<head>` that full head commit.
     The retry checks `<head>` against the item's newest entry in its own write:
     an entry queued since at another commit refuses `stale_head` and queues nothing. Report a refusal; never retry without the head.
   - Enqueue a PR that passes and has no lane entry, from a worktree checked out on its head branch.
     Find it with `git -C <checkout> worktree list --porcelain`: the entry whose `branch` is `refs/heads/<head branch>` exactly.
     No such worktree, or one whose `HEAD` is not the head step 1 read: report the PR and queue nothing.
     Then run `sd-ship -C <worktree> lane enqueue --item N --deliver|--associate-only --title "<title>" --body-file <body> --expected-head <head>`.
     The first enqueue of an item refuses without its claim: `--deliver` when this PR is the item's last, `--associate-only` when a later PR finishes it.
     `<title>` is the PR's title and `<body>` a file holding its live body.
     Enqueue reads the worktree's branch itself and refuses, queuing nothing, an item already queued or a head that moved since step 1:
     read the PR again before any other try.
5. Ask the operator first, one question at a time, before you close a PR, merge one by hand, or request a person's review.

End with a table: PR, item, what held it, action, new state.
