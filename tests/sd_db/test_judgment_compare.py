"""The comparison arms (sd:2366): a `kev` and a `haiku` row beside each Jev
call, what they may hold, and the per-arm report `judgments compare` prints.

The arms only record. Their rows share the Jev row's pair id, so agreement and
accuracy are computed here, at read time, from rows that hold numbers only.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sd_db import connect
from sd_db.judgment import (
    ARMS,
    JudgmentRefused,
    by_stage,
    compare,
    compare_json,
    compare_text,
    label,
    record,
    unlabelled,
)
from sd_db.migrate import initialise, migrate
from sd_db.schema import SCHEMA_DIR

AT = "2026-10-01T12:00:00+00:00"


class CompareCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)

    def write(self, **extra):
        fields = dict(caller="local-notify", stage="JEV_NOTIFY",
                      provider="typesafe", primitive="noul", outcome="ok",
                      now=AT)
        fields.update(extra)
        return record(self.connection, **fields)

    def row(self, row_id):
        return self.connection.execute(
            "SELECT * FROM judgment WHERE id = ?", (row_id,)).fetchone()


class TheArms(CompareCase):
    def test_the_two_comparison_arms_are_accepted(self):
        self.assertEqual(ARMS, ("jev", "baseline", "kev", "haiku"))
        kev = self.write(arm="kev", provider="local", model="jaredpalmer/kev-4b@v1.0",
                         pair="p1", usd=0.0)
        haiku = self.write(arm="haiku", provider="openrouter",
                           model="anthropic/claude-haiku-4.5", pair="p1")
        self.assertEqual(self.row(kev)["arm"], "kev")
        self.assertEqual(self.row(haiku)["provider"], "openrouter")

    def test_the_database_refuses_an_arm_the_library_does_not_name(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.connection:
                self.connection.execute(
                    "INSERT INTO judgment (timestamp, caller, stage, arm, provider, "
                    "primitive, outcome) VALUES (?, 'c', 's', 'gpt', 'p', 'noul', 'ok')",
                    (AT,))

    def test_server_latency_and_probabilities_are_stored(self):
        row = self.row(self.write(primitive="choice", answer="1", server_ms=495,
                                  probabilities="0.47,0.28,0.25"))
        self.assertEqual(row["server_ms"], 495)
        self.assertEqual(row["probabilities"], "0.47,0.28,0.25")

    def test_a_distribution_over_every_option_kev_supports_is_stored(self):
        # Kev's API takes up to 255 options; 0.0000 for each is 1784 characters.
        value = ",".join(["0.0000"] * 254 + ["1.0000"])
        row = self.row(self.write(primitive="choice", answer="255", probabilities=value))
        self.assertEqual(row["probabilities"], value)

    def test_a_distribution_over_more_options_than_kev_supports_is_refused(self):
        with self.assertRaises(JudgmentRefused):
            self.write(probabilities=",".join(["0"] * 256))

    def test_the_labelling_queue_holds_no_comparison_arm_row(self):
        jev_row = self.write(arm="jev", pair="p1", answer="0.9", question_id="q1")
        base = self.write(arm="baseline", provider="local", answer="1", question_id="q2")
        self.write(arm="kev", provider="local", pair="p1", answer="0.8", question_id="q1")
        self.write(arm="haiku", provider="anthropic", pair="p1", answer="0.7",
                   question_id="q1")
        found = unlabelled(self.connection, "JEV_NOTIFY")
        self.assertEqual(sorted(row["id"] for row in found), sorted([jev_row, base]))

    def test_a_label_on_a_comparison_arm_row_is_refused(self):
        self.write(arm="jev", pair="p1", answer="0.9")
        for arm, provider in (("kev", "local"), ("haiku", "anthropic")):
            row = self.write(arm=arm, provider=provider, pair="p1", answer="0.8")
            with self.subTest(arm=arm), self.assertRaises(JudgmentRefused):
                label(self.connection, row, "1", "test-rule")
            self.assertIsNone(self.row(row)["override"])

    def test_a_cost_that_is_not_finite_is_refused(self):
        for value in (float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(JudgmentRefused):
                self.write(usd=value)

    def test_a_choice_label_must_be_a_whole_position(self):
        row = self.write(arm="jev", primitive="choice", answer="2")
        for value in ("1.6", "0", "2.5"):
            with self.subTest(value=value), self.assertRaises(JudgmentRefused):
                label(self.connection, row, value, "test-rule")
        self.assertTrue(label(self.connection, row, "2", "test-rule"))
        score = self.write(arm="jev", primitive="score", answer="1.6")
        self.assertTrue(label(self.connection, score, "1.6", "test-rule"))

    def test_a_probability_outside_zero_to_one_is_refused(self):
        for value in ("9" * 4000, "1.5,0", "0.5,2", "1" + "0" * 400):
            with self.subTest(value=value[:12]), self.assertRaises(JudgmentRefused):
                self.write(probabilities=value)
        self.write(probabilities="0,1,0.5")

    def test_probabilities_that_are_not_numbers_are_refused(self):
        for value in ("returns,shipping", "0.5;0.5", "0.5, 0.5", "/home/x", ""):
            with self.subTest(value=value), self.assertRaises(JudgmentRefused):
                self.write(probabilities=value)

    def test_server_latency_must_be_a_count(self):
        with self.assertRaises(JudgmentRefused):
            self.write(server_ms=-1)

    def test_the_old_report_reads_only_jev_and_baseline(self):
        self.write(pair="p1", answer="0.9")
        self.write(arm="baseline", provider="local", primitive="baseline", pair="p1")
        self.write(arm="kev", provider="local", pair="p1", answer="0.8")
        self.write(arm="haiku", provider="anthropic", pair="p1", answer="0.7")
        stages = by_stage(self.connection)
        self.assertEqual(sorted(stages[0]["arms"]), ["baseline", "jev"])
        # One decision with both of the old arms is one paired sample, still.
        self.assertEqual(stages[0]["paired"], 1)


class TheMigration(unittest.TestCase):
    def test_rows_written_at_16_survive_017_with_their_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sd.db"
            initialise(path)
            raw = sqlite3.connect(path, isolation_level=None)
            # 049, 048, 046, 044, 043, 042, 041, 040, 039, 038, 034 and 026 to 018 came after 017 and are reversed first: their `ADD
            # COLUMN` and `CREATE TABLE` do not replay (pack sd:6711, pack sd:6046, pack sd:6093, pack sd:5648, sd:5496, sd:5357,
            # sd:5303, sd:5315, sd:4636, sd:3231, sd:3217, sd:3075, sd:2973, sd:2966, sd:2950, sd:2704, sd:1335,
            # sd:2581).
            # 049 and 048 drop their tables (pack sd:8053, pack sd:8050); then 046: 032's guard refuses the narrowed kind list (sd:7004).
            for name in ("049_home_asset.sql", "048_vote.sql", "046_item_retire_personal_skill_review.sql", "044_shadow_merge_state.sql", "043_note_harness_session.sql", "042_runner_run_build.sql", "041_recurrence_lead.sql", "040_shadow_filed.sql", "039_shadow_updated.sql", "038_shadow_settled.sql", "034_shadow_reason.sql", "026_repo_drop_status_source.sql", "025_repo_drop_satellite_gate.sql", "024_repo_lane_host.sql", "023_judgment_error.sql", "022_judgment_batch_children.sql",
                         "021_judgment_call_context.sql", "020_repo_satellite_gate.sql",
                         "019_request_outcome.sql",
                         "018_runner_run_repo_nullable.sql", "017_judgment_compare_arms.sql"):
                text = (SCHEMA_DIR / name).read_text()
                raw.executescript("\n".join(line[4:] for line in text.splitlines()
                                            if line.startswith("--   ")))
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 16)
            raw.execute(
                "INSERT INTO judgment (id, timestamp, caller, stage, arm, provider, "
                "primitive, outcome, answer) "
                "VALUES (7, ?, 'c', 's', 'jev', 'typesafe', 'noul', 'ok', '0.4')", (AT,))
            raw.close()
            migrate(path)
            connection = connect(path)
            try:
                row = connection.execute("SELECT * FROM judgment WHERE id = 7").fetchone()
                self.assertEqual(row["answer"], "0.4")
                self.assertIsNone(row["server_ms"])
                record(connection, caller="c", stage="s", provider="local",
                       primitive="noul", outcome="ok", arm="kev", now=AT)
            finally:
                connection.close()

    def test_a_database_before_017_has_no_comparison_rows(self):
        # A reader may open a schema 11-16 database it does not migrate.
        # That table has no comparison columns and no kev or haiku rows.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sd.db"
            initialise(path)
            raw = sqlite3.connect(path, isolation_level=None)
            text = (SCHEMA_DIR / "017_judgment_compare_arms.sql").read_text()
            raw.executescript("\n".join(line[4:] for line in text.splitlines()
                                        if line.startswith("--   ")))
            raw.execute(
                "INSERT INTO judgment (id, timestamp, caller, stage, arm, provider, "
                "primitive, outcome, answer) "
                "VALUES (7, ?, 'c', 's', 'jev', 'typesafe', 'noul', 'ok', '0.4')", (AT,))
            raw.row_factory = sqlite3.Row
            try:
                self.assertEqual(compare(raw), [])
            finally:
                raw.close()


class TheComparison(CompareCase):
    """Three noul decisions and one choice, each asked of all three arms."""

    def decision(self, pair, jev, kev, haiku, primitive="noul", probabilities=None,
                 ms=(100, 700, 1500)):
        probabilities = probabilities or (None, None, None)
        ids = []
        for arm, provider, answer, p, duration in (
                ("jev", "typesafe", jev, probabilities[0], ms[0]),
                ("kev", "local", kev, probabilities[1], ms[1]),
                ("haiku", "anthropic", haiku, probabilities[2], ms[2])):
            ids.append(self.write(arm=arm, provider=provider, pair=pair,
                                  primitive=primitive, answer=answer,
                                  probabilities=p, duration_ms=duration,
                                  tokens_in=10, tokens_out=2,
                                  usd=0.0 if arm == "kev" else (0.001 if arm == "haiku" else None),
                                  server_ms=50 if arm == "kev" else None))
        return ids

    def arm(self, report, name, provider=None):
        for entry in report[0]["arms"]:
            if entry["arm"] == name and (provider is None or entry["provider"] == provider):
                return entry
        self.fail(f"no {name} arm in {report}")

    def test_agreement_is_measured_against_the_jev_row_of_the_same_pair(self):
        self.decision("a", "0.9", "0.8", "0.3")   # kev agrees, haiku does not
        self.decision("b", "0.2", "0.1", "0.4")   # both agree
        self.decision("c", "0.6", "0.4", "0.7")   # haiku agrees
        report = compare(self.connection)
        kev, haiku = self.arm(report, "kev"), self.arm(report, "haiku")
        self.assertEqual((kev["paired"], kev["agree"]), (3, 2))
        self.assertEqual((haiku["paired"], haiku["agree"]), (3, 2))
        self.assertAlmostEqual(kev["mean_abs_dp"], (0.1 + 0.1 + 0.2) / 3)
        self.assertAlmostEqual(haiku["mean_abs_dp"], (0.6 + 0.2 + 0.1) / 3)
        self.assertIsNone(self.arm(report, "jev")["agree"])

    def test_latency_percentiles_tokens_and_cost(self):
        self.decision("a", "0.9", "0.8", "0.3", ms=(100, 600, 1000))
        self.decision("b", "0.9", "0.8", "0.3", ms=(200, 700, 2000))
        self.decision("c", "0.9", "0.8", "0.3", ms=(300, 800, 9000))
        report = compare(self.connection)
        haiku = self.arm(report, "haiku")
        self.assertEqual(haiku["calls"], 3)
        self.assertEqual(haiku["p50_ms"], 2000)
        self.assertEqual(haiku["p95_ms"], 9000)
        self.assertEqual((haiku["tokens_in"], haiku["tokens_out"]), (30, 6))
        self.assertAlmostEqual(haiku["usd"], 0.003)
        kev = self.arm(report, "kev")
        self.assertEqual(kev["usd"], 0.0)
        self.assertEqual(kev["server_p50_ms"], 50)

    def test_a_partial_cost_total_names_its_unpriced_calls(self):
        # sd:2980: one haiku call carries no cost, so $0.0020 is a floor, not
        # a total; `judgments` already says so per arm, and now compare does.
        self.decision("a", "0.9", "0.8", "0.3")
        self.decision("b", "0.9", "0.8", "0.3")
        self.write(arm="haiku", provider="anthropic", pair="c", answer="0.3",
                   tokens_in=10, tokens_out=2)
        self.write(arm="haiku", provider="anthropic", outcome="fallback",
                   cause="timeout", duration_ms=60000)
        report = compare(self.connection)
        haiku, kev = self.arm(report, "haiku"), self.arm(report, "kev")
        self.assertEqual((haiku["calls"], haiku["unpriced"]), (4, 1))
        self.assertAlmostEqual(haiku["usd"], 0.002)
        self.assertEqual(kev["unpriced"], 0)
        printed = compare_text(report)
        self.assertIn("cost $0.0020 (1 unpriced)", printed)
        self.assertIn("cost $0.0000,", printed)
        self.assertEqual(json.loads(compare_json(report))["stages"][0]["arms"][2]["unpriced"], 1)

    def test_a_batch_compares_question_by_question_and_counts_one_call(self):
        # sd:2966: one batched call per arm, two questions, the same pair.
        for arm, provider, risky, tier in (("jev", "typesafe", "0.9", "2"),
                                           ("kev", "local", "0.8", "1"),
                                           ("haiku", "anthropic", "0.3", "2")):
            self.write(arm=arm, provider=provider, pair="b", primitive="ask",
                       questions=2, tokens_in=10, tokens_out=2, duration_ms=500,
                       answers=[{"id": "risky", "primitive": "noul", "answer": risky},
                                {"id": "tier", "primitive": "choice", "answer": tier}])
        report = compare(self.connection)
        kev, haiku = self.arm(report, "kev"), self.arm(report, "haiku")
        self.assertEqual((kev["paired"], kev["agree"]), (2, 1))
        self.assertEqual((haiku["paired"], haiku["agree"]), (2, 1))
        for arm in ("jev", "kev", "haiku"):
            entry = self.arm(report, arm)
            self.assertEqual((entry["calls"], entry["tokens_in"]), (1, 10), arm)

    def test_a_choice_agrees_on_the_position_that_won(self):
        self.decision("a", "2", "2", "1", primitive="choice")
        report = compare(self.connection)
        self.assertEqual(self.arm(report, "kev")["agree"], 1)
        self.assertEqual(self.arm(report, "haiku")["agree"], 0)
        self.assertIsNone(self.arm(report, "kev")["mean_abs_dp"])

    def test_only_the_jev_row_carries_the_label_of_a_pair(self):
        # A label that reached an arm row some other way is not the pair's truth.
        _, kev, _ = self.decision("a", "0.9", "0.8", "0.3")
        with self.connection:
            self.connection.execute(
                "UPDATE judgment SET override = '0', override_source = 'test-rule' "
                "WHERE id = ?", (kev,))
        report = compare(self.connection)
        for name in ("kev", "haiku"):
            self.assertEqual(self.arm(report, name)["labelled"], 0)

    def test_a_fractional_choice_answer_is_not_a_position(self):
        jev, _, _ = self.decision("a", "1", "1.6", "1", primitive="choice")
        label(self.connection, jev, "1", "test-rule")
        report = compare(self.connection)
        kev = self.arm(report, "kev")
        self.assertEqual((kev["paired"], kev["labelled"]), (0, 0))
        self.assertEqual(self.arm(report, "haiku")["paired"], 1)

    def test_a_label_outside_the_distribution_has_no_brier_score(self):
        jev, _, _ = self.decision("a", "1", "1", "1", primitive="choice",
                                  probabilities=("0.5,0.3,0.2",) * 3)
        label(self.connection, jev, "4", "test-rule")
        report = compare(self.connection)
        kev = self.arm(report, "kev")
        self.assertEqual((kev["labelled"], kev["right"]), (1, 0))
        self.assertIsNone(kev["brier"])

    def test_a_fractional_choice_label_from_before_the_check_is_ignored(self):
        jev, _, _ = self.decision("a", "2", "2", "1", primitive="choice")
        with self.connection:
            self.connection.execute(
                "UPDATE judgment SET override = '1.6', override_source = 'test-rule' "
                "WHERE id = ?", (jev,))
        report = compare(self.connection)
        for name in ("jev", "kev", "haiku"):
            self.assertEqual(self.arm(report, name)["labelled"], 0)

    def test_a_score_agrees_and_is_right_on_the_recorded_score_not_the_mode(self):
        # Kev records 2.0, its expected level; its most likely level is 0.
        jev, _, _ = self.decision(
            "a", "2", "2", "0", primitive="score",
            probabilities=(None, "0.4,0.0,0.35,0.25", None))
        label(self.connection, jev, "2", "test-rule")
        report = compare(self.connection)
        kev = self.arm(report, "kev")
        self.assertEqual((kev["paired"], kev["agree"]), (1, 1))
        self.assertEqual((kev["labelled"], kev["right"]), (1, 1))
        # The distribution still scores Brier: 0.4^2 + 0.65^2 + 0.25^2.
        self.assertAlmostEqual(kev["brier"], 0.16 + 0.4225 + 0.0625)

    def test_accuracy_and_brier_read_the_label_on_the_jev_row(self):
        jev, _, _ = self.decision("a", "0.9", "0.8", "0.3")
        label(self.connection, jev, "1", "test-rule")
        jev, _, _ = self.decision(
            "b", "1", "1", "2", primitive="choice",
            probabilities=("0.6,0.4", "0.9,0.1", "0.2,0.8"))
        label(self.connection, jev, "1", "test-rule")
        report = compare(self.connection)
        kev, haiku = self.arm(report, "kev"), self.arm(report, "haiku")
        self.assertEqual((kev["labelled"], kev["right"]), (2, 2))
        self.assertEqual((haiku["labelled"], haiku["right"]), (2, 0))
        # Noul: (0.8 - 1)^2 = 0.04; choice: (0.9-1)^2 + 0.1^2 = 0.02.
        self.assertAlmostEqual(kev["brier"], (0.04 + 0.02) / 2)
        # Noul: 0.7^2 = 0.49; choice: 0.8^2 + 0.8^2 = 1.28.
        self.assertAlmostEqual(haiku["brier"], (0.49 + 1.28) / 2)

    def test_declines_are_counted_by_cause_and_excluded_from_agreement(self):
        self.decision("a", "0.9", "0.8", "0.3")
        self.write(arm="haiku", provider="anthropic", pair="b", outcome="unavailable",
                   cause="unkeyed")
        report = compare(self.connection)
        haiku = self.arm(report, "haiku")
        self.assertEqual(haiku["calls"], 2)
        self.assertEqual(haiku["ok"], 1)
        self.assertEqual(haiku["declines"], {"unkeyed": 1})
        self.assertEqual(haiku["paired"], 1)

    def test_each_haiku_transport_is_its_own_line(self):
        self.decision("a", "0.9", "0.8", "0.3")
        self.write(arm="haiku", provider="openrouter", pair="a", answer="0.95")
        report = compare(self.connection)
        self.assertEqual(self.arm(report, "haiku", "openrouter")["agree"], 1)
        self.assertEqual(self.arm(report, "haiku", "anthropic")["agree"], 0)

    def test_a_stage_filter_and_the_two_serialisations(self):
        self.decision("a", "0.9", "0.8", "0.3")
        self.write(stage="JEV_OTHER", pair="z", answer="0.5")
        self.assertEqual([s["stage"] for s in compare(self.connection)],
                         ["JEV_NOTIFY", "JEV_OTHER"])
        only = compare(self.connection, stage="JEV_OTHER")
        self.assertEqual([s["stage"] for s in only], ["JEV_OTHER"])
        report = compare(self.connection, stage="JEV_NOTIFY")
        printed = compare_text(report)
        self.assertIn("JEV_NOTIFY", printed)
        self.assertIn("kev", printed)
        self.assertIn("agree", printed)
        parsed = json.loads(compare_json(report))
        self.assertEqual(parsed["stages"][0]["stage"], "JEV_NOTIFY")

    def test_an_empty_ledger_says_so(self):
        self.assertEqual(compare(self.connection), [])
        self.assertIn("no calls", compare_text([]))


class AgainstTheOldMechanism(CompareCase):
    """sd:2761: each arm against the caller's own answer of the same pair, the
    baseline row a shadow call writes beside the judgment."""

    decision = TheComparison.decision
    arm = TheComparison.arm

    def old(self, pair, answer):
        return self.write(arm="baseline", provider="local", primitive="baseline",
                          pair=pair, answer=answer, shadow=True)

    def test_agreement_and_the_disagreement_split(self):
        self.decision("a", "0.9", "0.8", "0.3")
        self.old("a", "0.1")                     # jev and kev say yes, old said no
        self.decision("b", "0.2", "0.7", "0.4")
        self.old("b", "0.3")                     # jev agrees, kev does not
        self.decision("c", "0.6", "0.1", "0.7")
        self.old("c", "0.9")                     # jev agrees, kev does not
        report = compare(self.connection)
        jev, kev = self.arm(report, "jev"), self.arm(report, "kev")
        self.assertEqual((jev["old_pairs"], jev["old_compared"], jev["old_agree"]),
                         (3, 3, 2))
        self.assertAlmostEqual(jev["old_agreement"], 2 / 3)
        self.assertEqual(jev["old_disagreements"], [{"old": 0, "arm": 1, "n": 1}])
        self.assertEqual((kev["old_compared"], kev["old_agree"]), (3, 0))
        self.assertEqual(kev["old_disagreements"],
                         [{"old": 0, "arm": 1, "n": 2}, {"old": 1, "arm": 0, "n": 1}])
        printed = compare_text(report)
        self.assertIn("vs old 2/3 agree (67%), old→arm 0→1 ×1", printed)
        self.assertIn("vs old 0/3 agree (0%), old→arm 0→1 ×2 1→0 ×1", printed)
        parsed = json.loads(compare_json(report))
        self.assertEqual(parsed["stages"][0]["arms"][0]["old_agree"], 2)

    def test_a_choice_compares_positions_and_lists_the_top_five(self):
        for n, (said, was) in enumerate([(2, 1), (3, 1), (4, 1), (5, 1), (6, 1),
                                         (7, 1), (2, 1), (1, 1)]):
            self.decision(f"p{n}", str(said), str(said), str(said), primitive="choice")
            self.old(f"p{n}", str(was))
        jev = self.arm(compare(self.connection), "jev")
        self.assertEqual((jev["old_compared"], jev["old_agree"]), (8, 1))
        self.assertEqual(len(jev["old_disagreements"]), 5)
        self.assertEqual(jev["old_disagreements"][0], {"old": 1, "arm": 2, "n": 2})

    def test_an_old_answer_that_is_not_a_number_is_counted_but_not_compared(self):
        """A `--fallback` marker under the shadow switch records no answer."""
        self.decision("a", "0.9", "0.8", "0.3")
        self.old("a", None)
        report = compare(self.connection)
        jev = self.arm(report, "jev")
        self.assertEqual((jev["old_pairs"], jev["old_compared"]), (1, 0))
        self.assertIsNone(jev["old_agreement"])
        self.assertIn("vs old: no comparable old answer in 1 pair(s)",
                      compare_text(report))

    def test_a_stage_with_no_paired_sample_says_so(self):
        self.decision("a", "0.9", "0.8", "0.3")
        report = compare(self.connection)
        self.assertEqual(self.arm(report, "kev")["old_pairs"], 0)
        printed = compare_text(report)
        self.assertIn("JEV_NOTIFY: no paired samples with the old mechanism", printed)
        self.assertNotIn("vs old", printed)


class TheCompareVerb(CompareCase):
    """`sd-db.sh judgments compare`, through the same CLI entry the shell uses."""

    def cli(self, *argv):
        import contextlib
        import io
        import os
        from unittest import mock

        from sd_db.jobs import cli

        home = Path(self.tmp.name) / "home"
        target = home / ".local/share/sd/sd.db"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(self.path)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_text_json_and_a_stage_filter(self):
        self.write(pair="a", answer="0.9")
        self.write(arm="kev", provider="local", pair="a", answer="0.8")
        self.write(stage="JEV_OTHER", pair="b", answer="0.1")
        code, out, _ = self.cli("judgments", "compare")
        self.assertEqual(code, 0)
        self.assertIn("JEV_NOTIFY", out)
        self.assertIn("agree with jev 1/1", out)
        code, out, _ = self.cli("judgments", "compare", "--stage", "JEV_OTHER", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([s["stage"] for s in json.loads(out)["stages"]], ["JEV_OTHER"])

    def test_an_unknown_flag_is_refused(self):
        code, _, err = self.cli("judgments", "compare", "--bogus")
        self.assertEqual(code, 1)
        self.assertIn("compare", err)


if __name__ == "__main__":
    unittest.main()
