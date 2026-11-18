"""Fixtures for bin/sd-review: policy, routing, the runner seam, and disposition.

Every provider here is a fake runner. The point of the seam is that no test in
this file starts a real codex, prism, gito or kimi process, and none of them
reaches a network: a test that needed one would be a test proving the seam is
not a seam.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from typing import Any, Mapping, Sequence

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SD_REVIEW = REPO_ROOT / "bin" / "sd-review"
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))


def load_module() -> Any:
    """Import `bin/sd-review` as a module.

    It ships without a `.py` suffix, so the loader has to be named: the default
    finder recognises files by extension and returns no spec for this one.
    """

    loader = importlib.machinery.SourceFileLoader("sd_review_under_test", str(SD_REVIEW))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


sd_review = load_module()
from tests import row_seed  # noqa: E402


class FakeRunner:
    """A runner that answers from a script and records what it was handed.

    It records the environment it received, not the environment the caller
    intended to build: the credential-scrub assertions in
    `test_sd_review_codex.py` read `calls[i]["env"]` for exactly that reason.
    """

    def __init__(self, answers: Mapping[str, Any] | None = None, default: Any = None) -> None:
        self.answers = dict(answers or {})
        self.default = default or sd_review.Completed(0, '{"findings": []}', "")
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: pathlib.Path,
        timeout: int,
        input_text: str | None = None,
    ) -> Any:
        self.calls.append(
            {"argv": list(argv), "env": dict(env), "cwd": pathlib.Path(cwd), "timeout": timeout, "stdin": input_text}
        )
        # The opencode confinement probe runs as `python sd_opencode.py probe
        # ... opencode`, so it answers as the opencode program it asks about.
        probe = sd_review.sd_opencode.probe_program(list(argv))
        program = pathlib.Path((probe or argv)[0]).name
        if not probe and (program.startswith("python") or argv[-1] == "--json" and "sd-check" in " ".join(argv)):
            program = "sd-check"
        answer = self.answers.get(program, self.default)
        if callable(answer):
            return answer(argv, env, cwd, timeout)
        # The skill probe (sd:1248) reads a render, not a findings document,
        # so the unscripted default is not its answer: an unscripted probe
        # gets the measured shape with no block. A scripted answer, callable
        # or not, keeps control of what the probe sees.
        if program not in self.answers and list(argv[1:3]) == SKILL_PROBE_WORDS:
            return sd_review.Completed(0, PROMPT_INPUT_SUPPRESSED, "")
        # The opencode confinement probe (sd:1375) reads a resolved agent, so
        # an unscripted one gets the agent a clean launch resolves.
        if program not in self.answers and probe:
            return sd_review.Completed(0, resolved_agent(), "")
        return answer


#: The skill-suppression probe's argv, by the two words that identify it.
SKILL_PROBE_WORDS = ["debug", "prompt-input"]

#: Built-in plugins as `plugin.list` lists them on 2.0.20 (two of seventy).
BUILTIN_PLUGINS = [{"id": "opencode.agent", "source": {"type": "builtin"}, "features": {"server": True},
                    "state": {"status": "active"}},
                   {"id": "opencode.config.agent", "source": {"type": "builtin"}, "features": {"server": True},
                    "state": {"status": "active"}}]


def resolved_agent(*extra: dict[str, str], version: str = "opencode v2.0.20",
                   plugins: list[dict[str, Any]] | None = None) -> str:
    """The probe's report in the shape measured on 2.0.20: opencode's defaults,
    its tool-output allowance among them, then the map's `*: deny` and its
    rules. `extra` lands right after the deny, where a widening would."""

    deny, *confined = sd_review.sd_opencode.confined_rules()
    defaults = [{"action": "*", "resource": "*", "effect": "allow"},
                {"action": "external_directory", "resource": "*", "effect": "ask"},
                {"action": "external_directory", "resource": "/h/.local/share/opencode/tool-output/*",
                 "effect": "allow"}]
    agent = {"id": "sd-review", "name": "sd-review", "mode": "primary",
             "permissions": [*defaults, deny, *extra, *confined]}
    return json.dumps({"version": version, "agent": agent,
                       "plugins": BUILTIN_PLUGINS if plugins is None else plugins})


def prompt_input(*texts: str) -> str:
    """One `codex debug prompt-input` render in the shape measured on 0.155.1:
    a list of `message` items, each with a `content` list of text parts."""

    return json.dumps([{"type": "message", "role": "developer",
                        "content": [{"type": "input_text", "text": text}]} for text in texts])


#: A render with skill instructions loaded, and one with the key in effect.
PROMPT_INPUT_LOADED = prompt_input("<skills_instructions>\n## Skills", "<permissions instructions>")
PROMPT_INPUT_SUPPRESSED = prompt_input("<permissions instructions>")


def codex_sessions(runner: Any) -> list[dict[str, Any]]:
    """The codex calls that are review sessions, not the skill probe.

    `codex debug prompt-input` (sd:1248) runs the same program under the same
    name, so a test that filtered on `argv[0] == "codex"` saw one call before
    it existed and sees two after. Filtering here rather than in each test
    keeps one definition of what a review session is. Its `--version` read
    (sd:3499) is not a session either.
    """

    return [call for call in runner.calls
            if call["argv"][0] == "codex" and call["argv"][1:3] != SKILL_PROBE_WORDS
            and call["argv"][1:] != ["--version"]]


def chat_answer(content: str, **extra: Any) -> tuple[int, str, str, bool]:
    """One OpenAI-compatible response, as the client hands it back."""
    message: dict[str, Any] = {"content": content, **extra}
    return (0, json.dumps({"choices": [{"message": message}]}), "", True)


class FakeClient:
    """The second seam records what the client sent.

    `sent` is the assertion that carries the weight. For an entry this
    repository has not consented to -- a host that moved, a scheme edited down
    to cleartext -- it must stay empty, and no other kind of test can show
    that: a mocked-out refusal proves only that the mock refused.
    """

    def __init__(
        self,
        answers: Mapping[str, Any] | None = None,
        default: Any = None,
    ) -> None:
        self.answers = dict(answers or {})
        self.default = default or chat_answer('{"findings": []}')
        self.sent: list[dict[str, Any]] = []

    def __call__(
        self,
        provider: Any,
        prompt: str,
        env: Mapping[str, str],
        timeout: int,
    ) -> tuple[int, str, str, bool]:
        self.sent.append(
            {"provider": provider.name, "url": provider.url, "prompt": prompt,
             "env": dict(env), "timeout": timeout}
        )
        answer = self.answers.get(provider.name, self.default)
        return answer(provider) if callable(answer) else answer


def namespace(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "scope": "worktree",
        "challenge": False,
        "explain": False,
        "dry_run": False,
        "json": False,
        "draft": False,
        "provider": None,
        "timeout": 60,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


#: The fixture's own registry, deliberately not the shipped one. A test that
#: read `providers.yaml` would assert against whichever providers happen to be
#: pinned this month, and every chain assertion below would change meaning the
#: next time an entry is added. Two entries with the same reader is the shape
#: the chain tests need: a second reviewer that actually runs.
FIXTURE_REGISTRY = """
bills:
  first:  { cost: subscription }
  second: { cost: subscription }

providers:
  codex:  { start: "codex exec", vendor: openai, bill: first,
            roles: [reviewer], reader: codex-json, env: [] }
  second: { start: "second exec", vendor: secondvendor, bill: second,
            roles: [author, reviewer], reader: codex-json, env: [] }

roles:
  author: [second]
  reviewer: [codex, second]
"""

#: What the fixture repository consents to. `entry@executable` for both, which
#: is what `recipient()` derives from each entry's start line.
FIXTURE_CONSENT = "codex@codex, second@second"


class ReviewFixture(unittest.TestCase):
    #: sd:7519 made every reviewing tier ask two readers. A class whose tests
    #: pin the chain's mechanics with one reader per tier -- fallthrough, the
    #: ledger, the meter, unlocated answers -- sets this to 1: those paths are
    #: the same at any depth, and `test_sd_review_parallel.py` runs the shipped two.
    reviewing_depth: int | None = None

    def one_reader_per_tier(self, depth: int = 1) -> None:
        """Pin every reviewing tier's depth for this test; `reviewing_depth` does it for a class."""
        for pinned in (unittest.mock.patch.dict(sd_review.sd_route.TIER_DEPTH, {
                tier: depth for tier in ("cheap", "standard", "deep")}),
                unittest.mock.patch.object(sd_review.sd_route, "DEFAULT_DEPTH", depth)):
            pinned.start()
            self.addCleanup(pinned.stop)

    def setUp(self) -> None:
        if self.reviewing_depth is not None:
            self.one_reader_per_tier(self.reviewing_depth)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        self.registry_home = self.tmp / "registry-home"
        self.tool_bin = self.tmp / "tools"
        self.tool_bin.mkdir()
        for name in ("codex", "second", "third", "claude", "fourth", *(f"p{i}" for i in range(10))):
            executable = self.tool_bin / name
            executable.write_text("#!/usr/bin/env python3\nraise SystemExit('fixture must use FakeRunner')\n")
            executable.chmod(0o700)
        (self.registry_home / ".local" / "share" / "sd").mkdir(parents=True)
        (self.registry_home / ".local" / "share" / "sd" / "providers.yaml").write_text(
            FIXTURE_REGISTRY, encoding="utf-8"
        )

    def assert_no_session_started(self, runner: Any) -> None:
        """No review session ran. At most the skill-suppression probe did.

        `--explain` starts one local process on a ready `codex-json` lane:
        `debug prompt-input`, which renders a prompt offline and bills nothing
        (sd:1248), and its `--version` read (sd:3499). Named here rather than
        relaxed to "some calls", so another process arriving under `--explain`
        still fails every caller.
        """

        started = [call["argv"][1:3] for call in runner.calls]
        self.assertIn(started, ([], [SKILL_PROBE_WORDS, ["--version"]]), started)

    def declare_policy(self, root: pathlib.Path, policy: Any) -> None:
        self.declare(root, "review", policy)

    def stub_policy(self, policy: Any) -> None:
        """`policy` as the review declaration this process reads, and no other kind declared (sd:5109).

        For an in-process run whose test is what a machine with no database does: no database is made.
        `declare_policy` writes a real row, which a child process reads too.
        """
        def declared(root: Any, kind: str, environ: Any = None) -> Any:
            return policy if kind == "review" else None
        patcher = unittest.mock.patch.object(sd_review.sd_lib, "repo_declaration", side_effect=declared)
        patcher.start()
        self.addCleanup(patcher.stop)

    def declare(self, root: pathlib.Path, kind: str, value: Any) -> None:
        """`root`'s `kind` declaration on its row (sd:5109), in a database under the fixture's HOME.

        This process reads that HOME too for the rest of the test, as a run under `environment()` does.
        """
        row_seed.seed_declaration(self.registry_home, root, kind, value)
        patcher = unittest.mock.patch.dict(os.environ, {"HOME": str(self.registry_home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def environment(self, **extra: str) -> dict[str, str]:
        """An environment whose HOME is the fixture's, so the run reads the
        fixture's registry rather than the developer's. A gate it starts reads
        no machine load (sd:2262): a test must not wait for a busy machine."""
        return {"HOME": str(self.registry_home), "PATH": str(self.tool_bin) + os.pathsep + os.defpath,
                "SD_GATE_LOAD_MAX": "0", "SD_GATE_SETTLE_SECONDS": "0", **extra}

    def local_block(self, root: pathlib.Path, *lines: str) -> None:
        body = "\n".join((f"{sd_review.sd_lib.CONSENT_KEY}: {FIXTURE_CONSENT}", *lines))
        (root / "CLAUDE.local.md").write_text(
            "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\n"
            f"{body}\n"
            "<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n",
            encoding="utf-8",
        )

    def make_repo(self, name: str = "repo") -> pathlib.Path:
        root = self.tmp / name
        root.mkdir(parents=True)
        for args in (
            ["init", "--quiet", "--initial-branch", "main"],
            ["config", "user.email", "fixture@example.invalid"],
            ["config", "user.name", "Fixture"],
        ):
            subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)
        (root / "README.md").write_text("seed\n", encoding="utf-8")
        self.local_block(root)
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "seed"], cwd=str(root), check=True, capture_output=True
        )
        return root

    def chatgpt_home(self, key: Any = None, mode: str = "chatgpt") -> pathlib.Path:
        home = self.tmp / f"codex-home-{len(list(self.tmp.iterdir()))}"
        home.mkdir(parents=True)
        payload: dict[str, Any] = {"auth_mode": mode, "OPENAI_API_KEY": key}
        (home / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
        return home


class PolicyTests(ReviewFixture):
    def test_absent_file_uses_the_documented_default(self) -> None:
        root = self.make_repo()
        policy, source = sd_review.load_policy(root)
        self.assertEqual(source, "built-in default")
        self.assertEqual(policy, sd_review.DEFAULT_POLICY)

    def test_declared_policy_is_merged_over_the_default(self) -> None:
        root = self.make_repo()
        self.declare_policy(root, {"severity_floor": "low"})
        policy, source = sd_review.load_policy(root)
        self.assertEqual(policy["severity_floor"], "low")
        self.assertEqual(policy["large_change_lines"], 800)
        # Not said by the declaration, so not decided by it: the machine answers.
        self.assertEqual(policy["copilot_review"], {"automatic_deep": None})
        self.assertEqual(source, sd_review.POLICY_SOURCE)

    def test_a_policy_file_in_the_tree_is_not_read(self) -> None:
        """sd:5109: `.github/sd-review.json` is retired; the row alone declares the policy."""
        root = self.make_repo()
        (root / ".github").mkdir()
        (root / ".github" / "sd-review.json").write_text(json.dumps({"severity_floor": "low"}), encoding="utf-8")
        self.declare_policy(self.make_repo("other"), {"severity_floor": "medium"})
        policy, source = sd_review.load_policy(root)
        self.assertEqual((policy, source), (sd_review.DEFAULT_POLICY, "built-in default"))

    def test_default_severity_floor_is_the_fleet_default_high(self) -> None:
        """sd:1657: the operator ruled the fleet floor `high`."""
        self.assertEqual(sd_review.DEFAULT_POLICY["severity_floor"], "high")

    def assert_rejects(self, payload: Any, fragment: str) -> None:
        self._rejections = getattr(self, "_rejections", 0) + 1
        root = self.make_repo(f"reject-{self._rejections}")
        self.declare_policy(root, payload)
        with self.assertRaises(sd_review.PolicyError) as caught:
            sd_review.load_policy(root)
        self.assertIn(fragment, str(caught.exception))

    def test_malformed_policies_name_what_is_wrong(self) -> None:
        # Not JSON and not an object no longer reach a reader: the row holds an object or nothing
        # (`TheDeclarationsSetting`), and `sd-db repo declare` refuses both before writing.
        self.assert_rejects({"nonsense": 1}, "unknown key(s) nonsense")
        self.assert_rejects({"tiers": {"cheap": []}}, "tiers is retired")
        self.assert_rejects({"default_tier": "gold"}, "default_tier must be one of")
        self.assert_rejects({"categories": [{"paths": ["a"]}]}, "name must be a non-empty string")
        self.assert_rejects({"categories": [{"name": "x", "paths": []}]}, "must name at least one glob")
        self.assert_rejects({"large_change_lines": -1}, "non-negative integer")
        self.assert_rejects({"large_change_lines": True}, "non-negative integer")
        self.assert_rejects({"severity_floor": "urgent"}, "severity_floor must be one of")
        self.assert_rejects({"copilot_review": []}, "copilot_review must be an object")
        self.assert_rejects({"copilot_review": {}}, "automatic_deep must be a boolean")
        self.assert_rejects({"copilot_review": {"automatic_deep": []}},
                            "automatic_deep must be a boolean")
        self.assert_rejects({"copilot_review": {"automatic_deep": False, "review_new_pushes": True}},
                            "unknown key(s) review_new_pushes")
        # A string value, not a list: retirement is checked by name, before any
        # type rule, and this shape is the one `test_cut_symbols` bounds out.
        self.assert_rejects({"authors": "x"}, "authors is retired")
        self.assert_rejects({"tier_order": ["a", "a"]}, "must not repeat a tier")
        self.assert_rejects({"challenge_providers": ["x"]}, "challenge_providers is retired")
        self.assert_rejects({"planning_providers": ["x"]}, "planning_providers is retired")

    def test_a_broken_policy_never_falls_back_to_the_default(self) -> None:
        root = self.make_repo()
        self.declare_policy(root, {"default_tier": "cheap"})
        sd_review.sd_lib.import_sd_db().module.default_path(self.registry_home).write_bytes(b"not a database")
        with self.assertRaises(sd_review.PolicyError) as caught:
            sd_review.load_policy(root)
        self.assertIn(f"{sd_review.POLICY_SOURCE} cannot be read", str(caught.exception))


def _planned_for(case: Any, *, start: str) -> list[dict[str, Any]]:
    return sd_review._planned([case.provider(start=start)], pathlib.Path("/repo"), "prompt")


class ReaderTests(ReviewFixture):
    """What a registry entry's `reader` decides, now that no table does."""

    def provider(self, **overrides: Any) -> Any:
        fields: dict[str, Any] = {
            "name": "someone",
            "vendor": "somevendor",
            "bill": "first",
            "start": "someone review",
            "reader": "codex-json",
        }
        fields.update(overrides)
        return sd_review.sd_registry.Provider(**fields)

    def test_an_unimplemented_reader_is_not_run_and_names_itself(self) -> None:
        runner = FakeRunner()
        outcome = sd_review.run_provider(
            self.provider(reader="unimplemented-json"),
            pathlib.Path("/nonexistent"),
            sd_review.Subject("worktree", "HEAD", "worktree", (), 0, ""),
            "prompt",
            runner,
            {},
            60,
        )
        self.assertEqual(outcome.status, sd_review.NOT_RUN)
        self.assertIn("unimplemented-json", outcome.detail)
        self.assertEqual(runner.calls, [], "an unreadable provider is not started")

    def test_a_url_entry_does_not_borrow_a_start_entry_s_words(self) -> None:
        """Copilot found this. `refuse_environment` ran first and
        unconditionally -- it speaks of what a spawned session inherits and
        ends "No session was started" -- so a `url` entry, which spawns
        nothing, was answered in the language of a mechanism it does not use.

        The entry runs now, and the environment here still holds a URL on
        purpose: that is what used to trigger the start-session refusal on an
        entry that starts nothing.
        """
        runner = FakeRunner()
        client = FakeClient()
        outcome = sd_review.run_provider(
            self.provider(start=None, url="https://api.example/v1", reader=None),
            pathlib.Path("/nonexistent"),
            sd_review.Subject("worktree", "HEAD", "worktree", (), 0, ""),
            "prompt",
            runner,
            {"SOMEVENDOR_KEY": "https://elsewhere.example"},
            60,
            client=client,
        )
        self.assertEqual(outcome.status, sd_review.CLEAN)
        self.assertNotIn("No session was started", outcome.detail)
        self.assertEqual(runner.calls, [], "a url entry spawns nothing")
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(outcome.argv, ())
        self.assertEqual(outcome.scrubbed, (), "nothing inherits an environment")

    def test_the_three_places_that_ask_give_one_answer(self) -> None:
        """The chain marks it, the dry run plans it, the run reports it. Each
        had its own sentence, and two of the three were wrong about the same
        case, so fixing one left the others saying the old thing."""
        provider = self.provider(reader="unimplemented-json")
        expected = sd_review.sd_registry.refuse_reader(provider, sd_review.READERS)
        self.assertIsNotNone(expected)
        planned = sd_review._planned([provider], pathlib.Path("/nonexistent"), "prompt")
        self.assertEqual(planned[0]["reason"], expected)
        self.assertFalse(planned[0]["would_run"])

    def test_the_dry_run_offers_a_url_entry_s_endpoint_where_an_argv_would_be(
        self,
    ) -> None:
        """The same three places, for the entry kind that has no argv. Marking
        it `would_run: False` would be the dry run and the real run disagreeing
        again, now in the other direction."""
        provider = self.provider(start=None, url="https://api.example/v1", reader=None)
        row = sd_review._planned([provider], pathlib.Path("/nonexistent"), "prompt")[0]
        self.assertTrue(row["would_run"])
        self.assertEqual(row["argv"], [])
        self.assertEqual(row["endpoint"], "POST https://api.example/v1/chat/completions")

    def test_an_entry_whose_start_line_names_no_program_is_refused(self) -> None:
        """Copilot found this. `shlex.split("")` is empty, so the hardened
        invocation's first flag became the executable and the run tried to
        start `--sandbox`."""

        runner = FakeRunner()
        outcome = sd_review.run_provider(
            self.provider(start=""),
            pathlib.Path("/nonexistent"),
            sd_review.Subject("worktree", "HEAD", "worktree", (), 0, ""),
            "prompt",
            runner,
            {},
            60,
        )
        self.assertEqual(outcome.status, sd_review.REFUSED)
        self.assertIn("names no program", outcome.detail)
        self.assertEqual(runner.calls, [], "nothing is started")

    def test_the_dry_run_does_not_offer_an_argv_that_starts_with_a_flag(self) -> None:
        """The same defect reached `--dry-run`, which prints the invocation for
        a person to read. Asserted over the whole plan rather than one entry, so
        a future reader that forgets the check fails here too."""

        for row in _planned_for(self, start=""):
            self.assertFalse(row["would_run"])
            self.assertEqual(row["argv"], [])
        for row in _planned_for(self, start="codex exec"):
            self.assertTrue(row["would_run"])
            self.assertFalse(row["argv"][0].startswith("-"), row["argv"])

    def test_the_entrys_start_line_is_what_runs(self) -> None:
        """`codex-json` names a protocol, not one executable. An entry that
        speaks it through a wrapper runs the wrapper, and a reader that
        hardcoded `codex` would silently review with the wrong binary."""

        argv = sd_review.codex_argv(
            pathlib.Path("/repo"), pathlib.Path("/work"), "wrapped codex exec"
        )
        self.assertEqual(argv[:3], ["wrapped", "codex", "exec"])
        self.assertIn("--sandbox", argv)

    def test_skill_instructions_are_suppressed_like_project_instructions(self) -> None:
        """A committed `.agents/skills/<name>/SKILL.md` writes into the
        reviewing model's context on every run unless this key is set, and no
        `--enable`/`--disable` feature flag reaches skills. Lose the key and
        the checkout under review instructs its own reviewer again (C-40)."""

        argv = sd_review.codex_argv(pathlib.Path("/repo"), pathlib.Path("/work"))
        self.assertIn("skills.include_instructions=false", argv)
        # A `-c` value, not a bare word: misplaced, codex reads it as a prompt.
        self.assertEqual(argv[argv.index("skills.include_instructions=false") - 1], "-c")


class SubjectTests(ReviewFixture):
    def setUp(self) -> None:
        super().setUp()
        self.row_home = self.tmp / "row-home"
        self.row_home.mkdir()
        patched = unittest.mock.patch.dict(os.environ, {"HOME": str(self.row_home)})
        patched.start()
        self.addCleanup(patched.stop)

    def item(self, root: pathlib.Path, name: str, status: str, branch: str) -> pathlib.Path:
        """A work item whose status and branch are the row's, as the build leaves them."""
        folder = root / "docs" / "work" / name
        folder.mkdir(parents=True)
        (folder / "prd.md").write_text("---\ntitle: probe\n---\n\n- [ ] one\n", encoding="utf-8")
        row_seed.seed_row(self.row_home, root, folder, status, branch=branch)
        return folder

    def test_worktree_scope_sees_modified_and_untracked_files(self) -> None:
        root = self.make_repo()
        (root / "README.md").write_text("seed\nchanged\n", encoding="utf-8")
        (root / "new.py").write_text("x = 1\n", encoding="utf-8")
        subject = sd_review.resolve_subject(root, "worktree")
        self.assertEqual(subject.base, "HEAD")
        self.assertEqual(subject.paths, ("README.md", "new.py"))
        self.assertGreater(subject.lines, 0)

    def test_branch_scope_is_the_committed_delta_from_the_merge_base(self) -> None:
        root = self.make_repo()
        subprocess.run(["git", "checkout", "--quiet", "-b", "topic"], cwd=str(root), check=True)
        (root / "feature.py").write_text("y = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "feature"], cwd=str(root), check=True, capture_output=True
        )
        subject = sd_review.resolve_subject(root, "branch")
        self.assertEqual(subject.paths, ("feature.py",))
        self.assertNotEqual(subject.base, subject.head)

    def test_worktree_scope_ignores_committed_history(self) -> None:
        root = self.make_repo()
        subprocess.run(["git", "checkout", "--quiet", "-b", "topic"], cwd=str(root), check=True)
        (root / "committed.py").write_text("y = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "c"], cwd=str(root), check=True, capture_output=True
        )
        self.assertEqual(sd_review.resolve_subject(root, "worktree").paths, ())

    def test_planning_scope_reads_the_active_work_item(self) -> None:
        root = self.make_repo()
        item = self.item(root, "2026-01-01-thing", "in_progress", "topic")
        (item / "design.md").write_text("design\n", encoding="utf-8")
        subject = sd_review.resolve_subject(root, "planning")
        self.assertEqual(
            subject.paths,
            ("docs/work/2026-01-01-thing/design.md", "docs/work/2026-01-01-thing/prd.md"),
        )

    def test_planning_scope_without_an_active_item_is_a_usage_error(self) -> None:
        root = self.make_repo()
        with self.assertRaises(sd_review.UsageError):
            sd_review.resolve_subject(root, "planning")

    # -- criterion 31: planning scope is the item this branch is on ---------
    #
    # The fixture the criterion names: two planning items and one `ready`
    # item. `ready` is not a reviewable state and must not appear whatever
    # branch is checked out, so it is in every case below as a control.
    #
    # What these replace: one test that asserted the union -- two items, no
    # `--item`, two paths, no refusal. That was the behaviour, and the
    # subject it produced still described itself as "the active work item's
    # planning documents", singular. Owner decision 2026-09-19 took the
    # description over the behaviour.

    def plan_fixture(self, first: str = "topic-one", second: str = "topic-two") -> pathlib.Path:
        root = self.make_repo()
        self.item(root, "2026-01-01-first", "planning", first)
        self.item(root, "2026-01-02-second", "planning", second)
        self.item(root, "2026-01-03-shipped", "ready", "topic-one")
        return root

    def checkout(self, root: pathlib.Path, branch: str) -> None:
        subprocess.run(["git", "checkout", "--quiet", "-B", branch],
                       cwd=str(root), check=True, capture_output=True)

    def test_planning_scope_picks_the_item_whose_branch_is_checked_out(self) -> None:
        root = self.plan_fixture()
        self.checkout(root, "topic-two")
        self.assertEqual(
            sd_review.resolve_subject(root, "planning").paths,
            ("docs/work/2026-01-02-second/prd.md",),
        )
        self.checkout(root, "topic-one")
        self.assertEqual(
            sd_review.resolve_subject(root, "planning").paths,
            ("docs/work/2026-01-01-first/prd.md",),
        )

    def test_planning_scope_refuses_when_no_active_item_is_on_this_branch(self) -> None:
        root = self.plan_fixture()
        self.checkout(root, "somewhere-else")
        with self.assertRaises(sd_review.UsageError) as caught:
            sd_review.resolve_subject(root, "planning")
        message = str(caught.exception)
        self.assertIn("somewhere-else", message)
        self.assertIn("--item", message)
        for name in ("2026-01-01-first", "2026-01-02-second"):
            self.assertIn(name, message)
        self.assertNotIn("2026-01-03-shipped", message)

    def test_planning_scope_refuses_when_two_active_items_share_this_branch(self) -> None:
        """Ambiguity the branch cannot settle is refused, not guessed at."""
        root = self.plan_fixture(first="shared", second="shared")
        self.checkout(root, "shared")
        with self.assertRaises(sd_review.UsageError) as caught:
            sd_review.resolve_subject(root, "planning")
        self.assertIn("more than one", str(caught.exception))

    def test_one_active_item_off_its_branch_is_still_the_subject(self) -> None:
        """One candidate is not an ambiguity, so the branch decides nothing.

        Refusing here would stop planning review on every feature branch to
        answer a question that was never asked.
        """
        root = self.make_repo()
        self.item(root, "2026-01-01-only", "planning", "topic")
        self.checkout(root, "somewhere-else")
        self.assertEqual(
            sd_review.resolve_subject(root, "planning").paths,
            ("docs/work/2026-01-01-only/prd.md",),
        )

    def test_item_narrows_planning_scope_whatever_branch_is_checked_out(self) -> None:
        root = self.plan_fixture()
        self.checkout(root, "somewhere-else")
        one = sd_review.resolve_subject(root, "planning", "2026-01-02-second")
        self.assertEqual(one.paths, ("docs/work/2026-01-02-second/prd.md",))

    def test_a_ready_item_is_never_the_planning_subject(self) -> None:
        root = self.plan_fixture()
        self.checkout(root, "topic-one")
        self.assertEqual(
            sd_review.resolve_subject(root, "planning").paths,
            ("docs/work/2026-01-01-first/prd.md",),
        )
        with self.assertRaises(sd_review.UsageError):
            sd_review.resolve_subject(root, "planning", "2026-01-03-shipped")

    def test_item_that_names_no_active_item_is_a_usage_error_naming_the_active_ones(self) -> None:
        root = self.make_repo()
        self.item(root, "2026-01-01-thing", "planning", "topic")
        with self.assertRaises(sd_review.UsageError) as caught:
            sd_review.resolve_subject(root, "planning", "2026-01-01-other")
        self.assertIn("2026-01-01-thing", str(caught.exception))

    def test_item_outside_planning_scope_is_a_usage_error(self) -> None:
        root = self.make_repo()
        with self.assertRaises(sd_review.UsageError):
            sd_review.resolve_subject(root, "worktree", "2026-01-01-thing")


class DispositionTests(unittest.TestCase):
    def test_the_floor_decides_blocking_from_advisory(self) -> None:
        findings = [
            {"severity": "high", "summary": "a"},
            {"severity": "medium", "summary": "b"},
            {"severity": "low", "summary": "c"},
            {"severity": "unspecified", "summary": "d"},
        ]
        disposed = sd_review.dispose(findings, "medium")
        self.assertEqual(
            [record["disposition"] for record in disposed],
            ["blocking", "blocking", "advisory", "advisory"],
        )
        raised = sd_review.dispose(findings, "high")
        self.assertEqual([record["disposition"] for record in raised][:2], ["blocking", "advisory"])

    def test_unspecified_never_blocks_even_at_the_lowest_floor(self) -> None:
        disposed = sd_review.dispose([{"severity": "unspecified", "summary": "d"}], "unspecified")
        self.assertEqual(disposed[0]["disposition"], "advisory")


class ParseTests(unittest.TestCase):
    def test_the_codex_shape_and_the_prism_shape_both_normalise(self) -> None:
        flat = sd_review.parse_findings(
            json.dumps(
                {"findings": [{"path": "a.py", "line": 3, "severity": "high", "summary": "s", "family": "f"}]}
            )
        )
        assert flat is not None
        self.assertEqual(flat.findings[0]["path"], "a.py")
        self.assertEqual(flat.error, "")
        nested = sd_review.parse_findings(
            json.dumps(
                {
                    "findings": [
                        {
                            "locations": [{"path": "b.py", "lines": {"start": 9}}],
                            "severity": "low",
                            "title": "t",
                            "category": "testing",
                        }
                    ]
                }
            )
        )
        assert nested is not None
        self.assertEqual(nested.findings[0], {"path": "b.py", "line": 9, "severity": "low", "summary": "t", "family": "testing"})
        self.assertIn("schema", nested.error)

    def test_non_json_and_wrong_shapes_are_not_findings(self) -> None:
        self.assertIsNone(sd_review.parse_findings("boom"))
        self.assertIsNone(sd_review.parse_findings(""))
        self.assertIsNone(sd_review.parse_findings(json.dumps({"issues": []})))

    def test_a_finding_missing_its_path_is_incomplete_not_clean(self) -> None:
        parsed = sd_review.parse_findings(json.dumps({"findings": [{"summary": "s"}]}))
        self.assertEqual(parsed.findings[0]["path"], "<unknown>")
        self.assertIn("schema", parsed.error)

    def test_excess_findings_are_bounded_and_omissions_block_completion(self) -> None:
        many = [
            {"path": "a", "line": None, "severity": "low", "summary": str(index), "family": "f"}
            for index in range(sd_review.MAX_FINDINGS + 10)
        ]
        parsed = sd_review.parse_findings(json.dumps({"findings": many}))
        assert parsed is not None
        self.assertEqual(len(parsed.findings), sd_review.MAX_FINDINGS)
        self.assertIn("omitted", parsed.findings[-1]["summary"])
        self.assertEqual(parsed.findings[-1]["severity"], "high")
        self.assertIn("limits", parsed.error)


class ClassifyTests(unittest.TestCase):
    def test_rate_limited_is_not_unavailable(self) -> None:
        limited = sd_review.Completed(1, "", "You have hit your usage limit. Try again at 3pm.")
        self.assertEqual(sd_review.classify_failure(limited), sd_review.RATE_LIMITED)
        broken = sd_review.Completed(1, "", "panic: bad flag")
        self.assertEqual(sd_review.classify_failure(broken), sd_review.UNAVAILABLE)

    def test_a_process_that_never_launched_is_unavailable(self) -> None:
        missing = sd_review.Completed(127, "", "codex: not found on PATH", launched=False)
        self.assertEqual(sd_review.classify_failure(missing), sd_review.UNAVAILABLE)

    def test_the_word_rate_limit_in_findings_text_still_reads_as_a_quota_stop(self) -> None:
        # Deliberate: the classifier only runs on a FAILED invocation, so a
        # successful run whose findings discuss rate limits never reaches it.
        ok = sd_review.Completed(0, '{"findings": [{"path": "a", "line": 1, "severity": "low", "summary": "rate limit handling", "family": "x"}]}', "")
        self.assertIsNotNone(sd_review.parse_findings(ok.stdout))


class PipelineTests(ReviewFixture):
    def prepare(self, root: pathlib.Path) -> None:
        (root / "src.py").write_text("x = 1\n", encoding="utf-8")

    def run_review(
        self,
        root: pathlib.Path,
        runner: FakeRunner,
        env: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> dict[str, Any]:
        return sd_review.review(
            root,
            namespace(**overrides),
            runner,
            self.environment(**dict(env or {})),
            self.chatgpt_home(),
        )

    def test_a_clean_codex_run_is_clean(self) -> None:
        root = self.make_repo()
        self.prepare(root)
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", ""), "codex": sd_review.Completed(0, '{"findings": []}', "")})
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["remote_reviews"]["copilot"], {
            "automatic": False, "tier": "standard", "policy": "deep", "source": "machine default", "repository": None})

    def test_automatic_deep_keeps_the_remote_review_report_shape(self) -> None:
        root = self.make_repo()
        self.prepare(root)
        self.declare_policy(root, {"sensitive": ["src.py"], "copilot_review": {"automatic_deep": True}})
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", ""),
                             "codex": sd_review.Completed(0, '{"findings": []}', "")})
        result = self.run_review(root, runner)
        self.assertEqual(result["remote_reviews"]["copilot"], {
            "automatic": True, "tier": "deep", "policy": "deep", "source": "repository", "repository": True})

    @staticmethod
    def programs(runner: FakeRunner) -> list[str]:
        """Each call by what it ran: `sd-check` for the gate, else the program's name."""
        return ["sd-check" if "sd-check" in " ".join(call["argv"]) else pathlib.Path(call["argv"][0]).name
                for call in runner.calls]

    def test_a_failing_gate_after_a_clean_review_is_gate_failed(self) -> None:
        """sd:2605. The gate runs once the review has cleared, and its failure is the answer."""
        root = self.make_repo()
        self.prepare(root)
        runner = FakeRunner({"sd-check": sd_review.Completed(1, "{}", "lint failed")})
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "gate_failed")
        self.assertEqual(result["cleared_status"], "clean")  # sd:2721: what sd-ship restores once a gate passes
        self.assertEqual(self.programs(runner)[-1], "sd-check")
        self.assertIn("codex", self.programs(runner)[:-1])

    def test_a_blocking_review_never_starts_the_gate(self) -> None:
        """sd:2605. A blocking finding refuses the head whatever the gate says, so
        the gate, the longest step, does not run, and the report says it did not."""
        root = self.make_repo()
        self.prepare(root)
        payload = json.dumps(
            {"findings": [{"path": "src.py", "line": 1, "severity": "high", "summary": "bad", "family": "correctness"}]}
        )
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", ""), "codex": sd_review.Completed(0, payload, "")})
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "blocking")
        self.assertNotIn("sd-check", self.programs(runner))
        self.assertEqual(result["check"]["status"], "not_run")
        self.assertIn("the review is blocking", result["check"]["reason"])
        stream = io.StringIO()
        sd_review.render(result, stream)
        self.assertIn("check: not run -- the review is blocking", stream.getvalue())

    def test_an_unmanaged_repository_gets_the_review_and_no_gate(self) -> None:
        """pack sd:5953. Every gate road refuses an unmanaged repository: sd-check,
        the worktree gate and a receipt. The review runs alone; the check is not
        run, never passed, and the report says why."""
        root = self.make_repo()
        self.prepare(root)
        why = sd_review.sd_lib.unmanaged_text("~/theirs")
        refusal = sd_review.Completed(2, "", f"sd-check: error: {why}")
        receipt = {"status": "pass", "exit_code": 0, "detail": "", "checks": [], "source": "receipt"}
        for road in ({}, {"gate_check": "main"}, {"reuse_check": True}):
            with self.subTest(**road), \
                    unittest.mock.patch.object(sd_review.sd_lib, "unmanaged", return_value=why), \
                    unittest.mock.patch.object(sd_review.sd_check_receipts, "reuse_checked_result",
                                               return_value=receipt) as reuse:
                runner = FakeRunner({"sd-check": refusal, "codex": sd_review.Completed(0, '{"findings": []}', "")})
                result = self.run_review(root, runner, **road)
                self.assertEqual(result["status"], "clean", result["check"])
                self.assertNotIn("sd-check", self.programs(runner))
                reuse.assert_not_called()
                self.assertEqual(result["check"]["status"], "not_run")
                stream = io.StringIO()
                sd_review.render(result, stream)
                self.assertIn(f"check: not run -- {why}; review only", stream.getvalue())

    # sd:1343 -- a worktree of this pack has no .venv, and `make check` died
    # on the toolchain before any reviewer ran. The receipt has to say what is
    # missing, not just that the gate failed -- and no more than it can prove.
    # The codex review of 23f2f429 ran a helper that passed its check and then
    # hit a missing postprocessor, and this lane called /bin/sh a missing
    # interpreter. What a shell's text proves is which command it could not
    # find; what sd-check's own spawn failure proves is that nothing ran.
    def test_a_make_gate_whose_interpreter_is_absent_names_the_path(self) -> None:
        root = self.make_repo()
        self.prepare(root)
        payload = json.dumps({"checks": [{
            "name": "check", "command": ["make", "check"], "status": "fail", "exit_code": 2,
            "stderr": "/bin/sh: .venv/bin/python: No such file or directory\nmake: *** [lint] Error 127\n",
        }]})
        runner = FakeRunner({"sd-check": sd_review.Completed(1, payload, "")})
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "gate_failed")
        self.assertEqual(result["check"]["reason"], "command_not_found")
        self.assertEqual(result["check"]["missing_command"], ".venv/bin/python")
        self.assertIsNone(result["check"]["missing_line"])
        self.assertNotIn("interpreter", result["check"])
        self.assertEqual(self.programs(runner)[-1], "sd-check")
        stream = io.StringIO()
        sd_review.render(result, stream)
        self.assertIn(".venv/bin/python was not found", stream.getvalue())
        self.assertNotIn("no check ran", stream.getvalue())

    def test_a_gate_command_that_exits_127_names_the_command_the_shell_reported(self) -> None:
        """Through the real sd-check and a real process."""
        root = self.make_repo()
        (root / "gate.sh").write_text(
            "#!/bin/sh\nprintf '%s\\n' '/bin/sh: .venv/bin/python: No such file or directory' >&2\nexit 127\n",
            encoding="utf-8")
        self.local_block(root, "check: sh gate.sh")
        report = sd_review.run_check(root, sd_review.subprocess_runner, self.environment(), 60)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["checks"][0]["exit_code"], 127)
        self.assertEqual(report["reason"], "command_not_found")
        self.assertEqual(report["missing_command"], ".venv/bin/python")

    def test_a_gate_whose_own_program_cannot_be_spawned_is_a_missing_toolchain(self) -> None:
        """The one proof that no check ran: sd-check could not spawn the
        program the check names, and reports it as exit_code None."""
        root = self.make_repo()
        interpreter = root / "no-such-venv" / "bin" / "python"
        self.local_block(root, f"check: {interpreter} -c pass")
        report = sd_review.run_check(root, sd_review.subprocess_runner, self.environment(), 60)
        self.assertEqual(report["status"], "fail")
        self.assertIsNone(report["checks"][0]["exit_code"])
        self.assertEqual(report["reason"], "toolchain_missing")
        self.assertEqual(report["interpreter"], str(interpreter))
        self.assertTrue(report["evidence"].startswith("cannot run "), report["evidence"])
        self.assertIn("no check ran", sd_review.gate_failed_line(report))

    def test_a_recipe_that_passed_its_check_then_lost_a_command_is_not_a_missing_toolchain(self) -> None:
        """The reviewer's case against 23f2f429: exit 127 after a check ran."""
        root = self.make_repo()
        self.prepare(root)
        (root / "helper.sh").write_text("#!/bin/sh\necho check passed\npostproc-sd1343\n", encoding="utf-8")
        self.local_block(root, "check: sh helper.sh")
        report = sd_review.run_check(root, sd_review.subprocess_runner, self.environment(), 60)
        self.assertEqual(report["checks"][0]["exit_code"], 127)
        self.assertEqual(report["checks"][0]["stdout"], "check passed\n")
        self.assertEqual(report["reason"], "command_not_found")
        self.assertEqual(report["missing_command"], "postproc-sd1343")
        self.assertEqual(report["missing_line"], 3)
        self.assertNotIn("interpreter", report)
        runner = FakeRunner({"sd-check": sd_review.Completed(1, json.dumps({"checks": report["checks"]}), "")})
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "gate_failed")
        stream = io.StringIO()
        sd_review.render(result, stream)
        text = stream.getvalue()
        self.assertIn("postproc-sd1343 was not found at line 3", text)
        self.assertNotIn("no check ran", text)
        self.assertNotIn("interpreter", text)

    def test_a_gate_that_failed_on_its_own_says_nothing_about_missing_tools(self) -> None:
        # A "No such file or directory" inside a failing suite's output without
        # a 127 is a finding, not a missing-command report.
        checks = [{"name": "test", "command": ["make", "test"], "status": "fail", "exit_code": 2,
                   "stderr": "cat: fixture.txt: No such file or directory\nmake: *** [test] Error 1\n"}]
        self.assertIsNone(sd_review.classify_gate(checks))
        self.assertIsNone(sd_review.classify_gate(None))
        # A 127 with no shell report names nothing rather than guessing the
        # program: `make` started, or there would be no exit code to read.
        self.assertEqual(sd_review.classify_gate(
            [{"name": "check", "command": ["make", "check"], "status": "fail", "exit_code": 127, "stderr": ""}]),
            {"reason": "command_not_found", "missing_command": None, "missing_line": None, "evidence": "exit 127",
             "entrypoint": "check", "started": ["check"]})

    def test_one_entrypoint_that_cannot_spawn_does_not_say_no_check_ran(self) -> None:
        """The fix-verification review of 30e45b6b: no aggregate `check`, a
        `test` that runs and passes, a `lint` sd-check cannot spawn. The
        sentence names lint and what it could not start; it may not say no
        check ran, because test did."""
        root = self.make_repo()
        self.prepare(root)
        interpreter = root / "no-such-venv" / "bin" / "python"
        self.local_block(root, "test: sh -c 'echo test passed'", f"lint: {interpreter} -m ruff check .")
        report = sd_review.run_check(root, sd_review.subprocess_runner, self.environment(), 60)
        by_name = {record["name"]: record for record in report["checks"]}
        self.assertEqual((by_name["test"]["status"], by_name["test"]["exit_code"]), ("pass", 0))
        self.assertIsNone(by_name["lint"]["exit_code"])
        line = sd_review.gate_failed_line(report)
        self.assertNotIn("no check ran", line)
        self.assertIn("lint could not start " + str(interpreter), line)
        self.assertIn("test ran", line)
        self.assertEqual(report["reason"], "toolchain_missing")
        self.assertEqual(report["entrypoint"], "lint")
        self.assertEqual(report["started"], ["test"])
        runner = FakeRunner({"sd-check": sd_review.Completed(1, json.dumps({"checks": report["checks"]}), "")})
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "gate_failed")
        stream = io.StringIO()
        sd_review.render(result, stream)
        self.assertNotIn("no check ran", stream.getvalue())
        self.assertIn("lint could not start", stream.getvalue())

    def test_a_check_sd_check_killed_at_its_timeout_started_and_the_line_says_so(self) -> None:
        """The timeout arm of `_started`, exercised with the record sd-check
        writes rather than one typed here: no aggregate `check`, a `test`
        sd-check kills at `--timeout 1`, a `lint` it cannot spawn. The test
        record has no exit code and a `timed out` reason, and it started --
        the process ran until it was killed. The sentence names lint and may
        not say no check ran. Against `_started` without the arm, `started`
        is empty and the line says no check ran, so this fails there."""
        root = self.make_repo()
        self.prepare(root)
        interpreter = root / "no-such-venv" / "bin" / "python"
        self.local_block(root, "test: sleep 30", f"lint: {interpreter} -m ruff check .")
        argv = [sys.executable, str(pathlib.Path(sd_review._BIN) / "sd-check"), "--json", "--timeout", "1"]
        completed = sd_review.subprocess_runner(argv, self.environment(), root, 60)
        checks = json.loads(completed.stdout)["checks"]
        by_name = {record["name"]: record for record in checks}
        self.assertEqual((by_name["test"]["status"], by_name["test"]["exit_code"]), ("fail", None))
        self.assertEqual(by_name["test"]["reason"], "timed out after 1s")
        self.assertTrue(sd_review._started(by_name["test"]), "killed at the timeout is not never started")
        self.assertFalse(sd_review._started(by_name["lint"]), "a spawn failure is")
        report = sd_review.classify_gate(checks)
        self.assertEqual((report["reason"], report["entrypoint"], report["started"]),
                         ("toolchain_missing", "lint", ["test"]))
        line = sd_review.gate_failed_line(report)
        self.assertNotIn("no check ran", line)
        self.assertIn("lint could not start " + str(interpreter), line)
        self.assertIn("test ran", line)
        runner = FakeRunner({"sd-check": sd_review.Completed(1, json.dumps({"checks": checks}), "")})
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "gate_failed")
        stream = io.StringIO()
        sd_review.render(result, stream)
        self.assertNotIn("no check ran", stream.getvalue())
        self.assertIn("test ran", stream.getvalue())

    def test_a_plain_gate_failure_names_the_failing_check_and_its_log(self) -> None:
        """sd:6075: a gate that failed with no missing tool or timeout named
        neither the failing check nor where sd-check kept its output. The
        record comes from the real sd-check, so the path is one it wrote."""
        root = self.make_repo()
        self.prepare(root)
        (root / "gate.sh").write_text("#!/bin/sh\necho 'FAIL: test_widget' >&2\nexit 1\n", encoding="utf-8")
        self.local_block(root, "test: sh gate.sh", "lint: sh -c 'echo lint passed'")
        report = sd_review.run_check(root, sd_review.subprocess_runner, self.environment(), 60)
        failed = next(record for record in report["checks"] if record["name"] == "test")
        self.assertTrue(pathlib.Path(failed["output_path"]).is_file(), failed)
        runner = FakeRunner({"sd-check": sd_review.Completed(1, json.dumps({"checks": report["checks"]}), "")})
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "gate_failed")
        stream = io.StringIO()
        sd_review.render(result, stream)
        text = stream.getvalue()
        self.assertIn(f"failed check: test: test exit 1; output: {failed['output_path']}", text)
        self.assertNotIn("failed check: lint", text)
        # A failed record with no kept output says so, and a gate with no
        # record at all says that, rather than naming nothing.
        bare = [{"name": "check", "status": "fail", "exit_code": 2, "failed_steps": ["make target check"],
                 "reason": "output not kept: disk full"}]
        self.assertEqual(sd_review.gate_failed_checks({"checks": bare}),
                         ["  failed check: check: make target check; output not kept: disk full"])
        self.assertEqual(sd_review.gate_failed_checks({"checks": None, "detail": "x\nsd-check: bad config"}),
                         ["  failed check: none recorded; sd-check said: sd-check: bad config"])

    def test_a_gate_that_timed_out_names_the_timeout_flag(self) -> None:
        """sd:1560: a gate stopped at its bound says so and names the flag that
        gives it longer, whether sd-check killed one entrypoint or the runner
        killed sd-check itself and left no record."""
        root = self.make_repo()
        self.prepare(root)
        killed = [{"name": "check", "command": ["make", "check"], "status": "fail", "exit_code": None,
                   "reason": "timed out after 3600s", "stderr": ""}]
        for label, answer, evidence in (
                ("record", sd_review.Completed(1, json.dumps({"checks": killed}), ""), "timed out after 3600s"),
                ("runner", sd_review.Completed(124, "", "python3: timed out after 3600s", False), "timed out after 3600s")):
            with self.subTest(label=label):
                result = self.run_review(root, FakeRunner({"sd-check": answer}))
                self.assertEqual(result["status"], "gate_failed")
                self.assertEqual((result["check"]["reason"], result["check"]["evidence"]), ("timed_out", evidence))
                line = sd_review.gate_failed_line(result["check"])
                self.assertIn("--timeout SECONDS", line)
                self.assertIn("--review-timeout", line)
                self.assertIn("the review had cleared before it ran", line)
        # A spawn failure beside a timeout is still the stronger evidence.
        spawn = [killed[0], {"name": "lint", "command": ["/no/python"], "status": "fail", "exit_code": None,
                             "reason": "cannot run /no/python: [Errno 2] No such file or directory"}]
        self.assertEqual(sd_review.classify_gate(spawn)["reason"], "toolchain_missing")

    def test_a_gate_that_waited_for_a_slot_reads_as_not_run_not_fail(self) -> None:
        """sd:6343: no gate slot came free is a busy gate, not a failed check.

        The head is still not gated, so the status and exit code stay
        `gate_failed`; the text says the check did not run and what to do.
        """
        root = self.make_repo()
        self.prepare(root)
        no_slot = [{"name": "check", "command": ["make", "check"], "status": "fail", "exit_code": None,
                    "reason": "no gate slot came free within 3600s", "stderr": "",
                    "failed_steps": ["check: no gate slot came free within 3600s"]}]
        result = self.run_review(root, FakeRunner({"sd-check": sd_review.Completed(1, json.dumps({"checks": no_slot}), "")}))
        self.assertEqual(result["status"], "gate_failed")
        self.assertEqual((result["check"]["reason"], result["check"]["evidence"]),
                         ("gate_busy", "no gate slot came free within 3600s"))
        stream = io.StringIO()
        sd_review.render(result, stream)
        text = stream.getvalue()
        self.assertIn("check: not run -- gate busy: no gate slot came free within 3600s", text)
        self.assertNotIn("check: fail", text)
        self.assertNotIn("failed check:", text)
        self.assertNotIn("--timeout SECONDS", text)
        self.assertIn("the lane runs the full gate", text)
        self.assertIn("sd gate status", text)

    def test_the_code_prompt_names_the_six_operator_listed_defect_classes(self) -> None:
        """sd:1635: the classes a retired manual checklist held, named in the lane's own prompt."""
        root = self.make_repo()
        self.prepare(root)
        code = sd_review.build_prompt(sd_review.resolve_subject(root, "worktree"), False, "")
        for phrase in ("external JSON used before its type is narrowed",
                       "an error that reports a stage other than the one that failed",
                       "identifiers matched by substring rather than at token boundaries",
                       "tests that touch files, network or environment without isolating them",
                       "empty, very long, non-ASCII or markup-bearing input",
                       "check every sibling site of the same shape, not only the reported line"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, code)
        planning = sd_review.Subject("planning", "", "", ("docs/work/x/prd.md",), 0, "")
        self.assertNotIn("external JSON", sd_review.build_prompt(planning, False, ""))

    def test_every_prompt_weighs_findings_for_one_developer(self) -> None:
        """sd:3563: KISS and one developer's speed reach every lane, code and planning, plain and challenge."""
        root = self.make_repo()
        self.prepare(root)
        code = sd_review.resolve_subject(root, "worktree")
        planning = sd_review.Subject("planning", "", "", ("docs/work/x/prd.md",), 0, "")
        for subject in (code, planning):
            for challenge in (False, True):
                prompt = sd_review.build_prompt(subject, challenge, "")
                with self.subTest(scope=subject.scope, challenge=challenge):
                    self.assertIn("one developer on their own machines", prompt)
                    self.assertIn("is low severity at most", prompt)
                    self.assertIn("Security, data loss and correctness defects still block", prompt)

    def test_a_blocking_finding_blocks(self) -> None:
        root = self.make_repo()
        self.prepare(root)
        payload = json.dumps(
            {"findings": [{"path": "src.py", "line": 1, "severity": "high", "summary": "bad", "family": "correctness"}]}
        )
        runner = FakeRunner(
            {"sd-check": sd_review.Completed(0, "{}", ""), "codex": sd_review.Completed(0, payload, "")}
        )
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "blocking")
        self.assertEqual(result["findings"][0]["disposition"], "blocking")
        self.assertEqual(result["findings"][0]["backend"], "codex")

    def test_a_rate_limited_provider_falls_through_without_a_shortfall(self) -> None:
        self.one_reader_per_tier()
        root = self.make_repo()
        self.prepare(root)
        runner = FakeRunner(
            {
                "sd-check": sd_review.Completed(0, "{}", ""),
                "codex": sd_review.Completed(1, "", "usage limit reached"),
                "second": sd_review.Completed(0, '{"findings": []}', ""),
            }
        )
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "clean")
        statuses = {row["backend"]: row["status"] for row in result["outcomes"]}
        self.assertEqual(statuses["codex"], sd_review.RATE_LIMITED)
        self.assertEqual(statuses["second"], sd_review.CLEAN)
        self.assertEqual(result["remaining"], [])
        self.assertEqual(result["reviewed_by"], ["second"])
        self.assertEqual((result["completed_reviews"], result["requested_reviews"]), (1, 1))

    def test_an_unavailable_provider_lets_the_chain_continue(self) -> None:
        self.one_reader_per_tier()
        root = self.make_repo()
        self.prepare(root)
        runner = FakeRunner(
            {
                "sd-check": sd_review.Completed(0, "{}", ""),
                "codex": sd_review.Completed(127, "", "codex: not found on PATH", False),
                "second": sd_review.Completed(0, '{"findings": []}', ""),
            }
        )
        result = self.run_review(root, runner)
        statuses = {row["backend"]: row["status"] for row in result["outcomes"]}
        self.assertEqual(statuses["codex"], sd_review.UNAVAILABLE)
        self.assertEqual(statuses["second"], sd_review.CLEAN)
        self.assertEqual(result["status"], "clean")
        self.assertEqual((result["completed_reviews"], result["requested_reviews"]), (1, 1))

    def test_every_provider_unavailable_is_not_a_clean_review(self) -> None:
        root = self.make_repo()
        self.prepare(root)
        runner = FakeRunner(
            {"sd-check": sd_review.Completed(0, "{}", "")},
            default=sd_review.Completed(127, "", "not found", False),
        )
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(sd_review.STATUS_EXIT[result["status"]], sd_review.EXIT_GATE)

    def test_rate_limited_and_unavailable_get_different_exit_codes(self) -> None:
        self.assertNotEqual(
            sd_review.STATUS_EXIT["rate_limited"], sd_review.STATUS_EXIT["unavailable"]
        )
        self.assertEqual(sd_review.STATUS_EXIT["rate_limited"], sd_review.EXIT_RATE_LIMITED)

    def test_explain_runs_no_review_and_only_the_skill_probe(self) -> None:
        root = self.make_repo()
        self.prepare(root)
        runner = FakeRunner()
        result = self.run_review(root, runner, explain=True)
        self.assertEqual(result["status"], "explained")
        self.assert_no_session_started(runner)
        self.assertEqual([call["argv"][1:3] for call in runner.calls], [SKILL_PROBE_WORDS, ["--version"]])
        self.assertTrue(result["route"]["reason"])

    def test_explain_digests_what_the_reviewers_would_be_asked(self) -> None:
        """sd:1397. sd-ship replays `--explain` at a moved binding and keeps the receipt on an equal digest."""
        root = self.make_repo()
        self.prepare(root)
        first = self.run_review(root, FakeRunner(), explain=True)["request_sha256"]
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(self.run_review(root, FakeRunner(), explain=True)["request_sha256"], first)
        self.assertNotEqual(self.run_review(root, FakeRunner(), explain=True, challenge=True)["request_sha256"], first)
        (root / "src.py").write_text("x = 2\n", encoding="utf-8")
        self.assertNotEqual(self.run_review(root, FakeRunner(), explain=True)["request_sha256"], first)

    def test_a_coverage_change_moves_the_request_digest_though_the_material_does_not(self) -> None:
        """sd:1397 review: `partial` marks a review incomplete from the manifest's `omitted_paths`.
        A change to `sd_review_material` coverage that leaves material, prompt and route alone must
        still move the digest, or a replay would keep a receipt the new code calls incomplete."""
        root = self.make_repo()
        self.prepare(root)
        first = self.run_review(root, FakeRunner(), explain=True)
        original = sd_review.sd_review_material.coverage

        def summarizing(inventory, overheads):
            return original([dict(row, summarized=True) for row in inventory], overheads)

        with unittest.mock.patch.object(sd_review.sd_review_material, "coverage", summarizing):
            moved = self.run_review(root, FakeRunner(), explain=True)
        self.assertNotEqual(moved["input_manifest"]["omitted_paths"], first["input_manifest"]["omitted_paths"])
        self.assertNotEqual(moved["request_sha256"], first["request_sha256"])

    def test_dry_run_prints_argv_and_runs_nothing(self) -> None:
        root = self.make_repo()
        self.prepare(root)
        runner = FakeRunner()
        result = self.run_review(root, runner, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(runner.calls, [])
        codex = [row for row in result["planned_invocations"] if row["backend"] == "codex"]
        self.assertTrue(codex[0]["would_run"])
        self.assertEqual(codex[0]["argv"][:2], ["codex", "exec"])

    def test_challenge_adds_a_stance_without_removing_the_chain(self) -> None:
        root = self.make_repo()
        self.prepare(root)
        runner = FakeRunner()
        plain = self.run_review(root, runner, dry_run=True)
        challenged = self.run_review(root, runner, dry_run=True, challenge=True)
        self.assertEqual(set(plain["providers"]) <= set(challenged["providers"]), True)
        prompt = " ".join(
            row["stdin"] for row in challenged["planned_invocations"] if row["would_run"]
        )
        self.assertIn("Argue against the approach itself", prompt)

    def test_the_local_block_reaches_the_prompt(self) -> None:
        root = self.make_repo()
        self.prepare(root)
        self.local_block(root, "check: make check")
        result = self.run_review(root, FakeRunner(), dry_run=True)
        self.assertTrue(result["local_block_prepended"])
        # Every planned reviewer gets the block, not only the first (sd:1002).
        prompts = [row["stdin"] for row in result["planned_invocations"] if row["would_run"]]
        self.assertTrue(prompts)
        for prompt in prompts:
            self.assertIn("check: make check", prompt)
            self.assertTrue(prompt.startswith("Repository-local conventions"))

    def test_without_a_local_file_no_block_is_prepended(self) -> None:
        root = self.make_repo()
        self.prepare(root)
        (root / "CLAUDE.local.md").unlink()
        result = self.run_review(root, FakeRunner(), dry_run=True)
        self.assertIs(result["local_block_prepended"], False)
        for row in result.get("planned_invocations") or []:
            self.assertNotIn("Repository-local conventions", row["stdin"] or "")

    def test_the_prompt_names_the_endpoints_the_scope_resolved(self) -> None:
        root = self.make_repo()
        subprocess.run(["git", "checkout", "--quiet", "-b", "topic"], cwd=str(root), check=True)
        (root / "feature.py").write_text("y = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "f\n\nAuthored-with: human"],
            cwd=str(root), check=True, capture_output=True
        )
        result = self.run_review(root, FakeRunner(), dry_run=True, scope="branch")
        prompt = [row for row in result["planned_invocations"] if row["would_run"]][0]["stdin"]
        self.assertIn(f"{result['subject']['base']}..{result['subject']['head']}", prompt)

    def test_a_docs_only_change_routes_to_skip_and_asks_nobody(self) -> None:
        root = self.make_repo()
        (root / "docs").mkdir()
        (root / "docs" / "note.md").write_text("hello\n", encoding="utf-8")
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})
        result = self.run_review(root, runner)
        self.assertEqual(result["route"]["tier"], "skip")
        self.assertEqual(result["providers"], [])
        self.assertEqual(result["status"], "skipped")


class CliTests(ReviewFixture):
    def run_cli(self, args: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        # The fixture's HOME, so the CLI reads the fixture registry. Without it
        # these tests would pass or fail on whether whoever runs them has run
        # the installer, which is not what they are about.
        return subprocess.run(
            [sys.executable, str(SD_REVIEW), *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "HOME": str(self.registry_home)},
        )

    def test_explain_against_this_repository_exits_zero(self) -> None:
        finished = self.run_cli(["--explain"], REPO_ROOT)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("route       tier", finished.stdout)
        self.assertIn("explain only, nothing ran", finished.stdout)

    def test_explain_json_is_one_object(self) -> None:
        finished = self.run_cli(["--explain", "--json"], REPO_ROOT)
        payload = json.loads(finished.stdout)
        self.assertEqual(payload["status"], "explained")

    def test_outside_a_repository_is_a_usage_error(self) -> None:
        finished = self.run_cli(["--explain"], self.tmp)
        self.assertEqual(finished.returncode, sd_review.EXIT_USAGE)
        self.assertIn("not inside a git repository", finished.stderr)

    def test_a_bad_timeout_is_a_usage_error(self) -> None:
        finished = self.run_cli(["--explain", "--timeout", "0"], REPO_ROOT)
        self.assertEqual(finished.returncode, sd_review.EXIT_USAGE)

    def test_a_malformed_policy_exits_two_without_a_traceback(self) -> None:
        root = self.make_repo()
        self.declare_policy(root, {"default_tier": "gold"})
        finished = self.run_cli(["--explain"], root)
        self.assertEqual(finished.returncode, sd_review.EXIT_USAGE)
        self.assertNotIn("Traceback", finished.stderr)
        self.assertIn("default_tier must be one of", finished.stderr)


class TheExplainRenderTests(ReviewFixture):
    """What `--explain` prints, and what a run that is not explaining must not.

    Copilot found this on the pull request. `render`'s explain block had been
    edited by text substitution and its nesting was wrong: the chain table sat
    inside the consent-refusal branch, so it printed only when consent was
    refused, and a real run that hit a consent refusal printed "explain only,
    nothing ran" and returned before its outcomes.

    The manual check that passed before this landed had both a missing registry
    and a missing consent line, which is the one combination under which the
    broken nesting looks right.
    """

    def explain(self, root: pathlib.Path, env: Mapping[str, str] | None = None) -> str:
        result = sd_review.review(
            root,
            namespace(explain=True),
            FakeRunner(),
            dict(env) if env is not None else self.environment(),
            self.chatgpt_home(),
        )
        stream = io.StringIO()
        sd_review.render(result, stream)
        return stream.getvalue()

    def test_the_chain_prints_when_nothing_is_refused(self) -> None:
        """The fixture repository consents to both entries and has a registry,
        so there is no refusal to carry the table into view."""

        root = self.make_repo()
        # A changed file: an empty subject is a readiness blocker (sd:1405).
        (root / "src.py").write_text("x = 1\n", encoding="utf-8")
        text = self.explain(root)
        self.assertIn("reviewer chain", text)
        self.assertIn("use codex", " ".join(text.split()))
        self.assertIn("second", text)
        # This lane is ready, so the skill probe ran and the footer says so
        # rather than claiming nothing started.
        self.assertIn("skill probe suppressed;", text)
        self.assertIn("explain only; the codex skill probe ran, no review", text)

    def test_a_real_run_does_not_print_the_explain_footer(self) -> None:
        """A run that reviews must not claim it explained. This is the half of
        the defect that changed what a real invocation reported."""

        root = self.make_repo()
        (root / "src.py").write_text("x = 1\n", encoding="utf-8")
        runner = FakeRunner(
            {
                "sd-check": sd_review.Completed(0, "{}", ""),
                "codex": sd_review.Completed(0, '{"findings": []}', ""),
            }
        )
        result = sd_review.review(
            root, namespace(), runner, self.environment(), self.chatgpt_home()
        )
        stream = io.StringIO()
        sd_review.render(result, stream)
        text = stream.getvalue()
        self.assertNotIn("explain only", text)
        self.assertIn("outcomes:", text)

    def test_a_consent_refusal_does_not_turn_a_real_run_into_an_explain(self) -> None:
        """The exact shape of the defect: a repository with no `reviewers`
        line, reviewed for real."""

        root = self.make_repo()
        (root / "CLAUDE.local.md").unlink()
        (root / "src.py").write_text("x = 1\n", encoding="utf-8")
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})
        result = sd_review.review(
            root, namespace(), runner, self.environment(), self.chatgpt_home()
        )
        stream = io.StringIO()
        sd_review.render(result, stream)
        text = stream.getvalue()
        self.assertNotIn("explain only", text)
        self.assertNotIn("reviewer chain", text)


class DepthCountsProvidersThatCanAnswerTests(ReviewFixture):
    """A tier's depth is a count of reviewers, not of chain positions.

    Copilot found this. Entries were marked eligible without regard to whether
    this build can run them, so on the shipped registry a `deep` change --
    depth 3, chain `codex, claude, minimax, ...` -- spent two of its three
    slots on readers that do not exist, ran one provider, and reported `clean`
    with exit 0. The commit that introduced the depth model claimed "a change
    is read by as many providers as before", which that made false.

    Whether a reader is implemented is a fact about the build, knowable without
    touching the machine, so unlike preflight it belongs in the chain.
    """

    def registry_with(self, *readers: str) -> pathlib.Path:
        entries = "\n".join(
            f'  p{i}: {{ start: "p{i} exec", vendor: v{i}, bill: first, '
            f"roles: [reviewer], reader: {reader}, env: [] }}"
            for i, reader in enumerate(readers)
        )
        names = ", ".join(f"p{i}" for i in range(len(readers)))
        text = (
            "bills:\n  first: { cost: subscription }\n\n"
            f"providers:\n{entries}\n  author: {{ start: \"author exec\", vendor: av, "
            "bill: first, roles: [author], reader: codex-json, env: [] }\n\n"
            f"roles:\n  author: [author]\n  reviewer: [{names}]\n"
        )
        home = self.tmp / f"home-{abs(hash(readers))}"
        (home / ".local" / "share" / "sd").mkdir(parents=True)
        (home / ".local" / "share" / "sd" / "providers.yaml").write_text(text, encoding="utf-8")
        return home

    def consenting_repo(self, count: int) -> pathlib.Path:
        """A repository consenting to every `pN` entry, so the only thing left
        to make an entry ineligible is its reader."""
        root = self.make_repo()
        self.local_block(root)
        line = ", ".join(f"p{i}@p{i}" for i in range(count))
        (root / "CLAUDE.local.md").write_text(
            "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\n"
            f"{sd_review.sd_lib.CONSENT_KEY}: {line}\n"
            "<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n",
            encoding="utf-8",
        )
        (root / "src.py").write_text("x = 1\n", encoding="utf-8")
        return root

    def explained(self, home: pathlib.Path, count: int) -> dict[str, Any]:
        return sd_review.review(
            self.consenting_repo(count),
            namespace(explain=True),
            FakeRunner(),
            {"HOME": str(home)},
            self.chatgpt_home(),
        )

    def test_an_unimplemented_reader_is_ineligible_and_says_why(self) -> None:
        rows = self.explained(self.registry_with("unimplemented-json", "codex-json"), 2)["chain"]
        self.assertFalse(rows[0]["eligible"])
        self.assertIn("unimplemented-json", rows[0]["reason"])
        self.assertTrue(rows[1]["eligible"], rows[1]["reason"])

    def test_it_does_not_consume_a_depth_slot(self) -> None:
        """The defect in one assertion: with an unrunnable entry ahead of a
        runnable one, the run still picks the one that can answer."""

        result = self.explained(self.registry_with("unimplemented-json", "codex-json"), 2)
        self.assertGreaterEqual(result["route"]["depth"], 1)
        self.assertEqual(result["providers"][:1], ["p1"], result["chain"])

    def test_every_reader_this_build_names_is_one_a_provider_can_run(self) -> None:
        """`READERS` is the allow-list the chain filters on. A name in it that
        `run_provider` does not implement would mark an entry eligible and then
        refuse it at the run, which is the hole this closes reopened."""

        self.assertEqual(sd_review.READERS, ("codex-json", "claude-json", "agy-json", "opencode-json"))


class AnEmptyChainThatWantedReviewersTests(ReviewFixture):
    """`skipped` exits zero. Only tier `skip` may claim it.

    Copilot found the second half of this. The missing-registry case was fixed
    by special-casing that one refusal, which left every other way of emptying
    the chain reporting `skipped` -- a repository with a registry and no
    `reviewers` line reviewed nothing and exited 0 saying so. The rule is not
    about registries: a run that wanted reviewers and got none is
    `unavailable`, and `depth == 0` is the only thing that earns `skipped`.
    """

    def review(self, root: pathlib.Path, **overrides: Any) -> dict[str, Any]:
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})
        return sd_review.review(
            root, namespace(**overrides), runner, self.environment(), self.chatgpt_home()
        )

    def test_no_consent_line_is_unavailable_not_skipped(self) -> None:
        root = self.make_repo()
        (root / "CLAUDE.local.md").unlink()
        (root / "src.py").write_text("x = 1\n", encoding="utf-8")
        result = self.review(root)
        self.assertEqual(result["providers"], [])
        self.assertEqual(result["status"], "unavailable")
        self.assertNotEqual(sd_review.STATUS_EXIT[result["status"]], sd_review.EXIT_OK)

    def test_an_ordinary_run_says_why_it_had_nobody(self) -> None:
        """Copilot found this. The reasons were computed either way and
        printed only under `--explain`, so a plain run said the one word
        "unavailable" and exited 5. The operator whose repository has no
        'reviewers' line is the last one who would think to re-run with a flag
        to learn that."""
        root = self.make_repo()
        (root / "CLAUDE.local.md").unlink()
        (root / "src.py").write_text("x = 1\n", encoding="utf-8")
        result = self.review(root)
        stream = io.StringIO()
        sd_review.render(result, stream)
        printed = stream.getvalue()
        self.assertIn("not enough reviewers were available:", printed)
        self.assertIn("reviewers", printed)
        self.assertIn("unavailable", printed)

    def test_a_docs_only_change_is_still_skipped_and_exits_zero(self) -> None:
        """The control. Tier `skip` means nothing needed reviewing, which is a
        different sentence from "nobody could review", and still exits 0."""

        root = self.make_repo()
        (root / "docs").mkdir()
        (root / "docs" / "note.md").write_text("hello\n", encoding="utf-8")
        result = self.review(root)
        self.assertEqual(result["route"]["depth"], 0)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(sd_review.STATUS_EXIT.get(result["status"], sd_review.EXIT_OK), sd_review.EXIT_OK)


class NoRegistryOnThisMachineTests(ReviewFixture):
    """A machine with no installed registry still answers.

    CI found this, not a test. The routing lane runs `sd-review --explain` on a
    bare runner, which has never run the installer, and the first version of the
    registry reader let the refusal reach `main` and exit 2: `sd-review: error:
    no provider registry at /home/runner/.local/share/sd/providers.yaml`. The
    lane exists to report the plan and asks nobody, so needing an install to
    print one was backwards.
    """

    def bare(self) -> dict[str, str]:
        """An environment whose HOME holds no registry."""
        empty = self.tmp / "no-registry-home"
        empty.mkdir()
        return {"HOME": str(empty)}

    def test_explain_answers_without_an_installed_registry(self) -> None:
        root = self.make_repo()
        runner = FakeRunner()
        result = sd_review.review(
            root, namespace(explain=True), runner, self.bare(), self.chatgpt_home()
        )
        self.assertEqual(result["status"], "explained")
        self.assertIn("no provider registry", result["registry_refusal"])
        self.assertEqual(result["providers"], [])
        self.assertEqual(runner.calls, [])

    def test_the_explain_render_prints_the_reason(self) -> None:
        root = self.make_repo()
        result = sd_review.review(
            root, namespace(explain=True), FakeRunner(), self.bare(), self.chatgpt_home()
        )
        stream = io.StringIO()
        sd_review.render(result, stream)
        self.assertIn("no provider registry", stream.getvalue())

    def test_a_real_review_is_unavailable_and_not_skipped(self) -> None:
        """`skipped` exits 0 and means "nothing needed reviewing". A machine
        that could not have reviewed anything must not borrow that word."""

        root = self.make_repo()
        (root / "src.py").write_text("x = 1\n", encoding="utf-8")
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})
        result = sd_review.review(
            root, namespace(), runner, self.bare(), self.chatgpt_home()
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(sd_review.STATUS_EXIT[result["status"]], sd_review.EXIT_GATE)
        self.assertNotEqual(sd_review.STATUS_EXIT[result["status"]], sd_review.EXIT_OK)

    def test_a_named_provider_is_refused_rather_than_answered_emptily(self) -> None:
        """With no registry, `pick` would say "no provider 'codex'", which reads
        as "that name is wrong" rather than "there is no registry here"."""

        root = self.make_repo()
        with self.assertRaises(sd_review.sd_registry.RegistryError) as caught:
            sd_review.review(
                root, namespace(provider="codex"), FakeRunner(), self.bare(), self.chatgpt_home()
            )
        self.assertIn("no provider registry", str(caught.exception))


class TheRoutingLaneRunsOnABareRunner(ReviewFixture):
    """The lane's own invocation, run the way the workflow runs it.

    `.github/actions/review-route` calls `bin/sd-review --scope pr --explain`
    and fails the job on a non-zero exit. This asserts that exit code against a
    HOME with no registry, which is what a GitHub runner is.
    """

    def test_explain_exits_zero_with_an_empty_home(self) -> None:
        empty = self.tmp / "runner-home"
        empty.mkdir()
        finished = subprocess.run(
            [sys.executable, str(SD_REVIEW), "--explain"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "HOME": str(empty)},
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("no provider registry", finished.stdout)


class TheWorkstationLaneIsNotPrintedWhereItCannotBeReached(ReviewFixture):
    """The provider-capability lines print where a person can act on them.

    Five lines of `--explain` -- providers, consent, codex auth, env scrub and
    the reviewer chain -- describe a workstation. A GitHub runner can satisfy
    none of them by construction: `CLAUDE.local.md` is gitignored globally and
    tracked in no repository of this fleet, and no provider registry is
    installed anywhere in it. Across eight consumers the block printed 283
    times in 30 days and named a provider on none of them.

    The lane stays. The rest of its report is the reason: the same runs
    produce a real routing tier, and six of the eight name an unattributed
    commit together with the command that fixes it.
    """

    def bare(self) -> dict[str, str]:
        """A HOME with no registry, which is what a GitHub runner is."""
        empty = self.tmp / "gated-home"
        empty.mkdir()
        return {"HOME": str(empty)}

    def explained(self, env: Mapping[str, str]) -> tuple[dict, str]:
        root = self.make_repo()
        result = sd_review.review(
            root, namespace(explain=True), FakeRunner(), dict(env), self.chatgpt_home()
        )
        stream = io.StringIO()
        sd_review.render(result, stream)
        return result, stream.getvalue()

    def test_a_runner_is_not_told_to_configure_what_it_cannot_hold(self) -> None:
        _, text = self.explained(self.bare())
        for absent in ("providers  ", "codex auth", "env scrub", "reviewer chain"):
            self.assertNotIn(absent, text)

    def test_the_suppression_says_so_rather_than_going_quiet(self) -> None:
        """A reader who sees five lines vanish must be able to tell the
        difference between "suppressed" and "the tool forgot"."""

        _, text = self.explained(self.bare())
        self.assertIn("no provider registry", text)
        self.assertIn("the provider lines are not reported", text)

    def test_the_suppression_prints_the_registry_refusal_itself(self) -> None:
        """A registry that is present but unreadable must not be called absent (sd:1000)."""

        home = self.tmp / "broken-home"
        (home / ".local" / "share" / "sd").mkdir(parents=True)
        (home / ".local" / "share" / "sd" / "providers.yaml").write_text("providers: [\n", encoding="utf-8")
        result, text = self.explained({"HOME": str(home)})
        self.assertTrue(result["registry_refusal"])
        self.assertNotIn("no provider registry", result["registry_refusal"])
        self.assertIn(result["registry_refusal"], text)
        self.assertNotIn("no provider registry", text)
        self.assertIn("the provider lines are not reported", text)

    def test_what_the_lane_is_kept_for_still_prints(self) -> None:
        """The gate is five lines wide, not the whole report."""

        _, text = self.explained(self.bare())
        for kept in ("scope", "subject", "route", "because"):
            self.assertIn(kept, text)
        self.assertIn("explain only, nothing ran", text)

    def test_a_workstation_keeps_every_line(self) -> None:
        """The fixture repository has a registry and a reviewer that resolves,
        so nothing is gated away there."""

        _, text = self.explained(self.environment())
        for present in ("providers", "codex auth", "env scrub", "reviewer chain"):
            self.assertIn(present, text)
        self.assertNotIn("the provider lines are not reported", text)

    def test_json_carries_the_fields_either_way(self) -> None:
        """The gate is a print-time condition. A caller reading `--explain
        --json` sees the same keys on a runner as on a workstation, which is
        what keeps this a display change rather than a loss of data."""

        result, _ = self.explained(self.bare())
        for key in ("providers", "registry", "registry_refusal", "consent_refusal", "chain"):
            self.assertIn(key, result)
        self.assertIn("no provider registry", result["registry_refusal"])

    def test_the_predicate_answers_on_each_leg_on_its_own(self) -> None:
        """Either half is enough: a registry that reads, or a reviewer that
        resolves. Asserted directly so a future edit to `render` cannot make
        the gate look right by making both legs unreachable together."""

        self.assertTrue(sd_review.workstation_lane_is_reachable(
            {"providers": ["codex"], "registry_refusal": "no provider registry at /x"}))
        self.assertTrue(sd_review.workstation_lane_is_reachable(
            {"providers": [], "registry_refusal": ""}))
        self.assertTrue(sd_review.workstation_lane_is_reachable(
            {"providers": [], "registry_refusal": "no registry", "chain": [{"eligible": True}]}))
        self.assertFalse(sd_review.workstation_lane_is_reachable(
            {"providers": [], "registry_refusal": "no registry", "chain": [{"eligible": False}]}))
        self.assertFalse(sd_review.workstation_lane_is_reachable(
            {"providers": [], "registry_refusal": "no registry", "chain": []}))


#: A registry whose first reviewer is a `url` entry and whose second is a
#: `start` one, so a run can be watched at both seams at once: what the client
#: sent, and what the runner spawned.
URL_REGISTRY = """
bills:
  free: {{ cost: subscription }}

providers:
  remote: {{ url: "{url}", model: a-model, vendor: somevendor, bill: free,
            roles: [reviewer], max_tokens: 16384, env: [REMOTE_KEY] }}
  second: {{ start: "second exec", vendor: secondvendor, bill: free,
            roles: [author, reviewer], reader: codex-json, env: [] }}

roles:
  author: [second]
  reviewer: [remote, second]
"""


class TheUrlEntryRunsTests(ReviewFixture):
    """Criterion 6's `url` client, end to end through `review`.

    Every test here injects both seams and asserts on both. The consent cases
    assert `client.sent == []`: a refusal that is only a message is a refusal
    nobody has shown to prevent the request.
    """

    reviewing_depth = 1  # the chain falls through one url entry at a time

    def home_with(self, url: str = "https://api.example.test/v1") -> pathlib.Path:
        self.homes = getattr(self, "homes", 0) + 1
        home = self.tmp / f"url-home-{self.homes}"
        (home / ".local" / "share" / "sd").mkdir(parents=True)
        (home / ".local" / "share" / "sd" / "providers.yaml").write_text(
            URL_REGISTRY.format(url=url), encoding="utf-8"
        )
        return home

    def repo_allowing(self, *allowed: str) -> pathlib.Path:
        self.made = getattr(self, "made", 0) + 1
        root = self.make_repo(f"repo-{self.made}")
        (root / "CLAUDE.local.md").write_text(
            "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\n"
            f"{sd_review.sd_lib.CONSENT_KEY}: {', '.join(allowed)}\n"
            "<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n",
            encoding="utf-8",
        )
        (root / "src.py").write_text("x = 1\n", encoding="utf-8")
        return root

    def run_review(
        self,
        client: FakeClient,
        *,
        url: str = "https://api.example.test/v1",
        allowed: Sequence[str] = ("remote@api.example.test", "second@second"),
        runner: FakeRunner | None = None,
        **overrides: Any,
    ) -> dict[str, Any]:
        return sd_review.review(
            self.repo_allowing(*allowed),
            namespace(**overrides),
            runner
            or FakeRunner(
                {
                    "sd-check": sd_review.Completed(0, "{}", ""),
                    "second": sd_review.Completed(0, '{"findings": []}', ""),
                }
            ),
            {"HOME": str(self.home_with(url)), "REMOTE_KEY": "secret"},
            self.chatgpt_home(),
            client,
        )

    def test_a_think_block_and_reasoning_content_yield_a_clean_finding_list(self) -> None:
        """Criterion 6, against a fixture response carrying both."""
        client = FakeClient(
            {
                "remote": chat_answer(
                    "<think>Reading the diff. Nothing here is wrong.</think>\n"
                    '{"findings": []}',
                    reasoning_content="Reading the diff. Nothing here is wrong.",
                )
            }
        )
        result = self.run_review(client)
        statuses = {row["backend"]: row["status"] for row in result["outcomes"]}
        self.assertEqual(statuses["remote"], sd_review.CLEAN)
        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["findings"], [])
        self.assertEqual(len(client.sent), 1)
        self.assertIn("Review", client.sent[0]["prompt"])

    def test_incomplete_url_diagnostics_survive_the_fallback_receipt(self) -> None:
        client = FakeClient(default=chat_answer("", reasoning_content="rate_limit private-marker"))
        result = self.run_review(client)
        first = result["outcomes"][0]
        self.assertEqual(first["status"], sd_review.UNAVAILABLE)
        self.assertEqual(first["diagnostic"]["category"], "reasoning_only")
        self.assertNotIn("private-marker", json.dumps(first))
        self.assertEqual(result["reviewed_by"], ["second"])

    def test_findings_come_back_through_the_same_reader(self) -> None:
        client = FakeClient(
            {
                "remote": chat_answer(
                    '<think>a</think>{"findings": [{"path": "src.py", "line": 1, '
                    '"severity": "high", "summary": "wrong", "family": "correctness"}]}'
                )
            }
        )
        result = self.run_review(client)
        self.assertEqual(result["status"], "blocking")
        self.assertEqual(result["findings"][0]["path"], "src.py")
        self.assertEqual(result["findings"][0]["backend"], "remote")

    def test_a_body_this_build_cannot_read_is_unavailable_and_never_clean(self) -> None:
        """The one property the whole unit rests on. `None` from the reader is
        a review that did not happen; `[]` is a review that found nothing. A
        body with no answer in it must never take the second road."""
        for body in ("<think>only reasoning</think>", "I could not comply.", ""):
            client = FakeClient({"remote": chat_answer(body)})
            result = self.run_review(client)
            statuses = {row["backend"]: row["status"] for row in result["outcomes"]}
            self.assertEqual(statuses["remote"], sd_review.UNAVAILABLE, body)
            self.assertEqual(len(client.sent), 1, body)

    def test_an_empty_findings_array_is_clean_and_the_two_are_not_confused(self) -> None:
        client = FakeClient({"remote": chat_answer('{"findings": []}')})
        statuses = {
            row["backend"]: row["status"] for row in self.run_review(client)["outcomes"]
        }
        self.assertEqual(statuses["remote"], sd_review.CLEAN)

    def test_a_429_falls_through_to_one_successful_review(self) -> None:
        client = FakeClient({"remote": (429, "", "HTTP 429 from remote: slow down", True)})
        result = self.run_review(client)
        statuses = {row["backend"]: row["status"] for row in result["outcomes"]}
        self.assertEqual(statuses["remote"], sd_review.RATE_LIMITED)
        self.assertEqual(statuses["second"], sd_review.CLEAN)
        self.assertEqual(result["status"], "clean")

    def test_a_connection_error_is_unavailable_and_the_chain_continues(self) -> None:
        # `launched=False` even though the text says 429: an answer that never
        # arrived is not a quota stop, and reading it as one would halt the
        # chain on a typo in somebody's error string.
        client = FakeClient({"remote": (1, "", "remote: refused after 429 tries", False)})
        result = self.run_review(client)
        statuses = {row["backend"]: row["status"] for row in result["outcomes"]}
        self.assertEqual(statuses["remote"], sd_review.UNAVAILABLE)
        self.assertEqual(statuses["second"], sd_review.CLEAN)
        self.assertEqual(result["status"], "clean")

    def test_a_host_that_moved_refuses_naming_both_and_sends_nothing(self) -> None:
        client = FakeClient()
        result = self.run_review(client, allowed=("remote@elsewhere.test", "second@second"))
        rows = {row["provider"]: row for row in result["chain"]}
        self.assertFalse(rows["remote"]["eligible"])
        self.assertIn("elsewhere.test", rows["remote"]["reason"])
        self.assertIn("api.example.test", rows["remote"]["reason"])
        self.assertEqual(client.sent, [], "no request leaves for a host that moved")
        self.assertNotIn("remote", result["providers"])

    def test_https_edited_to_http_refuses_in_print_and_sends_nothing(self) -> None:
        """`netloc` carries no scheme, so consent to the host is satisfied by
        both and this edit used to pass `refuse_allowance` in silence -- and
        this repository's diff would have left in the clear."""
        client = FakeClient()
        result = self.run_review(client, url="http://api.example.test/v1")
        rows = {row["provider"]: row for row in result["chain"]}
        self.assertFalse(rows["remote"]["eligible"])
        self.assertIn("in the clear", rows["remote"]["reason"])
        self.assertEqual(client.sent, [], "no request leaves in the clear")
        self.assertNotIn("remote", result["providers"])
        # Printed where every other passed-over entry is printed. A real run's
        # render names only the providers it used, which is where the refusal
        # for a host that moved goes too; `--explain` is the page that says why.
        printed = io.StringIO()
        sd_review.render(self.run_review(FakeClient(), url="http://api.example.test/v1",
                                         explain=True), printed)
        self.assertIn("in the clear", printed.getvalue())

    def test_a_loopback_entry_still_runs_over_http(self) -> None:
        client = FakeClient()
        result = self.run_review(
            client,
            url="http://localhost:52415/v1",
            allowed=("remote@localhost:52415", "second@second"),
        )
        self.assertEqual(result["registry_refusal"], "")
        self.assertEqual(len(client.sent), 1)

    def test_the_dry_run_names_the_endpoint_and_sends_nothing(self) -> None:
        client = FakeClient()
        result = self.run_review(client, dry_run=True)
        rows = {row["backend"]: row for row in result["planned_invocations"]}
        self.assertTrue(rows["remote"]["would_run"])
        self.assertEqual(
            rows["remote"]["endpoint"], "POST https://api.example.test/v1/chat/completions"
        )
        self.assertEqual(client.sent, [])


class ScopeProvidersOverASkipTier(unittest.TestCase):
    """A `skip` tier silences the tier, not the scope.

    Kept last in this file on purpose. Its line span is cited from
    `docs/work/2026-09-02-dashboard-ack-and-mutation-count/design.md`, and it
    drifted four times in one pull request because every new class landed above
    it. Last means only its own edits move it.

    This exists because its absence let a false concern stand. C-18 in
    `docs/work/2026-09-02-dashboard-ack-and-mutation-count/design.md` claimed
    `sd-review --scope planning` never asks a provider, reasoning correctly that
    `docs_skip` routes every work item to tier `skip` and then stopping one
    function short of the floor a scope puts under the tier's depth. Nothing in
    the repository disagreed, because nothing pinned the interaction. The
    concern survived a review round and an explanation to its owner before
    anyone ran it.

    The seam moved when the tier stopped naming providers: it used to be
    `plan_providers`, prepending names to the tier's chain, and it is now
    `review_depth`, raising the tier's count. The claim it refutes is the same
    one, so the class is kept and re-aimed rather than deleted with the
    function -- a deleted test is a claim that becomes true again quietly.
    """

    def setUp(self) -> None:
        self.policy = {
            "tier_order": ["skip", "cheap", "standard", "deep"],
            "default_tier": "standard",
            "categories": [
                {"name": "installer", "required": True, "paths": ["bin/sd_install.py"], "tier": "deep"},
            ],
            "docs_skip": ["docs/**", "*.md"],
        }
        self.skip = sd_review.sd_route.route(
            ["docs/work/2026-01-01-any-item/prd.md"],
            lines=1, draft=False, policy=self.policy)

    def test_a_work_item_really_does_route_to_skip(self) -> None:
        """The half of C-18 that was right, kept so the rest has a subject."""

        self.assertEqual(self.skip.tier, "skip")
        self.assertEqual(self.skip.depth, 0)

    def test_planning_scope_asks_a_provider_even_at_tier_skip(self) -> None:
        self.assertEqual(
            sd_review.review_depth(self.skip, challenge=False, scope="planning"), 2,
            "scope=planning earned no reviewer at tier skip. Either `review_depth`"
            " stopped honouring FLOOR_SCOPES, or `planning` left it -- and"
            " `sd-plan` gates `planning -> ready` on a lane that now asks nobody.")

    def test_challenge_asks_a_provider_even_at_tier_skip(self) -> None:
        """The same seam, reached by the other role that uses it."""

        self.assertEqual(
            sd_review.review_depth(self.skip, challenge=True, scope="worktree"), 2)  # both readers (sd:7519)

    def test_an_ordinary_scope_at_tier_skip_asks_nobody(self) -> None:
        """The control. Without it the two above pass on a floor that is never
        zero, which would prove nothing about the scope."""

        self.assertEqual(
            sd_review.review_depth(self.skip, challenge=False, scope="worktree"), 0)

    def test_the_scope_adds_to_the_tier_rather_than_replacing_it(self) -> None:
        """The floor is a minimum, not a setting. At tier `skip` those two
        readings agree, so the difference is only visible against a tier that
        already asks for more than one."""

        deep = sd_review.sd_route.route(
            ["bin/sd_install.py"], lines=1, draft=False, policy=self.policy)
        self.assertEqual(deep.tier, "deep")
        # Historical or explicit multi-review plans must retain their count.
        deep = deep._replace(depth=2)
        self.assertEqual(
            sd_review.review_depth(deep, challenge=False, scope="planning"), deep.depth,
            "the scope's floor reduced the deep tier's read")

    def test_every_floor_scope_is_a_scope_the_cli_accepts(self) -> None:
        """A typo in FLOOR_SCOPES is a floor that never fires, and every
        assertion above would still pass: they name their scope directly."""

        for scope in sd_review.FLOOR_SCOPES:
            self.assertIn(scope, sd_review.SCOPES, f"{scope!r} is not a scope")


class StandingReviewPolicyTests(ReviewFixture):
    def policy(self, value):
        path = self.registry_home / ".config" / sd_review.sd_lib.CONFIG_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"config": {"sd": {"external_reviews": value}}}))
        return str(path)

    def test_two_consumers_inherit_one_policy_and_explain_matches_actual_canned_review(self):
        policy_path = self.policy("configured")
        for name in ("one", "two"):
            root = self.make_repo(name)
            (root / "CLAUDE.local.md").unlink()
            (root / "src.py").write_text("x = 1\n")
            runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})
            args = namespace(provider="second", explain=True)
            explained = sd_review.review(root, args, runner, self.environment(), self.chatgpt_home())
            self.assertEqual(explained["authorization"]["source"], "machine-configured")
            self.assertEqual(explained["authorization"]["path"], policy_path)
            self.assertEqual(explained["providers"], ["second"])
            args.explain = False
            actual = sd_review.review(root, args, runner, self.environment(), self.chatgpt_home())
            self.assertEqual(actual["authorization"], explained["authorization"])
            self.assertEqual(actual["status"], "clean")
            self.assertEqual(actual["reviewed_by"], ["second"])

    def test_linked_worktree_reports_the_main_checkout_consent_path(self):
        self.policy("configured")
        root = self.make_repo()
        linked = self.tmp / "linked"
        subprocess.run(["git", "worktree", "add", "--detach", str(linked), "HEAD"], cwd=root, check=True, capture_output=True)
        (linked / "src.py").write_text("x = 1\n")
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})
        result = sd_review.review(linked, namespace(explain=True), runner, self.environment(), self.chatgpt_home())
        self.assertEqual(result["authorization"]["source"], "repository")
        self.assertEqual(result["authorization"]["path"], str(root / "CLAUDE.local.md"))

    def test_configured_policy_cannot_inherit_through_existing_unreadable_local_path(self):
        self.policy("configured")
        root = self.make_repo()
        local = root / "CLAUDE.local.md"
        local.unlink()
        local.mkdir()
        (root / "src.py").write_text("x = 1\n")
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})
        with self.assertRaises(sd_review.sd_lib.ConfigError):
            sd_review.review(root, namespace(), runner, self.environment(), self.chatgpt_home())
        self.assertEqual(runner.calls, [])

    def test_local_empty_and_machine_denial_send_nothing(self):
        root = self.make_repo()
        policy_path = self.policy("configured")
        self.local_block(root, "reviewers: \"\"")
        (root / "src.py").write_text("x = 1\n")
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})
        result = sd_review.review(root, namespace(), runner, self.environment(), self.chatgpt_home())
        self.assertEqual(result["providers"], [])
        self.assertEqual(result["authorization"]["source"], "repository")
        self.assertEqual(result["authorization"]["path"], str(root / "CLAUDE.local.md"))
        self.assertEqual(result["completed_reviews"], 0)
        self.local_block(root)
        result = sd_review.review(root, namespace(explain=True), runner, self.environment(), self.chatgpt_home())
        self.assertEqual(result["authorization"], {"source": "repository", "policy": "configured", "path": str(root / "CLAUDE.local.md")})
        self.policy("deny")
        result = sd_review.review(root, namespace(), runner, self.environment(), self.chatgpt_home())
        self.assertEqual(result["providers"], [])
        self.assertEqual(result["authorization"]["source"], "machine-deny")
        self.assertEqual(result["authorization"]["path"], policy_path)
        self.assertEqual(result["completed_reviews"], 0)


class TimingPlanTests(ReviewFixture):
    def planned(self, count=4, depth="deep", timeout=1800):
        root = self.make_repo()
        (root / "src.py").write_text("x=1\n")
        self.declare_policy(root, {"default_tier": depth})
        registry = self.registry_home / ".local/share/sd/providers.yaml"
        entries = [f"p{index}" for index in range(count)]
        registry.write_text("bills:\n  fixture: {cost: subscription}\nproviders:\n"
            + "".join(f"  {name}: {{start: '{name} exec', vendor: '{name}', bill: fixture, roles: [reviewer], reader: claude-json}}\n" for name in entries)
            + "roles:\n  author: []\n  reviewer: [" + ", ".join(entries) + "]\n")
        (root / "CLAUDE.local.md").write_text("<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\nreviewers: "
            + ", ".join(f"{name}@{name}" for name in entries) + "\n<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n")
        runner = FakeRunner()
        args = namespace(explain=True, timeout=timeout)
        report = sd_review.review(root, args, runner, self.environment(), self.chatgpt_home())
        self.assertEqual(runner.calls, [])
        return root, args, report

    def test_all_fallbacks_receive_an_allowance_without_changing_depth(self):
        root, args, planned = self.planned()
        self.assertEqual(planned["requested_reviews"], 2)  # sd:7519
        self.assertEqual(planned["fallback_candidates"], ["p2", "p3"])
        self.assertEqual(planned["timing"]["execution_seconds"], 12600 + sd_review.sd_lib.GATE_SLOT_SECONDS)  # sd:2611
        self.assertEqual([row["name"] for row in planned["timing"]["candidates"]], ["p0", "p1", "p2", "p3"])
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", ""), "p0": sd_review.Completed(127, "", "missing", False)},
                            default=sd_review.Completed(0, json.dumps({"type": "result", "subtype": "success", "structured_output": {"findings": []}}), ""))
        args.explain = False
        args.expected_timing = sd_review.hashlib.sha256(json.dumps(planned["timing"], sort_keys=True).encode()).hexdigest()
        actual = sd_review.review(root, args, runner, self.environment(), self.chatgpt_home())
        self.assertEqual(len(runner.calls), 4)  # p0 fails, p1 and p2 answer, then the gate
        self.assertEqual(actual["timing"], planned["timing"])
        self.assertEqual(actual["completed_reviews"], 2)
        self.assertEqual(actual["requested_reviews"], 2)
        self.assertEqual(actual["reviewed_by"], ["p1", "p2"])

    def test_timeout_change_refuses_before_check_or_provider(self):
        root, args, report = self.planned(count=2, depth="standard", timeout=90)
        args.explain = False
        args.expected_timing = sd_review.hashlib.sha256(json.dumps(report["timing"], sort_keys=True).encode()).hexdigest()
        args.timeout = 91
        runner = FakeRunner()
        with self.assertRaisesRegex(sd_review.Refusal, "timing inputs changed"):
            sd_review.review(root, args, runner, self.environment(), self.chatgpt_home())
        self.assertEqual(runner.calls, [])

    def test_candidate_identity_drift_refuses_even_when_count_is_unchanged(self):
        root, args, report = self.planned(count=2, depth="standard")
        args.explain = False
        args.expected_timing = sd_review.hashlib.sha256(json.dumps(report["timing"], sort_keys=True).encode()).hexdigest()
        registry = self.registry_home / ".local/share/sd/providers.yaml"
        registry.write_text(registry.read_text().replace("p0", "replacement"))
        local = root / "CLAUDE.local.md"
        local.write_text(local.read_text().replace("p0", "replacement"))
        runner = FakeRunner()
        with self.assertRaisesRegex(sd_review.Refusal, "timing inputs changed"):
            sd_review.review(root, args, runner, self.environment(), self.chatgpt_home())
        self.assertEqual(runner.calls, [])

    def test_only_timing_inputs_are_bound(self):
        root, args, report = self.planned(count=2, depth="cheap")
        args.explain = False
        args.expected_timing = sd_review.hashlib.sha256(json.dumps(report["timing"], sort_keys=True).encode()).hexdigest()
        args.challenge = True
        runner = FakeRunner({"sd-check": sd_review.Completed(1, "{}", "fixture gate failure")},
                            default=sd_review.Completed(0, json.dumps({"type": "result", "subtype": "success", "structured_output": {"findings": []}}), ""))
        actual = sd_review.review(root, args, runner, self.environment(), self.chatgpt_home())
        self.assertEqual(actual["timing"], report["timing"])
        self.assertEqual(actual["status"], "gate_failed")
        # sd:2605: the reviewers ran first, and the gate once, last.
        gate = [index for index, call in enumerate(runner.calls) if "sd-check" in " ".join(call["argv"])]
        self.assertEqual(gate, [len(runner.calls) - 1])


class CopilotPolicyTests(ReviewFixture):
    """Where `remote_reviews.copilot.automatic` comes from (sd:1328).

    The machine's `sd.copilot_review` is the default for every repository;
    unset it reads `deep`, so a repository with no review declaration
    gets Copilot on deep-tier changes and on nothing else. The repository's
    own `copilot_review.automatic_deep` wins over `deep` and `always` when the
    declaration names it, and one that does not name it inherits. A machine
    `never` wins over the declaration (sd:1444). `--explain` says which one answered.
    """

    def machine(self, value: str | None) -> None:
        """`external_reviews` in the machine's file; `sd.copilot_review` a row, as a fleet key is (sd:5109)."""
        path = self.registry_home / ".config" / sd_review.sd_lib.CONFIG_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"config": {"sd": {"external_reviews": "configured"}}}))
        sd_db = sd_review.sd_lib.import_sd_db().module
        if not sd_db.default_path(self.registry_home).exists():
            sd_db.initialise(home=self.registry_home)
        sd_review.sd_lib.set_database_setting("sd.copilot_review", value, {"HOME": str(self.registry_home)})

    def repo(self, name: str, *, deep: bool, policy: dict[str, Any] | None = None) -> pathlib.Path:
        """A repository whose one change routes deep (a sensitive path under
        the built-in policy) or standard, with or without a policy file."""
        root = self.make_repo(name)
        if policy is not None:
            self.declare_policy(root, policy)
        if deep:
            (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
            (root / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        else:
            (root / "src.py").write_text("x = 1\n")
        return root

    def copilot(self, root: pathlib.Path, **overrides: Any) -> dict[str, Any]:
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", ""),
                             "codex": sd_review.Completed(0, '{"findings": []}', "")})
        result = sd_review.review(root, namespace(**overrides), runner, self.environment(), self.chatgpt_home())
        return result["remote_reviews"]["copilot"]

    def test_unset_machine_and_no_file_requests_on_deep_and_not_on_standard(self) -> None:
        self.machine(None)
        deep = self.copilot(self.repo("deep", deep=True))
        self.assertEqual(deep, {"automatic": True, "tier": "deep",
                                "policy": "deep", "source": "machine default", "repository": None})
        standard = self.copilot(self.repo("standard", deep=False))
        self.assertEqual(standard, {"automatic": False, "tier": "standard",
                                    "policy": "deep", "source": "machine default", "repository": None})

    def test_machine_never_requests_nothing_even_on_deep(self) -> None:
        self.machine("never")
        deep = self.copilot(self.repo("deep", deep=True))
        self.assertEqual(deep, {"automatic": False, "tier": "deep",
                                "policy": "never", "source": "machine config", "repository": None})

    def test_machine_always_requests_on_standard(self) -> None:
        self.machine("always")
        standard = self.copilot(self.repo("standard", deep=False))
        self.assertEqual(standard, {"automatic": True, "tier": "standard",
                                    "policy": "always", "source": "machine config", "repository": None})

    def test_machine_always_still_skips_a_change_nobody_local_reviews(self) -> None:
        self.machine("always")
        root = self.make_repo("docs-only")
        (root / "README.md").write_text("seed\nmore\n")
        skipped = self.copilot(root)
        self.assertEqual(skipped["tier"], "skip")
        self.assertEqual(skipped["policy"], "always")
        self.assertFalse(skipped["automatic"])

    def test_a_repository_file_that_names_the_key_overrides_deep_and_always(self) -> None:
        self.machine("always")
        off = self.copilot(self.repo("off", deep=True, policy={"copilot_review": {"automatic_deep": False}}))
        self.assertEqual(off, {"automatic": False, "tier": "deep",
                               "policy": "never", "source": "repository", "repository": False})
        self.machine("deep")
        off = self.copilot(self.repo("off-deep", deep=True, policy={"copilot_review": {"automatic_deep": False}}))
        self.assertEqual(off, {"automatic": False, "tier": "deep",
                               "policy": "never", "source": "repository", "repository": False})
        self.machine("always")
        on = self.copilot(self.repo("on", deep=True, policy={"copilot_review": {"automatic_deep": True}}))
        self.assertEqual(on, {"automatic": True, "tier": "deep",
                              "policy": "deep", "source": "repository", "repository": True})

    def test_machine_never_beats_a_repository_opt_in(self) -> None:
        """sd:1444: the operator's `never` is the cost lever, and a repository's
        `automatic_deep: true` does not buy a paid review past it. The report
        still records what the file said."""
        self.machine("never")
        on = self.copilot(self.repo("on", deep=True, policy={"copilot_review": {"automatic_deep": True}}))
        self.assertEqual(on, {"automatic": False, "tier": "deep",
                              "policy": "never", "source": "machine config", "repository": True})
        root = self.repo("on-explained", deep=True, policy={"copilot_review": {"automatic_deep": True}})
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})
        explained = sd_review.review(root, namespace(explain=True), runner, self.environment(), self.chatgpt_home())
        out = io.StringIO()
        sd_review.render(explained, out)
        self.assertIn("copilot     never (machine config); tier deep; not requested by sd-review, sd-ship will not request it", out.getvalue())

    def test_a_repository_file_without_the_key_inherits_the_machine(self) -> None:
        self.machine("never")
        inherited = self.copilot(self.repo("inherit", deep=True, policy={"severity_floor": "high"}))
        self.assertEqual(inherited["source"], "machine config")
        self.assertEqual(inherited["policy"], "never")
        self.assertFalse(inherited["automatic"])
        policy, _ = sd_review.load_policy(self.tmp / "inherit")
        self.assertIsNone(policy["copilot_review"]["automatic_deep"])

    def test_explain_names_the_policy_and_its_source(self) -> None:
        self.machine("always")
        root = self.repo("explained", deep=False)
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})
        explained = sd_review.review(root, namespace(explain=True), runner, self.environment(), self.chatgpt_home())
        self.assertEqual(explained["status"], "explained")
        self.assertEqual(explained["remote_reviews"]["copilot"], {
            "automatic": True, "tier": "standard", "policy": "always", "source": "machine config", "repository": None})
        self.assert_no_session_started(runner)
        out = io.StringIO()
        sd_review.render(explained, out)
        self.assertIn("copilot     always (machine config)", out.getvalue())
        # sd:1568: sd-review requests nothing, so `automatic` must not print
        # as "requested"; before any pull request exists that claimed a
        # review nobody asked for.
        self.assertIn("tier standard; not requested by sd-review, sd-ship will request it"
                      " on the pull request", out.getvalue())
        self.assertNotIn(", requested", out.getvalue())

    def test_a_malformed_machine_value_stops_the_run(self) -> None:
        self.machine("sometimes")
        with self.assertRaises(sd_review.sd_lib.ConfigError):
            self.copilot(self.repo("bad", deep=True))


if __name__ == "__main__":
    unittest.main()
