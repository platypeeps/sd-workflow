# The mechanical half of the documentation review

`actions/docs-gate` runs `sd-research-kit` against the repository the workflow is checked out in.
It reads that checkout, writes nothing to it, and requests no reviewer.
By default it runs `review` and then `checklinks`.

Set `verbs` to choose others, space separated and in order.
`review`, `checklinks`, `pins` and `conventions` are accepted.
`render` is refused: it writes, and this action promises it does not.
An unknown verb stops the run rather than being skipped, because a gate that silently ran nothing is the failure this action exists to avoid.

Every named verb runs even when an earlier one fails, so one report names every problem instead of only the first.
The action exits non-zero if any of them did.
An empty `verbs` is refused rather than treated as "nothing to do": a gate that reports a pass on nothing is worse than no gate.

## Trust

This action executes `research.conf.py` from the checkout it runs against, because that is how `sd-research-kit` reads the document list (`source:bin/sd_research_review.py::load_docs`).
Run it only where the checked-out code is trusted.
On a `pull_request` trigger that means the fork's code runs on your runner with the job's permissions, so keep `permissions: contents: read` and never combine this action with `pull_request_target` or with secrets the fork should not reach.

## Why an action rather than a checkout

The kit lives beside this action, outside the workspace, and needs no `PATH` entry: `sd-research-kit` resolves its own helpers from the directory it sits in, and resolves the repository it checks from the current working directory.

That placement is the point for `checklinks`, which walks the tree it is standing in.
A consumer that checked this pack out into its own workspace had to move the checkout aside afterwards, or the pack's own markdown landed in the consumer's link report.

## Use

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
- uses: platypeeps/sd-workflow/actions/docs-gate@<full commit sha>
```

Both refs are full SHAs, as everything in this repository's own workflows is.

`review` runs `sd-docs-lint` over `docs/work/` only in a repository that also has `research.conf.py`.
`sd_research_review.check()` returns success at `source:bin/sd_research_review.py::check` when that file is absent, before it reaches the work items, so a checkout that follows the workflow's work items and not `sd-research-repo` is not linted by this action and should call `sd-docs-lint` directly.
A research repository with no `docs/work/` never pays for the lint.
