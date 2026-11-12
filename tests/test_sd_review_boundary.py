"""The boundaries bin/sd-review must not cross, asserted rather than promised.

Two of them are absences, and an absence is only ever proved structurally:

  * **Findings are never posted.** No code path hands a finding to GitHub;
    the one network client, `sd_registry`'s, posts a diff to a model endpoint
    (the allow-list below says why). This file reads the source's import
    graph and its call sites, so adding `import urllib.request` or a `gh pr
    comment` argv fails here even if no other test notices.
  * **The repository comes from cwd (R10-D6).** No option accepts a path to a
    repository, so a session cannot be pointed at another checkout.

These read the file as text and as an AST. That is deliberate: a mock-based
test would only prove the mocked path does not post.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SD_REVIEW = REPO_ROOT / "bin" / "sd-review"
SOURCE = SD_REVIEW.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

BIN_FILES = tuple(
    path for path in sorted((REPO_ROOT / "bin").iterdir()) if path.is_file()
)
# `sd_lib`, `sd_route` and `sd_registry` are shared core, budgeted on the
# design's core line rather than the lane's. Everything else `bin/sd-review`
# imports out of `bin/` is the lane, derived from the import graph so a module
# added to the lane starts counting against it without anyone remembering to
# add it here.
#
# This list is a judgement, not a derivation, and is written down rather than
# computed because no computable rule separates these three: `sd_route` and
# `sd_registry` each have exactly one importer in `bin/` today, so "imported by
# more than one entry point" would evict the router as well and prove only that
# the rule was chosen to fit. What earns `sd_registry` its place is that
# `bin/sd_install.py` already depends on its contract -- it restates
# `REGISTRY_RELATIVE` because the installer runs before anything in `bin/` is
# importable, and `ProviderRegistrySeedTests` fails if the two ever disagree.
# The registry reader answers "who may review"; the lane's budget is for the
# code that runs a review.
SHARED_CORE = frozenset({"sd_lib", "sd_route", "sd_registry"})


def _bin_imports(path: pathlib.Path) -> frozenset:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return frozenset(names)


_BIN_MODULES = {path.stem: path for path in BIN_FILES}
REVIEW_LANE = frozenset(
    [SD_REVIEW]
    + [
        _BIN_MODULES[name]
        for name in _bin_imports(SD_REVIEW)
        if name in _BIN_MODULES and name not in SHARED_CORE
    ]
)

# Modules that can reach a network, plus the ones that wrap a client. A tool
# that never posts a finding has no business importing any of them.
NETWORK_MODULES = frozenset(
    {
        "http",
        "http.client",
        "httplib",
        "urllib",
        "urllib.request",
        "urllib.error",
        "socket",
        "ssl",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "asyncio",
        "requests",
        "httpx",
        "aiohttp",
        "urllib3",
    }
)

# Argv fragments that would publish a finding. `gh` is the pack's usual client,
# so the check is on the words, not on one spelling of the client.
POSTING_FRAGMENTS = (
    "pr comment",
    "pr review",
    "pr edit",
    "issue comment",
    "api repos",
    "/pulls/",
    "/reviews",
    "check-runs",
    "--add-label",
    "add-label",
    "create-review",
    "submit_pending",
)


def imported_names() -> set[str]:
    names: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class NeverPostsTests(unittest.TestCase):
    def test_no_network_module_is_imported(self) -> None:
        offending = sorted(
            name
            for name in imported_names()
            if name in NETWORK_MODULES or name.split(".")[0] in NETWORK_MODULES
        )
        self.assertEqual(offending, [], f"sd-review imports network module(s): {offending}")

    def test_every_import_is_from_the_standard_library_or_this_repository(self) -> None:
        allowed = {
            "__future__",
            "argparse",
            "contextlib",  # `closing` around the read-only receipt connection.
            "hashlib",  # Binds fix verification to the exact preceding report.
            "json",
            "os",
            "pathlib",
            "re",
            "shlex",
            "sqlite3",  # Only to name the error class the receipt read catches.
            "subprocess",
            "sys",
            "tempfile",
            "time",  # sd:2359: times the routing for the Jev ledger's paired row.
            "typing",
            # sd:495. The runner records the repository check it ran on the
            # clone, and `recorded_check` reads that row instead of running the
            # same deterministic gate again. Read-only and local: the
            # connection is opened `write=False` against the `--database` this
            # run was already handed, and the only call is `check_record`, a
            # single SELECT. It is a SQLite file on this machine, so it widens
            # the allow-list by a database the lane already depends on for
            # provider state and not by a way out of the process.
            "sd_db",
            "sd_db.errors",
            "sd_lib",
            "sd_route",
            # The registry reader, and since #754 a network client as well.
            # `bin/sd_registry.py` holds the `url` client -- a stdlib-HTTP POST
            # that carries the diff to a review provider and reads the answer
            # back -- and `bin/sd-review` reaches it by default, as the
            # `chat_completion` fallback of its `client` parameter. So this
            # name does not have `sd_setup_github`'s standing below: nothing
            # holds `sd_registry` to a never-posts assertion, and it would not
            # pass `test_no_network_module_is_imported` if anything did. The client landed
            # in that file because the sub-cap below left it nowhere else to
            # go; R11-D34 records that, and this file does not re-argue it.
            #
            # What still holds the boundary this file is for: the client posts
            # to a model endpoint, never a finding to GitHub, and the argv,
            # `gh` and posting-fragment assertions above cover the entry point
            # that would have to do the posting.
            "sd_registry",
            # The optional Jev tier reading. It runs one bounded local command
            # and only when an explicit opt-in is set, so it widens the
            # allow-list by a subprocess and not by a way out to GitHub; the
            # never-posts assertions below cover it like the rest of the lane.
            "sd_jev",
            # Focused local readers; only sd-check explicitly calls receipt writers.
            "sd_check_receipts",
            "sd_review_material",
            "sd_review_readiness",
            # The `opencode-json` reader (sd:1329): an argv builder, the inline
            # agent config, and an NDJSON read-back. It spawns nothing itself
            # and imports only the standard library, so it widens the allow-list
            # by a reader and not by a way out; the never-posts assertions
            # below cover it like the rest of the lane.
            "sd_opencode",
            # The installer, imported inside the one dispatch branch. It is in
            # this repository and is itself held to the never-posts assertions
            # below, so it widens the allow-list without widening the boundary.
            "sd_setup_github",
            # The local gate's run (sd:2041), imported inside `--gate-check`
            # only. It is the half of the gate that posts nothing: the status
            # post stayed in `sd_local_gate`, which this lane does not import,
            # so the gate check widens the allow-list by a worktree and a
            # child process and not by a way out to GitHub.
            "sd_gate_run",
        }
        self.assertEqual(sorted(imported_names() - allowed), [])

    def test_no_posting_argv_fragment_appears_anywhere_in_the_source(self) -> None:
        lowered = SOURCE.lower()
        found = [fragment for fragment in POSTING_FRAGMENTS if fragment in lowered]
        self.assertEqual(found, [], f"sd-review contains posting fragment(s): {found}")

    def test_the_gh_client_is_never_invoked(self) -> None:
        for node in ast.walk(TREE):
            if isinstance(node, ast.Constant) and node.value == "gh":
                self.fail("sd-review names the gh client; this lane never posts")

    def test_the_only_subprocess_call_is_the_injectable_runner(self) -> None:
        # `sd_lib.run_group` is a process start too: it is how the runner
        # starts one since sd:1482, so it counts against the same one.
        calls = [
            node
            for node in ast.walk(TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (node.func.value.id == "subprocess"
                 or (node.func.value.id == "sd_lib" and node.func.attr == "run_group"))
        ]
        self.assertEqual(len(calls), 1, "subprocess is started in more than one place")
        enclosing = [
            node.name
            for node in ast.walk(TREE)
            if isinstance(node, ast.FunctionDef) and calls[0] in list(ast.walk(node))
        ]
        self.assertIn("subprocess_runner", enclosing)

    def test_nothing_is_opened_for_writing_outside_the_attempt_directory(self) -> None:
        writes = [node for node in ast.walk(TREE) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.func.attr == "write_text"]
        self.assertEqual(len(writes), 1)  # One CLI attempt writer: Codex schema or Claude review material.
        for call in writes:
            target = call.func.value
            self.assertIsInstance(target, ast.BinOp)
            self.assertEqual(ast.unparse(target.left), "workdir")
            containers = [node for node in ast.walk(TREE) if isinstance(node, ast.With)
                          and call in list(ast.walk(node))]
            self.assertTrue(any("tempfile.TemporaryDirectory" in ast.unparse(node.items[0].context_expr)
                                for node in containers), "writes must stay in a temporary attempt")
        self.assertNotIn('open(', SOURCE.replace('.open("r"', "").replace('.open("rb"', ""))


class RepoFromCwdTests(unittest.TestCase):
    def test_no_option_takes_a_repository_path(self) -> None:
        for node in ast.walk(TREE):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
                continue
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    self.assertNotIn(
                        argument.value,
                        {"--repo", "--repository", "--path", "--root", "--cwd", "--dir"},
                        "R10-D6: sd-review resolves its repository from cwd only",
                    )

    def test_repo_root_is_resolved_from_the_process_working_directory(self) -> None:
        self.assertIn("sd_lib.repo_root(None)", SOURCE)


class SetupGithubLivesElsewhereTests(unittest.TestCase):
    """The installer landed at step 3-d; the boundary it must respect did not move.

    This class replaced one that asserted no subcommand existed. It exists now
    because `setup-github` *writes a workflow file*, and the proof above --
    "nothing here is opened for writing" -- is a structural read of this one
    file. Keeping the installer in `bin/sd_setup_github.py` is what lets that
    proof stay literal instead of growing an exception, so these assertions are
    about where the code is, not about whether it exists.
    """

    def test_the_dispatch_is_here_and_the_implementation_is_not(self) -> None:
        self.assertIn("SETUP_GITHUB_SEAM", SOURCE)
        self.assertIn("sd_setup_github", SOURCE)
        # No subparsers: they would make every existing invocation
        # positional-first and break `sd-review --scope pr`. Asserted against
        # the call graph, not the text, so the prose explaining the choice is
        # not itself a violation of it.
        calls = [
            node
            for node in ast.walk(TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_subparsers"
        ]
        self.assertEqual(calls, [])
        # The workflow this pack installs is named in the installer, never here.
        self.assertNotIn("sd-review-route.yml", SOURCE)

    def test_the_installer_module_exists_and_is_the_one_that_writes(self) -> None:
        installer = (REPO_ROOT / "bin" / "sd_setup_github.py").read_text(encoding="utf-8")
        self.assertIn("sd-review-route.yml", installer)
        self.assertEqual(installer.count(".write_text("), 1)

    def test_the_installer_never_posts_either(self) -> None:
        installer = (REPO_ROOT / "bin" / "sd_setup_github.py").read_text(encoding="utf-8")
        tree = ast.parse(installer)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        offending = sorted(
            name for name in imported
            if name in NETWORK_MODULES or name.split(".")[0] in NETWORK_MODULES
        )
        self.assertEqual(offending, [])
        lowered = installer.lower()
        self.assertEqual([f for f in POSTING_FRAGMENTS if f in lowered], [])
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "gh":
                self.fail("the installer names the gh client; this lane never posts")


def _lines(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


class LineBudgetTests(unittest.TestCase):
    """Budgets measured over what they name, enumerated from the filesystem.

    Both assertions below used to be written against a remembered list rather
    than the tree, and both were wrong in the same direction: the sub-cap read
    one file while naming a lane, so splitting the lane in two hid 294 lines
    from it; the ceiling summed every file in `bin/` while the design places
    `migrate-*` outside the cap, so the migration tool was silently spending
    the backbone's budget. Deriving each set here is what keeps a future split
    or a new module from escaping the number that governs it.
    """

    def test_the_review_lane_stays_under_its_sub_cap(self) -> None:
        # The whole lane, not the entry point: `bin/sd-review` plus every
        # bin/ module it imports. A cap that measures one file is a cap you
        # can duck by adding a second file.
        #
        # The number is the lane's exact size, so it is a ratchet: the next
        # line spent has to be argued for here. Raise this only with the reason
        # written down; a cap moved in silence is not a cap.
        #
        # 2043 -> 2069 is sd:376, which gates the five provider-capability
        # lines on a registry that reads or a reviewer that resolves: 26 lines
        # that make the lane print less, on eight consumers where those lines
        # were unreachable by construction.
        #
        # 2069 -> 2123 is sd:495, which lets the lane accept the runner's own
        # `sd-check` pass when it is recorded against the tree being reviewed
        # instead of running the same deterministic gate a second time. The 54
        # lines buy back the whole native suite on every clean `system` row,
        # and most of them are the fall-through legs: no database, no run, no
        # row, a non-zero exit, or a tree that moved all run the gate.
        #
        # 2123 -> 2127 is sd:405. `WORKFLOW.md` claimed "every writing
        # skill refuses the upstream tree" and nothing refused anything: the
        # rule was a bullet in `skills/sd-plan/SKILL.md` addressed to an agent,
        # and 162 committed `prd.md` files across two `guest` checkouts went
        # unremarked. What lands in this lane is the call and a two-line comment
        # saying why this lane performs it: `sd-plan` step 4 routes the triad
        # here and step 5 gates `planning -> ready` on it. The rule itself, the
        # mode resolution and the sentence are `sd_lib.guest_artifact_refusal`,
        # outside this lane, so nothing else is spent here.
        #
        # 2127 -> 2130 is sd:932. The comment `workflow_text` writes above
        # `ref:` claimed the merge-ref default fails the job at checkout on a
        # conflicted pull request; measured on probe PR #966 (sd:878), such a
        # pull request gets no run at all. Three lines say what was measured
        # and name the window the head ref is held for, in the file every
        # consumer installs, where the claim would otherwise be read as fact.
        #
        # 2130 -> 2149 is sd:940. `setup-github` run in the pack's own
        # checkout rendered the Dependabot guard for a pin its workflow does
        # not name, and `--check` there reported that guard as drift. The
        # self-install now leaves `dependabot.yml` as it stands and diffs the
        # workflow alone. The decision is its own function, `guard_after`,
        # with the guard refusal moved into it: left inline, the extra branch
        # put `setup_github` one over the complexity ceiling in
        # `tests/test_code_health.py`, and that ceiling does not move.
        #
        # 2149 -> 2318 is sd:788 slice 3. Every `url` call the lane makes is
        # charged through the library's `sd_db.calls.call` -- reserve at the
        # bound, claim, one POST, settle or lose -- and a bill at its
        # `cap_usd_month` is passed over by the chain and refused by
        # `--provider`, naming the month's total. The lane writes no `cost`
        # row of its own: the 169 lines are the road to that function
        # (`Ledger`, `open_ledger`, `capped_bills`, `charged_call`), the
        # fake-client wire that keeps the `client` seam every existing test
        # dispatches through, and the fail-closed legs when the road is shut:
        # no library, a library older than `sd_db.calls`, a database that
        # will not open. Each leg is a test in `test_sd_review_ledger.py`.
        # The library is reached through `sd_lib.import_sd_db`, which the
        # lane already called, and not `sd_handoff_rows`, whose 162 lines
        # would otherwise join the lane for one function.
        #
        # 2318 -> 2430 is sd:788 slice 4. `metered_bills` reads the minimax
        # meter at review start -- the pinned GET, two `meter` rows per
        # enabled entry, the newest row per window -- and a zero, missing or
        # stale window joins the same map as the capped bills, so the chain
        # and the pick refuse it unchanged; `--explain` and `--dry-run` read
        # the rows and send nothing.
        #
        # 2430 -> 2561 is the approved workflow-efficiency slice. It adds
        # explicit, fully bound check receipts, zero-call readiness, complete
        # input-byte advice, and post-gate input revalidation. Shared JSON,
        # CLI attempts, result accounting, and redundant-wrapper removal saved
        # 59 lines before the final mutation guard. The user approved this
        # measured increase; shared-core classification and complexity limits
        # remain unchanged. The exact resulting size keeps this a ratchet.
        # Receipt identity includes directory topology/modes and resolves
        # executable paths from the same repository cwd used by dispatch.
        #
        # 2580 -> 2601 is criterion 31(c)'s planning scope. `--scope planning`
        # said it reviewed "the active work item's planning documents" and in
        # fact unioned every candidate's, so a finding against either half was
        # a finding against "the" item. `_for_this_branch` reads the `branch:`
        # an item already records and returns the one this checkout is on,
        # refusing rather than picking when the branch settles nothing. The
        # twenty-one lines are that function; the lane gained no new import,
        # no new file, and no new reach. It was written at thirty-five lines
        # first and compressed to this before the ceiling was moved, and the
        # exact resulting size keeps this a ratchet.
        #
        # 2561 -> 2580 repairs F1/F4 under the approved measured-increase rule.
        # Native Codex counts only transmitted prompt bytes; attached transports
        # keep their complete payload bounds. Readiness counts fitting reviewers
        # without letting an oversized fallback veto them. Dispatch rechecks the
        # complete payload. Prior-source metadata bounds reads before loading,
        # preserves cumulative size inventory, and suppresses partial dry-run
        # stdin. Complexity ceilings and shared-core classification are unchanged.
        #
        # 2601 -> 2620 adds repository-controlled automatic Copilot selection
        # for deep changes. Lower tiers remain explicit-only. The route report
        # carries the selection into shipping after local review. Complexity
        # limits and shared-core classification remain unchanged.
        #
        # 2620 -> 2826 admits the optional Jev tier reading. The number is the
        # argument, not the feature's size: the lane measured exactly 2620
        # before it, and 2636 with `bin/sd_jev.py` deleted outright, so the
        # sixteen lines that call it bust the ratchet on their own. A cap
        # sitting on its own floor refuses every change to the lane equally,
        # which is a stuck ratchet rather than a budget. This raise is its own
        # commit, before the one that spends it, because the rule the file
        # below states is that a cap is never raised in the change that busts
        # it -- and a raise nobody can read separately is the failure that rule
        # exists to prevent. Shared-core classification and the complexity
        # ceilings are unchanged; `sd_route` stays outside the lane and pure.
        # 2826 -> 2880 admits `agy-json`, a third reader for the antigravity
        # CLI. The number is the argument again: the lane measured exactly
        # 2826 before it, sitting on its own floor, so any third reader busts
        # the ratchet whatever it costs. The fifty-four lines are an argv
        # builder, an envelope parser, and four dispatch edits; the lane gained
        # no new import, no new file, and no new reach. The builder was written
        # at sixty-five lines first and compressed to this before the ceiling
        # moved, and the exact resulting size keeps this a ratchet. This raise
        # is its own commit, before the one that spends it. Shared-core
        # classification and the complexity ceilings are unchanged.
        # 2880 -> 2892 buys documentation, not machinery. `agy_argv` gains no
        # behaviour here: the twelve lines name the eight `claude_argv`
        # hardening flags `agy` has no equivalent for, and record that
        # `--sandbox` was measured rather than read off the help text -- a
        # write inside `--add-dir` lands under it, and what refuses one on the
        # argv below is headless mode's inability to prompt. Both facts lived
        # in a pull request body, which nobody reads twice, and the next
        # editor of that function needs them. This raise is its own commit,
        # before the one that spends it. Shared-core classification and the
        # complexity ceilings are unchanged.
        # 2892 -> 2928 answers two critical review findings on `agy-json`.
        # Twenty-two lines move the reader from `--output-format json` to
        # `stream-json`, because the `init` frame is the one surface that
        # names the model that answered, and a substituted model defeats the
        # independence guard below the registry. Frame reassembly replaces a
        # single `json.loads`, and a mismatch refuses. The remaining fourteen
        # record what could not be fixed: the transcript is retained by the
        # vendor, keyed by `conversation_id`, and nothing local deletes it, so
        # the limit is written where the operator enabling the entry will read
        # it. This raise is its own commit, before the one that spends it.
        # Shared-core classification and the complexity ceilings are unchanged.
        #
        # 2928 -> 2938 is the Jev stage flip. `JEV_SD_REVIEW` was an opt-in
        # testing for `=1`, which meant a machine with `jev` installed and
        # keyed took no reading at all, for want of an export nobody had
        # written. Ten lines: the branch that makes `jev enabled` exiting 3
        # silent rather than loud -- absent, unkeyed and switched off are one
        # case, and the ordinary state of a public checkout of a repository
        # whose companion is private -- plus the two comments saying which
        # failures stay loud and why, which is the half of the flip a reviewer
        # has to be able to check. `sd_lib` supplies the switch itself, so the
        # vocabulary is not spent here twice.
        #
        # 2938 -> 2942 pays for four lines of comment that correct a false
        # statement about what leaves the machine. Review of #1124 found the
        # payload sentence claiming the tier names come from *this*
        # repository's policy and that each carries a description. Neither
        # holds: `load_policy` reads the checkout the command runs in, and a
        # tier a repository invented is sent as a bare name -- so a private
        # repo's `embargo-legal` goes to a third party with the name as the
        # whole of the message. The fourth line says exit 3 collapses four
        # reasons and not the two the comment named. Prose was tried first and
        # cost the claim its precision, which is the wrong trade on a sentence
        # a reader consults to decide whether to switch the stage off. This
        # raise is its own commit, before the one that spends it. Shared-core
        # classification and the complexity ceilings are unchanged.
        #
        # **This raise is not its own commit and does not precede the spend.**
        # The convention above is the right one and this is a departure from
        # it: the flip was already committed when CI measured the lane, so the
        # honest record is that the number was taken from the tree afterwards
        # rather than argued for in advance. It was still argued for -- the
        # commit before this one gives back nine lines of duplicated prose and
        # one import idiom, and ten is what the change itself costs.
        #
        # 2942 -> 2966 pays for the control that closes C-40, which is twenty-
        # two lines of comment and two of argv. The flag is one `-c` pair; the
        # comment is the change. Two things look like the control and are not:
        # `--enable skip_host_skill_discovery` and `--disable skill_search`
        # were each measured and each left the prompt byte-identical with both
        # canaries still emitted. Unrecorded, the next hardening pass buys that
        # measurement again. The rest names the two discovery roots, says
        # `--ignore-user-config` reaches neither `$HOME` one, and marks the two
        # limits the key does not carry -- it is a zero rather than an
        # allow-list, and it suppresses injection rather than reading. This
        # raise is its own commit, before the one that spends it. Shared-core
        # classification and the complexity ceilings are unchanged.
        #
        # 2966 -> 3095 buys the measurement the C-40 flag above cannot carry.
        # sd:1248: a codex build that does not know
        # `skills.include_instructions` ignores it and exits 0 -- measured on
        # 0.155.1 with `-c skills.totally_unknown_key_xyz=false` -- so the argv
        # establishes nothing, and the operator who reads it believes a control
        # that may not exist. `codex_skill_state` asks the binary instead: one
        # offline `codex debug prompt-input`, 1.44s, and the key took effect if
        # and only if `<skills_instructions>` is absent from the rendered
        # prompt. The 129 lines are that function, its argv builder, the two
        # points the probe runs at, and the report on the result and in the
        # codex outcome. About half are the comments this lane's convention
        # requires: which flags the probe cannot carry and why, what each
        # number was measured on, and why an inert key warns instead of
        # refusing. A version gate was rejected, because the registry treats
        # `codex-json` as a protocol: a fork that speaks it is a different
        # entry, not an older binary. This raise is its own commit, before the
        # one that spends it. Shared-core classification and the complexity
        # ceilings are unchanged.
        #
        # 3095 -> 3126 pays for two defects the codex review of sd:1248 found
        # in that probe. It read any exit-0 stdout without the marker as
        # `suppressed`, so a wrapper's help page passed as proof; now the
        # stdout must parse as the render measured on 0.155.1, a list of
        # `message` items with text parts, or the answer is `unknown`. And it
        # kept only the first token of the entry's start line, so `python3 -m
        # wrapper exec` probed the interpreter; now the line is kept whole up
        # to its `exec`. The 31 lines are the render parser and the two
        # reasons. This raise is its own commit, before the one that spends
        # it. Shared-core classification and the complexity ceilings are
        # unchanged.
        #
        # 3126 -> 3176 is sd:1328, which makes Copilot-on-deep-changes the
        # machine default instead of a per-repository opt-in. Nineteen of the
        # twenty-one `runner_merge=auto` repositories carry no
        # `.github/sd-review.json`, so `automatic_deep: false` in the built-in
        # default meant no Copilot on any of them, and the alternative was
        # nineteen files saying one thing. The 50 lines are `copilot_policy`
        # (repository file when it names the key, else `sd.copilot_review`,
        # else `deep`, with the source named for `--explain`),
        # `copilot_automatic` (which also keeps `always` off the `skip` tier),
        # the guard that holds only a file naming `copilot_review` to its
        # grammar so an absent key inherits, two more report keys, one render
        # line, and the comments that say why the default carries `None`.
        # This raise is its own commit, before the one that spends it.
        # Shared-core classification and the complexity ceilings are
        # unchanged.
        #
        # 3176 -> 3148 is the review of sd:1328 finding that `sd-ship` read
        # the report's `automatic` verdict back instead of the setting as it
        # stood at dispatch, so `never` set after a deep review still bought
        # one. The resolution (`copilot_policy`, `copilot_automatic`) is
        # policy both lanes need and mechanics of neither, so it moved to
        # `sd_lib`; the lane keeps the one call and gains the `repository`
        # report key. Lowered to the measured size in the change that took
        # the lines out, so the cap does not hide the next spend.
        #
        # 3126 -> 3167 is sd:1343. A linked worktree of this pack has no
        # .venv, and `make check` is the gate this lane runs, so the pass
        # died on `/bin/sh: .venv/bin/python: No such file or directory`
        # (Error 127) before any reviewer ran and the receipt read as a
        # failed review. The 41 lines are `missing_toolchain`, which reads
        # the sd-check payload for a check that exited 127 or a recipe that
        # reported `Error 127` and names the interpreter /bin/sh looked for;
        # the two keys `run_check` adds (`reason: toolchain_missing`,
        # `interpreter`); and the human line that says the gate could not
        # start rather than that it failed. This raise is its own commit,
        # before the one that spends it. Shared-core classification and the
        # complexity ceilings are unchanged.
        #
        # 3167 -> 3198 is sd:1343's second pass. The codex review of 23f2f429
        # ran a recipe that passed its check and then lost a command on a
        # later line; the lane read the 127, called it toolchain_missing and
        # named /bin/sh. The 31 lines make the claim follow the evidence:
        # `classify_gate` takes sd-check's own spawn failure (`exit_code:
        # None`, `cannot run <program>`) as the one proof that no check ran,
        # and reads any other 127 as `command_not_found`, naming the command
        # and line from the shell's report; `gate_failed_line` says only
        # what is known. This raise is its own commit, before the one that
        # spends it. Shared-core classification and the complexity ceilings
        # are unchanged.
        #
        # 3198 -> 3233 is sd:1343's third pass. The fix-verification review of
        # 30e45b6b ran a gate with no aggregate `check`: `test` passed and
        # `lint` could not spawn, and the line said no check ran, from one
        # record. The 35 lines make `classify_gate` read every record --
        # `entrypoint`, the record the reason came from, and `started`, each
        # entrypoint whose record shows it executing -- and make
        # `gate_failed_line` claim that no check ran only when `started` is
        # empty, naming the entrypoint otherwise; the 127 parsing moved to
        # `_not_found_report` to keep `classify_gate` under the branch
        # ceiling. This raise is not its own commit: it landed inside
        # 7bfda9bc, the commit that spends it, after the pre-commit hook
        # refused the split twice and the second attempt carried the whole
        # index; the number is still the measurement. Shared-core
        # classification and the complexity ceilings are unchanged.
        #
        # 3148 -> 3255 is sd:1343 landing after sd:1328. The three raises
        # above were measured from a base without sd:1328's move of
        # `copilot_policy` out of the lane, so on the merged tree neither
        # chain's number is the size; 3255 is what the merged tree measures.
        # The 107 lines are what lets a receipt say why a gate failed instead
        # of `gate_failed`: `classify_gate` and `_not_found_report`, which
        # read sd-check's records for the one proof that a program never
        # started (`exit_code: None`, `cannot run <program>`) and otherwise
        # name the command a 127 could not find and its line; `_started` and
        # `_spawn_failed`, so `entrypoint` and `started` come from every
        # record; `gate_failed_line`, which says no check ran only when
        # nothing started and names the entrypoint otherwise; the keys
        # `run_check` adds; and the comments that say why exit 127 alone is
        # not evidence. A worktree without .venv is the case that needed it:
        # `make check` died on the interpreter before any reviewer ran, and
        # the receipt read as a failed review. This raise is its own commit,
        # after the merge it measures and before nothing, since the merge
        # already spends it. Shared-core classification and the complexity
        # ceilings are unchanged.
        #
        # 3148 -> 3293 admits `opencode-json`, a fourth reader (sd:1329), and
        # with it a seventh lane member: `bin/sd_opencode.py` enters because
        # `bin/sd-review` imports it, and the lane is that import closure.
        # It belongs inside the lane rather than beside it because a reader
        # is review mechanics and nothing else -- the argv that confines the
        # child, the inline agent whose permission map is what refuses a
        # write or an inherited MCP tool, and the read-back that turns a
        # stream into findings. A wrong reader is a wrong verdict or an
        # unconfined child, which is exactly what this ratchet keeps small
        # enough to read whole; `sd-ship` never launches a reviewer, so none
        # of it is shared core. The 145 lines are 8 in `bin/sd-review` --
        # the import, two tuple entries, three two-line dispatch branches --
        # and 137 in the module: the builders, the default-deny map
        # (`*: deny`, a read-only allow-list, `read` refusing `mcp:*`), the
        # NDJSON read-back, and the docstring that records what was measured
        # on 1.18.30 (a by-name denylist let `github_get_me` run; under `*`
        # no server tool is offered; the resource readers fall to `mcp:*`;
        # a write, a bash command and a read outside the project refused)
        # and what stays open (the servers still connect, and no event names
        # the model that answered). Raised to the measured size of the
        # merged tree, in its own commit after the merge that spent it.
        # Shared-core classification and the complexity ceilings are
        # unchanged.
        #
        # 3293 -> 3399 closes the config-merge escape (sd:1375). The first
        # cut confined the child with the permission map alone, but opencode
        # deep-merges the reviewed checkout's own `opencode.json` over the
        # inline config when it launches inside that checkout, and a hostile
        # checkout's `{"bash": "allow", "<server>_<tool>": "allow"}` survives
        # `*: deny` under last-match evaluation -- reproduced live, the merged
        # config started the checkout's MCP server and a `bash touch` wrote
        # into the tree. The fix launches opencode from a neutral directory,
        # never the checkout: `IsolationError` and `assert_isolated_launch`
        # in `bin/sd_opencode.py` refuse a launch dir the checkout could
        # reach (it is the checkout, or an ancestor carries opencode config),
        # `bin/sd-review` launches from `workdir` and records why `--dir` is
        # not the way in, and the docstring records the second confinement
        # layer and the residual: the material is embedded, so no checkout
        # read is granted, and the residual paragraph names the cost of that
        # (this reviewer cannot make an out-of-diff finding, with `#1146`'s
        # `never_skip` override as the shape of what is lost) and the bounded
        # way to reverse it. The 106 lines are the two new symbols and their
        # comment in the module, the launch branch and its note in
        # `bin/sd-review`, and the docstring's added paragraphs. This raise
        # is its own commit, before the one that spends it: the rule this
        # file states is that a cap is never raised in the change that busts
        # it, and a raise nobody can read separately is the failure that rule
        # exists to prevent. So the cap sits above the measurement here until
        # the next commit adds the lines it pays for. Shared-core
        # classification and the complexity ceilings are unchanged.
        #
        # 3399 -> 3506 is the merge-forward of `origin/main` at `30902a68`.
        # This branch's opencode chain (3148 -> 3293 -> 3399) and main's
        # sd:1343 chain (documented above, 3148 -> 3255) branched from the
        # same 3148, so neither number is the size of the tree that carries
        # both. The merged tree measures 3506: sd:1343 adds +107 to
        # `bin/sd-review` (its `classify_gate`/`_not_found_report` receipt
        # reasoning) and this branch adds the opencode reader (+145) and the
        # isolation fix (+106), all on the one `bin/sd-review` and
        # `bin/sd_opencode.py`. Measured, not carried: `sd-review` is 2310
        # and `sd_opencode.py` is 223 on the merged tree. This raise is its
        # own commit, before the merge commit that spends it -- the cap leads
        # the merge, and at this commit the lane is still 3399, under 3506.
        # Shared-core classification and the complexity ceilings are
        # unchanged.
        #
        # 3506 -> 3553 is sd:1405 and sd:1406. `bin/sd-review` grows +40
        # (`reviewed_paths`, `unlocated` and its docstring, and the one call
        # in the dispatch loop): a completed answer whose findings cite no
        # reviewed path is voided, and a failed answer's unlocated advice is
        # dropped. `bin/sd_review_readiness.py` grows +7 for the
        # `subject_empty` blocker and its comment. Measured, not carried:
        # `sd-review` is 2350 on this tree. This raise is its own commit,
        # before the one that spends it. Shared-core classification and the
        # complexity ceilings are unchanged.
        #
        # 3553 -> 3639 is sd:1375. `bin/sd_opencode.py` grows +81: the
        # docstring paragraph recording why the neutral launch dir is not the
        # boundary (+17), and `confined_rules`, `probe_argv`, `resolved_rules`
        # and `confinement_breach` (+64), which ask opencode what it resolved
        # and refuse any rule after `*: deny` that is not the map.
        # `bin/sd-review` grows +5 for the one call and its comment. Measured,
        # not carried: `sd-review` is 2355 and `sd_opencode.py` is 304 on this
        # tree. Shared-core classification and the complexity ceilings are
        # unchanged.
        #
        # 3639 -> 3641 is sd:1253. `bin/sd_jev.py` grows +2 for `CALLER` and
        # its one-line comment; the two `jev` calls gain their ledger flags
        # on existing lines. Measured, not carried: `sd_jev.py` is 205 on this
        # tree. This raise is its own commit, before the one that spends it.
        # Shared-core classification and the complexity ceilings are
        # unchanged.
        #
        # 3641 -> 3645 is sd:1602. `bin/sd-review` grows +4: the review
        # prompt tells each lane not to report cosmetic findings and names
        # the contract text that is never cosmetic. Measured, not carried:
        # `sd-review` is 2351 on this tree. Shared-core classification and
        # the complexity ceilings are unchanged.
        #
        # 3645 -> 3649 is sd:1556 (PR #1200), one named source for the
        # reviewer order. `bin/sd-review` grows +4: the `order_source` report
        # key, the two-line `order from` render, and a second line for the
        # `codex auth` text, which no longer says `ok` for an auth mode read
        # from a file with no call made. Both are what the lane reports about
        # its own chain, so they belong in it. Which store the order came from
        # is `sd_registry.order_source`, shared core, so the resolution is not
        # spent here. The branch raised 3641 -> 3645 alongside sd:1602 on main,
        # so the merge of main stacks it on top. Measured, not carried:
        # `sd-review` is 2355 on the merged tree. Shared-core classification
        # and the complexity ceilings are unchanged.
        #
        # 3649 -> 3652 is #1194 (PR #1199), `sd-review --lens research-brief`.
        # `bin/sd-review` grows +3: the `--lens` argument, the `lens` report
        # key, and the one-line comment saying a lens, like `--challenge`, is
        # a request to be read that `docs_skip` does not answer. The lens is
        # part of the prompt and of the tier decision, which is review
        # mechanics, so its switch belongs in the lane. The lens text itself
        # is `sd_lib.REVIEW_LENSES`, shared core, so none of it is spent here.
        # The branch raised 3645 -> 3648 alongside sd:1602; the merge of
        # sd:1556 on main stacks it on top. Measured, not carried: `sd-review`
        # is 2358 on the merged tree. Shared-core classification and the
        # complexity ceilings are unchanged.
        # 3652 -> 3699 is sd:1821. MiniMax-M3 broke the findings schema on 13
        # of 45 runs; the URL contract restates the validated shape last (10
        # lines), and a free entry retries one schema failure on itself.
        # Moving the url call out of `run_provider` to hold the complexity
        # ceiling costs the rest: three helper signatures and docstrings.
        # 3699 -> 3702 is sd:1843, no-CI mode. `bin/sd_setup_github.py` grows
        # +3: one branch in `main` that prints why a `repo.ci = local`
        # repository gets no workflow and exits 0. The reason and the row read
        # are `sd_setup_guard.ci_local_skip` and `sd_lib.ci_mode`, outside the
        # lane, so only the dispatch is spent here.
        # 3702 -> 3709 is sd:1910, `-C <dir>` on the lane commands. `bin/sd-review`
        # grows +7: the argument (kept for `--help`) and `-C` parsed once from
        # the raw argv before the `setup-github` dispatch, for both paths, with
        # its usage error printed (a try, an except, a print, a return, two
        # comment lines). The parse and the change of directory are
        # `sd_lib.enter_directory_from_argv` and `sd_lib.enter_directory`,
        # shared core, so none of that is spent here. Measured on the branch.
        # 3709 -> 3736 is `setup-github --remove`, the supported way for a
        # `repo.ci = local` repository (sd:1843) to shed a route workflow that
        # never runs. `bin/sd_setup_github.py` grows +27: the flag, its
        # dispatch in `main` (refused beside `--check`, which never writes), `--check` naming a tracked workflow REMOVE there,
        # `remove` itself, and `_apply`, which the install and the removal now
        # share as the lane's one write site. What goes and what stays --
        # the guard lifted out of `dependabot.yml`, kept while another
        # workflow pins the action -- is `sd_setup_guard.removal` and
        # `unguarded`, outside the lane, so none of that is spent here.
        # 3736 -> 4036 is sd:2041, `sd-review --gate-check`: `sd-ship
        # prepare` runs the review's deterministic gate as the merge gate
        # does, so a pass leaves a receipt the merge gate reuses instead of
        # running the check a second time. `bin/sd_gate_run.py` joins the
        # lane at 229 lines: the clean worktree, the scrubbed environment and
        # the receipt lookup, split out of `sd_local_gate` so the lane imports
        # the run and not the status post. It is the local gate's code moved,
        # not new code; the merge gate runs the same lines. `bin/sd-review`
        # grows +71: `run_gate_check`, the `--gate-check` flag and its
        # refusals, and a `check_seconds` bound in the timing plan beside the
        # reviewers' phase. The binding and its age limit are
        # `sd_gate_receipts`, outside the lane, so none of that is spent here.
        # 4036 -> 4042 is the one-handoff window (sd:2041 review): prepare's
        # gate check passes `reuse=False` and `sd_gate_run` takes `reuse` and
        # `record`, so a receipt spans prepare to merge and nothing else.
        # 4042 -> 4052 is sd:2132: Jev may raise the review tier, never lower
        # it. `bin/sd_jev.py` grows +8: the floor (3) and the docstring that
        # states it (5). `bin/sd-review` grows +2: the route reason names an
        # answer below the routed tier. Measured on the branch.
        # 4052 -> 4065 is sd:2107: `bin/sd_jev.py` passes `jev --subject`, the
        # judged change as `sd-review-tier:<owner>.<repo>:<sha12>`, so a later
        # labeller can score the tier from the pull request's outcome. It grows
        # +12 (`_jev_subject`, the optional argv pair, the `root` argument) and
        # `bin/sd-review` +1 (passing `root`). Reading `origin` and `HEAD` is
        # `sd_lib.github_head`, shared core, so none of that is spent here.
        # 4065 -> 4161 is sd:2181: `bin/sd_review_material.py` grows +96 to send
        # a media file (PNG, JPEG, GIF, WebP, WOFF, PDF by magic bytes) as
        # its sizes and hashes instead of `git diff --binary` base64, to send
        # what git calls binary but is UTF-8 or BOM-marked UTF-16 as a text
        # diff, and to read both sides in one `cat-file --batch`; three helpers
        # keep each under the complexity ceiling.
        # 4161 -> 4196 is sd:2181 review pass 2: a summarized path makes a
        # material-only reader's coverage partial, so its answer does not count
        # toward depth (`partial` in `bin/sd-review`, `coverage` in the manifest),
        # and a text diff names a UTF-16 or BOM encoding, sending an
        # encoding-only change in binary form (`encoding`, `text_diff`).
        # 4196 -> 4264 is sd:2325, sd:2326 and sd:2328 in `sd_check_receipts.py`:
        # exact tracked paths the secret-name filter admits, a digest of the
        # declared environment alone, and each rustup toolchain bound by its
        # contents. They let a Rust repository declare `complete: true`.
        # 4264 -> 4286 is sd:2325 review: an explicit `+toolchain`, `rustup run`
        # or declared `+<toolchain>` binds the toolchain it selects, or refuses.
        # 4286 -> 4338 is sd:2327: declared build outputs, which a recorded run
        # builds into fresh temporary folders through each output's variable,
        # removed after the run; the operator's folder is never touched.
        # 4338 -> 4348 is sd:2327 review: a toolchain link that dangles, loops or
        # resolves outside the toolchain refuses the binding.
        # 4348 -> 4354 is sd:1397: `bin/sd-review` grows +6 for
        # `request_sha256`, the digest of what the reviewers are asked, which
        # `sd-ship` replays at a moved binding to keep an unchanged receipt.
        # The replay itself is `bin/sd_ship_review.py`, outside the lane.
        # 4354 -> 4520 is sd:2445: opencode 2.x dropped `debug agent` and
        # `--pure`. `bin/sd_opencode.py` grows +163: the probe that replaces
        # `debug agent`, a private `opencode serve` polled for the lazily
        # loaded agent and its plugins (+67 with `__main__`); the report,
        # version, plugin and tail checks (+42); the 2.x permission list and
        # environment (+32); the docstring's 2.x measurements (+22).
        # `bin/sd-review` grows +3 for the run's own config dir. Measured, not
        # carried: `sd_opencode.py` is 467 on this tree.
        # 4520 -> 4550 is sd:2400: `bin/sd_review_material.py` grows +30 for
        # `renamed_material`, which sends a rename git detects as its rename
        # patch under the new path and a one-line record under the old one, so
        # a folder move fits the input limit and both paths stay listed.
        # 4550 -> 4594 is six receipt-friction items, raised in its own commit
        # before the commits that spend it. sd:2431: `bin/sd_review_material.py`
        # +2, `file_material` returns its `is_media` verdict so a text file that
        # starts with the media marker is not summarized. sd:1560: `bin/sd-review`
        # +17 for `timed_out` in `classify_gate`, `gate_failure` for the runner's
        # own timeout line, and the `--timeout` sentence in `gate_failed_line`;
        # `bin/sd_check_receipts.py` +8 for `EXAMPLE` and `declaration_needed`,
        # so a refusal names the declaration file. sd:2386: receipts +4, one
        # fixed `LOCALE` recorded in the binding. sd:1296: `bin/sd-review` +2,
        # the `--reuse-check` help says the pack never reuses. sd:1635:
        # `bin/sd-review` +11, `DEFECT_CLASSES` in the code review prompt.
        # 4594 -> 4605 is sd:2432, raised in its own commit before the one that
        # spends it: `bin/sd_review_material.py` +11 for `RENAMED` and the
        # `--binary -M` fallback in `binary_material`, so a renamed and edited
        # non-media binary is sent as its rename delta, not a full literal.
        # 4605 -> 4608 is sd:2076: `bin/sd_gate_run.py` grows +3 so the gate's
        # child drops `FORCE_COLOR`, `CLICOLOR_FORCE` and `PY_COLORS` and gets
        # `NO_COLOR=1` and `PYTHON_COLORS=0`: the wrapped tuple and the
        # commented constant. A terminal's colour failed another repo's gate.
        # 4608 -> 4682 is sd:2359: `bin/sd_jev.py` +69 passes the routed tier
        # as `--baseline` and the routing time as `--baseline-ms`, retries once
        # without them when an older `jev` refuses the flag, and says why in the
        # module docstring (+19); `bin/sd-review` +5 times `sd_route.route`.
        # 4682 -> 4683 is sd:1912: `bin/sd_gate_run.py` +1 asks
        # `sd_gate_receipts.tree_key` whether the head declares a tree key; the
        # key, binding and docstring for it live in `sd_gate_receipts`, outside
        # the lane.
        # 4683 -> 4687 is sd:2493: `bin/sd_gate_run.py` +4 to run the check
        # inside `sd_gate_cache.cargo_environment` (+1), drop the operator's
        # `CARGO_TARGET_DIR` (+1 comment line), import the module (+1) and
        # say so in `gate_environment` (+1). The cache itself is
        # `bin/sd_gate_cache.py`, gate code outside the lane as
        # `sd_gate_receipts` is: `sd-review` does not import it.
        # 4687 -> 4698 is sd:2608: `bin/sd_gate_run.py` +11 so a failed gate's
        # one-line summary names the steps `sd-check` says failed (`summary_row`,
        # +6 with its blank lines) within the status bound (+1), then the file
        # with the whole output (+2) after `WHOLE_OUTPUT` (+2 with its comment),
        # which `sd_local_gate` cuts from the public status. Reading the steps
        # out of the output is `bin/sd-check`'s, outside the lane.
        #
        # 4698 -> 4718 is sd:2605, under the operator's 4900 ceiling (ruling of
        # 2026-10-04): `bin/sd-review` +21 (2545 -> 2566 lines; the lane sat at
        # 4697) runs the reviewers before the gate. The gate step moved below
        # dispatch into `after_gate`, with the input-change refusal it took
        # along; a blocked review records `check: not_run` instead of running
        # it; and `render` prints that check. The gate that adjudicated
        # findings still need runs in `sd_ship_review`, outside the lane.
        #
        # 4718 -> 4723 is sd:2523, under the operator's 4900 ceiling (ruling of
        # 2026-10-04): `bin/sd-review` +5 holds a machine-wide review slot
        # through `sd_lib.review_slot` from the first reviewer to the last
        # (+1), refuses when none comes free (+1), and gives it back in a
        # `try`/`finally` (+3) on every exit, before the gate. The
        # slot logic is `bin/sd_review_slots.py`, outside the lane: `sd-review`
        # does not import it.
        #
        # 4723 -> 4820 is sd:2092, under the operator's 4900 ceiling (ruling of
        # 2026-10-04), raised in its own commit before the one that spends it:
        # `bin/sd-review` +3 asks the shadow triage after the reviewers, and
        # `bin/sd_jev.py` +94 holds it with the shadow helpers `sd task add`
        # shares (sd:2093): the public-repository gate, one shadow `choice`,
        # the loud stop, and the section saying what is sent. The duplicate
        # hint itself is in `bin/sd_work.py`, outside the lane. Review rounds
        # then spent 4820 -> 4841: `--hostname github.com` on the visibility
        # question (+3), `errors="replace"` in `_jev_run` (+1), and
        # `shadow_safely`, which turns any exception in a shadow stage into a
        # note and checks the switch before any probe (+17). The lane review of
        # b527597e9 spent 4841 -> 4849: the local Kev by default and hosted Jev
        # only on the machine's `SD_JEV_SHADOW_HOSTED=1`, as `sd-docs-lint` does.
        #
        # 4849 -> 4873 is sd:2969, under the operator's 4900 ceiling (ruling of
        # 2026-10-04): `bin/sd_jev.py` +24 asks the hint-blind twin,
        # `JEV_SD_REVIEW_BLIND`, the tier question without the rule's reason, in
        # shadow: `_jev_blind` (+15 with its docstring and blank lines), its call
        # (+1), its stage constant (+2), the module paragraph saying what it
        # sends (+5) and a docstring line on the reasonless state (+1). The
        # operator approved the stage for the Jev experiment (sd:2968). Main sat
        # at 4848 when this was set; the 4849 left room for sd:2954's `JEV_RUN`
        # line, which may land before or after this.
        #
        # 4873 -> 4770 is sd:3013: the shadow triage and its helpers moved to
        # `bin/sd_jev_shadow.py`, which records and never reads, so it is not
        # review logic. `bin/sd-review` imports `sd_jev` only, so the cap no
        # longer counts them; `sd_jev.jev_triage` is the one delegate left.
        #
        # 4770 -> 4777 is sd:2993 and sd:2978: `bin/sd_gate_run.py` +7 for the
        # gate's `core.fsmonitor=false` and its `sd-check` process group. That
        # branch was cut before sd:3013 lowered the cap.
        #
        # 4777 -> 4798 is sd:3499, under the operator's 4900 ceiling: receipts
        # name the reviewer binary's version, `codex --version` beside the skill
        # probe and the opencode probe's version in the outcome (`bin/sd-review`
        # +13), and a probe that runs out of time names its stage and elapsed
        # seconds and reads as a timeout, not a breach (`bin/sd_opencode.py` +24).
        #
        # 4798 -> 4813 is sd:5109, under the operator's 4900 ceiling: the review
        # policy and the gate's reuse key read the repo row, and a row that does
        # not read refuses (`bin/sd_gate_run.py` +4, `bin/sd_check_receipts.py` +6).
        #
        # 4813 -> 4848 is sd:5988, under the operator's 4900 ceiling:
        # `record_lead_review` (+38 in `bin/sd-review`) writes a clean plain
        # branch pass to the workflow database, so lane prepare runs the gate
        # alone on the head the lead already reviewed; main sat at 4810.
        #
        # 4848 -> 4857 is sd:6088, under the operator's 4900 ceiling:
        # `bin/sd_review_material.py` +9 collects the material again, with a
        # deleted file as its header only, when it exceeds the review limit.
        #
        # 4857 -> 4899 is pack sd:6028, under the operator's 4900 ceiling:
        # `blocker_checklist` (+42 in `bin/sd-review`) gives the reviewer the
        # blocker classes the repository's ship receipts hold, read-only, and
        # anchors the read at the base so a replayed request stays the same.
        # 4899 -> 4937 is pack sd:8503, the operator raising the 4900
        # ceiling (2026-11-12): each reader's wall clock and reported cost
        # (+38 in `bin/sd-review`), so a report shows what each reader took.
        lane = sorted(REVIEW_LANE)
        total = sum(_lines(path) for path in lane)
        self.assertLessEqual(
            total,
            4937,
            f"the review lane is {total} lines across {[p.name for p in lane]}",
        )

    # The `bin/` ceiling used to be asserted here too, at 8,000. R11-D15
    # re-derived it at 14,000 and updated `tests/test_loc_caps.py` and
    # `tests/test_verb_inventory.py`, but not this third copy, which sat 6,000
    # lines below the governing number until the next change to `bin/` tripped
    # it. One cap, one place was the answer then; R11-D48 has since retired the
    # `bin/` ceiling outright, so there is no governing number to drift from and
    # nothing here to re-add. **The lane ceiling above is a different number
    # with a different record and is NOT retired** -- do not read R11-D48 as
    # covering it. `test_the_migration_tools_stay_under_theirs` below is still
    # a duplicate of `test_loc_caps.py`, currently in agreement, which is
    # exactly the state the bin ceiling was in before it drifted.
    #
    # What replaced the `bin/` ceiling is `tests/test_code_health.py`, and it
    # is deliberately not a number this file could hold a second copy of: its
    # ceilings are per function, so nothing here aggregates to a total that
    # could drift from one. The three line-count ceilings left in the
    # repository are the lane total above, the migration ceiling checked by
    # `test_the_migration_tools_stay_under_theirs` below, and the caps
    # `tests/test_loc_caps.py` still keeps.

    def test_the_shared_core_exemption_names_files_that_exist(self) -> None:
        # The one hand-written name in the lane's derivation. A rename that
        # emptied it would silently move core lines onto the lane's budget --
        # or, worse, quietly shrink the lane and hide a real overrun.
        missing = sorted(name for name in SHARED_CORE if name not in _BIN_MODULES)
        self.assertEqual(missing, [])

    def test_the_migration_tools_stay_under_theirs(self) -> None:
        migrations = [path for path in BIN_FILES if path.name.startswith("migrate-")]
        total = sum(_lines(path) for path in migrations)
        self.assertLessEqual(
            total,
            1500,
            f"migrate-* is {total} lines across {[p.name for p in migrations]}",
        )


if __name__ == "__main__":
    unittest.main()
