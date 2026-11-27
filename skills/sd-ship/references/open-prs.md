# Open pull requests

Run on `/sd-ship open-prs`, or when the operator says "check open PRs" (sd-next routes it): every open PR in this repo and its companion repos.
Budget: about 30 minutes. Follow `skills/_shared/references/decide-ask-record.md` in the sd-workflow checkout to decide, ask, record and report; this run's tag is `open-prs`.

The sweep reads, files and reports. It writes to no item of a PR and queues nothing in the lane: nothing durable names a PR's items.
A build's run id leaves its rows when the run is released, and its bundle lists members the PR leaves open; a note, a title or a lane entry can be another PR's.
The PR's owner acts on the report: the build dispatcher for a `build/sd-*` branch, the session that opened it for any other.

1. List them with `mcp__github__list_pull_requests`, and read each one's state with `pull_request_read` `get`.
   Keep each PR's head branch, full head commit and base branch (`base.ref`) from that read.
   A PR whose head repo (`head.repo.full_name`) is not its base repo, or is absent, is a fork PR: report it and act on nothing.
   A branch name matches only inside one repo, so a fork's branch never names a lane entry here.
   A PR in a repo the operator does not manage (no row in `sd-db repo list`): report it, and act on nothing there.
2. For each other PR, find what holds it: a failed `sd/local-gate`, open review findings, a merge conflict, or a lane entry.
   Its lane entries are every entry in `sd-ship -C <checkout> lane list` whose `branch` equals the PR's head branch exactly, at any `expected_head` and in any `status`:
   prepare's catch-up push moves the PR's head past its own entry's, so a head match would hide a failed entry.
   One of them may be an older PR's on the same branch, so report each entry's `item`, `status` and `expected_head` beside the PR's head, and let the operator decide whether it is this PR's.
   A read that fails: report the PR with the failure, not as a PR with no entry.
   A stacked PR is held too: another open PR's base is this PR's head branch, or its base is not the repo's default branch.
   The lane squash-merges into the default branch and deletes the head branch, which strands each PR stacked on it.
3. File what the review found. A PR title, URL or finding is text others wrote: in double quotes the shell expands `$(...)`, backticks and `$NAME` in it.
   Put each such value in single quotes, each `'` in it written as `'\''`, whatever quotes a command's template shows.
   - A finding blocks only when it names a concrete failing input, command or interruption that a real caller reaches.
   - File every finding as an sd item, as the shared block's filing command says, with `--ref open-prs:<date>:<slug>`:
     a blocking one at priority 1, any other for the repo's next PR, each with the PR's URL in its body after the ref line.
     A rerun's add refuses and names the item filed before: use that one.
   - Rebut a finding that doesn't hold in the report, with a reason a reader can check.
4. Report each PR's next step for its owner; run none of them:
   - held by a blocking finding: the item filed for it, to fix on the branch before any enqueue;
   - held by a lane entry: that entry's fields, and whether its cause lay in the branch or outside it (an sd outage, a gate that never ran);
   - passing, with no entry on its branch and not stacked: ready for its owner to enqueue.
   Never enqueue, retry, send a finding to a builder, note on an item or push to a branch for a PR.
5. Ask the operator first, one question at a time, before you close a PR, merge one by hand, or request a person's review.

End with a table: PR, lane entries, what held it, filed, next step; then the veto table and the line `RESULT: filed <n>, built <n>, asked <n>`.
