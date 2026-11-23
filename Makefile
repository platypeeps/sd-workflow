BREW_PYTHON ?= /opt/homebrew/bin/python3.15
PYTHON ?= $(shell if [ -x "$(BREW_PYTHON)" ]; then printf '%s' "$(BREW_PYTHON)"; elif [ -x /usr/local/bin/python3.15 ]; then printf '%s' /usr/local/bin/python3.15; elif [ -x /opt/homebrew/bin/python3 ]; then printf '%s' /opt/homebrew/bin/python3; elif [ -x /usr/local/bin/python3 ]; then printf '%s' /usr/local/bin/python3; else command -v python3; fi)
# `make setup` provisions one virtualenv, into the checkout it ran in, so a
# linked worktree has none and every recipe below died on `.venv/bin/python:
# No such file or directory`. `--git-common-dir` names the shared git
# directory, whose parent is the main checkout from any worktree -- so a
# worktree borrows the virtualenv that was actually provisioned, and a
# checkout with its own still uses its own. Overriding VENV skips all of it.
#
# Borrowing is sound only while the borrowed environment holds what this
# working tree asks for. A branch that moves a pin would otherwise lint, test
# and audit against another branch's versions, and `audit` would keep claiming
# it ran the requirements-security.txt pin while running something else. So
# `setup` leaves a copy of the two requirements files inside the environment it
# provisions, and a borrow is allowed only where those copies are byte for byte
# this tree's; anything else refuses by name instead of borrowing. Copies and
# not hashes: `cmp` is on every machine and costs no interpreter start, the
# comparison is the whole check, and whoever hits the refusal can `diff` the
# two files to see what moved. An environment provisioned before this change
# carries no copies, so it is refused rather than borrowed -- `make setup` in
# the checkout that owns it is what gives it a record.
#
# A VENV given on the command line or in the environment is a deliberate
# choice and is never second-guessed. A `.venv` the checkout carries as a real
# directory is its own environment and is not checked either.
#
# A `.venv` that is a *symlink* into another checkout is neither: it is this
# same borrow wearing a local name, and sd:1020 made it the usual way a
# worktree here gets an environment. So it is compared too -- but only on a
# proven mismatch. An environment with no record is left alone on that path,
# because the link is not this Makefile's doing and refusing would strand
# every checkout whose environment predates the record; the day that
# environment is provisioned again, the symlink case is covered like any
# other. The automatic borrow is strict instead: it is this Makefile reaching
# for an environment nobody pointed it at, so an environment that cannot say
# what it holds is refused rather than assumed to fit.
#
# The lenient half says so out loud, on stderr, once per make and without
# touching an exit code. Every worktree on this machine reaches its
# environment through an sd:1020 symlink and every one of those environments
# predates the record, so today the rule is correct and covers nothing in the
# case that actually occurs. A check whose passing output cannot be told from
# the check not running is the failure this repository names by name. sd:1349
# tracks re-provisioning that environment and whether the symlink convention
# retires; the note is what makes that row findable from a `make` run instead
# of from an exchange nobody reads.
#
# `sd-provisioning` is the in-flight marker, and it answers a question the
# record alone cannot. Since `setup` deletes the record before it mutates
# anything, a missing record meant two different things: an environment
# provisioned before this repository recorded anything, which is safe to
# borrow, and one that is half-built right now or died half-built, which is
# not. The marker separates them. It is written before the first mutation
# and removed after the record is published, so every instant in which the
# environment is inconsistent is an instant in which the marker is there.
# Absence of the record *without* the marker keeps its old meaning exactly:
# legacy, borrowed, and said out loud.
#
# A marked environment is refused by every path here -- the strict borrow,
# the sd:1020 symlink, and a real local `.venv` alike -- because "this
# environment is being rebuilt" is one fact, not three, and a reader who has
# to remember which paths honour it will guess wrong. The refusal names the
# checkout to finish it in, not `VENV=.venv`: the remedy is to run `make
# setup` again, which rewrites the marker and clears it on success, so a
# `make` killed mid-provision leaves a worktree that is refused and not one
# that is bricked.
#
# One shell for the whole decision, run once per make (`:=`), not once per
# expansion. A checkout with a real `.venv` pays one `test -x` and one
# `test -L`, and reaches neither git nor `cmp`.
#
# None of it applies to the local gate (sd:1918). `bin/sd_local_gate.py` runs
# `sd-check` in a fresh detached worktree with `SD_LOCAL_GATE=1` exported, and
# there a borrowed environment is exactly what must not pass: it is the
# operator's, and its requirements are whatever `make setup` last installed
# rather than the commit under test's. So gate mode never borrows. VENV is
# `.venv` in the worktree, whatever the caller exported, and every lane first
# runs `gate-env`, which builds it; `sd_db` comes from the worktree's own `lib/`
# on the path, never from the environment (sd:3280). The variable is not passed on to recipes: the suite runs `make` in its
# own fixtures, and those must see the ordinary rules.
ifeq ($(SD_LOCAL_GATE),1)
override VENV := .venv
GATE_ENV := gate-env
unexport SD_LOCAL_GATE
else
BORROWED := $(shell \
  venv=.venv; name=.venv; record=; strict=; \
  if [ -x .venv/bin/python ]; then \
    if [ -L .venv ]; then record=.venv/sd-requirements; name=$$(readlink .venv); fi; \
  else \
    common=$$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null); \
    main=; [ -n "$$common" ] && main=$$(dirname "$$common"); \
    if [ -n "$$main" ] && [ -x "$$main/.venv/bin/python" ]; then \
      venv="$$main/.venv"; name="$$venv"; record="$$venv/sd-requirements"; strict=yes; \
    fi; \
  fi; \
  own="run 'make setup VENV=.venv' to give this worktree its own environment (a linked worktree serves nothing unless SERVE=yes)"; \
  dir=$${name%/*}; where="in $$dir"; [ "$$dir" = "$$name" ] && where="here"; \
  why=; \
  if [ -x "$$venv/bin/python" ] && [ -f "$$venv/sd-provisioning" ]; then \
    why="$$name is mid-provision, so nothing may be taken out of it; run 'make setup' $$where to finish it"; \
  elif [ -n "$$record" ] && [ ! -d "$$record" ]; then \
    if [ -n "$$strict" ]; then why="$$name does not record what it was provisioned from; $$own"; \
    else printf '%s\n' "note: $$name records no provisioning; this worktree's requirements are unchecked. Run 'make setup' in $$dir to cover it." >&2; fi; \
  elif [ -n "$$record" ]; then \
    for f in requirements-dev.txt requirements-security.txt; do \
      cmp -s "$$f" "$$record/$$f" \
        || { why="$$f here is not the file $$name was provisioned from; $$own"; break; }; \
    done; \
  fi; \
  if [ -n "$$why" ]; then \
    printf '%s' "refusing-to-borrow: $$why"; \
  else printf '%s' "$$venv"; fi)
# The refusal fires when a recipe asks for the interpreter, not at parse time:
# `setup` is the remedy and must stay runnable in a worktree that is refused,
# and SETUP_VENV below reaches `$(VENV)` only when VENV was set deliberately.
VENV ?= $(if $(filter refusing-to-borrow:,$(firstword $(BORROWED))),$(error $(BORROWED)),$(BORROWED))
endif
VENV_PYTHON = $(VENV)/bin/python
VENV_BIN = $(VENV)/bin
# Borrowing is for commands that *consume* an environment. `setup` creates one,
# and a borrowed path would make it rewrite the main checkout's virtualenv --
# installing this branch's requirements over whatever another session is
# running on. So setup provisions the checkout it stands in, unless VENV was
# set deliberately, which `origin` is what distinguishes from our own default.
# Trailing slashes come off first: `[ -L link/ ]` is false, so `VENV=link/`
# passed the detach and the rebuild wiped the link's target (sd:5254). `/`
# strips to empty, which setup refuses.
setup_path = $(if $(filter %/,$(1)),$(call setup_path,$(patsubst %/,%,$(1))),$(1))
SETUP_VENV = $(call setup_path,$(if $(filter command line environment,$(origin VENV)),$(VENV),.venv))
SETUP_PYTHON = $(SETUP_VENV)/bin/python
# Why setup leaves the machine alone, or empty to serve it. The serving tree
# takes this checkout's HEAD, so a linked worktree's setup served its unmerged
# branch to every sd command (sd:5551). A git that cannot say counts as a
# worktree. A SERVE given on the command line or in the environment decides
# alone. Lazy, so only setup pays the two `git` calls.
SERVE_SKIP = $(if $(filter undefined,$(origin SERVE)),$(shell \
  dir=$$(git rev-parse --path-format=absolute --git-dir 2>/dev/null) \
    && common=$$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
    && [ -n "$$dir" ] && [ -n "$$common" ] \
    || { printf '%s' "git cannot tell whether $(CURDIR) is a linked worktree"; exit 0; }; \
  [ "$$dir" = "$$common" ] || printf '%s' "$(CURDIR) is a linked worktree"))

.PHONY: setup hooks fonts test lint precheck audit docs-lint check gate-env post-merge

# The record BORROWED reads lives inside the environment, so `rm -rf .venv`
# takes both and a record can never outlive what it describes. The rest of
# this recipe is about the two moments where that is not enough.
#
# It is removed before the first mutation and published after the last, not
# merely written last. Written last is right for a fresh environment and wrong
# for a re-provision: the previous run's record sat there while pip
# changed the packages under it, so a failing step after it
# aborted `make` and left a record that still matched
# requirements the environment no longer held. A worktree comparing against it
# passed the borrow check and ran the wrong versions in silence -- the exact
# failure the record exists to prevent, reached by the record itself.
# Publication is a rename of a directory built beside it, so a record that
# exists is a record that is complete.
#
# The symlink is detached first, and out loud. `python -m venv` refuses a path
# that is a symlink -- `Error: Unable to create directory '<path>'`, exit 1,
# pinned against the real interpreter in tests/test_sd_lib.py -- so
# `make setup VENV=.venv`, which is what the borrow refusal above tells a
# reader to run, failed in precisely the configuration that produced the
# refusal: a worktree whose `.venv` is an sd:1020 link. Detaching removes the
# link and never what it points at, and it says what it detached, because
# replacing somebody's deliberate link in silence is worse than the error it
# repairs. It has to come before the record is removed, too: through a link,
# `$(SETUP_VENV)/sd-requirements` is the *other* checkout's record.
# Every command in this block carries `|| exit 1`, and that is not belt and
# braces. A recipe line is one shell and `;` between commands, with no
# `set -e`: a command that fails is stepped over, and the line's status is the
# last command's. Both defects found in review were that. The redirection
# writing the marker could fail and the `rm -rf` below would run anyway, on a
# line that then exits 0 -- record gone, no marker, and the borrow paths read
# the absence as legacy, which is the state the marker exists to end. And `rm`
# on the link could fail while `mkdir -p` on a symlink to an existing
# directory succeeds silently, so the marker would be written *through* the
# surviving link and the `rm -rf` would take the record out of the environment
# every other worktree borrows from. That one leaves this checkout and is why
# the detach also asserts its own result rather than trusting an exit code:
# the invariant is that nothing is provisioned through a link, and a race that
# recreates one does not announce itself in `rm`'s status.
#
# The marker goes in with one write, before anything is touched: `mkdir -p`
# then the file, and only then the `rm -rf` and the `python -m venv`. That
# covers both windows at once. On a fresh provision the directory holds no
# `bin/python` until `venv` runs, so nothing can borrow it before the marker
# exists; on a re-provision `bin/python` is there from the start, but the
# first mutation is the `rm -rf` below and the marker precedes it. CPython's
# `venv` accepts a directory that already exists and leaves files it did not
# write alone -- measured, not assumed -- which is why the marker survives
# the step that rebuilds the interpreter around it.
#
# An environment another Python built is emptied, all but the marker, before
# `venv` runs (sd:5254). venv keeps a `bin/python` it finds, so over a 3.14
# environment python3.15 rewrote `pyvenv.cfg` and pip still ran 3.14, into
# `lib/python3.14`. The version is asked of each interpreter, not read from
# `pyvenv.cfg`, which that run had already made lie; an interpreter that does
# not answer is a mismatch, and a PYTHON that does not answer stops the run
# before anything moves. `bin/` goes last, so a run stopped mid-wipe leaves
# the old interpreter there for the next run to find and finish. Three
# refusals come before any mutation: a PYTHON inside the environment, which
# the wipe would delete before `venv` ran it, or inside the target of a link
# the detach would cut it off from, both compared as physical paths; and a
# non-empty directory with neither `pyvenv.cfg` nor the
# marker, such as `/usr/local`, which is not ours to empty. The marker counts
# because a stopped wipe may already have taken `pyvenv.cfg`.
#
# Every guard and the wipe act on one resolved path, never on VENV as typed:
# `[ -L env/. ]` is false while `env` is a link, so a lexical check let the
# wipe reach another checkout's environment (sd:5254, second round). The leaf
# must be a name, not `.` or `..`; the directory holding it is resolved once
# with `cd -P`, so a symlinked parent, `..`, `//` and a relative or outside
# path all land on the directory the kernel would open, and `[ -L ]` then sees
# the leaf itself. A parent that does not exist holds nothing to delete. The
# recipe lines after this one open `$(SETUP_VENV)` through the same kernel
# walk, and by then the leaf is a real directory, so they reach the same one.
#
# No `sd_db` goes into the environment: `bin/` reads this checkout's `lib/`
# (sd:3280). The uninstall takes out a copy an older `make setup` or a
# satellite self-install left, so a caller that still reads the environment
# fails with `ImportError` instead of running stale code.
#
# The last step serves the machine (sd:3009). The commands run from a plain
# clone that nobody works in, at this checkout's HEAD (sd:3111). In the clone,
# `--serve` renders it; in any other checkout, it clones the tree the first
# time, fetches this HEAD into it, detaches there and runs this recipe there.
# `SERVE=no` skips the serving step. A linked worktree skips it unless SERVE
# is set (sd:5551): its HEAD is unmerged work. README "A dedicated serving
# tree" has the rest.
setup:
	@venv="$(SETUP_VENV)"; name="$$venv"; \
	  [ -n "$$venv" ] || { printf '%s\n' "error: VENV is empty; there is no path to provision" >&2; exit 1; }; \
	  case "$${venv##*/}" in .|..) printf '%s\n' "error: VENV=$$name ends in '$${venv##*/}'; name the environment's own directory; nothing was changed" >&2; exit 1;; esac; \
	  parent=$$(dirname "$$venv"); \
	  if [ -e "$$parent" ] || [ -L "$$parent" ]; then \
	    parent=$$(cd -P "$$parent" && pwd -P) \
	      || { printf '%s\n' "error: cannot resolve the directory that holds $$name; nothing was changed" >&2; exit 1; }; \
	    venv="$${parent%/}/$${venv##*/}"; \
	  fi; \
	  probe='import sys; print("%d.%d" % sys.version_info[:2])'; \
	  want=$$("$(PYTHON)" -c "$$probe") && [ -n "$$want" ] \
	    || { printf '%s\n' "error: $(PYTHON) did not report its version; nothing was changed" >&2; exit 1; }; \
	  python="$(PYTHON)"; case "$$python" in */*) ;; *) python=$$(command -v "$$python") \
	    || { printf '%s\n' "error: $(PYTHON) is not on PATH; nothing was changed" >&2; exit 1; };; esac; \
	  if [ -d "$$venv" ]; then \
	    dir=$$(cd -P "$$(dirname "$$python")" && pwd -P) && root=$$(cd -P "$$venv" && pwd -P) \
	      || { printf '%s\n' "error: cannot tell whether $(PYTHON) lives in $$name; nothing was changed" >&2; exit 1; }; \
	    case "$$dir/" in "$$root"/*) printf '%s\n' "error: $(PYTHON) lives in $$name, which setup may rebuild or detach; pass a PYTHON from outside it" >&2; exit 1;; esac; \
	  fi; \
	  have=; if [ ! -L "$$venv" ] && { [ -e "$$venv/bin/python" ] || [ -L "$$venv/bin/python" ]; }; then \
	    have=$$("$$venv/bin/python" -c "$$probe" 2>/dev/null) || have=; [ -n "$$have" ] || have="unknown (it does not run)"; \
	  fi; \
	  if [ ! -L "$$venv" ] && [ -d "$$venv" ] && [ ! -f "$$venv/pyvenv.cfg" ] && [ ! -f "$$venv/sd-provisioning" ]; then \
	    entries=$$(ls -A "$$venv") || { printf '%s\n' "error: cannot list $$name; nothing was changed" >&2; exit 1; }; \
	    [ -z "$$entries" ] || { printf '%s\n' "error: $$name is not a virtualenv (no pyvenv.cfg) and is not empty; nothing was changed. Pick another VENV" >&2; exit 1; }; \
	  fi; \
	  if [ -L "$$venv" ]; then \
	    target=$$(readlink "$$venv") || exit 1; \
	    printf '%s\n' "detaching $$name, a link to $$target; the environment it points at is left alone" >&2 || exit 1; \
	    rm "$$venv" || exit 1; \
	    [ ! -L "$$venv" ] || { printf '%s\n' "error: $$name is still a link; refusing to provision through it" >&2; exit 1; }; \
	  fi; \
	  mkdir -p "$$venv" || exit 1; \
	  printf '%s\n' "make setup is building this environment; nothing may be taken out of it until this file is gone" > "$$venv/sd-provisioning" || exit 1; \
	  rm -rf "$$venv/sd-requirements" "$$venv/.sd-requirements.new"; \
	  if [ -n "$$have" ] && [ "$$have" != "$$want" ]; then \
	    printf '%s\n' "recreating $$name: its bin/python is Python $$have and $(PYTHON) is $$want" >&2 || exit 1; \
	    find "$$venv" -mindepth 1 -maxdepth 1 ! -name sd-provisioning ! -name bin -exec rm -rf {} + || exit 1; \
	    rm -rf "$$venv/bin" || exit 1; \
	  fi
	"$(PYTHON)" -m venv "$(SETUP_VENV)"
	"$(SETUP_PYTHON)" -m pip install --require-hashes -r requirements-dev.txt -r requirements-security.txt
	"$(SETUP_PYTHON)" -m pip uninstall --yes --quiet sd-db
	@mkdir -p "$(SETUP_VENV)/.sd-requirements.new"
	cp requirements-dev.txt requirements-security.txt "$(SETUP_VENV)/.sd-requirements.new/"
	@mv "$(SETUP_VENV)/.sd-requirements.new" "$(SETUP_VENV)/sd-requirements"
	@rm -f "$(SETUP_VENV)/sd-provisioning"
ifneq ($(SERVE),no)
	$(if $(SERVE_SKIP),@printf '%s\n' "not serving the machine: $(SERVE_SKIP); SERVE=yes serves its HEAD" >&2,"$(SETUP_PYTHON)" bin/sd_install.py --serve)
endif

# The pre-commit tier of sd:431. `hooks/pre-commit` runs Ruff over the staged
# Python and the two whole-tree test passes that walk the tree, with a
# wall-time budget in its header. `hooks/commit-msg` (sd:1931) refuses a
# trailer line git will not read, and is linked the same way beside it; both
# paths are checked before either link is made. The install is one relative symlink,
# <common .git>/hooks/pre-commit -> ../../hooks/pre-commit, in the clone's
# common git directory: one hook per clone, read from the main checkout's
# tracked file, shared by every linked worktree, whichever worktree ran
# `make hooks` (a worktree's own copy is never linked, so no worktree's
# branch becomes every other worktree's policy). Not
# `.githooks/` and not `core.hooksPath`: those are the retired gate stack's
# signatures, and bin/sd-status reports each as residue with a removal
# command, so the pack's own hook must not wear them. A clone that still
# carries `core.hooksPath` is refused first, since `git rev-parse --git-path
# hooks` would honour it and place the link under the retired directory. A
# file already at the path that is not this link is refused by name and left
# alone; a dangling link is replaced, as 19 clones linked a retired pack path
# and git skipped the hook silently (pack sd:10212). A clone whose common git directory is not `<main checkout>/.git`, such
# as a `--separate-git-dir` clone, is refused too: the relative link would
# dangle there (sd:3136). This is a
# setting of the clone, not a render: the installer does not make it, and
# folding it into `--user` is the owner's call. `SD_SKIP_HOOKS=1 git commit`
# skips the hook with a notice.
hooks:
	@if set="$$(git config --get core.hooksPath)"; then \
		printf '%s\n' "error: core.hooksPath is set to $$set; the pack's hook lives in .git/hooks -- run 'git config --unset core.hooksPath' (bin/sd-status names it as residue) and retry" >&2; \
		exit 1; \
	fi; \
	common="$$(git rev-parse --path-format=absolute --git-common-dir)" || { \
		printf '%s\n' "error: $$(pwd) is not a git checkout; run 'make hooks' from the pack's clone" >&2; \
		exit 1; \
	}; \
	top="$$(git rev-parse --show-toplevel)"; gitdir="$$(git rev-parse --path-format=absolute --git-dir)"; \
	dir="$$common/hooks"; \
	for hook in pre-commit commit-msg; do \
		link="$$dir/$$hook"; target="../../hooks/$$hook"; \
		if ! [ -f "$$common/../hooks/$$hook" ] || { [ "$$gitdir" = "$$common" ] && ! [ "$$top/hooks/$$hook" -ef "$$common/../hooks/$$hook" ]; }; then \
			printf '%s\n' "error: the git directory $$common is not <checkout>/.git (a --separate-git-dir clone?), so $$link -> $$target would not reach the checkout's hooks/$$hook; link it by hand" >&2; \
			exit 1; \
		fi; \
		if [ -e "$$link" ] && [ "$$(readlink "$$link")" != "$$target" ]; then \
			printf '%s\n' "error: $$link exists and is not the link to hooks/$$hook; move it aside first" >&2; \
			exit 1; \
		fi; \
	done; \
	mkdir -p "$$dir" && for hook in pre-commit commit-msg; do \
		ln -sfn "../../hooks/$$hook" "$$dir/$$hook" && printf '%s\n' "git hooks: $$dir/$$hook -> $$(readlink "$$dir/$$hook")" || exit 1; \
	done

# `generate` and `surface-check` are gone with step 3e. They regenerated the
# committed per-platform copies under templates/ from .github/command-sources/,
# and there are no committed copies any more: bin/sd_install.py renders from skills/
# at install time, so there is nothing to keep in sync and nothing to check for
# closure against a generator.

# `make check CHANGED="<paths>"` is the changed-files fast path (sd:10
# criterion 16): the tests those paths need plus an always-run set, chosen by
# .github/scripts/select-tests.py. Only a CHANGED given on the command line
# counts; one inherited from the environment is ignored, so the full suite
# stays the default. A run the selector narrowed skips `coverage combine` and
# the installer gate, which only the full suite can meet; a run it widened to
# the full suite keeps both. The local gate runs the full suite.
#
# A narrowed run then exits 2, so that a zero from `test` means the full suite
# ran with both coverage gates and means nothing else. Until sd:840 it printed
# the notice below and exited 0, and a caller that reads the status rather than
# the transcript -- a script, a hook, the left half of an `&&` -- could not
# tell a partial run from a green one.
#
# 2 rather than 1 is for whoever reads this recipe: 1 is already the
# skipped-test gate below and a failing shard inside run-tests.sh. It buys
# nothing at the call site, and the number is not a signal a caller can read.
# `make` reports 2 for any failed recipe whatever the recipe exited -- a 1
# here would reach a caller of `make check` as 2 as well -- so the recipe's
# own number appears only on make's `*** [test] Error 2` line. The
# distinction this does deliver, and the one the item asked for, is 0 for the
# whole suite with both coverage gates and non-zero for anything less.
#
# The value is exported rather than written into the recipe as a quoted word:
# a path holding a quote would otherwise end the quoting and the rest would be
# read as shell. And with no CHANGED on the command line the runner is given a
# tree with TEST_CHANGED_FILES removed, so an operator who left one in their
# environment still gets the full suite from a plain `make check`.
ifeq ($(origin CHANGED),command line)
export TEST_CHANGED_FILES := $(CHANGED)
TEST_RUNNER_ENV =
else
TEST_RUNNER_ENV = env -u TEST_CHANGED_FILES
endif

# sd:1541. Local test runs at once on this machine; `SD_GATE_SLOTS=0 make test` lifts it.
# Under `sd-check`, which holds a machine-wide gate slot (sd:1996), the variable
# arrives as 0 and `?=` keeps it, so the gate's own tests take no second slot.
# sd:2522. Unset, the count is the machine's, read as `sd-check` reads it, so a
# plain `make test` and every gate share one pool; a helper that cannot answer still caps.
SD_GATE_SLOTS ?= $(shell "$(PYTHON)" bin/sd_gate_slots.py count 2>/dev/null || echo 1)

# sd:2523. A test review must not wait on this machine's real reviews for a review slot.
test:
	PYTHON_BIN="$(VENV_PYTHON)" SD_GATE_SLOTS="$(SD_GATE_SLOTS)" SD_REVIEW_SLOTS=0 $(TEST_RUNNER_ENV) bash .github/scripts/run-tests.sh
	@if grep -Eq 'skipped=[1-9][0-9]*' unittest-output.log; then printf '%s\n' "Tests skipped locally; install required tools or make the skip explicit."; exit 1; fi
	@if head -n 1 unittest-output.log | grep -q '^test selection: changed files'; then \
		printf '%s\n' "Changed-files fast path: coverage combine and the installer gate were not run. Run make check without CHANGED before a push."; \
		exit 2; \
	else \
		"$(VENV_PYTHON)" -m coverage combine && \
		PYTHON_BIN="$(VENV_PYTHON)" bash .github/scripts/check-installer-coverage.sh; \
	fi

# The one definition of what the Python linters cover. Readers use
# the lint-ruff-paths / lint-mypy-paths targets rather than restating them:
# the retired CI workflow carried its own hand-copied list until 2026-08-29 and had
# silently omitted every bin/ file, so each tool added since sd_route.py was
# lint-clean locally and unlinted in CI. Derive it, do not duplicate it.
#
# `bin/` is enumerated from the index rather than listed, for the same reason
# tests/test_code_health.py enumerates: a hand-written list cannot see the file
# somebody adds next month, and that file is then lint-clean by never having
# been linted. The list here was that trap in miniature until 2026-08-31, and
# tests/test_loc_caps.py's docstring had already named it as one.
# Everything tracked under `bin/`
# is Python (tests/test_no_shipped_shell.py enforces it), so a non-Python file
# arriving there fails lint loudly, which is the right direction to fail.
# `--deduplicate`: an unmerged path is listed once per merge stage, so during
# a conflict ruff and mypy would each be handed the same file three times.
# Both dedupe internally, so this one is waste rather than a wrong answer --
# but every `ls-files` in this repository now says what it means (item 481).
# `:(exclude)bin/fonts`: that directory holds vendored woff2 files, and ruff
# reads every path it is handed as UTF-8 source -- a binary there fails the
# lane with E902 rather than being skipped. Excluded by directory and not by
# extension, so a second binary asset lands in the same place or not at all.
LINT_BIN := $(shell git ls-files --deduplicate -- bin ':(exclude)bin/fonts')
LINT_RUFF_PATHS := $(LINT_BIN) dashboard lib tests
LINT_MYPY_PATHS := $(LINT_BIN)

.PHONY: lint-ruff-paths lint-mypy-paths
lint-ruff-paths:
	@printf '%s\n' "$(LINT_RUFF_PATHS)"
lint-mypy-paths:
	@printf '%s\n' "$(LINT_MYPY_PATHS)"

# Pass STRICT=1 to turn missing-tool skips below into hard errors, so the
# ShellCheck lane cannot skip. Ruff and mypy cover the paths named in LINT_RUFF_PATHS and
# LINT_MYPY_PATHS above: Ruff over the tracked bin/ files, dashboard/, lib/ and tests/,
# mypy over the tracked bin/ files. mypy's error count over lib/ and
# tests/sd_db may not rise: MYPY_CEILING in tests/test_code_health.py, an
# always-run module, holds it (sd:3485). The installer
# package and the shipped payload that step 3e removed are not in either.
#
# The bash 3.2 lane survives 3e on a narrower rationale than it had, and the
# narrowing is worth stating. It existed because the pack shipped shell scripts
# that ran on whatever bash a consumer's macOS had, which is 3.2. Nothing is
# shipped now. What it still protects is this repo's own three scripts under
# .github/scripts/, which `make check` runs through /bin/bash on the
# maintainer's machine -- a real subject, just a smaller one. This is the only
# place the lane runs: the CI job that built bash 3.2 from source to run it on
# Linux was cut by sd:10 criterion 17, because a from-source interpreter build
# was guarding three scripts the local run already executes under the real
# thing. A platform without bash 3.2 prints a skip line; STRICT=1 makes it
# fatal, so do not pass STRICT=1 on Linux and expect this target to pass.
lint:
	"$(VENV_PYTHON)" -m ruff check $(LINT_RUFF_PATHS)
	"$(VENV_PYTHON)" -m mypy $(LINT_MYPY_PATHS)
	@if command -v shellcheck >/dev/null 2>&1; then \
		git ls-files -z --deduplicate '*.sh' | xargs -0 shellcheck -S warning; \
	elif [ "$(STRICT)" = "1" ]; then \
		printf '%s\n' "error: shellcheck not found and STRICT=1; shell lint is required." >&2; \
		exit 1; \
	else \
		printf '%s\n' "warning: shellcheck not found; skipping shell lint."; \
	fi
	@STRICT="$(STRICT)" bash .github/scripts/check-bash32-syntax.sh

# `precheck` is `lint` plus the always-run test modules, about a minute
# (sd:2604). `sd-check` runs it before it waits for a gate slot and runs
# nothing more when it fails, so a lint error or a broken whole-tree module
# costs a minute rather than the slot wait and a full suite. The modules are
# the ones select-tests.py reads as the always-run set; run-precheck.py runs
# each on its own and names every one that fails. A lint failure stops make
# at `lint`, and make's own error line names it. `check` does not run
# `precheck`: under `sd-check` it has already run, and `check` still runs
# `lint` first and every module in `test`.
precheck: lint
	"$(VENV_PYTHON)" .github/scripts/run-precheck.py

# A scanner found on PATH is whatever version happens to be installed, while
# `make setup` installs the requirements-security.txt pin under
# --require-hashes. When the two differ the gate is not reproducible: a newer
# scanner invents findings the pin never reports, an older one misses findings
# the pin would catch. The fallback still runs -- it
# is better than no audit -- but it announces the skew instead of hiding it.
#
# A scanner that is missing entirely is a silent pass: the `if` exits 0 and
# `make audit` reports success having audited nothing. STRICT=1 makes that
# fatal, matching the node and shellcheck lanes above, so the local gate or a
# release gate can demand the audit actually ran.
# The zizmor lane audits `.github/workflows/` when the directory exists.
# This repository gates locally (`repo.ci = local`) and carries no workflow,
# so the lane says it has nothing to audit. A present directory is audited;
# an absent one is not a pass that looks like an audit.
audit:
	@if [ -x "$(VENV_BIN)/bandit" ]; then \
		"$(VENV_BIN)/bandit" -q -r --severity-level medium bin; \
	elif command -v bandit >/dev/null 2>&1; then \
		printf '%s\n' "warning: $(VENV_BIN)/bandit is missing; using an UNPINNED bandit from PATH ($$(bandit --version 2>&1 | head -1 | tr -d '\r')). requirements-security.txt holds the pin; run 'make setup' to match it."; \
		bandit -q -r --severity-level medium bin; \
	elif [ "$(STRICT)" = "1" ]; then \
		printf '%s\n' "error: bandit not found and STRICT=1; the Python security audit is required." >&2; \
		exit 1; \
	else \
		printf '%s\n' "warning: bandit not found; skipping Python security audit."; \
	fi
	@if [ ! -d .github/workflows ]; then \
		printf '%s\n' "audit: no .github/workflows; no workflow to audit with zizmor."; \
	elif [ -x "$(VENV_BIN)/zizmor" ]; then \
		"$(VENV_BIN)/zizmor" --offline .github/workflows/; \
	elif command -v zizmor >/dev/null 2>&1; then \
		printf '%s\n' "warning: $(VENV_BIN)/zizmor is missing; using an UNPINNED zizmor from PATH ($$(zizmor --version 2>&1 | head -1 | tr -d '\r')). requirements-security.txt holds the pin; run 'make setup' to match it."; \
		zizmor --offline .github/workflows/; \
	elif [ "$(STRICT)" = "1" ]; then \
		printf '%s\n' "error: zizmor not found and STRICT=1; the workflow security audit is required." >&2; \
		exit 1; \
	else \
		printf '%s\n' "warning: zizmor not found; skipping workflow security audit."; \
	fi

# WORKFLOW.md said `make check` ran `sd-docs-lint`, and nothing did:
# `test` lints temporary fixture repositories, `sd-ship` lints at delivery
# time, and nothing ran either against this checkout's own docs/work. The lint
# reads `sd_lib` and the working tree only, so it needs no database and no
# provisioned library. Item 370. `--no-history` keeps it to the tree: without
# it rule 2 fetches the remote and reads `git log` for each item's delivery,
# and the gate's verdict would hang on both (sd:2606).
docs-lint:
	"$(VENV_PYTHON)" bin/sd-docs-lint --no-history

# Re-vendor the research renderer's Latin woff2 faces and rewrite
# bin/sd_research_fonts.py from them. Needs network; not part of `check`,
# because a check that reaches Google fails on a plane. Run it after
# changing FAMILIES in bin/sd_research_fonts_build.py, and commit both the
# files under bin/fonts/ and the regenerated module.
fonts:
	"$(VENV_PYTHON)" bin/sd_research_fonts_build.py

# `full-check` is gone with step 3e: it ran a shipped script that no longer
# exists, and every lane it wrapped that still has a subject is already a target
# here. `check` is the four gates `sd-check` runs for `sd/local-gate`. The
# bash 3.2 parse inside `lint` runs only where a bash 3.2 exists, which is
# this machine when it is a Mac.
#
# `test` runs last, and the order is load-bearing twice over (sd:840). `make`
# stops at the first prerequisite that fails, and a narrowed `test` now fails
# by design, so anything listed after it would not run at all on the
# changed-files fast path: with `test` last, `make check CHANGED="<paths>"`
# still runs the three whole-tree lanes and still comes out non-zero. And on
# a full run the cheap gates now go first, so a Ruff error is seconds away
# rather than a whole suite away. tests/test_changed_files_fast_path.py pins
# the position; do not move `test` back to the front.
check: lint audit docs-lint test

# Gate mode only (see SD_LOCAL_GATE above): every lane waits for the in-tree
# environment, which is built once per make. Outside the gate
# GATE_ENV is empty and this line adds nothing.
lint precheck audit docs-lint test: $(GATE_ENV)

gate-env:
	"$(PYTHON)" .github/scripts/provision-gate-env.py "$(PYTHON)"

# The lane runs `make post-merge` in the hub checkout after a pack merge, once
# its runner lock is released (sd:3566), whether the checkout is pinned
# (detached) or on the default branch (sd:3647). `refresh` moves and installs
# a pinned checkout inside its own protection (consumers stopped, old SHA kept);
# it leaves a branch checkout alone. Setup here, outside that protection, would
# race live services and lanes, so a branch checkout is refused with the step
# that pins it.
# repo-sync lives in the `system` checkout; SYSTEM_REPO names it when it is not
# ~/repos/system. The dashboard moved to this checkout (pack sd:5987).
# The lane ends the target at 600 s, so the refresh waits on the lanes alone,
# not on gates run by hand, and at most 420 s, as system's post-merge does
# (system sd:5799). Without them its drain waited up to 45 minutes on gates and
# other lanes, and the lane killed it at 600 s with nothing moved (sd:7209).
SYSTEM_REPO ?= $(HOME)/repos/system

post-merge:
	@if git symbolic-ref -q HEAD >/dev/null 2>&1; then echo "post-merge: $(CURDIR) is on a branch, so refresh will not install it; pin it: git -C $(CURDIR) switch --detach && make post-merge" >&2; exit 1; fi
	REPO_SYNC_LANES_ONLY=1 REPO_SYNC_DRAIN_WAIT=420 sh "$(SYSTEM_REPO)/local-repo-sync/repo-sync.sh" refresh "$(CURDIR)"
	sh "$(CURDIR)/dashboard/dashboard.sh" health
