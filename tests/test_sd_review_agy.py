"""The `agy-json` reader: how it is invoked, read back, and kept independent.

`agy` is one command in front of several vendors' models. It serves Google's
Gemini, Anthropic's Claude and OpenAI's GPT-OSS from the same executable, so
an entry's `vendor` is a claim about the model rather than about the program.
The independence guard in `bin/sd_registry.py` compares that claim against the
branch's authorship trailers, which makes a wrong claim worse than a missing
one: the review would run on the author's own model and be reported as
independent. The refusals below are what keeps the claim honest.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_registry  # noqa: E402

from tests.test_sd_review import ReviewFixture, sd_review  # noqa: E402

#: The smallest registry that runs `agy`. Each test edits one line of it, so
#: the line a test changes is the reason it fails.
AGY = """\
bills:
  free: { cost: local }
providers:
  one: { url: "http://localhost:1/v1", vendor: alpha, bill: free, roles: [author] }
  agy: { start: "agy", model: MODEL, vendor: VENDOR, bill: free,
         roles: [reviewer], reader: agy-json }
roles:
  author: [one]
  reviewer: [agy]
"""


def registry_text(vendor: str = "google", model: str | None = "gemini-3.1-pro-high",
                  start: str = "agy", reader: str = "agy-json") -> str:
    text = AGY.replace("VENDOR", vendor).replace('"agy"', f'"{start}"')
    text = text.replace("reader: agy-json", f"reader: {reader}")
    return text.replace("model: MODEL, ", "") if model is None else text.replace("MODEL", model)


def written(case: unittest.TestCase, text: str) -> pathlib.Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    with handle:
        handle.write(text)
    case.addCleanup(pathlib.Path(handle.name).unlink)  # sd:3032: the file outlived every run
    return pathlib.Path(handle.name)


def provider(**overrides: object) -> object:
    fields: dict[str, object] = {
        "name": "agy", "vendor": "google", "bill": "free", "start": "agy",
        "model": "gemini-3.1-pro-high", "reader": "agy-json",
    }
    fields.update(overrides)
    return sd_registry.Provider(**fields)  # type: ignore[arg-type]


class TheReaderIsRunnable(unittest.TestCase):
    """A name in `READERS` that nothing runs is eligible and then refused."""

    def test_the_reader_is_listed(self) -> None:
        self.assertIn("agy-json", sd_review.READERS)

    def test_the_reader_carries_its_material_in_the_prompt(self) -> None:
        """`agy` reviews what it is handed; it does not resolve the subject."""
        self.assertIn("agy-json", sd_review.MATERIAL_READERS)

    def test_provider_argv_routes_to_the_agy_builder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workdir = pathlib.Path(raw)
            argv = sd_review.provider_argv(provider(), REPO_ROOT, workdir)
            self.assertEqual(argv, sd_review.agy_argv(workdir, "agy", "gemini-3.1-pro-high"))
            self.assertEqual(argv[0], "agy")


class TheArgvIsConfined(unittest.TestCase):
    """What the spawned session may reach, pinned flag by flag."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.workdir = pathlib.Path(self.dir.name)
        self.argv = sd_review.agy_argv(self.workdir, "agy", "gemini-3.1-pro-high")

    def test_it_sandboxes_and_refuses_slash_expansion(self) -> None:
        for flag in ("--sandbox", "--disable-slash-commands"):
            self.assertIn(flag, self.argv)

    def test_the_workspace_is_the_review_workdir(self) -> None:
        self.assertEqual(self.argv[self.argv.index("--add-dir") + 1], str(self.workdir))

    def test_it_never_skips_permissions(self) -> None:
        self.assertNotIn("--dangerously-skip-permissions", self.argv)
        self.assertNotIn("accept-edits", self.argv)

    def test_it_asks_for_the_declared_schema_as_json(self) -> None:
        """`stream-json`, not `json`: only the stream names the model that ran."""
        self.assertEqual(self.argv[self.argv.index("--output-format") + 1], "stream-json")
        schema = json.loads(self.argv[self.argv.index("--json-schema") + 1])
        self.assertEqual(schema, sd_review.CODEX_OUTPUT_SCHEMA)

    def test_the_prompt_is_attached_to_the_flag(self) -> None:
        """A separate word is read as the prompt by `agy`, whatever it is."""
        prompts = [word for word in self.argv if word.startswith("--print=")]
        self.assertEqual(len(prompts), 1, self.argv)
        self.assertIn(str(self.workdir / "review-subject.md"), prompts[0])

    def test_the_model_comes_from_the_entry(self) -> None:
        self.assertEqual(self.argv[self.argv.index("--model") + 1], "gemini-3.1-pro-high")
        self.assertNotIn("--model", sd_review.agy_argv(self.workdir, "agy", None))


def stream(result: dict[str, object], model: str | None = "gemini-3.1-pro-high") -> str:
    """The NDJSON `agy --output-format stream-json` writes, as observed."""
    init = {"event": "init", "conversation_id": "x",
            "init": {"model": model, "cwd": "/tmp", "tools": ["view_file"],
                     "permission_mode": "request-review"}}
    frames = [] if model is None else [json.dumps(init)]
    frames.append(json.dumps({"event": "result", "result": result}))
    return "\n".join(frames) + "\n"


class TheEnvelopeIsReadBack(unittest.TestCase):
    """The observed shape, and every malformed one that must not crash."""

    def answer(self, stdout: str, exit_code: int = 0,
               expected: str | None = None) -> tuple[object, object]:
        return sd_review.agy_answer(sd_review.Completed(exit_code, stdout, ""), expected)

    def test_a_successful_envelope_yields_its_findings(self) -> None:
        finding = {"path": "src.py", "line": 1, "severity": "high", "instances": ["src.py:1"],
                   "summary": "defect", "family": "correctness"}
        result, parsed = self.answer(stream(
            {"conversation_id": "x", "status": "SUCCESS", "response": "{}",
             "structured_output": {"findings": [finding]},
             "usage": {"total_tokens": 1}}), expected="gemini-3.1-pro-high")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(parsed.findings[0]["summary"], "defect")

    def test_an_error_envelope_is_nonzero_even_at_exit_zero(self) -> None:
        result, parsed = self.answer(stream(
            {"conversation_id": "", "status": "ERROR", "response": "",
             "error": "invalid model selection"}))
        self.assertEqual(result.exit_code, 1)
        self.assertIn("invalid model selection", result.stderr)
        self.assertIsNone(parsed)

    def test_malformed_envelopes_do_not_crash(self) -> None:
        cases = ["", "not json", "[]", "null", '"text"', "{}",
                 '{"event": "result"}', '{"event": "result", "result": {}}',
                 '{"event": 7, "result": {}}', '{"event": "init", "init": {}}',
                 stream({"status": 7}), stream({"status": "SUCCESS"}),
                 stream({"status": "SUCCESS", "structured_output": []}),
                 stream({"status": "ERROR", "error": None}),
                 stream({"status": "SUCCESS", "structured_output": {"findings": "no"}}),
                 # A literal past `sys.get_int_max_str_digits()`. `json.loads`
                 # raises a bare `ValueError`, not `JSONDecodeError`.
                 '{"event": "result", "result": {"status": "SUCCESS", '
                 '"structured_output": {"findings": [{"line": ' + "9" * 5000 + "}]}}}"]
        for stdout in cases:
            with self.subTest(stdout=stdout[:60]):
                result, parsed = self.answer(stdout)
                self.assertNotEqual((result, parsed), (None, None))

    def test_a_denied_tool_run_is_not_read_as_a_clean_review(self) -> None:
        """Observed: `agy` reports SUCCESS when a tool was auto-denied.

        The response is empty and no schema key is written, so reading
        `status` alone would turn a session that did nothing into a clean bill.
        """
        result, parsed = self.answer(stream(
            {"conversation_id": "x", "status": "SUCCESS", "response": "",
             "denied_actions": [{"action": "write_file", "display_name": "WriteToFile"}],
             "usage": {"total_tokens": 1}}))
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(parsed)

    def test_a_substituted_model_is_not_a_review(self) -> None:
        """Argv asks for a model; the `init` frame says which one answered.

        A Google entry served by an Anthropic model is the defeat the vendor
        guard exists to stop, reached below the registry instead of in it.
        """
        clean = {"conversation_id": "x", "status": "SUCCESS", "response": "{}",
                 "structured_output": {"findings": []}}
        result, parsed = self.answer(stream(clean, model="claude-opus-4-6-thinking"),
                                     expected="gemini-3.1-pro-high")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("claude-opus-4-6-thinking", result.stderr)
        self.assertIn("not a review", result.stderr)
        self.assertIsNone(parsed)

    def test_a_stream_that_names_no_model_refuses_a_pinned_entry(self) -> None:
        """Unverifiable fails closed; the pin is what independence rests on."""
        clean = {"conversation_id": "x", "status": "SUCCESS", "response": "{}",
                 "structured_output": {"findings": []}}
        result, parsed = self.answer(stream(clean, model=None), expected="gemini-3.1-pro-high")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("reported no model", result.stderr)
        self.assertIsNone(parsed)

    def test_the_matching_model_passes_through(self) -> None:
        result, parsed = self.answer(stream(
            {"conversation_id": "x", "status": "SUCCESS", "response": "{}",
             "structured_output": {"findings": []}}), expected="gemini-3.1-pro-high")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(parsed.findings, ())

    def test_an_oversized_response_becomes_a_blocker(self) -> None:
        _result, parsed = self.answer("x" * (sd_review.MAX_OUTPUT_BYTES + 1))
        self.assertEqual(parsed.error, "response exceeds the declared byte limit")


class TheMaterialReachesTheSession(ReviewFixture):
    """End to end: `agy` resolves nothing itself, so the diff must be written.

    `agy_argv` points the prompt at `review-subject.md` inside `--add-dir`.
    Nothing else puts it there, and a session handed an empty file would
    review nothing and report it clean.
    """

    def test_the_subject_lands_where_the_prompt_points(self) -> None:
        root = self.make_repo()
        (root / "src.py").write_text("agy_exact_subject = True\n")
        entry = sd_review.sd_registry.Provider(
            name="agy", vendor="google", bill="first", start="agy",
            model="gemini-3.1-pro-high", reader="agy-json", env=())
        calls: list[dict[str, Any]] = []

        def reply(argv: Any, env: Any, cwd: Any, timeout: Any) -> Any:
            subject = pathlib.Path(argv[argv.index("--add-dir") + 1]) / "review-subject.md"
            calls.append({"argv": argv, "material": subject.read_text()})
            return sd_review.Completed(0, stream(
                {"conversation_id": "x", "status": "SUCCESS", "response": "{}",
                 "structured_output": {"findings": []}}), "")

        outcome = sd_review.run_provider(
            entry, root, sd_review.resolve_subject(root, "worktree"),
            "instructions", reply, self.environment(), 5)
        self.assertEqual(outcome.status, sd_review.CLEAN, outcome.detail)
        self.assertIn("agy_exact_subject = True", calls[0]["material"])
        self.assertIn("instructions", calls[0]["material"])
        self.assertIn(str(pathlib.Path(calls[0]["argv"][calls[0]["argv"].index("--add-dir") + 1])),
                      next(word for word in calls[0]["argv"] if word.startswith("--print=")))


class TheVendorMustMatchTheModel(unittest.TestCase):
    """The defeat this guard exists for, at both of the places it can arrive."""

    def read(self, text: str) -> object:
        """Both readers, because a machine with a database uses the other one.

        `read` answers through `sd_db.registry` where the library is present
        and `read_file` never does, so checking one would leave the entry
        refused on half the machines.
        """
        path, outcomes = written(self, text), []
        for reader in (sd_registry.read_file, sd_registry.read):
            try:
                outcomes.append(reader(path))
            except sd_registry.RegistryError as error:
                outcomes.append(error)
        refused = [isinstance(one, sd_registry.RegistryError) for one in outcomes]
        self.assertEqual(refused[0], refused[1], outcomes)
        if isinstance(outcomes[0], sd_registry.RegistryError):
            raise outcomes[0]
        return outcomes[0]

    def test_a_matching_entry_reads(self) -> None:
        registry = self.read(registry_text())
        self.assertEqual(registry.providers["agy"].reader, "agy-json")

    def test_a_model_of_another_vendor_is_refused_at_parse_time(self) -> None:
        with self.assertRaises(sd_registry.RegistryError) as caught:
            self.read(registry_text(model="claude-opus-4-6-thinking"))
        message = str(caught.exception)
        self.assertIn("claude-opus-4-6-thinking", message)
        self.assertIn("anthropic", message)
        self.assertIn("wrong vendor", message)

    def test_a_model_hidden_in_the_start_line_is_refused_too(self) -> None:
        """A start line is argv: `--model` written there selects a model."""
        with self.assertRaises(sd_registry.RegistryError) as caught:
            self.read(registry_text(start="agy --model claude-sonnet-4-6"))
        self.assertIn("claude-sonnet-4-6", str(caught.exception))

    def test_an_entry_pinning_no_model_is_refused(self) -> None:
        """The defeat reached by omitting a key rather than by lying in one.

        `agy` picks its own default when no `--model` is given, and that
        default is configuration this registry cannot read. An operator who
        repoints it at an Anthropic model turns a `vendor: google` entry into
        an Anthropic reviewer, with the guard still reporting independence.
        """
        with self.assertRaises(sd_registry.RegistryError) as caught:
            self.read(registry_text(model=None))
        message = str(caught.exception)
        self.assertIn("pins no model", message)
        self.assertIn("may not be the one that reviewed", message)
        self.assertIn("cannot read", message)

    def test_a_start_line_model_satisfies_the_requirement(self) -> None:
        """`declared_models` reads both, so either place pins the entry."""
        registry = self.read(registry_text(model=None, start="agy --model gemini-3.1-pro-high"))
        self.assertIsNone(registry.providers["agy"].model)

    def test_the_requirement_is_keyed_on_the_reader_not_on_every_entry(self) -> None:
        """`claude -p` pins no model in the shipped registry and must not have to."""
        registry = self.read(registry_text(vendor="anthropic", model=None, reader="claude-json"))
        self.assertIsNone(registry.providers["agy"].model)
        self.assertNotIn("claude-json", sd_registry.MULTIVENDOR_READERS)

    def test_the_chain_refuses_a_missing_model_where_no_file_was_parsed(self) -> None:
        candidate = sd_registry._reviewer_candidate(
            provider(model=None), consent={},
            capped_bills={}, readers=sd_review.READERS)
        self.assertFalse(candidate.eligible)
        self.assertIn("pins no model", candidate.reason)

    def test_an_unfamiliar_model_name_draws_no_conclusion(self) -> None:
        registry = self.read(registry_text(vendor="moonshot", model="kimi-k3"))
        self.assertEqual(registry.providers["agy"].model, "kimi-k3")

    def test_the_shipped_registry_still_reads(self) -> None:
        shipped = sd_registry.read(sd_registry.shipped_path(REPO_ROOT))
        self.assertTrue(shipped.providers)

    def test_the_chain_refuses_it_where_no_file_was_parsed(self) -> None:
        """`_adapt` builds providers from `sd_db` rows without `_provider`."""
        candidate = sd_registry._reviewer_candidate(
            provider(model="claude-opus-4-6-thinking"), consent={},
            capped_bills={}, readers=sd_review.READERS)
        self.assertFalse(candidate.eligible)
        self.assertIn("anthropic", candidate.reason)

    def test_a_matching_entry_is_not_refused_by_the_same_check(self) -> None:
        self.assertIsNone(
            sd_registry.refuse_vendor_model("agy", "google", "gemini-3.1-pro-high", "agy", "agy-json"))


class TheBillCannotBeCapped(unittest.TestCase):
    """A spawned command has no call to reserve, so a cap would enforce nothing."""

    def test_a_capped_bill_refuses_the_entry(self) -> None:
        text = registry_text().replace("free: { cost: local }", "free: { cost: company, cap_usd_month: 50 }")
        with self.assertRaises(sd_registry.RegistryError) as caught:
            sd_registry.read(written(self, text))
        self.assertIn("nothing enforces", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
