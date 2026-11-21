"""The provider registry, read two ways.

`bin/sd_registry.py` answers through `sd_db.registry` when the library is
installed and from the file alone when it is not. Criterion 32 is the reason
both are allowed to exist and the only thing that keeps them honest: they must
return the same reviewer order from the same `providers.yaml`.

So the comparison below does not skip when the library is missing. A skip
there would leave the one test that pins two implementations together passing
on one of them, which is the failure it exists to catch. `make setup`
provisions the library and CI checks the sibling repository out; where neither
has happened this file says so and fails.
"""

from __future__ import annotations

import ast
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import replace
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_registry  # noqa: E402

SHIPPED = sd_registry.shipped_path(REPO_ROOT)

#: The line `review` in `bin/sd-review` hands the chain for a bill at its
#: cap: the month's total, so a refusal names it without a second lookup.
AT_CAP = "$50.00 of its $50.00 cap for 2026-09 is spent or held"

#: A whole registry in four lines, for the refusal cases. Each test edits one
#: thing, so the line it changes is the reason it fails.
MINIMAL = """\
bills:
  free: { cost: local }
providers:
  one: { url: "http://localhost:1/v1", vendor: alpha, roles: [author] }
  two: { url: "http://localhost:2/v1", vendor: beta, roles: [reviewer] }
roles:
  author: [one]
  reviewer: [two]
"""

#: A valid registry with a disabled reviewer on loopback: the pack no longer
#: ships a disabled entry, so the cases that need one bring their own.
WITH_DISABLED = """\
bills:
  free: { cost: local }
providers:
  one: { url: "http://localhost:1/v1", model: m, vendor: alpha, bill: free,
         roles: [author], env: [] }
  two: { url: "http://localhost:2/v1", model: m, vendor: beta, bill: free,
         roles: [reviewer], env: [] }
  three: { url: "http://localhost:3/v1", model: m, vendor: gamma, bill: free,
           roles: [reviewer], enabled: false, reason: "model not pinned", env: [] }
roles:
  author: [one]
  reviewer: [two, three]
"""


def written(case: unittest.TestCase, text: str) -> pathlib.Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    with handle:
        handle.write(text)
    case.addCleanup(pathlib.Path(handle.name).unlink)  # sd:3032: the file outlived every run
    return pathlib.Path(handle.name)


class TheShippedRegistry(unittest.TestCase):
    """What the pack ships, read as a checkout with no database reads it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = sd_registry.read_file(SHIPPED)

    def test_it_is_at_the_root_of_the_checkout(self) -> None:
        self.assertTrue(SHIPPED.is_file(), f"no registry at {SHIPPED}")
        self.assertEqual(SHIPPED.name, sd_registry.REGISTRY_NAME)

    def test_the_two_roles_resolve_to_different_providers(self) -> None:
        """Criterion 6. A review by the author is not a review."""
        author = self.registry.resolve("author")
        reviewer = self.registry.resolve("reviewer")
        self.assertNotEqual(author.name, reviewer.name)
        self.assertNotEqual(author.vendor, reviewer.vendor)

    def test_only_the_local_clients_enter_the_default_reviewer_order(self) -> None:
        self.assertEqual(
            [provider.name for provider in self.registry.order("reviewer")],
            ["codex", "claude", "opencode"],
        )
        for name in ("minimax", "baseten"):
            with self.subTest(provider=name):
                provider = self.registry.providers[name]
                self.assertTrue(provider.enabled)
                self.assertIn("reviewer", provider.roles)
                self.assertNotIn("reviewer", provider.ranks)

    def test_only_kimi_opts_in_to_the_strict_response_format(self) -> None:
        """sd:1827 rulings: kimi-k3 holds its answer to the schema; MiniMax-M3
        ignores the field and Baseten is untested, so neither sends it."""
        self.assertEqual(
            {name: provider.response_format for name, provider in self.registry.providers.items()
             if provider.response_format is not None},
            {"kimi": "json_schema"},
        )

    #: Each shipped `url` reviewer's documented output ceiling, read 2026-09-27:
    #: kimi-k3 `max_completion_tokens` "can be set up to 1048576" (Kimi API
    #: platform, K3 quickstart); MiniMax-M3 counts input and output together
    #: against 1,000,000 (MiniMax API overview); Baseten's DeepSeek-V4-Pro-0813
    #: lists 262,144 max tokens (pi.dev model table for the Baseten entry).
    OUTPUT_CEILINGS = {"kimi-k3": 1_048_576, "MiniMax-M3": 1_000_000,
                       "deepseek-ai/DeepSeek-V4-Pro-0813": 262_144}
    #: The fewest tokens a reasoning reviewer gets (sd:1805). kimi-k3 spent all
    #: 16384 reasoning on a 35k-token prompt (example-simulator#215) and
    #: sent no answer; K3 always thinks, so the budget holds reasoning too.
    REASONING_FLOOR = 65536

    def test_every_url_reviewer_has_room_to_reason_and_answer(self) -> None:
        reviewers = [p for p in self.registry.providers.values() if p.url and "reviewer" in p.roles]
        self.assertEqual(sorted(p.model for p in reviewers), sorted(self.OUTPUT_CEILINGS))
        for provider in reviewers:
            with self.subTest(provider=provider.name):
                self.assertGreaterEqual(provider.max_tokens, self.REASONING_FLOOR)
                self.assertLessEqual(provider.max_tokens, self.OUTPUT_CEILINGS[provider.model])

    def test_every_entry_carries_a_vendor(self) -> None:
        """A review receipt names its reviewer's vendor, so every entry needs one."""
        for name, provider in self.registry.providers.items():
            with self.subTest(provider=name):
                self.assertTrue(provider.vendor, f"{name} has no vendor")

    def test_no_start_entry_sits_on_a_capped_bill(self) -> None:
        for name, provider in self.registry.providers.items():
            with self.subTest(provider=name):
                if self.registry.bills[provider.bill].capped:
                    self.assertEqual(provider.kind, "url")

    def test_a_disabled_entry_carries_its_reason_and_never_resolves(self) -> None:
        fixture = sd_registry.read_file(written(self, WITH_DISABLED))
        self.assertFalse(fixture.providers["three"].enabled, "the fixture lost its disabled entry")
        for registry in (self.registry, fixture):
            self.assert_disabled_entries_never_resolve(registry)

    def assert_disabled_entries_never_resolve(self, registry: sd_registry.Registry) -> None:
        disabled = [
            provider for provider in registry.providers.values()
            if not provider.enabled
        ]
        for provider in disabled:
            with self.subTest(provider=provider.name):
                self.assertTrue(provider.reason, "disabled with no reason")
        resolved = {
            provider.name
            for role in sd_registry.ROLES
            for provider in registry.order(role)
        }
        self.assertEqual(resolved & {provider.name for provider in disabled}, set())

    def test_the_registry_names_no_backend_the_review_lane_used_to_carry(self) -> None:
        """`prism` and `gito` pointed at the same endpoint and added limits of
        their own. The registry is the only list of providers, so their absence
        here is what removes them. `exo` was removed too (#1025); a re-added
        `enabled: false` row would leave the reviewer chain unchanged, so its
        absence is asserted here and not read off the chain (sd:1002)."""
        self.assertEqual(
            {"prism", "gito", "exo"} & set(self.registry.providers), set()
        )


class AddingAProviderIsAnEntry(unittest.TestCase):
    """Criterion 6: a new provider with an OpenAI-compatible URL needs no code
    change, which is a property of having no table of known names."""

    def test_an_unknown_name_with_a_url_resolves(self) -> None:
        path = written(
            self,
            MINIMAL.replace(
                "roles:\n  author: [one]\n  reviewer: [two]\n",
                "roles:\n  author: [one]\n  reviewer: [exo, two]\n",
            ).replace(
                '  two: { url: "http://localhost:2/v1", vendor: beta, roles: [reviewer] }\n',
                '  two: { url: "http://localhost:2/v1", vendor: beta, roles: [reviewer] }\n'
                '  exo: { url: "http://localhost:52415/v1", model: pinned-3b, '
                "vendor: local, bill: free, roles: [reviewer] }\n",
            ).replace(
                '  one: { url: "http://localhost:1/v1", vendor: alpha, roles: [author] }',
                '  one: { url: "http://localhost:1/v1", vendor: alpha, bill: free, roles: [author] }',
            ).replace(
                '  two: { url: "http://localhost:2/v1", vendor: beta, roles: [reviewer] }',
                '  two: { url: "http://localhost:2/v1", vendor: beta, bill: free, roles: [reviewer] }',
            )
        )
        registry = sd_registry.read_file(path)
        self.assertEqual(registry.resolve("reviewer").name, "exo")
        self.assertEqual(registry.providers["exo"].model, "pinned-3b")


class TheTwoReadersAgree(unittest.TestCase):
    """Criterion 32, and the only reason a second reader is allowed here."""

    def test_the_library_is_installed_so_the_comparison_means_something(self) -> None:
        self.assertIsNotNone(
            sd_registry.library(),
            "sd_db is not installed in this virtualenv, so the two readers "
            "cannot be compared and this file would pass on one of them. Run "
            "`make setup`, which provisions the library from the `system` "
            "checkout.",
        )

    def test_the_same_file_gives_the_same_reviewer_order(self) -> None:
        by_file = sd_registry.read(SHIPPED, prefer_library=False)
        by_library = sd_registry.read(SHIPPED, prefer_library=True)
        self.assertEqual(
            [provider.name for provider in by_file.order("reviewer")],
            [provider.name for provider in by_library.order("reviewer")],
        )

    def test_the_same_file_gives_the_same_entry_for_every_field(self) -> None:
        by_file = sd_registry.read(SHIPPED, prefer_library=False)
        by_library = sd_registry.read(SHIPPED, prefer_library=True)
        self.assertEqual(by_file.providers, by_library.providers)
        self.assertEqual(by_file.bills, by_library.bills)

    def test_the_file_only_reader_refuses_a_connection_rather_than_ignoring_it(
        self,
    ) -> None:
        """A caller holding a connection and getting file-only answers would be
        reading a registry the dashboard has already changed."""
        with self.assertRaises(sd_registry.RegistryError) as caught:
            sd_registry.read(SHIPPED, connection=object(), prefer_library=False)
        self.assertIn("sd_db is not installed", str(caught.exception))

    def test_database_membership_keeps_explicit_reviewers_and_supports_rollback(self) -> None:
        from sd_db import connect, provider_controls  # noqa: PLC0415
        from sd_db.migrate import initialise  # noqa: PLC0415
        from sd_db.workflow import StaleItem  # noqa: PLC0415

        original = SHIPPED.read_text()
        legacy = original.replace("reviewer: [codex, claude, opencode]",
            "reviewer: [codex, claude, opencode, minimax, baseten]")
        self.assertNotEqual(legacy, original)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "providers.yaml"
            path.write_text(legacy)
            database = pathlib.Path(directory) / "sd.db"
            initialise(database)
            with closing(connect(database)) as connection:
                baseline = provider_controls.snapshot(connection, path=path)
                enabled = {entry["name"]: entry["enabled"] for entry in baseline["providers"]}
                orders = {**baseline["orders"], "reviewer": ["codex", "claude", "opencode"]}
                changed = provider_controls.configure(connection, enabled=enabled, orders=orders,
                    expected_revision=baseline["revision"], path=path, who="fixture")
                adapted = sd_registry.read(path, connection=connection)
                consent, _ = sd_registry.resolve_consent(adapted, None, "configured")
                # Both openers: a loopback entry takes `_DIRECT_OPENER`, so
                # guarding only `_OPENER` would stop catching one.
                with (unittest.mock.patch.object(sd_registry._OPENER, "open", side_effect=AssertionError("no provider calls")),
                      unittest.mock.patch.object(sd_registry._DIRECT_OPENER, "open", side_effect=AssertionError("no provider calls"))):
                    self.assertEqual([entry.name for entry in sd_registry.select_reviewers(adapted, consent=consent)],
                        ["codex", "claude", "opencode"])
                    for name in ("minimax", "baseten"):
                        with self.subTest(provider=name):
                            self.assertTrue(adapted.providers[name].enabled)
                            self.assertNotIn("reviewer", adapted.providers[name].ranks)
                            self.assertEqual(sd_registry.select_reviewers(adapted, name, consent=consent)[0].name, name)
                self.assertEqual(changed["bills"], baseline["bills"])
                self.assertEqual(path.read_text(), legacy)
                before = tuple(connection.iterdump())
                with self.assertRaises(StaleItem):
                    provider_controls.configure(connection, enabled=enabled, orders=baseline["orders"],
                        expected_revision=baseline["revision"], path=path, who="fixture")
                self.assertEqual(tuple(connection.iterdump()), before)
                fresh = provider_controls.snapshot(connection, path=path)
                restored = provider_controls.configure(connection, enabled=enabled, orders=baseline["orders"],
                    expected_revision=fresh["revision"], path=path, who="fixture")
                self.assertEqual(restored["providers"], baseline["providers"])
                self.assertEqual([entry.name for entry in sd_registry.read(path, connection=connection).order("reviewer")],
                    ["codex", "claude", "opencode", "minimax", "baseten"])


class TheReasoningControls(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "providers.yaml"
        self.text = MINIMAL.replace("vendor: alpha,", "vendor: alpha, bill: free,").replace(
            "vendor: beta,", "vendor: beta, bill: free, model: exact-model, max_tokens: 16384,")

    def write(self, **controls: Any) -> pathlib.Path:
        fields = "".join(f", {name}: {json.dumps(value)}" for name, value in controls.items())
        self.path.write_text(self.text.replace("roles: [reviewer] }", "roles: [reviewer]" + fields + " }"))
        return self.path

    def test_both_readers_preserve_every_explicit_control(self) -> None:
        for name, values in (("thinking", ("disabled", "adaptive")),
                             ("reasoning_effort", ("none", "low", "high", "max"))):
            for value in values:
                with self.subTest(name=name, value=value):
                    path = self.write(**{name: value})
                    standalone = sd_registry.read(path, prefer_library=False)
                    shared = sd_registry.read(path)
                    self.assertEqual(standalone.providers, shared.providers)
                    self.assertEqual(getattr(shared.providers["two"], name), value)

    def test_invalid_or_conflicting_controls_refuse_in_both_readers(self) -> None:
        cases = ({"thinking": value} for value in (True, 1, [], {}, "enabled"))
        cases = [*cases, *({"reasoning_effort": value} for value in (False, 1, [], {}, "medium")),
                 {"thinking": "disabled", "reasoning_effort": "none"}]
        for controls in cases:
            for prefer in (True, False):
                with self.subTest(controls=controls, prefer_library=prefer):
                    with self.assertRaises(sd_registry.RegistryError):
                        sd_registry.read(self.write(**controls), prefer_library=prefer)

    def test_process_entries_cannot_declare_url_controls(self) -> None:
        self.text = self.text.replace('url: "http://localhost:2/v1"', 'start: "two -p", reader: claude-json')
        for name, value in (("thinking", "disabled"), ("reasoning_effort", "none")):
            for prefer in (True, False):
                with self.assertRaises(sd_registry.RegistryError):
                    sd_registry.read(self.write(**{name: value}), prefer_library=prefer)

    def test_old_library_remains_compatible_without_controls_but_cannot_drop_them(self) -> None:
        source = sd_registry.read_file(self.write())
        old = types.SimpleNamespace(**{**vars(source), "providers": {
            name: types.SimpleNamespace(**{key: value for key, value in vars(provider).items()
                                          if key not in ("thinking", "reasoning_effort")})
            for name, provider in source.providers.items()}})
        module = types.SimpleNamespace(read=lambda *args, **kwargs: old, RegistryError=sd_registry.RegistryError)
        with unittest.mock.patch.object(sd_registry, "library", return_value=module):
            self.assertEqual(sd_registry.read(self.path).providers, source.providers)
            for name, value in (("thinking", "disabled"), ("reasoning_effort", "none")):
                with self.assertRaisesRegex(sd_registry.RegistryError, "cannot preserve reasoning controls"):
                    sd_registry.read(self.write(**{name: value}))

    def test_request_options_change_only_the_declared_top_level_fields(self) -> None:
        prompt = "full diff\n" + "unchanged subject " * 10000
        for controls, additions in (({}, {}), ({"thinking": "disabled"}, {"thinking": {"type": "disabled"}}),
                                    ({"reasoning_effort": "none"}, {"reasoning_effort": "none"})):
            provider = sd_registry.read(self.write(**controls)).providers["two"]
            provider = sd_registry.Provider(**{**vars(provider), "env": ("OWN_KEY",)})
            # The fixture url is `http://localhost:2/v1`, which routes past
            # any configured proxy and so through `_DIRECT_OPENER`.
            with unittest.mock.patch.object(sd_registry._DIRECT_OPENER, "open", return_value=_Answer("{}")) as opened:
                sd_registry.chat_completion(provider, prompt, {"OWN_KEY": "fixture"}, 30)
            request = opened.call_args.args[0]
            self.assertEqual(json.loads(request.data), {"model": "exact-model", "max_tokens": 16384,
                "messages": [{"role": "user", "content": prompt}], **additions})
            self.assertEqual(request.full_url, "http://localhost:2/v1/chat/completions")
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture")


class TheStrictResponseFormat(unittest.TestCase):
    """sd:1827: `response_format: json_schema` is a per-entry opt-in; the file
    reader holds it, and only an entry that opted in sends a schema."""

    setUp = TheReasoningControls.setUp
    write = TheReasoningControls.write
    SCHEMA = {"type": "object", "required": ["items"], "properties": {
        "items": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {
            "line": {"type": ["integer", "null"]}, "level": {"enum": ["high", "low"]},
            "minLength": {"type": "string", "minLength": 1}}}}}}

    def provider(self, **fields: Any) -> sd_registry.Provider:
        provider = sd_registry.read(self.write(**fields), prefer_library=False).providers["two"]
        return sd_registry.Provider(**{**vars(provider), "env": ("OWN_KEY",)})

    def sent(self, provider: sd_registry.Provider, **options: Any) -> dict[str, Any]:
        with unittest.mock.patch.object(sd_registry._DIRECT_OPENER, "open", return_value=_Answer("{}")) as opened:
            sd_registry.chat_completion(provider, "prompt", {"OWN_KEY": "fixture"}, 30, **options)
        return json.loads(opened.call_args.args[0].data)

    def test_the_file_reader_preserves_the_opt_in(self) -> None:
        self.assertEqual(self.provider(response_format="json_schema").response_format, "json_schema")
        self.assertIsNone(self.provider().response_format)

    def test_a_value_other_than_json_schema_or_a_process_entry_refuses(self) -> None:
        for value in ("json_object", True, 1, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(sd_registry.RegistryError, "response_format in"):
                sd_registry.read(self.write(response_format=value), prefer_library=False)
        self.text = self.text.replace('url: "http://localhost:2/v1"', 'start: "two -p", reader: claude-json')
        with self.assertRaisesRegex(sd_registry.RegistryError, "needs a URL"):
            sd_registry.read(self.write(response_format="json_schema"), prefer_library=False)

    def test_a_library_without_the_field_cannot_drop_the_opt_in(self) -> None:
        source = sd_registry.read_file(self.write())
        old = types.SimpleNamespace(**{**vars(source), "providers": {
            name: types.SimpleNamespace(**{key: value for key, value in vars(provider).items() if key != "response_format"})
            for name, provider in source.providers.items()}})
        module = types.SimpleNamespace(read=lambda *args, **kwargs: old, RegistryError=sd_registry.RegistryError)
        with unittest.mock.patch.object(sd_registry, "library", return_value=module):
            self.assertEqual(sd_registry.read(self.path).providers, source.providers)
            with self.assertRaisesRegex(sd_registry.RegistryError, "cannot preserve response_format"):
                sd_registry.read(self.write(response_format="json_schema"))

    def test_the_strict_copy_is_typed_has_no_type_arrays_and_drops_what_strict_mode_rejects(self) -> None:
        before = json.dumps(self.SCHEMA)
        strict = sd_registry.strict_schema(self.SCHEMA)
        self.assertEqual(json.dumps(self.SCHEMA), before)
        self.assertEqual(sd_registry.strict_schema(strict), strict)
        items = strict["properties"]["items"]
        self.assertNotIn("maxItems", items)
        self.assertEqual(items["items"]["properties"], {
            "line": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "level": {"type": "string", "enum": ["high", "low"]},
            "minLength": {"type": "string"}})

    def test_an_opted_in_entry_sends_the_strict_body(self) -> None:
        body = self.sent(self.provider(response_format="json_schema"), response_schema=self.SCHEMA, schema_name="items")
        self.assertEqual(body.pop("response_format"), {"type": "json_schema", "json_schema": {
            "name": "items", "strict": True, "schema": sd_registry.strict_schema(self.SCHEMA)}})
        self.assertEqual(set(body), {"model", "max_tokens", "messages"})

    def test_any_other_entry_sends_exactly_todays_body(self) -> None:
        today = {"model": "exact-model", "max_tokens": 16384, "messages": [{"role": "user", "content": "prompt"}]}
        self.assertEqual(self.sent(self.provider(), response_schema=self.SCHEMA), today)
        self.assertEqual(self.sent(self.provider(response_format="json_schema")), today)


class TheRefusals(unittest.TestCase):
    """A registry that cannot mean anything never reaches a caller."""

    def refuse(self, text: str) -> str:
        with self.assertRaises(sd_registry.RegistryError) as caught:
            sd_registry.parse(text, "fixture.yaml")
        return str(caught.exception)

    def test_a_provider_with_both_start_and_url(self) -> None:
        message = self.refuse(
            MINIMAL.replace(
                '{ url: "http://localhost:1/v1", vendor: alpha, roles: [author] }',
                '{ url: "http://localhost:1/v1", start: "one -p", vendor: alpha, '
                "bill: free, roles: [author] }",
            )
        )
        self.assertIn("both", message)

    def test_a_provider_with_neither(self) -> None:
        message = self.refuse(
            MINIMAL.replace(
                '{ url: "http://localhost:1/v1", vendor: alpha, roles: [author] }',
                "{ vendor: alpha, bill: free, roles: [author] }",
            )
        )
        self.assertIn("neither", message)

    def test_a_start_entry_on_a_capped_bill(self) -> None:
        message = self.refuse(
            MINIMAL.replace("free: { cost: local }", "free: { cost: company }").replace(
                '{ url: "http://localhost:1/v1", vendor: alpha, roles: [author] }',
                '{ start: "one -p", vendor: alpha, bill: free, roles: [author] }',
            )
        )
        self.assertIn("nothing enforces", message)

    def test_a_bill_the_bills_section_does_not_name(self) -> None:
        message = self.refuse(
            MINIMAL.replace(
                '{ url: "http://localhost:1/v1", vendor: alpha, roles: [author] }',
                '{ url: "http://localhost:1/v1", vendor: alpha, bill: nosuch, '
                "roles: [author] }",
            )
        )
        self.assertIn("nosuch", message)

    def test_a_role_list_naming_a_provider_that_does_not_exist(self) -> None:
        message = self.refuse(self.filled().replace("reviewer: [two]", "reviewer: [two, three]"))
        self.assertIn("three", message)

    def test_a_role_list_naming_an_entry_that_does_not_declare_the_role(self) -> None:
        message = self.refuse(self.filled().replace("author: [one]", "author: [one, two]"))
        self.assertIn("may not add one", message)

    def test_one_provider_first_in_both_lists(self) -> None:
        message = self.refuse(
            self.filled()
            .replace("roles: [author]", "roles: [author, reviewer]")
            .replace("reviewer: [two]", "reviewer: [one, two]")
        )
        self.assertIn("a review would be by the author", message)

    def test_a_missing_section(self) -> None:
        # Split on the section header, not on `roles:`, which also opens each
        # entry's own role list -- cutting there truncates a flow value and
        # the refusal that follows is about the brace, not the section.
        without = self.filled().split("\nroles:\n")[0] + "\n"
        self.assertIn("'roles'", self.refuse(without))

    def test_a_tab(self) -> None:
        self.assertIn("a tab", self.refuse(self.filled().replace("  free:", "\tfree:")))

    def test_a_flow_value_that_never_closes(self) -> None:
        self.assertIn("never closed", self.refuse(self.filled().replace("cost: local }", "cost: local")))

    def test_a_role_with_no_list_at_all(self) -> None:
        """Copilot found this. The list is the order, so a section without one
        left every entry declaring that role holding it in name only: the file
        said `a` reviews, and the registry answered that nothing does."""
        message = self.refuse(self.filled().replace("  reviewer: [two]\n", ""))
        self.assertIn("no 'reviewer' list", message)

    def test_a_role_list_naming_the_same_entry_twice(self) -> None:
        message = self.refuse(self.filled().replace("reviewer: [two]", "reviewer: [two, two]"))
        self.assertIn("more than once", message)

    def test_an_empty_list_is_how_nobody_is_written(self) -> None:
        """Refusing the missing list is only fair if there is a way to say it
        on purpose."""
        registry = sd_registry.parse(
            self.filled().replace("reviewer: [two]", "reviewer: []"), "fixture.yaml"
        )
        self.assertEqual(list(registry.order("reviewer")), [])

    def test_an_unknown_role(self) -> None:
        self.assertIn("'auditor'", self.refuse(self.filled() + "  auditor: [two]\n"))

    def test_a_second_section_of_the_same_name(self) -> None:
        message = self.refuse(self.filled() + "roles:\n  reviewer: [one]\n")
        self.assertIn("second 'roles' section", message)

    def test_a_second_entry_of_the_same_name(self) -> None:
        """The later one won and the file did not say so. An operator reading
        the first entry -- its vendor, its bill, whether it is on at all --
        was reading something the code had already discarded."""
        message = self.refuse(
            self.filled().replace(
                "roles:\n",
                '  one: { url: "http://localhost:9/v1", vendor: omega, '
                "bill: free, roles: [author] }\nroles:\n",
                1,
            )
        )
        self.assertIn("second 'one' entry", message)

    def test_a_key_twice_in_one_flow_mapping(self) -> None:
        message = self.refuse(
            self.filled().replace("vendor: beta,", "vendor: beta, vendor: gamma,")
        )
        self.assertIn("'vendor' twice", message)

    def test_enabled_written_as_anything_but_true_or_false(self) -> None:
        """`no`, `off` and a quoted `"false"` are strings in this subset, and
        `bool()` of any non-empty string is true. An entry turned off with one
        of them stayed on, and stayed on the reviewer chain, which is the one
        place a silent misread sends the repository's diff somewhere the
        operator had already said no to."""
        for literal in ("no", "off", '"false"', "nope"):
            with self.subTest(enabled=literal):
                message = self.refuse(
                    self.filled().replace(
                        "vendor: beta,", f"enabled: {literal}, vendor: beta,"
                    )
                )
                self.assertIn("not true or false", message)

    def test_enabled_written_true_or_false_is_read(self) -> None:
        for literal, expected in (("true", True), ("false", False)):
            with self.subTest(enabled=literal):
                registry = sd_registry.parse(
                    self.filled().replace(
                        "vendor: beta,", f"enabled: {literal}, vendor: beta,"
                    ),
                    "fixture.yaml",
                )
                self.assertIs(registry.providers["two"].enabled, expected)

    def test_a_field_of_the_wrong_shape_is_named_and_refused(self) -> None:
        """Copilot's sixth pass. Each of these was read straight into a
        `Provider` and failed later, or did not fail at all. `env` given a
        bare name walked the string: fourteen single-character variable
        names, which the fingerprint then covered and the environment check
        then looked for -- so consent was granted over a list nobody wrote."""
        for edit, expected in (
            (", env: OPENAI_API_KEY", "'env' of provider 'two'"),
            (", price: [1, 2]", "'price' of provider 'two'"),
            (", max_tokens: many", "'max_tokens' of provider 'two'"),
            (", reader: 7", "'reader' of provider 'two'"),
        ):
            with self.subTest(field=edit):
                message = self.refuse(
                    self.filled().replace(
                        "roles: [reviewer] }", "roles: [reviewer]" + edit + " }"
                    )
                )
                self.assertIn(expected, message)

    def test_a_role_list_written_as_one_name(self) -> None:
        """It walked the string too, and refused for a reason that named five
        single letters instead of the shape it was given."""
        message = self.refuse(self.filled().replace("roles: [reviewer]", "roles: reviewer"))
        self.assertIn("'roles' of provider 'two'", message)

    def test_a_bill_amount_that_is_not_one(self) -> None:
        message = self.refuse(
            self.filled().replace("cost: local }", "cost: local, cap_usd_month: lots }")
        )
        self.assertIn("'cap_usd_month' of bill 'free'", message)

    def test_a_start_line_whose_first_word_is_a_flag(self) -> None:
        """Copilot found this. An earlier round refused the empty start line
        because `shlex.split("")` left the hardened invocation's first flag as
        the executable. A line that opens with a flag reaches the same place
        by a shorter road, and passed the guard: `executable('--sandbox exec')`
        is `'--sandbox'`, which is truthy, so the run tried to execute it."""
        for line in ('"--sandbox exec"', '"-p review"'):
            with self.subTest(start=line):
                message = self.refuse(
                    self.filled().replace(
                        '{ url: "http://localhost:2/v1", vendor: beta,',
                        "{ start: " + line + ", reader: codex-json, vendor: beta,",
                    )
                )
                self.assertIn("is a flag", message)

    def test_a_quoted_path_with_a_space_is_still_a_program(self) -> None:
        """The refusal above must not catch the case the shlex split exists
        for."""
        registry = sd_registry.parse(
            self.filled().replace(
                '{ url: "http://localhost:2/v1", vendor: beta,',
                "{ start: \"'/opt/my tools/codex' exec\", reader: codex-json, vendor: beta,",
            ),
            "fixture.yaml",
        )
        self.assertEqual(
            sd_registry.executable(registry.providers["two"].start or ""),
            "/opt/my tools/codex",
        )

    def test_a_start_entry_that_says_nothing_about_reading_it_back(self) -> None:
        """Running one means parsing what it prints. An entry that does not
        say how could be picked, occupy a slot in a tier's depth, and never
        be read."""
        message = self.refuse(
            self.filled().replace(
                '{ url: "http://localhost:2/v1", vendor: beta,',
                '{ start: "two review", vendor: beta,',
            )
        )
        self.assertIn("no 'reader'", message)

    def test_a_url_that_names_no_host(self) -> None:
        message = self.refuse(
            self.filled().replace('"http://localhost:1/v1"', '"file:///tmp/x"')
        )
        self.assertIn("names no host", message)

    def test_a_vendor_with_padding_or_a_capital(self) -> None:
        """The other half of the same exact match. A registry vendor that is
        not already in the form a trailer is folded into could never match
        one, and the entry would review work its own vendor wrote."""
        for value in ('" beta"', "Beta", '"beta "'):
            with self.subTest(vendor=value):
                message = self.refuse(
                    self.filled().replace("vendor: beta,", f"vendor: {value},")
                )
                self.assertIn("lower case", message)

    def filled(self) -> str:
        """`MINIMAL` with the bill each entry needs, so a test that is not
        about the bill does not fail on it."""
        return MINIMAL.replace("vendor: alpha,", "vendor: alpha, bill: free,").replace(
            "vendor: beta,", "vendor: beta, bill: free,"
        )


class TheBoundsInputs(unittest.TestCase):
    """sd:788 slice 3a. A `url` entry on a capped bill is refused at read
    when the ledger could not compute its bound: `price.in`, `price.out` or
    `max_tokens` missing, or a value the library cannot price. The predicate
    is `sd_db.calls`'s -- a price is a finite non-negative number,
    `max_tokens` a whole number above zero -- and the message is this
    reader's own, naming the entry, the key and the value."""

    PRICED = 'two: { url: "https://two.example/v1", vendor: beta, bill: paid, roles: [reviewer], max_tokens: 100, price: { in: 1.5, out: 2 } }'

    def registry(self, entry: str, bill: str = "paid: { cost: company, cap_usd_month: 50 }") -> str:
        return (
            f"bills:\n  free: {{ cost: local }}\n  {bill}\nproviders:\n"
            '  one: { url: "http://localhost:1/v1", vendor: alpha, bill: free, roles: [author] }\n'
            f"  {entry}\nroles:\n  author: [one]\n  reviewer: [two]\n"
        )

    def refuse(self, entry: str) -> str:
        with self.assertRaises(sd_registry.RegistryError) as caught:
            sd_registry.parse(self.registry(entry), "fixture.yaml")
        return str(caught.exception)

    def test_a_priced_entry_on_a_capped_bill_reads(self) -> None:
        registry = sd_registry.parse(self.registry(self.PRICED), "fixture.yaml")
        self.assertEqual(registry.providers["two"].price, {"in": 1.5, "out": 2})

    def test_the_same_entry_on_an_uncapped_bill_needs_no_price(self) -> None:
        bare = self.PRICED.replace(", max_tokens: 100, price: { in: 1.5, out: 2 }", "")
        registry = sd_registry.parse(self.registry(bare, "paid: { cost: subscription }"), "fixture.yaml")
        self.assertEqual(registry.providers["two"].price, {})

    def test_a_missing_key_is_refused_naming_the_entry_the_key_and_the_value(self) -> None:
        cases = {
            "price.in": self.PRICED.replace("in: 1.5, ", ""),
            "price.out": self.PRICED.replace(", out: 2", ""),
            "max_tokens": self.PRICED.replace("max_tokens: 100, ", ""),
        }
        for key, entry in cases.items():
            with self.subTest(key=key):
                message = self.refuse(entry)
                self.assertIn("provider 'two' is a 'url' entry on the capped bill 'paid'", message)
                self.assertIn(f"{key} is None", message)

    def test_a_value_the_library_cannot_price_is_refused_naming_it(self) -> None:
        cases = {
            ("price.in", "'1.5'"): self.PRICED.replace("in: 1.5", 'in: "1.5"'),
            ("price.out", "nan"): self.PRICED.replace("out: 2", "out: nan"),
            ("price.out", "inf"): self.PRICED.replace("out: 2", "out: inf"),
            ("price.in", "-1"): self.PRICED.replace("in: 1.5", "in: -1"),
            ("price.in", "True"): self.PRICED.replace("in: 1.5", "in: true"),
            ("max_tokens", "0"): self.PRICED.replace("max_tokens: 100", "max_tokens: 0"),
            ("max_tokens", "-5"): self.PRICED.replace("max_tokens: 100", "max_tokens: -5"),
        }
        for (key, shown), entry in cases.items():
            with self.subTest(key=key, value=shown):
                self.assertIn(f"{key} is {shown}", self.refuse(entry))

    def test_a_capped_cost_basis_without_a_number_is_capped_too(self) -> None:
        """`prepaid` and `plan` are capped bills by basis; the library refuses
        the unpriced entry on them, so this reader does the same."""
        for basis in ("prepaid", "plan", "company"):
            with self.subTest(basis=basis):
                with self.assertRaises(sd_registry.RegistryError) as caught:
                    sd_registry.parse(self.registry(self.PRICED.replace("max_tokens: 100, ", ""),
                                                    f"paid: {{ cost: {basis} }}"), "fixture.yaml")
                self.assertIn("max_tokens is None", str(caught.exception))

    def test_the_merged_read_refuses_a_cap_the_rows_carry(self) -> None:
        """`_adapt` builds providers from `sd_db`'s registry, which merges a
        cap the dashboard set over a bill the file left uncapped, so the same
        refusal has to fire there or a capped bill reads two ways."""
        library = sd_registry.library()
        self.assertIsNotNone(library, "sd_db is not installed; run `make setup`")
        merged = library.Registry(
            path=pathlib.Path("merged.yaml"),
            bills={"paid": library.Bill(name="paid", cost_basis="subscription", cap_usd_month=50.0)},
            providers={"two": library.Provider(name="two", vendor="beta", bill="paid",
                                               url="https://two.example/v1", max_tokens=100,
                                               price={"in": float("nan"), "out": 2.0})},
        )
        with self.assertRaises(sd_registry.RegistryError) as caught:
            sd_registry._adapt(merged)
        self.assertIn("merged.yaml: provider 'two' is a 'url' entry on the capped bill 'paid' and price.in is nan", str(caught.exception))
        merged.bills["paid"] = library.Bill(name="paid", cost_basis="subscription")
        self.assertEqual(sd_registry._adapt(merged).providers["two"].max_tokens, 100)


class TheHomeItLivesIn(unittest.TestCase):
    def test_the_registry_sits_beside_the_database(self) -> None:
        path = sd_registry.registry_path("/tmp/somewhere")
        self.assertEqual(
            path, pathlib.Path("/tmp/somewhere/.local/share/sd/providers.yaml")
        )

    def test_the_environment_home_is_honoured_when_none_is_given(self) -> None:
        path = sd_registry.registry_path(environ={"HOME": "/tmp/elsewhere"})
        self.assertEqual(
            path, pathlib.Path("/tmp/elsewhere/.local/share/sd/providers.yaml")
        )

    def test_a_missing_file_is_refused_by_path(self) -> None:
        with self.assertRaises(sd_registry.RegistryError) as caught:
            sd_registry.read_file("/tmp/no/such/providers.yaml")
        self.assertIn("/tmp/no/such/providers.yaml", str(caught.exception))


class TheConsentLine(unittest.TestCase):
    """`CLAUDE.local.md`'s `reviewers` key: who may receive this diff.

    The registry says who can review. This says who may be sent the change,
    from this repository, on this machine. Nothing derives it.
    """

    def test_no_line_at_all_refuses_and_names_the_key(self) -> None:
        with self.assertRaises(sd_registry.ConsentRefusal) as caught:
            sd_registry.parse_consent(None)
        self.assertIn("reviewers", str(caught.exception))

    def test_a_bare_name_is_refused_naming_the_form(self) -> None:
        """An entry is a name for a destination, not the destination."""
        with self.assertRaises(sd_registry.ConsentRefusal) as caught:
            sd_registry.parse_consent("codex, claude@claude")
        message = str(caught.exception)
        self.assertIn("bare name", message)
        self.assertIn("entry@host", message.replace("`", ""))

    def refuse_consent(self, line: str) -> str:
        with self.assertRaises(sd_registry.ConsentRefusal) as caught:
            sd_registry.parse_consent(line)
        return str(caught.exception)

    def test_the_same_entry_twice_is_refused_naming_both(self) -> None:
        """Copilot found this. The later pair silently won, so a line an
        operator wrote to allow one destination allowed a different one, and
        the discarded half left no trace. This is the consent boundary, which
        is the worst place in the pack for a quiet answer."""
        message = self.refuse_consent("codex@one, codex@two")
        self.assertIn("twice", message)
        self.assertIn("'one'", message)
        self.assertIn("'two'", message)

    def test_half_a_pair_names_neither_side(self) -> None:
        self.assertIn("empty entry", self.refuse_consent("@host"))
        self.assertIn("empty recipient", self.refuse_consent("codex@"))

    def test_a_recipient_with_a_space_is_quoted_and_reads_back(self) -> None:
        """Copilot's sixth pass, and a contradiction between two earlier
        fixes. `executable()` was made shlex-aware so a quoted path with a
        space consents to the program that actually runs; `parse_consent`
        still split on whitespace, so the one line that could name such an
        entry was unreadable -- and was refused as a bare name, which it was
        not. The two halves of consent have to agree where a word ends."""
        provider = sd_registry.Provider(
            name="codex", vendor="v", bill="b", start='"/opt/my tools/codex" exec'
        )
        want = sd_registry.recipient(provider)
        line = str(want)
        self.assertIn("'/opt/my tools/codex'", line)
        allowed = sd_registry.parse_consent(line)["codex"]
        self.assertEqual(allowed, want)
        self.assertIsNone(sd_registry.refuse_allowance(provider, allowed))

    def test_a_recipient_holding_a_hash_keeps_it(self) -> None:
        """`shlex` treats `#` as a comment, which the quote-aware split
        inherited: `codex@codex#x` silently consented to `codex`. Nothing on
        this line is a comment, and a truncated recipient is consent to a
        destination the operator did not write."""
        allowed = sd_registry.parse_consent("codex@codex#x")
        self.assertEqual(allowed["codex"].recipient, "codex#x")

    def test_a_recipient_carrying_its_own_separator_is_one_pair(self) -> None:
        """A url entry's recipient is its netloc, userinfo included. The first
        separator splits and only the first, so this is well defined -- an
        earlier version of the duplicate check refused it as ambiguous and made
        such an entry impossible to consent to."""
        allowed = sd_registry.parse_consent("codex@user:pw@host.example")
        self.assertEqual(allowed["codex"].recipient, "user:pw@host.example")

    def test_commas_and_spaces_separate_the_same_way(self) -> None:
        self.assertEqual(
            sd_registry.parse_consent("a@one, b@two"),
            sd_registry.parse_consent("a@one b@two"),
        )

    def test_a_start_entry_carries_its_executable_and_a_fingerprint(self) -> None:
        registry = sd_registry.read_file(SHIPPED)
        allowance = sd_registry.recipient(registry.providers["codex"])
        self.assertEqual(allowance.recipient, "codex")
        self.assertEqual(
            len(allowance.fingerprint), sd_registry.FINGERPRINT_LENGTH
        )

    def test_the_executable_is_split_the_way_the_runner_splits_it(self) -> None:
        """Copilot found this on the pull request. `str.split` on a quoted
        path consents to `"/opt/my` while `bin/sd-review`, which builds its
        argv with `shlex`, runs `/opt/my tools/codex`: consent to a string
        nobody executes, and an executable nobody consented to."""

        quoted = sd_registry.Provider(
            name="wrapped", vendor="v", bill="b", start='"/opt/my tools/codex" exec'
        )
        self.assertEqual(
            sd_registry.recipient(quoted).recipient, "/opt/my tools/codex"
        )
        # The property that has to hold, stated as one: whatever the reviewer
        # is consented to is the program the review lane starts.
        self.assertEqual(
            sd_registry.executable(quoted.start or ""),
            shlex.split(quoted.start or "")[0],
        )

    def test_an_unbalanced_quote_names_no_executable(self) -> None:
        """It fails the consent comparison rather than raising, so listing a
        registry that holds one still works."""

        self.assertEqual(sd_registry.executable('"oops exec'), "")

    def test_a_url_entry_carries_its_host(self) -> None:
        registry = sd_registry.read_file(SHIPPED)
        allowance = sd_registry.recipient(registry.providers["baseten"])
        self.assertEqual(allowance.recipient, "inference.baseten.co")
        self.assertIsNone(allowance.fingerprint)


class WhatConsentRefuses(unittest.TestCase):
    """Each refusal names both values, because the operator has to choose."""

    def setUp(self) -> None:
        self.registry = sd_registry.read_file(SHIPPED)

    def refusal(self, name: str, line: str) -> str:
        allowed = sd_registry.parse_consent(line).get(name)
        message = sd_registry.refuse_allowance(self.registry.providers[name], allowed)
        self.assertIsNotNone(message, f"{name} was allowed and should not be")
        return message or ""

    def test_an_entry_the_line_does_not_name(self) -> None:
        message = self.refusal("kimi", "codex@codex")
        self.assertIn("not allowed by the effective review authorization", message)

    def test_a_url_host_edited_to_another_host_names_both(self) -> None:
        message = self.refusal("baseten", "baseten@inference.baseten.co.evil")
        self.assertIn("inference.baseten.co.evil", message)
        self.assertIn("inference.baseten.co'", message)

    def test_a_start_executable_edited_to_another_names_both(self) -> None:
        message = self.refusal("codex", "codex@codex-wrapper")
        self.assertIn("codex-wrapper", message)
        self.assertIn("executable", message)

    def test_an_argument_added_with_the_executable_unchanged(self) -> None:
        """The fingerprint is what catches this; the executable did not move."""
        allowed = sd_registry.parse_consent("codex@codex+00000000")["codex"]
        message = sd_registry.refuse_allowance(self.registry.providers["codex"], allowed)
        self.assertIn("fingerprint", message or "")
        self.assertIn("same one", message or "")

    def test_a_second_entry_for_an_allowed_vendor_is_still_refused(self) -> None:
        """`baseten` allowed does not allow `baseten-openrouter`.

        The line names entries because the entry is the recipient. A second
        entry pointing the same model at another host is another recipient.
        """
        second = sd_registry.Provider(
            name="baseten-openrouter",
            vendor="deepseek",
            bill="local",
            url="https://openrouter.ai/api/v1",
        )
        message = sd_registry.refuse_allowance(second, None)
        self.assertIn("baseten-openrouter", message or "")

    def test_a_line_that_matches_allows_the_entry(self) -> None:
        allowed = sd_registry.recipient(self.registry.providers["baseten"])
        self.assertIsNone(
            sd_registry.refuse_allowance(self.registry.providers["baseten"], allowed)
        )

    def test_a_fingerprint_the_line_omits_is_not_required(self) -> None:
        """The shorter form is the weaker statement, and is allowed to be."""
        allowed = sd_registry.parse_consent("codex@codex")["codex"]
        self.assertIsNone(
            sd_registry.refuse_allowance(self.registry.providers["codex"], allowed)
        )

    def test_a_variable_whose_value_is_a_url_refuses_the_session(self) -> None:
        message = sd_registry.refuse_environment(
            self.registry.providers["codex"],
            {"OPENAI_API_KEY": "https://collector.example/ingest"},
        )
        self.assertIn("OPENAI_API_KEY", message or "")
        self.assertIn("No session was started", message or "")

    def test_the_refusal_prints_no_value(self) -> None:
        """The value is the destination; printing it logs what was refused."""
        secret = "https://collector.example/ingest"
        message = sd_registry.refuse_environment(
            self.registry.providers["codex"], {"OPENAI_API_KEY": secret}
        )
        self.assertNotIn(secret, message or "")

    def test_an_ordinary_key_is_not_a_url_and_passes(self) -> None:
        self.assertIsNone(
            sd_registry.refuse_environment(
                self.registry.providers["codex"], {"OPENAI_API_KEY": "sk-abc"}
            )
        )


class WhatAnEntryWaitsOn(unittest.TestCase):
    """Copilot's seventh pass. Two situations shared one sentence, and the
    sentence described the rarer one.

    Four of the five entries on the shipped reviewer chain are `url` entries.
    They declare no reader, and correctly so -- a reader parses what a spawned
    command prints, and they spawn nothing. Reporting each as reading "back as
    None, and this build implements no such reader" named a typo nobody had
    made, on the output whose whole job is to say why an entry was passed
    over.
    """

    def chain(self, text: str, line: str) -> dict[str, str]:
        registry = sd_registry.parse(text, "fixture.yaml")
        return {
            candidate.provider.name: candidate.reason
            for candidate in sd_registry.reviewer_chain(
                registry,
                consent=sd_registry.parse_consent(line),
                readers=("codex-json",),
            )
        }

    def test_a_url_entry_names_no_reader_and_is_refused_for_none(self) -> None:
        """The client landed, so the sentence this class was written about is
        gone. What must not come back is the other half of the same defect: a
        `url` entry declares no reader, and asking `readers` about `None` would
        refuse every one of them for a name nobody wrote."""
        reasons = self.chain(
            MINIMAL.replace("vendor: alpha,", "vendor: alpha, bill: free,").replace(
                "vendor: beta,", "vendor: beta, bill: free,"
            ),
            "two@localhost:2",
        )
        self.assertEqual(reasons["two"], "")

    def test_a_start_entry_naming_another_build_s_reader_still_says_that(self) -> None:
        text = (
            MINIMAL.replace("vendor: alpha,", "vendor: alpha, bill: free,")
            .replace(
                '{ url: "http://localhost:2/v1", vendor: beta,',
                '{ start: "two review", reader: claude-json, vendor: beta,',
            )
            .replace("vendor: beta,", "vendor: beta, bill: free,")
        )
        reasons = self.chain(text, "two@two")
        self.assertIn("claude-json", reasons["two"])
        self.assertIn("no such reader", reasons["two"])


class TheReviewerChain(unittest.TestCase):
    """Criterion 6's fallthrough, and what it says about what it passed over."""

    def setUp(self) -> None:
        self.registry = sd_registry.read_file(SHIPPED)
        self.all = sd_registry.parse_consent(
            " ".join(
                str(sd_registry.recipient(provider))
                for provider in self.registry.providers.values()
            )
        )

    def names(self, **kwargs) -> list[str]:
        return [
            candidate.provider.name
            for candidate in sd_registry.reviewer_chain(
                self.registry, consent=self.all, **kwargs
            )
            if candidate.eligible
        ]

    def test_with_everything_allowed_the_chain_is_the_registry_order(self) -> None:
        self.assertEqual(self.names(), ["codex", "claude", "opencode"])

    def test_unranked_reviewers_never_enter_automatic_fallback(self) -> None:
        self.assertEqual(
            [candidate.provider.name for candidate in sd_registry.reviewer_chain(
                self.registry, consent=self.all,
            )],
            ["codex", "claude", "opencode"],
        )

    def test_a_bill_at_its_cap_is_passed_over(self) -> None:
        self.registry.providers["baseten"] = replace(
            self.registry.providers["baseten"], ranks={"reviewer": 2},
        )
        self.assertNotIn("baseten", self.names(capped_bills={"baseten": AT_CAP}))

    def test_the_chain_reports_every_entry_it_passed_over_and_why(self) -> None:
        """The interesting sentence is the one about what did not run, and
        for a capped bill it carries the month's total the caller measured
        (sd:788 slice 3: `capped_bills` is bill name to exposure line)."""
        self.registry.providers["baseten"] = replace(
            self.registry.providers["baseten"], ranks={"reviewer": 3},
        )
        chain = sd_registry.reviewer_chain(
            self.registry,
            consent=self.all,
            capped_bills={"baseten": AT_CAP},
        )
        skipped = {c.provider.name: c.reason for c in chain if not c.eligible}
        self.assertEqual(skipped["baseten"], f"baseten is billed to baseten: {AT_CAP}")
        self.assertEqual(len(chain), 4)

    def test_consent_bounds_the_chain_absolutely(self) -> None:
        """Two allowed: two candidates, and the fallthrough does not reach a
        third entry it was never allowed."""
        two = sd_registry.parse_consent("claude@claude codex@codex")
        chain = [
            candidate.provider.name
            for candidate in sd_registry.reviewer_chain(
                self.registry, consent=two
            )
            if candidate.eligible
        ]
        self.assertEqual(chain, ["codex", "claude"])

    def test_a_disabled_entry_is_not_on_the_chain_at_all(self) -> None:
        self.assertNotIn("exo", [c.provider.name for c in sd_registry.reviewer_chain(
            self.registry, consent=self.all
        )])


class ThePick(unittest.TestCase):
    """`--provider <name>`: one entry for one run, refused by its own reason."""

    def setUp(self) -> None:
        self.registry = sd_registry.read_file(SHIPPED)
        self.all = sd_registry.parse_consent(
            " ".join(
                str(sd_registry.recipient(provider))
                for provider in self.registry.providers.values()
            )
        )

    def test_a_name_the_registry_does_not_carry(self) -> None:
        with self.assertRaises(sd_registry.RegistryError) as caught:
            sd_registry.pick(self.registry, "greptile", consent=self.all)
        self.assertIn("only list of providers", str(caught.exception))

    def test_a_disabled_entry_is_refused_by_its_reason(self) -> None:
        registry = sd_registry.read_file(written(self, WITH_DISABLED))
        consent = sd_registry.parse_consent(
            " ".join(str(sd_registry.recipient(p)) for p in registry.providers.values())
        )
        with self.assertRaises(sd_registry.RegistryError) as caught:
            sd_registry.pick(registry, "three", consent=consent)
        self.assertIn("model not pinned", str(caught.exception))

    def test_an_entry_that_does_not_hold_the_role(self) -> None:
        registry = sd_registry.read_file(SHIPPED)
        registry.providers["kimi"] = sd_registry.Provider(
            name="kimi", vendor="moonshot", bill="moonshot", url="http://x/v1",
            roles=("author",), ranks={"author": 9},
        )
        with self.assertRaises(sd_registry.RegistryError) as caught:
            sd_registry.pick(registry, "kimi", consent=self.all)
        self.assertIn("cannot review", str(caught.exception))

    def test_an_enabled_unranked_reviewer_requires_an_explicit_pick(self) -> None:
        for name in ("minimax", "kimi", "baseten"):
            with self.subTest(provider=name):
                provider = self.registry.providers[name]
                self.assertNotIn("reviewer", provider.ranks)
                self.assertEqual(
                    sd_registry.pick(self.registry, name, consent=self.all), provider,
                )

    def test_an_explicit_unranked_pick_does_not_grant_transmission_consent(self) -> None:
        with self.assertRaises(sd_registry.ConsentRefusal) as caught:
            sd_registry.pick(self.registry, "minimax", consent={})
        self.assertIn("minimax", str(caught.exception))

    def test_an_explicit_unranked_pick_refuses_changed_recipients(self) -> None:
        self.registry.providers["minimax"] = replace(
            self.registry.providers["minimax"], url="https://other.example/v1",
        )
        with self.assertRaises(sd_registry.ConsentRefusal) as caught:
            sd_registry.pick(self.registry, "minimax", consent=self.all)
        self.assertIn("other.example", str(caught.exception))

    def test_the_direct_pick_asks_readers_the_same_question_the_chain_does(self) -> None:
        """`pick` says it "is refused for the same reasons a fallthrough
        skips", and it took no `readers`, so `--provider` reached an entry the
        chain had already marked unrunnable -- the one path where the promise
        was written down and not kept. `claude` carries the case now that a
        `url` entry has a client and runs."""
        with self.assertRaises(sd_registry.ConsentRefusal) as caught:
            sd_registry.pick(
                self.registry, "claude", consent=self.all, readers=("codex-json",)
            )
        self.assertIn("claude-json", str(caught.exception))

    def test_a_url_entry_is_picked_now_that_a_client_calls_it(self) -> None:
        picked = sd_registry.pick(
            self.registry, "kimi", consent=self.all, readers=("codex-json",)
        )
        self.assertEqual(picked.name, "kimi")

    def test_a_bill_at_its_cap_refuses_the_direct_pick_naming_the_months_total(self) -> None:
        with self.assertRaises(sd_registry.ConsentRefusal) as caught:
            sd_registry.pick(
                self.registry, "baseten", consent=self.all, capped_bills={"baseten": AT_CAP}
            )
        self.assertEqual(str(caught.exception), f"baseten is billed to baseten: {AT_CAP}")

    def test_an_allowed_enabled_entry_is_returned(self) -> None:
        self.assertEqual(
            sd_registry.pick(self.registry, "kimi", consent=self.all).name, "kimi"
        )


class TheRoundTrip(unittest.TestCase):
    """What `Allowance.__str__` writes, `parse_consent` must read back whole.

    The bug this pins: `__str__` quoted a recipient with `shlex.quote`, whose
    safe set *includes* the comma, and `consent_parts` splits on the comma. So
    the one call that looked like protection was a no-op on the one character
    that needed it, and `Allowance("entry", "x,y@z")` rendered as
    `entry@x,y@z` -- two allowances on the way back in, granting `entry` a
    host nobody wrote and inventing an entry called `y`.

    Written as a round trip over the awkward set rather than as an assertion
    about the quoting's output, because the output string is an implementation
    and this is the property.
    """

    AWKWARD = (
        "x,y@z",
        "host.example",
        "user:pw@host.example",
        "localhost:52415",
        "/opt/my tools/codex",
        "tab\tseparated",
        "line\nbreak",
        "hash#mark",
        'double"quote',
        "single'quote",
        "both'\"quotes",
        "g++",
        "codex+notahash",
        "trailing,",
        ",leading",
    )

    def test_every_recipient_survives_the_line_it_is_written_on(self) -> None:
        for recipient in self.AWKWARD:
            for fingerprint in (None, "0123abcd"):
                allowance = sd_registry.Allowance("entry", recipient, fingerprint)
                read = sd_registry.parse_consent(str(allowance))
                self.assertEqual(
                    list(read), ["entry"], f"{allowance} split into {list(read)}"
                )
                self.assertEqual(read["entry"], allowance, str(allowance))

    def test_a_pair_beside_others_still_reads_as_one(self) -> None:
        """One allowance rendered into a real line, not alone on it: the split
        that invented an entry needed a neighbour to hide among."""
        pairs = [sd_registry.Allowance("entry", "x,y@z"), sd_registry.Allowance("b", "two")]
        read = sd_registry.parse_consent(", ".join(str(pair) for pair in pairs))
        self.assertEqual(sorted(read), ["b", "entry"])
        self.assertEqual(read["entry"].recipient, "x,y@z")

    def test_the_reader_and_the_writer_share_one_separator_list(self) -> None:
        """The drift itself. Two lists, and the writer's was missing what the
        reader split on."""
        lexer = shlex.shlex("a", posix=True)
        lexer.whitespace = sd_registry.CONSENT_WHITESPACE
        for character in sd_registry.CONSENT_WHITESPACE:
            rendered = str(sd_registry.Allowance("entry", f"a{character}b"))
            self.assertEqual(len(sd_registry.consent_parts(rendered)), 1, rendered)

    def test_a_recipient_holding_a_plus_keeps_all_of_it(self) -> None:
        """`partition` took the first `+`, so `g++` consented to `g`."""
        read = sd_registry.parse_consent(str(sd_registry.Allowance("entry", "g++")))
        self.assertEqual(read["entry"].recipient, "g++")
        self.assertIsNone(read["entry"].fingerprint)

    def test_a_fingerprint_is_still_read_off_a_hand_written_line(self) -> None:
        allowed = sd_registry.parse_consent("codex@codex+00000000")["codex"]
        self.assertEqual((allowed.recipient, allowed.fingerprint), ("codex", "00000000"))

    def test_a_start_entry_is_immune_to_its_own_plus(self) -> None:
        """The fingerprint is appended last and taken from the right, so an
        executable that ends the way a fingerprint does still round-trips."""
        provider = sd_registry.Provider(
            name="odd", vendor="v", bill="b", start="/opt/build+0123abcd exec"
        )
        allowance = sd_registry.recipient(provider)
        self.assertEqual(sd_registry.parse_consent(str(allowance))["odd"], allowance)
        self.assertIsNone(sd_registry.refuse_allowance(provider, allowance))

    def test_the_one_recipient_the_grammar_cannot_hold_fails_closed(self) -> None:
        """The residue, stated rather than hidden. A recipient that ends in
        `+` and exactly eight hex characters, carrying no fingerprint of its
        own, is spelled the same way as a recipient plus its fingerprint, and
        no quoting separates them -- the split happens after the quotes are
        gone. Only a `url` entry can reach this, its host being the one
        recipient with no fingerprint appended after it.

        What matters is the direction it fails in: the pair reads back as
        something the comparison refuses, never as consent to a destination
        nobody wrote."""
        allowance = sd_registry.Allowance("odd", "host+0123abcd")
        read = sd_registry.parse_consent(str(allowance))["odd"]
        self.assertNotEqual(read, allowance)
        provider = sd_registry.Provider(
            name="odd", vendor="v", bill="b", url="https://host+0123abcd/v1"
        )
        self.assertIsNotNone(sd_registry.refuse_allowance(provider, read))


class _Answer:
    """The two methods `chat_completion` uses of an opened response."""

    def __init__(self, body: str) -> None:
        self.body = body.encode("utf-8")

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def __enter__(self) -> "_Answer":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class Wire(unittest.TestCase):
    """The one client, with the socket replaced by a recorder.

    Every test here asserts on `self.sent`, and the ones about a refusal assert
    that it is empty. An outbound request this repository never consented to is
    the failure this whole file exists to prevent, and the only way to see it
    is to watch the place the request would have left from.
    """

    def setUp(self) -> None:
        self.sent: list[Any] = []
        self.answer: Any = _Answer('{"choices": [{"message": {"content": "{}"}}]}')

        def opened(request: Any, timeout: int = 0) -> Any:
            self.sent.append(request)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        patched = unittest.mock.patch.object(sd_registry._OPENER, "open", opened)
        patched.start()
        self.addCleanup(patched.stop)

    def entry(self, url: str = "https://api.example.test/v1") -> sd_registry.Provider:
        return sd_registry.Provider(
            name="entry",
            vendor="somevendor",
            bill="free",
            url=url,
            model="a-model",
            max_tokens=16384,
            env=("ENTRY_KEY",),
        )

    def call(self, provider: Any, **env: str) -> tuple[int, str, str, bool, int | None]:
        return sd_registry.chat_completion(provider, "prompt", env, 30)

    def test_the_request_is_the_one_the_entry_describes(self) -> None:
        code, stdout, stderr, launched, _ = self.call(self.entry(), ENTRY_KEY="secret")
        self.assertEqual((code, stderr, launched), (0, "", True))
        self.assertIn("choices", stdout)
        request = self.sent[0]
        self.assertEqual(request.full_url, "https://api.example.test/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "a-model")
        self.assertEqual(body["max_tokens"], 16384)
        self.assertEqual(body["messages"], [{"role": "user", "content": "prompt"}])
        # `price` and `bill` are the ledger's business, not the endpoint's.
        self.assertNotIn("price", body)

    def test_an_https_entry_edited_to_http_refuses_and_sends_nothing(self) -> None:
        """The hole this closes. `recipient()` compares `netloc`, which carries
        no scheme, so this edit passes `refuse_allowance` in silence -- and
        would put the diff on the wire in the clear."""
        code, _, stderr, launched, _ = self.call(
            self.entry("http://api.example.test/v1"), ENTRY_KEY="secret"
        )
        self.assertEqual((code, launched), (1, False))
        self.assertIn("in the clear", stderr)
        self.assertIn("api.example.test", stderr)
        self.assertEqual(self.sent, [], "a cleartext entry sends nothing")

    def test_loopback_is_the_exception_because_exo_is_one(self) -> None:
        for url in ("http://localhost:52415/v1", "http://127.0.0.1:8080/v1",
                    "http://[::1]/v1", "http://127.0.0.2/v1"):
            self.assertIsNone(sd_registry.refuse_cleartext(self.entry(url)), url)

    def test_a_host_that_only_looks_like_loopback_is_not_one(self) -> None:
        """The exemption's own defect class, one level down. `startswith`
        answered a question about spelling; RFC 1123 lets a DNS label begin
        with a digit, so each of these is a registrable public domain that
        took the loopback exemption and got cleartext."""
        for host in ("127.evil.com", "127.0.0.1.evil.com", "localhost.evil.com",
                     "0127.0.0.1", "notlocalhost"):
            refusal = sd_registry.refuse_cleartext(self.entry(f"http://{host}/v1"))
            self.assertIsNotNone(refusal, host)
            self.assertIn("in the clear", str(refusal))
            self.assertIsNone(
                sd_registry.refuse_cleartext(self.entry(f"https://{host}/v1")), host
            )

    def test_the_shortened_form_is_refused_rather_than_parsed(self) -> None:
        """Deliberate. curl reads `127.1` as loopback and `ipaddress` does not
        parse it, so this refuses -- a second opinion about what an address
        means does not belong on the path that decides whether a diff leaves
        in the clear."""
        self.assertIsNotNone(sd_registry.refuse_cleartext(self.entry("http://127.1/v1")))

    def test_the_scheme_is_read_and_not_spelled(self) -> None:
        self.assertIsNone(sd_registry.refuse_cleartext(self.entry("HTTPS://api.example.test/v1")))
        self.assertIsNotNone(sd_registry.refuse_cleartext(self.entry("ftp://api.example.test/v1")))

    def test_a_missing_key_sends_nothing_and_never_reads_as_a_quota_stop(self) -> None:
        code, _, stderr, launched, _ = self.call(self.entry())
        self.assertEqual((code, launched), (1, False))
        self.assertIn("ENTRY_KEY", stderr)
        self.assertEqual(self.sent, [])

    def test_a_429_carries_the_status_into_the_rate_limit_vocabulary(self) -> None:
        self.answer = urllib.error.HTTPError(
            "https://api.example.test/v1/chat/completions", 429, "Too Many Requests",
            {}, io.BytesIO(b"slow down"),
        )
        code, _, stderr, launched, _ = self.call(self.entry(), ENTRY_KEY="secret")
        self.assertEqual((code, launched), (429, True))
        self.assertIn("HTTP 429", stderr)
        markers = [m for m in sd_review_markers() if m in stderr.lower()]
        self.assertTrue(markers, f"no rate-limit marker in {stderr!r}")

    def test_a_connection_error_never_launched(self) -> None:
        # The message says 429 on purpose: `launched=False` is read first, so a
        # connection that never happened cannot be reported as a quota stop.
        self.answer = urllib.error.URLError("connection refused after 429 tries")
        code, _, stderr, launched, _ = self.call(self.entry(), ENTRY_KEY="secret")
        self.assertEqual((code, launched), (1, False))
        self.assertIn("connection refused", stderr)

    def test_a_redirect_is_not_followed(self) -> None:
        """A 3xx names a host the `reviewers` line never did, and urllib would
        carry the `Authorization` header to it."""
        self.assertIsNone(
            sd_registry._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://elsewhere.test")
        )


def sd_review_markers() -> tuple[str, ...]:
    """The rate-limit markers `bin/sd-review` already carries, read from it so
    this file cannot drift from the vocabulary it is asserting against."""
    source = (REPO_ROOT / "bin" / "sd-review").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "RATE_LIMIT_MARKERS"
            for target in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("bin/sd-review no longer defines RATE_LIMIT_MARKERS")


class TheOneReader(unittest.TestCase):
    """`url_answer`, and the one distinction the whole lane rests on.

    `""` and a findings list are different answers. `""` becomes `None` from
    `parse_findings` and then an unavailable review; a findings list, empty or
    not, is a review that happened. A response this build could not read must
    never come back as the second.
    """

    def body(self, content: Any, **extra: Any) -> str:
        message: dict[str, Any] = {"content": content, **extra}
        return json.dumps({"choices": [{"message": message}], "id": "x"})

    def test_a_think_block_and_reasoning_content_still_read_clean(self) -> None:
        """Criterion 6's test. `<think>` is stripped and `reasoning_content` is
        ignored rather than concatenated -- concatenating it is what turns an
        entry that answered correctly into an unavailable one."""
        answer = self.body(
            '<think>The diff renames a symbol. Let me check every caller.</think>\n'
            '{"findings": [{"path": "bin/sd", "line": 4, "severity": "high", "instances": ["src.py:1"],'
            ' "summary": "the old name is still read", "family": "correctness"}]}',
            reasoning_content="The diff renames a symbol. Let me check every caller.",
        )
        text = sd_registry.url_answer(answer)
        self.assertTrue(text.startswith("{"), text)
        self.assertNotIn("Let me check", text)
        self.assertEqual(json.loads(text)["findings"][0]["path"], "bin/sd")

    def test_every_block_goes_whatever_its_case_or_attributes(self) -> None:
        text = sd_registry.url_answer(
            self.body('<think>one</think>{"findings"<THINK reason="x">two</think>: []}')
        )
        self.assertEqual(json.loads(text), {"findings": []})

    def test_an_unclosed_block_runs_to_the_end_and_leaves_nothing(self) -> None:
        # Reachable by construction: `max_tokens` is 16,384 on every enabled
        # `url` entry, so a model that spends it reasoning stops mid-tag. Cut
        # inside the opening block there is nothing left; cut after a partial
        # answer, what survives is a truncated one, which does not parse and so
        # is unavailable rather than clean.
        self.assertEqual(sd_registry.url_answer(self.body("<think>oh")), "")
        truncated = sd_registry.url_answer(self.body('{"findings"<think>oh'))
        self.assertEqual(truncated, '{"findings"')
        with self.assertRaises(json.JSONDecodeError):
            json.loads(truncated)

    def test_content_that_is_only_reasoning_is_not_a_clean_review(self) -> None:
        for content in ("<think>all of it</think>", "  <think>x</think>\n\t ", "   "):
            self.assertEqual(sd_registry.url_answer(self.body(content)), "", content)

    def test_a_nested_pair_leaves_a_stray_close_and_does_not_parse(self) -> None:
        text = sd_registry.url_answer(self.body("<think>a<think>b</think>c</think>{}"))
        self.assertEqual(text, "c</think>{}")

    def test_what_is_not_an_answer_at_all(self) -> None:
        for body in (
            "not json",
            "",
            json.dumps({"error": {"message": "no"}}),
            json.dumps({"choices": []}),
            json.dumps({"choices": [{"message": {}}]}),
            json.dumps({"choices": [{"message": {"content": None}}]}),
            json.dumps({"choices": [{"message": {"reasoning_content": "only this"}}]}),
            json.dumps(["not", "a", "mapping"]),
        ):
            self.assertEqual(sd_registry.url_answer(body), "", body)


class TheSchemeIsAConsentQuestion(unittest.TestCase):
    """Where the cleartext refusal lives, and why it is not two other places.

    Not in `_provider`: `_adapt` builds providers from `sd_db`'s rows without
    passing through it, so a check there fires only on machines with no
    database, and one mistake would have two behaviours. Not only in the
    client either: the chain, the dry run and the run have to give the same
    answer, and `refuse_allowance` is where the other consent refusal -- the
    host that moved -- already is. The client keeps its own copy as the last
    thing before the socket, for callers that never asked the chain.
    """

    def entry(self, url: str) -> sd_registry.Provider:
        return sd_registry.Provider(name="two", vendor="beta", bill="free", url=url)

    def allowance(self, url: str) -> str | None:
        provider = self.entry(url)
        return sd_registry.refuse_allowance(provider, sd_registry.recipient(provider))

    def test_the_scheme_alone_decides_it(self) -> None:
        self.assertIsNone(self.allowance("https://api.example.test/v1"))
        refusal = self.allowance("http://api.example.test/v1")
        self.assertIn("in the clear", str(refusal))
        self.assertIn("api.example.test", str(refusal))

    def test_consent_to_the_host_does_not_cover_the_scheme(self) -> None:
        """The hole itself: `netloc` is identical either way, so the consented
        recipient of the https entry consents to the http one."""
        self.assertEqual(
            sd_registry.recipient(self.entry("https://api.example.test/v1")).recipient,
            sd_registry.recipient(self.entry("http://api.example.test/v1")).recipient,
        )

    def test_a_loopback_entry_still_resolves_over_http(self) -> None:
        # A local server on `http://localhost:<port>/v1` is what the loopback
        # exception is for. If this fails, the exception is wrong.
        self.assertIsNone(self.allowance("http://localhost:52415/v1"))


class StandingReviewConsentTests(unittest.TestCase):
    def test_machine_policy_and_local_presence_resolve_without_widening_restrictions(self):
        registry = sd_registry.read_file(SHIPPED)
        allowed, source = sd_registry.resolve_consent(registry, None, "configured")
        self.assertEqual(source, "machine-configured")
        self.assertEqual(allowed, {
            p.name: sd_registry.recipient(p)
            for p in registry.providers.values()
            if p.enabled and "reviewer" in p.roles
        })
        self.assertEqual(sd_registry.resolve_consent(registry, "", "configured"), ({}, "repository"))
        local = str(next(iter(allowed.values())))
        self.assertEqual(sd_registry.resolve_consent(registry, local, "configured")[0], sd_registry.parse_consent(local))
        self.assertEqual(sd_registry.resolve_consent(registry, local, "deny"), ({}, "machine-deny"))
        for policy, line in ((None, None), ("configured", "broken"), ("wrong", None)):
            with self.subTest(policy=policy, line=line), self.assertRaises(sd_registry.ConsentRefusal):
                sd_registry.resolve_consent(registry, line, policy)

    def test_new_configured_providers_still_obey_bill_reader_and_transport_guards(self):
        registry = sd_registry.read_file(SHIPPED)
        template = registry.providers["baseten"]
        entry = replace(template, name="future-reviewer", vendor="future-vendor", ranks={})
        registry.providers[entry.name] = entry
        consent, _ = sd_registry.resolve_consent(registry, None, "configured")
        self.assertIn(entry.name, consent)
        self.assertNotIn(entry, registry.order("reviewer"))
        self.assertEqual(sd_registry.pick(registry, entry.name, consent=consent), entry)
        with self.assertRaises(sd_registry.ConsentRefusal):
            sd_registry.pick(registry, entry.name, consent=consent, capped_bills={entry.bill: AT_CAP})
        registry.providers[entry.name] = replace(entry, enabled=False)
        self.assertNotIn(entry.name, sd_registry.resolve_consent(registry, None, "configured")[0])
        with self.assertRaises(sd_registry.RegistryError):
            sd_registry.pick(registry, entry.name, consent=consent)
        for changes in ({"url": "http://external.example/v1"}, {"url": None, "start": "future-cli", "reader": "unknown-reader"}):
            registry.providers[entry.name] = replace(entry, **changes)
            consent, _ = sd_registry.resolve_consent(registry, None, "configured")
            with self.subTest(changes=changes), self.assertRaises(sd_registry.ConsentRefusal):
                sd_registry.pick(registry, entry.name, consent=consent, readers=("codex-json",))

    def test_configured_consent_does_not_authorize_non_reviewers(self):
        registry = sd_registry.read_file(written(self, WITH_DISABLED))
        consent, _ = sd_registry.resolve_consent(registry, None, "configured")
        self.assertEqual(set(consent), {"two"})


# sd:788 slice 4: the meter, pinned to one destination and read fail-closed

RECORDED_METER = REPO_ROOT / "tests" / "fixtures" / "minimax" / "token_plan_remains.json"
PINNED_METER = "https://www.minimax.io/v1/token_plan/remains"


def metered_bill(meter: str = PINNED_METER, meter_env: str | None = "METER_KEY") -> sd_registry.Bill:
    return sd_registry.Bill(name="minimax", cost_basis="plan", meter=meter, meter_env=meter_env)


class TheMeterPin(Wire):
    """`meter_reading` sends the bill's key to the pinned destination and to
    nothing else; the recorder is where a request would have left from."""

    def test_the_pinned_value_is_one_get_with_the_bills_key(self) -> None:
        self.answer = _Answer(RECORDED_METER.read_text(encoding="utf-8"))
        code, body, stderr, launched, _ = sd_registry.meter_reading(metered_bill(), {"METER_KEY": "secret"}, 30)
        self.assertEqual((code, stderr, launched), (0, "", True))
        self.assertIn("model_remains", body)
        request = self.sent[0]
        self.assertEqual((request.full_url, request.get_method()), (PINNED_METER, "GET"))
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertIsNone(sd_registry.refuse_meter(metered_bill()))

    def test_any_other_scheme_host_port_or_path_is_refused_naming_the_value_and_the_four(self) -> None:
        for value, part in (
            ("https://other.example.test/v1/token_plan/remains", "host"),
            ("http://www.minimax.io/v1/token_plan/remains", "scheme"),
            ("https://www.minimax.io:8443/v1/token_plan/remains", "port"),
            ("https://www.minimax.io/v1/other", "path"),
            ("https://www.minimax.io/v1/token_plan/remains?to=elsewhere", "path"),
        ):
            with self.subTest(value=value):
                code, _, stderr, launched, _ = sd_registry.meter_reading(metered_bill(value), {"METER_KEY": "secret"}, 30)
                self.assertEqual((code, launched), (1, False))
                self.assertEqual(stderr, sd_registry.refuse_meter(metered_bill(value)))
                self.assertIn(value, stderr)
                self.assertIn(f"whose {part} is not", stderr)
                for pinned in ("'https'", "'www.minimax.io'", "no explicit port", "'/v1/token_plan/remains'"):
                    self.assertIn(pinned, stderr)
                self.assertEqual(self.sent, [], "a meter off the pin sends nothing")

    def test_no_meter_env_or_no_value_sends_nothing(self) -> None:
        for bill, environ, named in ((metered_bill(meter_env=None), {"METER_KEY": "secret"}, "no meter_env:"),
                                     (metered_bill(), {}, "no value for METER_KEY")):
            with self.subTest(named=named):
                code, _, stderr, launched, _ = sd_registry.meter_reading(bill, environ, 30)
                self.assertEqual((code, launched), (1, False))
                self.assertIn(named, stderr)
                self.assertEqual(self.sent, [])

    def test_a_connection_failure_is_not_an_answer(self) -> None:
        self.answer = urllib.error.URLError("connection refused")
        code, body, stderr, launched, status = sd_registry.meter_reading(metered_bill(), {"METER_KEY": "secret"}, 30)
        self.assertEqual((code, body, launched, status), (1, "", False, None))
        self.assertIn("connection refused", stderr)


class TheMeterAnswer(unittest.TestCase):
    """`meter_percents` over the one recording, edited in memory per case:
    the fixture directory stays one file, and each case names what it broke."""

    def setUp(self) -> None:
        self.recorded = RECORDED_METER.read_text(encoding="utf-8")

    def edited(self, **changes: Any) -> str:
        """The recording with `general`'s fields changed; `absent=<field>` drops one."""
        answer = json.loads(self.recorded)
        general = next(entry for entry in answer["model_remains"] if entry["model_name"] == "general")
        for key, value in changes.items():
            if key == "absent":
                del general[value]
            else:
                general[key] = value
        return json.dumps(answer)

    def test_the_recording_reads_as_both_percents_of_the_general_entry(self) -> None:
        self.assertEqual(sd_registry.meter_percents(self.recorded), (100.0, 100.0))
        self.assertEqual(sd_registry.meter_percents(self.edited(current_interval_remaining_percent=0)), (0.0, 100.0))

    def test_no_general_entry_or_two_caps_naming_the_count(self) -> None:
        self.assertEqual(sd_registry.meter_percents(self.edited(model_name="video")),
                         "model_remains carries 0 'general' entries, not one")
        answer = json.loads(self.recorded)
        answer["model_remains"].append(dict(answer["model_remains"][0]))
        self.assertEqual(sd_registry.meter_percents(json.dumps(answer)),
                         "model_remains carries 2 'general' entries, not one")

    def test_each_field_is_validated_naming_the_field_and_the_value(self) -> None:
        for field in sd_registry.METER_FIELDS:
            with self.subTest(field=field, value="missing"):
                self.assertEqual(sd_registry.meter_percents(self.edited(absent=field)), f"{field} is missing")
            for value in ("50", True, float("nan"), float("inf"), -1, 101):
                with self.subTest(field=field, value=value):
                    sentence = sd_registry.meter_percents(self.edited(**{field: value}))
                    self.assertEqual(sentence, f"{field} is {value!r}, not a number from 0 to 100")

    def test_an_answer_that_is_not_the_shape_is_one_sentence(self) -> None:
        self.assertEqual(sd_registry.meter_percents("not json"), "the answer is not JSON")
        self.assertEqual(sd_registry.meter_percents('{"base_resp": {}}'), "the answer carries no model_remains list")


class TheMeterEnvField(unittest.TestCase):
    """`meter_env` on the bill row, read as one variable name by both readers,
    and a `meter:` without it read without refusal (sd:788 note 2694)."""

    def test_the_shipped_minimax_bill_names_its_key_through_both_readers(self) -> None:
        for prefer_library in (False, True):
            with self.subTest(prefer_library=prefer_library):
                bill = sd_registry.read(SHIPPED, prefer_library=prefer_library).bills["minimax"]
                self.assertEqual((bill.meter, bill.meter_env), (PINNED_METER, "MINIMAX_API_KEY"))

    def test_a_meter_without_meter_env_reads_and_a_list_is_refused(self) -> None:
        def registry(tail: str) -> str:
            return WITH_DISABLED.replace("free: { cost: local }", f'free: {{ cost: local }}\n  plan: {{ cost: plan, meter: "{PINNED_METER}"{tail} }}')

        self.assertIsNone(sd_registry.parse(registry("")).bills["plan"].meter_env)
        self.assertEqual(sd_registry.parse(registry(", meter_env: PLAN_KEY")).bills["plan"].meter_env, "PLAN_KEY")
        with self.assertRaises(sd_registry.RegistryError) as caught:
            sd_registry.parse(registry(", meter_env: [A, B]"))
        self.assertIn("'meter_env' of bill 'plan' is ['A', 'B'], which is not a variable name", str(caught.exception))


class LoopbackNeedsNoCredentialTests(unittest.TestCase):
    """sd:1145 -- a locally hosted server authenticates nobody.

    The registry has no way to spell "this recipient wants no key", so the
    credential rule kept every loopback entry unreachable and the only way
    round it was a placeholder variable: a secret in name only, and one more
    thing to keep exported. The exemption is deliberately narrow -- loopback
    AND no declared variable -- because each half alone is a hole.
    """

    def entry(self, url, env=()):
        return sd_registry.Provider(
            name="local", url=url, start=None, model="m", vendor="v", bill="b",
            roles=("reviewer",), ranks={}, reader=None, max_tokens=16, env=tuple(env),
            price=None, enabled=True, reason=None, thinking=None, reasoning_effort=None)

    def test_a_loopback_entry_with_no_variable_is_called_without_a_key(self):
        sent = {}

        def opener(request, timeout=None):
            sent["headers"] = dict(request.headers)
            raise OSError("no server, and the headers are what this asserts")

        with unittest.mock.patch.object(sd_registry, "_DIRECT_OPENER", types.SimpleNamespace(open=opener)):
            code, _, detail, launched, _ = sd_registry.chat_completion(
                self.entry("http://localhost:8084/v1"), "p", {}, 5)
        # It reached the transport rather than being refused before it.
        self.assertIn("no server", detail)
        self.assertNotIn("Authorization", sent["headers"])
        self.assertNotIn("Authorization".lower().capitalize(), sent["headers"])

    def test_a_loopback_url_routes_past_a_configured_proxy(self):
        """`build_opener` installs a `ProxyHandler` reading `HTTP_PROXY` when
        it is built, and urllib's bypass list does not special-case loopback. On
        a box where `HTTP_PROXY` is set and `NO_PROXY` omits `localhost`,
        the default opener forwards a loopback request to the proxy and the
        diff leaves the machine -- the more so now that such a call carries
        no key and so looks harmless.
        """
        self.assertIs(sd_registry._opener("http://localhost:8084/v1/chat/completions"),
                      sd_registry._DIRECT_OPENER)
        self.assertIs(sd_registry._opener("http://127.0.0.1:8084/v1/chat/completions"),
                      sd_registry._DIRECT_OPENER)
        self.assertIs(sd_registry._opener("https://api.example.com/v1/chat/completions"),
                      sd_registry._OPENER)
        self.assertIs(sd_registry._opener("http://127.evil.com/v1/chat/completions"),
                      sd_registry._OPENER)

    def test_the_two_opener_recipes_differ_under_a_configured_proxy(self):
        """Both are built at their first request, so what they hold depends on
        the environment this process had then -- which is why this
        rebuilds each recipe under a forced `http_proxy` rather than reading
        the module's own two. `build_opener` drops a `ProxyHandler({})`
        entirely rather than registering an inert one, so "no ProxyHandler"
        is what a proxy-free opener looks like.
        """
        with unittest.mock.patch.dict(os.environ, {"http_proxy": "http://proxy.example:3128"}):
            default = urllib.request.build_opener(sd_registry._NoRedirect)
            direct = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), sd_registry._NoRedirect)
        self.assertEqual(
            [h.proxies for h in default.handlers if isinstance(h, urllib.request.ProxyHandler)],
            [{"http": "http://proxy.example:3128"}])
        self.assertEqual(
            [h for h in direct.handlers if isinstance(h, urllib.request.ProxyHandler)], [])

    def test_importing_the_registry_reads_no_proxy_settings(self):
        """sd:1615. The default opener's `ProxyHandler` asks the system for
        its proxies, an IPC round trip of 0.1-0.2 s on macOS, and every
        `sd-status`, `sd-review` and `sd-ship` process imports this module.
        The openers are built on their first request; a process that sends
        none never asks."""
        probe = (
            "import sys, urllib.request\n"
            "asked = []\n"
            "urllib.request.getproxies = lambda: asked.append(1) or {}\n"
            f"sys.path.insert(0, {str(REPO_ROOT / 'bin')!r})\n"
            "import sd_registry\n"
            "print(len(asked))\n"
            "opener = sd_registry._OPENER.built()\n"
            "print(len(asked), opener is sd_registry._OPENER.built(),\n"
            "      any(isinstance(h, sd_registry._NoRedirect) for h in opener.handlers))\n"
        )
        done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
        self.assertEqual(done.stdout.split("\n")[0], "0", "the import asked for the proxies")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split("\n")[1], "1 True True")

    def test_a_public_entry_with_no_variable_is_still_refused(self):
        """The half that would be a hole: no key, but not this machine."""
        code, _, detail, launched, _ = sd_registry.chat_completion(
            self.entry("https://api.example.com/v1"), "p", {}, 5)
        self.assertEqual((code, launched), (1, False))
        self.assertIn("has no value for any key", detail)

    def test_a_loopback_entry_that_declares_a_variable_still_needs_its_value(self):
        """The other half: a server configured to check a key gets one."""
        code, _, detail, launched, _ = sd_registry.chat_completion(
            self.entry("http://localhost:8084/v1", ("LOCAL_KEY",)), "p", {}, 5)
        self.assertEqual((code, launched), (1, False))
        self.assertIn("has no value for LOCAL_KEY", detail)

    def test_loopback_refuses_a_public_name_that_merely_starts_with_127(self):
        for host in ("127.evil.com", "127.0.0.1.evil.com", "127.1", "localhost.evil.com"):
            with self.subTest(host=host):
                self.assertFalse(sd_registry.loopback(self.entry(f"http://{host}/v1")))

    def test_loopback_accepts_the_real_local_addresses(self):
        for host in ("localhost", "127.0.0.1", "[::1]", "127.0.0.2"):
            with self.subTest(host=host):
                self.assertTrue(sd_registry.loopback(self.entry(f"http://{host}/v1")))


class TheGuidanceDefersToTheOrder(unittest.TestCase):
    """Pack-owned reviewer guidance names no provider preference (sd:1556).

    "Prefer Codex" and "MiniMax and Baseten require explicit selection" sent a
    session around the chain the operator had configured, which ranked
    minimax second: it called `codex exec` by hand and fell back to
    same-vendor reviews. The order has one home, the registry, so the prose
    that tells an agent how to pick a reviewer points there and ranks nobody.
    """

    PREFERENCE = r"\b(prefer|preferred|as backup|requires? explicit selection|only after an explicit)\b"
    GUIDANCE = (".claude/rules", "skills", "hooks", "agents", "AGENTS.md", "WORKFLOW.md")

    def names(self) -> list[str]:
        """Enumerated from the shipped registry, not remembered here."""
        return sorted(sd_registry.read_file(SHIPPED).providers)

    def guidance(self) -> list[pathlib.Path]:
        found = []
        for entry in self.GUIDANCE:
            path = REPO_ROOT / entry
            found.extend([path] if path.is_file() else
                         sorted(p for p in path.rglob("*") if p.is_file() and p.suffix in ("", ".md", ".sh")))
        return found

    def test_no_guidance_line_ranks_a_registry_provider(self) -> None:
        import re  # noqa: PLC0415 - local, so the lines docs cite above stay put

        named = re.compile(r"\b(" + "|".join(map(re.escape, self.names())) + r")\b", re.IGNORECASE)
        hits = [f"{path.relative_to(REPO_ROOT)}: {line.strip()}"
                for path in self.guidance()
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                if re.search(self.PREFERENCE, line, re.IGNORECASE) and named.search(line)]
        self.assertEqual(hits, [])

    def test_the_operator_defaults_point_at_the_resolved_order(self) -> None:
        text = (REPO_ROOT / ".claude/rules/sd-operator-defaults.md").read_text(encoding="utf-8")
        section = text.split("## Reviewers", 1)[1].split("\n## ", 1)[0]
        self.assertIn("sd providers list", section)
        import re  # noqa: PLC0415

        named = [name for name in self.names() if re.search(rf"\b{re.escape(name)}\b", section, re.IGNORECASE)]
        self.assertEqual(named, [])


if __name__ == "__main__":
    unittest.main()
