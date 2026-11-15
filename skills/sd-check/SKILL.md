---
name: sd-check
description: Run this repository's own check, test and lint entrypoints and report one typed result each.
disable-model-invocation: true
---

# sd-check

`bin/sd-check` owns no checks. It asks `sd_lib.detect_entrypoints` how *this*
repository spells `check`, `test` and `lint`, runs what it found, and reports
one typed result per name. Use it instead of guessing at `make test` or
`npm run lint`.

## Detection order

The `CLAUDE.local.md` marked block first if it names entrypoints, otherwise the
first of `Makefile`, `Taskfile`, `package.json`, `Cargo.toml`, `pyproject.toml`
that yields any. Detection is a library function with its own tests — do not
re-derive it, and do not "helpfully" run a command sd-check reported as
`absent`.

## The four statuses

| Status | Meaning |
|---|---|
| `pass` | ran, exited 0 |
| `fail` | ran and exited non-zero, **or timed out** |
| `skipped` | the entrypoint exists but this run deliberately did not use it |
| `absent` | this repository has no such entrypoint |

Only `fail` is a failure. **`absent` is not `skipped`, and neither is a
failure** — a repository with no lint target is not a repository that failed
lint. Do not report a run as incomplete because something came back `absent`.

The aggregate rule: when the repo defines its own `check`, `test` and `lint`
come back `skipped` with reason `covered by the check entrypoint`. That is
correct, not a gap. `--only lint` still runs one on demand.

## Flags

| Flag | Effect |
|---|---|
| `--json` | one machine-readable object (`schema` 1) |
| `--dry-run` | print what would run, exit 0 without running it |
| `--only NAME` | run exactly one of `check`, `test`, `lint` |
| `--timeout SECONDS` | per-check timeout, default 900; a timeout is a `fail` |
| `--base REF` | scope the run to the change since the merge base with `REF`; see Docs-only scope |
| `--record-receipt` | Explicitly record eligible full-check evidence in the shared database. |
| `--database PATH` | Select the receipt database; valid only with `--record-receipt`. |

## Docs-only scope

A repository may declare a docs-only scope on its repo row (sd:5109):

```sh
sd-db repo declare PATH check_scope '{"schema_version": 1, "docs_paths": ["docs/**", "*.md"], "docs_command": ["make", "docs-check"]}'
```

With `--base REF`, when every path changed since the merge base matches a `docs_paths` glob, only `docs_command` runs.
The three names then read `skipped` with reason `docs-only scope`, a fourth row `docs` carries the result, and `scope.mode` is `docs-only`.
A change to a Makefile, the entrypoint's source file, `CLAUDE.local.md`, or a file a check command names runs the full check.
The full check also runs without a declaration, without `--base`, or on an empty range.
A declaration that does not parse, or a database that does not answer, exits 2 and runs nothing.
The local gate and the review lane's gate check pass the pull request's base branch.

## Required services

A check that needs a running service, such as a database on a local port, declares it in the `CLAUDE.local.md` block:
`services: db=127.0.0.1:5433`, whitespace-separated `[name=]host:port` entries, and optionally `services_start: docker compose up -d db`.
Before any step, sd-check connects to each; one that does not answer within 2 seconds fails every check unrun, naming it and the start hint (sd:1649).
sd-check only probes; starting the service is the operator's step. A dry run and a docs-only scope probe nothing, and a malformed entry exits 2.

## Local gate receipts

Under `repo.ci = local`, `sd-ship prepare` runs the check as the merge gate does, and a pass leaves a gate receipt.
The merge gate at the same head and binding, within 30 minutes, reads it instead of running the check again; its status says `(reused)`.
Prepare reads one too: a pass that `sd gate check` or an earlier prepare left at the same head and binding stands (sd:1912).
`sd gate check` runs the gate's check at the committed `HEAD` and records a pass; a plain `make check` leaves no receipt.
The merge gate never writes one, and the window counts from the run that passed.
A gate that read no receipt and ran in full says why in `reuse_miss`: no receipt, a failed one, the binding fields that differ, or the expired window (sd:2602).
The gate child drops the agent harness's session variables and fnm's per-shell `FNM_MULTISHELL_PATH`, and resolves each `PATH` entry, so two sessions' passes at one head bind equal.
Inputs outside the repository are not bound: external makefiles, files a tool reads, machine state.
The short same-head window is the accepted residual risk; a repository that needs more uses the explicit contract below.
`bin/sd_gate_receipts.py` names the binding and `REUSE_WINDOW_SECONDS`.
These receipts need no declaration and are separate from the optional receipts below.

A repository whose check reads no commit history may key its gate receipts by tree instead of head:

```json
{"schema_version": 1, "key": "tree", "reason": "the check reads no commit message, range or tag"}
```

Declare it as `sd-db repo declare PATH gate_reuse '<json>'`; the gate reads the repo row (sd:5109).
A database that does not answer fails the gate unrun: an unknown key neither reuses nor records.
A new head with the same tree, and a merge base with the base branch of the same tree, then reuses the earlier pass.
The merge base binds by its tree, not its commit, so a predicted landing and the real one match (sd:2586).
That covers an empty commit, a reworded message, or a rebase that changed nothing (sd:1912).
Without the declaration a new head runs again, since a commit-message lint can pass at one head and fail at the next.
A run with no base branch or another `key` keeps the head key; `sd-db repo declare` refuses a declaration that does not parse.
A tree-keyed receipt stands for 6 hours (`TREE_REUSE_WINDOW_SECONDS`); a head-keyed one stays at 30 minutes.
An optional `"tool": "tree"` is for the pack gating itself (sd:2613); the gate ignores it in any other repository.

## Optional check receipts

Default invocations store nothing.
Use `--record-receipt` only for a proven complete, local-only check in a clean committed checkout.
The dependency declaration must be tracked at `.github/sd-check-reuse.json`.
Before recording or reusing evidence, read `skills/sd-check/references/check-receipts.md` in the sd-workflow checkout.
It defines the declaration, controlled environment, identity bindings, and refusal conditions.
Do not combine receipt recording with `--only` or `--dry-run`.
Recording a receipt does not enable reuse automatically.

The declaration is an operator assertion, not proof of arbitrary command hermeticity or filesystem isolation.
Do not enable reuse when the check depends on undeclared ignored files, network access, environment, or external dependencies.
Unknown dependencies mean run the check normally.

## Exit codes

`0` nothing failed; `1` a check or receipt-storage operation failed; `2` invocation or configuration was invalid.
Receipt-storage failure reports `receipt_error` and provides no reusable receipt, even when the underlying checks passed.

## Never

- **Never try to point sd-check at another checkout by a path option.** There
  is no `--repo` and there will not be one (R10-D6): the repository is resolved
  from the working directory and nowhere else, and `tests/test_verb_inventory.py`
  enumerates `bin/` to keep it that way. The one way to run it elsewhere is
  `sd-check -C <dir>`, which changes the working directory first, as `git -C`
  does; it exists on the lane commands only, because a permission layer that
  approves a command line sees `-C` and does not see a `cd <dir> &&` in front.
- **Never treat sd-check as a gate you can wave through.** `sd-review` runs it
  after a review that does not block, and a failing deterministic gate is
  a failing review: no change that does not build passes review.
- Keep wrapper effects separate from repository check effects.
  The wrapper changes no Git state and makes no network calls itself.
  Default execution stores nothing; explicit receipt recording permits only its shared-database checkpoint writes.
  Repository commands can have their own effects; receipt recording does not authorize unrelated writes or transmission.
- **Never substitute a hand-rolled command for a `fail` you did not like.**
  Fix the repo's entrypoint or report the failure.

## Code health

The registry's code rules are enforced by the tests their rows name, not by
sd-check; sd-check meets them as one more red result from the repository's
own `test` entrypoint. They are cited here because this is the skill an author
has open when that result arrives. Four are `tests/test_code_health.py`'s —
three per-function ceilings and a rule over pairs, each with a baseline that
may shrink and may not grow — and one is about where a file may live:

- R12-D1 — complexity, `COMPLEXITY_CEILING`, on the cyclomatic score.
- R12-D2 — length, `LENGTH_CEILING`, in unparsed statements.
- R12-D3 — depth, `DEPTH_CEILING`, in indented blocks.
- R12-D4 — duplication: two functions of one shape, once each is at least
  `CLONE_FLOOR` AST nodes.
- R11-D6 — shell placement: where a file with a shell suffix or shebang may
  live, held by `tests/test_no_shipped_shell.py`.

The rows in `bin/sd_rules.py` state each number and the one directory; none
is restated here.
`sd-rules --for <path>` prints the rows in scope for the file being
written.

## Prose rules

The suites named by each registry row enforce the prose rules.
The full suite makes each new violation red.
The changed-file selector currently omits `tests.test_prose_counts` for
the pack's `.claude/rules/sd-planning-adversarial-review.md`.
Each rule uses a per-document baseline that can shrink but cannot grow:

- R13-D1 — a citation into code names the symbol, `source:<path>::<symbol>`,
  or the file alone; every `path:line` into code counts.
- R13-D2 — a count of something the tree enumerates is derived, or carries
  the commit, pull request number or date it was measured at.

The rows in `bin/sd_rules.py` state what each rule exempts; it is not
restated here. `sd-rules --for <path>` prints the rows in scope for the
page being written.

## The store and plugin contract

`sd plugin add` and `sd store` are the two verbs a plugin author meets, and
the registry holds one rule about what they accept.
Write a kind with keys only from `KIND_KEYS` in `bin/sd` (eight), with
`fields` and `initial-status` required and the other six optional; a ninth key
is a decision record before it is a commit, and `validate_kind` turns a
manifest carrying one away by name (R11-D14).
A store block names `"driver": "db"`: the rows are the source, `sd store`
reads and changes them. Each base also has one HTML page in the vault. The `vault`
driver is gone, and a manifest that names it is refused with the fix.
The rows in `bin/sd_rules.py` state each rule; none is restated here.
`sd-rules --for <path>` prints the rows in scope for the file being
written.

## Reading the output

Output is captured and attributed per check, never interleaved, and tails at
4,000 characters with a truncation marker. When reporting to the user, quote
the shortest decisive line of that tail rather than the whole block.

A failing check also keeps its whole output in a file under the Git common
directory, `sd-check-output/`, which holds the newest 20. The report names it
as `output_path` and the human output as `whole output:`. `failed_shards`
lists each `shard <name>: <n>s exit=<code>` line with a non-zero code from
the whole output, so a shard that failed early is named though the tail no
longer reaches it.

A failing check also names every step that failed in `failed_steps`, and is
never empty: each failed shard, each suite a runner lists as `<tool>: failed:
<suite> ...`, and each make target from `make: *** [<target>] Error <n>`.
A failure that names none of these reads `<name> exit <code>`, or the reason
the check did not finish, such as a timeout. `failure` holds the failing part
of the output: each failed shard's own block, else each stream's failure lines
(`FAIL:`, `ERROR:`, an exception, `FAILED`) and its tail. The human output
prints them as `failed step:` and `[failure]`; `sd gate check` adds the steps
to its one-line summary, then `whole output: <path>`. The `sd/local-gate`
status it posts leaves that local path out.
