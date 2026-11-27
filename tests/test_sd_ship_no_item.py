"""No-item CLI contracts use real Git and sd_db, never providers or GitHub."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib
import io
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sd_db import connect, initialise
from sd_db import ship as receipts
from sd_db.testing.remote import FixtureRemote, _git

from tests import test_sd_ship as fixture

ship = fixture.ship
no_item = importlib.import_module("sd_ship_no_item")
CAP = fixture.CAP
bindings = importlib.import_module("sd_ship_bindings")
#: The library file every worktree's borrowed `.venv` shares.
INSTALLED_LIBRARY = pathlib.Path(receipts.__file__)


def freeze_library(case, directory):
    """Point the adjudicator's library binding at this test's own copy (sd:1459).

    The binding reads the file named by `sd_db.ship.__file__` on every command,
    and the installed one is shared by every worktree that borrows the pack's
    `.venv`, so an install by another session mid-test moved it. The copy is
    taken once per test and nothing else writes it.
    """
    library = directory / "sd_db-ship.py"
    shutil.copyfile(INSTALLED_LIBRARY, library)
    frozen = patch.object(receipts, "__file__", str(library))
    frozen.start()
    case.addCleanup(frozen.stop)


class NoItemContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sd-no-item-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = pathlib.Path(temporary.name).resolve()
        self.remote = FixtureRemote(self.directory / "remote")
        self.remote.commit_on(
            "topic", "change\n\nAuthored-with: human", files={"src.py": "value = 1\n"}
        )
        self.root = self.directory / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(self.remote.path), str(self.root)], check=True
        )
        _git(self.root, "checkout", "topic")
        _git(self.root, "config", "user.name", "Fixture")
        _git(self.root, "config", "user.email", "fixture@example.invalid")
        self.remote_url = "https://github.com/fixture/repo.git"
        _git(self.root, "remote", "set-url", "origin", self.remote_url)
        self.head = _git(self.root, "rev-parse", "HEAD")
        self.base = _git(self.root, "rev-parse", "origin/main")
        self.database = self.directory / "store" / "sd.db"
        initialise(self.database)
        self.connection = connect(self.database)
        self.addCleanup(self.connection.close)
        freeze_library(self, self.directory)
        self.environment = {
            "HOME": str(self.directory / "home"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.remote.path}.insteadOf",
            "GIT_CONFIG_VALUE_0": self.remote_url,
        }

    def snapshot(self):
        return list(self.connection.iterdump())

    def keys(self, prefix="ship-review-no-item:"):
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT DISTINCT key FROM state WHERE kind='checkpoint' AND key LIKE ? ORDER BY key",
                (prefix + "%",),
            )
        ]

    def index_entry(self, state):
        """The branch index row for a record: the one claim a close releases."""
        key = no_item.index_key(state["repository"], "branch", state["branch"])
        _revision, entry = receipts.read(self.connection, key)
        return entry or {}

    def record(self, review_id):
        matches = [
            (key, receipts.read(self.connection, key))
            for key in self.keys()
            if receipts.read(self.connection, key)[1].get("review_id") == review_id
        ]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def cli(self, command, *extra, reviewer=None, root=None):
        """`root` runs the command in another checkout of the same repository."""
        output, errors = io.StringIO(), io.StringIO()
        args = [command, "--no-item", "--json", "--database", str(self.database), *extra]
        previous = pathlib.Path.cwd()
        with (
            patch.dict(os.environ, self.environment),
            patch.object(receipts, "identity", side_effect=AssertionError("item lookup")),
            patch.object(ship.Ship, "__init__", side_effect=AssertionError("item-bound constructor")),
            patch.object(ship, "GitHub", side_effect=AssertionError("publication client")),
            patch("sd_ship_remote.GitHub", side_effect=AssertionError("publication client")),
            patch.object(
                ship,
                "review_process",
                side_effect=reviewer or AssertionError("unexpected provider dispatch"),
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            try:
                os.chdir(root or self.root)
                try:
                    code = ship.main(args)
                except SystemExit as error:
                    code = error.code
            finally:
                os.chdir(previous)
        try:
            value = json.loads(output.getvalue())
        except ValueError:
            value = {}
        return code, value, output.getvalue() + errors.getvalue()

    def success(self, command, *args, **kwargs):
        code, value, diagnostic = self.cli(command, *args, **kwargs)
        self.assertEqual(code, 0, diagnostic)
        self.assertIs(value.get("ok"), True, diagnostic)
        self.assertEqual(value.get("identity_mode"), "no-item", diagnostic)
        self.assertNotIn("item", value)
        return value

    def refused(self, command, *args, pattern, **kwargs):
        before = self.snapshot()
        code, value, diagnostic = self.cli(command, *args, **kwargs)
        self.assertEqual(code, 3, diagnostic)
        self.assertIs(value.get("ok"), False, diagnostic)
        self.assertRegex(value.get("error", ""), pattern)
        self.assertEqual(self.snapshot(), before)
        return value

    def create(self, *extra):
        return self.success(
            "review", "--create-record", "--assert-new-work", *extra
        )["review_id"]

    def file_claim(self, name, value):
        path = self.directory / name
        data = (json.dumps(value, indent=2) + "\n").encode()
        path.write_bytes(data)
        return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}

    def commit_fix(self, name, value):
        """Commit a descendant of the current head and return the head it left."""
        (self.root / name).write_text(value)
        _git(self.root, "add", name)
        _git(self.root, "commit", "-q", "-m", f"fix {name}\n\nAuthored-with: human")
        previous, self.head = self.head, _git(self.root, "rev-parse", "HEAD")
        return previous

    def report(self, *, blocking=False):
        return {
            "scope": "branch",
            "status": "blocking" if blocking else "clean",
            "subject": {"head": self.head, "base": self.base},
            "authorship_base": self.base,
            "authored_with": ["historical-author"],
            "requested_reviews": 1,
            "completed_reviews": 1,
            "reviewed_by": ["fixture"],
            "outcomes": [{"backend": "fixture", "status": "findings" if blocking else "clean"}],
            "check": {"status": "pass", "exit_code": 0},
            "findings": [
                {"path": "src.py", "line": 1, "disposition": "blocking", "summary": "fixture dispute"}
            ] if blocking else [],
        }

    def manifest(self, count, *, missing=False):
        rows = []
        for ordinal in range(1, count + 1):
            report = None if missing and ordinal == count else self.file_claim(
                f"report-{ordinal}.json", self.report(blocking=True)
            )
            rows.append({
                "ordinal": ordinal,
                "head": self.head,
                "report": report,
                "request": None,
                "prior_input": None,
                "exit_code": 124 if report is None else 1,
                "execution_error": {"kind": "watchdog_expired"} if report is None else None,
            })
        value = {"schema_version": 1, "repository": "fixture/repo", "passes": rows}
        claim = self.file_claim(f"history-{count}.json", value)
        return pathlib.Path(claim["path"]), value

    def import_history(self, count, *, missing=False):
        path, manifest = self.manifest(count, missing=missing)
        review_id = self.create(
            "--import-history", str(path), "--assert-history-complete"
        )
        return review_id, manifest

    def native_reviewer(self, review_id, *, imported=0, blocking=False, interrupt=False, duplicate=False,
                        resume=None, shape=None):
        """`resume` continues a full history without an import; `shape` adjusts
        the report the stub returns, which is how a fix verification says which
        head and which prior report it continues.
        """
        calls = []
        resume = bool(imported) if resume is None else resume
        _key, (_revision, initial) = self.record(review_id)
        initial_native = len(initial["passes"])

        def reviewer(_root, argv, *, timeout):
            self.assertGreater(timeout, 0)
            if "--explain" in argv:
                plan = {
                    "status": "explained", "requested_reviews": 1,
                    "readiness": {"status": "ready", "blockers": [], "warnings": [], "runtime_approval": "not_observable"},
                    "timing": {
                        "phase_seconds": 60, "setup_seconds": 3600,
                        "execution_seconds": 3720,
                        "candidates": [{"name": "fixture", "recipient": "fixture@local"}],
                    },
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(plan), "")
            # The persisted reservation, not an in-memory list, owns this pass.
            _key, (_revision, state) = self.record(review_id)
            self.assertEqual(len(state["historical_passes"]), imported)
            self.assertEqual(len(state["passes"]), initial_native + len(calls) + 1)
            self.assertEqual(state["passes"][-1]["head"], self.head)
            self.assertNotIn("report", state["passes"][-1])
            calls.append(argv)
            if interrupt:
                raise ship.ReviewTimeout({"kind": "watchdog_expired", "allowed_seconds": timeout})
            report = self.report(blocking=blocking)
            if duplicate:
                report["findings"].append(dict(report["findings"][0]))
            if resume:
                self.assertIn("--resume-report", argv)
                self.assertNotIn("--verify-report", argv)
                self.assertNotIn("--base", argv)
                prior = pathlib.Path(argv[argv.index("--resume-report") + 1])
                aggregate = json.loads(prior.read_bytes())
                self.assertEqual(len(aggregate["history"]), imported + initial_native + len(calls) - 1)
                report["resume_report_digest"] = ship.digest(aggregate)
            if shape is not None:
                shape(report, state, argv)
            return subprocess.CompletedProcess(argv, 1 if blocking else 0, json.dumps(report), "")

        return reviewer, calls

    def blocking_proposal(self, *, duplicate=False):
        review_id = self.create()
        reviewer, calls = self.native_reviewer(review_id, blocking=True, duplicate=duplicate)
        code, value, diagnostic = self.cli("review", "--review-id", review_id, reviewer=reviewer)
        self.assertEqual(code, 3, diagnostic)
        self.assertIn("blocking", value["error"])
        self.assertEqual(len(calls), 1)
        before = self.snapshot()
        result = self.success(
            "adjudicate", "--review-id", review_id, "--expected-head", self.head
        )
        self.assertEqual(self.snapshot(), before)
        proposal = result["proposal"]
        for row in proposal["findings"]:
            row.update(response_disposition="rebutted", reason="src.py:1 already handles the case the finding names")
        proposal_path = self.directory / "dispositions.json"
        proposal_path.write_text(json.dumps(proposal))
        return review_id, proposal, proposal_path

    def accept(self, review_id, proposal_path):
        """One command records the filled template; there is no digest step."""
        result = self.success(
            "adjudicate", "--review-id", review_id, "--expected-head", self.head,
            "--dispositions-file", str(proposal_path),
        )
        self.assertEqual(result["phase"], "disposition_recorded")
        return result

    def test_fixture_starts_without_items_and_uses_only_local_git_transport(self):
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM item").fetchone()[0], 0)
        with patch.dict(os.environ, self.environment):
            remote = _git(self.root, "ls-remote", "origin", "refs/heads/topic")
        self.assertEqual(remote.split()[0], self.head)

    def test_create_is_provider_free_and_persists_no_item_identity(self):
        review_id = self.create()
        self.assertIsInstance(review_id, str)
        self.assertTrue(review_id)
        key, (revision, record) = self.record(review_id)
        self.assertGreater(revision, 0)
        suffix = hashlib.sha256(("fixture/repo\0" + review_id).encode()).hexdigest()
        self.assertEqual(key, "ship-review-no-item:" + suffix)
        self.assertEqual(record["identity_mode"], "no-item")
        self.assertEqual(record["repository"], "fixture/repo")
        self.assertEqual(record["branch"], "topic")
        self.assertEqual(record["passes"], [])
        self.assertEqual(record["historical_passes"], [])
        self.assertNotIn("item", record)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM item").fetchone()[0], 0)
        self.assertEqual(self.keys("ship:"), [])
        self.assertTrue(self.keys("ship-review-no-item-index:"))

    def test_create_requires_explicit_new_work_assertion(self):
        self.refused("review", "--create-record", pattern="new work|assert-new-work")

    def test_missing_record_identity_never_allocates_a_budget(self):
        self.refused("review", pattern="review-id|record identity")
        self.assertEqual(self.keys(), [])

    def test_each_identity_refusal_names_the_command_that_allocates_a_record(self):
        """sd:2008, sd:2026: a caller learns the allocation command from the refusal, not the skill.

        A chosen ID, a missing one, and a chosen one beside --create-record each
        cost an attempt before the working call.
        """
        allocate = "sd-ship review --no-item --create-record --assert-new-work"
        missing = self.refused("review", pattern="review-id|record identity")
        chosen = self.refused("review", "--review-id", "my-chosen-name", pattern="no no-item record my-chosen-name")
        beside = self.refused("review", "--create-record", "--assert-new-work", "--review-id", "my-chosen-name",
                              pattern="drop --review-id my-chosen-name")
        prepare = self.refused("prepare", pattern="record identity")
        merge = self.refused("merge", "--manual", "--expected-head", self.head, pattern="record identity")
        self.assertIn(allocate, chosen["error"])
        for failure, code in ((missing, "review_identity_required"), (prepare, "review_identity_required"),
                              (merge, "review_identity_required"), (chosen, "review_record_missing"),
                              (beside, "review_id_not_chosen")):
            with self.subTest(code=code):
                self.assertEqual(failure["workflow"]["blocker"]["code"], code)
                self.assertIn(allocate, failure["workflow"]["next_action"])
                self.assertIn("review_id", failure["workflow"]["next_action"])
        self.assertEqual(self.keys(), [])

    def test_both_missing_identity_sites_refuse_with_one_definition(self):
        # The CLI refuses in `validate_identity` before the adapter runs, so the
        # adapter's own guard is reached only by a direct call.
        _code, cli, _diagnostic = self.cli("review")
        args = ship.parser().parse_args(["review", "--no-item", "--json"])
        with self.assertRaises(ship.Refusal) as direct:
            no_item.run_no_item(self.root, self.connection, self.database, args, receipts, None)
        self.assertEqual(ship.failure("review", direct.exception), cli)

    def test_a_branch_mismatch_names_the_rebind_command(self):
        review_id = self.create()
        _git(self.root, "checkout", "-q", "-b", "renamed")
        failure = self.refused("review", "--review-id", review_id, pattern="bound to branch topic, not renamed")
        self.assertEqual(failure["workflow"]["blocker"]["code"], "review_branch_mismatch")
        self.assertIn(f"--review-id {review_id} --rebind-branch topic", failure["workflow"]["next_action"])

    def test_dirty_checkout_and_empty_branch_refuse_creation(self):
        (self.root / "src.py").write_text("value = 2\n")
        self.refused("review", "--create-record", "--assert-new-work", pattern="uncommitted|clean")
        _git(self.root, "commit", "-am", "fixture edit\n\nAuthored-with: human")
        _git(self.root, "checkout", "-b", "empty", self.base)
        self.refused("review", "--create-record", "--assert-new-work", pattern="empty|nonempty|committed diff")

    def test_same_head_alias_cannot_allocate_another_budget(self):
        review_id = self.create()
        _git(self.root, "checkout", "-b", "copied-topic")
        failure = self.refused("review", "--create-record", "--assert-new-work", pattern="existing|record|owned")
        self.assertIn(review_id, failure["error"])
        self.assertEqual(len(self.keys()), 1)

    def test_distinct_committed_work_can_share_a_merge_base(self):
        first = self.create()
        _git(self.root, "checkout", "-b", "independent", self.base)
        (self.root / "other.py").write_text("other = 2\n")
        _git(self.root, "add", "other.py")
        _git(self.root, "commit", "-m", "independent work\n\nAuthored-with: human")
        second = self.create()
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.keys()), 2)

    def test_an_empty_commit_on_merged_work_allocates_its_own_record(self):
        """sd:2009. An empty commit: its tree is the
        base's, which a merged record already claimed, so allocation refused
        with "already owns this tree". The base tree is no claim; the commit is."""
        merged = self.create()
        _git(self.remote.path, "update-ref", "refs/heads/main", self.head)
        _git(self.root, "fetch", "-q", str(self.remote.path), "+main:refs/remotes/origin/main")
        _git(self.root, "checkout", "-q", "-b", "attribution-repair", "origin/main")
        _git(self.root, "commit", "-q", "--allow-empty", "-m", "Attributes: repair\n\nAuthored-with: human")
        repair = self.create()
        self.assertNotEqual(merged, repair)
        _git(self.root, "checkout", "-q", "-b", "repair-copy")
        self.refused("review", "--create-record", "--assert-new-work", pattern=f"{repair} already owns this head")

    def test_import_preserves_three_spent_passes_and_exact_original_bytes(self):
        review_id, manifest = self.import_history(3, missing=True)
        _key, (_revision, record) = self.record(review_id)
        self.assertEqual(len(record["historical_passes"]), 3)
        self.assertEqual(record["passes"], [])
        for source, stored in zip(manifest["passes"], record["historical_passes"], strict=True):
            self.assertEqual(stored["ordinal"], source["ordinal"])
            self.assertEqual(stored["head"], source["head"])
            self.assertEqual(stored["exit_code"], source["exit_code"])
            if source["report"] is None:
                self.assertIsNone(stored["report"])
                self.assertEqual(stored["execution_error"], source["execution_error"])
            else:
                self.assertEqual(stored["report"]["sha256"], source["report"]["sha256"])
                self.assertEqual(
                    base64.b64decode(stored["report"]["bytes_base64"], validate=True),
                    pathlib.Path(source["report"]["path"]).read_bytes(),
                )
        self.refused(
            "verify-review", "--review-id", review_id, "--expected-head", self.head,
            pattern="native|completed|coverage",
        )

    def test_import_requires_completeness_and_matching_raw_hash(self):
        review_id = self.create()
        path, manifest = self.manifest(1)
        self.refused("review", "--review-id", review_id, "--import-history", str(path), pattern="complete|assert-history-complete")
        manifest["passes"][0]["report"]["sha256"] = "0" * 64
        path.write_text(json.dumps(manifest))
        self.refused(
            "review", "--review-id", review_id, "--import-history", str(path),
            "--assert-history-complete", pattern="hash|SHA256|digest",
        )

    def test_import_rejects_unknown_schema_fields_and_foreign_repository(self):
        review_id = self.create()
        path, original = self.manifest(1)
        for field, value in (("schema_version", 2), ("repository", "other/repo"), ("trusted", True)):
            with self.subTest(field=field):
                path.write_text(json.dumps({**original, field: value}))
                self.refused(
                    "review", "--review-id", review_id, "--import-history", str(path),
                    "--assert-history-complete", pattern="schema|repository|field|manifest",
                )

    def test_imported_prior_input_digest_must_match_original_report(self):
        review_id = self.create()
        path, manifest = self.manifest(1)
        prior = self.file_claim("prior.json", {"findings": [], "authored_with": []})
        report = self.report()
        report["resume_report_digest"] = "0" * 64
        manifest["passes"][0].update(
            report=self.file_claim("linked-report.json", report), prior_input=prior
        )
        path.write_text(json.dumps(manifest))
        self.refused(
            "review", "--review-id", review_id, "--import-history", str(path),
            "--assert-history-complete", pattern="input|digest|reference",
        )

    def test_foreign_history_head_refuses_without_writing_import(self):
        review_id = self.create()
        self.remote.commit_on("foreign", "unrelated", files={"foreign.py": "foreign = 1\n"})
        with patch.dict(os.environ, self.environment):
            _git(self.root, "fetch", "origin", "foreign")
        foreign = _git(self.root, "rev-parse", "FETCH_HEAD")
        path, manifest = self.manifest(1)
        report = self.report()
        report["subject"]["head"] = foreign
        manifest["passes"][0].update(head=foreign, report=self.file_claim("foreign-report.json", report))
        path.write_text(json.dumps(manifest))
        self.refused(
            "review", "--review-id", review_id, "--import-history", str(path),
            "--assert-history-complete", pattern="ancestor|ancestry|history head",
        )

    def test_each_short_import_requires_explicit_native_continuation(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                # Independent databases keep each case's identity registry isolated.
                self.database = self.directory / f"history-case-{count}.db"
                initialise(self.database)
                self.connection = connect(self.database)
                self.addCleanup(self.connection.close)
                review_id, _manifest = self.import_history(count)
                self.refused("review", "--review-id", review_id, pattern="explicit|request|continuation")
                _key, (_revision, state) = self.record(review_id)
                self.assertEqual(len(state["historical_passes"]), count)
                self.assertEqual(state["passes"], [])

    def test_imported_continuation_reserves_global_fourth_pass_before_dispatch(self):
        review_id, _manifest = self.import_history(3)
        _key, (_revision, state) = self.record(review_id)
        history_digest = state["history_digest"]
        reviewer, calls = self.native_reviewer(review_id, imported=3)
        self.success(
            "review", "--review-id", review_id, "--additional-review-for", self.head,
            "--review-history-digest", history_digest, "--request-reason", "fixture approval assertion",
            reviewer=reviewer,
        )
        self.assertEqual(len(calls), 1)
        _key, (_revision, state) = self.record(review_id)
        self.assertEqual(len(state["historical_passes"]) + len(state["passes"]), 4)
        self.assertEqual(state["passes"][0]["additional_review_request"]["prior_history_digest"], history_digest)

    def test_stale_import_continuation_digest_refuses_without_reservation(self):
        review_id, _manifest = self.import_history(3)
        self.refused(
            "review", "--review-id", review_id, "--additional-review-for", self.head,
            "--review-history-digest", "0" * 64, "--request-reason", "fixture assertion",
            pattern="history|digest",
        )

    def test_one_and_two_imports_dispatch_full_history_at_global_ordinals(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self.database = self.directory / f"approved-history-{count}.db"
                initialise(self.database)
                self.connection = connect(self.database)
                self.addCleanup(self.connection.close)
                review_id, _manifest = self.import_history(count)
                _key, (_revision, state) = self.record(review_id)
                prefix = state["history_digest"]
                reviewer, calls = self.native_reviewer(review_id, imported=count)
                self.success(
                    "review", "--review-id", review_id, "--additional-review-for", self.head,
                    "--review-history-digest", prefix, "--request-reason", "fixture continuation assertion",
                    reviewer=reviewer,
                )
                self.assertEqual(len(calls), 1)
                _key, (_revision, state) = self.record(review_id)
                self.assertEqual(len(state["historical_passes"]) + len(state["passes"]), count + 1)
                self.assertEqual(state["passes"][0]["additional_review_request"]["prior_history_digest"], prefix)
                self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)

    def test_rebind_after_native_continuation_preserves_original_request_binding(self):
        review_id, _manifest = self.import_history(1)
        _key, (_revision, state) = self.record(review_id)
        reviewer, _calls = self.native_reviewer(review_id, imported=1)
        self.success(
            "review", "--review-id", review_id, "--additional-review-for", self.head,
            "--review-history-digest", state["history_digest"], "--request-reason", "fixture assertion",
            reviewer=reviewer,
        )
        _key, (_revision, original) = self.record(review_id)
        _git(self.root, "branch", "-m", "renamed-after-native")
        self.success("review", "--review-id", review_id, "--rebind-branch", "topic")
        _key, (_revision, current) = self.record(review_id)
        self.assertEqual(current["historical_passes"], original["historical_passes"])
        self.assertEqual(current["passes"], original["passes"])
        self.assertEqual(current["history_digest"], original["history_digest"])
        self.assertGreater(current["identity_revision"], original["identity_revision"])
        reviewer, calls = self.native_reviewer(review_id, imported=1)
        self.success(
            "review", "--review-id", review_id, "--additional-review-for", self.head,
            "--review-history-digest", current["history_digest"], "--request-reason", "fixture renewed assertion",
            reviewer=reviewer,
        )
        self.assertEqual(len(calls), 1)
        self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)

    def test_related_item_receipt_refuses_allocation_without_item_lookup(self):
        key = receipts.receipt_key("fixture/repo", "topic", 41)
        receipts.save(self.connection, key, 0, {
            "repository": "fixture/repo", "branch": "topic", "item": 41,
            "passes": [{"head": self.head}],
        })
        failure = self.refused("review", "--create-record", "--assert-new-work", pattern="item|item-backed")
        self.assertIn("41", failure["error"])
        self.assertIn(key, failure["error"])

    def test_malformed_item_receipt_cannot_be_treated_as_empty_history(self):
        self.connection.execute(
            "INSERT INTO state(kind,key,timestamp,body) VALUES ('checkpoint',?,?,?)",
            ("ship:unreadable", "2026-09-17T00:00:00Z", "not JSON"),
        )
        self.connection.commit()
        self.refused("review", "--create-record", "--assert-new-work", pattern="unreadable|malformed|history")

    def test_a_record_closes_and_reopens_from_a_detached_head(self):
        # The hub's pack checkout sits on a detached HEAD (sd:3097); the help
        # says close and reopen run from any branch (sd:7497).
        review_id, _manifest = self.import_history(1)
        _git(self.root, "checkout", "-q", "--detach")
        self.success("review", "--review-id", review_id, "--close-record", "fixture abandoned")
        self.success("review", "--review-id", review_id, "--reopen-record")
        self.refused("review", "--review-id", review_id, "--rebind-branch", "topic", pattern="branch")

    def test_rebind_and_lifecycle_preserve_spent_history_and_invalidate_identity(self):
        review_id, _manifest = self.import_history(3)
        _key, (_revision, original) = self.record(review_id)
        _git(self.root, "branch", "-m", "renamed")
        self.refused("review", "--review-id", review_id, pattern="branch|rebind")
        self.success("review", "--review-id", review_id, "--rebind-branch", "topic")
        self.success("review", "--review-id", review_id, "--close-record", "fixture abandoned")
        self.refused("verify-review", "--review-id", review_id, "--expected-head", self.head, pattern="closed")
        self.success("review", "--review-id", review_id, "--reopen-record")
        _key, (_revision, current) = self.record(review_id)
        self.assertEqual(current["historical_passes"], original["historical_passes"])
        self.assertEqual(current["passes"], original["passes"])
        self.assertEqual(current["history_digest"], original["history_digest"])
        self.assertEqual(current["identity_revision"], original["identity_revision"] + 3)
        self.assertEqual(current["branch"], "renamed")

    def test_verify_without_native_coverage_is_read_only_and_provider_free(self):
        review_id = self.create()
        self.refused(
            "verify-review", "--review-id", review_id, "--expected-head", self.head,
            pattern="native|completed|coverage|receipt",
        )

    def test_native_clean_clearance_is_read_only_and_creates_no_item_rows(self):
        review_id = self.create()
        reviewer, calls = self.native_reviewer(review_id)
        self.success("review", "--review-id", review_id, reviewer=reviewer)
        before = self.snapshot()
        for _ in range(2):
            self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM item").fetchone()[0], 0)

    def test_interrupted_native_reservation_survives_close_and_reopen(self):
        review_id = self.create()
        reviewer, calls = self.native_reviewer(review_id, interrupt=True)
        code, value, diagnostic = self.cli("review", "--review-id", review_id, reviewer=reviewer)
        self.assertEqual(code, 3, diagnostic)
        self.assertIn("watchdog", value["error"])
        self.assertEqual(len(calls), 1)
        _key, (_revision, interrupted) = self.record(review_id)
        self.assertEqual(len(interrupted["passes"]), 1)
        self.success("review", "--review-id", review_id, "--close-record", "fixture interruption")
        self.success("review", "--review-id", review_id, "--reopen-record")
        _key, (_revision, reopened) = self.record(review_id)
        self.assertEqual(reopened["passes"], interrupted["passes"])
        self.refused("review", "--review-id", review_id, pattern="incomplete|retry|request")

    def test_a_blocking_review_names_each_finding_and_the_command_for_this_record(self):
        """sd:1986. `sd-ship review` said only `see item ship receipt`, and a no-item record has none."""
        review_id = self.create()
        reviewer, _calls = self.native_reviewer(review_id, blocking=True)
        code, value, diagnostic = self.cli("review", "--review-id", review_id, reviewer=reviewer)
        self.assertEqual(code, 3, diagnostic)
        self.assertIn("src.py:1 fixture dispute", value["error"])
        self.assertNotIn("item ship receipt", value["error"])
        self.assertIn(f"sd-ship adjudicate --no-item --review-id {review_id} --expected-head {self.head}",
                      value["workflow"]["next_action"])

    def test_recording_clears_at_once_and_never_dispatches_a_provider(self):
        review_id, _proposal, proposal_path = self.blocking_proposal()
        self.refused(
            "verify-review", "--review-id", review_id, "--expected-head", self.head,
            pattern="blocking|recorded dispositions",
        )
        self.accept(review_id, proposal_path)
        suffix = hashlib.sha256(("fixture/repo\0" + review_id).encode()).hexdigest()
        self.assertEqual(self.keys("ship-adjudication-no-item:"), ["ship-adjudication-no-item:" + suffix])
        self.assertEqual(self.keys("ship-adjudication:"), [])
        before = self.snapshot()
        for _ in range(2):
            self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
        self.assertEqual(self.snapshot(), before)

    def test_a_library_reinstalled_mid_test_does_not_move_the_adjudicator_binding(self):
        """sd:1459. The shared environment is not this test's to hold still.

        `adjudicator_binding` hashes the installed `sd_db/ship.py` again on
        every command. Worktrees borrow one `.venv`, and a session installing
        `sd_db` into it between two of this fixture's commands made the accept
        refuse with "does not bind the current review, history, tools and
        head": the flake the row names. The reinstall is simulated as the
        installed file reading different bytes after the proposal is written.
        """
        review_id, _proposal, proposal_path = self.blocking_proposal()
        original = pathlib.Path.read_bytes

        def reinstalled(path):
            return original(path) + (b"# reinstalled\n" if path == INSTALLED_LIBRARY else b"")

        with patch.object(pathlib.Path, "read_bytes", reinstalled):
            self.accept(review_id, proposal_path)
            self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)

    def test_repeated_template_and_clearance_open_database_read_only(self):
        review_id, _proposal, proposal_path = self.blocking_proposal()
        self.accept(review_id, proposal_path)
        before = self.snapshot()
        opened = []

        def readonly(database, *, write=True):
            self.assertEqual(pathlib.Path(database), self.database)
            self.assertIs(write, False, "read-only commands must not request a writable connection")
            opened.append(database)
            return connect(database, write=False)

        with patch("sd_db.database.connect", side_effect=readonly):
            for _ in range(2):
                self.success("adjudicate", "--review-id", review_id, "--expected-head", self.head)
                self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
        # Two opens per command: the managed-repository check (sd:2566), then the command.
        self.assertEqual(len(opened), 8)
        self.assertEqual(self.snapshot(), before)

    def test_accepted_clearance_refuses_changed_head(self):
        review_id, _proposal, proposal_path = self.blocking_proposal()
        self.accept(review_id, proposal_path)
        (self.root / "src.py").write_text("value = 2\n")
        _git(self.root, "commit", "-am", "later change\n\nAuthored-with: human")
        changed = _git(self.root, "rev-parse", "HEAD")
        for expected in (self.head, changed):
            with self.subTest(expected=expected):
                self.refused(
                    "verify-review", "--review-id", review_id, "--expected-head", expected,
                    pattern="head|HEAD|review|receipt",
                )

    def test_invalid_or_incomplete_dispositions_cannot_clear_blockers(self):
        review_id, original, proposal_path = self.blocking_proposal()
        for mutation in ("missing", "addressed", "parked_without_reason", "changed_finding"):
            proposal = json.loads(json.dumps(original))
            if mutation == "missing":
                proposal["findings"].clear()
            elif mutation == "addressed":
                proposal["findings"][0]["response_disposition"] = "addressed"
            elif mutation == "parked_without_reason":
                proposal["findings"][0].update(response_disposition="parked", reason="")
            else:
                proposal["findings"][0]["raw_finding"]["summary"] = "rewritten raw finding"
            proposal_path.write_text(json.dumps(proposal))
            with self.subTest(mutation=mutation):
                self.refused(
                    "adjudicate", "--review-id", review_id, "--expected-head", self.head,
                    "--dispositions-file", str(proposal_path), pattern="finding|risk|disposition|reason",
                )

    def test_duplicate_blockers_require_separate_response_indices(self):
        review_id, proposal, proposal_path = self.blocking_proposal(duplicate=True)
        self.assertEqual([row["index"] for row in proposal["findings"]], [1, 2])
        self.assertEqual(proposal["findings"][0]["finding_digest"], proposal["findings"][1]["finding_digest"])
        incomplete = json.loads(json.dumps(proposal))
        incomplete["findings"].pop()
        proposal_path.write_text(json.dumps(incomplete))
        self.refused(
            "adjudicate", "--review-id", review_id, "--expected-head", self.head,
            "--dispositions-file", str(proposal_path), pattern="finding|separate|disposition",
        )
        proposal_path.write_text(json.dumps(proposal))
        self.accept(review_id, proposal_path)
        self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)


    def test_schema_drift_refuses_creation_dispatch_and_clearance(self):
        """A layout this adapter was not tested against stops every path.

        The guard has to sit on the read paths too. Enumeration alone would
        leave dispatch and clearance reading a table whose shape nothing
        checked, which is the case a schema change actually produces.
        """

        review_id = self.create()
        reviewer, calls = self.native_reviewer(review_id)
        self.success("review", "--review-id", review_id, reviewer=reviewer)
        drifted = dict(no_item.SCHEMA_CONTRACT, columns=(*no_item.SCHEMA_CONTRACT["columns"], "absent_column"))
        with patch.object(no_item, "SCHEMA_CONTRACT", drifted):
            for command in (
                ("review", "--create-record", "--assert-new-work"),
                ("review", "--review-id", review_id),
                ("verify-review", "--review-id", review_id, "--expected-head", self.head),
            ):
                with self.subTest(command=command[0]):
                    failure = self.refused(*command, pattern="layout|schema")
                    self.assertIn("absent_column", failure["error"])
        self.assertEqual(len(calls), 1)
        self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)

    def test_a_failed_enumeration_query_is_never_an_empty_history(self):
        class FailingQueries:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, statement, *parameters):
                if "LIKE" in statement:
                    raise sqlite3.DatabaseError("fixture enumeration failure")
                return self.connection.execute(statement, *parameters)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        def failing(database, *, write=True):
            return FailingQueries(connect(database, write=write))

        with patch("sd_db.database.connect", side_effect=failing):
            failure = self.refused(
                "review", "--create-record", "--assert-new-work", pattern="query failed|empty history"
            )
        self.assertIn("fixture enumeration failure", failure["error"])
        self.assertEqual(self.keys(), [])

    def test_a_malformed_no_item_record_refuses_dispatch_and_clearance(self):
        review_id = self.create()
        key, _record = self.record(review_id)
        self.connection.execute(
            "INSERT INTO state(kind,key,timestamp,body) VALUES ('checkpoint',?,?,?)",
            (key, "2026-09-18T00:00:00Z", "not JSON"),
        )
        self.connection.commit()
        # No reviewer is supplied, so any provider dispatch raises instead.
        self.refused("review", "--review-id", review_id, pattern="unreadable|malformed|receipt")
        self.refused(
            "verify-review", "--review-id", review_id, "--expected-head", self.head,
            pattern="unreadable|malformed|receipt",
        )

    def test_a_forged_item_receipt_under_the_no_item_key_cannot_clear(self):
        """The expected key is not the authority. The bound identity is.

        A copy placed under the key this mode reads is the whole attack, so the
        forgery keeps a recomputed `proposal_digest`: the bookkeeping check
        passes and only the identity bindings stand in its way.
        """

        review_id, _proposal, proposal_path = self.blocking_proposal()
        self.accept(review_id, proposal_path)
        self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
        key = no_item.no_item_acceptance_key("fixture/repo", review_id)
        revision, receipt = receipts.read(self.connection, key)
        forged = json.loads(json.dumps(receipt))
        bindings = forged["proposal"]["bindings"]
        for name in ("identity_mode", "review_id", "identity_revision", "schema_version"):
            bindings.pop(name)
        bindings["item"] = 41
        forged["proposal_digest"] = ship.sd_ship_dispositions.digest(forged["proposal"])
        receipts.save(self.connection, key, revision, forged)
        self.refused(
            "verify-review", "--review-id", review_id, "--expected-head", self.head,
            pattern="bind|identity|item",
        )

    # The strict reading (sd:10929): by default a tool change keeps the clearance (sd:10928).
    @patch.object(fixture.ship.sd_lib, "TOOL_CHANGES_KEEP_REVIEWS", False)
    def test_each_verdict_member_mutation_refuses_no_item_clearance(self):
        """The manifest guards both modes, so both modes are measured.

        Enumerated from the manifest rather than listed here, so a gate added
        to it is covered the moment it joins.
        """

        review_id = self.create()
        reviewer, calls = self.native_reviewer(review_id)
        self.success("review", "--review-id", review_id, reviewer=reviewer)
        self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
        original = pathlib.Path.read_bytes
        for name in bindings.REVIEW_TOOL_FILES:
            target = bindings.BIN / name

            def changed(path, target=target):
                return original(path) + (b"changed" if path == target else b"")

            with self.subTest(name=name), patch.object(pathlib.Path, "read_bytes", changed):
                if name in bindings.VERDICT_FILES:
                    self.refused(
                        "verify-review", "--review-id", review_id, "--expected-head", self.head,
                        pattern=f"tools or repository policy changed after review: {name} \\(verdict\\)",
                    )
                else:
                    # sd:1834: gate and check code runs live, so it binds no receipt.
                    self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
        self.assertEqual(len(calls), 1)
        self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)

    def test_capacity_limits_refuse_actionably_and_keep_every_record_readable(self):
        """A limit refuses the operation. It never prunes to make room.

        Both limits are lowered rather than reached, because a test that writes
        five thousand receipts measures patience, not the refusal.
        """

        for item in (41, 42):
            receipts.save(
                self.connection, receipts.receipt_key("other/repo", f"topic-{item}", item), 0,
                {"repository": "other/repo", "branch": f"topic-{item}", "item": item, "passes": []},
            )
        for name, limit in (("RECEIPT_LIMIT", 1), ("INDEX_LIMIT", 0)):
            with self.subTest(limit=name), patch.object(no_item, name, limit):
                failure = self.refused(
                    "review", "--create-record", "--assert-new-work", pattern="limit|exceed|reached",
                )
                self.assertIn(str(limit), failure["error"])
                self.assertIn("readable", failure["error"])
        # Nothing was deleted or hidden: allocation still works, and the
        # unrelated receipts still read exactly as they were written.
        review_id = self.create()
        _key, (_revision, state) = self.record(review_id)
        self.assertEqual(state["review_id"], review_id)
        for item in (41, 42):
            key = receipts.receipt_key("other/repo", f"topic-{item}", item)
            self.assertEqual(receipts.read(self.connection, key)[1]["item"], item)

    def test_accepted_clearance_rejects_every_mutation_of_imported_history(self):
        """Imported records cannot be added, dropped, reordered or rewritten.

        Each forgery recomputes the stored combined digest, so the cheap
        self-consistency guard cannot be what refuses. Three separate bindings
        do: the native request's exact combined prior history, the full-branch
        coverage digest, and each retained report's own SHA256. Removing any
        one of the three still refuses every mutation here except a reordering,
        which the first binding alone catches.
        """

        review_id, _manifest = self.import_history(2)
        _key, (_revision, state) = self.record(review_id)
        reviewer, calls = self.native_reviewer(review_id, imported=2)
        self.success(
            "review", "--review-id", review_id, "--additional-review-for", self.head,
            "--review-history-digest", state["history_digest"],
            "--request-reason", "fixture continuation assertion", reviewer=reviewer,
        )
        self.assertEqual(len(calls), 1)
        self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
        key, (_revision, original) = self.record(review_id)
        for mutation in ("added", "removed", "reordered", "content"):
            forged = json.loads(json.dumps(original))
            records = forged["historical_passes"]
            if mutation == "added":
                records.append(json.loads(json.dumps(records[-1])))
            elif mutation == "removed":
                records.pop()
            elif mutation == "reordered":
                records.reverse()
            else:
                records[0]["report"]["sha256"] = "0" * 64
            forged["history_digest"] = no_item.combined_digest(forged)
            with self.subTest(mutation=mutation):
                receipts.save(self.connection, key, receipts.read(self.connection, key)[0], forged)
                self.refused(
                    "verify-review", "--review-id", review_id, "--expected-head", self.head,
                    pattern="history|coverage|SHA256|digest",
                )
                receipts.save(self.connection, key, receipts.read(self.connection, key)[0], original)
                self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)

    def test_a_native_only_record_verifies_its_fixes_and_clears_a_requested_extra_pass(self):
        """The control case with no imported history at all.

        Pass one is the code review, each further automatic pass the fix
        verification of the head its predecessor reviewed, and the pass after
        the cap an explicitly requested continuation. That requested pass is
        where the record used to spend a paid review and then refuse its own
        clearance: the request carries the combined digest that
        `history_digest` writes, and the native-only reader compared it against
        the raw native prefix instead. The last two assertions are that
        mismatch, stated as the two formats that are not each other.
        """

        review_id = self.create()
        reviewer, calls = self.native_reviewer(review_id, blocking=True)
        code, value, diagnostic = self.cli("review", "--review-id", review_id, reviewer=reviewer)
        self.assertEqual(code, 3, diagnostic)
        self.assertIn("blocking", value["error"])

        verified = 0
        for index in range(1, CAP):
            reviewed = self.commit_fix(f"fix-{index}.py", f"value = {index + 1}\n")

            def verification(report, state, argv, reviewed=reviewed, index=index):
                self.assertIn("--verify-report", argv)
                self.assertEqual(argv[argv.index("--base") + 1], reviewed)
                report["subject"]["base"] = reviewed
                report["verification_report_digest"] = ship.digest(state["passes"][index - 1]["report"])

            reviewer, verify_calls = self.native_reviewer(review_id, shape=verification)
            self.success("review", "--review-id", review_id, reviewer=reviewer)
            self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
            verified += len(verify_calls)

        # The cap is spent, so the next head needs an explicit request, and
        # the renewal digest belongs to the pass after that one.
        reviewed = self.commit_fix("more.py", "value = 99\n")
        self.refused("review", "--review-id", review_id, pattern="spent|explicit new request")
        _key, (_revision, state) = self.record(review_id)
        self.refused(
            "review", "--review-id", review_id, "--additional-review-for", self.head,
            "--request-reason", "fixture continuation assertion",
            "--review-history-digest", state["history_digest"], pattern="renews only after",
        )
        # The request follows a completed pass on an earlier head, so it
        # verifies that fix as an automatic pass would (sd:2147).
        def extra_verification(report, state, argv):
            self.assertIn("--verify-report", argv)
            self.assertEqual(argv[argv.index("--base") + 1], reviewed)
            report["subject"]["base"] = reviewed
            report["verification_report_digest"] = ship.digest(state["passes"][CAP - 1]["report"])

        reviewer, extra_calls = self.native_reviewer(review_id, shape=extra_verification)
        self.success(
            "review", "--review-id", review_id, "--additional-review-for", self.head,
            "--request-reason", "fixture continuation assertion", reviewer=reviewer,
        )
        cleared = self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
        self.assertEqual([len(calls), verified, len(extra_calls)], [1, CAP - 1, 1])
        self.assertEqual(cleared["spent_passes"], CAP + 1)

        _key, (_revision, state) = self.record(review_id)
        passes = state["passes"]
        request = passes[CAP]["additional_review_request"]
        self.assertEqual(request["prior_history_digest"], no_item.combined_digest(state, passes[:CAP]))
        self.assertNotEqual(request["prior_history_digest"], ship.digest(passes[:CAP]))

    def test_a_second_import_never_overwrites_the_import_that_landed_first(self):
        """Eligibility is read before the lock, so it is read again inside it.

        Parsing a manifest and walking its ancestry is the slow part, and it
        happens outside the lock. A competing import that finishes that work
        first and lands is what the second one used to overwrite, recomputing
        the stored digest over a budget three passes shorter.
        """

        review_id = self.create()
        landed_path, _manifest = self.manifest(3)
        second_path, _manifest = self.manifest(2)
        original = no_item.import_claim
        landed = []

        def racing_claim(root, args, facts):
            value = original(root, args, facts)
            if not landed:
                landed.append(None)
                landed[0] = self.success(
                    "review", "--review-id", review_id, "--import-history", str(landed_path),
                    "--assert-history-complete",
                )
            return value

        with patch.object(no_item, "import_claim", racing_claim):
            code, value, diagnostic = self.cli(
                "review", "--review-id", review_id, "--import-history", str(second_path),
                "--assert-history-complete",
            )
        self.assertEqual(code, 3, diagnostic)
        self.assertRegex(value["error"], "already holds history")
        self.assertEqual(landed[0]["spent_passes"], 3)
        _key, (_revision, state) = self.record(review_id)
        self.assertEqual(len(state["historical_passes"]), 3)
        self.assertEqual(state["history_digest"], no_item.combined_digest(state))

    def test_a_closed_alias_frees_the_branch_name_but_never_its_content(self):
        """Closing releases a name. It never releases the work behind it.

        The branch index is the only claim a closed record gives up, so the
        released name can carry unrelated committed work. The same tree under
        another name is the same work, and it keeps the budget it already spent
        instead of being handed a fresh one.
        """
        closed = self.create()
        reviewer, calls = self.native_reviewer(closed)
        self.success("review", "--review-id", closed, reviewer=reviewer)
        self.assertEqual(len(calls), 1)
        self.success("review", "--review-id", closed, "--close-record", "fixture abandoned")

        # Identical content under a second name gets no second budget.
        _git(self.root, "checkout", "-q", "-b", "copied-topic")
        failure = self.refused(
            "review", "--create-record", "--assert-new-work", pattern="existing|record|owned"
        )
        self.assertIn(closed, failure["error"])
        self.assertEqual(len(self.keys()), 1)

        # The released name does carry work that is not the closed record's.
        _git(self.root, "checkout", "-q", "-B", "topic", "origin/main")
        self.head = _git(self.root, "rev-parse", "HEAD")
        self.commit_fix("separate.py", "value = 9\n")
        active = self.create()
        self.assertNotEqual(active, closed)
        _key, (_revision, fresh) = self.record(active)
        self.assertEqual(fresh["passes"], [])
        _key, (_revision, retained) = self.record(closed)
        self.assertEqual(len(retained["passes"]), 1)
        self.assertEqual(retained["lifecycle"], "closed")

    def test_close_and_reopen_invalidate_acceptance_and_refuse_concurrent_writes(self):
        """Acceptance binds an identity revision, and the lifecycle moves it.

        Closing and reopening preserve every spent pass, so the budget is not
        refunded, but the accepted clearance they carried no longer counts. A
        write that lands between the in-lock read and the identity write leaves
        the close refusing rather than overwriting it.
        """
        review_id, _proposal, proposal_path = self.blocking_proposal()
        self.accept(review_id, proposal_path)
        self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
        key, (_revision, before) = self.record(review_id)

        self.success("review", "--review-id", review_id, "--close-record", "fixture abandoned")
        self.success("review", "--review-id", review_id, "--reopen-record")
        _key, (_revision, after) = self.record(review_id)
        self.assertEqual(after["passes"], before["passes"])
        self.assertEqual(after["historical_passes"], before["historical_passes"])
        self.assertEqual(after["identity_revision"], before["identity_revision"] + 2)
        self.refused(
            "verify-review", "--review-id", review_id, "--expected-head", self.head,
            pattern="accept|bind|digest|revision|blocking|clearance",
        )

        # The competitor lands between the record read and the transaction,
        # which is where a real one lands: `close_record` releases the branch
        # index and writes the identity inside one transaction, so a writer
        # that got in after the read makes the versioned save refuse. Racing
        # inside `claim_branch` would now be racing inside that transaction,
        # where a second connection gets `database is locked` rather than the
        # version conflict this case is about.
        original = no_item.library_transaction
        landed = []

        def racing_transaction(connection):
            if not landed:
                landed.append(None)
                revision, state = receipts.read(self.connection, key)
                receipts.save(self.connection, key, revision,
                              dict(state, identity_revision=state["identity_revision"] + 1))
            return original(connection)

        with patch.object(no_item, "library_transaction", racing_transaction):
            code, value, diagnostic = self.cli(
                "review", "--review-id", review_id, "--close-record", "fixture race"
            )
        self.assertEqual(code, 3, diagnostic)
        self.assertRegex(value.get("error", ""), "concurrent|changed")
        _key, (_revision, unchanged) = self.record(review_id)
        self.assertEqual(unchanged["lifecycle"], "active")
        self.assertEqual(unchanged["passes"], after["passes"])
        # A refused close must leave nothing half-done. `close_record` gives up
        # the branch index before its versioned `write_identity`, so reading
        # the record alone would pass even if the claim had been released and
        # the name left free for a later allocation to take. The index is the
        # only claim a close releases, so it is where a partial close shows.
        index = self.index_entry(unchanged)
        self.assertEqual(
            index.get("review_id"), review_id,
            "the refused close released the branch claim anyway",
        )

    def test_a_concurrent_record_change_refuses_the_recording(self):
        """The dispositions belong to the record their template was read from.

        The template and the recording are separate commands, so a record can
        move between them. The proposal binds the identity revision the
        lifecycle raises, so a record that changed no longer binds its own
        proposal and no adjudication receipt is written.
        """
        review_id, _proposal, proposal_path = self.blocking_proposal()
        args = (
            "--review-id", review_id, "--expected-head", self.head,
            "--dispositions-file", str(proposal_path),
        )
        original = no_item.open_review
        landed = []

        def racing_open(root, connection, database, cli_args, store, runtime):
            review = original(root, connection, database, cli_args, store, runtime)
            if cli_args.dispositions_file is not None and not landed:
                landed.append(None)
                self.success("review", "--review-id", review_id, "--rebind-branch", "topic")
            return review

        with patch.object(no_item, "open_review", racing_open):
            code, value, diagnostic = self.cli("adjudicate", *args)
        self.assertEqual(landed, [None], diagnostic)
        self.assertEqual(code, 3, diagnostic)
        self.assertRegex(value.get("error", ""), "does not bind the current review")
        self.assertEqual(self.keys("ship-adjudication-no-item:"), [])

    def test_rebinding_a_closed_record_leaves_another_record_its_branch(self):
        """A stored branch name is a name, not current ownership.

        A closed record released its alias, and the next record took it. The
        rebind of the closed one released that branch unconditionally, which
        erased a live claim and left the branch free for a second budget.
        """

        closed = self.create()
        self.success("review", "--review-id", closed, "--close-record", "fixture abandoned")
        _git(self.root, "checkout", "-q", "-b", "revived")
        # Unrelated work on the released branch name, so the new record's own
        # head and tree claims never collide with the closed record's.
        _git(self.root, "checkout", "-q", "-B", "topic", "origin/main")
        self.head = _git(self.root, "rev-parse", "HEAD")
        self.commit_fix("separate.py", "value = 9\n")
        active = self.create()
        branch = no_item.index_key("fixture/repo", "branch", "topic")
        self.assertEqual(receipts.read(self.connection, branch)[1]["review_id"], active)

        _git(self.root, "checkout", "-q", "revived")
        self.head = _git(self.root, "rev-parse", "HEAD")
        self.success("review", "--review-id", closed, "--rebind-branch", "topic")
        _key, (_revision, state) = self.record(closed)
        self.assertEqual(state["branch"], "revived")
        self.assertEqual(
            receipts.read(self.connection, no_item.index_key("fixture/repo", "branch", "revived"))[1]["review_id"],
            closed,
        )
        # The active record still owns the branch it was allocated on.
        self.assertEqual(receipts.read(self.connection, branch)[1]["review_id"], active)

    def test_a_merged_record_closes_and_releases_the_branch_it_no_longer_needs(self):
        """A record outlives its branch diff, and closing it is how it ends.

        Once the work reaches the refreshed default branch, `base..head` is
        empty. That is the allocation and dispatch rule, and it used to run
        during identity lookup, which left a completed record permanently
        active and holding its branch alias against every later record.
        """

        review_id = self.create()
        branch_index = no_item.index_key("fixture/repo", "branch", "topic")
        self.assertEqual(receipts.read(self.connection, branch_index)[1]["review_id"], review_id)
        _git(self.remote.path, "update-ref", "refs/heads/main", self.head)

        # The eligibility rule itself is unchanged: no diff, no new record and
        # no further review of this one.
        self.refused("review", "--create-record", "--assert-new-work", pattern="committed diff")
        self.refused("review", "--review-id", review_id, pattern="committed diff")
        self.success("review", "--review-id", review_id, "--close-record", "fixture work merged")
        _key, (_revision, state) = self.record(review_id)
        self.assertEqual(state["lifecycle"], "closed")
        self.assertEqual(state["closed"]["reason"], "fixture work merged")
        self.assertIsNone(receipts.read(self.connection, branch_index)[1]["review_id"])
        self.success("review", "--review-id", review_id, "--reopen-record")
        self.assertEqual(receipts.read(self.connection, branch_index)[1]["review_id"], review_id)

    def _restore(self, key, original):
        revision, _current = receipts.read(self.connection, key)
        receipts.save(self.connection, key, revision, no_item.with_digest(json.loads(json.dumps(original))))

    def test_each_clearance_precondition_refuses_on_its_own(self):
        """Missing depth, a failed check and a stale source each refuse alone.

        The criterion names four conditions and the fourth already has a
        case: stale tool bindings is
        `test_each_review_manifest_member_mutation_refuses_no_item_clearance`,
        which enumerates the manifest rather than naming a file.

        Every mutation recomputes `history_digest`, so the record stays
        self-consistent and `stored_digest` never answers first. Without that
        each case refuses for the same unrelated reason and proves nothing.
        Clearance is re-run clean between cases, so the refusal belongs to the
        mutation and not to something a previous case left behind.
        """

        review_id = self.create()
        reviewer, calls = self.native_reviewer(review_id)
        self.success("review", "--review-id", review_id, reviewer=reviewer)
        self.assertEqual(len(calls), 1)
        self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
        key, (_revision, original) = self.record(review_id)

        def missing_depth(state):
            state["passes"][-1]["report"]["completed_reviews"] = 0

        def failed_check(state):
            state["passes"][-1]["report"]["check"] = {"status": "fail", "exit_code": 1}

        def stale_source(state):
            state["passes"][-1]["head"] = self.base

        cases = (("missing depth", missing_depth),
                 ("failed checks", failed_check),
                 ("stale source", stale_source))
        for condition, mutate in cases:
            with self.subTest(condition=condition):
                revision, _current = receipts.read(self.connection, key)
                mutated = json.loads(json.dumps(original))
                mutate(mutated)
                receipts.save(self.connection, key, revision, no_item.with_digest(mutated))
                self.refused(
                    "verify-review", "--review-id", review_id, "--expected-head", self.head,
                    pattern="receipt is incomplete|does not name this exact head",
                )
                self._restore(key, original)
                self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)

    def _fresh_three_import(self, label):
        """A private database per case: a spent pass is never spent twice."""
        self.database = self.directory / f"continuation-{label}.db"
        initialise(self.database)
        self.connection = connect(self.database)
        self.addCleanup(self.connection.close)
        review_id, _manifest = self.import_history(3)
        _key, (_revision, state) = self.record(review_id)
        return review_id, state["history_digest"]

    def test_the_approved_import_continuation_rejects_each_malformed_input(self):
        """The fourth pass is approved; what it returns still has to hold.

        A reservation is written before the provider runs, so these cases
        cannot use `refused`, which asserts the database never moved. The
        spent pass is the point: an approval buys one dispatch, and a dispatch
        that comes back malformed does not buy another.
        """

        def incomplete_aggregate(report, _state, argv):
            prior = json.loads(pathlib.Path(argv[argv.index("--resume-report") + 1]).read_bytes())
            prior["history"] = prior["history"][:-1]
            report["resume_report_digest"] = ship.digest(prior)

        def missing_authors(report, _state, argv):
            prior = json.loads(pathlib.Path(argv[argv.index("--resume-report") + 1]).read_bytes())
            prior["authored_with"] = []
            report["resume_report_digest"] = ship.digest(prior)

        def wrong_coverage(report, _state, _argv):
            report["authorship_base"] = "0" * 40

        cases = (("incomplete-aggregate-input", incomplete_aggregate),
                 ("missing-author-exclusions", missing_authors),
                 ("incorrect-full-branch-coverage", wrong_coverage))
        for label, shape in cases:
            with self.subTest(condition=label):
                review_id, prefix = self._fresh_three_import(label)
                reviewer, calls = self.native_reviewer(review_id, imported=3, shape=shape)
                code, value, diagnostic = self.cli(
                    "review", "--review-id", review_id, "--additional-review-for", self.head,
                    "--review-history-digest", prefix, "--request-reason", "fixture approval assertion",
                    reviewer=reviewer,
                )
                self.assertEqual(code, 3, diagnostic)
                self.assertRegex(value.get("error", ""), "full-branch coverage")
                self.assertEqual(len(calls), 1)
                _key, (_revision, state) = self.record(review_id)
                self.assertEqual(len(state["historical_passes"]), 3)
                self.assertEqual(len(state["passes"]), 1)
                # The evidence is retained rather than discarded, and the pass
                # stays spent. What it never does is clear the branch.
                self.assertIn("report", state["passes"][0])
                self.refused(
                    "verify-review", "--review-id", review_id, "--expected-head", self.head,
                    pattern="full-branch coverage",
                )

        # Stale combined-prefix approval: the continuation landed, and the
        # request stored with it no longer names the prefix it was approved
        # against. Clearance reads the request, not the dispatch that wrote it.
        review_id, prefix = self._fresh_three_import("stale-combined-prefix")
        reviewer, calls = self.native_reviewer(review_id, imported=3)
        self.success(
            "review", "--review-id", review_id, "--additional-review-for", self.head,
            "--review-history-digest", prefix, "--request-reason", "fixture approval assertion",
            reviewer=reviewer,
        )
        self.assertEqual(len(calls), 1)
        self.success("verify-review", "--review-id", review_id, "--expected-head", self.head)
        key, (revision, state) = self.record(review_id)
        self.assertNotEqual(state["passes"][0]["additional_review_request"]["prior_history_digest"], "0" * 64)
        state["passes"][0]["additional_review_request"]["prior_history_digest"] = "0" * 64
        receipts.save(self.connection, key, revision, no_item.with_digest(state))
        self.refused(
            "verify-review", "--review-id", review_id, "--expected-head", self.head,
            pattern="combined prior history",
        )

    def _racing_lock(self, action):
        """Enter the real lock, but let `action` land first.

        Both guarded paths read before they lock and read again inside it.
        Entering the lock is the last moment a concurrent writer could win, so
        it is where the race is injected. The real lock still runs, so the test
        proves the recheck and not the absence of locking.
        """

        original = receipts.repository_lock

        @contextlib.contextmanager
        def racing(database, repository):
            action(repository)
            with original(database, repository):
                yield

        return patch.object(receipts, "repository_lock", racing)

    def test_a_claim_landing_under_the_lock_refuses_creation_and_rebinding(self):
        """Creation and rebinding recheck inside the lock and orphan nothing.

        The racing writer calls `write_allocation`, the same function a real
        concurrent creation calls, so it leaves every claim that one leaves
        rather than the single index this case happens to read.
        """

        with patch.dict(os.environ, self.environment):
            facts = no_item.committed_facts(self.root)

        def allocate(_repository):
            no_item.write_allocation(self.connection, receipts, "concurrentrecord", facts)

        before = self.keys()
        with self._racing_lock(allocate):
            code, result, diagnostic = self.cli("review", "--create-record", "--assert-new-work")
        self.assertEqual(code, 3, diagnostic)
        self.assertRegex(result.get("error", ""), "already owns this")
        # The loser of the race allocates no budget: the only record key added
        # is the racing writer's own.
        self.assertEqual(
            [key for key in self.keys() if key not in before],
            [no_item.review_key(facts.repository, "concurrentrecord")],
        )

        # Rebinding, on a private database so the race above leaves no residue.
        self.database = self.directory / "rebind-race.db"
        initialise(self.database)
        self.connection = connect(self.database)
        self.addCleanup(self.connection.close)
        review_id = self.create()
        reviewer, calls = self.native_reviewer(review_id)
        self.success("review", "--review-id", review_id, reviewer=reviewer)
        self.assertEqual(len(calls), 1)
        key, (_revision, original) = self.record(review_id)
        _git(self.root, "branch", "-m", "renamed")
        # The reservations the criterion means are the index claims, not the
        # record payload: a rebind that claimed the new name and released the
        # old one before its stale save would leave the payload untouched.
        claims = {
            name: receipts.read(self.connection, no_item.index_key("fixture/repo", "branch", name))[1]
            for name in ("topic", "renamed")
        }
        self.assertEqual(claims["topic"].get("review_id"), review_id)

        def rename(_repository):
            revision, state = receipts.read(self.connection, key)
            state["branch"] = "someone-else-renamed-it"
            receipts.save(self.connection, key, revision, no_item.with_digest(state))

        with self._racing_lock(rename):
            code, result, diagnostic = self.cli(
                "review", "--review-id", review_id, "--rebind-branch", "topic"
            )
        self.assertEqual(code, 3, diagnostic)
        self.assertRegex(result.get("error", ""), "changed concurrently")
        # The spent reservation survives the refusal, and no identity write
        # landed half-done: the revision the rebind would have raised is intact.
        _key, (_revision, current) = self.record(review_id)
        self.assertEqual(current["passes"], original["passes"])
        self.assertEqual(current["historical_passes"], original["historical_passes"])
        self.assertEqual(current["identity_revision"], original["identity_revision"])
        self.assertEqual(
            {name: receipts.read(self.connection, no_item.index_key("fixture/repo", "branch", name))[1]
             for name in ("topic", "renamed")},
            claims,
        )


if __name__ == "__main__":
    unittest.main()
