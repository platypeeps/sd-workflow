"""The URL output reminder follows complete input without changing review gates."""

from __future__ import annotations

import hashlib
import json
import subprocess
from unittest import mock

from tests.test_sd_review import (
    FakeClient,
    FakeRunner,
    ReviewFixture,
    chat_answer,
    namespace,
    sd_review,
)

#: An answer that clears the prior blocker B1, so the gate alone decides (pack sd:10087).
FIXED = chat_answer(json.dumps({"findings": [], "verdicts": [{"id": "B1", "verdict": "fixed", "evidence": "prior.py:2 guards it"}]}))


class URLContractTests(ReviewFixture):
    def prepare(self):
        root = self.make_repo()
        (root / "prior.py").write_text("# unchanged prior blocker\n" + "value = 'évidence'\n" * 1000)
        self.stub_policy({"default_tier": "standard"})
        (root / "CLAUDE.local.md").write_text(
            "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\n"
            "reviewers: first@first.example, second@second.example, third@third.example\n"
            "review_context: keep-the-complete-local-provenance\n"
            "<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n")
        registry = self.registry_home / ".local/share/sd/providers.yaml"
        registry.write_text("bills:\n  fixture: {cost: subscription}\nproviders:\n"
            + "".join(f"  {name}: {{url: 'https://{name}.example/v1', vendor: {name}, bill: fixture, roles: [reviewer]}}\n"
                      for name in ("first", "second", "third"))
            + "roles:\n  author: []\n  reviewer: [first, second, third]\n")
        self.commit(root)
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        subprocess.run(["git", "checkout", "-qb", "work"], cwd=root, check=True)
        (root / "src.py").write_text("# first input marker\n" + "value = '完整'\n" * 10000 + "# final input marker\n")
        self.commit(root)
        prior = {"scope": "branch", "subject": {"head": base},
                 "findings": [{"path": "prior.py", "line": 2, "severity": "high", "instances": ["src.py:1"], "family": "correctness",
                               "summary": "unresolved original blocker", "disposition": "blocking"}]}
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(prior))
        return root, namespace(scope="branch", resume_report=str(report)), prior

    def commit(self, root):
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture\n\nAuthored-with: human"], cwd=root, check=True)

    def run_review(self, root, args, client, runner=None):
        return sd_review.review(root, args, runner or FakeRunner(), self.environment(), self.chatgpt_home(), client)

    def test_complete_history_source_and_provenance_are_an_unchanged_prefix(self):
        root, args, prior = self.prepare()
        client = FakeClient()
        with mock.patch.object(sd_review, "run_provider", wraps=sd_review.run_provider) as transport:
            result = self.run_review(root, args, client)
        subject, prompt = transport.call_args_list[0].args[2:4]
        material = sd_review.review_material(root, subject, prompt)
        prefix = f"{prompt}\nSchema: {json.dumps(sd_review.CODEX_OUTPUT_SCHEMA)}\n\nReview input:\n{material}"
        self.assertGreater(len(prefix.encode()), 150000)
        self.assertIn(json.dumps([{"id": "B1", **prior["findings"][0]}], sort_keys=True), prompt)
        self.assertIn((root / "prior.py").read_text(), prompt)
        self.assertIn("keep-the-complete-local-provenance", prompt)
        self.assertIn("+# first input marker", material)
        self.assertIn("+# final input marker", material)
        for sent in client.sent:
            self.assertEqual(sent["prompt"].encode()[:len(prefix.encode())], prefix.encode())
            footer = sent["prompt"][len(prefix):]
            self.assertTrue(footer.startswith("\n\nFinal response contract:"))
            self.assertEqual(footer, sd_review.URL_OUTPUT_CONTRACT)
            self.assertLess(len(footer.encode()), 1600)
            self.assertIn("80 words", footer)
            self.assertIn("every distinct current defect", footer)
            self.assertIn("findings and verdicts", footer)
            self.assertIn("Do not omit findings", footer)
            self.assertIn("no Markdown fences", footer)
            for key in sd_review.CODEX_OUTPUT_SCHEMA["properties"]["findings"]["items"]["required"]:
                self.assertIn(key, footer)
        expected = hashlib.sha256(json.dumps(prior, sort_keys=True).encode()).hexdigest()
        self.assertEqual(result["resume_report_digest"], expected)

    def test_selection_timing_and_checks_still_control_dispatch(self):
        root, args, _ = self.prepare()
        args.explain = True
        client = FakeClient(default=FIXED)
        planned = self.run_review(root, args, client)
        self.assertEqual(client.sent, [])
        args.explain = False
        failed = self.run_review(root, args, client, FakeRunner({"sd-check": sd_review.Completed(1, "{}", "failed")}))
        self.assertEqual(failed["status"], "gate_failed")
        # sd:2605: the reviewers run first, and a gate that fails after them still fails the review.
        self.assertEqual(sorted(call["provider"] for call in client.sent), sorted(planned["providers"]))  # sent at once (sd:7519)
        client.sent.clear()
        actual = self.run_review(root, args, client)
        for key in ("providers", "fallback_candidates", "timing", "requested_reviews"):
            self.assertEqual(actual[key], planned[key])
        self.assertEqual(sorted(call["provider"] for call in client.sent), sorted(planned["providers"]))  # sent at once (sd:7519)

    def test_footer_restates_the_validated_shape_last(self):
        # sd:1821: a reviewer that drifts from the schema 35k tokens back
        # reads the exact shape again, from the validator's own constants.
        footer = sd_review.URL_OUTPUT_CONTRACT
        restated = footer[footer.index("Output shape, restated"):]
        self.assertIn(f"at most {sd_review.MAX_FINDINGS} finding objects", restated)
        self.assertIn(f"exactly these {len(sd_review.FINDING_PROPERTIES)} keys and no other key: "
                      + ", ".join(sd_review.FINDING_PROPERTIES), restated)
        self.assertIn("severity note in summary, never in a new key", restated)
        self.assertIn("one of high, medium, low;", restated)
        self.assertNotIn("unspecified", restated)
        example = json.loads(restated[restated.index("Example: ") + len("Example: "):])
        parsed = sd_review.parse_findings(json.dumps(example))
        self.assertEqual(parsed.error, "")
        self.assertEqual(len(parsed.findings), 1)

    def test_summary_guidance_does_not_silently_truncate_findings(self):
        row = {"path": "src.py", "line": 1, "severity": "high", "instances": ["src.py:1"], "family": "correctness",
               "summary": ("meaningful defect " * 100).strip()}
        parsed = sd_review.parse_findings(json.dumps({"findings": [row]}))
        self.assertFalse(parsed.error)
        self.assertEqual(list(parsed.findings), [row])
