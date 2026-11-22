"""Build runs read from the store for the monthly lane report (pack sd:6947)."""
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from sd_db import build, telemetry
from sd_db.database import connect
from sd_db.jobs import cli
from sd_db.migrate import initialise
from sd_db.writes import create_item, record_cost, upsert_repo

REPO = "/fixture/repo"
SHA = "3d60d444699dfae13cbf9ad68a568c3172b0ab20"


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        initialise(self.root / "sd.db")
        self.db = connect(self.root / "sd.db")
        self.addCleanup(self.db.close)
        upsert_repo(self.db, REPO, remote="/fixture/remote", managed=1)

    def run_row(self, titles, *, outcome, detail, notes, start="2026-10-31T07:22:07+00:00",
                end="2026-10-31T09:02:06+00:00", review_round=None):
        items = [create_item(self.db, kind="task", title=title, repo=REPO, status="ready") for title in titles]
        build.queue(self.db, items, who="operator")
        run = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                          root=self.root / "worktrees")
        assert run is not None
        ident = run["id"]
        self.db.execute("UPDATE runner_run SET created_at = ?, released_at = ?, outcome = ?, detail = ?, "
                        "review_round = ?, model = 'opus' WHERE id = ?",
                        (start, end, outcome, detail, review_round, ident))
        for stamp, body in notes:
            self.db.execute("INSERT INTO note (item, timestamp, kind, body) VALUES (?, ?, 'comment', ?)",
                            (items[0], stamp, body.format(run=ident)))
        self.db.commit()
        return ident, items

    def merged(self, review_round=None):
        return self.run_row(["Build me", "And me"], outcome="done", detail="worktree /Users/x removed",
                            review_round=review_round, notes=[
                                ("2026-10-31T07:34:11+00:00", "Build run {run} report:\nBuilt."),
                                ("2026-10-31T07:50:00+00:00", "Build run {run} report:\nFixed round 1."),
                                ("2026-10-31T08:26:35+00:00",
                                 "Build run {run} enqueued build/sd-1 in the lane as entry 20261031T082558Z-4791"),
                                ("2026-10-31T09:01:01+00:00",
                                 f"Code delivery https://github.com/o/r/pull/1722 at {SHA}")])


class Spans(Case):
    def test_a_merged_run_is_one_trace_with_its_steps(self):
        ident, items = self.merged(review_round=1)
        record_cost(self.db, source="run", role="author", repo=REPO, pass_=ident, tokens_in=100, tokens_out=7,
                    usd=1.25)
        spans = telemetry.run_spans(self.db, telemetry.ended_runs(self.db)[0])
        self.assertEqual([span["name"] for span in spans],
                         ["build_run", "claim", "build", "review_round", "enqueue", "lane", "merge"])
        self.assertEqual({span["traceId"] for span in spans}, {ident})
        root = spans[0]
        self.assertNotIn("parentSpanId", root)
        self.assertEqual({span["parentSpanId"] for span in spans[1:]}, {root["spanId"]})
        attributes = {row["key"]: row["value"] for row in root["attributes"]}
        self.assertEqual(attributes["sd.items"], {"stringValue": f"{items[0]},{items[1]}"})
        self.assertEqual(attributes["sd.item_titles"], {"stringValue": "Build me | And me"})
        self.assertEqual(attributes["sd.cost_usd"], {"doubleValue": 1.25})
        self.assertEqual(attributes["sd.tokens_in"], {"intValue": "100"})
        self.assertEqual(root["status"], {"code": 1})
        self.assertNotIn("sd.failure_class", attributes)
        merge = {row["key"]: row["value"] for row in spans[-1]["attributes"]}
        self.assertEqual((merge["sd.pr"], merge["sd.commit"]), ({"intValue": "1722"}, {"stringValue": SHA}))

    def test_a_failed_run_carries_its_class_and_an_error_status(self):
        self.run_row(["Fail me"], outcome="lane_failed",
                     detail="lane entry 1 failed: merging origin/main into build/sd-1 conflicts", notes=[
                         ("2026-10-31T08:26:35+00:00", "Build run {run} enqueued build/sd-1 in the lane as entry 1")])
        spans = telemetry.run_spans(self.db, telemetry.ended_runs(self.db)[0])
        self.assertEqual(spans[0]["status"], {"code": 2, "message": "conflict"})
        self.assertEqual(spans[-1]["name"], "lane")
        self.assertEqual(spans[-1]["status"], {"code": 2, "message": "conflict"})

    def test_failure_classes(self):
        cases = {
            "supervisor lost at step reviewing: no live beat for 180 s": "lost_run",
            "a child ran past 3600 s; its group was ended": "timed_out",
            "sd-ship lane enqueue refused: The hub database did not answer: database is locked": "db_locked",
            "sd-ship lane enqueue refused: lines too long: body line 31 is 149 characters (limit 72)":
                "line_too_long",
            "git push failed (leak-guard: 1 line(s) match a pattern": "leak_guard",
            "the ship adapter owns association and delivery trailers: line 15: `Closes: sd:1`":
                "no_closes_trailer",
            "ended at step building: permission denied: Bash": "denied_tool",
            "3 review rounds left blockers: a.py:1 merge conflict handling": "review_blockers",
            "the review gave no answer twice at d67de91c0b40": "review_no_answer",
            "the review stayed rate_limited after 4 waits of 900 s at d67de91c0b40": "review_no_answer",
            "the repository gate failed after the review cleared": "gate_failed",
            "the builder left no commit on build/sd-1": "no_commit",
            "gh: Server Error (HTTP 502)": "other",
        }
        for detail, expected in cases.items():
            with self.subTest(detail=detail):
                self.assertEqual(telemetry.failure_class("blocked", detail), expected)
        self.assertIsNone(telemetry.failure_class("done", "anything"))
        self.assertEqual(telemetry.failure_class("cancelled", "cancelled by operator"), "cancelled")

    def test_an_open_run_is_not_read(self):
        self.run_row(["Open"], outcome=None, detail=None, end=None, notes=[])
        self.assertEqual(telemetry.ended_runs(self.db), [])


class Report(Case):
    """The monthly lane report draft for the writing pipeline (pack sd:6947)."""

    def failed(self, detail, end="2026-10-31T09:02:06+00:00"):
        self.db.execute("UPDATE runner_lease SET released_at = ? WHERE released_at IS NULL", (end,))
        return self.run_row(["Fail me"], outcome="blocked", detail=detail, end=end, notes=[])[0]

    def test_a_month_reports_prs_failures_reviews_and_notable_runs(self):
        ident, items = self.merged(review_round=2)
        record_cost(self.db, source="run", role="author", repo=REPO, pass_=ident, usd=1.25)
        denied = self.failed("ended at step building: permission denied: Bash")
        self.failed("3 review rounds left blockers: a.py:1")
        self.failed("sd-ship lane enqueue refused: database is locked")
        self.failed("permission denied: Bash", end="2026-09-30T09:00:00+00:00")
        self.failed("permission denied: Bash", end="2026-09-29T09:00:00+00:00")
        text = telemetry.month_report(self.db, "2026-10")
        self.assertIn("# Lane report, 2026-10", text)
        self.assertIn(f"| repo | #1722 | sd:{items[0]}, sd:{items[1]} | 1.25 | 1.6 | 2 |", text)
        self.assertIn("| denied_tool | 1 | 2 | -1 |", text)
        self.assertIn("| review_blockers | 1 | 0 | +1 |", text)
        self.assertIn("Denied tool calls: 1 run", text)
        self.assertIn("Database-lock refusals: 1 run", text)
        self.assertIn("Repeat-blocker rate: 1 of 2 reviewed runs (50%)", text)
        # No export sends a build run to Phoenix (pack sd:7365): the report names the run, no trace link.
        self.assertIn(f"- denied_tool: build run {denied}", text)
        self.assertIn(f"- repo PR #1722, 1.25 USD: build run {ident}", text)
        self.assertNotIn("/redirects/traces/", text)

    def test_a_merge_noted_on_an_interrupted_run_counts_for_its_retry(self):
        """pack sd:7008: the merge was lost when only the bundle's cleanup retry ended done."""
        ident, items = self.run_row(["Build me"], outcome="interrupted", detail="supervisor lost", notes=[
            ("2026-10-31T08:26:35+00:00",
             "Build run {run} enqueued build/sd-1 in the lane as entry 20261031T082558Z-4791"),
            ("2026-10-31T09:01:01+00:00", f"Code delivery https://github.com/o/r/pull/1722 at {SHA}")])
        self.db.execute("INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, "
                        "created_at, updated_at, released_at, outcome, detail) SELECT id || '-retry', assignment, "
                        "run + 1, repo, branch, owner, work_path, retained_path, '2026-10-31T09:10:00+00:00', "
                        "'2026-10-31T09:12:00+00:00', '2026-10-31T09:12:00+00:00', 'done', 'worktree removed' "
                        "FROM runner_run WHERE id = ?", (ident,))
        self.db.commit()
        text = telemetry.month_report(self.db, "2026-10")
        self.assertIn(f"| repo | #1722 | sd:{items[0]} |", text)

    def test_the_report_carries_no_title_detail_or_note_body(self):
        self.merged(review_round=1)
        self.failed("ended at /Users/x/secret: permission denied: Bash")
        text = telemetry.month_report(self.db, "2026-10")
        for secret in ("/Users/x", "Fixed round 1", "Built.", "Build me", "Fail me", "secret"):
            self.assertNotIn(secret, text)

    def test_the_cli_writes_the_draft_and_defaults_to_last_month(self):
        self.merged()
        out = self.root / "draft" / "lane-report.md"
        with mock.patch.object(cli, "_open_for_read", return_value=connect(self.root / "sd.db", write=False)), \
                redirect_stdout(io.StringIO()) as said:
            code = cli.main(["telemetry", "report", "--month", "2026-10", "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("#1722", out.read_text())
        self.assertIn(str(out), said.getvalue())
        self.assertEqual(telemetry.last_month(telemetry.date(2026, 1, 15)), "2025-12")


class Export(Case):
    def test_the_otlp_export_is_gone(self):
        """pack sd:7365: the genai-traces collector is the one sink; sd-db sends no traces."""
        with mock.patch.object(cli, "_open_for_read", return_value=connect(self.root / "sd.db", write=False)), \
                mock.patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:9/v1/traces"}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as said:
            code = cli.main(["telemetry", "export"])
        self.assertEqual(code, 1)
        self.assertIn("expected report", said.getvalue())


if __name__ == "__main__":
    unittest.main()
