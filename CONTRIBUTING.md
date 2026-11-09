# Contributing

Use Homebrew Python 3.15 for the local virtual environment on macOS.
The `requires-python` floor stays 3.13; tests run on 3.15 only.
Use Git 2.31 or later: the pack and its tests call `git ls-files --deduplicate`.

## Setup

```bash
make setup
```

This command creates `.venv`, installs the pinned requirements, and provisions the shared library.
It also copies the two requirements files into `.venv/sd-requirements/`, which records what the environment was provisioned from.
A linked worktree with no `.venv` borrows the main checkout's, and borrows it only while those copies match its own requirements files.
A worktree that moved a pin is refused by name and told to run `make setup VENV=.venv` for an environment of its own.
A `.venv` that is a symlink into another checkout is the same borrow under a local name, so it is compared too, on a proven mismatch only.
A symlinked environment that records nothing is used anyway, and `make` prints one note naming the checkout to re-provision (sd:1349).
`make setup` detaches a symlink at the path it provisions, and says what it detached; the environment the link pointed at is left alone.
It also removes the record before it changes anything and republishes it only after the last step, so a provision that failed leaves no record to match.
Because a missing record would otherwise mean both "provisioned before this was recorded" and "being built right now", `make setup` writes `.venv/sd-provisioning` before its first change and removes it after the record is published.
An environment carrying that file is refused by every path -- the borrow, the symlink and a real local `.venv` alike -- and the refusal names the checkout to run `make setup` in, which rewrites the marker and clears it on success.
`bin/sd_lib.py` and `hooks/pre-commit` read the same file and treat such an environment as absent.
A `.venv` the checkout carries as a real directory is its own environment and is never compared against the record.
The requirements files use `--require-hashes`, in `make setup` and in the local gate.
To update a dependency, change its pin and run the compile command from that file's header:

```bash
uv pip compile --universal --generate-hashes --python-version <floor> <file> -o <file>
```

`<floor>` is the `requires-python` lower bound in `pyproject.toml`; the resolver pins only releases that support it.
`tests/test_requirements_target.py` fails when a file's header names another version (sd:1391).

Remove a conflicting transitive pin before recompiling. Do not edit hashes by hand.

## Local Checks

```bash
make test
make lint
make precheck
make audit
make docs-lint
make check
```

`make check` runs `lint`, `audit`, `docs-lint`, then `test`. It stops at the first failure.
Run the full command before each push.

`make precheck` runs `lint` and then each always-run test module, in about a minute.
`sd-check` runs it before it waits for a gate slot, and stops there when it fails.
The output names the failing check: make's `lint` error line, or each failing module.

During development, select tests for changed paths:

```bash
make check CHANGED="$(git diff --name-only origin/main) $(git ls-files --others --exclude-standard)"
```

Only a command-line `CHANGED` value selects this mode.
The test runner ignores this mode when `CI` or `GITHUB_ACTIONS` has a non-empty value.
Do not use this mode for paths containing whitespace.
The selector splits the list on whitespace and can miss the intended tests.

The selector runs matching modules and an always-run set of checks.
It cannot detect dependencies that tests do not name.
An empty list, unknown path, or broad change selects the full suite.
Read `.github/scripts/select-tests.py` for the selection rules.

A narrowed run skips coverage combination and the installer coverage gate, then exits 2.
The other three checks still run over their full scope.
A narrowed run does not replace the full check before a push.

`.github/scripts/run-tests.sh` runs the shards at `nice -n 10` locally.
Its local workers are the CPUs divided by the machine's gate cap, at least one: 5 at the cap of 3 on 16 CPUs.
With no cap it uses half the CPUs.
It uses every CPU when `CI` or `GITHUB_ACTIONS` has a non-empty value.
Set `TEST_WORKERS` to override either default.
Each shard reports its name, elapsed seconds, and exit status.

Ruff checks `bin/` and `tests/`. Mypy checks `bin/`.
The Makefile owns both path inventories.
Missing optional ShellCheck, Bandit, or Zizmor tools produce warnings.
Use `STRICT=1 make check` to make missing tools fail.
This also requires a bash 3.2 interpreter.

The local lint parses tracked `*.sh` files through `.github/scripts/check-bash32-syntax.sh`.
The script enumerates its inputs from git.
Set `SD_AI_COMMAND_PACK_BASH32` to override its space-separated interpreter candidates.
Without bash 3.2, the script warns and passes unless `STRICT=1`.

Installer coverage requires **100% line and branch coverage** over `bin/sd_install.py`.
The gate also checks file and statement floors.
Lower a floor only in the pull request that reduces the measured implementation.

## Main Branch Policy

Every change to `main` requires a pull request.
Branch protection on `main` requires a pull request, the strict `sd/local-gate` check, and enforce_admins.
It requires no approving review, because the sole maintainer cannot approve their own pull request.
The pack's `status` declaration on its repo row records that accepted `reviews` gap, its reason, and the condition that ends it.
Read it with `sd-db repo declare <checkout> status`.
Run `bin/sd-status` to inspect live protection.

Use the pack's ship workflow for publication, review and merge.
`sd-ship merge` checks the protection object, the exact-head checks and the review findings before it merges.
This repository has `repo.ci = local` and carries no GitHub Actions workflow.
`sd-ship merge` runs `sd-check` in a fresh worktree and posts `sd/local-gate` on the head commit.
WORKFLOW.md "No-CI mode" describes that gate.

The gate runs on the maintainer's machine only.
Nothing verifies other Python versions or operating systems.
Treat gate results as evidence from the tested machine only.

## Payload Rules

- `v0.72.0` is the terminal release; main is the release. Do not add release tags or a changelog.
- `skills/sd-*/SKILL.md` holds the payload. Edit that source directly.
- `python3 bin/sd_install.py --user` renders the skills during installation. The repository has no generated platform copies.
- `python3 bin/sd_install.py --status` reports the serving checkout's commit; in the serving tree it also counts the commits behind `origin/main`.
  Run `make setup` in your working checkout to move the serving tree; `--pull` refuses a detached tree.

## Repository Conventions

### Command shape

A verb takes a positional id, then a positional value, and flags for the rest; an `item` positional takes `sd_notes.item_id` (sd:3500).

### Documentation and decisions

Use Claude's STE-Concise style for new and revised prose:

- Use active voice and simple tenses.
- Limit each sentence to 20 words and one idea.
- Remove filler. Preserve exact identifiers, paths, commands, and quoted evidence.

Keep one authoritative statement of each current rule or decision. Link to it from other documents.
Governing documents contain current instructions and, when needed, one sentence explaining the reason.
Put dated evidence in the existing work item's history. Preserve original review findings and their dispositions there.
Update the current status in place. Record a history entry only when the decision, scope, or evidence materially changes.
Do not append repeated corrections or copy decision discussions between documents.
Git history preserves earlier wording.

### Citations

Cite code declarations by symbol: `source:bin/sd-docs-lint::check_shape`.
The citation check requires one matching declaration.
For a file without a supported declaration, name the file in prose.
Use `path:line` only for markdown targets. Keep historical citations tied to their original version.

Preview citation repairs before applying them:

```bash
python3 tests/test_doc_citations.py --repoint
python3 tests/test_doc_citations.py --repoint --apply
```

The repair tool matches anchored text. It refuses ambiguous or missing matches.
Do not replace line numbers without checking their targets.

#### The optional review-tier reading

`bin/sd-review` takes an optional second reading through `jev`. `sd_route.route` decides the tier and
keeps the decision; the reading is a second opinion over a diff shape the
policy's globs cannot see, and it is taken wherever `jev` says it can answer.
It needs no per-repository opt-in, because it sends no prose.

What leaves the machine: the tier names, the repository-relative paths the
change touches (capped, then a count), the number of lines it moves, and the
routing reason `sd_route` composed. **No file contents, no diff, no code**, and
no absolute path, repository name, branch, author or commit message.

**The tier names are the running checkout's, not this one's.** `load_policy` reads the policy out of the
repository the command runs in, and `sd-review` hands that policy's
`tier_order` straight to the reading. The four standard tiers carry a fixed
description; **a tier a repository invented is sent as a bare name, with
nothing to say what it means.** So a private repo that declares a tier called
`embargo-legal` sends that string to a third-party model, and the name is the
whole of what arrives. Name your tiers as though they leave the machine,
because they do.

```bash
JEV_SD_REVIEW=0 sd-review --scope branch    # or export it once
```

The same vocabulary and the same subtract-only rule apply. **It prints no
*note*, which is not the same as changing nothing**: on the path where the
reading works, the result object gains a `jev` key, the routing reason gains a
clause, and the tier itself may move — that is the point of taking it. The
note naming this switch appears only when the reading was attempted and
failed.

#### The shadow readings: finding triage and duplicate hints

Two more stages ask Jev with `--shadow`: `jev` prints the answer handed to it
and records its own judgment beside it in the ledger. Nothing reads the
judgment back, so neither changes an output, an exit code or a status.

- `JEV_SD_REVIEW_TRIAGE` (sd:2092): after a review with findings, `sd-review`
  asks for each of the first ten whether it is correctness, robustness, style
  or likely wrong. Sent per finding: severity, disposition, family,
  repository-relative path and summary.
- `JEV_SD_TASK_DEDUPE` (sd:2093): after `sd task add` files a row in a
  checkout, it asks which open item of that repository, if any, tracks the
  same work. Sent: the new title, and the ids and titles of up to forty open
  items.

**The local Kev by default.** Both call `jev --local-only`: a public origin does not make a review summary or
a tracker title public. Hosted Jev needs `SD_JEV_SHADOW_HOSTED=1` in the
machine's environment, which no commit can set. A failed local call sends
nothing, and nothing retries it hosted.

**Public repositories only**, on either path. Each asks GitHub whether
`origin` is a public github.com repository, after `jev enabled` says a reading
could be taken, and sends nothing unless the answer is `private: false`. No
`gh`, a failing `gh` and a private answer all read as private.

**The hint-blind twin follows the tier reading instead.** `JEV_SD_REVIEW_BLIND`
(sd:2969) asks, after the tier reading's gate passes, the same tier question
without `deterministic_routing_said`, so the ledger shows how far the rule's
reason steers the answer. Sent: the reading's state less that reason. It sends
less than the reading, so it goes where the reading goes, hosted Jev and private
repositories included. `--shadow` prints the routed tier, and nothing reads
Jev's answer.

```bash
JEV_SD_REVIEW_TRIAGE=0 JEV_SD_TASK_DEDUPE=0 JEV_SD_REVIEW_BLIND=0    # any, for a run or a shell
```

#### Subject and run in the ledger

Every `jev` call names what it judged with `--subject`, so a later outcome can
join the judgment, and inherits one `JEV_RUN` per run (sd:2954).
`sd_lib.jev_env` keeps an inherited `JEV_RUN` or sets
`<caller>-<UTC yyyymmddThhmmss>-<4 hex>` once per process.
A subject holds no content: it is an existing id, or the first 16 hex of the
SHA-256 of a stable identifier.

- `sd-review` tier and triage, and `sd task add`: unchanged subjects.
- `sd-docs-lint`: `sd-docs-lint:<16 hex>` over the batch's sorted origins
  (`<item>/<file> `<citation>``), one per line; the weak-reading note names
  each origin, so an outcome recomputes the key from it.
- `sd-fact-check` and the prose scores: the skill pages show the key and
  the run id the agent sets.

### Permissions

Put machine-specific rules in ignored `.claude/settings.local.json`.
Put repository workflow rules in tracked `.claude/settings.json`.
Do not grant arbitrary execution or generic file readers through Bash permissions.
Use scoped commands and the harness's file tools.

`tests/test_permission_allowlist.py` derives the tracked allowlist from three inventories:

- Public Makefile `.PHONY` targets.
- The README installer table.
- GitHub MCP tools named by shipped skills.

Change the relevant inventory instead of editing grants by hand.
The derivation excludes modes with path placeholders and effects that begin with `Remove` or `Delete`.
Only README surfaces marked `Read-only:` receive wildcards. Other grants match exact commands.
Every granted path must exist.
Executable Python entrypoints require both direct and `python3` invocation forms.
An added `./` prefix can change permission matching.

Edit `.gitignore` directly. No generator maintains it.

## Specs To Read First

Read the [current architecture](docs/current-architecture.md) before changing the installer or command set.
That page links the historical design and implementation records.

Some surviving `docs/spec/**` pages describe the retired installer model.
Read each page's dated notice before using it.
The Machine-Scope Installer section's final location remains undecided.
Its current location is `docs/spec/backend/manifest-and-filesystem.md`.

Keep an `index.md` that links every surviving page in each spec directory.
Spec changes remain subject to review through `never_skip`.
Update a spec on the branch whose change alters what it documents.
Keep `docs/fleet/README.md` as the historical link target.
Preserve the marked historical review quotations in `docs/review-learnings.md`.
