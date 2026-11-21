"""The `opencode-json` reader: how it is invoked, confined, read back, and
kept independent (sd:1329).

`opencode` is one client in front of every vendor it holds a credential for:
`-m provider/model` picks the model per run, and this machine's copy lists
OpenAI, Anthropic, Moonshot, MiniMax, Baseten and Google among them. So an
entry's `vendor` is a claim about the model rather than about the program,
exactly as for `agy`, and `sd_registry.MULTIVENDOR_READERS` requires the
entry to pin one. The stream fixtures below are captured from `opencode
1.18.30` (`opencode run --format json`, 2026-09-22) unless labelled 2.0.20;
2.0.20 prints the same `text` part (measured 2026-10-02). Each is labelled
with what produced it.
"""

from __future__ import annotations

import fnmatch
import json
import pathlib
import sys
import tempfile
import unittest
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_opencode  # noqa: E402
import sd_registry  # noqa: E402

from tests.test_sd_review import sd_review  # noqa: E402

SHIPPED = sd_registry.shipped_path(REPO_ROOT)

#: Verbatim from a real 1.18.30 run: `opencode run --format json --pure --agent
#: sd-review -m openai/gpt-5.5 'Reply with exactly ... {"findings": []}'`.
#: One event per line; the answer is the whole `text` part, not deltas.
CLEAN_STREAM = "\n".join((
    '{"type":"step_start","timestamp":1790102253601,"sessionID":"ses_f3597732effeNUZHQD02qHERYn",'
    '"part":{"id":"prt_0ca68b01f001z32fsoo7DoAPdD","messageID":"msg_0ca688e86001nRr7dUn5yXBXkZ",'
    '"sessionID":"ses_f3597732effeNUZHQD02qHERYn","snapshot":"3942cedfea53e999bd13d0a27a905957c5e62b1a","type":"step-start"}}',
    '{"type":"text","timestamp":1790102254459,"sessionID":"ses_f3597732effeNUZHQD02qHERYn",'
    '"part":{"id":"prt_0ca68b02000188iqz8NpAYEq3y","messageID":"msg_0ca688e86001nRr7dUn5yXBXkZ",'
    '"sessionID":"ses_f3597732effeNUZHQD02qHERYn","type":"text","text":"{\\"findings\\": []}",'
    '"time":{"start":1790102253600,"end":1790102254456},'
    '"metadata":{"openai":{"itemId":"msg_06a8b808bb42eb03016ab2caed8b9c87d0aae43d3a93736fb0","phase":"final_answer"}}}}',
    '{"type":"step_finish","timestamp":1790102254702,"sessionID":"ses_f3597732effeNUZHQD02qHERYn",'
    '"part":{"id":"prt_0ca68b46c001Moxxhp6UhiqvTO","reason":"stop","snapshot":"3942cedfea53e999bd13d0a27a905957c5e62b1a",'
    '"messageID":"msg_0ca688e86001nRr7dUn5yXBXkZ","sessionID":"ses_f3597732effeNUZHQD02qHERYn","type":"step-finish",'
    '"tokens":{"total":122176,"input":122166,"output":10,"reasoning":0,"cache":{"write":0,"read":0}},"cost":0}}',
))

#: Verbatim shape of the `error` event a run with an unsupported model
#: printed (`-m openai/gpt-5.4-mini` under a ChatGPT credential); the
#: response headers are cut, nothing else is.
ERROR_STREAM = (
    '{"type":"error","timestamp":1790102120244,"sessionID":"ses_f35997149ffeoF77r7ycSv9zZb",'
    '"error":{"name":"APIError","data":{"message":"Bad Request: {\\"detail\\":\\"The \'gpt-5.4-mini\' model '
    'is not supported when using Codex with a ChatGPT account.\\"}","statusCode":400,"isRetryable":false,'
    '"metadata":{"url":"https://api.openai.com/v1/responses"}}}}'
)

#: Verbatim from 2.0.20 (`opencode run --standalone --format json --agent
#: sd-review` with the inline config unset): the cause is `error.message`.
ERROR_STREAM_V2 = (
    '{"type":"error","timestamp":1790980295125,"sessionID":"ses_f0141770effeWsopVwITFpYgv8",'
    '"error":{"type":"unknown","message":"Agent not found: \\"sd-review\\""}}'
)


def finding_stream(text: str) -> str:
    """The clean stream with the model's answer replaced, fence and all."""
    return CLEAN_STREAM.replace('{\\"findings\\": []}', json.dumps(text)[1:-1])


FINDING = {"findings": [{"path": "bin/x.py", "line": 3, "severity": "high", "instances": ["src.py:1"],
                         "summary": "unchecked input", "family": "correctness"}]}


class TheReaderIsRunnable(unittest.TestCase):
    """A name in `READERS` that nothing runs is eligible and then refused."""

    def test_the_reader_is_listed(self) -> None:
        self.assertIn("opencode-json", sd_review.READERS)

    def test_the_reader_carries_its_material_in_the_prompt(self) -> None:
        """`opencode` reviews what it is handed, attached as a file."""
        self.assertIn("opencode-json", sd_review.MATERIAL_READERS)

    def test_the_reader_is_multivendor(self) -> None:
        """One program, every vendor: the entry has to pin a model."""
        self.assertIn("opencode-json", sd_registry.MULTIVENDOR_READERS)

    def test_provider_argv_routes_to_the_opencode_builder(self) -> None:
        provider = sd_registry.Provider(name="opencode", vendor="openai", bill="openai",
                                        start="opencode run", model="openai/gpt-5.5",
                                        reader="opencode-json")
        with tempfile.TemporaryDirectory() as raw:
            workdir = pathlib.Path(raw)
            argv = sd_review.provider_argv(provider, REPO_ROOT, workdir)
            self.assertEqual(argv, sd_opencode.opencode_argv(workdir, "opencode run", "openai/gpt-5.5"))
            self.assertEqual(argv[:2], ["opencode", "run"])


class TheArgvIsConfined(unittest.TestCase):
    """What the spawned session may reach, pinned flag by flag."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.workdir = pathlib.Path(self.dir.name)
        self.argv = sd_opencode.opencode_argv(self.workdir, "opencode run", "openai/gpt-5.5")

    def test_it_asks_for_json_events_from_a_private_server(self) -> None:
        """2.x's shared service never sees the inline config (measured)."""
        self.assertEqual(self.argv[self.argv.index("--format") + 1], "json")
        self.assertIn("--standalone", self.argv)
        self.assertNotIn("--server", self.argv)

    def test_it_runs_the_private_agent(self) -> None:
        self.assertEqual(self.argv[self.argv.index("--agent") + 1], sd_opencode.AGENT)

    def test_it_never_auto_approves(self) -> None:
        self.assertNotIn("--auto", self.argv)

    def test_the_subject_is_attached_after_the_message(self) -> None:
        """`--file` is an array flag: placed before the message it swallows
        the message as a file name (measured: "File not found: Three tasks")."""
        attached = self.argv.index("--file")
        self.assertEqual(self.argv[attached + 1], str(self.workdir / "review-subject.md"))
        self.assertEqual(attached, len(self.argv) - 2)
        self.assertIn("review-subject.md", self.argv[attached - 1])

    def test_the_model_comes_from_the_entry(self) -> None:
        self.assertEqual(self.argv[self.argv.index("--model") + 1], "openai/gpt-5.5")
        self.assertNotIn("--model", sd_opencode.opencode_argv(self.workdir, "opencode run", None))


def _decide(permission: list[dict[str, str]], name: str, pattern: str = "*") -> str:
    """opencode's evaluation: the last rule whose action and resource both
    match wins, and no match is `ask`. An MCP server's tool asks as its own
    name; 1.x's resource readers asked `read` with `mcp:<server>:<uri>`, and
    2.x's ask as `opencode_*_mcp_resource(s)`."""
    hits = [rule["effect"] for rule in permission
            if fnmatch.fnmatchcase(name, rule["action"]) and fnmatch.fnmatchcase(pattern, rule["resource"])]
    return hits[-1] if hits else "ask"


class TheEnvironmentCarriesTheAgent(unittest.TestCase):
    """The private agent travels as inline config, and its map is
    default-deny: `*` first, a read-only allow-list after it, decided the way
    opencode decides. Nothing it refuses is refused by name."""

    CONFIG_DIR = pathlib.Path("/run/sd-review-x/opencode-config")

    def permission(self) -> list[dict[str, str]]:
        child = sd_opencode.opencode_environment({"PATH": "/usr/bin", "HOME": "/h"}, self.CONFIG_DIR)
        self.assertEqual(child["PATH"], "/usr/bin")
        config = json.loads(child["OPENCODE_CONFIG_CONTENT"])
        return list(config["agents"][sd_opencode.AGENT]["permissions"])

    def test_the_default_is_deny_and_it_comes_first(self) -> None:
        permission = self.permission()
        self.assertEqual(permission[0], {"action": "*", "resource": "*", "effect": "deny"},
                         "a later `*` would outrank every allowance")
        self.assertEqual({rule["action"] for rule in permission[1:]}, {"read", "glob", "grep", "list"})
        self.assertNotIn("*", [rule["action"] for rule in permission[1:]])

    def test_the_built_in_tools_are_denied_without_being_named(self) -> None:
        permission = self.permission()
        for tool in ("edit", "bash", "shell", "write", "patch", "webfetch", "websearch", "task", "subagent",
                     "skill", "lsp", "question", "external_directory", "opencode_read_mcp_resource",
                     "opencode_list_mcp_resources"):
            self.assertEqual(_decide(permission, tool), "deny", tool)
            self.assertNotIn(tool, [rule["action"] for rule in permission], f"{tool} falls to `*`")
        for tool in ("read", "glob", "grep", "list"):
            self.assertEqual(_decide(permission, tool, "/repo/README.md"), "allow", tool)

    def test_a_tool_outside_the_allow_list_is_refused_by_default(self) -> None:
        permission = self.permission()
        tool = "a_tool_this_module_never_heard_of"
        self.assertNotIn(tool, json.dumps(permission))
        self.assertEqual(_decide(permission, tool), "deny")

    def test_a_configured_mutating_mcp_server_is_not_reachable(self) -> None:
        """The operator's global config holds a server that writes, and the
        reader's config names neither the server nor its tools: the tools
        fall to `*`, the resources to `read`'s `mcp:*`, and a file read to
        `read`'s `*`."""
        operator = {"mcp": {"mutator": {"type": "local", "command": ["mutator-mcp"], "enabled": True}}}
        child = sd_opencode.opencode_environment({"HOME": "/h"}, self.CONFIG_DIR)
        config = json.loads(child["OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn("mcp", config, "the reader disables nothing by name")
        for server in operator["mcp"]:
            self.assertNotIn(server, json.dumps(config))
            permission = config["agents"][sd_opencode.AGENT]["permissions"]
            for tool in (f"{server}_write", f"{server}_delete", f"{server}_get"):
                self.assertEqual(_decide(permission, tool), "deny", tool)
            self.assertEqual(_decide(permission, "read", f"mcp:{server}:*"), "deny")
            self.assertEqual(_decide(permission, "read", f"mcp:{server}:db://rows/1"), "deny")
            self.assertEqual(_decide(permission, "read", "/repo/README.md"), "allow")

    def test_the_parent_is_not_mutated(self) -> None:
        parent = {"PATH": "/usr/bin", "PWD": "/repo"}
        sd_opencode.opencode_environment(parent, self.CONFIG_DIR)
        self.assertEqual(parent, {"PATH": "/usr/bin", "PWD": "/repo"})

    def test_only_the_inline_config_is_loaded(self) -> None:
        """On 2.0.20 an inherited `PWD` naming the checkout started its MCP
        server from a neutral cwd, and the operator's config dir loads every
        plugin in it. The run's own empty dir replaces that dir, a named
        config file goes, and project config is off under both names."""
        child = sd_opencode.opencode_environment(
            {"PWD": "/repo", "OPENCODE_CONFIG": "/repo/hostile.json", "OPENCODE_CONFIG_DIR": "/home/op/.config/opencode",
             "OPENCODE_CONFIG_PROJECT_DISABLE": "0", "HOME": "/h"}, self.CONFIG_DIR)
        self.assertNotIn("PWD", child)
        self.assertNotIn("OPENCODE_CONFIG", child)
        self.assertEqual(child["OPENCODE_CONFIG_DIR"], str(self.CONFIG_DIR))
        self.assertEqual(child["OPENCODE_CONFIG_PROJECT_DISABLE"], "1")
        self.assertEqual(child["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")


class TheStreamIsReadBack(unittest.TestCase):
    def answer(self, stdout: str, exit_code: int = 0):
        result = sd_review.Completed(exit_code, stdout, "")
        return sd_opencode.opencode_answer(result, parse=sd_review.parse_findings,
                                  oversized=sd_review.oversized_findings,
                                  limit=sd_review.MAX_OUTPUT_BYTES)

    def test_a_clean_answer_is_clean(self) -> None:
        result, parsed = self.answer(CLEAN_STREAM)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.findings, ())
        self.assertEqual(parsed.error, "")

    def test_findings_come_out_in_the_codex_shape(self) -> None:
        _result, parsed = self.answer(finding_stream(json.dumps(FINDING)))
        self.assertEqual(parsed.error, "")
        self.assertEqual(len(parsed.findings), 1)
        self.assertEqual(parsed.findings[0]["path"], "bin/x.py")
        self.assertEqual(parsed.findings[0]["line"], 3)
        self.assertEqual(parsed.findings[0]["severity"], "high")

    def test_a_fenced_answer_is_still_read(self) -> None:
        fenced = "```json\n" + json.dumps(FINDING) + "\n```"
        _result, parsed = self.answer(finding_stream(fenced))
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed.findings), 1)

    def test_the_last_text_event_is_the_answer(self) -> None:
        """A run that thinks aloud, calls a tool and then answers prints
        more than one `text` part; the final one carries the findings."""
        earlier = CLEAN_STREAM.replace('{\\"findings\\": []}', "Looking at the diff first.")
        _result, parsed = self.answer(earlier + "\n" + finding_stream(json.dumps(FINDING)))
        self.assertEqual(len(parsed.findings), 1)

    def test_an_error_event_refuses_and_names_the_cause(self) -> None:
        for stream, cause in ((ERROR_STREAM, "gpt-5.4-mini"), (ERROR_STREAM_V2, "Agent not found")):
            result, parsed = self.answer(stream)
            self.assertIsNone(parsed)
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn(cause, result.stderr)

    def test_no_text_event_is_not_a_clean_review(self) -> None:
        only_steps = "\n".join(line for line in CLEAN_STREAM.splitlines() if '"type":"text"' not in line)
        result, parsed = self.answer(only_steps)
        self.assertIsNone(parsed)
        self.assertNotEqual(result.exit_code, 0)

    def test_prose_is_not_a_clean_review(self) -> None:
        _result, parsed = self.answer(finding_stream("Looks fine to me."))
        self.assertIsNone(parsed)

    def test_an_oversized_stream_is_a_blocker(self) -> None:
        result, parsed = self.answer("x" * (sd_review.MAX_OUTPUT_BYTES + 1))
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(parsed.findings[0]["severity"], "high")


class TheShippedEntry(unittest.TestCase):
    """The tracked registry: the entry parses, sits third, and pins a model
    whose vendor it names honestly."""

    def setUp(self) -> None:
        self.registry = sd_registry.read_file(SHIPPED)

    def test_the_entry_parses_with_its_reader(self) -> None:
        entry = self.registry.providers["opencode"]
        self.assertEqual(entry.reader, "opencode-json")
        self.assertEqual(entry.start, "opencode run")
        self.assertIn("reviewer", entry.roles)
        self.assertNotIn("author", entry.roles)

    def test_the_vendor_is_the_pinned_model_s(self) -> None:
        entry = self.registry.providers["opencode"]
        self.assertIsNotNone(entry.model)
        self.assertEqual(sd_registry.model_vendor(entry.model), entry.vendor)
        self.assertEqual(entry.bill, entry.vendor)
        self.assertFalse(self.registry.bills[entry.bill].capped)

    def test_the_reviewer_order_is_codex_claude_opencode(self) -> None:
        self.assertEqual([p.name for p in self.registry.order("reviewer")],
                         ["codex", "claude", "opencode"])

    def test_the_entry_without_a_model_is_refused(self) -> None:
        text = SHIPPED.read_text(encoding="utf-8")
        entry = next(line for line in text.splitlines() if line.startswith("  opencode:"))
        self.assertIn("model:", entry)
        without = text.replace(entry, entry.replace(
            entry[entry.index("model:"):entry.index(",", entry.index("model:")) + 2], ""))
        with self.assertRaises(sd_registry.RegistryError) as caught:
            sd_registry.parse(without, SHIPPED)
        self.assertIn("pins no model", str(caught.exception))


class TheChainReachesOpencode(unittest.TestCase):
    """A claude-authored branch with codex out of the running falls through
    to opencode rather than refusing for want of a reviewer."""

    def setUp(self) -> None:
        self.registry = sd_registry.read_file(SHIPPED)
        self.readers = sd_review.READERS

    def consent(self, *names: str) -> dict:
        return sd_registry.parse_consent(" ".join(
            str(sd_registry.recipient(self.registry.providers[name])) for name in names))

    def eligible(self, **kwargs) -> list[str]:
        return [c.provider.name for c in sd_registry.reviewer_chain(
            self.registry, readers=self.readers, **kwargs) if c.eligible]

    def test_codex_disabled_falls_through_to_the_next_entries(self) -> None:
        from dataclasses import replace
        self.registry.providers["codex"] = replace(self.registry.providers["codex"], enabled=False,
                                                   reason="rate limited this hour")
        names = self.eligible(consent=self.consent("codex", "claude", "opencode"))
        self.assertEqual(names, ["claude", "opencode"])


class TheLaunchIsIsolated(unittest.TestCase):
    """opencode runs from a neutral directory, never the reviewed checkout, so
    the checkout's own `opencode.json` cannot deep-merge into the confined
    config. The escape this closes: launched inside the checkout (the shipped
    default), a hostile `opencode.json` re-granting `bash` survives under the
    map's `*: deny`, because last-match evaluation keeps its specific
    allowances (sd:1375)."""

    def opencode_provider(self) -> Any:
        return sd_registry.Provider(name="opencode", vendor="openai", bill="openai",
                                    start="opencode run", reader="opencode-json",
                                    model="openai/gpt-5.5")

    def test_opencode_launches_from_a_neutral_dir_not_the_checkout(self) -> None:
        from tests.test_sd_review import FakeRunner
        runner = FakeRunner(default=sd_review.Completed(0, finding_stream('{"findings": []}'), ""))
        with tempfile.TemporaryDirectory() as raw_root:
            root = pathlib.Path(raw_root)
            outcome = sd_review.run_provider(
                self.opencode_provider(), root,
                sd_review.Subject("worktree", "HEAD", "worktree", (), 0, ""),
                "prompt", runner, {}, 60)
        # The confinement probe, then the review: both from the neutral dir.
        self.assertEqual(len(runner.calls), 2, outcome.detail)
        for call in runner.calls:
            launched = call["cwd"]
            self.assertNotEqual(launched.resolve(), root.resolve(),
                                "launched inside the reviewed checkout -- the config-merge escape")
            self.assertTrue(launched.name.startswith("sd-review-"),
                            f"opencode's launch dir {launched} is not the neutral workdir")

    def test_a_launch_dir_inside_the_checkout_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = pathlib.Path(raw_root)
            inside = root / "sub"
            inside.mkdir()
            with self.assertRaises(sd_opencode.IsolationError):
                sd_opencode.assert_isolated_launch(inside, root)
            with self.assertRaises(sd_opencode.IsolationError):
                sd_opencode.assert_isolated_launch(root, root)

    def test_a_config_bearing_ancestor_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = pathlib.Path(raw)
            (base / "opencode.json").write_text("{}")
            launch = base / "a" / "b"
            launch.mkdir(parents=True)
            with self.assertRaises(sd_opencode.IsolationError):
                sd_opencode.assert_isolated_launch(launch, base / "elsewhere")

    def test_a_neutral_dir_outside_the_checkout_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_launch, tempfile.TemporaryDirectory() as raw_root:
            sd_opencode.assert_isolated_launch(pathlib.Path(raw_launch), pathlib.Path(raw_root))


class TheResolvedConfinementIsChecked(unittest.TestCase):
    """The run is refused unless opencode itself resolves the sd-review agent
    to this map and loads no plugin of its own (sd:1375, sd:2445). The neutral
    launch dir decides where opencode looks; this reads back what it resolved,
    so a widening that arrives by any other route refuses the run instead of
    starting it. The fakes answer in the probe's report shape, built from what
    `agent.list` and `plugin.list` returned on 2.0.20."""

    SHELL = {"action": "shell", "resource": "*", "effect": "allow"}

    def provider(self) -> Any:
        return sd_registry.Provider(name="opencode", vendor="openai", bill="openai",
                                    start="opencode run", reader="opencode-json",
                                    model="openai/gpt-5.5")

    def run_with(self, probe: Any) -> tuple[Any, Any]:
        from tests.test_sd_review import FakeRunner

        def answer(argv, env, cwd, timeout):
            if sd_opencode.probe_program(list(argv)):
                return probe(env) if callable(probe) else probe
            return sd_review.Completed(0, finding_stream('{"findings": []}'), "")
        runner = FakeRunner(answers={"opencode": answer})
        with tempfile.TemporaryDirectory() as raw_root:
            outcome = sd_review.run_provider(
                self.provider(), pathlib.Path(raw_root),
                sd_review.Subject("worktree", "HEAD", "worktree", (), 0, ""),
                "prompt", runner, {"HOME": "/home/reviewer", "PWD": raw_root}, 60)
        return outcome, runner

    def test_a_widened_resolution_refuses_before_the_review_starts(self) -> None:
        from tests.test_sd_review import resolved_agent
        outcome, runner = self.run_with(sd_review.Completed(0, resolved_agent(self.SHELL), ""))
        self.assertEqual(outcome.status, sd_review.REFUSED, outcome.detail)
        self.assertIn('"shell"', outcome.detail)
        self.assertEqual(len(runner.calls), 1, "the review itself must not start")

    def test_the_probe_asks_the_same_opencode_launch_dir_and_environment(self) -> None:
        from tests.test_sd_review import resolved_agent
        outcome, runner = self.run_with(sd_review.Completed(0, resolved_agent(), ""))
        self.assertEqual(outcome.status, sd_review.CLEAN, outcome.detail)
        probe, review = runner.calls
        self.assertEqual(sd_opencode.probe_program(probe["argv"]), ["opencode"])
        self.assertEqual(probe["argv"][1:4], ["-I", str(pathlib.Path(sd_opencode.__file__).resolve()),
                                              sd_opencode.PROBE])
        self.assertEqual(probe["cwd"], review["cwd"])
        self.assertEqual(probe["env"], review["env"])
        self.assertNotIn("PWD", review["env"])
        config_dir = pathlib.Path(review["env"]["OPENCODE_CONFIG_DIR"])
        self.assertEqual(config_dir.parent, review["cwd"], "the config dir is the run's own")

    def test_a_probe_that_cannot_answer_refuses(self) -> None:
        for probe in (sd_review.Completed(1, "", "unknown command"), sd_review.Completed(0, "not json", ""),
                      sd_review.Completed(0, json.dumps({"agent": None}), "")):
            outcome, runner = self.run_with(probe)
            self.assertEqual(outcome.status, sd_review.REFUSED, outcome.detail)
            self.assertIn("unconfirmed", outcome.detail)
            self.assertEqual(len(runner.calls), 1)

    def breach(self, *extra: dict, accepted: list | None = None, **report: Any) -> str | None:
        from tests.test_sd_review import resolved_agent
        stdout = resolved_agent(*extra, **report)
        return sd_opencode.confinement_breach(
            lambda *args: sd_review.Completed(0, stdout, ""), "opencode run", {"HOME": "/h"}, pathlib.Path("/x"),
            60, accepted=accepted)

    def test_only_the_map_and_trailing_denies_pass(self) -> None:
        self.assertIsNone(self.breach())
        # Not a list of known keys: an unknown tool, a re-opened MCP read and
        # an external directory all allow, and all refuse.
        for rule in (self.SHELL, {"action": "mutator_mutate", "resource": "*", "effect": "allow"},
                     {"action": "read", "resource": "mcp:*", "effect": "allow"},
                     {"action": "external_directory", "resource": "*", "effect": "allow"}):
            self.assertIsNotNone(self.breach(rule), rule)

    def test_a_rule_after_the_deny_is_judged_by_its_effect(self) -> None:
        """sd:3498: 2.0.25 appended `browser: deny` after the map, and the
        pinned list refused every review until it was named. Under last-match
        a deny grants nothing, so any deny passes and is named; allow, ask
        and an unknown effect refuse."""
        browser = {"action": "browser", "resource": "*", "effect": "deny"}
        for rule in (browser, {**browser, "resource": "x"},
                     {"action": "grep", "resource": "*", "effect": "deny"}):
            accepted: list = []
            self.assertIsNone(self.breach(rule, accepted=accepted), rule)
            self.assertEqual(accepted, [rule])
        for effect in ("allow", "ask", "maybe", ""):
            self.assertIsNotNone(self.breach({**browser, "effect": effect}), effect)

    def test_a_map_rule_that_is_missing_refuses(self) -> None:
        from tests.test_sd_review import resolved_agent
        report = json.loads(resolved_agent())
        glob = {"action": "glob", "resource": "*", "effect": "allow"}
        report["agent"]["permissions"].remove(glob)
        stdout = json.dumps(report)
        breach = sd_opencode.confinement_breach(
            lambda *args: sd_review.Completed(0, stdout, ""), "opencode run", {"HOME": "/h"}, pathlib.Path("/x"), 60)
        self.assertIsNotNone(breach)

    def test_an_accepted_deny_is_named_in_the_outcome(self) -> None:
        """The ruling's receipt: the outcome report carries what was accepted."""
        from tests.test_sd_review import resolved_agent
        narrowing = {"action": "browser", "resource": "x", "effect": "deny"}
        outcome, runner = self.run_with(sd_review.Completed(0, resolved_agent(narrowing), ""))
        self.assertEqual(outcome.status, sd_review.CLEAN, outcome.detail)
        self.assertEqual(outcome.outcome_report()["diagnostic"],
                         {"version": "opencode v2.0.20", "accepted_after_deny": [narrowing]})

    def test_the_receipt_names_the_binary_version(self) -> None:
        """sd:3499. A refusal or a changed finding rate could not be tied to an upgrade from the receipts."""
        from tests.test_sd_review import resolved_agent
        outcome, _ = self.run_with(sd_review.Completed(0, resolved_agent(version="opencode v2.0.31"), ""))
        self.assertEqual(outcome.status, sd_review.CLEAN, outcome.detail)
        self.assertEqual(outcome.outcome_report()["diagnostic"], {"version": "opencode v2.0.31"})

    def test_a_probe_timeout_reads_as_a_timeout_and_names_its_stage(self) -> None:
        """sd:3499. A slow machine read as an unconfirmed confinement, with no stage or elapsed time."""
        stalled = json.dumps({"version": "opencode v2.0.20", "agent": None, "plugins": None,
                              "stage": "serve never listened", "elapsed": 55.2, "seconds": 55})
        breach = sd_opencode.confinement_breach(
            lambda *args: sd_review.Completed(0, stalled, ""), "opencode run", {"HOME": "/h"}, pathlib.Path("/x"), 60)
        self.assertIn("timed out after 55.2 s of 55 s", breach or "")
        self.assertIn("serve never listened", breach or "")
        self.assertIn("not a breach", breach or "")
        killed = sd_opencode.confinement_breach(
            lambda *args: sd_review.Completed(124, "", "python3: timed out after 60s", launched=False),
            "opencode run", {"HOME": "/h"}, pathlib.Path("/x"), 60)
        self.assertIn("timed out after 60 s", killed or "")
        self.assertIn("not a breach", killed or "")

    def test_the_probe_reports_where_it_stalled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stub = pathlib.Path(raw) / "opencode"
            stub.write_text('#!/bin/sh\n[ "$1" = --version ] && { echo "opencode v2.0.20"; exit 0; }\nexec sleep 30\n')
            stub.chmod(0o755)
            report = sd_opencode.resolve_probe([str(stub)], 1)
        self.assertEqual((report["stage"], report["seconds"]), ("serve never listened", 1))
        self.assertGreaterEqual(report["elapsed"], 1)
        self.assertIsNone(report["agent"])

    def test_a_resolution_without_the_default_deny_refuses(self) -> None:
        stdout = json.dumps({"version": "opencode v2.0.20", "plugins": [],
                             "agent": {"permissions": [{"action": "*", "resource": "*", "effect": "allow"}]}})
        breach = sd_opencode.confinement_breach(
            lambda *args: sd_review.Completed(0, stdout, ""), "opencode run", {"HOME": "/h"}, pathlib.Path("/x"), 60)
        self.assertIn("no `*: deny`", breach or "")

    def test_an_unmeasured_major_version_refuses_and_names_it(self) -> None:
        for version in ("1.18.30", "opencode v3.0.0", ""):
            breach = self.breach(version=version) or ""
            self.assertIn("is not 2.x", breach, version)
            self.assertIn(version or "(no version)", breach)

    def test_a_plugin_that_is_not_built_in_refuses_whatever_its_state(self) -> None:
        """2.x imports every file in a config dir's `plugins/`, and one that
        then fails to register has still run (measured on 2.0.20)."""
        from tests.test_sd_review import BUILTIN_PLUGINS
        for state in ("active", "failed"):
            local = {"id": "widen", "source": {"type": "local", "path": "/x/plugins/widen.js"},
                     "state": {"status": state}}
            breach = self.breach(plugins=[*BUILTIN_PLUGINS, local]) or ""
            self.assertIn("not built in", breach, state)
            self.assertIn("/x/plugins/widen.js", breach)
        self.assertIsNone(self.breach(plugins=BUILTIN_PLUGINS))
        self.assertIn("unconfirmed", self.breach(plugins="none") or "")

    def test_the_probe_argv_is_the_entrys_program(self) -> None:
        argv = sd_opencode.probe_argv("/opt/oc/bin/opencode run", 40)
        self.assertEqual(sd_opencode.probe_program(argv), ["/opt/oc/bin/opencode"])
        self.assertEqual(argv[4], "40")
        self.assertIsNone(sd_opencode.probe_program(["opencode", "run"]))
        self.assertEqual(sd_opencode.major_version("opencode v2.0.20"), 2)
        self.assertIsNone(sd_opencode.major_version("opencode"))


if __name__ == "__main__":
    unittest.main()
