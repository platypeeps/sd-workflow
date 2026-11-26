# Open pull requests

Run on `/sd-ship open-prs`, or when the operator says "check open PRs" (sd-next routes it): every open PR in this repo and its companion repos.

1. List them with `mcp__github__list_pull_requests`, and read each one's state with `pull_request_read` `get`.
2. For each PR, find what holds it: a failed `sd/local-gate`, open review findings, a merge conflict, a failed lane entry, or no lane entry.
3. Skip, and only report, a PR in either case:
   - its builder or its lane entry is still running: touching it races that writer;
     this read can go stale, so each step-4 command checks again in its own write, and its refusal is a skip too;
   - its repo is one the operator does not manage (no row in `sd-db repo list`): report it, and act on nothing there.
4. Act on the rest:
   - A finding blocks only when it names a concrete failing input, command or interruption that a real caller reaches.
     File every other finding as an sd item for the repo's next PR.
   - Send a blocking finding with `sd assignments build N --finding "<finding>"`, the dashboard's Fix, and let the command decide:
     never predict from a build's status whether it takes one.
     When no builder can take it, it refuses with `--finding goes to a Retry or a Fix` and writes nothing:
     then record the finding with `sd task note N "<finding>"` and report the PR.
     Never push by hand to a branch the lane holds.
   - Past round 3, counting builder and lane rounds, record fix, file or reject for each blocker with `sd task note N "<fix, file or reject: reason>" --kind decision`.
   - Rebut a finding that doesn't hold, with a reason a reader can check.
   - Retry a failed lane entry once its cause is fixed: `sd-ship -C <worktree> lane retry N --expected-head <head>`, `<head>` this PR's full head commit.
     Retry takes the item's newest entry, which may belong to another PR of the item, and checks `<head>` against it in its own write:
     an entry at another head refuses `stale_head` and queues nothing. Report that; never read the entry first and retry without the head.
     No flag names the branch, so read the retry's answer: if the queued entry's `branch` is not this PR's head branch,
     take it out with `sd-ship -C <worktree> lane cancel N --expected-revision <revision>`, the `revision` of `sd-ship -C <worktree> lane list`, and report.
   - Enqueue a PR that passes, from the worktree on its head branch, at the head step 2 read.
     Do not look for a lane entry first: enqueue refuses an item already queued, and queues nothing.
     `sd-ship -C <worktree> lane enqueue --item N --deliver|--associate-only --title "<title>" --body-file <body> --expected-head <head>`.
     The first enqueue of an item refuses without its claim: `--deliver` when this PR is the item's last, `--associate-only` when a later PR finishes it.
     `<title>` is the PR's title and `<body>` a file holding its live body.
     Enqueue reads the worktree's branch and head itself and refuses a head that moved since step 2: read the PR again.
     If the queued entry's `branch` is not this PR's head branch, cancel it as for a retry, and report.
5. Ask the operator first, one question at a time, before you close a PR, merge one by hand, or request a person's review.

End with a table: PR, item, what held it, action, new state.
