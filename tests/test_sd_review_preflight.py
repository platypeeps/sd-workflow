"""Explicit provider protocol probes use canned responses, never real providers."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
from unittest import mock

from tests.test_sd_review import FakeClient, ReviewFixture, namespace, sd_review

REGISTRY = """
bills:
  fixture: { cost: subscription }
providers:
  minimax: { url: "https://minimax.invalid/v1", model: fixture-minimax, vendor: minimax, bill: fixture, roles: [reviewer], env: [KEY] }
  kimi: { url: "https://kimi.invalid/v1", model: fixture-kimi, vendor: moonshot, bill: fixture, roles: [reviewer], env: [KEY] }
  author: { start: "fixture-author", vendor: other, bill: fixture, reader: codex-json, roles: [author, reviewer], env: [] }
roles:
  author: [author]
  reviewer: [minimax, kimi, author]
"""


#: A registry of `url` entries whose credential rule is the subject, not their
#: protocol. `local` takes the loopback exemption, `keyed` and `remote` hold
#: one half of it each, so the three rows cover every branch of the preflight
#: predicate rather than one row and a restatement of the other two.
CREDENTIAL_REGISTRY = """
bills:
  fixture: { cost: subscription }
providers:
  local: { url: "http://localhost:8084/v1", model: fixture-local, vendor: localvendor, bill: fixture, roles: [reviewer], env: [] }
  keyed: { url: "http://localhost:8084/v1", model: fixture-keyed, vendor: keyedvendor, bill: fixture, roles: [reviewer], env: [LOCAL_KEY] }
  remote: { url: "https://remote.invalid/v1", model: fixture-remote, vendor: remotevendor, bill: fixture, roles: [reviewer], env: [] }
  author: { start: "fixture-author", vendor: other, bill: fixture, reader: codex-json, roles: [author, reviewer], env: [] }
roles:
  author: [author]
  reviewer: [local, keyed, remote, author]
"""


class PreflightFixture(ReviewFixture):
    """The repository, registry and consent every probe below runs against.

    The registry, the consent line, the probed entry and the environment are
    class attributes so a second registry needs a second class rather than a
    second copy of this setup. A subclass that changed them by overriding
    `setUp` would inherit this class's tests too and run them against the
    wrong registry.
    """

    registry = REGISTRY
    reviewers = "minimax@minimax.invalid, kimi@kimi.invalid, author@fixture-author"
    probed = "minimax"
    probe_environment: dict[str, str] = {"KEY": "secret-key-marker"}

    def setUp(self):
        super().setUp()
        self.registry_path = self.registry_home / ".local/share/sd/providers.yaml"
        self.registry_path.write_text(self.registry)
        self.root = self.make_repo()
        self.consent(self.reviewers)
        subprocess.run(["git", "checkout", "-qb", "fixture"], cwd=self.root, check=True, capture_output=True)
        (self.root / "private.py").write_text('private_source = "never-transmit-marker"\n')
        self.commit("human")
        self.client = FakeClient()
        self.runner = mock.Mock(side_effect=AssertionError("preflight called repository checks or a provider CLI"))

    def consent(self, value):
        (self.root / "CLAUDE.local.md").write_text(
            "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\nreviewers: " + value +
            "\ncontext: never-transmit-convention\n<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n")

    def commit(self, author):
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "fixture\n\nAuthored-with: " + author],
                       cwd=self.root, check=True, capture_output=True)

    def probe(self, **values):
        overrides = {"preflight": True, "provider": self.probed, **values}
        with mock.patch.object(sd_review, "local_conventions", side_effect=AssertionError("private conventions read")), mock.patch.object(
            sd_review, "review_material", side_effect=AssertionError("private source read")
        ):
            return sd_review.review(self.root, namespace(**overrides), self.runner,
                                    self.environment(**self.probe_environment), client=self.client)


class ProviderPreflight(PreflightFixture):

    def test_one_selected_probe_is_distinct_from_review_and_sends_no_repository(self):
        before = {str(p.relative_to(self.tmp)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.tmp.rglob("*") if p.is_file()}
        result = self.probe()
        self.assertEqual(result["status"], "preflight_passed")
        self.assertEqual(result["operation"], "provider_preflight")
        self.assertEqual(result["scope"], "provider_preflight")
        self.assertEqual((result["requested_reviews"], result["completed_reviews"], result["reviewed_by"]), (0, 0, []))
        self.assertIsNone(result["check"])
        self.assertEqual(result["probe_calls"], 1)
        self.assertEqual([row["provider"] for row in self.client.sent], ["minimax"])
        for forbidden in ("never-transmit", "private.py", str(self.root), "secret-key-marker"):
            self.assertNotIn(forbidden, self.client.sent[0]["prompt"])
        self.assertIn("Schema:", self.client.sent[0]["prompt"])
        self.assertEqual(before, {str(p.relative_to(self.tmp)): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in self.tmp.rglob("*") if p.is_file()})
        output = io.StringIO()
        sd_review.render(result, output)
        self.assertIn("no branch review", output.getvalue())

    def test_probe_cannot_be_used_as_a_prior_branch_review(self):
        result = self.probe()
        saved = self.tmp / "probe.json"
        saved.write_text(json.dumps(result))
        self.client.sent.clear()
        with self.assertRaisesRegex(sd_review.UsageError, "prior review report does not match"):
            sd_review.review(self.root, namespace(scope="branch", provider="minimax", verify_report=str(saved),
                base=result["subject"]["head"]), self.runner, self.environment(KEY="secret"), client=self.client)
        self.assertEqual(self.client.sent, [])

    def test_both_named_providers_use_the_same_protocol_and_one_call_each(self):
        for name in ("minimax", "kimi"):
            with self.subTest(provider=name):
                self.client.sent.clear()
                result = self.probe(provider=name)
                self.assertEqual(result["status"], "preflight_passed")
                self.assertEqual([row["provider"] for row in self.client.sent], [name])

    def test_explain_never_dispatches_or_claims_success(self):
        result = self.probe(explain=True)
        self.assertEqual(result["status"], "preflight_planned")
        self.assertEqual(result["probe_calls"], 0)
        self.assertEqual(result["outcomes"], [])
        self.assertEqual(self.client.sent, [])

    def test_missing_key_is_refused_before_client(self):
        with mock.patch.object(self, "environment", return_value={"HOME": str(self.registry_home)}):
            result = self.probe()
        self.assertEqual(result["status"], "preflight_failed")
        self.assertIn("credential", result["probe_refusal"])
        self.assertEqual((result["probe_calls"], self.client.sent), (0, []))

    def test_consent_and_disabled_provider_send_nothing(self):
        self.consent("")
        with self.assertRaises(sd_review.sd_registry.ConsentRefusal):
            self.probe()
        self.consent("minimax@minimax.invalid")
        self.registry_path.write_text(REGISTRY.replace("model: fixture-minimax", "enabled: false, reason: fixture, model: fixture-minimax"))
        with self.assertRaises(sd_review.sd_registry.RegistryError):
            self.probe()
        self.assertEqual(self.client.sent, [])

    def test_incompatible_flags_and_cli_provider_send_nothing(self):
        for values in ({"provider": None}, {"scope": "planning"}, {"scope": "pr"}, {"base": "a" * 40},
                       {"verify_report": "prior.json"}, {"resume_report": "prior.json"}, {"item": "work"},
                       {"draft": True}, {"challenge": True}, {"dry_run": True}, {"expected_timing": "a"},
                       {"provider": "author"}):
            with self.subTest(values=values), self.assertRaises(sd_review.UsageError):
                self.probe(**values)
        self.assertEqual(self.client.sent, [])

    def test_fabricated_findings_fail_the_fixed_empty_protocol_probe(self):
        body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"findings": [
            {"severity": "high", "instances": ["src.py:1"], "family": "correctness", "path": "invented.py", "line": 1,
             "summary": "A fabricated finding without any supplied repository."}]})}}]}
        self.client = FakeClient(default=(0, json.dumps(body), "", True))
        result = self.probe()
        self.assertEqual(result["status"], "preflight_failed")
        self.assertEqual(result["protocol_error"], "synthetic preflight expected an empty findings list")
        self.assertEqual(result["outcomes"][0]["status"], sd_review.FINDINGS)
        self.assertEqual(result["outcomes"][0]["findings"], 1)
        self.assertEqual(result["completed_reviews"], 0)
        self.assertEqual(len(self.client.sent), 1)

    def test_failures_never_fall_back_or_complete_a_review(self):
        cases = [(1, "", "connection failed", False), (429, "{}", "HTTP 429", True),
                 (0, '{"choices":[{"finish_reason":"length","message":{"content":"{\\"findings\\":[]}"}}]}', "", True),
                 (0, '{"choices":[{"message":{"content":"{}"}}]}', "", True)]
        for response in cases:
            self.client = FakeClient(default=response)
            result = self.probe()
            self.assertEqual(result["status"], "preflight_failed")
            self.assertEqual(sd_review.STATUS_EXIT[result["status"]], 5)
            self.assertEqual([row["provider"] for row in self.client.sent], ["minimax"])
            self.assertEqual(result["completed_reviews"], 0)

    def test_actual_cli_explain_has_no_review_or_process_boundary(self):
        with mock.patch.object(sd_review.sd_lib, "repo_root", return_value=self.root), mock.patch.dict(
            sd_review.os.environ, self.environment(KEY="secret-key-marker"), clear=True
        ), mock.patch.object(sd_review, "subprocess_runner", self.runner), mock.patch.object(
            sd_review.sd_registry._OPENER, "open", side_effect=AssertionError("HTTP request")
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            code = sd_review.main(["--preflight", "--provider", "minimax", "--explain", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["operation"], "provider_preflight")


class PreflightCredentialRule(PreflightFixture):
    """Who the probe may call without a key, asserted through the probe.

    `--preflight` carries its own credential test, beside the one
    `sd_registry.chat_completion` applies to a review. The transport's copy
    is pinned in `tests/test_sd_registry.py`; this one was not pinned at all,
    so the pair could disagree and the probe would refuse an entry the review
    it rehearses would have called -- which is the one thing a rehearsal must
    not do.

    The exemption is deliberate and knowingly accepted: a server on loopback
    is already restricted to processes running as this user, and the registry
    has no way to spell "this recipient authenticates nobody". It is also
    narrow. Loopback AND no declared variable, because each half alone opens
    a hole, and the three entries here hold the exemption and both halves.
    """

    registry = CREDENTIAL_REGISTRY
    reviewers = "local@localhost:8084, keyed@localhost:8084, remote@remote.invalid, author@fixture-author"
    probed = "local"
    probe_environment: dict[str, str] = {}

    def test_a_loopback_entry_declaring_no_variable_probes_without_a_key(self):
        """No key is exported at all, and the probe still reaches the client."""
        result = self.probe()
        self.assertIsNone(result["probe_refusal"])
        self.assertEqual(result["status"], "preflight_passed")
        self.assertEqual(result["probe_calls"], 1)
        self.assertEqual([row["provider"] for row in self.client.sent], ["local"])
        self.assertNotIn("LOCAL_KEY", self.client.sent[0]["env"])

    def test_a_loopback_entry_that_declares_a_variable_still_needs_its_value(self):
        """Half one. A server configured to check a key is not called without one."""
        result = self.probe(provider="keyed")
        self.assertEqual(result["status"], "preflight_failed")
        self.assertEqual(result["probe_refusal"], "keyed has no declared credential value")
        self.assertEqual((result["probe_calls"], self.client.sent), (0, []))

    def test_a_public_entry_that_declares_no_variable_is_still_refused(self):
        """Half two. Declaring nothing is not a licence to call the internet."""
        result = self.probe(provider="remote")
        self.assertEqual(result["status"], "preflight_failed")
        self.assertEqual(result["probe_refusal"], "remote has no declared credential value")
        self.assertEqual((result["probe_calls"], self.client.sent), (0, []))

    def test_the_declared_value_is_read_and_the_probe_then_runs(self):
        """The refusal is the missing value, not the declaration."""
        self.probe_environment = {"LOCAL_KEY": "local-secret-marker"}
        result = self.probe(provider="keyed")
        self.assertIsNone(result["probe_refusal"])
        self.assertEqual(result["status"], "preflight_passed")
        self.assertEqual([row["provider"] for row in self.client.sent], ["keyed"])

    def test_explain_reports_the_same_refusal_without_dispatching(self):
        """`--explain` is the page an operator reads before spending anything."""
        for name, refusal in (("local", None), ("keyed", "keyed has no declared credential value"),
                              ("remote", "remote has no declared credential value")):
            with self.subTest(provider=name):
                self.client.sent.clear()
                result = self.probe(provider=name, explain=True)
                self.assertEqual(result["probe_refusal"], refusal)
                self.assertEqual((result["probe_calls"], self.client.sent), (0, []))
