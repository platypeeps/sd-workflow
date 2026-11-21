"""Bounded, local response handling; no paid provider is called."""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import unittest
import urllib.error
from unittest import mock

from tests.test_sd_review import FakeClient, FakeRunner, ReviewFixture, sd_review


def finding(severity: str = "low", summary: str = "defect") -> dict:
    return {"path": "src.py", "line": 1, "severity": severity, "summary": summary, "family": "correctness",
            **({"instances": ["src.py:1"]} if severity == "high" else {})}


class ResponseRegressions(unittest.TestCase):
    def test_unparseable_success_is_not_quota_evidence(self) -> None:
        for content in ("unfinished JSON about rate_limit", "unfinished JSON about parsing"):
            outcome = sd_review._answer("url", sd_review.Completed(0, content, ""), None)
            self.assertEqual(outcome.status, sd_review.UNAVAILABLE)

    def test_failed_process_model_output_is_not_quota_evidence(self) -> None:
        text = json.dumps({"findings": [finding("high", "rate_limit defect")]})
        outcome = sd_review._answer("cli", sd_review.Completed(1, text, "pipe failed"),
                                   sd_review.parse_findings(text))
        self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
        self.assertEqual(outcome.findings[0]["severity"], "high")

    def test_oversized_response_is_refused_before_json_parsing(self) -> None:
        with mock.patch.object(sd_review, "MAX_OUTPUT_BYTES", 100), mock.patch.object(
            sd_review.json, "loads", side_effect=AssertionError("oversized input reached JSON parser")
        ):
            parsed = sd_review.parse_findings("x" * 101)
        self.assertIsNotNone(parsed)
        self.assertIn("limit", parsed.error)
        self.assertTrue(any(row["severity"] == "high" for row in parsed.findings))

    def test_findings_are_bounded_without_hiding_a_late_blocker(self) -> None:
        rows = [finding(summary=str(i)) for i in range(sd_review.MAX_FINDINGS)]
        rows.append(finding("high", "late blocker"))
        parsed = sd_review.parse_findings(json.dumps({"findings": rows}))
        self.assertLessEqual(len(parsed.findings), sd_review.MAX_FINDINGS)
        self.assertIn("limit", parsed.error)
        self.assertTrue(any(row["summary"] == "late blocker" for row in parsed.findings))
        self.assertTrue(any("omitted" in row["summary"] for row in parsed.findings))

    def test_missing_passwd_uid_keeps_only_declared_environment(self) -> None:
        provider = sd_review.sd_registry.Provider(name="local", vendor="local", bill="free", env=("OWN_KEY",))
        with mock.patch.object(sd_review.sd_registry.pwd, "getpwuid", side_effect=KeyError(424242)):
            env = sd_review.sd_registry.provider_environment(provider, {
                "USER": "fixture-user", "OWN_KEY": "owned", "OTHER_KEY": "unrelated"
            })
        self.assertEqual(env, {"OWN_KEY": "owned"})


class URLDiagnostics(ReviewFixture):
    def run_response(self, payload: object, exit_code: int = 0, stderr: str = "", max_tokens: int | None = 16384):
        body = payload if isinstance(payload, str) else json.dumps(payload)
        client = FakeClient(default=(exit_code, body, stderr, True))
        provider = sd_review.sd_registry.Provider(name="url", vendor="fixture", bill="free",
            url="https://fixture.example/v1", env=("OWN_KEY",), max_tokens=max_tokens)
        return sd_review.run_provider(provider, self.tmp,
            sd_review.Subject("worktree", "HEAD", "worktree", (), 0, ""),
            "private-prompt-marker", FakeRunner(), {"OWN_KEY": "credential-marker"}, 7,
            client=client)

    def envelope(self, content: object, reason: str = "stop", **extra: object) -> dict:
        return {"choices": [{"finish_reason": reason, "message": {"content": content, **extra}}]}

    def test_the_raw_response_rides_the_diagnostic_only_when_asked(self) -> None:
        """`SD_REVIEW_RAW_DIR` puts a url reviewer's raw response in its diagnostic.

        The first MiniMax pass on system #590 failed and left only hashes.
        sd-review writes no file; sd-ship moves the field to one, so the
        database never holds it. The prompt and credential never ride along.
        """
        body = self.envelope("not the findings JSON")
        with mock.patch.dict(sd_review.os.environ, {}, clear=False):
            sd_review.os.environ.pop("SD_REVIEW_RAW_DIR", None)
            outcome = self.run_response(body)
        self.assertNotIn("raw_response", outcome.diagnostic)
        with mock.patch.dict(sd_review.os.environ, {"SD_REVIEW_RAW_DIR": str(self.tmp / "raw")}):
            outcome = self.run_response(body, stderr="transport note")
        raw = outcome.diagnostic["raw_response"]
        self.assertEqual(json.loads(raw["body"]), body)
        self.assertEqual(raw["stderr"], "transport note")
        self.assertNotIn("private-prompt-marker", json.dumps(raw))
        self.assertNotIn("credential-marker", json.dumps(raw))
        self.assertFalse((self.tmp / "raw").exists(), "sd-review itself writes no file")

    def test_reasoning_only_does_not_borrow_model_quota_words(self) -> None:
        outcome = self.run_response(self.envelope(None, reasoning_content="rate_limit credential-marker private-prompt-marker"))
        self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
        self.assertEqual(outcome.diagnostic["category"], "reasoning_only")
        self.assertGreater(outcome.diagnostic["reasoning_bytes"], 0)
        encoded = json.dumps(outcome.diagnostic)
        for secret in ("credential-marker", "private-prompt-marker", "rate_limit"):
            self.assertNotIn(secret, encoded)

    def test_length_finish_never_completes_even_with_valid_json(self) -> None:
        for rows in ([], [finding("high", "truncated blocker")]):
            outcome = self.run_response(self.envelope(json.dumps({"findings": rows}), "length"))
            self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
            self.assertEqual(outcome.diagnostic["category"], "truncated")
            self.assertEqual(outcome.diagnostic["schema"], "invalid")
            self.assertEqual(bool(outcome.findings), bool(rows))

    def test_length_finish_names_the_max_tokens_it_hit(self) -> None:
        """sd:1805: kimi-k3 on example-simulator#215 reasoned through all
        16384 tokens and sent no content; the detail must say so, not shrug."""
        body = self.envelope("", "length", reasoning_content="private reasoning marker " * 40)
        body["usage"] = {"prompt_tokens": 34946, "completion_tokens": 16384, "total_tokens": 51330}
        outcome = self.run_response(body)
        self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
        self.assertIn("url hit max_tokens (16384) and it sent no answer: 16384 completion tokens", outcome.detail)
        self.assertIn(f"{outcome.diagnostic['reasoning_bytes']} bytes of reasoning (finish_reason length)", outcome.detail)
        self.assertNotIn("did not produce a usable answer", outcome.detail)
        self.assertNotIn("private reasoning marker", outcome.detail)
        body = self.envelope('{"findings": [', "length")
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 16384, "total_tokens": 16394}
        cut = self.run_response(body)
        self.assertIn("url hit max_tokens (16384) and its answer was cut off after 14 bytes", cut.detail)

    def test_length_finish_below_max_tokens_does_not_blame_the_budget(self) -> None:
        """`length` also means a full context window (DeepSeek's contract);
        a count below the ceiling must not send the operator to raise it."""
        for usage in ({"prompt_tokens": 10, "completion_tokens": 4096, "total_tokens": 4106}, None):
            body = self.envelope("", "length", reasoning_content="thinking")
            if usage:
                body["usage"] = usage
            outcome = self.run_response(body)
            self.assertIn("url stopped on length below max_tokens (16384) and it sent no answer", outcome.detail)
            self.assertIn("shorten the review input", outcome.detail)
            self.assertNotIn("hit max_tokens", outcome.detail)

    def test_length_finish_without_max_tokens_names_no_ceiling(self) -> None:
        """sd:1819: an uncapped entry may leave max_tokens unset; the detail
        must not read "below max_tokens (None)" or say to raise it."""
        body = self.envelope("", "length", reasoning_content="thinking")
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 131072, "total_tokens": 131082}
        outcome = self.run_response(body, max_tokens=None)
        self.assertIn("url stopped on length with no max_tokens set and it sent no answer: 131072 completion tokens",
                      outcome.detail)
        self.assertIn("set max_tokens for url in the provider registry", outcome.detail)
        self.assertNotIn("None", outcome.detail)
        self.assertNotIn("raise max_tokens", outcome.detail)

    def test_valid_findings_about_rate_limits_complete(self) -> None:
        outcome = self.run_response(self.envelope(json.dumps({"findings": [finding("high", "rate_limit defect")]})))
        self.assertEqual(outcome.status, sd_review.FINDINGS)
        self.assertEqual(outcome.diagnostic["schema"], "valid")
        self.assertEqual(outcome.diagnostic["normalization"], "none")

    def test_exact_json_fences_preserve_findings_and_original_diagnostics(self) -> None:
        for label in ("json", ""):
            for newline in ("\n", "\r\n"):
                for rows in ([], [finding("high", "fixture blocker")]):
                    content = "```" + label + newline + json.dumps({"findings": rows}) + newline + "```"
                    body = json.dumps(self.envelope(content))
                    outcome = self.run_response(body)
                    self.assertEqual(outcome.status, sd_review.FINDINGS if rows else sd_review.CLEAN)
                    self.assertEqual(list(outcome.findings), rows)
                    self.assertEqual(outcome.diagnostic["normalization"], "json_fence")
                    self.assertEqual(outcome.diagnostic["content_format"], "fenced")
                    self.assertEqual(outcome.diagnostic["response_text_sha256"], hashlib.sha256(body.encode()).hexdigest())
                    self.assertEqual(outcome.diagnostic["response_text_bytes"], len(body.encode()))

    def test_only_one_complete_json_fence_is_accepted(self) -> None:
        valid = json.dumps({"findings": []})
        wrapped = "```json\n" + valid + "\n```"
        for content in ("preface\n" + wrapped, wrapped + "\ntrailer", wrapped + "\n" + wrapped,
                        "```python\n" + valid + "\n```", "```JSON\n" + valid + "\n```",
                        "``` json\n" + valid + "\n```", "```json " + valid + "```",
                        "```json\n" + valid, valid + "\n```", "````json\n" + valid + "\n````",
                        "```json\n" + valid + "\n``", "rate_limit credential-marker"):
            with self.subTest(content=content):
                outcome = self.run_response(self.envelope(content))
                self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
                self.assertEqual(outcome.diagnostic["schema"], "unparseable")
                self.assertNotIn(content, json.dumps(outcome.diagnostic))

    def test_fenced_json_syntax_and_top_level_schema_remain_strict(self) -> None:
        for content, issue in (("not JSON", "invalid JSON at line 1 column 1"),
                               ('{"findings": []} trailing', "invalid JSON at line 1 column 18"),
                               ("[]", "response: expected object"),
                               ("{}", "response.findings: missing required field"),
                               ('{"findings": {}}', "response.findings: expected array")):
            with self.subTest(content=content):
                outcome = self.run_response(self.envelope("```json\n" + content + "\n```"))
                self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
                self.assertEqual(outcome.diagnostic["normalization"], "json_fence")
                self.assertEqual(outcome.diagnostic["validation_error"], issue)

    def test_first_schema_issue_is_precise_and_never_discloses_unknown_fields(self) -> None:
        base = finding("high", "retained blocker")
        cases = [(None, "expected object"), ({k: v for k, v in base.items() if k != "line"}, "line: missing required field"),
                 ({**base, "credential-marker": "private"}, "unexpected field count 1"),
                 ({**base, "path": " "}, "path: expected nonempty string"),
                 ({**base, "family": 2}, "family: expected nonempty string"),
                 ({**base, "summary": None}, "summary: expected nonempty string"),
                 ({**base, "severity": "credential-marker"}, "severity: invalid enum value"),
                 ({**base, "line": True}, "line: expected integer or null")]
        for row, issue in cases:
            payload = {"findings": [base, row, {"second-error-marker": "private"}]}
            for fenced in (False, True):
                with self.subTest(issue=issue, fenced=fenced):
                    content = json.dumps(payload)
                    outcome = self.run_response(self.envelope("```\n" + content + "\n```" if fenced else content))
                    self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
                    self.assertEqual(outcome.findings[0], base)
                    self.assertEqual(outcome.diagnostic["validation_error"],
                                     "response violates the findings schema: findings[1]." + issue)
                    for marker in ("credential-marker", "second-error-marker", "private"):
                        self.assertNotIn(marker, json.dumps(outcome.diagnostic))

    def test_a_blocking_finding_without_instances_is_refused(self) -> None:
        """Pack sd:9769: a high finding names its class (family) and lists every instance in the change."""
        listed = {**finding("high", "listed"), "instances": ["src.py:1", "src.py:9"]}
        outcome = self.run_response(self.envelope(json.dumps({"findings": [listed]})))
        self.assertEqual(outcome.status, sd_review.FINDINGS)
        self.assertEqual(outcome.findings[0]["instances"], ["src.py:1", "src.py:9"])
        for bare in ({k: v for k, v in listed.items() if k != "instances"}, {**listed, "instances": []},
                     {**listed, "instances": [" "]}):
            with self.subTest(bare=bare):
                outcome = self.run_response(self.envelope(json.dumps({"findings": [bare]})))
                self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
                self.assertEqual(outcome.diagnostic["validation_error"], "response violates the findings schema: "
                                 "findings[0].instances: a high finding lists no instance")
        advisory = finding("medium", "unlisted")
        self.assertEqual(self.run_response(self.envelope(json.dumps({"findings": [advisory]}))).status,
                         sd_review.FINDINGS, "a finding below high needs no instance")

    def test_top_level_extra_keys_refuse_without_disclosing_them(self) -> None:
        unknown_key = "secret-marker" * 1000
        outcome = self.run_response(self.envelope(json.dumps({"findings": [], unknown_key: True})))
        self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
        self.assertEqual(outcome.diagnostic["validation_error"],
                         "response violates the findings schema: response: unexpected field count 1")
        self.assertNotIn("secret-marker", json.dumps(outcome.diagnostic))
        self.assertLess(len(outcome.diagnostic["validation_error"]), 100)

    def test_every_required_field_stays_required_inside_a_fence(self) -> None:
        for key in finding():
            row = {k: v for k, v in finding().items() if k != key}
            outcome = self.run_response(self.envelope("```json\n" + json.dumps({"findings": [row]}) + "\n```"))
            self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
            self.assertIn(key + ": missing required field", outcome.diagnostic["validation_error"])

    def test_fence_decoding_never_bypasses_byte_or_finish_limits(self) -> None:
        # Keep the request within the patched limit so this exercises response bounds.
        content = '```json\n{"findings": []}\n' + ' ' * 4096 + '\n```'
        with mock.patch.object(sd_review, "MAX_OUTPUT_BYTES", len(content.encode()) - 1):
            outcome = self.run_response(self.envelope(content))
        self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
        self.assertIn("byte limit", outcome.diagnostic["validation_error"])
        self.assertNotEqual(outcome.diagnostic.get("normalization"), "json_fence")
        for reason in ("length", "content_filter", "tool_calls"):
            outcome = self.run_response(self.envelope(content, reason))
            self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
            self.assertEqual(outcome.diagnostic["normalization"], "json_fence")
            self.assertIn("incomplete response", outcome.diagnostic["validation_error"])
        rows = [finding()] * sd_review.MAX_FINDINGS + [finding("high", "late blocker")]
        outcome = self.run_response(self.envelope("```\n" + json.dumps({"findings": rows}) + "\n```"))
        self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
        self.assertLessEqual(len(outcome.findings), sd_review.MAX_FINDINGS)
        self.assertTrue(any(row["summary"] == "late blocker" for row in outcome.findings))
        self.assertIn("output limits", outcome.diagnostic["validation_error"])

    def test_schema_failure_preserves_adverse_evidence(self) -> None:
        row = finding("high", "blocker")
        row["extra"] = "untrusted"
        outcome = self.run_response(self.envelope(json.dumps({"findings": [row]})))
        self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
        self.assertEqual(outcome.findings[0]["summary"], "blocker")
        self.assertIn("schema", outcome.diagnostic["validation_error"])

    def test_api_error_and_http_429_have_separate_diagnostics(self) -> None:
        payload = {"error": {"code": "rate_limit", "message": "credential-marker private-prompt-marker"}}
        successful_transport = self.run_response(payload)
        self.assertEqual(successful_transport.status, sd_review.UNAVAILABLE)
        self.assertEqual(successful_transport.diagnostic["category"], "api_error")
        limited = self.run_response(payload, 429, "HTTP 429 from url")
        self.assertEqual(limited.status, sd_review.RATE_LIMITED)
        self.assertEqual(limited.diagnostic["http_status"], 429)
        self.assertNotIn("credential-marker", json.dumps(limited.diagnostic))

    def test_malformed_or_deep_envelope_refuses_without_crashing(self) -> None:
        for payload in ("not JSON", "[" * 2000, [], {"choices": []}, {"choices": [{}]}):
            outcome = self.run_response(payload)
            self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
            self.assertIn(outcome.diagnostic["category"], ("invalid_json", "invalid_envelope"))

    def test_network_limit_is_nonpassing_and_explicitly_preserves_unknown_blockers(self) -> None:
        with mock.patch.object(sd_review.sd_registry, "MAX_RESPONSE_BYTES", 64):
            outcome = self.run_response("rate_limit " + "x" * 65)
        self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
        self.assertEqual(outcome.diagnostic["category"], "response_limit")
        self.assertEqual(outcome.findings[0]["severity"], "high")
        self.assertIn("omitted", outcome.findings[0]["summary"])

    def test_integer_conversion_limit_is_a_nonpassing_redacted_diagnostic(self) -> None:
        content = '{"findings":[{"path":"src.py","line":' + "1" * 5000 + ',"severity":"high","instances":["src.py:1"],"summary":"private-marker","family":"correctness"}]}'
        for fenced in (False, True):
            with self.subTest(fenced=fenced):
                outcome = self.run_response(self.envelope("```json\n" + content + "\n```" if fenced else content))
                self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
                self.assertEqual(outcome.diagnostic["schema"], "unparseable")
                self.assertEqual(outcome.diagnostic["validation_error"], "JSON value exceeds the parser limit")
                self.assertNotIn("private-marker", json.dumps(outcome.diagnostic))
                self.assertNotIn("1" * 100, json.dumps(outcome.diagnostic))

    def test_direct_and_codex_parsers_do_not_accept_unrecorded_fence_normalization(self) -> None:
        content = '```json\n{"findings": []}\n```'
        self.assertIsNone(sd_review.parse_findings(content))
        provider = sd_review.sd_registry.Provider(name="fixture", vendor="fixture", bill="free",
            start="fixture exec", reader="codex-json")

        def reply(argv, env, cwd, timeout, input_text=None):
            target = pathlib.Path(argv[argv.index("--output-last-message") + 1])
            target.write_text(content)
            return sd_review.Completed(0, "", "")

        outcome = sd_review.run_provider(provider, self.tmp,
            sd_review.Subject("worktree", "HEAD", "worktree", (), 0, ""), "review", reply,
            self.environment(), 5, self.chatgpt_home())
        self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
        self.assertEqual(outcome.findings, ())

    def test_codex_answer_file_uses_a_bounded_read(self) -> None:
        provider = sd_review.sd_registry.Provider(name="fixture", vendor="fixture", bill="free",
            start="fixture exec", reader="codex-json")
        original = pathlib.Path.read_text

        def refuse_unbounded(path: pathlib.Path, *args, **kwargs):
            self.assertNotEqual(path.name, sd_review.CODEX_ANSWER_FILE)
            return original(path, *args, **kwargs)

        def reply(argv, env, cwd, timeout, input_text=None):
            target = pathlib.Path(argv[argv.index("--output-last-message") + 1])
            target.write_text("x" * 101)
            return sd_review.Completed(0, "", "")

        with mock.patch.object(sd_review, "MAX_OUTPUT_BYTES", 100), mock.patch.object(pathlib.Path, "read_text", refuse_unbounded):
            outcome = sd_review.run_provider(provider, self.tmp,
                sd_review.Subject("worktree", "HEAD", "worktree", (), 0, ""), "review", reply,
                self.environment(), 5, self.chatgpt_home())
        self.assertEqual(outcome.status, sd_review.UNAVAILABLE)
        self.assertEqual(outcome.findings[0]["severity"], "high")


class WireBounds(unittest.TestCase):
    def test_success_and_error_reads_are_bounded(self) -> None:
        registry = sd_review.sd_registry
        provider = registry.Provider(name="url", vendor="fixture", bill="free",
            url="https://fixture.example/v1", model="fixture", env=("OWN_KEY",))
        for code in (0, 429):
            class Body(io.BytesIO):
                def __init__(self):
                    super().__init__(b"x" * 1000)
                    self.sizes = []
                def read(self, size=-1):
                    self.sizes.append(size)
                    return super().read(size)
            body = Body()
            error = urllib.error.HTTPError(provider.url, code, "fixture", {}, body) if code else None
            with mock.patch.object(registry, "MAX_RESPONSE_BYTES", 64), mock.patch.object(
                registry._OPENER, "open", side_effect=error if code else None, return_value=body
            ):
                result = registry.chat_completion(provider, "private-prompt-marker", {"OWN_KEY": "credential-marker"}, 7)
            self.assertEqual(body.sizes, [65])
            self.assertTrue(body.closed)
            self.assertEqual(len(result[1].encode()), 65)
            self.assertNotEqual(result[0], 0)
            self.assertNotIn("private-prompt-marker", result[2])
            self.assertNotIn("credential-marker", result[2])


class RecoveryDiagnostics(ReviewFixture):
    run_response = URLDiagnostics.run_response
    envelope = URLDiagnostics.envelope

    def test_projection_preserves_identity_without_arbitrary_model_or_error_text(self):
        secret = "secret-credential-and-source-marker"
        payload = self.envelope('{"findings": []}', reasoning_content=secret)
        payload.update(model=secret, usage={"prompt_tokens": 12, "completion_tokens": 3,
                       "total_tokens": 15, "private": secret})
        outcome = self.run_response(payload)
        diagnostic = outcome.diagnostic
        self.assertNotIn(secret, json.dumps(diagnostic))
        safe = diagnostic["sanitized_response"]
        self.assertEqual(safe["model"]["sha256"], hashlib.sha256(secret.encode()).hexdigest())
        self.assertEqual(safe["message"]["reasoning_content"]["bytes"], len(secret))
        self.assertEqual(safe["usage"], {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15})
        self.assertFalse(diagnostic["model_matches_requested"])
        failed = self.run_response({"error": {"code": secret, "type": secret, "message": secret}}, 401, "HTTP 401")
        self.assertNotIn(secret, json.dumps(failed.diagnostic))
        self.assertEqual(set(failed.diagnostic["sanitized_response"]["error"]), {"code", "type", "message"})

    def test_usage_rejects_booleans_negative_strings_and_huge_values(self):
        payload = self.envelope('{"findings": []}')
        for invalid in (True, -1, "12", 10**13, None, {}):
            payload["usage"] = dict.fromkeys(("prompt_tokens", "completion_tokens", "total_tokens"), invalid)
            self.assertEqual(self.run_response(payload).diagnostic["sanitized_response"]["usage"], {})

    def test_transport_http_envelope_completion_and_schema_are_distinct(self):
        cases = [((1, "", "connection refused", False), "transport"),
                 ((401, '{"error":{"message":"private"}}', "HTTP 401", True), "http"),
                 ((429, "{}", "HTTP 429", True), "http"),
                 ((503, "{}", "HTTP 503", True), "http"),
                 ((0, '{"error":{"message":"private"}}', "", True), "api"),
                 ((0, "not JSON", "", True), "envelope"),
                 ((0, json.dumps(self.envelope('{"findings": []}', "length")), "", True), "completion"),
                 ((0, json.dumps(self.envelope('{}')), "", True), "schema")]
        for response, stage in cases:
            provider = sd_review.sd_registry.Provider(name="fixture", vendor="fixture", bill="fixture",
                url="https://fixture.invalid/v1", model="fixture-model", env=("KEY",))
            outcome = sd_review.run_provider(provider, self.tmp,
                sd_review.Subject("branch", "a" * 40, "b" * 40, (), 0, ""), "synthetic", FakeRunner(),
                {"KEY": "secret"}, 1, client=FakeClient(default=response))
            self.assertEqual(outcome.diagnostic["failure_stage"], stage, response)
            self.assertNotIn(outcome.status, (sd_review.CLEAN, sd_review.FINDINGS))

    def attempts(self, price, answers):
        answers = iter(answers)
        client = FakeClient(default=lambda _provider: next(answers))
        provider = sd_review.sd_registry.Provider(name="fixture", vendor="fixture", bill="fixture",
            url="https://fixture.invalid/v1", model="fixture-model", env=("KEY",), price=price)
        outcomes = list(sd_review._attempts(provider, lambda: sd_review.run_provider(provider, self.tmp,
            sd_review.Subject("branch", "a" * 40, "b" * 40, (), 0, ""), "synthetic", FakeRunner(),
            {"KEY": "secret"}, 1, client=client)))
        return outcomes, client

    def test_a_free_entry_retries_one_schema_failure_and_a_priced_one_does_not(self):
        """sd:1821: MiniMax-M3 (price 0/0) added a `severity_note` key on about
        one run in nine; a failed pass fell through to a reviewer that bills."""
        bad = (0, json.dumps(self.envelope(json.dumps({"findings": [{**finding("high"), "severity_note": "x"}]}))), "", True)
        good = (0, json.dumps(self.envelope(json.dumps({"findings": [finding("high", "real blocker")]}))), "", True)
        for price, calls in (({"in": 0, "out": 0}, 2), ({"in": 0.3, "out": 1.2}, 1), ({}, 1), ({"in": 0}, 1)):
            outcomes, client = self.attempts(price, (bad, good))
            self.assertEqual(len(client.sent), calls, price)
            self.assertEqual(len(outcomes), calls, price)
            self.assertEqual(outcomes[0].diagnostic["failure_stage"], "schema", price)
            if calls == 2:
                self.assertEqual(outcomes[1].status, sd_review.FINDINGS)
                self.assertEqual(outcomes[1].findings[0]["summary"], "real blocker")

    def test_a_retry_keeps_the_blockers_the_failed_attempt_recovered(self):
        """Codex on the sd:1821 PR: a clean retry must not clear a blocker the
        schema-invalid first answer carried; both outcomes reach the review."""
        bad = (0, json.dumps(self.envelope(json.dumps({"findings": [{**finding("high", "first blocker"), "extra": 1}]}))), "", True)
        clean = (0, json.dumps(self.envelope('{"findings": []}')), "", True)
        outcomes, _ = self.attempts({"in": 0, "out": 0}, (bad, clean))
        self.assertEqual([o.status for o in outcomes][1], sd_review.CLEAN)
        self.assertIn("first blocker", json.dumps([row for o in outcomes for row in o.findings]))

    def test_a_free_entry_retries_a_schema_failure_only_once(self):
        bad = (0, json.dumps(self.envelope('{}')), "", True)
        outcomes, client = self.attempts({"in": 0, "out": 0}, (bad, bad, bad))
        self.assertEqual(len(client.sent), 2)
        self.assertTrue(all(o.diagnostic["failure_stage"] == "schema" for o in outcomes))

    def test_a_transport_failure_is_not_retried(self):
        outcomes, client = self.attempts({"in": 0, "out": 0}, ((503, "{}", "HTTP 503", True),) * 2)
        self.assertEqual(len(client.sent), 1)

    def test_actual_http_status_requested_model_and_prompt_digest_survive(self):
        registry = sd_review.sd_registry
        provider = registry.Provider(name="fixture", vendor="vendor", bill="fixture",
            url="https://fixture.invalid/v1", model="fixture-model", env=("KEY",))
        payload = self.envelope('{"findings": []}')
        payload["model"] = "fixture-model"
        class Response(io.BytesIO):
            status = 201
        body = json.dumps(payload).encode()
        with mock.patch.object(registry._OPENER, "open", return_value=Response(body)) as opened:
            outcome = sd_review.run_provider(provider, self.tmp,
                sd_review.Subject("branch", "a" * 40, "b" * 40, (), 0, ""), "synthetic", FakeRunner(), {"KEY": "secret"}, 1)
        diagnostic = outcome.diagnostic
        sent = json.loads(opened.call_args.args[0].data)["messages"][0]["content"]
        self.assertEqual(diagnostic["http_status"], 201)
        self.assertEqual(diagnostic["request"]["model"], "fixture-model")
        self.assertEqual(diagnostic["request"]["head"], "b" * 40)
        self.assertEqual(diagnostic["request"]["prompt_sha256"], hashlib.sha256(sent.encode()).hexdigest())
        self.assertTrue(diagnostic["model_matches_requested"])
        self.assertIsNone(diagnostic["failure_stage"])
        self.assertNotIn("secret", json.dumps(diagnostic))

    def test_invalid_and_oversized_response_projection_stays_bounded_and_private(self):
        for body in ("private-marker", "private-marker" * 10000):
            with mock.patch.object(sd_review.sd_registry, "MAX_RESPONSE_BYTES", 100):
                outcome = self.run_response(body)
            self.assertNotIn("private-marker", json.dumps(outcome.diagnostic))
            self.assertLess(len(json.dumps(outcome.diagnostic)), 2000)
            self.assertEqual(outcome.diagnostic["sanitized_response"]["body"]["bytes"], len(body.encode()))
