"""sd:788 slice 3: every `url` call `bin/sd-review` makes goes through the
library's ledger, and a bill at its cap is passed over and refused by name.

The road is `sd_db.calls.call` (sd:234 slice 8b): `release_orphans`, `reserve`
at the bound, `claim`, one POST, `settle` or `lose`. The pack writes no cost
row; these tests read the rows the library left. The wire is the pack's fake
client, handed to the library as its transport, so no test reaches a network.

Every case runs in a fixture home with its own `sd.db` beside its own
`providers.yaml`, never the operator's: `review` resolves both from the
environment it is handed, and the connection it opens for writing is on that
database and no other.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import threading
import types
import unittest
from typing import Any
from unittest import mock

from sd_db import connect, initialise, read_registry, record_cost, seed, writes
from sd_db.calls import BYTES_PER_TOKEN, bound_for
from sd_db.ledger import exposure

from tests.test_sd_registry import SHIPPED, _Answer
from tests.test_sd_review import (
    FakeClient,
    FakeRunner,
    ReviewFixture,
    chat_answer,
    namespace,
    sd_review,
)

#: A capped `url` entry with the price and `max_tokens` its bound needs, and
#: an uncapped one beside it, so the same run shows both roads. `paid`'s
#: bound is the prompt's tokens at $1/M plus 100000 tokens at $2/M, so a
#: little over $0.20 a call against a $1 cap.
REGISTRY = """\
bills:
  fixture: { cost: subscription }
  paid:    { cost: company, cap_usd_month: 1 }
providers:
  paid: { url: "https://paid.example.test/v1", model: fixture, vendor: paidvendor, bill: paid,
          roles: [reviewer], max_tokens: 100000, price: { in: 1.0, out: 2.0 }, env: [REMOTE_KEY] }
  free: { url: "https://free.example.test/v1", model: fixture, vendor: freevendor, bill: fixture,
          roles: [reviewer], env: [REMOTE_KEY] }
roles:
  author: []
  reviewer: [paid, free]
"""


def usage_answer(prompt_tokens: int = 10, completion_tokens: int = 10) -> tuple[int, str, str, bool, int]:
    """A clean answer whose body carries the usage the library settles at."""
    body = json.loads(chat_answer('{"findings": []}')[1])
    body["usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                     "total_tokens": prompt_tokens + completion_tokens}
    return (0, json.dumps(body), "", True, 200)


class LedgerFixture(ReviewFixture):
    def setUp(self) -> None:
        super().setUp()
        self.registry_path = self.registry_home / ".local/share/sd/providers.yaml"
        self.registry_path.write_text(REGISTRY, encoding="utf-8")
        self.database = self.registry_path.with_name("sd.db")
        self.root = self.make_repo()
        (self.root / "src.py").write_text("the_review_subject = 123\n")
        self.stub_policy({"default_tier": "cheap"})
        (self.root / "CLAUDE.local.md").write_text(
            "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\n"
            "reviewers: paid@paid.example.test, free@free.example.test\n"
            "<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n")
        self.runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})

    def seed(self) -> None:
        initialise(self.database)
        with connect(self.database) as connection:
            seed(connection, read_registry(self.registry_path))
        connection.close()

    def rows(self) -> list[tuple[Any, ...]]:
        connection = connect(self.database, write=False)
        try:
            return [tuple(row) for row in connection.execute(
                "SELECT provider, bill, source, usd, tokens_in, tokens_out FROM cost ORDER BY id")]
        finally:
            connection.close()

    def spend(self, usd: float, bill: str = "paid") -> None:
        connection = connect(self.database)
        try:
            record_cost(connection, source="run", provider=bill, bill=bill, usd=usd)
        finally:
            connection.close()

    def review(self, client: FakeClient | None = None, **options: Any) -> dict[str, Any]:
        return sd_review.review(self.root, namespace(**options), self.runner,
                                self.environment(REMOTE_KEY="fixture"), client=client or FakeClient(default=usage_answer()))

    def ledger(self) -> Any:
        registry = sd_review.sd_registry.read_file(self.registry_path)
        return sd_review.open_ledger(self.environment(), None, registry)


class TheCostRows(LedgerFixture):
    """The cost-row test and the integration test through `review`: an
    at-cap bill in the database, nothing handed in by the test."""
    reviewing_depth = 1  # one charged url call per review

    def test_a_charged_call_leaves_one_run_row_at_the_usage_and_the_receipt_names_it(self) -> None:
        self.seed()
        client = FakeClient(default=usage_answer(1000, 2000))
        result = self.review(client)
        self.assertEqual(result["reviewed_by"], ["paid"])
        self.assertEqual(self.rows(), [("paid", "paid", "run", 1000 * 1.0 / 1e6 + 2000 * 2.0 / 1e6, 1000, 2000)])
        receipt = result["outcomes"][0]["diagnostic"]["ledger"]
        self.assertEqual((receipt["outcome"], receipt["usd"]), ("run", self.rows()[0][3]))
        self.assertGreater(receipt["bound_usd"], 0.2)
        self.assertEqual(result["capped_bills"], {})
        self.assertEqual(result["ledger_fault"], "")
        # pack sd:8503: the outcome itself names what the call cost, and when it ran.
        self.assertEqual(result["outcomes"][0]["cost"], {"usd": receipt["usd"], "input_tokens": 1000, "output_tokens": 2000})
        self.assertIsInstance(result["outcomes"][0]["wall_seconds"], float)

    def test_cost_rows_at_the_cap_are_passed_over_by_fallthrough_naming_the_months_total(self) -> None:
        self.seed()
        self.spend(0.6)
        self.spend(0.4)
        client = FakeClient(default=usage_answer())
        result = self.review(client)
        self.assertEqual(result["reviewed_by"], ["free"])
        self.assertEqual([row["provider"] for row in client.sent], ["free"])
        month = writes.now()[:7]
        line = f"$1.00 of its $1.00 cap for {month} is spent or held"
        self.assertEqual(result["capped_bills"], {"paid": line})
        skipped = {row["provider"]: row["reason"] for row in result["chain"] if not row["eligible"]}
        self.assertEqual(skipped, {"paid": f"paid is billed to paid: {line}"})
        self.assertEqual([row[2] for row in self.rows()], ["run", "run", "run"])

    def test_a_direct_pick_of_the_at_cap_bill_refuses_naming_the_months_total(self) -> None:
        self.seed()
        self.spend(1.0)
        client = FakeClient(default=usage_answer())
        with self.assertRaises(sd_review.sd_registry.ConsentRefusal) as caught:
            self.review(client, provider="paid")
        self.assertIn("paid is billed to paid: $1.00 of its $1.00 cap for", str(caught.exception))
        self.assertEqual(client.sent, [])

    def test_the_chain_and_the_pick_read_the_rows_through_capped_bills(self) -> None:
        """The unit under the integration: `capped_bills` sums the month's
        rows per capped bill and hands the chain the line."""
        self.seed()
        registry = sd_review.sd_registry.read_file(self.registry_path)
        self.assertEqual(sd_review.capped_bills(registry, self.ledger()), {})
        self.spend(1.0)
        capped = sd_review.capped_bills(registry, self.ledger())
        self.assertEqual(list(capped), ["paid"])
        consent = sd_review.sd_registry.parse_consent("paid@paid.example.test, free@free.example.test")
        chain = sd_review.sd_registry.reviewer_chain(registry, consent=consent, capped_bills=capped)
        self.assertEqual([c.provider.name for c in chain if c.eligible], ["free"])
        with self.assertRaises(sd_review.sd_registry.ConsentRefusal) as caught:
            sd_review.sd_registry.pick(registry, "paid", consent=consent, capped_bills=capped)
        self.assertEqual(str(caught.exception), f"paid is billed to paid: {capped['paid']}")

    def test_a_bound_that_would_pass_the_cap_is_refused_at_reserve_and_falls_through(self) -> None:
        """Exposure under the cap, room below one bound: not in
        `capped_bills`, refused by the ledger inside `run_provider`, passed
        over; and the refusal names the bill, its room and the bound."""
        self.seed()
        self.spend(0.9)
        client = FakeClient(default=usage_answer())
        result = self.review(client)
        self.assertEqual(result["capped_bills"], {})
        self.assertEqual([row["provider"] for row in client.sent], ["free"])
        refused = result["outcomes"][0]
        self.assertEqual((refused["backend"], refused["status"]), ("paid", sd_review.REFUSED))
        self.assertIn("bill 'paid' has 0.10 of its 1.00 cap left", refused["detail"])
        self.assertEqual(result["reviewed_by"], ["free"])
        self.assertEqual([row[2] for row in self.rows()], ["run", "run"])

    def test_explanation_keeps_orphans_until_an_actual_ready_review(self) -> None:
        """A read-only explanation must not release a reservation."""
        self.seed()
        connection = connect(self.database)
        try:
            record_cost(connection, source="reserved", provider="paid", bill="paid", usd=1.0,
                        call_id="dead", owner_pid=2**22 + os.getpid())
            self.assertEqual(exposure(connection, bill="paid"), 1.0)
        finally:
            connection.close()
        result = self.review(FakeClient(default=usage_answer()), explain=True)
        self.assertIn("paid", result["capped_bills"])
        connection = connect(self.database, write=False)
        try:
            self.assertEqual(exposure(connection, bill="paid"), 1.0)
        finally:
            connection.close()
        result = self.review(FakeClient(default=usage_answer()))
        self.assertEqual(result["capped_bills"], {})


class TheConcurrentPair(LedgerFixture):
    def test_two_calls_against_room_for_one_one_goes_and_one_is_refused(self) -> None:
        """Two threads, one connection each, opened inside `run_provider`.
        The first on the wire holds its reservation until the other has been
        answered, so the second sees a `sending` row and is refused; the
        settled rows sum under the cap."""
        self.seed()
        self.spend(0.7)  # room for one bound of a little over 0.20, not two
        released = threading.Event()
        outcomes: list[Any] = []

        def held(provider: Any) -> tuple[int, str, str, bool, int]:
            released.wait(timeout=10)
            return usage_answer(10, 10)

        client = FakeClient({"paid": held})
        registry = sd_review.sd_registry.read_file(self.registry_path)
        ledger = sd_review.open_ledger(self.environment(), None, registry)
        subject = sd_review.resolve_subject(self.root, "worktree")

        def attempt() -> None:
            outcomes.append(sd_review.run_provider(
                registry.providers["paid"], self.root, subject, "review", self.runner,
                self.environment(REMOTE_KEY="fixture"), 5, client=client, ledger=ledger))

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for _ in range(1000):
            if any(outcome.status == sd_review.REFUSED for outcome in outcomes):
                break
            threading.Event().wait(0.01)
        released.set()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(outcome.status for outcome in outcomes), sorted([sd_review.CLEAN, sd_review.REFUSED]))
        refused = next(outcome for outcome in outcomes if outcome.status == sd_review.REFUSED)
        self.assertIn("bill 'paid' has", refused.detail)
        self.assertEqual(len(client.sent), 1)
        settled = [row for row in self.rows() if row[2] == "run"]
        self.assertEqual(len(settled), 2)
        self.assertLess(sum(row[3] for row in settled), 1.0)


class TheLifecycle(LedgerFixture):
    """The row's state at each point, read by the fake wire while it holds
    the request."""
    reviewing_depth = 1  # one charged url call per review

    def state_seen_by(self, answer: Any) -> tuple[list[str], Any]:
        seen: list[str] = []

        def wire(provider: Any) -> Any:
            connection = connect(self.database, write=False)
            try:
                seen.extend(row[0] for row in connection.execute("SELECT source FROM cost"))
            finally:
                connection.close()
            return answer

        client = FakeClient({"paid": wire})
        result = self.review(client)
        return seen, result["outcomes"][0]

    def test_the_row_is_sending_while_the_request_is_out_and_run_at_the_usage_after(self) -> None:
        self.seed()
        seen, outcome = self.state_seen_by(usage_answer(100, 200))
        self.assertEqual(seen, ["sending"])
        self.assertEqual(outcome["status"], sd_review.CLEAN)
        self.assertEqual(self.rows(), [("paid", "paid", "run", 100 * 1.0 / 1e6 + 200 * 2.0 / 1e6, 100, 200)])

    def test_a_timeout_binds_the_row_at_the_bound(self) -> None:
        self.seed()
        seen, outcome = self.state_seen_by((1, "", "paid: timed out", False))
        self.assertEqual(seen, ["sending"])
        self.assertEqual(outcome["status"], sd_review.UNAVAILABLE)
        self.assertEqual(outcome["diagnostic"]["ledger"]["outcome"], "bound")
        self.assertIn("response lost: paid: timed out", outcome["diagnostic"]["ledger"]["reason"])
        # The fallthrough then charged `free` on its uncapped bill: a second row.
        paid, free = self.rows()
        self.assertEqual((paid[0], paid[2], paid[3] > 0.2), ("paid", "bound", True))
        self.assertEqual((free[0], free[2]), ("free", "bound"))

    def test_an_answer_without_usage_binds_the_row_and_is_still_read(self) -> None:
        self.seed()
        seen, outcome = self.state_seen_by(chat_answer('{"findings": []}'))
        self.assertEqual(outcome["status"], sd_review.CLEAN)
        self.assertEqual([row[2] for row in self.rows()], ["bound"])

    def test_a_429_binds_the_row_and_reads_as_a_rate_limit(self) -> None:
        self.seed()
        seen, outcome = self.state_seen_by((429, '{"error": {"message": "slow down"}}', "HTTP 429 from paid", True, 429))
        self.assertEqual(outcome["status"], sd_review.RATE_LIMITED)
        self.assertEqual(outcome["diagnostic"]["http_status"], 429)
        self.assertEqual([(row[0], row[2]) for row in self.rows()], [("paid", "bound"), ("free", "bound")])

    def test_a_refusal_before_the_request_is_built_leaves_no_row(self) -> None:
        """The key variable unset: `refuse_environment` answers before the
        ledger is reached, and the library is not asked."""
        self.seed()
        client = FakeClient(default=usage_answer())
        result = sd_review.review(self.root, namespace(provider="paid"), self.runner, self.environment(), client=client)
        self.assertEqual(result["outcomes"][0]["status"], sd_review.REFUSED)
        self.assertEqual(client.sent, [])
        self.assertEqual(self.rows(), [])

    def test_explain_and_dry_run_touch_no_row(self) -> None:
        self.seed()
        for option in ("explain", "dry_run"):
            with self.subTest(option=option):
                client = FakeClient(default=usage_answer())
                result = self.review(client, **{option: True})
                self.assertEqual(result["status"], "explained" if option == "explain" else "dry_run")
                self.assertEqual(client.sent, [])
                self.assertEqual(self.rows(), [])

    def test_a_failed_check_after_the_review_keeps_the_call_it_charged(self) -> None:
        """sd:2605: the gate runs after the reviewers, so a failing gate does
        not undo the call they made; its row stays, as for any answered call."""
        self.seed()
        self.runner = FakeRunner({"sd-check": sd_review.Completed(1, "{}", "failed")})
        client = FakeClient(default=usage_answer())
        result = self.review(client)
        self.assertEqual(result["status"], "gate_failed")
        self.assertEqual([call["provider"] for call in client.sent], ["paid"])
        self.assertEqual([(row[0], row[2]) for row in self.rows()], [("paid", "run")])


class ThePreflight(LedgerFixture):
    def test_a_preflight_probe_is_charged_and_leaves_one_row(self) -> None:
        self.seed()
        client = FakeClient(default=usage_answer(5, 5))
        result = sd_review.review(self.root, namespace(preflight=True, provider="paid", scope="branch"),
                                  self.runner, self.environment(REMOTE_KEY="fixture"), client=client)
        self.assertEqual(result["status"], "preflight_passed")
        self.assertEqual(result["probe_calls"], 1)
        self.assertEqual([row[2] for row in self.rows()], ["run"])

    def test_a_preflight_probe_on_an_at_cap_bill_is_refused_before_the_wire(self) -> None:
        self.seed()
        self.spend(1.0)
        client = FakeClient(default=usage_answer())
        with self.assertRaises(sd_review.sd_registry.ConsentRefusal):
            sd_review.review(self.root, namespace(preflight=True, provider="paid", scope="branch"),
                             self.runner, self.environment(REMOTE_KEY="fixture"), client=client)
        self.assertEqual(client.sent, [])
        self.assertEqual([row[2] for row in self.rows()], ["run"])


class TheBoundaries(LedgerFixture):
    """The library refusing to import and the database refusing to open,
    each with an uncapped bill (dispatched as before) and a capped one
    (refused naming the fault). No test bypasses the cap or blocks an
    uncapped review."""
    reviewing_depth = 1  # one charged url call per review

    def no_library(self) -> Any:
        absent = sd_review.sd_lib.Imported(None, "sd_db is not installed in this virtualenv", "")
        return mock.patch.object(sd_review.sd_lib, "import_sd_db", return_value=absent)

    def test_no_library_dispatches_the_uncapped_bill_through_the_seam(self) -> None:
        client = FakeClient(default=usage_answer())
        with self.no_library():
            result = self.review(client, provider="free")
        self.assertEqual(result["reviewed_by"], ["free"])
        self.assertEqual([row["provider"] for row in client.sent], ["free"])
        self.assertIsNone(result["outcomes"][0]["diagnostic"]["ledger"])
        self.assertIn("no library to hold the reservation: sd_db is not installed", result["ledger_fault"])

    def test_no_library_refuses_the_capped_bill_naming_the_fault(self) -> None:
        client = FakeClient(default=usage_answer())
        with self.no_library():
            with self.assertRaises(sd_review.sd_registry.ConsentRefusal) as caught:
                self.review(client, provider="paid")
            result = self.review(client)
        self.assertIn("paid is billed to paid: no library to hold the reservation: sd_db is not installed", str(caught.exception))
        self.assertEqual(result["reviewed_by"], ["free"])
        self.assertEqual([row["provider"] for row in client.sent], ["free"])
        self.assertEqual(result["capped_bills"], {"paid": result["ledger_fault"]})

    def test_no_database_dispatches_the_uncapped_bill_through_the_seam(self) -> None:
        """A fresh machine: the library imports and there is no `sd.db`."""
        client = FakeClient(default=usage_answer())
        result = self.review(client, provider="free")
        self.assertEqual(result["reviewed_by"], ["free"])
        self.assertEqual([row["provider"] for row in client.sent], ["free"])
        self.assertFalse(self.database.exists())
        self.assertEqual(result["ledger_fault"], "")

    def test_no_database_refuses_the_capped_bill_naming_the_fault(self) -> None:
        client = FakeClient(default=usage_answer())
        with self.assertRaises(sd_review.sd_registry.ConsentRefusal) as caught:
            self.review(client, provider="paid")
        self.assertIn(f"paid is billed to paid: no database to hold the reservation: no database at {self.database}",
                      str(caught.exception))
        result = self.review(client)
        self.assertEqual(result["reviewed_by"], ["free"])
        self.assertEqual([row["provider"] for row in client.sent], ["free"])
        self.assertFalse(self.database.exists())

    def test_a_database_that_will_not_open_refuses_the_capped_bill_and_not_the_uncapped(self) -> None:
        """`--database` naming a file that is not a database. Through
        `review` the registry read refuses it first, as it did; the ledger's
        own leg is reached through `open_ledger` with that path."""
        self.database.write_text("not a database\n")
        client = FakeClient(default=usage_answer())
        with self.assertRaises(sd_review.sd_registry.RegistryError) as caught:
            self.review(client, provider="paid", database=self.database)
        self.assertIn("cannot read provider state", str(caught.exception))
        self.assertEqual(client.sent, [])
        registry = sd_review.sd_registry.read_file(self.registry_path)
        ledger = sd_review.open_ledger(self.environment(), self.database, registry)
        self.assertEqual(ledger.fault, "")
        capped = sd_review.capped_bills(registry, ledger)
        self.assertEqual(list(capped), ["paid"])
        self.assertIn("no database to hold the reservation: ", capped["paid"])
        env = self.environment(REMOTE_KEY="fixture")
        charged = sd_review.charged_call(ledger, registry.providers["free"], "prompt", env, 5, client, self.root)
        self.assertIsNone(charged)
        charged = sd_review.charged_call(ledger, registry.providers["paid"], "prompt", env, 5, client, self.root)
        self.assertEqual((charged.backend, charged.status, charged.detail), ("paid", sd_review.REFUSED, capped["paid"]))
        self.assertEqual(client.sent, [])

    def test_a_locked_database_refuses_the_charged_calls_and_raises_nothing(self) -> None:
        """pack sd:5955. Another writer held the shared database past the
        connection's busy timeout; `release_orphans` ended the run in a
        traceback. A locked ledger is a fault: the capped bill and both
        charged calls refuse, naming it, and nothing is sent."""
        self.seed()
        registry = sd_review.sd_registry.read_file(self.registry_path)
        ledger = sd_review.open_ledger(self.environment(), self.database, registry)
        holder = sqlite3.connect(self.database, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        brief = mock.patch.object(ledger.library, "connect",
                                  lambda *a, real=ledger.library.connect, **k: real(*a, **{**k, "busy_timeout": 50}))
        client = FakeClient(default=usage_answer())
        env = self.environment(REMOTE_KEY="fixture")
        with brief:
            capped = sd_review.capped_bills(registry, ledger)
            self.assertEqual(list(capped), ["paid"])
            self.assertIn("database is locked", capped["paid"])
            for name in ("paid", "free"):
                charged = sd_review.charged_call(ledger, registry.providers[name], "prompt", env, 5, client, self.root)
                self.assertEqual((charged.backend, charged.status), (name, sd_review.REFUSED))
                self.assertIn("database is locked", charged.detail)
        self.assertEqual(client.sent, [])

    def test_a_library_older_than_calls_is_a_fault_not_a_crash(self) -> None:
        """The pack's provisioned copy may predate sd:234's 8b: a library
        without `sd_db.calls` names itself and the capped bill is refused."""
        client = FakeClient(default=usage_answer())
        real = __import__("builtins").__import__

        def without_calls(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "sd_db" and args and args[2] and "calls" in args[2]:
                raise ImportError("cannot import name 'calls' from 'sd_db'")
            return real(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=without_calls):
            result = self.review(client)
        self.assertIn("no library to hold the reservation: cannot import name 'calls'", result["ledger_fault"])
        self.assertEqual(result["reviewed_by"], ["free"])
        self.assertEqual(result["capped_bills"], {"paid": result["ledger_fault"]})


class OverTheWire(LedgerFixture):
    """sd:2679. On a satellite the ledger refuses every reservation, naming the
    hub (`LedgerRefused`); each `url` lane reports refused and a CLI lane runs."""

    def test_url_lanes_report_refused_and_the_cli_lane_still_runs(self) -> None:
        from sd_db import calls
        from sd_db.ledger import LedgerRefused
        self.registry_path.write_text(REGISTRY.replace(
            "  free:", "  claude: { start: 'claude exec', vendor: anthropic, bill: fixture, roles: [reviewer], "
            "reader: claude-json }\n  free:").replace("[paid, free]", "[paid, free, claude]"), encoding="utf-8")
        (self.root / "CLAUDE.local.md").write_text(
            "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\n"
            "reviewers: paid@paid.example.test, free@free.example.test, claude@claude\n"
            "<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n")
        self.seed()
        self.runner.default = sd_review.Completed(
            0, json.dumps({"type": "result", "subtype": "success", "structured_output": {"findings": []}}), "")
        hub = "provider calls are charged on the sd hub only; this machine reaches the database on hub.example.test:8769"
        client = FakeClient(default=usage_answer())
        with mock.patch.object(calls, "call", side_effect=LedgerRefused(hub, scope="hub")):
            result = self.review(client)
        self.assertEqual([(row["backend"], row["status"]) for row in result["outcomes"]],
                         [("paid", sd_review.REFUSED), ("free", sd_review.REFUSED), ("claude", sd_review.CLEAN)])
        self.assertIn(hub, result["outcomes"][0]["detail"])
        self.assertEqual(result["reviewed_by"], ["claude"])
        self.assertEqual(client.sent, [])


class TheShippedBounds(unittest.TestCase):
    """sd:1805 raised the shipped reviewers' `max_tokens`; the reservation
    must move with it, or a cap is held against a bound the call can exceed.

    Priced at the prompt that ran out of room on example-simulator#215:
    131600 bytes, estimated at the library's `BYTES_PER_TOKEN` (sd:2935 made
    it 3, so 43866 tokens; it was 4, 32900 tokens).
    """

    PROMPT = "x" * 131600

    def test_each_bound_reserves_the_whole_new_max_tokens(self) -> None:
        registry = read_registry(SHIPPED)
        entries = {name: entry for name, entry in registry.providers.items() if entry.url}
        self.assertEqual(sorted(entries), ["baseten", "kimi", "minimax"])
        tokens = len(self.PROMPT) // BYTES_PER_TOKEN
        expected = {"kimi": (tokens * 3.00 + 65536 * 15.00) / 1e6, "minimax": 0.0,
                    "baseten": (tokens * 1.32 + 65536 * 3.96) / 1e6}
        for name, entry in entries.items():
            with self.subTest(provider=name):
                self.assertAlmostEqual(bound_for(entry, self.PROMPT), expected[name], places=9)
        cap = registry.bills["baseten"].cap_usd_month
        self.assertLess(bound_for(entries["baseten"], self.PROMPT), cap)


class TheSeamStays(LedgerFixture):
    """A direct caller with no ledger, which is every existing test of
    `run_provider`, dispatches through the injectable client as before."""

    def test_run_provider_without_a_ledger_uses_the_client(self) -> None:
        provider = sd_review.sd_registry.Provider(name="url", vendor="fixture", bill="paid",
                                                  url="https://fixture.example/v1", env=("OWN_KEY",))
        client = FakeClient(default=usage_answer())
        outcome = sd_review.run_provider(provider, self.tmp, sd_review.Subject("worktree", "HEAD", "worktree", (), 0, ""),
                                         "prompt", FakeRunner(), {"OWN_KEY": "k"}, 7, client=client)
        self.assertEqual(outcome.status, sd_review.CLEAN)
        self.assertEqual(len(client.sent), 1)
        self.assertIsNone(outcome.diagnostic["ledger"])
        self.assertFalse(self.database.exists())

    def test_the_wire_is_what_the_fake_client_answers(self) -> None:
        """The library builds the request; the fake answers it with what it
        answered before, and a body over the byte limit is still read as
        the response limit."""
        self.seed()
        client = FakeClient(default=(1, "x" * (sd_review.MAX_OUTPUT_BYTES + 1), "response exceeds the declared byte limit", True, 200))
        result = self.review(client, provider="paid")
        outcome = result["outcomes"][0]
        self.assertEqual(outcome["diagnostic"]["category"], "response_limit")
        self.assertEqual(outcome["diagnostic"]["ledger"]["outcome"], "bound")
        self.assertEqual(client.sent[0]["provider"], "paid")
        self.assertIn("Schema:", client.sent[0]["prompt"])
        self.assertEqual(client.sent[0]["env"]["REMOTE_KEY"], "fixture")


class TheStrictSchema(LedgerFixture):
    """sd:1827: an entry with `response_format: json_schema` hands both roads
    the Moonshot-flavoured findings schema; any other entry asks as before."""

    #: Every keyword `charged_call` handed `calls.call` before sd:1827.
    TODAY = {"entry", "prompt", "environ", "timeout", "role", "repo", "transport"}
    #: What an opted-in entry is held to: the copy Moonshot's strict mode takes.
    STRICT = sd_review.sd_registry.strict_schema(sd_review.CODEX_OUTPUT_SCHEMA)

    def provider(self, **fields: Any) -> Any:
        return sd_review.sd_registry.Provider(name="url", vendor="fixture", bill="paid", model="fixture",
                                              max_tokens=100, url="https://fixture.example.test/v1",
                                              env=("OWN_KEY",), **fields)

    def library(self, provider_class: type) -> tuple[Any, list[dict[str, Any]]]:
        """A library whose `calls.call` records what it was handed."""
        handed: list[dict[str, Any]] = []

        def call(connection: Any, **keywords: Any) -> Any:
            handed.append(keywords)
            return types.SimpleNamespace(call_id=1, outcome="run", bound=0.0, usd=0.0, reason="",
                                         http_status=200, body=usage_answer()[1])
        refused = type("Refused", (Exception,), {})
        library = types.SimpleNamespace(Provider=provider_class, LedgerRefused=refused,
            calls=types.SimpleNamespace(call=call, CallRefused=refused),
            connect=lambda database, write: sqlite3.connect(":memory:"))
        return sd_review.Ledger(library, self.database, frozenset(), ""), handed

    def charged(self, provider: Any, provider_class: type) -> tuple[Any, list[dict[str, Any]]]:
        ledger, handed = self.library(provider_class)
        return sd_review.charged_call(ledger, provider, "prompt", {"OWN_KEY": "k"}, 7, None, self.root), handed

    def test_the_url_schema_is_typed_has_no_type_arrays_and_drops_what_strict_mode_rejects(self) -> None:
        def walk(schema: Any) -> list[Any]:
            found = [schema]
            for key, value in schema.items():
                if key == "properties":
                    for sub in value.values():
                        found += walk(sub)
                elif isinstance(value, dict):
                    found += walk(value)
                elif key == "anyOf":
                    for sub in value:
                        found += walk(sub)
            return found
        schemas = walk(self.STRICT)
        for schema in schemas:
            with self.subTest(schema=schema):
                self.assertTrue(isinstance(schema.get("type"), str) or "anyOf" in schema)
                self.assertFalse({"minLength", "maxItems"} & set(schema))
        finding = self.STRICT["properties"]["findings"]["items"]["properties"]
        self.assertEqual(finding["line"], {"anyOf": [{"type": "integer"}, {"type": "null"}]})
        self.assertEqual(finding["severity"], {"type": "string", "enum": list(sd_review.SEVERITIES)})
        self.assertEqual(sd_review.CODEX_OUTPUT_SCHEMA["properties"]["findings"]["maxItems"], sd_review.MAX_FINDINGS)

    def test_an_opted_in_entry_hands_the_library_the_strict_schema(self) -> None:
        fields = [field.name for field in dataclasses.fields(sd_review.sd_registry.Provider)]
        current = dataclasses.make_dataclass("Current", [(name, Any, dataclasses.field(default=None)) for name in fields])
        response, handed = self.charged(self.provider(response_format="json_schema"), current)
        self.assertEqual(response[0][0], 0)
        self.assertEqual(handed[0]["entry"].response_format, "json_schema")
        self.assertEqual(handed[0]["response_schema"], self.STRICT)
        self.assertEqual(handed[0]["schema_name"], "findings")
        self.assertEqual(set(handed[0]), self.TODAY | {"response_schema", "schema_name"})

    def test_any_other_entry_hands_the_library_exactly_todays_call(self) -> None:
        """Even to a library whose `Provider` has no `response_format`."""
        fields = [field.name for field in dataclasses.fields(sd_review.sd_registry.Provider) if field.name != "response_format"]
        older = dataclasses.make_dataclass("Older", [(name, Any, dataclasses.field(default=None)) for name in fields])
        response, handed = self.charged(self.provider(), older)
        self.assertEqual(response[0][0], 0)
        self.assertEqual(set(handed[0]), self.TODAY)
        refused, handed = self.charged(self.provider(response_format="json_schema"), older)
        self.assertEqual(refused.status, sd_review.REFUSED)
        self.assertIn("cannot send response_format", refused.detail)
        self.assertEqual(handed, [])

    def sent(self, provider: Any) -> dict[str, Any]:
        with mock.patch.object(sd_review.sd_registry._OPENER, "open", return_value=_Answer(usage_answer()[1])) as opened:
            outcome = sd_review.run_provider(provider, self.root, sd_review.Subject("worktree", "HEAD", "worktree", (), 0, ""),
                                             "prompt", FakeRunner(), {"OWN_KEY": "k"}, 7)
        self.assertEqual(outcome.status, sd_review.CLEAN)
        return json.loads(opened.call_args.args[0].data)

    def test_without_a_ledger_an_opted_in_entry_sends_the_strict_body(self) -> None:
        body = self.sent(self.provider(response_format="json_schema"))
        self.assertEqual(body["response_format"], {"type": "json_schema", "json_schema": {
            "name": "findings", "strict": True, "schema": self.STRICT}})

    def test_without_a_ledger_any_other_entry_sends_exactly_todays_body(self) -> None:
        self.assertEqual(set(self.sent(self.provider())), {"model", "max_tokens", "messages"})
