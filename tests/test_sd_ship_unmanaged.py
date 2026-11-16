"""`sd-ship merge` and `prepare` in an unmanaged repository, through GitHub alone (sd:9100).

A row that says `repo.managed = 0` used to refuse every verb (sd:2566). For
`prepare` and `merge` it now reads readiness from GitHub only -- the open pull
request for the branch, its exact head, `reviewDecision`, the status rollup and
`mergeStateStatus` -- and merges by the repository's declared `merge_method`,
with `sha` the expected head. Every other verb still refuses.

A real database, as `test_sd_managed` uses, and a `gh` on PATH that answers
from a JSON file and logs each call, so a test can say what was never sent.
"""

from __future__ import annotations

import json
import pathlib
import subprocess  # nosec B404 - fixed argv
import sys
import tempfile
import unittest

import sd_db
from sd_db.repos import set_declaration

from tests.clean_env import clean_environment

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SHIP = REPO_ROOT / "bin" / "sd-ship"
SLUG = "example/theirs"
MERGE_PUT, PULL_GET = f"PUT repos/{SLUG}/pulls/7/merge", f"GET repos/{SLUG}/pulls/7"

#: A `gh api PATH --method M [--input -]` that answers `state["M PATH"]`, the path without its query.
FAKE_GH = """#!{python}
import json, os, sys
args = sys.argv[1:]
body = sys.stdin.read() if "--input" in args else None
path, method = args[1], args[args.index("--method") + 1]
key = method + " " + path.split("?")[0]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps({{"key": key, "path": path, "body": json.loads(body) if body else None}}) + "\\n")
with open(os.environ["FAKE_GH_STATE"]) as handle:
    answer = json.load(handle).get(key)
if answer is None or "fail" in answer:
    sys.stderr.write((answer or {{}}).get("fail", "gh: Not Found (HTTP 404)") + "\\n")
    sys.exit(1)
print(json.dumps(answer["json"]))
"""


class Unmanaged(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name).resolve()
        self.root = self.home / "theirs"
        programs = self.home / "programs"
        programs.mkdir()
        (programs / "gh").write_text(FAKE_GH.format(python=sys.executable))
        (programs / "gh").chmod(0o755)
        self.state_file, self.log = self.home / "gh-state.json", self.home / "gh-log.jsonl"
        #: What every child gets on top of `clean_environment`: this HOME and the fake `gh` first on PATH.
        self.extra = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
                      "FAKE_GH_STATE": str(self.state_file), "FAKE_GH_LOG": str(self.log),
                      "PATH": f"{programs}:{clean_environment()['PATH']}"}
        self.git_init(self.root)
        git = ["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.test"]
        for step in (["remote", "add", "origin", f"https://github.com/{SLUG}"],
                     ["commit", "-q", "--allow-empty", "-m", "base"], ["checkout", "-q", "-b", "work"],
                     ["commit", "-q", "--allow-empty", "-m", "change"]):
            subprocess.run([*git, *step], check=True, env=clean_environment(**self.extra))  # nosec B603 B607
        self.head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True,  # nosec B603 B607
                                   text=True, env=clean_environment(**self.extra)).stdout.strip()
        sd_db.initialise(home=self.home)
        self.connection = sd_db.connect(home=self.home)
        self.addCleanup(self.connection.close)
        self.key = self.register(self.root, managed=0)
        self.declare({"mode": "external", "merge_method": "rebase"})
        self.item = sd_db.create_item(self.connection, kind="work", title="theirs work", status="in_progress",
                                      repo=self.key, branch="work")
        self.connection.commit()
        self.github()

    def git_init(self, root: pathlib.Path) -> None:
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=clean_environment(**self.extra))  # nosec B603 B607

    def register(self, root: pathlib.Path, **columns: object) -> str:
        key = sd_db.add_repo(self.connection, root, home=self.home)
        sd_db.writes.upsert_repo(self.connection, key, remote=f"https://github.com/example/{root.name}", **columns)
        self.connection.commit()
        return key

    def declare(self, policy: dict | None) -> None:
        set_declaration(self.connection, self.key, "policy", policy)
        self.connection.commit()

    def github(self, *, pull: dict | None = None, readiness: dict | None = None, routes: dict | None = None) -> None:
        """What the fake GitHub answers: one open, ready PR #7 at the checkout's head unless told otherwise."""
        listed = {"number": 7, "state": "open", "merged_at": None, "merge_commit_sha": None,
                  "head": {"ref": "work", "sha": self.head}, **(pull or {})}
        ready = {"number": 7, "state": "OPEN", "headRefOid": self.head, "reviewDecision": "APPROVED",
                 "mergeStateStatus": "CLEAN",
                 "commits": {"nodes": [{"commit": {"oid": self.head, "statusCheckRollup": {"state": "SUCCESS"}}}]},
                 **(readiness or {})}
        state = {f"GET repos/{SLUG}/pulls": {"json": [listed]},
                 "POST graphql": {"json": {"data": {"repository": {"pullRequest": ready}}}},
                 f"GET repos/{SLUG}": {"json": {"full_name": SLUG, "allow_squash_merge": True,
                                                "allow_rebase_merge": True, "allow_merge_commit": False}},
                 MERGE_PUT: {"json": {"merged": True, "sha": "f" * 40}}}
        state.update(routes or {})
        self.state_file.write_text(json.dumps(state))

    def ship(self, *argv: str) -> tuple[int, dict]:
        done = subprocess.run([sys.executable, str(SHIP), *argv, "--json"], cwd=self.root,  # nosec B603
                              env=clean_environment(**self.extra), capture_output=True, text=True, timeout=120, check=False)
        try:
            return done.returncode, json.loads(done.stdout)
        except ValueError:
            self.fail(f"sd-ship printed no JSON: {done.stdout}{done.stderr}")

    def merge(self, *extra: str, head: str | None = None) -> tuple[int, dict]:
        return self.ship("merge", *(extra or ("--item", str(self.item))), "--expected-head", head or self.head)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def puts(self) -> list[dict]:
        return [call for call in self.calls() if call["key"].startswith("PUT ")]

    def notes(self) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT body FROM note WHERE item = ? AND kind = 'comment' ORDER BY id", (self.item,))]

    def blocker(self, result: dict) -> str | None:
        return (result.get("workflow", {}).get("blocker") or {}).get("code")


class AReadyMerge(Unmanaged):
    def test_merges_by_the_declared_method_at_the_expected_head_and_notes_the_item(self) -> None:
        code, result = self.merge()
        self.assertEqual(code, 0, result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["workflow"]["state"], "success")
        self.assertEqual([call["body"] for call in self.puts()], [{"sha": self.head, "merge_method": "rebase"}])
        self.assertEqual((result["pull"], result["merge_method"], result["merge_commit"]), (7, "rebase", "f" * 40))
        self.assertEqual(len(notes := self.notes()), 1, notes)
        for word in ("#7", "rebase", "f" * 40):
            self.assertIn(word, notes[0])

    def test_no_item_merges_and_writes_no_note(self) -> None:
        code, result = self.merge("--no-item")
        self.assertEqual(code, 0, result)
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual(self.notes(), [])

    def test_no_sd_review_gate_or_receipt_runs(self) -> None:
        self.merge()
        receipts = self.connection.execute("SELECT count(*) FROM state WHERE key LIKE 'ship:%'").fetchone()[0]
        self.assertEqual(receipts, 0, "the GitHub-only path wrote a ship receipt")


class ARefusedMerge(Unmanaged):
    def assert_refused(self, code: str, result: dict, exit_code: int) -> None:
        self.assertEqual(exit_code, 3, result)
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.blocker(result), code, result)
        self.assertEqual(self.puts(), [], "a refused merge reached the merge PUT")

    def test_no_declared_merge_method_refuses_naming_the_command(self) -> None:
        self.declare(None)
        exit_code, result = self.merge()
        self.assert_refused("merge_method_undeclared", result, exit_code)
        self.assertIn(f"sd-db repo declare {self.key} policy", result["error"])
        self.assertIn('"merge_method": "squash"', result["error"])

    def test_each_unready_answer_refuses_with_its_own_code(self) -> None:
        cases = {"head_mismatch": {"headRefOid": "a" * 40},
                 "not_approved": {"reviewDecision": "REVIEW_REQUIRED"},
                 "checks_not_passing": {"commits": {"nodes": [{"commit": {
                     "oid": self.head, "statusCheckRollup": {"state": "FAILURE"}}}]}},
                 "merge_state_not_clean": {"mergeStateStatus": "BLOCKED"}}
        for code, readiness in cases.items():
            with self.subTest(code):
                self.log.unlink(missing_ok=True)
                self.github(readiness=readiness)
                exit_code, result = self.merge()
                self.assert_refused(code, result, exit_code)
                self.assertIn("readiness", result)

    def test_an_expected_head_the_pull_request_lacks_refuses(self) -> None:
        exit_code, result = self.merge(head="b" * 40)
        self.assert_refused("head_mismatch", result, exit_code)

    def test_checks_github_reports_for_another_commit_do_not_pass(self) -> None:
        self.github(readiness={"commits": {"nodes": [{"commit": {
            "oid": "c" * 40, "statusCheckRollup": {"state": "SUCCESS"}}}]}})
        exit_code, result = self.merge()
        self.assert_refused("checks_not_passing", result, exit_code)

    def test_a_method_github_does_not_allow_refuses(self) -> None:
        self.declare({"merge_method": "merge"})
        exit_code, result = self.merge()
        self.assert_refused("merge_method_not_allowed", result, exit_code)

    def test_no_open_pull_request_refuses(self) -> None:
        self.github(pull={"state": "closed"})
        exit_code, result = self.merge()
        self.assert_refused("no_pull_request", result, exit_code)

    def test_an_unread_readiness_refuses_as_unknown(self) -> None:
        self.github(routes={"POST graphql": {"fail": "gh: Bad Gateway (HTTP 502)"}})
        exit_code, result = self.merge()
        self.assertEqual(exit_code, 3, result)
        self.assertEqual(self.puts(), [])

    def test_an_item_of_another_repository_refuses_before_any_github_call(self) -> None:
        other = self.home / "other"
        self.git_init(other)
        foreign = sd_db.create_item(self.connection, kind="work", title="other work", status="in_progress",
                                    repo=self.register(other, managed=1), branch="work")
        self.connection.commit()
        exit_code, result = self.merge("--item", str(foreign))
        self.assert_refused("item_repository_mismatch", result, exit_code)
        self.assertEqual(self.calls(), [])


class AMergeInSteps(Unmanaged):
    """The merge, then the note: a run stopped between them is finished by the next."""

    def test_a_lost_merge_response_reads_the_pull_request_back_once(self) -> None:
        self.github(routes={MERGE_PUT: {"fail": "gh: Service Unavailable (HTTP 503)"},
                            PULL_GET: {"json": {"number": 7, "merged": True, "merge_commit_sha": "e" * 40,
                                                "head": {"sha": self.head}}}})
        code, result = self.merge()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["merge_commit"], "e" * 40)
        self.assertEqual(len(self.puts()), 1, "merge was sent twice")

    def test_a_refused_merge_that_did_not_land_stays_refused(self) -> None:
        self.github(routes={MERGE_PUT: {"fail": "gh: Method Not Allowed (HTTP 405)"},
                            PULL_GET: {"json": {"number": 7, "merged": False, "head": {"sha": self.head}}}})
        code, result = self.merge()
        self.assertEqual(code, 3, result)
        self.assertEqual(self.notes(), [])

    def test_a_rerun_after_the_merge_landed_writes_the_missing_note_once(self) -> None:
        self.github(pull={"state": "closed", "merged_at": "2026-11-16T00:00:00Z", "merge_commit_sha": "d" * 40})
        for _ in range(2):
            code, result = self.merge()
            self.assertEqual(code, 0, result)
            self.assertEqual(result["merge_commit"], "d" * 40)
        self.assertEqual(self.puts(), [], "a merged pull request was merged again")
        self.assertEqual(len(self.notes()), 1, self.notes())


class APrepare(Unmanaged):
    def test_a_ready_pull_request_exits_0_with_its_readiness(self) -> None:
        code, result = self.ship("prepare", "--item", str(self.item))
        self.assertEqual(code, 0, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["workflow"]["state"], "success")
        self.assertIsNone(result["workflow"]["blocker"])
        self.assertEqual(result["readiness"]["head"], self.head)
        self.assertEqual(self.puts(), [])

    def test_an_unready_pull_request_exits_3_naming_why(self) -> None:
        self.github(readiness={"reviewDecision": "CHANGES_REQUESTED"})
        code, result = self.ship("prepare", "--no-item")
        self.assertEqual(code, 3, result)
        self.assertEqual(self.blocker(result), "not_approved")
        self.assertEqual(self.puts(), [])

    def test_a_local_head_the_pull_request_lacks_is_not_ready(self) -> None:
        self.github(readiness={"headRefOid": "a" * 40}, pull={"head": {"ref": "work", "sha": "a" * 40}})
        code, result = self.ship("prepare", "--no-item")
        self.assertEqual((code, self.blocker(result)), (3, "head_mismatch"), result)


class OtherPaths(Unmanaged):
    def test_other_verbs_still_refuse_an_unmanaged_checkout(self) -> None:
        for verb in ("observe", "reconcile"):
            with self.subTest(verb):
                code, result = self.ship(verb, "--item", str(self.item))
                self.assertEqual((code, self.blocker(result)), (3, "unmanaged_repository"), result)
        self.assertEqual(self.calls(), [])

    def test_a_managed_checkout_never_takes_the_github_only_path(self) -> None:
        self.connection.execute("UPDATE repo SET managed = 1 WHERE path = ?", (self.key,))
        self.connection.commit()
        _code, result = self.merge()
        self.assertNotIn("readiness", result)
        self.assertNotIn("POST graphql", [call["key"] for call in self.calls()])
        self.assertEqual(self.puts(), [])


if __name__ == "__main__":
    unittest.main()
