"""`sd-ship lane` (sd:2524): a serial ship queue per repository that outlives its session.

The acceptance test is the first: an entry one process enqueued is there for
the next process to read. The runner tests replace `sd-ship` with a recorder,
so nothing reaches GitHub or a review.
"""

from __future__ import annotations

import calendar
import contextlib
import fcntl
import importlib.util
import io
import json
import math
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_lane  # noqa: E402
import sd_ship_body  # noqa: E402

from tests.clean_env import clean_environment  # noqa: E402
from tests.row_seed import seed_declaration  # noqa: E402


def git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


#: An `sd-ship` that prints `sd_ship_workflow.failure` of prepare's incomplete-review `Refusal`, as `main` does,
#: and passes `--retry-review` (sd:3482).
FAKE_PREPARE = """import json, sys
sys.path.insert(0, {bin!r})
from sd_ship_remote import Refusal
from sd_ship_workflow import failure
if "--retry-review" in sys.argv:
    print(json.dumps({{"ok": True, "phase": "ready_to_send", "head": "head-1"}}))
    raise SystemExit(0)
refused = Refusal("the preceding review did not complete its requested depth; use --retry-review for a "
                  "full-branch retry", code="review_incomplete", boundary="review", state="operator_decision",
                  next_action="Prepare again with --retry-review; it spends one automatic pass.")
print(json.dumps(failure("prepare", refused), indent=2, sort_keys=True))
raise SystemExit(3)
"""


#: A lane runner TERMed mid-prepare while its speculative gate runs (sd:5776). Every signal the runner sends a
#: gate's group goes to `sent`, and prepare's end to `prepare-ended`, both on this process's monotonic clock.
TERMED_RUNNER = """import json, os, pathlib, sys, time
from unittest import mock
sys.path.insert(0, {bin!r})
import sd_lane, sd_lib
repo, environ, marks = pathlib.Path(sys.argv[1]), json.loads(sys.argv[2]), pathlib.Path(sys.argv[3])
CHILD = "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
end_group = sd_lane.end_group

def noted(group, number):
    with (marks / "sent").open("a") as handle:
        handle.write(f"{{group}} {{number}} {{time.monotonic()}}\\n")
    end_group(group, number)

def gate(root, head, base):
    mark = marks / f"gate-{{pathlib.Path(root).name}}.pid"
    code, _, _ = sd_lane.gate_run()([sys.executable, "-c", CHILD, str(mark)], dict(os.environ), root, 120)
    return {{"status": "failure", "summary": f"exit {{code}}"}}

def ship(argv, log):
    for _ in range(1200):  # every gate's child runs before prepare starts
        if sum(1 for mark in marks.glob("gate-*.pid") if mark.read_text()) >= int(sys.argv[4]):
            break
        time.sleep(0.05)
    try:
        sd_lib.run_group([sys.executable, "-c", CHILD, str(marks / "prepare.pid")], cwd=repo, env=dict(os.environ),
                         timeout=120)
    finally:
        (marks / "prepare-ended").write_text(str(time.monotonic()))
    return {{"ok": False, "error": "not reached"}}

with mock.patch.object(sd_lane, "end_group", noted), \\
        mock.patch.object(sd_lane, "default_note", lambda *args, **kwargs: "written"), \\
        mock.patch.object(sd_lane, "default_runner_merge", lambda root: "manual"), \\
        mock.patch("sd_db.database.served_by", lambda target, home=None: None, create=True):
    sd_lane.run_lane(repo, environ, ship, gate)
"""


def end_group(group: int) -> None:
    """Kill a group a failed test left running; one already gone is fine."""
    with contextlib.suppress(OSError):
        os.killpg(group, signal.SIGKILL)


class Lane(unittest.TestCase):
    #: `repo.runner_merge` for a suite with no database (sd:3132); None reads the suite's own database.
    runner_merge: str | None = "manual"
    #: Whether enqueue and retry read the item in the suite's database (sd:3473); most suites queue items it lacks.
    checks_item = False
    #: Whether enqueue runs prepare's docs lint (sd:5989); most suites queue trees the lint never sees.
    lints_docs = False

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name).resolve()
        self.repo = self.tmp / "pack"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.test")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "a")
        # The queue is in the hub database (sd:3282): a scratch one under this `HOME`, never the operator's.
        # A lane needs a GitHub origin; Git resolves this one to a bare repository here.
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(self.repo, "remote", "add", "origin", "https://github.com/example/pack.git")
        git(self.repo, "config", f"url.{self.origin}.insteadOf", "https://github.com/example/pack.git")
        git(self.repo, "push", "-q", "origin", "main")
        # The lane publishes and prepares a feature branch only; `self.repo` stays the main checkout on main.
        self.feature = self.worktree("feature")
        import sd_db
        sd_db.initialise(home=self.tmp)
        self.root = self.tmp / "lanes"
        self.environ = {sd_lane.ROOT_VARIABLE: str(self.root), "HOME": str(self.tmp)}
        self.body = self.tmp / "body.md"
        self.body.write_text("Item: sd:1\n", encoding="utf-8")
        self.calls: list[list[str]] = []
        self.answers: dict[tuple[int, str], dict] = {}
        # A landing notes its item (sd:2568); no test writes to a real workflow database.
        self.notes: list[tuple[int, str, pathlib.Path]] = []
        self.note_kinds: list[str] = []
        patcher = mock.patch.object(sd_lane, "default_note", self.note)
        patcher.start()
        self.addCleanup(patcher.stop)
        if self.runner_merge is not None:
            patcher = mock.patch.object(sd_lane, "default_runner_merge", lambda root: self.runner_merge)
            patcher.start()
            self.addCleanup(patcher.stop)
        if not self.checks_item:
            patcher = mock.patch.object(sd_lane, "check_item", lambda *args: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        if not self.lints_docs:
            patcher = mock.patch.object(sd_lane, "check_docs", lambda *args: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        # And each runs as the hub would, which hosts every lane whose `repo.lane_host` is NULL (sd:3003).
        if importlib.util.find_spec("sd_db") is not None:
            patcher = mock.patch("sd_db.database.served_by", lambda target, home=None: None, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def register_items(self, count: int, remote: str = "https://github.com/example/pack.git") -> list[int]:
        """This checkout as a repository with `remote`, and `count` items of it, as prepare reads them (sd:3473)."""
        import sd_db
        connection = sd_db.connect(home=self.tmp)
        try:
            sd_db.upsert_repo(connection, str(self.repo), remote=remote, managed=1)
            return [sd_db.create_item(connection, kind="task", title=f"item {number}", status="in_progress",
                                      repo=str(self.repo)) for number in range(count)]
        finally:
            connection.close()

    def note(self, item: int, body: str, main: pathlib.Path, kind: str = "comment") -> str:
        self.notes.append((item, body, main))
        self.note_kinds.append(kind)
        return "written"

    def worktree(self, name: str) -> pathlib.Path:
        path = self.tmp / name
        git(self.repo, "worktree", "add", "-q", "-b", name, str(path))
        return path

    def ship(self, argv: list[str], log: pathlib.Path) -> dict:
        """The recorder: `(item, verb)` answers, ready and merged by default."""
        self.calls.append(argv)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("whole output " * 1000, encoding="utf-8")
        verb, item = argv[2], int(argv[argv.index("--item") + 1])
        default = ({"ok": True, "phase": "ready_to_send", "head": f"head-{item}"} if verb == "prepare"
                   else {"ok": True, "phase": "merged", "merge_commit": f"merged-{item}"})
        return self.answers.get((item, verb), default)

    def store(self) -> sd_lane.Queue:
        return sd_lane.queue_for(self.repo, self.environ)

    def entries(self) -> list[dict]:
        return sd_lane.read_queue(self.store())


class QueueOutlivesTheProcess(Lane):
    checks_item = True  # the verbs run in a child process, which no patch here reaches
    lints_docs = True

    def setUp(self) -> None:
        super().setUp()
        self.register_items(7)
        # The docs lint reads the privacy patterns (sd:5989): this fixture's, never the operator's. The child
        # gets them in `sd_ship`; `enqueue_entry` lints in this process, so `os.environ` names them too (sd:6445).
        self.config = self.tmp / "config"
        self.config.mkdir()
        (self.config / "privacy-patterns").write_text("secret-host\\.example\\.test\n", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"SYSTEM_TOOLS_CONFIG": str(self.config)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def sd_ship(self, cwd: pathlib.Path, *args: str, chdir: str | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, **self.environ, "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
               "SYSTEM_TOOLS_CONFIG": str(self.config), "JEV_SD_DOCS_LINT": "0"}
        return subprocess.run([sys.executable, str(REPO_ROOT / "bin/sd-ship"), *(["-C", chdir] if chdir else []),
                               "lane", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)

    def test_a_c_that_names_a_file_refuses_each_lane_verb_without_a_traceback(self) -> None:
        """sd:5773. `sd-ship -C <file> lane list` raised AttributeError: lane verbs take no --json."""
        for verb in (["list"], ["cancel", "7"], ["retry", "7"], ["move", "7", "top"], ["hold", "7"], ["release", "7"],
                     ["run"], ["prune"], ["prune", "--json"], ["watch", "--item", "7"],
                     ["enqueue", "--item", "7", "--title", "fix: t", "--body-file", str(self.body), "--deliver"]):
            done = self.sd_ship(self.repo, *verb, chdir=str(self.body))
            self.assertEqual((done.returncode, done.stderr), (3, ""), verb)
            answer = json.loads(done.stdout)
            self.assertEqual((answer["ok"], answer["error"]), (False, f"-C {self.body}: not a directory"), verb)

    def test_an_entry_one_process_enqueued_is_read_by_the_next(self) -> None:
        tree = self.worktree("topic")
        head = git(tree, "rev-parse", "HEAD")
        done = self.sd_ship(tree, "enqueue", "--item", "7", "--title", "fix: Topic", "--body-file", str(self.body), "--deliver")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        listed = self.sd_ship(self.repo, "list")
        self.assertEqual(listed.returncode, 0, listed.stderr + listed.stdout)
        answer = json.loads(listed.stdout)
        self.assertEqual(answer["queue"], str(self.root / "pack/lane/queue/queue.json"))
        [entry] = answer["entries"]
        self.assertEqual({key: entry[key] for key in ("worktree", "item", "expected_head", "title", "status", "branch")},
                         {"worktree": str(tree), "item": 7, "expected_head": head, "title": "fix: Topic", "status": "pending",
                          "branch": "topic"})
        # The entry holds the body's text, and `list` prints its size (sd:3282); nothing goes to `bodies/`.
        self.assertEqual((entry["body_bytes"], "body" in entry), (len(self.body.read_bytes()), False))
        self.assertFalse((self.root / "pack/lane/bodies").exists())

    def test_retry_prints_json_as_the_other_verbs_do(self) -> None:
        """sd:3254. The system Queue page calls `lane retry`; it reads the same JSON shape."""
        sd_lane.enqueue_entry(self.feature, 7, "fix: Topic", self.body, self.environ, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        done = self.sd_ship(self.repo, "retry", "7", "--manual")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        answer = json.loads(done.stdout)
        self.assertEqual((answer["ok"], answer["item"], answer["authority"], answer["status"]), (True, 7, "manual", "pending"))
        again = self.sd_ship(self.repo, "retry", "7")
        self.assertEqual((again.returncode, json.loads(again.stdout)["ok"]), (3, False))

    def test_retry_expected_head_refuses_another_head_and_names_both(self) -> None:
        """sd:3268. The Queue page sends the head it showed; another blocked head is refused with `stale_head`."""
        sd_lane.enqueue_entry(self.feature, 7, "fix: Topic", self.body, self.environ, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        head = self.entries()[0]["expected_head"]
        done = self.sd_ship(self.repo, "retry", "7", "--expected-head", "0" * 40)
        answer = json.loads(done.stdout)
        self.assertEqual((done.returncode, answer["ok"], answer["code"]), (3, False, sd_lane.STALE_HEAD), done.stdout)
        self.assertIn(head, answer["error"])
        self.assertIn("0" * 40, answer["error"])
        self.assertEqual([row["status"] for row in self.entries()], ["prepared"])
        done = self.sd_ship(self.repo, "retry", "7", "--manual", "--expected-head", head)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual(json.loads(done.stdout)["expected_head"], head)

    def test_retry_takes_a_cancelled_entry_and_its_help_says_so(self) -> None:
        """sd:3389. The system Queue page re-queues a cancelled row; it reads `cancelled` in the help to offer it."""
        sd_lane.enqueue_entry(self.feature, 7, "fix: Topic", self.body, self.environ, claim="deliver")
        sd_lane.cancel(self.repo, 7, self.environ)
        head = self.entries()[0]["expected_head"]
        done = self.sd_ship(self.repo, "retry", "7", "--expected-head", head)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual((json.loads(done.stdout)["status"], json.loads(done.stdout)["retried"]["status"]),
                         ("pending", "cancelled"))
        helped = self.sd_ship(self.repo, "retry", "--help")
        self.assertIn("cancelled", " ".join(helped.stdout.split()))

    def test_an_entry_without_a_delivery_claim_is_refused_with_exit_3(self) -> None:
        done = self.sd_ship(self.feature, "enqueue", "--item", "7", "--title", "fix: Topic", "--body-file", str(self.body))
        self.assertEqual(done.returncode, 3, done.stderr + done.stdout)
        self.assertIn("--associate-only", json.loads(done.stdout)["error"])
        self.assertEqual(self.entries(), [])


class Enqueue(Lane):
    def test_the_lane_root_comes_from_the_setting_when_no_variable_names_it(self) -> None:
        config = self.tmp / "config"
        (config / "sd-ai-command-pack").mkdir(parents=True)
        (config / "sd-ai-command-pack/config.json").write_text(
            json.dumps({"config": {"sd": {"lane_root": str(self.tmp / "storage")}}}), encoding="utf-8")
        environ = {"XDG_CONFIG_HOME": str(config), "HOME": str(self.tmp)}
        self.assertEqual(sd_lane.queue_path(self.repo, environ), self.tmp / "storage/pack/lane/queue/queue.json")

    def test_an_item_already_queued_is_refused(self) -> None:
        sd_lane.enqueue_entry(self.feature, 3, "fix: t", self.body, self.environ, claim="deliver")
        with self.assertRaisesRegex(sd_lane.LaneError, "already queued"):
            sd_lane.enqueue_entry(self.feature, 3, "fix: t", self.body, self.environ, claim="deliver")

    def test_a_branch_stacked_on_another_open_entry_is_refused_and_names_it(self) -> None:
        """sd:6493. A squash merge of the base leaves the stacked branch carrying its pre-squash commits."""
        git(self.feature, "commit", "-q", "--allow-empty", "-m", "fix: base")
        sd_lane.enqueue_entry(self.feature, 1, "fix: base", self.body, self.environ, claim="deliver")
        stacked = self.tmp / "stacked"
        git(self.repo, "worktree", "add", "-q", "-b", "stacked", str(stacked), "feature")
        git(stacked, "commit", "-q", "--allow-empty", "-m", "fix: on top")
        with self.assertRaises(sd_lane.LaneError) as caught:
            sd_lane.enqueue_entry(stacked, 2, "fix: on top", self.body, self.environ, claim="deliver")
        self.assertEqual(caught.exception.code, sd_lane.STACKED)
        self.assertIn("sd:1", str(caught.exception))
        self.assertIn("feature", str(caught.exception))
        self.assertEqual([row["item"] for row in self.entries()], [1])
        self.assertNotIn("refs/heads/stacked", git(self.origin, "for-each-ref", "--format=%(refname)"))
        sd_lane.cancel(self.repo, 1, self.environ)  # no longer open: the stacked branch queues
        sd_lane.enqueue_entry(stacked, 2, "fix: on top", self.body, self.environ, claim="deliver")

    def test_a_cancelled_entry_is_not_run(self) -> None:
        sd_lane.enqueue_entry(self.feature, 3, "fix: t", self.body, self.environ, claim="deliver")
        sd_lane.cancel(self.repo, 3, self.environ)
        self.assertEqual(sd_lane.run_lane(self.repo, self.environ, self.ship), {"ran": []})
        self.assertEqual(self.calls, [])


class Runner(Lane):
    def test_entries_run_in_order_and_a_failure_does_not_stop_the_next(self) -> None:
        first, second = self.worktree("first"), self.worktree("second")
        sd_lane.enqueue_entry(first, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.enqueue_entry(second, 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([(int(c[c.index("--item") + 1]), c[2]) for c in self.calls],
                         [(1, "prepare"), (2, "prepare"), (2, "merge")])
        one, two = self.entries()
        self.assertEqual((one["status"], one["step"], one["reason"]), ("failed", "prepare", "a blocking finding"))
        self.assertEqual((two["status"], two["merge_commit"]), ("merged", "merged-2"))

    def test_prepare_reads_the_lanes_copy_of_the_body_once_the_original_is_gone(self) -> None:
        """sd:3170: a reboot that clears /tmp left entries naming a lost body; the lane keeps its own copy."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        self.body.unlink()
        read: list[tuple[str, str]] = []

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            if argv[2] == "prepare":
                named = argv[argv.index("--body-file") + 1]
                read.append((named, pathlib.Path(named).read_text(encoding="utf-8")))
            return self.ship(argv, log)

        sd_lane.run_lane(self.repo, self.environ, ship)
        [entry] = self.entries()
        self.assertEqual(entry["status"], "merged", entry.get("reason"))
        [(named, text)] = read
        self.assertEqual(text, "Item: sd:1\n")
        self.assertFalse(pathlib.Path(named).exists(), "the private body file outlived its prepare")

    def test_an_entry_holds_its_texts_and_enqueue_copies_no_file(self) -> None:
        """sd:3282: the body and acceptance texts live in the entry, so any host can run it and `retry` needs no file."""
        acceptance = self.tmp / "acceptance.md"
        acceptance.write_text("accepted\n", encoding="utf-8")
        sd_lane.enqueue_entry(self.feature, 1, "fix: t", self.body, self.environ, claim="deliver", acceptance_file=acceptance)
        [entry] = self.entries()
        self.assertEqual((entry["body"], entry["acceptance"], "body_file" in entry), ("Item: sd:1\n", "accepted\n", False))
        self.assertFalse(sd_lane.lane_dir(self.repo, self.environ).joinpath("bodies").exists())

    def test_a_blocked_entry_is_retried_at_its_head_with_its_kept_body(self) -> None:
        """sd:3254. A failed entry could not be queued again without its body file; retry needs none."""
        tree, acceptance = self.worktree("topic"), self.tmp / "acceptance.md"
        acceptance.write_text("accepted\n", encoding="utf-8")
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver",
                              acceptance_file=acceptance)
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        failed = self.entries()[0]
        self.body.unlink()
        retried = sd_lane.retry(self.repo, 1, self.environ)
        self.assertEqual({key: retried[key] for key in ("item", "expected_head", "title", "authority", "claim",
                                                        "acceptance", "body", "status")},
                         {"item": 1, "expected_head": failed["expected_head"], "title": "fix: one", "authority": "manual",
                          "claim": "deliver", "acceptance": "accepted\n", "body": "Item: sd:1\n", "status": "pending"})
        self.assertEqual(retried["retried"], {"status": "failed", "finished_at": failed["finished_at"]})
        with self.assertRaisesRegex(sd_lane.LaneError, "already queued"):
            sd_lane.retry(self.repo, 1, self.environ)
        del self.answers[(1, "prepare")]
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([row["status"] for row in self.entries()], ["failed", "merged"])

    def test_retry_manual_approves_an_entry_that_stopped_prepared(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[0]["code"], sd_lane.RUNNER_MERGE_MANUAL)
        self.assertEqual(sd_lane.retry(self.repo, 1, self.environ, manual=True)["authority"], "manual")
        self.calls.clear()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(([c[2] for c in self.calls], self.entries()[-1]["status"]), (["prepare", "merge"], "merged"))

    def test_retry_starts_from_prepares_catch_up_merge(self) -> None:
        tree = self.worktree("topic")
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "base moved")
        git(self.repo, "push", "-q", "origin", "main")  # prepare merges `origin/<default>`

        def catch_up_then_block(argv: list[str], log: pathlib.Path) -> dict:
            git(tree, "merge", "-q", "--no-ff", "-m", "Merge origin/main", "main")
            self.ship(argv, log)
            return {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        sd_lane.run_lane(self.repo, self.environ, catch_up_then_block)
        self.assertEqual(sd_lane.retry(self.repo, 1, self.environ)["expected_head"], git(tree, "rev-parse", "HEAD"))

    def test_retry_refuses_what_it_cannot_retry(self) -> None:
        with self.assertRaisesRegex(sd_lane.LaneError, "no entry"):
            sd_lane.retry(self.repo, 1, self.environ)
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        with self.assertRaisesRegex(sd_lane.LaneError, "already queued"):
            sd_lane.retry(self.repo, 1, self.environ)
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        with self.assertRaisesRegex(sd_lane.LaneError, "last entry is merged"):
            sd_lane.retry(self.repo, 1, self.environ)
        sd_lane.enqueue_entry(self.feature, 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.cancel(self.repo, 2, self.environ)
        with self.assertRaisesRegex(sd_lane.LaneError, "cancelled.*--expected-head"):
            sd_lane.retry(self.repo, 2, self.environ)
        # An entry that ended before its copy was kept has none to queue again.
        sd_lane.enqueue_entry(self.feature, 3, "fix: three", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(3, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        sd_lane.update(self.store(), lambda rows: rows[-1].update(body=None))
        with self.assertRaisesRegex(sd_lane.LaneError, "--body-file"):
            sd_lane.retry(self.repo, 3, self.environ)
        self.assertEqual([row["status"] for row in self.entries()], ["merged", "cancelled", "failed"])

    def test_retry_expected_head_is_checked_under_the_queue_lock(self) -> None:
        """sd:3268. An entry that ends at another head between retry's read and its write is refused, not retried."""
        tree = self.worktree("topic")
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        shown = self.entries()[0]["expected_head"]
        # Another head the worktree is not at yet, so only the write's read refuses (sd:6057).
        moved = git(tree, "commit-tree", f"{shown}^{{tree}}", "-p", shown, "-m", "fix")
        caught_up = sd_lane.caught_up

        def another_run_ends_first(worktree: pathlib.Path, expected: str) -> str:
            sd_lane.update(self.store(), lambda entries: entries.append(
                {**entries[0], "id": "later", "position": 2, "expected_head": moved}))
            return caught_up(worktree, expected)
        with mock.patch.object(sd_lane, "caught_up", another_run_ends_first):
            with self.assertRaises(sd_lane.LaneError) as refused:
                sd_lane.retry(self.repo, 1, self.environ, manual=True, expected_head=shown)
        self.assertEqual(refused.exception.code, sd_lane.STALE_HEAD)
        self.assertIn(moved, str(refused.exception))
        self.assertEqual([(row["status"], row["expected_head"]) for row in self.entries()],
                         [("failed", shown), ("failed", moved)])
        git(tree, "reset", "-q", "--hard", moved)
        self.assertEqual(sd_lane.retry(self.repo, 1, self.environ, expected_head=moved)["expected_head"], moved)

    def moved_above(self, item: int) -> tuple[str, str]:
        """`item` fails at one head, then again at a newer one that `move top` put above it; both heads."""
        tree = self.worktree(f"topic{item}")
        heads = []
        for message in ("first", "second"):
            git(tree, "commit", "-q", "--allow-empty", "-m", message)
            heads.append(git(tree, "rev-parse", "HEAD"))
            sd_lane.enqueue_entry(tree, item, "fix: t", self.body, self.environ, claim="deliver")
            if message == "second":
                sd_lane.move(self.repo, item, "top", self.environ)
            self.answers[(item, "prepare")] = {"ok": False, "error": f"{message} failure"}
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([row["expected_head"] for row in self.entries() if row["item"] == item], heads[::-1])
        return heads[0], heads[1]

    def test_retry_takes_the_newest_entry_when_a_move_put_it_above_older_ones(self) -> None:
        """sd:3412. After a hold and `move top`, the newest entry sits above an older finished one."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: held", self.body, self.environ, claim="deliver")
        sd_lane.set_hold(self.repo, 1, self.environ, held=True)
        old, new = self.moved_above(2)
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, 2, self.environ, expected_head=old)
        self.assertEqual(refused.exception.code, sd_lane.STALE_HEAD)
        self.assertEqual(sd_lane.retry(self.repo, 2, self.environ, expected_head=new)["expected_head"], new)
        _, new = self.moved_above(3)
        self.assertEqual(sd_lane.retry(self.repo, 3, self.environ)["expected_head"], new)

    def test_a_cancel_records_who_cancelled_it(self) -> None:
        """sd:3483. Thirteen cancelled entries carried an empty reason, while a release says who released it."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual(self.entries()[0]["reason"], f"cancelled by the operator on {sd_lane.this_host()}")

    def test_lane_list_gives_each_entry_its_enqueue_order(self) -> None:
        """sd:3419. A reader of `lane list` takes an item's newest entry by `enqueue_seq`, not by its place after a move."""
        import argparse  # noqa: PLC0415
        sd_lane.enqueue_entry(self.feature, 1, "fix: held", self.body, self.environ, claim="deliver")
        sd_lane.set_hold(self.repo, 1, self.environ, held=True)
        _, new = self.moved_above(2)
        parser = argparse.ArgumentParser()
        sd_lane.add_lane_verbs(parser.add_subparsers(dest="command"))
        with contextlib.chdir(self.repo), mock.patch.dict(os.environ, self.environ), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(sd_lane.lane_main(parser.parse_args(["lane", "list"])), 0)
        listed = json.loads(said.getvalue())["entries"]
        self.assertTrue(all(type(row.get("enqueue_seq")) is int for row in listed), listed)
        self.assertEqual([row["item"] for row in sorted(listed, key=lambda row: row["enqueue_seq"])], [1, 2, 2])
        # sd:3483: each entry's round among its item's entries, so three blocking rounds need no counting by hand.
        self.assertEqual([(row["item"], row.get("round")) for row in sorted(listed, key=lambda row: row["enqueue_seq"])],
                         [(1, 1), (2, 1), (2, 2)])
        newest = max((row for row in listed if row["item"] == 2), key=lambda row: row["enqueue_seq"])
        self.assertEqual(newest["expected_head"], new)
        self.assertNotEqual([row for row in listed if row["item"] == 2][-1]["expected_head"], new)

    def test_retry_refuses_a_newer_entry_written_between_its_read_and_its_write(self) -> None:
        """sd:3412. With no expected head, the write still finds the entry it read is no longer the item's newest."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        self.answers[(1, "prepare")] = {"ok": False, "error": "a failure"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        real = sd_lane.require_published

        def another_entry_ends_first(*args: Any) -> None:
            real(*args)
            sd_lane.update(self.store(), lambda rows: rows.insert(
                0, {**rows[0], "id": "later", "position": 0, "status": "skipped"}))
        with mock.patch.object(sd_lane, "require_published", another_entry_ends_first):
            with self.assertRaises(sd_lane.LaneError) as refused:
                sd_lane.retry(self.repo, 1, self.environ)
        self.assertEqual(refused.exception.code, sd_lane.SUPERSEDED)
        self.assertEqual([row["status"] for row in self.entries()], ["skipped", "failed"])

    def test_a_queue_write_that_fails_changes_nothing_and_says_hub_unavailable(self) -> None:
        """sd:3282: a write is one hub transaction; a fault in it leaves the entry as it was."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: t", self.body, self.environ, manual=True, claim="deliver")
        with mock.patch.object(sd_lane, "store", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sd_lane.LaneError) as refused:
                sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual((refused.exception.code, self.entries()[0]["status"]), (sd_lane.HUB_UNAVAILABLE, "pending"))

    def test_an_associate_only_entry_prepares_with_associate_only(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="associate-only")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertIn("--associate-only", self.calls[0])
        self.assertNotIn("--deliver", self.calls[0])

    def test_an_acceptance_file_is_forwarded_to_prepare(self) -> None:
        acceptance = self.tmp / "acceptance.json"
        acceptance.write_text("{}", encoding="utf-8")
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver", acceptance_file=acceptance)
        acceptance.unlink()  # the entry holds the text (sd:3282)
        read: list[str] = []

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            if argv[2] == "prepare":
                read.append(pathlib.Path(argv[argv.index("--acceptance-file") + 1]).read_text(encoding="utf-8"))
            return self.ship(argv, log)
        sd_lane.run_lane(self.repo, self.environ, ship)
        self.assertEqual((read, "--deliver" in self.calls[0]), (["{}"], True))

    def test_each_item_the_enqueue_names_reaches_prepare(self) -> None:
        """sd:3704: the enqueue's --item list is the closing set; prepare gets every one, the claimed item first."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver", closes=(2, 3, 1, 2))
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        items = [self.calls[0][index + 1] for index, part in enumerate(self.calls[0]) if part == "--item"]
        self.assertEqual(items, ["1", "2", "3"])

    def test_prepare_catches_up_with_the_base_inside_the_runner(self) -> None:
        """The catch-up merge runs under the lane lock, never before it."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertIn("--catch-up", self.calls[0])

    def test_the_merge_names_the_prepared_head(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        merge = self.calls[1]
        self.assertEqual((merge[merge.index("--expected-head") + 1], "--manual" in merge), ("head-1", True))

    def test_without_manual_authority_the_runner_stops_at_a_prepared_head(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([c[2] for c in self.calls], ["prepare"])
        self.assertEqual(self.entries()[0]["status"], "prepared")

    def test_a_worktree_that_moved_is_skipped_without_a_prepare(self) -> None:
        tree = self.worktree("topic")
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        git(tree, "commit", "-q", "--allow-empty", "-m", "moved")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.calls, self.entries()[0]["status"]), ([], "skipped"))

    def incomplete_review(self, retried: bool):
        """A recorder whose prepare refuses for an incomplete review, also on its retry when `retried`."""
        refused = {"ok": False, "phase": "prepare",
                   "error": "the preceding review did not complete its requested depth; use --retry-review",
                   "workflow": {"blocker": {"code": "review_incomplete"}}}
        def ship(argv: list[str], log: pathlib.Path) -> dict:
            answer = self.ship(argv, log)
            return refused if argv[2] == "prepare" and (retried or "--retry-review" not in argv) else answer
        return ship

    def test_an_incomplete_review_spends_the_one_automatic_retry(self) -> None:
        """sd:3037. The lane retries as an operator would, with prepare --retry-review."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.incomplete_review(retried=False))
        self.assertEqual([(c[2], "--retry-review" in c) for c in self.calls],
                         [("prepare", False), ("prepare", True), ("merge", False)])
        self.assertEqual(self.entries()[0]["status"], "merged")

    def test_sd_ships_own_review_incomplete_refusal_reaches_the_retry(self) -> None:
        """sd:3482. Two entries kept this refusal with no -retry.log; sd-ship's real refusal, printed, still retries.

        The fake `sd-ship` prints what `sd_ship_workflow.failure` makes of the
        `Refusal` prepare raises, exits 3 as `sd-ship` does, and runs under
        the runner's own `default_ship`.
        """
        fake = self.tmp / "fake-bin"
        fake.mkdir()
        (fake / "sd-ship").write_text(FAKE_PREPARE.format(bin=str(REPO_ROOT / "bin")), encoding="utf-8")
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        with mock.patch.object(sd_lane, "BIN", fake), mock.patch.dict(os.environ, self.environ):
            sd_lane.run_lane(self.repo, self.environ)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], entry["head"]), ("prepared", "head-1"), entry.get("reason"))
        self.assertTrue(entry["prepare_log"].endswith("-retry.log"), entry["prepare_log"])
        self.assertIn("--retry-review", pathlib.Path(entry["prepare_log"]).read_text(encoding="utf-8"))

    def test_a_prepare_that_reconciles_a_landed_merge_records_it_merged(self) -> None:
        """sd:3482. A retry after a death at merge: prepare answered `merged`, and the entry failed 'not ready'."""
        head = sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")["expected_head"]
        self.answers[(1, "prepare")] = {"ok": True, "phase": "merged", "head": head, "merge_commit": "landed-1"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], entry.get("merge_commit"), entry.get("reconciled")),
                         ("merged", "landed-1", True), entry.get("reason"))
        self.assertEqual([c[2] for c in self.calls], ["prepare"])  # no second merge
        self.assertIn("Landed: merged at landed-1", self.notes[0][1])

    def caught_up_topic(self) -> tuple[pathlib.Path, str, str]:
        """A worktree whose branch has a commit, then prepare's catch-up merge of a moved `origin/main` on it."""
        tree = self.worktree("topic")
        git(tree, "commit", "-q", "--allow-empty", "-m", "work")
        work = git(tree, "rev-parse", "HEAD")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "base moved")
        git(self.repo, "push", "-q", "origin", "main")
        git(tree, "merge", "-q", "--no-ff", "-m", "Merge origin/main", "main")
        return tree, work, git(tree, "rev-parse", "HEAD")

    def test_a_reconciled_merge_of_the_entrys_catch_up_records_it_merged(self) -> None:
        """sd:3482: the merge that landed was of prepare's catch-up of the queued head."""
        tree, work, caught = self.caught_up_topic()
        git(tree, "reset", "-q", "--hard", work)
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, claim="deliver")
        git(tree, "reset", "-q", "--hard", caught)
        self.answers[(1, "prepare")] = {"ok": True, "phase": "merged", "head": caught, "merge_commit": "landed-1"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[0]["status"], "merged", self.entries()[0].get("reason"))

    def test_a_reconciled_merge_of_an_earlier_head_is_not_a_catch_up_of_it(self) -> None:
        """Codex round 1: the queued head is a catch-up of the merged one; that merge is not this entry's."""
        tree, work, caught = self.caught_up_topic()
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, claim="deliver")
        self.answers[(1, "prepare")] = {"ok": True, "phase": "merged", "head": work, "merge_commit": "landed-0"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        landings = [body for _, body, _ in self.notes if body.startswith("Landed")]
        self.assertEqual((entry["status"], landings), ("failed", []))
        self.assertIn(f"merge of {work}, not this entry's head {caught}", entry["reason"])

    def test_a_reconciled_merge_whose_claim_was_released_lands_nothing(self) -> None:
        """Codex round 1: the claim must still hold before the landing deletes the branch and notes the item."""
        head = sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")["expected_head"]

        def released_meanwhile(argv: list[str], log: pathlib.Path) -> dict:
            sd_lane.update(self.store(), lambda rows: rows[0]["holder"].update(token="released"))
            self.ship(argv, log)
            return {"ok": True, "phase": "merged", "head": head, "merge_commit": "landed-1"}
        answer = sd_lane.run_lane(self.repo, self.environ, released_meanwhile)
        self.assertEqual((self.notes, self.entries()[0]["status"]), ([], "running"))
        self.assertIn("reclaimed or released", answer["stopped"])

    def test_a_reconciled_merge_of_another_head_is_not_this_entrys(self) -> None:
        """sd:3482, evidence: the receipt's merge must be of the entry's head; an earlier pull request's is not."""
        tree = self.worktree("topic")
        git(tree, "commit", "-q", "--allow-empty", "-m", "follow-up")
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, claim="deliver")
        earlier = git(self.repo, "rev-parse", "main")
        self.answers[(1, "prepare")] = {"ok": True, "phase": "merged", "head": earlier, "merge_commit": "landed-0"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        landings = [body for _, body, _ in self.notes if body.startswith("Landed")]
        self.assertEqual((entry["status"], entry.get("merge_commit"), landings), ("failed", None, []))
        self.assertIn(f"merge of {earlier}, not this entry's head", entry["reason"])

    def test_the_lane_retries_an_incomplete_review_only_once(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.incomplete_review(retried=True))
        self.assertEqual([(c[2], "--retry-review" in c) for c in self.calls], [("prepare", False), ("prepare", True)])
        entry = self.entries()[0]
        self.assertEqual((entry["status"], entry["step"]), ("failed", "prepare"))
        self.assertIn("did not complete", entry["reason"])

    #: `sd-ship`'s answer when the hub dropped its session or refused its build (sd:3239).
    HUB_FAULT = {"ok": False, "phase": "prepare", "error": "the sd hub at hub.example.test:8769 is unreachable: "
                 "[Errno 32] Broken pipe", "workflow": {"blocker": {"code": "hub_unavailable", "retryable": True}}}

    #: `sd-ship`'s answer to a prepare whose review planning failed before it reserved a pass (sd:3478).
    NO_PLAN = {"ok": False, "phase": "prepare", "passes_spent": 0,
               "error": "local review emitted no valid timing plan; no provider pass was reserved",
               "workflow": {"blocker": {"code": "timing_plan_invalid", "retryable": True}}}

    def test_a_retryable_refusal_that_spent_no_pass_is_put_back_and_the_next_run_ships_it(self) -> None:
        """sd:3478. Five entries failed on this refusal in 10 to 13 s each; seven retries by hand shipped them."""
        sd_lane.enqueue_entry(self.worktree("topic"), 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = self.NO_PLAN
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([(c[2], c[4]) for c in self.calls], [("prepare", "1")])
        self.assertIn("sd:1", answer["stopped"])
        one, two = self.entries()
        self.assertEqual((one["status"], one["retryable_retries"], one["retryable_fault"]["step"], two["status"]),
                         ("pending", 1, "prepare", "pending"))
        self.assertIn("timing plan", one["retryable_fault"]["reason"])
        self.assertNotIn("hub_retries", one)
        del self.answers[(1, "prepare")]
        self.calls.clear()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([(c[2], c[4]) for c in self.calls],
                         [("prepare", "1"), ("merge", "1"), ("prepare", "2"), ("merge", "2")])
        self.assertEqual([entry["status"] for entry in self.entries()], ["merged", "merged"])

    def test_a_retryable_refusal_fails_the_entry_on_its_second_time(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = self.NO_PLAN
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], entry["step"], entry["retryable_retries"], len(self.calls)),
                         ("failed", "prepare", sd_lane.RETRYABLE_RETRIES, sd_lane.RETRYABLE_RETRIES + 1))
        self.assertIn("timing plan", entry["reason"])

    def test_a_refusal_that_spent_a_pass_or_does_not_say_or_is_not_retryable_fails_at_once(self) -> None:
        """sd:3478. An unknown spend is not "none"; a failed gate is not run again unasked."""
        blocker = self.NO_PLAN["workflow"]["blocker"]
        cases = {"spent": {"passes_spent": 1}, "unsaid": {"passes_spent": None}, "true": {"passes_spent": True},
                 "text": {"passes_spent": "0"},
                 "not retryable": {"workflow": {"blocker": {**blocker, "retryable": False}}},
                 "gate": {"workflow": {"blocker": {**blocker, "code": "gate_failed"}}}}
        for item, (case, change) in enumerate(cases.items(), start=1):
            with self.subTest(case=case):
                answer = {key: value for key, value in {**self.NO_PLAN, **change}.items() if value is not None}
                sd_lane.enqueue_entry(self.feature, item, "fix: one", self.body, self.environ, manual=True, claim="deliver")
                self.answers[(item, "prepare")] = answer
                self.calls.clear()
                sd_lane.run_lane(self.repo, self.environ, self.ship)
                entry = self.entries()[-1]
                self.assertEqual((entry["status"], "retryable_retries" in entry, len(self.calls)), ("failed", False, 1))

    def test_a_hub_fault_puts_the_entry_back_and_the_next_run_ships_it(self) -> None:
        """sd:3239. A hub upgrade broke prepare's session; the entry failed as a policy block and was lost."""
        tree = self.worktree("topic")
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = self.HUB_FAULT
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        # The run stops: the hub is mid-upgrade, and the next entry would meet it too.
        self.assertEqual([(c[2], c[4]) for c in self.calls], [("prepare", "1")])
        self.assertIn("sd:1", answer["stopped"])
        one, two = self.entries()
        self.assertEqual((one["status"], one["hub_retries"], one["hub_fault"]["step"], two["status"]),
                         ("pending", 1, "prepare", "pending"))
        self.assertIn("Broken pipe", one["hub_fault"]["reason"])
        self.assertNotIn("step", one)
        self.assertNotIn("holder", one)
        del self.answers[(1, "prepare")]
        self.calls.clear()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        # No --retry-review: a review that cleared before the fault is the receipt prepare reuses at this head.
        self.assertEqual([(c[2], c[4], "--retry-review" in c) for c in self.calls],
                         [("prepare", "1", False), ("merge", "1", False), ("prepare", "2", False), ("merge", "2", False)])
        self.assertEqual([entry["status"] for entry in self.entries()], ["merged", "merged"])

    def test_a_failed_entry_keeps_the_failing_test_its_refusal_names(self) -> None:
        """sd:3476. The reason was cut at 600 characters, before the gate refusal's failing lines."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        error = ("the repository gate failed after the review cleared: sd-check fail (check fail: suite leg-shared)\n"
                 "check (exit 2): whole output: " + "/tmp/" + "o" * 700 + ".log\nfailing: FAIL: test_sd3476_marker")
        self.answers[(1, "prepare")] = {"ok": False, "phase": "reviewed", "error": error}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("failing: FAIL: test_sd3476_marker", entry["reason"])

    def test_a_database_older_than_the_library_stops_the_run_and_fails_nothing(self) -> None:
        """sd:3475. On 2026-10-10 a pack landing built the library for schema 25 over a database at 24,
        and the runner failed four entries in turn, each in no time, before anyone migrated."""
        from sd_db.errors import SchemaTooOld
        sd_lane.enqueue_entry(self.worktree("topic"), 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = sd_lane.sd_ship_workflow.failure("prepare", SchemaTooOld(24, 25))
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([(c[2], c[4]) for c in self.calls], [("prepare", "1")])
        self.assertIn("sd:1", answer["stopped"])
        one, two = self.entries()
        self.assertEqual((one["status"], one["hub_retries"], two["status"]), ("pending", 1, "pending"))
        self.assertIn("migrate", one["hub_fault"]["reason"])

    def test_a_hub_fault_in_the_merge_is_retried_too(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "merge")] = {**self.HUB_FAULT, "phase": "merge"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], entry["hub_fault"]["step"], entry["hub_retries"]), ("pending", "merge", 1))
        del self.answers[(1, "merge")]
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.entries()[0]["status"], [c[2] for c in self.calls]),
                         ("merged", ["prepare", "merge", "prepare", "merge"]))

    def test_the_retry_starts_from_prepares_catch_up_merge_and_not_from_a_builder_commit(self) -> None:
        """The catch-up moved HEAD past the queued head; a commit of the builder's still skips the entry.

        Failure table, put-back (sd:3283): the catch-up is only in the worktree, so `expected_head` stays at
        the published head, and this host's worktree rule accepts the catch-up. Pushed, the entry moves to it.
        A retryable refusal's put-back takes the same path (sd:3478).
        """
        for builder, pushed, fault in ((False, False, "hub"), (True, False, "hub"), (False, True, "hub"),
                                       (False, False, "retryable"), (False, True, "retryable")):
            with self.subTest(builder=builder, pushed=pushed, fault=fault):
                name = f"topic-{builder}-{pushed}-{fault}"
                tree = self.worktree(name)
                queued = sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, manual=True,
                                               claim="deliver")["expected_head"]
                git(self.repo, "commit", "-q", "--allow-empty", "-m", "base moved")
                git(self.repo, "push", "-q", "origin", "main")  # prepare merges `origin/<default>`

                def catch_up_then_fault(argv: list[str], log: pathlib.Path, tree: pathlib.Path = tree,
                                        name: str = name, pushed: bool = pushed, fault: str = fault) -> dict:
                    git(tree, "merge", "-q", "--no-ff", "-m", "Merge origin/main", "main")
                    if pushed:
                        git(tree, "push", "-q", "origin", f"HEAD:refs/heads/{name}")
                    self.ship(argv, log)
                    return self.HUB_FAULT if fault == "hub" else self.NO_PLAN
                sd_lane.run_lane(self.repo, self.environ, catch_up_then_fault)
                caught_up = git(tree, "rev-parse", "HEAD")
                self.assertEqual((self.entries()[-1]["status"], self.entries()[-1]["expected_head"]),
                                 ("pending", caught_up if pushed else queued))
                if builder:
                    git(tree, "commit", "-q", "--allow-empty", "-m", "the builder's")
                sd_lane.run_lane(self.repo, self.environ, self.ship)
                self.assertEqual(self.entries()[-1]["status"], "skipped" if builder else "merged")

    def test_a_hub_that_keeps_failing_holds_the_entry_after_its_retries(self) -> None:
        """sd:4184. A hub fault is never the entry's: past its retries it waits, held, instead of failing."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = self.HUB_FAULT
        for _ in range(sd_lane.HUB_RETRIES):
            sd_lane.run_lane(self.repo, self.environ, self.ship)
            self.assertEqual((self.entries()[0]["status"], self.entries()[0].get("held")), ("pending", None))
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertIn("held", answer["stopped"])
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], entry["held"], entry["hub_retries"], len(self.calls)),
                         ("pending", True, sd_lane.HUB_RETRIES + 1, sd_lane.HUB_RETRIES + 1))
        self.assertIn("Broken pipe", entry["hub_fault"]["reason"])
        del self.answers[(1, "prepare")]
        sd_lane.set_hold(self.repo, 1, self.environ, held=False)
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[0]["status"], "merged")

    def test_the_whole_prepare_output_is_kept(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        log = pathlib.Path(self.entries()[0]["prepare_log"])
        self.assertEqual(log.read_text(encoding="utf-8"), "whole output " * 1000)
        self.assertEqual(log.parent, self.root / "pack/lane/logs")

    def test_a_second_runner_exits_at_once_instead_of_waiting(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        lock = self.store().lock_file
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("another runner holds", answer["busy"])
        self.assertEqual((self.calls, self.entries()[0]["status"]), ([], "pending"))

    def test_a_lane_that_starts_after_the_scan_runs_nothing_until_the_move_ends(self) -> None:
        """sd:3273 review 1: a lane queued after `other_lanes_idle` scanned, with no runner lock yet, waits."""
        with sd_lane.other_lanes_idle(self.root) as busy:
            self.assertIsNone(busy)
            sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
            lock = sd_lane.queue_path(self.repo, self.environ).parent / "runner.lock"
            self.assertFalse(lock.exists(), "the lane already had a runner lock for the scan to find")
            started = time.monotonic()
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("the tools this lane runs are moving", answer.get("busy", ""))
        self.assertEqual((self.calls, self.entries()[0]["status"]), ([], "pending"))
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(([call[4] for call in self.calls], self.entries()[0]["status"]), (["1"], "prepared"))

    def test_a_run_past_its_budget_starts_no_new_entry_and_leaves_the_rest_pending(self) -> None:
        """sd:3287: the job limit TERMed a runner mid `make check`; an entry started in budget still finishes."""
        for item in (1, 2, 3):
            sd_lane.enqueue_entry(self.worktree(f"b{item}"), item, f"fix: {item}", self.body, self.environ, claim="deliver")
        now = [0.0]

        def slow(argv: list[str], log: pathlib.Path) -> dict:
            now[0] += 30 * 60  # one entry on a busy lane
            return self.ship(argv, log)

        with mock.patch.object(sd_lane, "clock", lambda: now[0]):
            answer = sd_lane.run_lane(self.repo, self.environ, slow)
            self.assertEqual([call[4] for call in self.calls], ["1", "2"])
            self.assertEqual([row["status"] for row in self.entries()], ["prepared", "prepared", "pending"])
            self.assertIn("budget", answer["stopped"])
            spent = sd_lane.run_lane(self.repo, self.environ, slow, deadline=now[0])
        self.assertEqual((spent["ran"], len(self.calls)), ([], 2), "a hosted run's later lane started an entry")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([row["status"] for row in self.entries()], ["prepared"] * 3)

    def test_a_claim_leases_one_prepare_and_renews_it_before_the_retry(self) -> None:
        """sd:3481. A claim leased two prepares, 14 hours, so a dead satellite runner held its lane most of a day."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        leases: list[float] = []

        def incomplete_once(argv: list[str], log: pathlib.Path) -> dict:
            [row] = self.entries()
            leases.append(calendar.timegm(time.strptime(row["lease_until"], "%Y-%m-%dT%H:%M:%SZ")) - time.time())
            sd_lane.update(self.store(), lambda rows: rows[0].update(lease_until="2020-01-01T00:00:00Z"))
            self.ship(argv, log)
            if "--retry-review" in argv:
                return {"ok": True, "phase": "ready_to_send", "head": "head-1"}
            return {"ok": False, "error": "incomplete", "workflow": {"blocker": {"code": "review_incomplete"}}}
        sd_lane.run_lane(self.repo, self.environ, incomplete_once)
        self.assertEqual(len(leases), 2)
        for lease in leases:  # the retry's prepare runs under a renewed lease, not the old one
            self.assertGreater(lease, sd_lane.sd_lib.GATE_SLOT_SECONDS)
            self.assertLess(lease, 8 * 3600)

    def test_a_claim_released_before_the_retry_starts_no_second_prepare(self) -> None:
        """sd:3481, the renew step: a claim lost before the --retry-review prepare stops the run there."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")

        def released_then_incomplete(argv: list[str], log: pathlib.Path) -> dict:
            sd_lane.update(self.store(), lambda rows: rows[0]["holder"].update(token="released"))
            self.ship(argv, log)
            return {"ok": False, "error": "incomplete", "workflow": {"blocker": {"code": "review_incomplete"}}}
        answer = sd_lane.run_lane(self.repo, self.environ, released_then_incomplete)
        self.assertEqual([("--retry-review" in call) for call in self.calls], [False])
        self.assertIn("reclaimed or released", answer["stopped"])

    def test_a_loaded_machine_claims_and_its_gate_waits_in_the_slot_queue(self) -> None:
        """pack sd:7412. The claim-time load stop idled the pack lane 25.6 h in a week while builders took the slots;
        the gate queue's own load rule, in ticket order, is the one bound now."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        environ = {**self.environ, "SD_GATE_LOAD_MAX": "8"}
        with mock.patch.object(sd_lane.os, "getloadavg", lambda: (99.0, 99.0, 99.0)):
            answer = sd_lane.run_lane(self.repo, environ, self.ship)
        self.assertEqual(([c[2] for c in self.calls], [row["status"] for row in self.entries()]),
                         (["prepare"], ["prepared"]), answer.get("stopped"))
        with mock.patch.object(sd_lane.os, "getloadavg", side_effect=OSError("no load")):
            sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, claim="deliver")
            sd_lane.run_lane(self.repo, environ, self.ship)
        self.assertEqual([row["status"] for row in self.entries()], ["prepared", "prepared"])

    def test_prepare_waits_for_a_slot_no_longer_than_the_runs_budget(self) -> None:
        """pack sd:7412, failure table row 2: an unbounded slot wait ran into the job's TERM, which spends a reclaim."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        now = [1000.0]
        with mock.patch.object(sd_lane, "clock", lambda: now[0]):
            sd_lane.run_lane(self.repo, self.environ, self.ship, deadline=1000.0 + 600.5)
        prepare = self.calls[0]
        self.assertEqual(prepare[prepare.index("--slot-timeout") + 1], "600")
        self.calls.clear()
        sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.ship, deadline=float("inf"))
        prepare = self.calls[0]
        self.assertEqual(prepare[prepare.index("--slot-timeout") + 1], str(sd_lane.sd_lib.GATE_SLOT_SECONDS))

    #: `sd-ship`'s answer when the gate's slot wait ran out at the bound the lane gave it (pack sd:7412): the review
    #: cleared and its pass is kept, so the answer says one pass was spent.
    SLOT_WAIT = {"ok": False, "phase": "prepare", "passes_spent": 1,
                 "error": "the repository gate failed after the review cleared; the review pass is kept: "
                          "no gate slot came free within 600s",
                 "workflow": {"blocker": {"code": "gate_slot_wait", "retryable": True}}}

    def test_a_slot_wait_that_ran_out_puts_the_entry_back_and_spends_no_retry_or_reclaim(self) -> None:
        """pack sd:7412, failure table row 2: no gate ran, so the entry is not failed; the kept review serves the
        next prepare, and every run under load puts it back again, in its place."""
        sd_lane.enqueue_entry(self.worktree("topic"), 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = self.SLOT_WAIT
        for waits in (1, 2):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
            self.assertIn("sd:1", answer["stopped"])
            self.assertIn("gate slot", answer["stopped"])
            one, two = self.entries()
            self.assertEqual((one["status"], one["slot_waits"], one["slot_wait"]["step"], two["status"]),
                             ("pending", waits, "prepare", "pending"))
            for spent in ("reclaims", "retryable_retries", "hub_retries"):
                self.assertNotIn(spent, one)
        self.assertEqual([(c[2], c[4]) for c in self.calls], [("prepare", "1"), ("prepare", "1")])
        del self.answers[(1, "prepare")]
        self.calls.clear()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([(c[2], c[4]) for c in self.calls],
                         [("prepare", "1"), ("merge", "1"), ("prepare", "2"), ("merge", "2")])
        self.assertEqual([entry["status"] for entry in self.entries()], ["merged", "merged"])

    def test_a_row_with_slot_waits_claims_and_merges(self) -> None:
        """pack sd:7412, failure table row 5: a row this version put back carries `slot_waits`; a runner of either
        version, a rollback included, claims it as any pending row (this test passes on the older runner too)."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.update(self.store(), lambda rows: rows[0].update(
            slot_waits=3, slot_wait={"step": "prepare", "reason": "no gate slot came free within 600s",
                                     "at": "2026-11-03T00:00:00Z"}))
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[0]["status"], "merged")

    def test_a_frozen_repository_claims_no_entry_until_the_window_ends(self) -> None:
        """sd:6879. A freeze window holds the lane: every entry stays pending, and a lift lets the run take them."""
        import sd_db
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        connection = sd_db.connect(home=self.tmp)
        try:
            sd_db.set_freeze(connection, self.store().repository, "2099-01-01T00:00:00Z", "release",
                         now="2098-12-31T00:00:00Z")
        finally:
            connection.close()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.calls, [row["status"] for row in self.entries()]), ([], ["pending"]))
        self.assertIn("is frozen until 2099-01-01T00:00:00+00:00: release", answer["stopped"])
        connection = sd_db.connect(home=self.tmp)
        try:
            sd_db.set_freeze(connection, self.store().repository, None)
        finally:
            connection.close()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([row["status"] for row in self.entries()], ["prepared"])

    def left_running(self, pid: int | None, step: str = "prepare") -> None:
        """Item 1 claimed by the runner `pid` and never finished, as a killed runner leaves it (sd:2821)."""
        if not self.entries():
            sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")

        def claimed(entries: list[dict]) -> None:
            entries[0].update(status="running", step=step, holder={"host": sd_lane.this_host(), "pid": pid, "token": "t"})
        sd_lane.update(self.store(), claimed)

    def dead_pid(self) -> int:
        runner = subprocess.Popen([sys.executable, "-c", ""])
        runner.wait()
        return runner.pid

    def test_a_running_entry_whose_runner_died_mid_merge_is_failed_and_the_queue_goes_on(self) -> None:
        """Failure table, merge: the runner dies mid-merge; a merge may have landed, so reclaim fails it (sd:3282)."""
        pid = self.dead_pid()
        self.left_running(pid, step="merge")
        sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, claim="deliver")
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(answer["reclaimed"], [{"item": 1, "runner_pid": pid, "step": "merge", "status": "failed"}])
        first, second = self.entries()
        self.assertEqual((first["status"], first["step"], first["reclaimed_by"]), ("failed", "runner", os.getpid()))
        self.assertIn(f"runner pid {pid} died", first["reason"])
        self.assertEqual(([call[4] for call in self.calls], second["status"]), (["2"], "prepared"))
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")  # no longer refused

    def test_a_running_entry_whose_runner_may_live_is_kept(self) -> None:
        for pid in (os.getpid(), None, 1):  # alive, unreadable, and a pid this user may not signal
            with self.subTest(pid=pid):
                self.left_running(pid)
                answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
                self.assertNotIn("reclaimed", answer)
                self.assertEqual((self.calls, self.entries()[0]["status"]), ([], "running"))

    def test_a_running_entry_is_kept_while_another_runner_holds_the_lock(self) -> None:
        self.left_running(self.dead_pid())
        lock = self.store().lock_file
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertIn("another runner holds", answer["busy"])
        self.assertEqual(self.entries()[0]["status"], "running")

    def test_an_entry_queued_while_the_runner_works_is_run_too(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        second = self.worktree("second")
        ship = self.ship

        def enqueue_during(argv: list[str], log: pathlib.Path) -> dict:
            if not any(entry["item"] == 2 for entry in self.entries()):
                sd_lane.enqueue_entry(second, 2, "fix: two", self.body, self.environ, claim="deliver")
            return ship(argv, log)
        sd_lane.run_lane(self.repo, self.environ, enqueue_during)
        self.assertEqual([entry["status"] for entry in self.entries()], ["prepared", "prepared"])


class Reorder(Lane):
    """sd:2584: the runner reads the queue's top before each item, and the verbs reorder it."""

    def queue(self, *items: int, manual: bool = False) -> None:
        for item in items:
            sd_lane.enqueue_entry(self.worktree(f"t{item}"), item, f"fix: t{item}", self.body, self.environ,
                                  manual=manual, claim="deliver")

    def items_run(self) -> list[int]:
        return [int(c[c.index("--item") + 1]) for c in self.calls if c[2] == "prepare"]

    def pending(self) -> list[int]:
        return [row["item"] for row in self.entries() if row["status"] == "pending"]

    def test_a_move_while_the_first_merges_runs_the_moved_item_second_and_a_hold_skips(self) -> None:
        self.queue(1, 2, 3, 4, manual=True)
        sd_lane.set_hold(self.repo, 2, self.environ, held=True)
        ship = self.ship

        def move_during_the_first_merge(argv: list[str], log: pathlib.Path) -> dict:
            if argv[2] == "merge" and argv[argv.index("--item") + 1] == "1":
                sd_lane.move(self.repo, 4, "top", self.environ)
            return ship(argv, log)
        sd_lane.run_lane(self.repo, self.environ, move_during_the_first_merge)
        self.assertEqual((self.items_run(), self.pending()), ([1, 4, 3], [2]))
        sd_lane.set_hold(self.repo, 2, self.environ, held=False)
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.items_run(), [1, 4, 3, 2])

    def test_up_down_and_a_position_move_among_pending_entries_only(self) -> None:
        self.queue(1, 2, 3, 4)
        sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual(sd_lane.move(self.repo, 4, "up", self.environ)["pending"], [2, 4, 3])
        self.assertEqual(sd_lane.move(self.repo, 2, "down", self.environ)["pending"], [4, 2, 3])
        self.assertEqual(sd_lane.move(self.repo, 3, "1", self.environ)["pending"], [3, 4, 2])
        self.assertEqual(sd_lane.move(self.repo, 3, "9", self.environ)["pending"], [4, 2, 3])
        self.assertEqual(sd_lane.move(self.repo, 4, "up", self.environ)["pending"], [4, 2, 3])
        # The cancelled entry keeps its place in the history at the front.
        self.assertEqual([row["item"] for row in self.entries()], [1, 4, 2, 3])

    def test_a_running_entry_is_not_edited_and_an_absent_or_unheld_one_is_refused(self) -> None:
        self.queue(1)
        sd_lane.update(self.store(), lambda rows: rows[0].update(status="running"))
        for verb in (lambda: sd_lane.move(self.repo, 1, "top", self.environ),
                     lambda: sd_lane.set_hold(self.repo, 1, self.environ, held=True)):
            with self.assertRaisesRegex(sd_lane.LaneError, "between items"):
                verb()
        with self.assertRaisesRegex(sd_lane.LaneError, "no pending entry"):
            sd_lane.move(self.repo, 5, "top", self.environ)
        self.queue(2)
        with self.assertRaisesRegex(sd_lane.LaneError, "not held"):
            sd_lane.set_hold(self.repo, 2, self.environ, held=False)

    def test_a_position_must_be_up_down_top_or_a_positive_number(self) -> None:
        for where in ("0", "-1", "sideways"):
            with self.assertRaises(ValueError):
                sd_lane.position(where)

    def test_the_speculative_gate_skips_a_held_entry(self) -> None:
        self.queue(1, 2, 3, manual=True)
        sd_lane.set_hold(self.repo, 2, self.environ, held=True)
        sd_lane.update(self.store(), lambda rows: rows[0].update(status="running"))
        followers: list[int] = []
        with mock.patch.object(sd_lane, "predict", lambda entry, following: followers.append(following["item"]) or
                               {"skipped": "recorded"}):
            sd_lane.speculate(self.entries()[0], self.store(), lambda *a: {})
        self.assertEqual(followers, [3])

    def test_the_verbs_reorder_through_sd_ship(self) -> None:
        self.queue(1, 2)
        env = {**os.environ, **self.environ}

        def lane(*args: str) -> dict:
            done = subprocess.run([sys.executable, str(REPO_ROOT / "bin/sd-ship"), "-C", str(self.repo), "lane", *args],
                                  env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            return json.loads(done.stdout)
        self.assertEqual(lane("move", "2", "top")["pending"], [2, 1])
        self.assertTrue(lane("hold", "1")["held"])
        self.assertEqual([(row["item"], bool(row.get("held"))) for row in lane("list")["entries"]], [(2, False), (1, True)])
        self.assertFalse(lane("release", "1")["held"])

    def test_an_expected_revision_refuses_a_queue_that_changed_and_writes_nothing(self) -> None:
        """sd:2717. A caller that read the queue names its revision; a move between its read and its edit is caught."""
        self.queue(1, 2, 3)
        read = sd_lane.queue_revision(self.entries())
        self.assertEqual(sd_lane.move(self.repo, 3, "top", self.environ, expected_revision=read)["pending"], [3, 1, 2])
        before = self.entries()
        for verb in (lambda: sd_lane.move(self.repo, 2, "top", self.environ, expected_revision=read),
                     lambda: sd_lane.set_hold(self.repo, 2, self.environ, held=True, expected_revision=read)):
            with self.assertRaises(sd_lane.LaneError) as caught:
                verb()
            self.assertEqual(caught.exception.code, sd_lane.STALE_REVISION)
        self.assertEqual(self.entries(), before)
        held = sd_lane.set_hold(self.repo, 2, self.environ, held=True, expected_revision=sd_lane.queue_revision(before))
        self.assertTrue(held["held"])
        with self.assertRaises(sd_lane.LaneError) as caught:
            sd_lane.set_hold(self.repo, 2, self.environ, held=False, expected_revision=sd_lane.queue_revision(before))
        self.assertEqual((caught.exception.code, self.pending()), (sd_lane.STALE_REVISION, [3, 1, 2]))

    def test_cancel_takes_an_expected_revision_as_the_other_editors_do(self) -> None:
        """sd:3137. A cancel against a queue that changed since its read is refused with the same code."""
        self.queue(1, 2, 3)
        read = sd_lane.queue_revision(self.entries())
        sd_lane.move(self.repo, 3, "top", self.environ)
        before = self.entries()
        with self.assertRaises(sd_lane.LaneError) as caught:
            sd_lane.cancel(self.repo, 2, self.environ, expected_revision=read)
        self.assertEqual((caught.exception.code, self.entries()), (sd_lane.STALE_REVISION, before))
        cancelled = sd_lane.cancel(self.repo, 2, self.environ, expected_revision=sd_lane.queue_revision(before))
        self.assertEqual((cancelled["status"], self.pending()), ("cancelled", [3, 1]))

    def test_the_revision_is_compared_under_the_queue_lock(self) -> None:
        """A write that lands while the verb waits for the hub's write lock is caught; a check before it would miss it."""
        self.queue(1, 2)
        read = sd_lane.queue_revision(self.entries())
        refused: list[str | None] = []

        def stale_move() -> None:
            try:
                sd_lane.move(self.repo, 2, "top", self.environ, expected_revision=read)
            except sd_lane.LaneError as error:
                refused.append(error.code)
        import sd_db
        writer = sd_db.connect(home=self.tmp)
        self.addCleanup(writer.close)
        with sd_db.database.transaction(writer):  # BEGIN IMMEDIATE: the mover waits for it
            mover = threading.Thread(target=stale_move)
            mover.start()
            time.sleep(0.5)
            entries = sd_lane.stored(writer, "example/pack")
            entries[0]["position"], entries[1]["position"] = entries[1]["position"], entries[0]["position"]
            for row in entries:
                sd_lane.store(writer, "example/pack", row)
        mover.join(30)
        self.assertEqual((refused, self.pending()), ([sd_lane.STALE_REVISION], [2, 1]))

    def test_list_prints_the_revision_and_a_stale_one_exits_3_with_its_code(self) -> None:
        self.queue(1, 2)
        env = {**os.environ, **self.environ}

        def lane(*args: str) -> tuple[int, dict]:
            done = subprocess.run([sys.executable, str(REPO_ROOT / "bin/sd-ship"), "-C", str(self.repo), "lane", *args],
                                  env=env, capture_output=True, text=True, timeout=120)
            return done.returncode, json.loads(done.stdout)
        _, listed = lane("list")
        self.assertEqual(listed["revision"], sd_lane.queue_revision(listed["entries"]))
        self.assertEqual(lane("hold", "2", "--expected-revision", listed["revision"])[0], 0)
        code, refused = lane("release", "2", "--expected-revision", listed["revision"])
        self.assertEqual((code, refused["code"]), (3, sd_lane.STALE_REVISION))
        self.assertEqual(lane("move", "2", "top")[1]["pending"], [2, 1])  # unset keeps today's behaviour
        code, refused = lane("cancel", "1", "--expected-revision", listed["revision"])
        self.assertEqual((code, refused["code"]), (3, sd_lane.STALE_REVISION))
        self.assertEqual(lane("cancel", "1")[1]["status"], "cancelled")  # unset keeps today's behaviour


class Trees(Lane):
    """A bare `origin` holds main; two worktrees each add a file, and main moves after they fork."""

    def setUp(self) -> None:
        super().setUp()
        # The tree key is the repo row's `gate_reuse` declaration (sd:5109), read in this process too.
        seed_declaration(self.tmp, self.repo, "gate_reuse",
                         {"schema_version": 1, "key": "tree", "reason": "the check reads no history"})
        home = mock.patch.dict(os.environ, {"HOME": str(self.tmp), "XDG_CONFIG_HOME": str(self.tmp / ".config")})
        home.start()
        self.addCleanup(home.stop)
        self.commit_files(self.repo, {"declared.txt": "d\n"}, "declare")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "remote", "set-head", "origin", "main")
        self.first = self.branch("first", {"one.txt": "1\n"})
        self.second = self.branch("second", {"two.txt": "2\n"})
        self.commit_files(self.repo, {"base.txt": "b\n"}, "main moved")
        git(self.repo, "push", "-q", "origin", "main")
        self.gates: list[tuple[pathlib.Path, str, str]] = []
        self.events = {name: threading.Event() for name in ("landed", "gated")}
        self.seen: dict[str, bool] = {}

    def commit_files(self, tree: pathlib.Path, files: dict[str, str], message: str) -> None:
        for name, text in files.items():
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_text(text, encoding="utf-8")
        git(tree, "add", "-A")
        git(tree, "commit", "-q", "-m", message)

    def branch(self, name: str, files: dict[str, str]) -> pathlib.Path:
        path = self.tmp / name
        git(self.repo, "worktree", "add", "-q", "-b", name, str(path), "origin/main")
        self.commit_files(path, files, name)
        return path

    def caught_up(self, head: str, ref: str) -> str:
        """The tree `sd-ship prepare --catch-up` makes at `head` against `ref`, built apart from the lane's code."""
        scratch = self.tmp / f"scratch-{len(list(self.tmp.iterdir()))}"
        git(self.repo, "worktree", "add", "-q", "--detach", str(scratch), head)
        try:
            git(scratch, "merge", "--no-ff", "--no-edit", "-m", "catch up", ref)
            return git(scratch, "rev-parse", "HEAD^{tree}")
        finally:
            git(self.repo, "worktree", "remove", "--force", str(scratch))

    def assert_termed_run(self, gated: list[str]) -> None:
        """TERM a runner mid-prepare while a gate runs for each worktree in `gated`; each gate's group ends first."""
        marks = self.tmp / "marks"
        marks.mkdir()
        script = self.tmp / "runner.py"
        script.write_text(TERMED_RUNNER.format(bin=str(REPO_ROOT / "bin")), encoding="utf-8")
        runner = subprocess.Popen([sys.executable, str(script), str(self.repo), json.dumps(self.environ), str(marks),
                                   str(len(gated))],
                                  env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(runner.kill)

        def pid_of(name: str) -> int | None:
            text = (marks / name).read_text() if (marks / name).exists() else ""
            return int(text) if text else None
        pids: dict[str, int] = {}
        for name in [f"gate-{tree}.pid" for tree in gated] + ["prepare.pid"]:
            for _ in range(1200):  # bounded: a runner that never gets there fails on the read below
                if pid_of(name) is not None or runner.poll() is not None:
                    break
                time.sleep(0.05)
            child = pid_of(name)
            if child is None:
                self.fail(f"no {name}: {runner.stderr.read() if runner.poll() is not None else ''}")
            pids[name] = child
            self.addCleanup(end_group, child)
        runner.send_signal(signal.SIGTERM)
        _, errors = runner.communicate(timeout=60)
        self.assertEqual(runner.returncode, -signal.SIGTERM, errors)
        sent = [line.split() for line in (marks / "sent").read_text().splitlines()] if (marks / "sent").exists() else []
        gates = {str(pids[f"gate-{tree}.pid"]) for tree in gated}
        self.assertEqual({group for group, number, _ in sent[:len(gated)] if number == str(int(signal.SIGTERM))},
                         gates, "the runner did not signal every speculative gate's group first")
        ended = float((marks / "prepare-ended").read_text())
        self.assertTrue(all(float(at) < ended for _, _, at in sent[:len(gated)]), "TERM waited for prepare")
        for tree in gated:
            for _ in range(100):
                try:
                    os.kill(pids[f"gate-{tree}.pid"], 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"the speculative gate's child for {tree} outlived the runner")


class Speculation(Trees):
    """sd:2586: while one entry ships, the next one's gate runs on the predicted landing.

    The recorder lands entry 1 as GitHub's squash would: its catch-up tree as a new commit on main.
    """

    def ship(self, argv: list[str], log: pathlib.Path) -> dict:
        verb, item = argv[2], int(argv[argv.index("--item") + 1])
        if (item, verb) == (1, "merge"):
            git(self.repo, "fetch", "-q", "origin")
            tree = self.caught_up(git(self.first, "rev-parse", "HEAD"), "origin/main")
            landed = git(self.repo, "commit-tree", tree, "-p", "origin/main", "-m", "one (#1)")
            git(self.repo, "push", "-q", "origin", f"{landed}:refs/heads/main")
            self.events["landed"].set()
        if (item, verb) == (2, "prepare"):
            self.seen["gate done before prepare 2"] = self.events["gated"].is_set()
        return super().ship(argv, log)

    def gate(self, root: pathlib.Path, head: str, base: str) -> dict:
        """Still running when entry 1 lands, and ends half a second after."""
        self.gates.append((root, head, base))
        self.seen["gate ran while entry 1 shipped"] = self.events["landed"].wait(10)
        time.sleep(0.5)
        self.events["gated"].set()
        return {"status": "success", "summary": "sd-check pass (check pass)", "receipt_revision": 1}

    def queue_both(self, *, manual: bool = True) -> None:
        sd_lane.enqueue_entry(self.first, 1, "fix: one", self.body, self.environ, manual=manual, claim="deliver")
        sd_lane.enqueue_entry(self.second, 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")

    def test_the_next_entry_is_gated_on_the_predicted_landing_while_this_one_ships(self) -> None:
        self.queue_both()
        fork = git(self.repo, "rev-parse", "origin/main")
        sd_lane.run_lane(self.repo, self.environ, self.ship, self.gate)
        [(root, head, base)] = self.gates
        self.assertEqual(root, self.second)
        git(self.repo, "fetch", "-q", "origin")
        landed = git(self.repo, "rev-parse", "origin/main")
        # The predicted landing is another commit than the real one, on the same base, with the same tree.
        self.assertNotEqual(base, landed)
        self.assertEqual(git(self.repo, "rev-parse", f"{base}^"), fork)
        self.assertEqual(git(self.repo, "rev-parse", f"{base}^{{tree}}"), git(self.repo, "rev-parse", "origin/main^{tree}"))
        # The gated tree is the one entry 2's catch-up makes after the real landing.
        self.assertEqual(git(self.repo, "rev-parse", f"{head}^{{tree}}"),
                         self.caught_up(git(self.second, "rev-parse", "HEAD"), "origin/main"))
        self.assertTrue(self.seen["gate ran while entry 1 shipped"])

    def test_the_next_prepare_starts_after_the_speculative_gate_ends(self) -> None:
        self.queue_both()
        sd_lane.run_lane(self.repo, self.environ, self.ship, self.gate)
        self.assertTrue(self.seen["gate done before prepare 2"])

    def test_the_next_entry_records_the_speculation(self) -> None:
        self.queue_both()
        sd_lane.run_lane(self.repo, self.environ, self.ship, self.gate)
        [(_, head, base)] = self.gates
        speculation = self.entries()[1]["speculation"]
        self.assertEqual({key: speculation[key] for key in ("after", "head", "base", "status", "receipt")},
                         {"after": 1, "head": head, "base": base, "status": "success", "receipt": "recorded"})
        self.assertIn('"status": "success"', pathlib.Path(speculation["log"]).read_text(encoding="utf-8"))

    def test_an_entry_queued_without_manual_is_not_predicted(self) -> None:
        """It stops prepared and never lands, so the next entry's gate would name a tree no prepare makes."""
        self.queue_both(manual=False)
        sd_lane.run_lane(self.repo, self.environ, super().ship, self.gate)
        self.assertEqual(self.gates, [])
        self.assertNotIn("speculation", self.entries()[1])

    def test_without_the_tree_key_nothing_is_gated(self) -> None:
        seed_declaration(self.tmp, self.repo, "gate_reuse", None)
        self.queue_both()
        sd_lane.run_lane(self.repo, self.environ, super().ship, self.gate)
        self.assertEqual(self.gates, [])
        self.assertEqual(self.entries()[1]["speculation"]["status"], "skipped")
        self.assertIn("no tree key", self.entries()[1]["speculation"]["reason"])

    def test_a_database_that_does_not_answer_gates_nothing(self) -> None:
        """sd:5109: an unread gate_reuse row is unknown, so the prediction names no key and gates nothing."""
        self.queue_both()
        unread = sd_lane.sd_lib.DatabaseUnreadable("down")
        with mock.patch.object(sd_lane.sd_lib, "repo_declaration", side_effect=unread):
            sd_lane.run_lane(self.repo, self.environ, super().ship, self.gate)
        self.assertEqual(self.gates, [])
        self.assertEqual(self.entries()[1]["speculation"]["status"], "skipped")
        self.assertIn("the gate_reuse declaration cannot be read: down", self.entries()[1]["speculation"]["reason"])

    def test_a_conflict_with_the_predicted_landing_gates_nothing_and_both_entries_still_run(self) -> None:
        self.commit_files(self.second, {"one.txt": "not 1\n"}, "conflicts with first")
        worktrees = git(self.repo, "worktree", "list", "--porcelain").count("worktree ")
        self.queue_both()
        sd_lane.run_lane(self.repo, self.environ, super().ship, self.gate)
        self.assertEqual(self.gates, [])
        self.assertEqual([entry["status"] for entry in self.entries()], ["merged", "merged"])
        self.assertIn("conflicts with sd:1", self.entries()[1]["speculation"]["reason"])
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), worktrees)

    def test_a_gate_that_raises_does_not_stop_the_lane(self) -> None:
        def broken(root: pathlib.Path, head: str, base: str) -> dict:
            raise RuntimeError("the gate broke")
        self.queue_both()
        sd_lane.run_lane(self.repo, self.environ, super().ship, broken)
        self.assertEqual([entry["status"] for entry in self.entries()], ["merged", "merged"])
        self.assertEqual(self.entries()[1]["speculation"]["status"], "error")

    def test_a_satellite_entry_an_older_version_queued_is_skipped_and_never_shipped(self) -> None:
        """sd:3003. A `gate: satellite` entry the retired hand-off left names the main checkout, not its head."""
        satellite = self.tmp / "satellite"
        git(self.tmp, "clone", "-q", str(self.origin), str(satellite))
        self.commit_files(satellite, {"sat.txt": "s\n"}, "satellite")
        git(satellite, "push", "-q", "origin", "HEAD:refs/heads/sat")
        sd_lane.update(self.store(), lambda rows: rows.append({
            "id": "older", "position": 0, "worktree": str(self.repo), "item": 1, "gate": "satellite", "branch": "sat", "base": "main",
            "expected_head": git(satellite, "rev-parse", "HEAD"), "authority": "manual", "status": "pending",
            "enqueued_at": sd_lane.stamp_now(), "enqueued_on": sd_lane.this_host()}))
        sd_lane.enqueue_entry(self.second, 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, super().ship, lambda root, head, base: {"status": "success"})
        self.assertEqual([(entry["item"], entry["status"]) for entry in self.entries()], [(1, "skipped"), (2, "merged")])
        self.assertEqual([call[call.index("--item") + 1] for call in self.calls], ["2", "2"])

    def test_a_stopped_run_cancels_its_speculative_gate_and_frees_its_lock(self) -> None:
        """sd:3361. A run that stopped on a build refusal waited in an unbounded join for the
        speculative gate's `sd-check` child, and held its runner lock the whole time. The stop
        now ends the gate's process group and waits a bounded time for it."""
        import sd_db.database
        from sd_db import remote
        self.queue_both()
        marker = self.tmp / "gate-child.pid"
        refusing: list[bool] = []
        connect = sd_db.database.connect

        def hub(*args: Any, **kwargs: Any) -> Any:
            if refusing:
                raise remote.BuildMismatch("build", "0000aaaa", "1111bbbb")
            return connect(*args, **kwargs)

        def endless(root: pathlib.Path, head: str, base: str) -> dict:
            """A gate whose child would outlive the run by far, run as the real gate runs its `sd-check`."""
            child = f"import os, pathlib, time; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
            code, _, _ = sd_lane.gate_run()([sys.executable, "-c", child], dict(os.environ), root, 120)
            return {"status": "failure", "summary": f"exit {code}"}

        def prepare_then_upgrade(argv: list[str], log: pathlib.Path) -> dict:
            for _ in range(200):  # the gate's child is running before the run stops
                if marker.exists() and marker.read_text():
                    break
                time.sleep(0.05)
            self.ship(argv, log)
            refusing.append(True)
            return Runner.HUB_FAULT

        started = time.monotonic()
        with mock.patch.object(sd_lane, "WRITE_RETRY_SECONDS", 600), mock.patch.object(sd_lane, "WRITE_PAUSE", 30), \
                mock.patch("sd_db.database.connect", hub):
            answer = sd_lane.run_lane(self.repo, self.environ, prepare_then_upgrade, endless)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn("BuildMismatch", answer["stopped"])
        self.assertEqual(answer["speculative_gate"], "cancelled")
        with open(self.store().lock_file, "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free for `follow`
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)


    def test_a_speculative_gate_still_waiting_at_the_runs_deadline_is_cancelled(self) -> None:
        """pack sd:7412, failure table row 4: with the claim-time load stop gone, a speculative gate may wait in the
        slot queue past the run's budget; the runner waited for it after the merge with no regard to the deadline."""
        self.queue_both()
        marker = self.tmp / "gate-child.pid"

        def waiting(root: pathlib.Path, head: str, base: str) -> dict:
            """A gate whose child still waits long after the run's budget, run as the real gate runs `sd-check`."""
            child = f"import os, pathlib, time; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
            code, _, _ = sd_lane.gate_run()([sys.executable, "-c", child], dict(os.environ), root, 120)
            return {"status": "failure", "summary": f"exit {code}"}

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            for _ in range(200):  # the gate's child is running before entry 1 ships
                if marker.exists() and marker.read_text():
                    break
                time.sleep(0.05)
            return self.ship(argv, log)

        started = time.monotonic()
        answer = sd_lane.run_lane(self.repo, self.environ, ship, waiting, deadline=sd_lane.clock() + 3)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(answer["speculative_gate"], "cancelled")
        self.assertIn("budget", answer["stopped"])
        self.assertEqual([entry["status"] for entry in self.entries()], ["merged", "pending"])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def test_a_sigterm_mid_prepare_ends_the_speculative_gate_first(self) -> None:
        """sd:5776. A TERMed runner ended prepare's group, then TERMed itself again with the default action,
        so `cancel_gate` never ran: the gate's `sd-check`, in a group of its own, ran on with ppid 1 and
        held its slot. A real SIGTERM now reaches the gate's group before prepare's group ends."""
        self.queue_both()
        self.assert_termed_run(["second"])
        [speculation] = [entry["speculation"] for entry in self.entries() if "speculation" in entry]
        self.assertEqual((speculation["after"], speculation["status"]), (1, "cancelled"))

    def test_an_interruption_while_the_gate_starts_still_ends_it(self) -> None:
        """sd:5776, the step between the gate's start and the runner holding it: the run's cleanup ends it."""
        self.queue_both()
        marker, started = self.tmp / "gate-child.pid", []
        start = sd_lane.SpeculativeGate.start

        def endless(root: pathlib.Path, head: str, base: str) -> dict:
            child = f"import os, pathlib, time; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
            code, _, _ = sd_lane.gate_run()([sys.executable, "-c", child], dict(os.environ), root, 120)
            return {"status": "failure", "summary": f"exit {code}"}

        def start_then_interrupt(gate: sd_lane.SpeculativeGate) -> None:
            start(gate)
            started.append(gate)
            for _ in range(1200):  # the gate's child runs before the interruption
                if marker.exists() and marker.read_text():
                    break
                time.sleep(0.05)
            raise KeyboardInterrupt
        with mock.patch.object(sd_lane.SpeculativeGate, "start", start_then_interrupt), \
                self.assertRaises(KeyboardInterrupt):
            sd_lane.run_lane(self.repo, self.environ, self.ship, endless)
        self.addCleanup(end_group, int(marker.read_text()))
        [gate] = started
        self.assertTrue(gate.cancelled)
        self.assertFalse(gate.is_alive())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)


class DepthTwo(Trees):
    """pack sd:7412, slice 2: the entry after next is gated on both predicted landings, chained.

    The recorder lands every merged entry as GitHub's squash would, so each
    gated tree can be held against the real catch-up after the run.
    """

    def setUp(self) -> None:
        super().setUp()
        self.third = self.branch("third", {"three.txt": "3\n"})
        self.fourth = self.branch("fourth", {"four.txt": "4\n"})
        self.trees = {1: self.first, 2: self.second, 3: self.third, 4: self.fourth}
        self.landed: dict[int, str] = {}

    def queue_all(self, count: int) -> None:
        for item in range(1, count + 1):
            sd_lane.enqueue_entry(self.trees[item], item, f"fix: {item}", self.body, self.environ, manual=True,
                                  claim="deliver")

    def ship(self, argv: list[str], log: pathlib.Path) -> dict:
        verb, item = argv[2], int(argv[argv.index("--item") + 1])
        if verb == "merge":
            git(self.repo, "fetch", "-q", "origin")
            tree = self.caught_up(git(self.trees[item], "rev-parse", "HEAD"), "origin/main")
            self.landed[item] = git(self.repo, "commit-tree", tree, "-p", "origin/main", "-m", f"{item} (#{item})")
            git(self.repo, "push", "-q", "origin", f"{self.landed[item]}:refs/heads/main")
        return Lane.ship(self, argv, log)

    def passing(self, root: pathlib.Path, head: str, base: str) -> dict:
        self.gates.append((root, head, base))
        return {"status": "success", "summary": "sd-check pass (check pass)", "receipt_revision": 1}

    def endless(self, root: pathlib.Path, head: str, base: str) -> dict:
        """A gate whose child sleeps far past the run, as the real gate runs `sd-check`; it marks its pid."""
        self.gates.append((root, head, base))
        mark = self.tmp / f"gate-{root.name}.pid"
        child = f"import os, pathlib, time; pathlib.Path({str(mark)!r}).write_text(str(os.getpid())); time.sleep(60)"
        code, _, _ = sd_lane.gate_run()([sys.executable, "-c", child], dict(os.environ), root, 120)
        return {"status": "failure", "summary": f"exit {code}"}

    def wait_for_gates(self, *names: str) -> None:
        for _ in range(200):  # bounded: a gate that never starts fails on the pid read
            if all((self.tmp / f"gate-{name}.pid").exists() and (self.tmp / f"gate-{name}.pid").read_text()
                   for name in names):
                return
            time.sleep(0.05)

    def assert_ended(self, *names: str) -> None:
        for name in names:
            pid = int((self.tmp / f"gate-{name}.pid").read_text())
            self.addCleanup(end_group, pid)
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"the gate for {name} outlived the run")

    def tree(self, rev: str) -> str:
        return git(self.repo, "rev-parse", f"{rev}^{{tree}}")

    def test_each_entry_is_gated_once_two_ahead_on_the_chained_landings(self) -> None:
        self.queue_all(4)
        fork = git(self.repo, "rev-parse", "origin/main")
        sd_lane.run_lane(self.repo, self.environ, self.ship, self.passing)
        self.assertEqual([entry["status"] for entry in self.entries()], ["merged"] * 4)
        self.assertEqual(sorted(root.name for root, _, _ in self.gates), ["fourth", "second", "third"])
        gated = {root.name: (head, base) for root, head, base in self.gates}
        head, base = gated["third"]
        # Gated while entry 1 shipped: entry 2's landing is predicted on entry 1's predicted landing on the fork.
        self.assertNotEqual(git(self.repo, "rev-parse", f"{base}^"), self.landed[1])
        self.assertEqual(git(self.repo, "rev-parse", f"{base}^^"), fork)
        self.assertEqual(self.tree(f"{base}^"), self.tree(self.landed[1]))
        self.assertEqual(self.tree(base), self.tree(self.landed[2]))
        self.assertEqual(self.tree(head), self.caught_up(git(self.third, "rev-parse", "HEAD"), self.landed[2]))
        head, base = gated["fourth"]
        self.assertEqual(git(self.repo, "rev-parse", f"{base}^^"), self.landed[1])
        self.assertEqual(self.tree(base), self.tree(self.landed[3]))
        self.assertEqual(self.tree(head), self.caught_up(git(self.fourth, "rev-parse", "HEAD"), self.landed[3]))
        self.assertEqual([(entry["speculation"]["after"], entry["speculation"]["depth"], entry["speculation"]["status"])
                          for entry in self.entries()[1:]], [(1, 1, "success"), (2, 2, "success"), (3, 2, "success")])

    def test_a_failure_ahead_cancels_both_gates_and_the_next_entry_gates_again(self) -> None:
        self.queue_all(3)
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}

        def gate(root: pathlib.Path, head: str, base: str) -> dict:
            return self.endless(root, head, base) if len(self.gates) < 2 else self.passing(root, head, base)

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            self.wait_for_gates("second", "third")
            return self.ship(argv, log)
        sd_lane.run_lane(self.repo, self.environ, ship, gate, deadline=sd_lane.clock() + 20)
        self.assert_ended("second", "third")
        self.assertEqual([root.name for root, _, _ in self.gates], ["second", "third", "third"])
        self.assertEqual([entry["status"] for entry in self.entries()], ["failed", "merged", "merged"])
        cancelled = self.entries()[1]["speculation"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIn("sd:1 did not merge", cancelled["reason"])
        again = self.entries()[2]["speculation"]
        self.assertEqual((again["after"], again["depth"], again["status"]), (2, 1, "success"))

    def test_the_runs_deadline_cancels_both_gates(self) -> None:
        self.queue_all(3)

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            self.wait_for_gates("second", "third")
            return self.ship(argv, log)
        started = time.monotonic()
        answer = sd_lane.run_lane(self.repo, self.environ, ship, self.endless, deadline=sd_lane.clock() + 3)
        self.assertLess(time.monotonic() - started, 30)
        self.assert_ended("second", "third")
        self.assertEqual(answer["speculative_gate"], "cancelled")
        self.assertIn("budget", answer["stopped"])
        self.assertEqual([entry["status"] for entry in self.entries()], ["merged", "pending", "pending"])

    def test_a_sigterm_mid_prepare_ends_both_gates_first(self) -> None:
        self.queue_all(3)
        self.assert_termed_run(["second", "third"])
        self.assertEqual([(entry["speculation"]["after"], entry["speculation"]["status"])
                          for entry in self.entries()[1:]], [(1, "cancelled"), (2, "cancelled")])


class Landing(Lane):
    """sd:2568: after a merge, clean up, note the item and fast-forward the main checkout."""

    def setUp(self) -> None:
        super().setUp()
        git(self.repo, "push", "-q", "-u", "origin", "main")
        if str(REPO_ROOT / "dashboard") not in sys.path:  # as sd_lane.local_data adds it
            sys.path.insert(0, str(REPO_ROOT / "dashboard"))
        from sd_dashboard import health_collectors
        # No process works in a test tree; the real lsof gave no answer under load (sd:7025).
        self.cwds = mock.patch.object(health_collectors, "_cwds", return_value=[])
        self.cwds.start()
        self.addCleanup(self.cwds.stop)

    def topic(self, name: str = "topic") -> tuple[pathlib.Path, str]:
        """A pushed worktree whose commit tracks a file, a nested file and a symlink, as a real one does."""
        tree = self.worktree(name)
        (tree / "pkg/mod").mkdir(parents=True)
        (tree / "README.md").write_text("readme\n", encoding="utf-8")
        (tree / "pkg/mod/a.py").write_text("A = 1\n", encoding="utf-8")
        (tree / "link").symlink_to("README.md")
        git(tree, "add", "-A")
        git(tree, "commit", "-q", "-m", name)
        git(tree, "push", "-q", "origin", name)
        return tree, git(tree, "rev-parse", "HEAD")

    def merge_lands(self, tree: pathlib.Path, item: int, head: str) -> None:
        sd_lane.enqueue_entry(tree, item, "fix: t", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(item, "prepare")] = {"ok": True, "phase": "ready_to_send", "head": head}
        self.answers[(item, "merge")] = {"ok": True, "phase": "merged", "merge_commit": "c0ffee" * 6 + "c0ff"}

    def drain(self, ship=None) -> dict:
        return sd_lane.run_lane(self.repo, self.environ, ship or self.ship)

    def remote_branches(self) -> list[str]:
        return git(self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()

    def advance_origin_main(self) -> str:
        upstream = self.tmp / "upstream"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(upstream)], check=True)
        git(upstream, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "landed")
        git(upstream, "push", "-q", "origin", "main")
        return git(upstream, "rev-parse", "HEAD")

    def test_a_merged_worktree_stays_and_the_note_names_its_removal_command(self) -> None:
        """The review's race (sd:2568): removal can race a live builder, so the runner never removes the worktree."""
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)
        self.drain()
        self.assertEqual((tree / "pkg/mod/a.py").read_text(encoding="utf-8"), "A = 1\n")
        self.assertEqual(os.readlink(tree / "link"), "README.md")
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/topic"), head)
        self.assertEqual(self.remote_branches(), ["main"])
        remove = f"git -C {self.repo} worktree remove {tree} && git -C {self.repo} update-ref -d refs/heads/topic {head}"
        [(item, body, main)] = self.notes
        self.assertEqual((item, main), (1, self.repo))
        self.assertEqual(body, f"Landed: merged at c0ffeec0ffee (head {head[:12]}). Cleanup: removed origin/topic, "
                               f"worktree {tree} and branch topic kept on {sd_lane.this_host()} for removal once the builder stops. "
                               f"Recover: git branch topic {head}. Remove: {remove}")
        entry = self.entries()[0]
        self.assertEqual((entry["status"], entry["remove"]), ("merged", remove))
        subprocess.run(remove, shell=True, check=True, capture_output=True)  # the printed command works as printed
        self.assertFalse(tree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "topic"), "")

    def watched(self, *items: int) -> list[str]:
        lines: list[str] = []
        self.assertEqual(sd_lane.watch_items(self.store(), list(items), write=lines.append), 0)
        return lines

    def test_the_watch_after_a_merge_removes_a_clean_stopped_worktree_and_its_branches(self) -> None:
        """sd:6727: the operator ran the landing note's removal command by hand three times in one day."""
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)
        self.drain()
        git(self.origin, "update-ref", "refs/heads/topic", head)  # a remote branch the landing left
        removed = f"Removed: worktree {tree}, branch topic, origin/topic (merged head {head[:12]})"
        lines = self.watched(1)
        self.assertEqual(lines, ["pack sd:1 merged at c0ffeec0ffee", f"pack sd:1 {removed}; the builder had stopped."])
        self.assertFalse(tree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "topic"), "")
        self.assertEqual(self.remote_branches(), ["main"])
        self.assertEqual(self.notes[-1][1], f"{removed}; the builder had stopped.")
        self.assertEqual(self.watched(1), ["pack sd:1 merged at c0ffeec0ffee"])  # gone: nothing left to do

    def test_a_held_dirty_or_moved_merged_worktree_is_kept_and_tried_again(self) -> None:
        import sd_db
        from sd_db import sessions
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)
        self.drain()
        landed = len(self.notes)
        connection = sd_db.connect(home=self.tmp)
        try:
            sessions.beat(connection, "session-a", machine=sd_lane.this_host(), cwd=None,
                          found=sessions.worktrees([str(tree)]))
            connection.commit()
            [line] = self.watched(1)[1:]
            self.assertIn(f"kept {tree}: a live session holds it", line)
            sessions.end(connection, "session-a")
            connection.commit()
        finally:
            connection.close()
        (tree / "scratch").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.watched(1)[1:], [f"pack sd:1 kept {tree}: it has uncommitted changes"])
        (tree / "scratch").unlink()
        git(tree, "commit", "-q", "--allow-empty", "-m", "later")
        self.assertEqual(self.watched(1)[1:], [f"pack sd:1 kept {tree}: its tip is not the merged head {head[:12]}"])
        self.assertTrue(tree.exists())
        self.assertEqual(len(self.notes), landed)  # the landing's note, with its command, still stands
        git(tree, "reset", "-q", "--hard", head)
        self.assertEqual(self.watched(1)[1][:24], "pack sd:1 Removed: workt")

    def test_a_merged_worktree_on_another_branch_at_the_merged_head_is_kept(self) -> None:
        """Review of sd:6727: a new task branch started at the merged head is not the merged branch."""
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)
        self.drain()
        git(tree, "switch", "-q", "-c", "next-task")
        self.assertEqual(self.watched(1)[1:], [f"pack sd:1 kept {tree}: it is on next-task, not the merged branch "
                                               "topic"])
        self.assertTrue(tree.exists())
        self.assertEqual(git(tree, "rev-parse", "refs/heads/next-task"), head)

    def test_an_active_build_assignment_keeps_a_merged_worktree(self) -> None:
        import sd_db
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)
        self.drain()
        connection = sd_db.connect(home=self.tmp)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("INSERT INTO assignment (id, item, role, status) VALUES (9, 1, 'author', 'done')")
            connection.execute("INSERT INTO assignment (id, item, role, status) VALUES (7, 1, 'author', 'running')")
            connection.commit()
            self.assertEqual(self.watched(1)[1:], [f"pack sd:1 kept {tree}: build assignment 7 of item 1 is active"])
            connection.execute("UPDATE assignment SET status = 'done' WHERE id = 7")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.watched(1)[1][:24], "pack sd:1 Removed: workt")

    def test_ignored_data_beyond_build_output_keeps_a_merged_worktree(self) -> None:
        """Review of sd:6727: `git worktree remove` deletes ignored files without --force."""
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)
        self.drain()
        (self.repo / ".git/info/exclude").write_text(".env\n__pycache__/\n", encoding="utf-8")
        (tree / "__pycache__").mkdir()
        (tree / "__pycache__/a.pyc").write_bytes(b"x")
        (tree / ".env").write_text("TOKEN=local\n", encoding="utf-8")
        self.assertEqual(self.watched(1)[1:], [f"pack sd:1 kept {tree}: it holds ignored files that are not "
                                               "build output: .env"])
        self.assertEqual((tree / ".env").read_text(encoding="utf-8"), "TOKEN=local\n")
        (tree / ".env").unlink()
        self.assertEqual(self.watched(1)[1][:24], "pack sd:1 Removed: workt")  # build output alone goes
        self.assertFalse(tree.exists())

    def test_ignored_data_keeps_the_worktree_whatever_status_shows_by_default(self) -> None:
        """Review of sd:6727: `status.showUntrackedFiles=no` hides ignored files from a bare status too."""
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)
        self.drain()
        git(self.repo, "config", "status.showUntrackedFiles", "no")
        (self.repo / ".git/info/exclude").write_text(".env\n", encoding="utf-8")
        (tree / ".env").write_text("TOKEN=local\n", encoding="utf-8")
        self.assertEqual(self.watched(1)[1:], [f"pack sd:1 kept {tree}: it holds ignored files that are not "
                                               "build output: .env"])
        self.assertEqual((tree / ".env").read_text(encoding="utf-8"), "TOKEN=local\n")

    def test_a_process_working_in_a_merged_worktree_keeps_it(self) -> None:
        """Review of sd:6727: a build or server no registry names still runs there; an unread list keeps it too."""
        self.cwds.stop()  # this test reads the real lsof
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)
        self.drain()
        sleeper = subprocess.Popen(["sleep", "60"], cwd=tree)  # noqa: S607 - a stand-in for a manual build
        try:
            [line] = self.watched(1)[1:]
            self.assertIn(f"kept {tree}: a process works in ", line)
        finally:
            sleeper.kill()
            sleeper.wait()
        from sd_dashboard import health_collectors
        with mock.patch.object(health_collectors, "_cwds", return_value=None):
            self.assertEqual(self.watched(1)[1:], [f"pack sd:1 kept {tree}: not known whether a process works "
                                                   "in it: lsof gave no answer"])
        self.assertTrue(tree.exists())
        self.assertEqual(self.watched(1)[1][:24], "pack sd:1 Removed: workt")

    def test_a_worktree_the_merge_removed_is_landed_from_the_lane_root(self) -> None:
        """sd:3096. `sd-ship merge` removes a clean worktree (sd:3006); git cannot name the main
        checkout from a directory that is gone, so the landing used to read the dead path as the main
        checkout, note from it (FileNotFoundError) and see a detached HEAD."""
        tree, head = self.topic()
        landed = self.advance_origin_main()
        self.merge_lands(tree, 1, head)
        ship = self.ship

        def remove_after_merge(argv: list[str], log: pathlib.Path) -> dict:
            answer = ship(argv, log)
            if argv[2] == "merge":
                git(self.repo, "worktree", "remove", "--force", str(tree))
            return answer
        self.drain(remove_after_merge)
        entry = self.entries()[0]
        self.assertEqual(entry["note"], "written")
        [(item, body, main)] = self.notes
        self.assertEqual((item, main), (1, self.repo))
        self.assertIn("was already removed by the merge", body)
        self.assertIn("was already removed by the merge", entry["cleanup"])
        self.assertIsNone(entry["remove"])
        self.assertIn("fast-forwarded", entry["fast_forward"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), landed)

    def test_a_builder_write_through_an_open_handle_survives_the_landing(self) -> None:
        """The second review's case: a handle opened before the landing, written after it."""
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)
        with open(tree / "README.md", "a", encoding="utf-8") as handle:
            self.drain()
            handle.write("late\n")
        self.assertEqual((tree / "README.md").read_text(encoding="utf-8"), "readme\nlate\n")

    def test_the_runner_notes_through_default_note_when_given_none(self) -> None:
        """The fixture replaces `default_note`; `run_lane` must read it at the call, or a suite writes real notes."""
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([item for item, _, _ in self.notes], [1])
        self.assertEqual(self.entries()[0]["note"], "written")

    def test_a_tip_past_the_merged_head_is_kept(self) -> None:
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)
        ship = self.ship

        def commit_after_prepare(argv: list[str], log: pathlib.Path) -> dict:
            if argv[2] == "merge":
                git(tree, "commit", "-q", "--allow-empty", "-m", "later")
            return ship(argv, log)
        self.drain(commit_after_prepare)
        self.assertTrue(tree.exists())
        self.assertEqual(self.remote_branches(), ["main", "topic"])
        self.assertIn("is not the merged head", self.entries()[0]["cleanup"])

    def test_a_remote_branch_that_moved_is_not_deleted(self) -> None:
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)  # enqueue refuses an origin/topic already past the head (sd:5989)
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", "-b", "topic", str(self.origin), str(other)], check=True)
        git(other, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
        git(other, "push", "-q", "origin", "topic")
        self.drain()
        self.assertEqual(self.remote_branches(), ["main", "topic"])
        self.assertIn("origin/topic kept: it is at", self.entries()[0]["cleanup"])

    def test_the_main_checkout_is_never_removed(self) -> None:
        git(self.repo, "switch", "-q", "-c", "side")  # the lane refuses an entry on the default branch
        head = git(self.repo, "rev-parse", "HEAD")
        self.merge_lands(self.repo, 1, head)
        self.drain()
        self.assertTrue((self.repo / ".git").exists())
        self.assertIn("is the main checkout", self.entries()[0]["cleanup"])

    def test_the_main_checkout_fast_forwards_to_the_merged_base(self) -> None:
        tree, head = self.topic()
        landed = self.advance_origin_main()
        self.merge_lands(tree, 1, head)
        self.drain()
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), landed)
        self.assertIn("fast-forwarded", self.entries()[0]["fast_forward"])

    def test_a_checkout_off_the_default_branch_is_not_moved(self) -> None:
        tree, head = self.topic()
        git(self.repo, "switch", "-q", "-c", "elsewhere")
        before = git(self.repo, "rev-parse", "HEAD")
        self.advance_origin_main()
        self.merge_lands(tree, 1, head)
        self.drain()
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertIn("on elsewhere, not the default branch", self.entries()[0]["fast_forward"])

    def test_a_branch_checkout_that_deploys_by_post_merge_is_not_moved(self) -> None:
        # REGRESSION sd:3647 review: the lane moved the code, then post-merge refused the install,
        # leaving new code on old dependencies; the refusal must come before the move.
        tree, head = self.topic()
        (self.repo / "Makefile").write_text("post-merge:\n\ttrue\n", encoding="utf-8")
        before = git(self.repo, "rev-parse", "HEAD")
        self.advance_origin_main()
        self.merge_lands(tree, 1, head)
        self.drain()
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertIn("switch --detach", self.entries()[0]["fast_forward"])

    def test_origin_head_names_the_default_branch_over_the_usual_names(self) -> None:
        tree, head = self.topic()
        git(self.repo, "remote", "set-head", "origin", "main")
        git(self.repo, "switch", "-q", "-c", "master")
        before = git(self.repo, "rev-parse", "HEAD")
        self.advance_origin_main()
        self.merge_lands(tree, 1, head)
        self.drain()
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertIn("on master, not the default branch", self.entries()[0]["fast_forward"])

    def test_the_tools_checkout_tries_another_lanes_lock_once_and_never_waits(self) -> None:
        """The pack checkout runs every lane's `sd-ship`; move it only while no other lane runs."""
        tree, head = self.topic()
        before = git(self.repo, "rev-parse", "HEAD")
        self.advance_origin_main()
        self.merge_lands(tree, 1, head)
        other = self.root / "system/lane/queue/runner.lock"
        other.parent.mkdir(parents=True)
        with open(other, "a", encoding="utf-8") as held, mock.patch.object(sd_lane, "BIN", self.repo / "bin"):
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            self.drain()
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertIn("the system lane is running from this checkout", self.entries()[0]["fast_forward"])
        tree, head = self.topic("later")
        self.merge_lands(tree, 2, head)
        with mock.patch.object(sd_lane, "BIN", self.repo / "bin"):
            self.drain()  # the other lane is idle now: the next landing catches up
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), git(self.origin, "rev-parse", "main"))

    def test_a_failing_note_does_not_undo_the_merge(self) -> None:
        tree, head = self.topic()
        self.merge_lands(tree, 1, head)

        def broken(item: int, body: str, main: pathlib.Path) -> str:
            raise OSError("the store is locked")
        sd_lane.run_lane(self.repo, self.environ, self.ship, note=broken)
        entry = self.entries()[0]
        self.assertEqual(entry["status"], "merged")
        self.assertIn("the store is locked", entry["note"])
        self.assertIn("fast-forward", entry["fast_forward"])

    def post_merge(self, *recipe: str) -> None:
        """A stub Makefile in the main checkout with a `post-merge` target, untracked, as an install step would be."""
        lines = ["VERSION := 1", "post-merge:", *(f"\t{line}" for line in recipe), "other:", "\t@echo other"]
        (self.repo / "Makefile").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_post_merge_runs_once_per_run_after_the_runner_lock_is_released(self) -> None:
        """sd:3566. The system target's `repo-sync.sh refresh` waits for every lane's runner lock; under this
        lane's own it waited out its 45-minute bound. So the target runs once the run released the lock."""
        first, first_head = self.topic()
        second, second_head = self.topic("second")
        self.advance_origin_main()
        git(self.repo, "switch", "-q", "--detach")  # a deploying checkout is pinned (sd:3647)
        lock = self.store().lock_file
        self.post_merge(f'"{sys.executable}" -c "import fcntl, sys; '
                        f'fcntl.flock(open(sys.argv[1], \'a\'), fcntl.LOCK_EX | fcntl.LOCK_NB)" "{lock}"',
                        "echo ran >> post-merge.out")
        self.merge_lands(first, 1, first_head)
        self.merge_lands(second, 2, second_head)
        answer = self.drain()
        self.assertEqual([row["status"] for row in self.entries()], ["merged", "merged"])
        self.assertEqual(answer["post_merge"], f"make post-merge passed in {self.repo}")
        self.assertEqual((self.repo / "post-merge.out").read_text(encoding="utf-8").splitlines(), ["ran"])
        self.assertEqual([(item, body) for item, body, _ in self.notes if body.startswith("Post-merge")],
                         [(1, f"Post-merge: make post-merge passed in {self.repo}"),
                          (2, f"Post-merge: make post-merge passed in {self.repo}")])
        self.assertEqual(self.note_kinds[-2:], ["comment", "comment"])

    def test_a_makefile_without_the_target_runs_nothing_and_notes_nothing(self) -> None:
        tree, head = self.topic()
        (self.repo / "Makefile").write_text("post-merge-later:\n\ttouch ran\npost-merge := 1\n", encoding="utf-8")
        self.merge_lands(tree, 1, head)
        answer = self.drain()
        self.assertEqual((self.entries()[0]["status"], "post_merge" in answer, len(self.notes)), ("merged", False, 1))
        self.assertFalse((self.repo / "ran").exists())

    def test_a_run_that_merged_nothing_runs_no_post_merge(self) -> None:
        tree, head = self.topic()
        self.post_merge("touch ran")
        self.merge_lands(tree, 1, head)
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        answer = self.drain()
        self.assertEqual((self.entries()[0]["status"], "post_merge" in answer), ("failed", False))
        self.assertFalse((self.repo / "ran").exists())

    def test_a_failing_or_slow_post_merge_never_fails_the_merged_entry_and_files_a_followup(self) -> None:
        tree, head = self.topic()
        git(self.repo, "switch", "-q", "--detach")  # a deploying checkout is pinned (sd:3647)
        self.post_merge("@echo the install broke >&2", "@exit 3")
        self.merge_lands(tree, 1, head)
        answer = self.drain()
        self.assertEqual(self.entries()[0]["status"], "merged")
        self.assertTrue(answer["post_merge"].startswith(f"make post-merge failed (exit 2) in {self.repo}:"), answer)
        self.assertIn("the install broke", answer["post_merge"])
        self.assertEqual((self.notes[-1][1], self.note_kinds[-1]), (f"Post-merge: {answer['post_merge']}", "followup"))
        tree, head = self.topic("slow")
        self.post_merge("sleep 60")
        self.merge_lands(tree, 2, head)
        started = time.monotonic()
        with mock.patch.object(sd_lane, "POST_MERGE_SECONDS", 1):
            answer = self.drain()
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual((self.entries()[-1]["status"], answer["post_merge"]),
                         ("merged", f"make post-merge timed out after 1s in {self.repo}"))
        self.assertEqual(self.note_kinds[-1], "followup")

    def test_a_failed_post_merge_reports_both_tails_when_the_refusal_is_on_stdout(self) -> None:
        """sd:4047. A `[step]` line always fills stderr, so a refusal printed on stdout was dropped."""
        tree, head = self.topic()
        git(self.repo, "switch", "-q", "--detach")  # a deploying checkout is pinned (sd:3647)
        self.post_merge("@echo '[step] stop consumers' >&2", "@echo 'refused: the database is locked'",
                        f"@{sys.executable} -c \"print('x' * 900)\" >&2", "@exit 3")
        self.merge_lands(tree, 1, head)
        answer = self.drain()
        self.assertEqual(self.entries()[0]["status"], "merged")
        # make's own error line and exit code differ between make versions, so neither is asserted.
        head, _, stdout = answer["post_merge"].rpartition("; stdout: ")
        _, _, stderr = head.partition(": stderr: ")
        self.assertIn("refused: the database is locked", stdout)
        self.assertIn("x" * 100, stderr)
        self.assertLessEqual(len(stderr), 300)  # each tail stays bounded

    def test_a_checkout_that_did_not_fast_forward_skips_the_post_merge_and_says_why(self) -> None:
        tree, head = self.topic()
        git(self.repo, "switch", "-q", "-c", "elsewhere")
        self.post_merge("touch ran")
        self.merge_lands(tree, 1, head)
        answer = self.drain()
        self.assertFalse((self.repo / "ran").exists())
        self.assertEqual(self.entries()[0]["status"], "merged")
        self.assertTrue(answer["post_merge"].startswith("make post-merge skipped: the main checkout did not "
                                                        "fast-forward (fast-forward skipped: the main checkout is on "
                                                        "elsewhere"), answer["post_merge"])
        self.assertEqual((self.notes[-1][1], self.note_kinds[-1]), (f"Post-merge: {answer['post_merge']}", "followup"))

    def test_a_variable_the_lanes_environment_omits_never_reaches_the_post_merge(self) -> None:
        """sd:7051: `{**os.environ, **environ}` put back what the caller dropped, such as a GIT_DIR naming
        another repository, so the target ran against it."""
        self.post_merge('@echo "[$$GIT_DIR]" > env.out')
        environ = clean_environment()  # read before the ambient value is set: it has no GIT_DIR
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.tmp / "decoy" / ".git")}):
            ran = sd_lane.make_post_merge(self.repo, environ, "fast-forwarded to origin/main")
        self.assertEqual(ran, f"make post-merge passed in {self.repo}")
        self.assertEqual((self.repo / "env.out").read_text(encoding="utf-8").strip(), "[]")

    def test_a_pinned_detached_checkout_runs_the_post_merge_that_refreshes_it(self) -> None:
        """sd:3647. The hub's main checkout is pinned (detached): the fast-forward skips it, and `repo-sync.sh
        refresh`, which the target runs, moves only a pinned checkout. So the target must run for it."""
        tree, head = self.topic()
        git(self.repo, "switch", "-q", "--detach")
        self.post_merge("touch ran")
        self.merge_lands(tree, 1, head)
        answer = self.drain()
        self.assertEqual(self.entries()[0]["status"], "merged")
        self.assertEqual(answer.get("post_merge"), f"make post-merge passed in {self.repo}")
        self.assertTrue((self.repo / "ran").exists())

    def pin_at_a_base_without_the_target(self) -> str:
        """The real bootstrap: origin/main tracks a Makefile with no `post-merge`; the main checkout is pinned there."""
        (self.repo / "Makefile").write_text("other:\n\t@echo other\n", encoding="utf-8")
        git(self.repo, "add", "Makefile")
        git(self.repo, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-q", "-m", "base")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "switch", "-q", "--detach")
        return git(self.repo, "rev-parse", "HEAD")

    def land_a_makefile(self, text: str) -> str:
        upstream = self.tmp / "upstream"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(upstream)], check=True, env=clean_environment())
        (upstream / "Makefile").write_text(text, encoding="utf-8")
        git(upstream, "add", "Makefile")
        git(upstream, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-q", "-m", "target")
        git(upstream, "push", "-q", "origin", "main")
        return git(upstream, "rev-parse", "HEAD")

    def test_a_checkout_pinned_at_a_base_without_the_target_runs_origins_target_and_stays_put(self) -> None:
        """sd:3647. The first landing cannot rely on the old Makefile. The lane runs origin's, with the pin still
        checked out: the recipe's `repo-sync.sh refresh` stops the consumers, records the old SHA and moves it."""
        tree, head = self.topic()
        base = self.pin_at_a_base_without_the_target()
        landed = self.land_a_makefile("post-merge:\n\tgit rev-parse HEAD >> head.out\n")
        self.merge_lands(tree, 1, head)
        answer = self.drain()
        self.assertNotEqual(base, landed)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), base)
        self.assertEqual((self.repo / "Makefile").read_text(encoding="utf-8"), "other:\n\t@echo other\n")
        self.assertEqual((self.repo / "head.out").read_text(encoding="utf-8").split(), [base])
        self.assertEqual(answer.get("post_merge"), f"make post-merge passed in {self.repo}")

    def test_a_pinned_checkout_whose_origin_has_no_target_runs_nothing(self) -> None:
        tree, head = self.topic()
        base = self.pin_at_a_base_without_the_target()
        self.land_a_makefile("other:\n\t@echo other again\n")
        self.merge_lands(tree, 1, head)
        answer = self.drain()
        self.assertEqual((git(self.repo, "rev-parse", "HEAD"), "post_merge" in answer), (base, False))

    def test_a_pinned_checkout_that_cannot_fetch_origin_says_unknown_not_no_target(self) -> None:
        tree, head = self.topic()
        self.pin_at_a_base_without_the_target()
        self.merge_lands(tree, 1, head)
        self.origin.rename(self.origin.with_name("origin-moved"))
        answer = self.drain()
        self.assertTrue(answer["post_merge"].startswith("make post-merge unknown: git fetch failed"), answer)
        self.assertEqual(self.note_kinds[-1], "followup")

    def released(self, policy: dict, ruled: dict | None = None) -> dict:
        """One run with `policy` as the repository's declaration; `ruled` answers the chain's questions by step."""
        def declared(root, kind, environ=None):
            return policy if kind == "policy" else None
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sd_lane.sd_lib, "repo_declaration", declared))
            if ruled is not None:
                stack.enter_context(mock.patch.object(
                    sd_lane, "release_ruling", lambda queue, item, main: lambda tag, step, question: ruled.get(step)))
            return self.drain()

    def feat_on_origin_main(self) -> str:
        upstream = self.tmp / "feat"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(upstream)], check=True, env=clean_environment())
        git(upstream, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-q", "--allow-empty",
            "-m", "feat: a thing")
        git(upstream, "push", "-q", "origin", "main")
        return git(upstream, "rev-parse", "HEAD")

    def tag_on_origin(self, tag: str) -> str:
        return subprocess.run(["git", "-C", str(self.origin), "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
                              capture_output=True, text=True, check=False, env=clean_environment()).stdout.strip()

    def test_a_merge_in_a_repository_whose_policy_declares_release_tags_main(self) -> None:
        """sd:6881: the lane runs the release chain after it lands a PR; version from the commit types."""
        tree, head = self.topic()
        sha = self.feat_on_origin_main()
        self.merge_lands(tree, 1, head)
        answer = self.released({"release": {}})
        self.assertEqual(answer["release"], "released v0.1.0")
        self.assertEqual(self.tag_on_origin("v0.1.0"), sha)
        self.assertEqual(self.entries()[0]["release"]["step"], "done")
        self.assertEqual((self.notes[-1][:2], self.note_kinds[-1]), ((1, "Release: released v0.1.0"), "comment"))
        self.assertNotIn("release", self.released({"release": {}}))  # a run with no owed chain adds nothing

    def test_a_merge_whose_run_could_not_read_the_policy_is_released_by_a_later_run(self) -> None:
        """Review: the obligation was marked from this run's merges only, so a run that stopped, or failed to
        read the policy, after recording a merge left it with no release for good."""
        tree, head = self.topic()
        sha = self.feat_on_origin_main()
        self.merge_lands(tree, 1, head)

        def unread(root, kind, environ=None):
            raise sd_lane.sd_lib.DatabaseUnreadable("the hub did not answer")
        with mock.patch.object(sd_lane.sd_lib, "repo_declaration", unread):
            answer = self.drain()
        self.assertTrue(answer["release"].startswith("not run: the policy was not read"), answer)
        self.assertEqual(self.entries()[0]["release"], "owed")
        answer = self.released({"release": {}})
        self.assertEqual((answer["release"], self.tag_on_origin("v0.1.0")), ("released v0.1.0", sha))

    def test_a_release_killed_after_the_tag_push_resumes_at_the_verify(self) -> None:
        """Review: the step was stored only once the chain returned, so a kill after the tag push left it at
        version, and the restart read the pushed tag as the last one and released nothing."""
        tree, head = self.topic()
        sha = self.feat_on_origin_main()
        self.merge_lands(tree, 1, head)
        policy = {"release": {"verify": 'test "$SD_RELEASE_VERSION" = 0.1.0'}}

        def killed(world, command, tag, version, sha, deploy=False):
            raise Killed
        with mock.patch.object(sd_lane.ReleaseWorld, "verify", killed), self.assertRaises(Killed):
            self.released(policy)
        chain = self.entries()[0]["release"]
        self.assertEqual((chain["step"], chain["tag"], self.tag_on_origin("v0.1.0")), ("verify", "v0.1.0", sha))
        self.assertEqual(self.released(policy)["release"], "released v0.1.0")

    def test_a_tag_the_policy_holds_at_ask_waits_and_a_later_run_resumes_it(self) -> None:
        tree, head = self.topic()
        sha = self.feat_on_origin_main()
        self.merge_lands(tree, 1, head)
        answer = self.released({"mode": "org", "release": {}}, ruled={})
        self.assertTrue(answer["release"].startswith("waiting for the operator's ruling on: push the tag v0.1.0"),
                        answer)
        self.assertEqual((self.tag_on_origin("v0.1.0"), self.entries()[0]["release"]["step"]), ("", "tag"))
        notes = len(self.notes)
        self.released({"mode": "org", "release": {}}, ruled={})
        self.assertEqual(len(self.notes), notes)  # the same wait notes once
        answer = self.released({"mode": "org", "release": {}}, ruled={"tag": "go"})
        self.assertEqual((answer["release"], self.tag_on_origin("v0.1.0")), ("released v0.1.0", sha))

    def test_a_repository_without_a_release_policy_owes_no_chain(self) -> None:
        tree, head = self.topic()
        self.feat_on_origin_main()
        self.merge_lands(tree, 1, head)
        answer = self.released({"mode": "managed"})
        self.assertNotIn("release", answer)
        self.assertEqual(self.entries()[0]["release"], "owed")  # the mark waits; no chain runs
        self.assertEqual(self.tag_on_origin("v0.1.0"), "")

    def test_an_explicitly_empty_release_types_releases_nothing(self) -> None:
        """sd:7016: `release_types: []` fell back to the default types, so a `feat` still tagged."""
        tree, head = self.topic()
        self.feat_on_origin_main()
        self.merge_lands(tree, 1, head)
        answer = self.released({"commit_convention": {"release_types": []}, "release": {}})
        self.assertEqual(answer["release"], "nothing to release: release_types names no type")
        self.assertEqual((self.entries()[0]["release"]["step"], self.tag_on_origin("v0.1.0")), ("done", ""))

    def test_a_stored_release_the_reader_refuses_runs_nothing(self) -> None:
        """Older state: the reader accepted any `release` object, so a stored one may name a deploy repository
        with no file or key; a tag-only chain would skip the deploy it names."""
        tree, head = self.topic()
        self.feat_on_origin_main()
        self.merge_lands(tree, 1, head)
        answer = self.released({"release": {"deploy_repo": "~/repos/org/deploy"}})
        self.assertEqual(answer["release"], "not run: the policy declaration: release: deploy_repo, file and key "
                                            "are declared together", answer)
        self.assertEqual((self.entries()[0]["release"], self.tag_on_origin("v0.1.0")), ("owed", ""))

    def test_a_run_killed_after_the_tag_push_resumes_at_the_bump(self) -> None:
        """sd:7014: a policy naming a deploy repository runs the bump after the tag, under the deploy policy."""
        tree, head = self.topic()
        self.feat_on_origin_main()
        self.merge_lands(tree, 1, head)
        policy = {"merge_method": "rebase",
                  "release": {"deploy_repo": str(self.tmp / "deploy"), "file": "values.yaml", "key": "image.tag"}}
        made = []

        def killed(world):
            made.append((world.deploy, world.file, world.merge_method))
            raise Killed
        with mock.patch.object(sd_lane.ReleaseWorld, "deployed", killed), self.assertRaises(Killed):
            self.released(policy)
        chain = self.entries()[0]["release"]
        self.assertEqual((chain["step"], chain["tag"]), ("bump", "v0.1.0"))
        self.assertEqual(made, [(self.tmp / "deploy", "values.yaml", "rebase")], "the deploy policy's merge method")


class Killed(BaseException):
    """A process killed between two steps: nothing after the raise runs, and no `except Exception` catches it."""


class SharedQueue(Lane):
    """sd:3282, slice 1 of the sd:3174 design: the queue in the hub database. One test per failure-table row."""

    def setUp(self) -> None:
        super().setUp()
        for name, value in (("WRITE_RETRY_SECONDS", 0), ("WRITE_PAUSE", 0)):
            patcher = mock.patch.object(sd_lane, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.git_verbs: list[str] = []
        real = sd_lane.publish_git

        def recorded(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess | None:
            self.git_verbs.append(args[0])
            return real(root, *args)
        patcher = mock.patch.object(sd_lane, "publish_git", recorded)
        patcher.start()
        self.addCleanup(patcher.stop)

    def remote_tip(self, branch: str) -> str | None:
        return git(self.origin, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}") or None

    def committed(self, name: str, *messages: str) -> tuple[pathlib.Path, list[str]]:
        tree = self.worktree(name)
        heads = []
        for message in messages:
            git(tree, "commit", "-q", "--allow-empty", "-m", message)
            heads.append(git(tree, "rev-parse", "HEAD"))
        return tree, heads

    def merged_receipt(self, branch: str, item: int, head: str, phase: str = "merged") -> None:
        """The ship receipt `sd-ship merge` leaves for `branch` and `item`, in the lane or by hand."""
        import sd_db
        from sd_db import ship
        connection = sd_db.connect(home=self.tmp)
        try:
            key = ship.receipt_key("example/pack", branch, item)
            ship.save(connection, key, ship.read(connection, key)[0],
                      {"repository": "example/pack", "branch": branch, "item": item, "phase": phase, "head": head,
                       "merge_commit": "1" * 40})
        finally:
            connection.close()

    def rows(self) -> list[tuple[str, dict]]:
        import sd_db
        connection = sd_db.connect(home=self.tmp, write=False)
        try:
            return [(row["key"], json.loads(row["body"])) for row in connection.execute(
                "SELECT key, body FROM state WHERE kind = 'checkpoint' AND key LIKE 'lane:v1:%' ORDER BY id")]
        finally:
            connection.close()

    def failing(self, verb: str) -> Any:
        """A `publish_git` whose `verb` fails as a refused push or a dropped network does."""
        real = sd_lane.publish_git

        def answer(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess | None:
            self.git_verbs.append(args[0])
            if args[0] == verb:
                return subprocess.CompletedProcess(["git", *args], 1, "", "remote: refused")
            return real(root, *args)
        return mock.patch.object(sd_lane, "publish_git", answer)

    def repo_row(self, host: str | None = None) -> None:
        import sd_db
        connection = sd_db.connect(home=self.tmp)
        try:
            sd_db.upsert_repo(connection, str(self.repo), remote="https://github.com/example/pack.git", managed=1)
            connection.execute("UPDATE repo SET lane_host = ?", (host,))
        finally:
            connection.close()

    def failing_store(self, when: Any) -> Any:
        real = sd_lane.store

        def store(connection: Any, repository: str, entry: dict) -> None:
            if when(entry):
                raise sqlite3.OperationalError("the hub went away")
            real(connection, repository, entry)
        return mock.patch.object(sd_lane, "store", store)

    def make_dead(self) -> None:
        """The runner that holds the running entry exits, as a stopped or killed one does."""
        runner = subprocess.Popen([sys.executable, "-c", ""])
        runner.wait()
        sd_lane.update(self.store(), lambda rows: [row["holder"].update(pid=runner.pid) for row in rows
                                                   if row.get("status") == "running"])


class Publishing(SharedQueue):
    def test_a_failing_push_leaves_no_row_and_a_diverged_remote_refuses(self) -> None:
        """Failure table, enqueue: push refused or fails, or branch_diverged; nothing queued."""
        tree, [head] = self.committed("topic", "work")
        with self.failing("push"), self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertEqual((refused.exception.code, self.rows()), (sd_lane.PUBLISH_UNKNOWN, []))
        other = self.tmp / "other"
        git(self.tmp, "clone", "-q", str(self.origin), str(other))
        git(other, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
        git(other, "push", "-q", "origin", "HEAD:refs/heads/topic")
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertEqual((refused.exception.code, self.rows()), (sd_lane.BRANCH_DIVERGED, []))
        self.assertNotEqual(self.remote_tip("topic"), head)

    def test_enqueue_from_the_default_branch_refuses_before_any_push(self) -> None:
        """Failure table, enqueue: a main checkout with a commit origin lacks; nothing is pushed or queued."""
        before = self.remote_tip("main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "unreviewed")
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.enqueue_entry(self.repo, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertEqual((refused.exception.code, self.rows(), self.remote_tip("main"), self.git_verbs),
                         (sd_lane.DEFAULT_BRANCH, [], before, ["ls-remote"]))
        head = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(sd_lane.publish_head(self.repo, "", head), sd_lane.DEFAULT_BRANCH)

    def test_with_no_local_origin_head_the_default_branch_is_origins_not_a_guess(self) -> None:
        """Failure table, enqueue: no local `origin/HEAD`, and origin's default is `trunk`, not the local `main`."""
        git(self.repo, "push", "-q", "origin", "main:refs/heads/trunk")
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/trunk")
        before = self.remote_tip("trunk")
        tree, _ = self.committed("trunk", "unreviewed")
        self.assertEqual(git(self.repo, "for-each-ref", "refs/remotes/origin/HEAD"), "")
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertEqual((refused.exception.code, self.rows(), self.remote_tip("trunk"), "push" in self.git_verbs),
                         (sd_lane.DEFAULT_BRANCH, [], before, False))

    def test_a_stale_local_origin_head_does_not_hide_origins_default_branch(self) -> None:
        """Failure table, enqueue: the local `origin/HEAD` still names `old`; origin's HEAD is `main`."""
        git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/old")
        before = self.remote_tip("main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "unreviewed")
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.enqueue_entry(self.repo, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertEqual((refused.exception.code, self.rows(), self.remote_tip("main"), "push" in self.git_verbs),
                         (sd_lane.DEFAULT_BRANCH, [], before, False))

    def test_an_origin_that_names_no_default_branch_is_unknown_and_pushes_nothing(self) -> None:
        """Failure table, enqueue: origin advertises no HEAD symref; the lane does not guess one."""
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/unborn")
        tree, _ = self.committed("topic", "work")
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertEqual((refused.exception.code, self.rows(), self.remote_tip("topic"), "push" in self.git_verbs),
                         (sd_lane.PUBLISH_UNKNOWN, [], None, False))

    def test_the_publish_pushes_the_named_head_and_not_the_branch_tip(self) -> None:
        """Failure table, publish: the tip moved past the head; the explicit refspec publishes that head.

        Enqueue refuses such a head (`EnqueuePrechecks`); retry still publishes one.
        """
        tree, [older, tip] = self.committed("topic", "one", "two")
        self.assertIsNone(sd_lane.publish_head(tree, "topic", older))
        self.assertEqual(self.remote_tip("topic"), older)
        self.assertNotEqual(tip, older)

    def test_a_rerun_after_a_kill_between_push_and_row_writes_one_entry_and_pushes_nothing(self) -> None:
        """Failure table, enqueue: killed between the push and the row write."""
        tree, [head] = self.committed("topic", "work")
        with mock.patch.object(sd_lane, "add_entry", side_effect=Killed), self.assertRaises(Killed):
            sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertEqual((self.remote_tip("topic"), self.rows()), (head, []))
        self.git_verbs.clear()
        sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertNotIn("push", self.git_verbs)
        self.assertEqual([row["item"] for row in self.entries()], [1])

    def test_a_write_that_lands_but_reports_failure_gives_one_entry_on_rerun(self) -> None:
        """Failure table, enqueue: a hub fault after the write landed; the verb says hub_unavailable."""
        real = sd_lane.on_hub

        def landed_then_lost(queue: sd_lane.Queue, work: Any, **options: Any) -> Any:
            before = self.rows()
            answer = real(queue, work, **options)
            if self.rows() == before:  # a read, such as enqueue's check before it publishes (sd:3283)
                return answer
            raise sd_lane.LaneError("the session dropped", code=sd_lane.HUB_UNAVAILABLE)
        with mock.patch.object(sd_lane, "on_hub", landed_then_lost), self.assertRaises(sd_lane.LaneError) as lost:
            sd_lane.enqueue_entry(self.feature, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertEqual(lost.exception.code, sd_lane.HUB_UNAVAILABLE)
        with self.assertRaisesRegex(sd_lane.LaneError, "already queued"):
            sd_lane.enqueue_entry(self.feature, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertEqual(len(self.entries()), 1)


class HubReads(SharedQueue):
    """pack sd:7042: a read of the queue takes no write lock, so it waits on no writer."""

    def test_a_queue_read_does_not_wait_on_a_writer(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        import sd_db
        writer = sd_db.connect(home=self.tmp)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            entries = sd_lane.read_queue(self.store())
            waited = time.monotonic() - started
        finally:
            writer.execute("ROLLBACK")
        self.assertLess(waited, 1.0, "a queue read waited on the write lock")
        self.assertEqual(len(entries), 1)


class EnqueuePrechecks(SharedQueue):
    """sd:5989. Enqueue refuses, in seconds, what the lane would skip or fail before its review.

    system sd:5921 failed three lane runs in one night: a head the worktree
    had moved past, which the lane skipped `head_moved`, then a docs lint
    failure, each read only once the entry reached the queue's head. Enqueue
    now runs the head check and prepare's docs lint first; nothing is pushed
    or queued when either refuses. Tests stay in the lane's gate.
    """

    lints_docs = True

    def setUp(self) -> None:
        super().setUp()
        # The lint reads the privacy patterns and the item rows under `HOME`: this fixture's, never the operator's.
        patterns = self.tmp / "config" / "privacy-patterns"
        patterns.parent.mkdir()
        patterns.write_text("secret-host\\.example\\.test\n", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.tmp), "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
                                               "SYSTEM_TOOLS_CONFIG": str(patterns.parent), "JEV_SD_DOCS_LINT": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.repo / "docs/work").mkdir(parents=True)
        (self.repo / "docs/work/README.md").write_text("# Work\n", encoding="utf-8")
        git(self.repo, "add", "docs/work/README.md")
        git(self.repo, "commit", "-q", "-m", "work root")
        git(self.repo, "push", "-q", "origin", "main")

    def refused(self, tree: pathlib.Path, **extra: Any) -> sd_lane.LaneError:
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver", **extra)
        self.assertEqual((self.rows(), "push" in self.git_verbs), ([], False))
        return refused.exception

    def test_an_expected_head_the_worktree_moved_past_is_refused_before_any_push(self) -> None:
        """The lane skips this entry `head_moved` on this host; enqueue says so first."""
        tree, [older, tip] = self.committed("topic", "one", "two")
        refused = self.refused(tree, expected_head=older)
        self.assertEqual(refused.code, sd_lane.HEAD_MOVED)
        self.assertIn(f"{tree} is at {tip}, not the head {older}", str(refused))
        self.assertIn("nothing was queued or pushed", str(refused))
        self.assertIsNone(self.remote_tip("topic"))

    def test_an_expected_head_the_worktree_caught_up_from_is_queued(self) -> None:
        """Prepare's catch-up merge of the head is a head the lane runs (`at_head`)."""
        tree, [head] = self.committed("topic", "work")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "main moves")
        git(self.repo, "push", "-q", "origin", "main")
        git(tree, "merge", "-q", "--no-ff", "-m", "Merge origin/main", "origin/main")
        entry = sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver", expected_head=head)
        self.assertEqual((entry["status"], entry["expected_head"]), ("pending", head))

    def test_an_origin_branch_past_the_head_is_refused_before_any_push(self) -> None:
        """Another host's lane would skip it `head_moved`, and this host's prepare could not push."""
        tree, [head, ahead] = self.committed("topic", "one", "two")
        git(tree, "push", "-q", "origin", f"{ahead}:refs/heads/topic")
        git(tree, "reset", "-q", "--hard", head)
        refused = self.refused(tree)
        self.assertEqual(refused.code, sd_lane.HEAD_MOVED)
        self.assertIn(f"origin/topic is at {ahead}, past {head}", str(refused))
        self.assertEqual(self.remote_tip("topic"), ahead)

    def test_an_origin_branch_at_the_heads_catch_up_is_queued(self) -> None:
        """The lane pushed its catch-up of the head; that tip is the head's, not another change."""
        tree, [head] = self.committed("topic", "work")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "main moves")
        git(self.repo, "push", "-q", "origin", "main")
        git(tree, "merge", "-q", "--no-ff", "-m", "Merge origin/main", "origin/main")
        caught_up = git(tree, "rev-parse", "HEAD")
        git(tree, "push", "-q", "origin", "HEAD:refs/heads/topic")
        git(tree, "reset", "-q", "--hard", head)
        entry = sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertEqual((entry["expected_head"], self.remote_tip("topic")), (head, caught_up))

    def test_a_docs_lint_failure_the_branch_adds_is_refused_before_any_push(self) -> None:
        tree = self.worktree("topic")
        (tree / "docs/work/not-dated").mkdir(parents=True)
        (tree / "docs/work/not-dated/design.md").write_text("---\nitem: sd:1\n---\n\n# Design\n", encoding="utf-8")
        git(tree, "add", "docs/work/not-dated/design.md")
        git(tree, "commit", "-q", "-m", "a work item off the shape")
        refused = self.refused(tree)
        self.assertEqual(refused.code, "docs_lint_failed")
        self.assertIn("sd-docs-lint: 1 failure(s) this branch introduces", str(refused))
        self.assertIn("FAIL docs/work/not-dated", str(refused))
        self.assertIn("nothing was queued or pushed", str(refused))
        self.assertIsNone(self.remote_tip("topic"))

    def test_a_body_the_privacy_rule_refuses_is_refused_before_any_push(self) -> None:
        tree, _ = self.committed("topic", "work")
        self.body.write_text("Ran on secret-host.example.test\n", encoding="utf-8")
        self.assertEqual(self.refused(tree).code, "docs_lint_failed")

    def test_a_lint_that_cannot_run_is_unknown_and_queues_nothing(self) -> None:
        """Failures: a lint that did not finish is no pass."""
        tree, _ = self.committed("topic", "work")
        import sd_ship_body
        from sd_ship_remote import Refusal
        stopped = Refusal("sd-docs-lint could not finish: timed out", code="command_unavailable")
        with mock.patch.object(sd_ship_body, "completed_process", side_effect=stopped):
            refused = self.refused(tree)
        self.assertEqual(refused.code, "command_unavailable")
        self.assertIsNone(self.remote_tip("topic"))

    def test_a_clean_branch_is_linted_and_queued(self) -> None:
        tree, [head] = self.committed("topic", "work")
        entry = sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver")
        self.assertEqual((entry["status"], self.remote_tip("topic")), ("pending", head))


class ItemChecks(SharedQueue):
    """sd:3473. Enqueue refuses what prepare refuses on the item, before it pushes or queues anything.

    Eight system entries waited 3 to 32 minutes at the queue's head, then
    failed in seconds on an item of another repository, a `Closes:` item of
    another repository, or an item with no registered repository. A body
    `Closes:` line is refused now, and a later `--item` is checked instead (sd:3704).
    """

    checks_item = True

    def setUp(self) -> None:
        super().setUp()
        [self.own] = self.register_items(1)
        import sd_db
        other = self.tmp / "other"
        other.mkdir()
        connection = sd_db.connect(home=self.tmp)
        try:
            sd_db.upsert_repo(connection, str(other), remote="https://github.com/example/other.git", managed=1)
            self.foreign = sd_db.create_item(connection, kind="task", title="theirs", status="in_progress", repo=str(other))
            self.unregistered = sd_db.create_item(connection, kind="task", title="nowhere", status="in_progress")
        finally:
            connection.close()
        self.tree, _ = self.committed("topic", "work")

    def refused(self, item: int, title: str = "fix: t", body: str = "Words.\n", claim: str = "deliver",
                closes: tuple[int, ...] = ()) -> sd_lane.LaneError:
        self.body.write_text(body, encoding="utf-8")
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.enqueue_entry(self.tree, item, title, self.body, self.environ, claim=claim, closes=closes)
        self.assertEqual((self.rows(), "push" in self.git_verbs, self.remote_tip("topic")), ([], False, None))
        return refused.exception

    def test_an_item_of_another_repository_is_refused_before_any_push(self) -> None:
        refused = self.refused(self.foreign)
        self.assertEqual(refused.code, "item_repository_mismatch")
        self.assertIn(f"item sd:{self.foreign} belongs to example/other, not example/pack", str(refused))

    def test_an_item_with_no_registered_repository_is_refused_before_any_push(self) -> None:
        self.assertIn("requires an item with a registered repository", str(self.refused(self.unregistered)))

    def test_an_item_a_teammate_run_holds_is_refused_before_any_push(self) -> None:
        """pack sd:7221: the lane takes the branch only once the lead ends the run, the proof the builder stopped."""
        import sd_db
        from sd_db import teammate
        connection = sd_db.connect(home=self.tmp)
        try:
            repo = connection.execute("SELECT repo FROM item WHERE id = ?", (self.own,)).fetchone()["repo"]
            run = teammate.start(connection, [self.own], worktree=str(self.tree), branch="topic", budget_minutes=60,
                                 owner="lead")
            self.assertTrue(repo)
        finally:
            connection.close()
        refused = self.refused(self.own)
        self.assertEqual(refused.code, "teammate_held")
        self.assertIn(f"sd:{self.own} is held by teammate run {run['id']}", str(refused))
        self.assertIn(f"sd assignments beat {self.own} --end", str(refused))

    def test_an_explicit_close_a_teammate_run_holds_is_refused_before_any_push(self) -> None:
        """pack sd:7221: each later `--item` is delivered too, so it is checked as the claimed item is."""
        import sd_db
        from sd_db import teammate
        connection = sd_db.connect(home=self.tmp)
        try:
            repo = connection.execute("SELECT repo FROM item WHERE id = ?", (self.own,)).fetchone()["repo"]
            other = sd_db.create_item(connection, kind="task", title="a teammate's", status="in_progress", repo=repo)
            run = teammate.start(connection, [other], worktree="/w/teammate", branch="feat/t", budget_minutes=60,
                                 owner="lead")
        finally:
            connection.close()
        refused = self.refused(self.own, closes=(other,))
        self.assertEqual(refused.code, "teammate_held")
        self.assertIn(f"sd:{other} is held by teammate run {run['id']}", str(refused))

    def test_a_body_closes_line_is_refused_before_any_push(self) -> None:
        """sd:3704: the --item list is the one closing set, so a body `Closes:` line waits for no prepare to refuse it."""
        refused = self.refused(self.own, "fix: t", f"Closes: sd:{self.own + 99}\n")
        self.assertEqual(refused.code, "body_trailer_refused")
        self.assertIn("a body `Closes:` line closes nothing; name each item the merge closes with --item", str(refused))

    def test_a_second_item_of_another_repository_is_refused(self) -> None:
        """sd:3704: each later --item must be an item of this repository."""
        refused = self.refused(self.own, closes=(self.foreign,))
        self.assertEqual(refused.code, "closes_item_foreign")
        self.assertIn(f"names sd:{self.foreign}, which belongs to another repository", str(refused))

    def test_a_title_naming_another_repositorys_item_is_queued(self) -> None:
        """sd:3704: a title closes nothing, so the item it names is context, not a refusal."""
        self.body.write_text("Words.\n", encoding="utf-8")
        entry = sd_lane.enqueue_entry(self.tree, self.own, f"fix: two fixes (sd:{self.own}, sd:{self.foreign})", self.body,
                                      self.environ, claim="deliver")
        self.assertEqual(entry["status"], "pending")

    def items(self, *statuses: str, kind: str = "task") -> list[int]:
        """One item of this repository in each of `statuses`."""
        import sd_db
        connection = sd_db.connect(home=self.tmp)
        try:
            return [sd_db.create_item(connection, kind=kind, title=f"also {status}", status=status,
                                      repo=str(self.repo)) for status in statuses]
        finally:
            connection.close()

    def queued(self, title: str, body: str, claim: str = "deliver", closes: tuple[int, ...] = ()) -> None:
        self.body.write_text(body, encoding="utf-8")
        entry = sd_lane.enqueue_entry(self.tree, self.own, title, self.body, self.environ, claim=claim, closes=closes)
        self.assertEqual(entry["status"], "pending")

    def test_a_summary_heading_naming_an_open_item_missing_from_item_is_refused(self) -> None:
        """sd:5375: a bundled pull request closed only its first --item, and 18 items it delivered stayed open.
        Each `## Summary` group heading `<repo> sd:N` of this repository must be named with --item."""
        also, later = self.items("planning", "ready")
        body = (f"## Summary\n\npack sd:{self.own}: the first\n\n- pack sd:{also}: the second\n\n"
                f"## Test plan\n\n- pack sd:{later}: its test\n")
        refused = self.refused(self.own, "fix: two fixes", body)
        self.assertEqual(refused.code, "closing_set_incomplete")
        self.assertIn(f"names sd:{also}, which this merge leaves open; add --item {also}", str(refused))
        self.assertNotIn(f"sd:{later}", str(refused))

    def test_a_title_naming_an_open_item_missing_from_item_is_refused(self) -> None:
        """sd:5375: the title's ids of this repository count too; prepare's warning ran where no one read it."""
        first, second = self.items("in_progress", "ready_to_send", kind="work")
        refused = self.refused(self.own, f"Three fixes (sd:{self.own}, sd:{first}, sd:{second})", closes=(first,))
        self.assertEqual(refused.code, "closing_set_incomplete")
        self.assertIn(f"add --item {second}", str(refused))
        self.assertNotIn(f"--item {first}", str(refused))

    def test_every_named_item_with_an_item_flag_is_queued(self) -> None:
        also, = self.items("planning")
        self.queued(f"fix: two fixes (sd:{self.own}, sd:{also})", f"## Summary\n\npack sd:{also}: the second\n",
                    closes=(also,))

    def test_an_item_the_merge_cannot_or_need_not_close_is_not_demanded(self) -> None:
        """Done and blocked items, another repository's heading, a Refs: line, a quoted heading and an idea
        name nothing the merge must close; an associate-only claim closes nothing at all."""
        done, blocked, refs, quoted, other = self.items("done", "blocked", "planning", "planning", "planning")
        idea, = self.items("planning", kind="idea")
        body = (f"## Summary\n\npack sd:{done}: done\n\npack sd:{blocked}: blocked\n\nsystem sd:{other}: elsewhere\n\n"
                f"pack sd:{refs}: context only\n\npack sd:{idea}: an idea\n\n```\npack sd:{quoted}: example\n```\n\n"
                f"Refs: sd:{refs}\n")
        self.queued(f"fix: fixes (sd:{done}, sd:{self.foreign}, sd:{self.unregistered})", body)
        sd_lane.update(self.store(), lambda rows: rows[0].update(status="cancelled"))
        self.queued(f"docs: context (sd:{other})", "## Summary\n\nWords.\n", claim="associate-only")

    def test_a_failed_read_of_a_named_item_is_unknown_and_queues_nothing(self) -> None:
        """Failures: the hub fails on the named item's read; that is no pass, so nothing is pushed or queued."""
        also, = self.items("planning")
        real = sd_ship_body.refuse_left_open

        def hub_drops(connection: Any, *args: Any) -> None:
            class Dropped:
                def execute(self, *_: Any) -> None:
                    raise sqlite3.OperationalError("disk I/O error")
            real(Dropped(), *args)
        with mock.patch.object(sd_ship_body, "refuse_left_open", hub_drops):
            refused = self.refused(self.own, f"fix: two fixes (sd:{self.own}, sd:{also})")
        self.assertEqual(refused.code, sd_lane.HUB_UNAVAILABLE)

    def test_retry_of_an_entry_whose_named_item_reopened_is_refused(self) -> None:
        """Older state: an entry queued while its named item was done; the item reopened, so a retry
        would leave it open."""
        also, = self.items("done")
        self.queued(f"fix: two fixes (sd:{self.own}, sd:{also})", "Words.\n")
        sd_lane.update(self.store(), lambda rows: rows[0].update(status="failed"))
        import sd_db
        connection = sd_db.connect(home=self.tmp)
        try:
            connection.execute("UPDATE item SET status = 'planning' WHERE id = ?", (also,))
        finally:
            connection.close()
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, self.own, self.environ)
        self.assertEqual(refused.exception.code, "closing_set_incomplete")
        self.assertEqual([row["status"] for row in self.entries()], ["failed"])

    def test_an_associate_only_claim_with_a_second_item_is_refused(self) -> None:
        refused = self.refused(self.own, claim="associate-only", closes=(self.own + 99,))
        self.assertEqual(refused.code, "closes_need_delivery")

    def test_an_item_of_this_repository_is_queued(self) -> None:
        self.body.write_text("Words.\n", encoding="utf-8")
        entry = sd_lane.enqueue_entry(self.tree, self.own, f"fix: one fix (sd:{self.own})", self.body, self.environ,
                                      claim="deliver")
        self.assertEqual(([row["item"] for row in self.entries()], entry["status"]), ([self.own], "pending"))

    def test_retry_checks_the_item_again(self) -> None:
        """A retry queues the same item again; one moved to another repository since refuses as enqueue does."""
        sd_lane.enqueue_entry(self.tree, self.own, "fix: t", self.body, self.environ, claim="deliver")
        sd_lane.update(self.store(), lambda rows: rows[0].update(status="failed"))
        import sd_db
        connection = sd_db.connect(home=self.tmp)
        try:
            connection.execute("UPDATE item SET repo = (SELECT repo FROM item WHERE id = ?) WHERE id = ?",
                               (self.foreign, self.own))
        finally:
            connection.close()
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, self.own, self.environ)
        self.assertEqual(refused.exception.code, "item_repository_mismatch")
        self.assertEqual([row["status"] for row in self.entries()], ["failed"])


class CitedCloses(SharedQueue):
    """sd:5999, operator ruling 2026-10-24 (grill Q20): the merge closes every item a merged commit delivers.

    Enqueue adds to the closing set each open item of this repository that
    a branch commit's `Closes: sd:N` trailer names, or that a `Lane review
    fix, <repo> sd:N` line of the body or a commit names. Only those forms
    (lane review round 5 ruling): a subject or prose closes nothing. System
    sd:5939 shipped in #505 and stayed in_progress.
    """

    checks_item = True

    def setUp(self) -> None:
        super().setUp()
        [self.own] = self.register_items(1)
        import sd_db
        other = self.tmp / "other"
        other.mkdir()
        connection = sd_db.connect(home=self.tmp)
        try:
            sd_db.upsert_repo(connection, str(other), remote="https://github.com/example/other.git", managed=1)
            self.foreign = sd_db.create_item(connection, kind="task", title="theirs", status="in_progress",
                                             repo=str(other))
            self.open = [sd_db.create_item(connection, kind="task", title=f"cited {n}", status="in_progress",
                                           repo=str(self.repo)) for n in range(6)]
            self.done = sd_db.create_item(connection, kind="task", title="done", status="done", repo=str(self.repo))
        finally:
            connection.close()

    def enqueue(self, body: str, *subjects: str, claim: str = "deliver") -> dict:
        tree, _ = self.committed(f"topic-{len(self.entries())}", *subjects)
        self.body.write_text(body, encoding="utf-8")
        return sd_lane.enqueue_entry(tree, self.own, "fix: a slice", self.body, self.environ, claim=claim)

    def test_cited_open_items_join_the_closing_set_and_prose_does_not(self) -> None:
        a, b, c, d, e, f = self.open
        body = (f"## Summary\n\nKeeps the sd:{c} guard.\n\nLane review fix, pack sd:{b}: the read is unknown.\n"
                f"```\nLane review fix, pack sd:{d}: an example\n```\n<!--\nLane review fix, pack sd:{d}\n-->\n"
                f"Refs: sd:{f}\n")
        entry = self.enqueue(body, f"fix(lane): the slice (pack sd:{d})\n\nCloses: sd:{a}",
                             f"fix: a done one and a foreign one\n\nCloses: sd:{self.done}\nCloses: sd:{self.foreign}",
                             f"fix: a review fix\n\nLane review fix, pack sd:{e}: one more.\nAlso keeps sd:{c}.")
        self.assertEqual(entry["closes"], [a, b, e])
        self.calls.clear()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [prepare] = [call for call in self.calls if call[2] == "prepare"]
        self.assertEqual([prepare[i + 1] for i, part in enumerate(prepare) if part == "--item"],
                         [str(self.own), str(a), str(b), str(e)])

    def test_a_cited_close_a_teammate_run_holds_is_refused_before_any_push(self) -> None:
        """pack sd:7221: a cited close is delivered too, and the cited set is known only before the push."""
        import sd_db
        from sd_db import teammate
        a = self.open[0]
        connection = sd_db.connect(home=self.tmp)
        try:
            run = teammate.start(connection, [a], worktree="/w/teammate", branch="feat/t", budget_minutes=60,
                                 owner="lead")
        finally:
            connection.close()
        before = len(self.entries())
        with self.assertRaises(sd_lane.LaneError) as refused:
            self.enqueue("## Summary\n\nWords.\n", f"fix(lane): the slice\n\nCloses: sd:{a}")
        self.assertEqual(refused.exception.code, "teammate_held")
        self.assertIn(f"sd:{a} is held by teammate run {run['id']}", str(refused.exception))
        self.assertEqual((len(self.entries()), "push" in self.git_verbs), (before, False))

    def test_each_cited_close_is_listed_in_the_body_and_noted_on_its_item(self) -> None:
        """Lane review of sd:5999, operator ruling: a reader sees every close by citation, so a wrong one is one reopen."""
        a, b, c, *_ = self.open
        entry = self.enqueue(f"## Summary\n\nWords.\n\nLane review fix, pack sd:{b}: x.\n\nRefs: sd:{c}\n",
                             f"fix(lane): the slice\n\nCloses: sd:{a}", f"fix: first part (sd:{c})")
        sha = self.entries()[-1]["expected_head"]
        first = git(self.repo, "rev-parse", f"{sha}~1")
        self.assertEqual(entry["closes"], [a, b])
        self.assertEqual(entry["cited"], [{"item": a, "source": f"Closes trailer of commit {first[:12]}"},
                                          {"item": b, "source": "review fix line in the body"}])
        self.assertEqual(entry["body"], f"## Summary\n\nWords.\n\nLane review fix, pack sd:{b}: x.\n\n"
                                        f"## Closed by citation\n\n- pack sd:{a}: Closes trailer of commit {first[:12]}\n"
                                        f"- pack sd:{b}: review fix line in the body\n\nRefs: sd:{c}\n")
        bodies: list[str] = []

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            if argv[2] == "prepare":
                bodies.append(pathlib.Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8"))
            return self.ship(argv, log)
        with mock.patch.object(sd_lane, "default_runner_merge", lambda root: "auto"):
            sd_lane.run_lane(self.repo, self.environ, ship)
        self.assertEqual(bodies, [entry["body"]])
        cited = {item: body for item, body, _ in self.notes if item in (a, b)}
        self.assertEqual(sorted(cited), [a, b])
        self.assertIn(f"Closed by citation at merge merged-{self.own}: the Closes trailer of commit {first[:12]} cites it",
                      cited[a])
        self.assertIn(f"sd task status {b} in_progress", cited[b])

    def test_only_a_closes_trailer_closes_and_a_subject_does_not(self) -> None:
        """Lane review of sd:5999, round 5 ruling: no word list catches every way to say partial."""
        a, b, *_ = self.open
        entry = self.enqueue("Words.\n", f"fix: first part (sd:{a})", f"fix: the rest\n\nCloses: sd:{b}")
        self.assertEqual(entry["closes"], [b])

    def test_a_commit_the_branch_reverts_closes_nothing(self) -> None:
        """Lane review of sd:5999, round 2: `git revert` undoes the work its commit cites."""
        a, b, *_ = self.open
        tree, _ = self.committed("topic-revert")
        (tree / "work.txt").write_text("work\n", encoding="utf-8")
        git(tree, "add", "work.txt")
        git(tree, "commit", "-q", "-m", f"fix: implement\n\nLane review fix, pack sd:{b}: y.\n\nCloses: sd:{a}")
        git(tree, "revert", "--no-edit", "HEAD")
        self.body.write_text("Words.\n", encoding="utf-8")
        entry = sd_lane.enqueue_entry(tree, self.own, "fix: a slice", self.body, self.environ, claim="deliver")
        self.assertEqual((entry["closes"], "cited" in entry), ([], False))

    def test_a_merge_the_branch_reverts_closes_nothing_it_brought_in(self) -> None:
        """Lane review of sd:5999, round 3: `git revert -m 1` of a merge undoes the commits it brought in."""
        a, b, c, *_ = self.open
        tree, _ = self.committed("topic-merge")
        git(tree, "switch", "-q", "-c", "side")
        (tree / "side.txt").write_text("side\n", encoding="utf-8")
        git(tree, "add", "side.txt")
        git(tree, "commit", "-q", "-m", f"fix: implement\n\nLane review fix, pack sd:{b}: y.\n\nCloses: sd:{a}")
        git(tree, "switch", "-q", "topic-merge")
        git(tree, "commit", "-q", "--allow-empty", "-m", f"fix: mainline\n\nCloses: sd:{c}")
        git(tree, "merge", "-q", "--no-ff", "--no-edit", "side")
        git(tree, "revert", "--no-edit", "-m", "1", "HEAD")
        self.body.write_text("Words.\n", encoding="utf-8")
        entry = sd_lane.enqueue_entry(tree, self.own, "fix: a slice", self.body, self.environ, claim="deliver")
        self.assertEqual(entry["closes"], [c])

    def test_a_range_the_branch_reverts_closes_nothing(self) -> None:
        """Lane review of sd:5999, round 3: `git revert A..B` writes one revert per commit, each naming it."""
        a, b, c, *_ = self.open
        tree, _ = self.committed("topic-range")
        for number in (a, b, c):
            (tree / f"{number}.txt").write_text("work\n", encoding="utf-8")
            git(tree, "add", f"{number}.txt")
            git(tree, "commit", "-q", "-m", f"fix: implement\n\nCloses: sd:{number}")
        git(tree, "revert", "--no-edit", "HEAD~2..HEAD")
        self.body.write_text("Words.\n", encoding="utf-8")
        entry = sd_lane.enqueue_entry(tree, self.own, "fix: a slice", self.body, self.environ, claim="deliver")
        self.assertEqual(entry["closes"], [a])

    def test_a_hand_written_revert_closes_nothing_it_undid(self) -> None:
        """sd:8373: a `revert:` commit naming no sha undid a cited commit and its fixes; the item closed."""
        a, b, *_ = self.open
        tree, _ = self.committed("topic-hand-revert")
        (tree / "kept.txt").write_text("kept\n", encoding="utf-8")
        git(tree, "add", "kept.txt")
        git(tree, "commit", "-q", "-m", f"fix: kept\n\nCloses: sd:{b}")
        (tree / "work.txt").write_text("work\n", encoding="utf-8")
        git(tree, "add", "work.txt")
        git(tree, "commit", "-q", "-m", f"feat: implement\n\nCloses: sd:{a}")
        (tree / "work.txt").write_text("work, fixed\n", encoding="utf-8")
        git(tree, "commit", "-q", "-am", "fix: the work")
        git(tree, "rm", "-q", "work.txt")
        git(tree, "commit", "-q", "-m", "revert: hold the work for a redesign\n\nThe operator split it out.")
        self.body.write_text("Words.\n", encoding="utf-8")
        entry = sd_lane.enqueue_entry(tree, self.own, "fix: a slice", self.body, self.environ, claim="deliver")
        self.assertEqual(entry["closes"], [b])

    def test_a_revert_of_other_work_keeps_a_close_on_a_quoted_filename(self) -> None:
        """sd:8373 review: git quotes a name with a tab or non-ASCII byte; a quoted pathspec matched nothing."""
        a, b, *_ = self.open
        tree, _ = self.committed("topic-quoted-name")
        name = " naïve\tname.txt"
        (tree / name).write_text("work\n", encoding="utf-8")
        git(tree, "add", "--", name)
        git(tree, "commit", "-q", "-m", f"feat: implement\n\nCloses: sd:{a}")
        (tree / "other.txt").write_text("other\n", encoding="utf-8")
        git(tree, "add", "other.txt")
        git(tree, "commit", "-q", "-m", f"fix: other\n\nCloses: sd:{b}")
        git(tree, "rm", "-q", "other.txt")
        git(tree, "commit", "-q", "-m", "revert: hold the other fix")
        self.body.write_text("Words.\n", encoding="utf-8")
        entry = sd_lane.enqueue_entry(tree, self.own, "fix: a slice", self.body, self.environ, claim="deliver")
        self.assertEqual(entry["closes"], [a])

    def test_a_later_change_that_is_no_revert_keeps_the_close(self) -> None:
        """sd:8373: only a revert subject undoes a cited commit; a later fix that empties the file does not."""
        a, *_ = self.open
        tree, _ = self.committed("topic-later-fix")
        (tree / "work.txt").write_text("work\n", encoding="utf-8")
        git(tree, "add", "work.txt")
        git(tree, "commit", "-q", "-m", f"feat: implement\n\nCloses: sd:{a}")
        git(tree, "rm", "-q", "work.txt")
        git(tree, "commit", "-q", "-m", "refactor: fold the work into the caller")
        self.body.write_text("Words.\n", encoding="utf-8")
        entry = sd_lane.enqueue_entry(tree, self.own, "fix: a slice", self.body, self.environ, claim="deliver")
        self.assertEqual(entry["closes"], [a])

    def test_an_associate_only_claim_closes_nothing_it_cites(self) -> None:
        entry = self.enqueue("Words.\n", f"fix: the slice\n\nCloses: sd:{self.open[0]}", claim="associate-only")
        self.assertEqual(entry["closes"], [])

    def test_a_failed_hub_read_queues_and_pushes_nothing(self) -> None:
        """Failures: the cited items' read did not answer; that is no "nothing to close"."""
        unanswered = mock.patch.object(sd_lane, "cited_closes", side_effect=sqlite3.OperationalError("disk I/O error"))
        with unanswered, self.assertRaises(sd_lane.LaneError) as refused:
            self.enqueue("Words.\n", f"fix: the slice\n\nCloses: sd:{self.open[0]}")
        self.assertEqual((refused.exception.code, self.rows(), "push" in self.git_verbs),
                         (sd_lane.HUB_UNAVAILABLE, [], False))


class CommitConvention(SharedQueue):
    """sd:6715, run 210 blocker: enqueue applies the repository's declared `commit_convention` to the title
    and each branch commit, before anything is pushed or queued. A lane merge commit is exempt."""

    CONVENTION = {"types": ["feat", "fix", "doc"], "aliases": {"docs": "doc"}, "body": "required",
                  "footer": r"^Ref: LOG-\d+$"}

    def declare(self, convention: dict) -> None:
        import sd_db
        from sd_db.repos import set_declaration
        self.register_items(0)
        connection = sd_db.connect(home=self.tmp)
        try:
            set_declaration(connection, str(self.repo), "policy", {"mode": "org", "commit_convention": convention})
        finally:
            connection.close()

    def enqueue(self, tree: pathlib.Path, title: str) -> dict:
        return sd_lane.enqueue_entry(tree, 1, title, self.body, self.environ, claim="deliver")

    def test_a_commit_the_convention_refuses_is_named_before_any_push(self) -> None:
        self.declare(self.CONVENTION)
        tree, [bare, alias, _] = self.committed(
            "topic", "fix: no body", "docs: words\n\nWhy.\n\nRef: LOG-1", "fix: good\n\nWhy.\n\nRef: LOG-2")
        with self.assertRaises(sd_lane.LaneError) as refused:
            self.enqueue(tree, "chore: the slice")
        self.assertEqual((refused.exception.code, self.rows(), "push" in self.git_verbs, self.remote_tip("topic")),
                         ("commit_convention", [], False, None))
        message = str(refused.exception)
        self.assertIn("the title: type 'chore' is not one of feat, fix, doc", message)
        self.assertIn(f"commit {bare[:12]}: no footer line matches", message)
        self.assertIn(f"commit {alias[:12]}: reword its subject to 'doc: words'", message)

    def test_conforming_commits_and_a_lane_merge_are_queued(self) -> None:
        self.declare(self.CONVENTION)
        tree, _ = self.committed("topic", "fix: good\n\nWhy.\n\nRef: LOG-2")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "main moves")
        git(self.repo, "push", "-q", "origin", "main")
        git(tree, "merge", "-q", "--no-ff", "-m", "chore: merge origin/main", "origin/main")
        entry = self.enqueue(tree, "docs: the slice")
        self.assertEqual(entry["status"], "pending")

    def test_no_declared_convention_changes_nothing(self) -> None:
        tree, _ = self.committed("topic", "chore: no body")
        self.assertEqual(self.enqueue(tree, "chore: the slice")["status"], "pending")


class LineLimits(SharedQueue):
    """sd:5793: enqueue refuses a title over 63 characters, or a body or commit line over 72.

    Some repositories lint every commit line at 72; the squash subject is
    the title and ` (#NNNNN)`. The refusal names each line before anything
    is pushed or queued.
    """

    def refused(self, tree: pathlib.Path, title: str) -> sd_lane.LaneError:
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.enqueue_entry(tree, 1, title, self.body, self.environ, claim="deliver")
        self.assertEqual((self.rows(), "push" in self.git_verbs, self.remote_tip(tree.name)), ([], False, None))
        return refused.exception

    def test_long_title_body_and_commit_lines_are_named_before_any_push(self) -> None:
        tree, [first, _] = self.committed("topic", "s" * 73 + "\n\n" + "w" * 72 + "\n" + "b" * 80, "fine")
        self.body.write_text("## Summary\n\n" + "x" * 75 + "\n", encoding="utf-8")
        refused = self.refused(tree, "t" * 64)
        self.assertEqual(refused.code, "line_too_long")
        self.assertIn(f"the title is 64 characters (limit 63); body line 3 is 75 characters (limit 72); "
                      f"commit {first[:12]} line 1 is 73 characters (limit 72); "
                      f"commit {first[:12]} line 4 is 80 characters (limit 72); nothing was queued", str(refused))

    def test_lines_at_the_limit_a_fence_and_a_bare_url_are_queued(self) -> None:
        url = "https://example.test/" + "p" * 80
        tree, _ = self.committed("topic", "s" * 72 + "\n\n" + url + "\n\n```\n" + "c" * 90 + "\n```")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "main moves")
        git(self.repo, "push", "-q", "origin", "main")
        git(tree, "merge", "-q", "--no-ff", "-m", "m" * 72, "origin/main")
        self.body.write_text("b" * 72 + f"\n{url}\n~~~\n" + "c" * 90 + "\n", encoding="utf-8")
        entry = sd_lane.enqueue_entry(tree, 1, "fix: " + "t" * 58, self.body, self.environ, claim="deliver")
        self.assertEqual((entry["status"], self.remote_tip("topic")), ("pending", entry["expected_head"]))

    DEPENDABOT = "build(deps): bump example from 1.0 to 1.1\n\n" + "r" * 140

    def test_a_dependabot_commit_is_exempt(self) -> None:
        """sd:8191: a Dependabot body runs to 140 and the branch cannot be rewritten."""
        tree = self.worktree("topic")
        git(tree, "commit", "-q", "--allow-empty", "-m", self.DEPENDABOT,
            "--author", "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>")
        entry = sd_lane.enqueue_entry(tree, 1, "chore: bump example", self.body, self.environ, claim="deliver")
        self.assertEqual(entry["status"], "pending")

    def test_the_same_lines_from_another_author_are_refused(self) -> None:
        tree, [head] = self.committed("topic", self.DEPENDABOT)
        self.assertIn(f"commit {head[:12]} line 3 is 140 characters", str(self.refused(tree, "t")))

    def test_a_merge_commit_is_a_branch_commit_too(self) -> None:
        """Codex review: `--no-merges` let a 90-character merge subject through; only two exemptions exist."""
        tree, _ = self.committed("topic", "fine")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "main moves")
        git(self.repo, "push", "-q", "origin", "main")
        git(tree, "merge", "-q", "--no-ff", "-m", "m" * 90, "origin/main")
        merge = git(tree, "rev-parse", "HEAD")
        refused = self.refused(tree, "t")
        self.assertIn(f"commit {merge[:12]} line 1 is 90 characters (limit 72)", str(refused))

    def test_commits_main_already_has_are_not_the_branchs(self) -> None:
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "m" * 90)
        git(self.repo, "push", "-q", "origin", "main")
        tree, _ = self.committed("topic", "fine")
        self.assertEqual(sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver")["status"],
                         "pending")

    def test_an_unreadable_branch_range_is_unknown_and_queues_nothing(self) -> None:
        """Failures: no local `origin/main` to count the branch's commits from is no pass."""
        tree, _ = self.committed("topic", "fine")
        git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual(self.refused(tree, "t").code, sd_lane.PUBLISH_UNKNOWN)

    def test_an_entry_already_queued_is_not_checked_again(self) -> None:
        """Older state: an entry queued before the limit retries with its long title and body."""
        tree, _ = self.committed("topic", "fine")
        sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, claim="deliver")
        sd_lane.update(self.store(), lambda rows: rows[0].update(status="failed", title="t" * 80, body="b" * 80))
        self.assertEqual(sd_lane.retry(self.repo, 1, self.environ)["title"], "t" * 80)


class ConventionalTitles(SharedQueue):
    """sd:6059: enqueue refuses a title that is not a Conventional Commits header, before any push.

    The title is the squash subject, and semantic-release counts only such
    subjects (operator ruling 2026-10-24).
    """

    def test_a_plain_title_is_refused_before_any_push(self) -> None:
        tree, _ = self.committed("topic", "fine")
        for title in ("Lane retry refuses a moved head", "Fix: capital type", "fix:no space", "wip: draft"):
            with self.subTest(title=title), self.assertRaises(sd_lane.LaneError) as refused:
                sd_lane.enqueue_entry(tree, 1, title, self.body, self.environ, claim="deliver")
            self.assertEqual(refused.exception.code, "title_not_conventional")
            self.assertIn(f'the title "{title}" is not a Conventional Commits header', str(refused.exception))
        self.assertEqual((self.rows(), "push" in self.git_verbs, self.remote_tip("topic")), ([], False, None))

    def test_a_conventional_title_is_queued(self) -> None:
        for number, title in enumerate(("fix(lane): retry refuses a moved head", "feat!: drop a verb",
                                        "chore: bump the pin"), start=1):
            tree, _ = self.committed(f"topic-{number}", "fine")
            with self.subTest(title=title):
                entry = sd_lane.enqueue_entry(tree, number, title, self.body, self.environ, claim="deliver")
                self.assertEqual((entry["status"], entry["title"]), ("pending", title))

    def test_an_entry_queued_before_the_rule_retries_with_its_title(self) -> None:
        """Older state: retry does not check the title again, as it does not check line lengths."""
        tree, _ = self.committed("topic", "fine")
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, claim="deliver")
        sd_lane.update(self.store(), lambda rows: rows[0].update(status="failed", title="Lane fix"))
        self.assertEqual(sd_lane.retry(self.repo, 1, self.environ)["title"], "Lane fix")


class ClaimAndLease(SharedQueue):
    def test_a_claim_that_lands_with_its_answer_lost_is_put_back_by_the_next_run(self) -> None:
        """Failure table, claim: the hub fails after the claim landed; no step starts, and reclaim puts it back."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        real = sd_lane.on_hub

        def claim_lands_then_hub_goes(queue: sd_lane.Queue, work: Any, **options: Any) -> Any:
            answer = real(queue, work, **options)
            if any(body["status"] == "running" for _, body in self.rows()):
                raise sd_lane.LaneError("the session dropped", code=sd_lane.HUB_UNAVAILABLE)
            return answer
        with mock.patch.object(sd_lane, "on_hub", claim_lands_then_hub_goes):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertIn("session dropped", answer["stopped"])
        self.assertEqual((self.calls, self.entries()[0]["status"]), ([], "running"))
        self.make_dead()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([row["status"] for row in answer["reclaimed"]], ["pending"])
        self.assertEqual([(row["item"], row["status"]) for row in self.entries()], [(1, "merged")])

    def test_a_move_between_the_host_read_and_the_claim_claims_nothing(self) -> None:
        """Failure table, claim: the lane moved after the runner's last read; the claim reads the host itself."""
        self.repo_row()
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        settle = sd_lane.settle_checkout

        def then_moved(root: pathlib.Path, queue: sd_lane.Queue) -> dict:
            self.repo_row("build-2")
            return settle(root, queue)
        with mock.patch.object(sd_lane, "settle_checkout", then_moved):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertIn("runs on build-2", answer["stopped"])
        self.assertEqual((self.calls, self.entries()[0]["status"]), ([], "pending"))

    def test_two_hosts_claim_and_one_wins(self) -> None:
        """Failure table, claim: old and new host claim at once; the second sees a running entry and stops."""
        self.repo_row()
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, claim="deliver")
        first = sd_lane.claim_entry(self.store(), "hub-token")
        self.repo_row("build-2")
        with mock.patch("sd_db.ship.this_host", lambda: "build-2"), self.assertRaises(sd_lane.LaneError) as busy:
            sd_lane.claim_entry(self.store(), "satellite-token")
        self.assertEqual((first["item"], busy.exception.code), (1, "lane_busy"))
        self.assertEqual([row["status"] for row in self.entries()], ["running", "pending"])

    def test_another_hosts_entry_gets_no_speculative_gate_and_says_why(self) -> None:
        """Failure table, speculation (sd:3283): the next entry has no worktree on this host; nothing is gated."""
        self.repo_row()
        sd_lane.enqueue_entry(self.worktree("second"), 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        with mock.patch("sd_db.ship.this_host", lambda: "build-2"):
            sd_lane.enqueue_entry(self.feature, 2, "fix: two", self.body, self.environ, claim="deliver")
        claimed = sd_lane.claim_entry(self.store(), "token")
        self.assertEqual((claimed or {}).get("item"), 1)
        gate = mock.Mock()
        self.assertEqual(sd_lane.speculate(claimed, self.store(), gate), [])
        gate.assert_not_called()
        speculation = self.entries()[1]["speculation"]
        self.assertEqual((speculation["after"], speculation["status"]), (1, "skipped"))
        self.assertIn("no worktree on this host", speculation["reason"])

    def test_a_kill_mid_prepare_puts_the_entry_back_and_the_third_fails_it(self) -> None:
        """Failure table, prepare: the runner dies; pid gone, back to pending; the third reclaim fails it."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        seen: list[Any] = []

        def killed_in_prepare(argv: list[str], log: pathlib.Path) -> dict:
            seen.append(self.entries()[0].get("reclaims"))
            raise Killed
        for _ in range(sd_lane.RECLAIMS):
            with self.assertRaises(Killed):
                sd_lane.run_lane(self.repo, self.environ, killed_in_prepare)
            self.make_dead()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((seen, answer["reclaimed"][0]["status"], entry["status"], entry["reclaims"], self.calls),
                         ([None, 1, 2], "failed", "failed", sd_lane.RECLAIMS, []))
        self.assertIn("in prepare", entry["reason"])

    def test_release_voids_the_token_and_refuses_a_live_local_holder(self) -> None:
        """Failure table, release (D3): a released holder's writes are refused; a live pid here is not released."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        entry = sd_lane.claim_entry(self.store(), "mine")
        with self.assertRaises(sd_lane.LaneError) as alive:
            sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual((alive.exception.code, self.entries()[0]["status"]), ("holder_alive", "running"))
        sd_lane.update(self.store(), lambda rows: rows[0]["holder"].update(host="build-2"))
        released = sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual((released["status"], released["released"], "holder" in released), ("pending", True, False))
        before = self.rows()
        for write in (lambda: sd_lane.advance(self.store(), entry, "mine"),
                      lambda: sd_lane.finish_entry(self.store(), entry, "mine", {"status": "merged"})):
            with self.assertRaises(sd_lane.Stop) as lost:
                write()
            self.assertEqual(lost.exception.code, sd_lane.CLAIM_LOST)
        self.assertEqual(self.rows(), before)
        # At `step: merge` a release fails the entry: a merge may have landed.
        entry = sd_lane.claim_entry(self.store(), "again")
        sd_lane.advance(self.store(), entry, "again")
        sd_lane.update(self.store(), lambda rows: rows[0]["holder"].update(host="build-2"))
        released = sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual(released["status"], "failed")
        self.assertIn("a merge may have landed", released["reason"])

    def elsewhere(self) -> None:
        """The running entry's holder is on another machine, so a release counts it stuck on the operator's word."""
        sd_lane.update(self.store(), lambda rows: [row["holder"].update(host="build-2") for row in rows
                                                   if row.get("status") == "running"])

    def test_a_bound_release_refuses_a_step_the_caller_did_not_see(self) -> None:
        """Failure table, release (sd:3330): prepare moved to merge between the read and the cancel."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        entry = sd_lane.claim_entry(self.store(), "seen")
        seen = f"{self.entries()[0]['holder']['token']}:{self.entries()[0]['step']}"
        sd_lane.advance(self.store(), entry, "seen")
        self.elsewhere()
        before = self.rows()
        with self.assertRaises(sd_lane.LaneError) as stale:
            sd_lane.cancel(self.repo, 1, self.environ, expected_claim=seen)
        self.assertEqual((stale.exception.code, self.rows()), (sd_lane.STALE_CLAIM, before))
        with self.assertRaisesRegex(sd_lane.LaneError, "<holder token>:<step>"):  # a token alone names no step
            sd_lane.cancel(self.repo, 1, self.environ, expected_claim="seen")
        released = sd_lane.cancel(self.repo, 1, self.environ, expected_claim="seen:merge")
        self.assertEqual((released["status"], released["released"]), ("failed", True))

    def test_a_bound_release_refuses_a_reclaimed_entry_at_the_same_revision(self) -> None:
        """Failure table, release (sd:3330): recovery put the entry back and a new runner claimed it again."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, claim="deliver")
        sd_lane.claim_entry(self.store(), "seen")
        revision = sd_lane.queue_revision(self.entries())
        self.make_dead()
        reclaimed = sd_lane.runner_write(self.store(), sd_lane.reclaim_dead)
        again = sd_lane.claim_entry(self.store(), "new")
        self.elsewhere()
        self.assertEqual((reclaimed[0]["status"], again["item"], sd_lane.queue_revision(self.entries())),
                         ("pending", 1, revision))  # the revision alone cannot tell the two claims apart
        before = self.rows()
        with self.assertRaises(sd_lane.LaneError) as stale:
            sd_lane.cancel(self.repo, 1, self.environ, expected_revision=revision, expected_claim="seen:prepare")
        self.assertEqual((stale.exception.code, self.rows()), (sd_lane.STALE_CLAIM, before))
        self.assertEqual(self.entries()[0]["holder"]["token"], "new")

    def test_a_bound_release_refuses_a_finished_entry_and_another_items_entry(self) -> None:
        """Failure table, release (sd:3330): the claim ended before the cancel, and its item was queued again."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, claim="deliver")
        entry = sd_lane.claim_entry(self.store(), "seen")
        self.elsewhere()
        before = self.rows()
        with self.assertRaises(sd_lane.LaneError) as other:  # item 1's claim names no entry of item 2
            sd_lane.cancel(self.repo, 2, self.environ, expected_claim="seen:prepare")
        self.assertEqual((other.exception.code, self.rows()), (sd_lane.STALE_CLAIM, before))
        sd_lane.finish_entry(self.store(), entry, "seen", {"status": "failed", "step": "prepare", "reason": "red"})
        sd_lane.retry(self.repo, 1, self.environ)
        before = self.rows()
        with self.assertRaises(sd_lane.LaneError) as finished:
            sd_lane.cancel(self.repo, 1, self.environ, expected_claim="seen:prepare")
        self.assertEqual((finished.exception.code, self.rows()), (sd_lane.STALE_CLAIM, before))
        self.assertEqual([row["status"] for row in self.entries() if row["item"] == 1], ["failed", "pending"])

    def test_the_claim_is_compared_under_the_queue_lock(self) -> None:
        """A step write that lands while the cancel waits for the hub's write lock is caught (sd:3330)."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.claim_entry(self.store(), "seen")
        self.elsewhere()
        refused: list[str | None] = []

        def bound_cancel() -> None:
            try:
                sd_lane.cancel(self.repo, 1, self.environ, expected_claim="seen:prepare")
            except sd_lane.LaneError as error:
                refused.append(error.code)
        import sd_db
        writer = sd_db.connect(home=self.tmp)
        self.addCleanup(writer.close)
        with sd_db.database.transaction(writer):  # BEGIN IMMEDIATE: the cancel waits for it
            canceller = threading.Thread(target=bound_cancel)
            canceller.start()
            time.sleep(0.5)
            [row] = sd_lane.stored(writer, "example/pack")
            sd_lane.store(writer, "example/pack", {**row, "step": "merge"})
        canceller.join(30)
        self.assertEqual((refused, self.entries()[0]["status"], self.entries()[0]["step"]),
                         ([sd_lane.STALE_CLAIM], "running", "merge"))

    def test_cancel_takes_the_claim_list_prints_and_unbound_keeps_todays_behaviour(self) -> None:
        """`--expected-claim <holder.token>:<step>` from `lane list`; a stale one exits 3 with its code (sd:3330)."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.claim_entry(self.store(), "seen")
        self.elsewhere()

        def lane(*args: str) -> tuple[int, dict]:
            done = subprocess.run([sys.executable, str(REPO_ROOT / "bin/sd-ship"), "-C", str(self.repo), "lane", *args],
                                  env=clean_environment(**self.environ), capture_output=True, text=True, timeout=120)
            return done.returncode, json.loads(done.stdout)
        [row] = lane("list")[1]["entries"]
        code, refused = lane("cancel", "1", "--expected-claim", f"{row['holder']['token']}:merge")
        self.assertEqual((code, refused["code"]), (3, sd_lane.STALE_CLAIM))
        code, released = lane("cancel", "1", "--expected-claim", f"{row['holder']['token']}:{row['step']}")
        self.assertEqual((code, released["status"]), (0, "pending"))
        self.assertEqual(lane("cancel", "1")[1]["status"], "cancelled")  # unbound: a pending entry, as before

    def test_an_expired_lease_with_a_live_pid_on_this_host_keeps_the_entry_running(self) -> None:
        """Failure table, prepare: the lease passed while the holder slept; same host, live pid: no reclaim."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.claim_entry(self.store(), "sleeper")
        sd_lane.update(self.store(), lambda rows: rows[0].update(lease_until="2020-01-01T00:00:00Z"))
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertNotIn("reclaimed", answer)
        self.assertIn("one entry of a repository runs at a time", answer["stopped"])
        self.assertEqual((self.calls, self.entries()[0]["status"]), ([], "running"))

    def test_a_step_write_that_fails_stops_the_runner_and_reclaim_puts_it_back(self) -> None:
        """Failure table, before merge: the hub fails for the whole retry; no merge, and the rerun runs it."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        with self.failing_store(lambda entry: entry.get("step") == "merge"):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertIn("the hub went away", answer["stopped"])
        self.assertEqual(([c[2] for c in self.calls], self.entries()[0]["step"]), (["prepare"], "prepare"))
        self.make_dead()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((answer["reclaimed"][0]["status"], self.entries()[0]["status"]), ("pending", "merged"))

    def test_a_slot_wait_put_back_that_fails_to_write_is_settled_by_the_next_run(self) -> None:
        """pack sd:7412, failure table row 2b: the hub fails the write of `slot_waits`; the entry stays running,
        the next run's reclaim puts it back, and the run after ships it."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = Runner.SLOT_WAIT
        with self.failing_store(lambda entry: "slot_waits" in entry):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertIn("the hub went away", answer["stopped"])
        self.assertEqual((self.entries()[0]["status"], "slot_waits" in self.entries()[0]), ("running", False))
        self.make_dead()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(answer["reclaimed"][0]["status"], "pending")
        del self.answers[(1, "prepare")]
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[0]["status"], "merged")

    def test_a_retryable_put_back_that_fails_to_write_is_reclaimed_and_counts_nothing(self) -> None:
        """Failure table, sd:3478 put-back: the write of the count fails; the entry stays running, a reclaim
        puts it back with no count, and the next prepare still has its one retry."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = Runner.NO_PLAN
        with self.failing_store(lambda entry: "retryable_retries" in entry):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertIn("the hub went away", answer["stopped"])
        self.assertEqual((self.entries()[0]["status"], "retryable_retries" in self.entries()[0]), ("running", False))
        self.make_dead()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((answer["reclaimed"][0]["status"], entry["status"], entry["retryable_retries"]),
                         ("pending", "pending", 1))
        del self.answers[(1, "prepare")]
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.entries()[0]["status"], [c[2] for c in self.calls]),
                         ("merged", ["prepare", "prepare", "prepare", "merge"]))

    def test_a_runner_write_is_tried_again_until_the_hub_answers(self) -> None:
        """The 10-minute write retry: two faults, then the write lands and the entry merges."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        faults = [1, 1]
        with mock.patch.object(sd_lane, "WRITE_RETRY_SECONDS", 600), \
                self.failing_store(lambda entry: entry.get("step") == "merge" and faults and faults.pop()):
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((faults, self.entries()[0]["status"]), ([], "merged"))

    def test_a_hub_that_refuses_this_build_stops_the_runner_at_once(self) -> None:
        """sd:3359. Prepare met a hub fault, and the hub then refused the satellite's build.

        The runner retried the put-back for 10 minutes under its lock. Only `repo-sync.sh follow` clears
        that refusal, and `follow` waits for every runner lock: each waited on the other. A process keeps
        the build it loaded, so no retry in it can pass. It stops without a sleep, the entry stays running
        under a holder that is gone, and the next run reclaims it.
        """
        import sd_db.database
        from sd_db import remote
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        refusing: list[bool] = []
        connect = sd_db.database.connect

        def hub(*args: Any, **kwargs: Any) -> Any:
            if refusing:
                raise remote.BuildMismatch("build", "0000aaaa", "1111bbbb")
            return connect(*args, **kwargs)

        def prepare_then_upgrade(argv: list[str], log: pathlib.Path) -> dict:
            self.ship(argv, log)
            refusing.append(True)
            return Runner.HUB_FAULT

        class Slept(BaseException):
            pass

        clock = mock.Mock(wraps=time)
        clock.sleep.side_effect = Slept
        with mock.patch.object(sd_lane, "WRITE_RETRY_SECONDS", 600), mock.patch.object(sd_lane, "WRITE_PAUSE", 30), \
                mock.patch.object(sd_lane, "time", clock), mock.patch("sd_db.database.connect", hub):
            answer = sd_lane.run_lane(self.repo, self.environ, prepare_then_upgrade)
        self.assertEqual(clock.sleep.call_count, 0)
        self.assertIn("BuildMismatch", answer["stopped"])
        self.assertIn("repo-sync.sh", answer["stopped"])
        with open(self.store().lock_file, "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free for `follow`
        self.assertEqual((self.entries()[0]["status"], self.entries()[0]["step"]), ("running", "prepare"))
        self.make_dead()
        self.calls.clear()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((answer["reclaimed"][0]["status"], self.entries()[0]["status"]), ("pending", "merged"))
        self.assertEqual([c[2] for c in self.calls], ["prepare", "merge"])

    def test_a_finish_write_that_fails_after_a_merge_is_merged_by_reclaim(self) -> None:
        """Failure table, finish: the hub fails after a merge; the run stops, and reclaim writes the outcome it
        kept (sd:4184). A runner that died without a record still fails it: see the mid-merge test above."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        with self.failing_store(lambda entry: entry.get("status") == "merged"):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((answer["ran"][0]["recorded"], self.entries()[0]["step"]), (False, "merge"))
        self.make_dead()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry["merge_commit"], len(self.calls)), ("merged", "merged-1", 2))

    def test_a_late_finish_after_a_reclaim_writes_nothing(self) -> None:
        """Failure table, finish: the token was reclaimed while the holder slept; it reports claim_lost."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        entry = sd_lane.claim_entry(self.store(), "sleeper")
        self.make_dead()
        sd_lane.runner_write(self.store(), sd_lane.reclaim_dead)
        before = self.rows()
        with self.assertRaises(sd_lane.Stop) as lost:
            sd_lane.finish_entry(self.store(), entry, "sleeper", {"status": "merged"})
        self.assertEqual((lost.exception.code, self.rows()), (sd_lane.CLAIM_LOST, before))

    def test_a_double_finish_writes_one_outcome_row(self) -> None:
        """Failure table, finish: a retried outcome write finds its own outcome under its token."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        entry = sd_lane.claim_entry(self.store(), "mine")
        for _ in range(2):
            sd_lane.finish_entry(self.store(), entry, "mine", {"status": "merged", "merge_commit": "c0ffee"})
        merged = [body for _, body in self.rows() if body["status"] == "merged"]
        self.assertEqual(len(merged), 1)


class RefusingHub(SharedQueue):
    """A hub that refuses this machine's build once `refusing` holds a value, as after an upgrade (sd:4184)."""

    def setUp(self) -> None:
        self.authority = sd_lane.default_runner_merge  # the real reader, before the suite replaces it
        super().setUp()
        import sd_db.database
        from sd_db import remote
        self.refusing: list[bool] = []
        connect = sd_db.database.connect

        def hub(*args: Any, **kwargs: Any) -> Any:
            if self.refusing:
                raise remote.BuildMismatch("schema", 32, 33)
            return connect(*args, **kwargs)
        patcher = mock.patch("sd_db.database.connect", hub)
        patcher.start()
        self.addCleanup(patcher.stop)
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")

    def refused(self, step: str) -> dict:
        """`sd-ship`'s answer when the hub refused its build in `step`, as its `main` prints it."""
        from sd_db import remote
        return sd_lane.sd_ship_workflow.failure(step, remote.BuildMismatch("schema", 32, 33))

    def upgrade_after(self, verb: str, answer: dict | None = None) -> Any:
        """An `sd-ship` whose `verb` answers `answer`, else the recorder's; the hub refuses this build from then."""
        def ship(argv: list[str], log: pathlib.Path) -> dict:
            answered = self.ship(argv, log)
            if argv[2] != verb:
                return answered
            self.refusing.append(True)
            return answer if answer is not None else answered
        return ship

    def follow(self) -> None:
        """`repo-sync.sh follow`: this machine loads the hub's build, and the runner that stopped is gone."""
        self.refusing.clear()
        self.make_dead()

    def records(self) -> list[pathlib.Path]:
        return sorted((self.store().lane / "queue").glob("hub-stop-*.json"))


class HubSkew(RefusingHub):
    """sd:4184. The hub's `sd_db` moved past this machine's mid-run five times on 2026-10-14.

    `sd-ship` answered `hub_unavailable`, but the runner's own write that settles the entry met the same
    refusal, so the entry stayed `running` under a runner that had stopped. The next run reclaimed it as
    dead: eight prepares spent a reclaim, and a merge failed. One test per failure-table row.
    """

    def test_a_prepare_that_meets_the_upgrade_goes_back_to_pending_with_a_hub_retry(self) -> None:
        """Failure table, put-back after prepare: sd:3695 and seven more spent a reclaim as a dead runner's."""
        answer = sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("prepare", self.refused("prepare")))
        self.assertIn("BuildMismatch", answer["stopped"])
        self.follow()
        self.calls.clear()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(answer["reclaimed"], [{"item": 1, "runner_pid": mock.ANY, "step": "prepare",
                                                "status": "pending", "hub_stop": True}])
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry["hub_retries"], entry["hub_fault"]["step"], entry.get("reclaims")),
                         ("merged", 1, "prepare", None))
        self.assertIn("build mismatch", entry["hub_fault"]["reason"])
        self.assertEqual(([c[2] for c in self.calls], self.records()), (["prepare", "merge"], []))

    def test_a_merge_that_meets_the_upgrade_goes_back_to_pending_and_is_not_failed(self) -> None:
        """Failure table, put-back after merge: sd:4019 met schema 32 against 33 at 08:10 MDT; reclaim failed it."""
        sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("merge", self.refused("merge")))
        self.follow()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([(row["step"], row["status"]) for row in answer["reclaimed"]], [("merge", "pending")])
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry["hub_retries"], entry["hub_fault"]["step"]), ("merged", 1, "merge"))
        self.assertEqual([c[2] for c in self.calls], ["prepare", "merge", "prepare", "merge"])

    def test_a_merge_whose_outcome_the_hub_refused_is_merged_by_the_next_run(self) -> None:
        """Failure table, finish: the merge landed, then the outcome write was refused; reclaim failed it."""
        answer = sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("merge"))
        self.assertEqual(answer["ran"][0]["recorded"], False)
        self.follow()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([row["status"] for row in answer["reclaimed"]], ["merged"])
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry["merge_commit"], len(self.calls), entry.get("reclaims")),
                         ("merged", "merged-1", 2, None))

    def test_a_before_merge_write_the_hub_refused_puts_the_entry_back_with_a_hub_retry(self) -> None:
        """Failure table, before merge: prepare reached ready_to_send, and `step: merge` was refused."""
        sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("prepare"))
        self.follow()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry["hub_retries"], entry.get("reclaims")), ("merged", 1, None))
        self.assertEqual([c[2] for c in self.calls], ["prepare", "prepare", "merge"])

    def test_a_renew_the_hub_refused_puts_the_entry_back_with_a_hub_retry(self) -> None:
        """Failure table, renew: the review was incomplete, and the lease before its retry was refused."""
        incomplete = {"ok": False, "error": "incomplete", "workflow": {"blocker": {"code": "review_incomplete"}}}
        sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("prepare", incomplete))
        self.follow()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry["hub_retries"], entry.get("reclaims")), ("merged", 1, None))

    def test_an_upgrade_right_after_the_claim_raises_nothing_and_loses_nothing(self) -> None:
        """Failure table, claim to prepare: the speculation's queue read met the refusal, and the run raised."""
        sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")
        claim = sd_lane.claim_entry

        def then_upgraded(*args: Any, **kwargs: Any) -> Any:
            claimed = claim(*args, **kwargs)
            self.refusing.append(True)
            return claimed
        self.answers[(1, "prepare")] = self.refused("prepare")
        with mock.patch.object(sd_lane, "claim_entry", then_upgraded):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertIn("BuildMismatch", answer["stopped"])
        del self.answers[(1, "prepare")]
        self.follow()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([(row["item"], row["status"], row.get("hub_retries")) for row in self.entries()],
                         [(1, "merged", 1), (2, "merged", None)])

    def dropped(self, connection: Any) -> Any:
        """`connection` whose next read meets a dropped session, as `sd_db.ship`'s host read sees it."""
        from sd_db import remote

        class Dropped:
            def execute(self, *args: Any) -> Any:
                raise remote.TransactionLost("r-1")

            def __getattr__(self, name: str) -> Any:
                return getattr(connection, name)
        return Dropped()

    def test_a_session_drop_in_the_before_merge_host_read_is_a_hub_fault(self) -> None:
        """Failure table, before merge: a dropped session read as `lane_unknown`, and reclaim counted a death.

        The drop goes through `sd_db.ship`'s own host read, which wraps it in `LaneUnknown`.
        """
        dropping: list[bool] = []
        real = sd_lane.sd_lib.lane_elsewhere

        def lane_elsewhere(connection: Any, *args: Any) -> Any:
            return real(self.dropped(connection) if dropping else connection, *args)

        def prepared_then_dropped(argv: list[str], log: pathlib.Path) -> dict:
            dropping.append(True)
            return self.ship(argv, log)
        with mock.patch.object(sd_lane.sd_lib, "lane_elsewhere", lane_elsewhere):
            answer = sd_lane.run_lane(self.repo, self.environ, prepared_then_dropped)
        self.assertIn("did not commit", answer["stopped"])
        self.make_dead()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((answer["reclaimed"][0]["status"], entry["status"], entry.get("hub_retries"),
                          entry.get("reclaims")), ("pending", "merged", 1, None))

    def test_a_claim_that_landed_unseen_then_met_a_drop_in_its_host_read_is_retried(self) -> None:
        """Failure table, claim: the retry's host read met a dropped session; `lane_unknown` left it running."""
        real_hub, real_read = sd_lane.on_hub, sd_lane.sd_lib.lane_elsewhere
        lost, drops = [True], [True]

        def claim_lands_answer_lost(queue: sd_lane.Queue, work: Any, **options: Any) -> Any:
            answer = real_hub(queue, work, **options)
            if lost and any(body["status"] == "running" for _, body in self.rows()):
                lost.clear()
                raise sd_lane.LaneError("the session dropped", code=sd_lane.HUB_UNAVAILABLE)
            return answer

        def lane_elsewhere(connection: Any, *args: Any) -> Any:
            if not lost and drops:
                drops.clear()
                return real_read(self.dropped(connection), *args)
            return real_read(connection, *args)
        with mock.patch.object(sd_lane, "on_hub", claim_lands_answer_lost), \
                mock.patch.object(sd_lane.sd_lib, "lane_elsewhere", lane_elsewhere), \
                mock.patch.object(sd_lane, "WRITE_RETRY_SECONDS", 600):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((answer.get("stopped"), drops, entry["status"], entry.get("reclaims")),
                         (None, [], "merged", None))

    def test_a_merge_authority_read_the_hub_refused_puts_the_entry_back(self) -> None:
        """Failure table, authority: `repo.runner_merge` met the refusal, and the entry stopped `prepared`."""
        sd_lane.update(self.store(), lambda rows: rows[0].pop("authority", None))
        with mock.patch.object(sd_lane, "default_runner_merge", self.authority), \
                mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            answer = sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("prepare"))
        self.assertIn("BuildMismatch", answer["stopped"])
        self.follow()
        with mock.patch.object(sd_lane, "default_runner_merge", lambda root: "auto"):
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry["hub_retries"], entry["hub_fault"]["step"], entry.get("reclaims")),
                         ("merged", 1, "prepare", None))

    def test_a_kill_during_the_catch_up_read_after_a_hub_stop_keeps_the_record(self) -> None:
        """Failure table, record: the head lookup asks `origin`, which can take minutes; the record comes first."""
        real = sd_lane.pushed_catch_up

        def killed_once_recorded(*args: Any) -> Any:
            if self.records():
                raise Killed
            return real(*args)
        with mock.patch.object(sd_lane, "pushed_catch_up", killed_once_recorded), self.assertRaises(Killed):
            sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("prepare", self.refused("prepare")))
        self.follow()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((answer["reclaimed"][0].get("hub_stop"), entry["status"], entry["hub_retries"],
                          entry.get("reclaims")), (True, "merged", 1, None))

    def test_a_hub_stop_past_the_retries_holds_the_entry_and_release_runs_it(self) -> None:
        """Bound: the entry is never failed for a hub fault; past `HUB_RETRIES` it waits, held, for the operator."""
        sd_lane.update(self.store(), lambda rows: rows[0].update(hub_retries=sd_lane.HUB_RETRIES))
        sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("merge", self.refused("merge")))
        self.follow()
        self.calls.clear()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((answer["reclaimed"][0]["status"], entry["status"], entry.get("held"), entry["hub_retries"],
                          self.calls), ("pending", "pending", True, sd_lane.HUB_RETRIES + 1, []))
        sd_lane.set_hold(self.repo, 1, self.environ, held=False)
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[0]["status"], "merged")

    def test_a_run_before_the_follow_keeps_the_record_for_the_run_after(self) -> None:
        """Failure table, reclaim: the next run starts before `follow`, and the hub refuses its reclaim too."""
        sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("prepare", self.refused("prepare")))
        self.follow()
        self.refusing.append(True)  # the runner died; this machine has not followed yet
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((refused.exception.code, len(self.records())), (sd_lane.HUB_UNAVAILABLE, 1))
        self.refusing.clear()
        host = sd_lane.refuse_elsewhere

        def then_upgraded(*args: Any) -> Any:
            host(*args)
            self.refusing.append(True)  # the hub moves between the host read and the reclaim
        with mock.patch.object(sd_lane, "refuse_elsewhere", then_upgraded), self.assertRaises(sd_lane.Stop) as stopped:
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((stopped.exception.code, len(self.records())), (sd_lane.HUB_UNAVAILABLE, 1))
        self.follow()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry["hub_retries"], entry.get("reclaims"), self.records()),
                         ("merged", 1, None, []))

    def test_a_kill_after_the_reclaim_write_leaves_a_record_the_next_run_drops(self) -> None:
        """Failure table, record removal: the reclaim landed and the run died before the record went."""
        sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("prepare", self.refused("prepare")))
        self.follow()
        [record] = self.records()
        with mock.patch.object(sd_lane, "drop_hub_stops", side_effect=Killed), self.assertRaises(Killed):
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.entries()[0]["status"], self.entries()[0]["hub_retries"], record.exists()),
                         ("pending", 1, True))
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry["hub_retries"], record.exists()), ("merged", 1, False))

    def test_a_record_of_a_claim_that_holds_nothing_changes_nothing_and_goes(self) -> None:
        """Failure table, record removal: a record whose claim no running entry holds is old state."""
        path = self.store().lane / "queue" / "hub-stop-gone.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"token": "gone", "outcome": {"status": "merged"}}), encoding="utf-8")
        before = self.rows()
        self.assertEqual(sd_lane.settle_checkout(self.repo, self.store()), {})
        self.assertEqual((self.rows(), path.exists()), (before, False))

    def test_a_record_that_cannot_be_written_leaves_the_reclaim_of_a_dead_runner(self) -> None:
        """Failure table, record: the lane folder refused the file; the next run reclaims as before, and says why."""
        queue_dir = self.store().lane / "queue"

        def read_only_after_prepare(argv: list[str], log: pathlib.Path) -> dict:
            queue_dir.chmod(0o555)
            return self.upgrade_after("prepare", self.refused("prepare"))(argv, log)
        try:
            answer = sd_lane.run_lane(self.repo, self.environ, read_only_after_prepare)
        finally:
            queue_dir.chmod(0o755)
        self.assertIn("could not be written", answer["stopped"])
        self.follow()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((answer["reclaimed"][0].get("hub_stop"), self.entries()[0]["reclaims"]), (None, 1))

    def test_an_unreadable_record_leaves_the_reclaim_of_a_dead_runner_and_goes(self) -> None:
        """Failure table, record: a record that does not parse is unknown, so the entry is reclaimed as before."""
        sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("prepare", self.refused("prepare")))
        self.follow()
        [record] = self.records()
        record.write_text("{", encoding="utf-8")
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((answer["reclaimed"][0].get("hub_stop"), self.entries()[0].get("reclaims"), record.exists()),
                         (None, 1, False))


class PostMergeOwed(RefusingHub):
    """sd:4630. `post_merge` read only its own run's merged rows, so a run killed between a merge and its
    `make post-merge` never ran the target for that merge, and no later run did. One test per failure-table row.
    """

    def setUp(self) -> None:
        super().setUp()
        git(self.repo, "switch", "-q", "--detach")  # a deploying checkout is pinned (sd:3647)
        (self.repo / "Makefile").write_text("post-merge:\n\t@echo ran >> post-merge.out\n", encoding="utf-8")

    def runs(self) -> int:
        """How many times the target ran."""
        out = self.repo / "post-merge.out"
        return len(out.read_text(encoding="utf-8").splitlines()) if out.exists() else 0

    def noted(self) -> list[int]:
        return [item for item, body, _ in self.notes if body.startswith("Post-merge: ")]

    def killed_before_the_target(self, ship: Any = None) -> None:
        """A run that merges, then dies before `post_merge`: the last step `run_lane` takes before it."""
        with mock.patch.object(sd_lane, "finish_speculation", side_effect=Killed), self.assertRaises(Killed):
            sd_lane.run_lane(self.repo, self.environ, ship or self.ship)

    def claim_dead(self) -> None:
        """The run that claimed the target died: its claim names a pid that is gone."""
        runner = subprocess.Popen([sys.executable, "-c", ""])
        runner.wait()
        sd_lane.update(self.store(), lambda rows: [row["post_merge"].update(pid=runner.pid) for row in rows
                                                   if isinstance(row.get("post_merge"), dict)])

    def test_a_kill_after_the_merge_runs_the_target_in_the_next_run_and_only_there(self) -> None:
        """Failure table, between the outcome write and the target: the merge was recorded, the target lost."""
        self.killed_before_the_target()
        self.assertEqual((self.entries()[0]["status"], self.runs()), ("merged", 0))
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((answer.get("post_merge"), self.runs(), self.noted()),
                         (f"make post-merge passed in {self.repo}", 1, [1]))
        self.assertEqual(self.entries()[0]["post_merge"]["ran"], f"make post-merge passed in {self.repo}")
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(("post_merge" in answer, self.runs(), self.noted()), (False, 1, [1]))

    def test_a_kill_in_the_landing_leaves_the_target_owed_on_the_reclaimed_entry(self) -> None:
        """Failure table, landing: the merge landed, the outcome write never came; reclaim fails the entry."""
        with mock.patch.object(sd_lane, "land", side_effect=Killed), self.assertRaises(Killed):
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.make_dead()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((answer["reclaimed"][0]["step"], entry["status"], self.runs(), self.noted()),
                         ("merge", "failed", 1, [1]))

    def test_a_kill_in_the_target_runs_it_again_once_its_runner_is_gone_and_not_before(self) -> None:
        """Failure table, target: the claim's runner died mid-target; a live claim is another run's, left alone."""
        with mock.patch.object(sd_lane, "make_post_merge", side_effect=Killed), self.assertRaises(Killed):
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        sd_lane.run_lane(self.repo, self.environ, self.ship)  # this process holds the claim: it is alive
        self.assertEqual(self.runs(), 0)
        self.claim_dead()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.runs(), self.noted()), (1, [1]))

    def test_a_kill_after_the_result_write_loses_only_the_notes_and_never_runs_the_target_again(self) -> None:
        """Failure table, notes: the result landed, then the run died before the notes; once, not twice."""
        def killed_at_the_post_merge_note(item: int, body: str, *args: Any) -> str:
            if body.startswith("Post-merge: "):
                raise Killed
            return self.note(item, body, *args)
        with mock.patch.object(sd_lane, "default_note", killed_at_the_post_merge_note), self.assertRaises(Killed):
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.claim_dead()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.runs(), self.noted(), self.entries()[0]["post_merge"]["ran"]),
                         (1, [], f"make post-merge passed in {self.repo}"))

    def test_a_merge_the_next_run_applies_from_a_hub_stop_record_runs_the_target_there(self) -> None:
        """Failure table, hub stop (sd:4184): the outcome write was refused, then the run died before the target."""
        self.killed_before_the_target(self.upgrade_after("merge"))
        self.follow()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(([row["status"] for row in answer["reclaimed"]], self.runs(), self.noted()),
                         (["merged"], 1, [1]))

    def test_a_hub_stop_record_an_older_version_wrote_still_owes_the_target(self) -> None:
        """Failure table, older state: a record from before the mark has a merged outcome without it."""
        self.killed_before_the_target(self.upgrade_after("merge"))
        [record] = self.records()
        written = json.loads(record.read_text(encoding="utf-8"))
        written["outcome"].pop("post_merge", None)
        record.write_text(json.dumps(written), encoding="utf-8")
        self.follow()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.entries()[0]["status"], self.runs(), self.noted()), ("merged", 1, [1]))

    def test_a_hub_that_refuses_the_claim_leaves_the_target_to_the_next_run(self) -> None:
        """Failure table, claim: the claim write met the refusal; unknown runs nothing, and the next run runs it."""
        answer = sd_lane.run_lane(self.repo, self.environ, self.upgrade_after("merge"))
        self.assertEqual((answer["ran"][0]["recorded"], self.runs()), (False, 0))
        self.assertIn("the next run", answer["post_merge"])
        self.follow()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.runs(), self.noted()), (1, [1]))

    def test_a_result_the_hub_refused_is_said_and_the_target_runs_again_after_its_runner(self) -> None:
        """Failure table, result: the target ran, its result write failed; at least once, never lost."""
        real = sd_lane.update

        def refused_after_the_target(queue: Any, change: Any, check: Any = None) -> Any:
            if self.runs():
                raise sd_lane.LaneError("the hub went away", code=sd_lane.HUB_UNAVAILABLE)
            return real(queue, change, check)
        with mock.patch.object(sd_lane, "update", refused_after_the_target):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertIn("could not be recorded", answer["post_merge"])
        self.assertEqual((self.runs(), self.noted()), (1, [1]))
        self.claim_dead()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.runs(), 2)

    def test_a_mark_write_that_failed_then_a_kill_in_the_landing_still_owes_the_target(self) -> None:
        """Failure table, mark: the hub refused the mark after the merge, and the run died in the landing."""
        real = sd_lane.update

        def refused_after_the_merge(queue: Any, change: Any, check: Any = None) -> Any:
            if self.calls and self.calls[-1][2] == "merge":
                raise sd_lane.LaneError("the hub went away", code=sd_lane.HUB_UNAVAILABLE)
            return real(queue, change, check)
        with mock.patch.object(sd_lane, "update", refused_after_the_merge), \
                mock.patch.object(sd_lane, "land", side_effect=Killed), self.assertRaises(Killed):
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.make_dead()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual((entry["status"], self.runs(), self.noted(), self.records()), ("merged", 1, [1], []))

    def test_a_target_that_could_not_start_stays_owed_and_only_its_first_try_notes(self) -> None:
        """Failure table, unknown: the Makefile or origin could not be read; that is no run, so the mark stays."""
        unknown = "make post-merge unknown: the Makefile could not be read"
        with mock.patch.object(sd_lane, "make_post_merge", return_value=unknown):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
            self.assertEqual((answer["post_merge"], self.entries()[0]["post_merge"], self.noted(), self.note_kinds[-1]),
                             (unknown, sd_lane.POST_MERGE_OWED, [1], "followup"))
            sd_lane.run_lane(self.repo, self.environ, self.ship)  # a retry that meets it again notes nothing
        self.assertEqual(self.noted(), [1])
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.runs(), self.noted()), (1, [1, 1]))

    def test_an_entry_merged_before_the_mark_never_runs_the_target(self) -> None:
        """Older state: rows an older version merged carry no mark, and a run that merges nothing runs nothing."""
        sd_lane.update(self.store(), lambda rows: rows[0].update(status="merged", finished_at=sd_lane.stamp_now()))
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(("post_merge" in answer, self.runs()), (False, 0))


class AnyHost(SharedQueue):
    """sd:3283, slice 2 of the sd:3174 design: the lane host runs any entry. One test per failure-table row."""

    def enqueued_elsewhere(self, item: int, name: str, *messages: str) -> list[str]:
        """`name`'s commits, enqueued by another host; this disk keeps neither its worktree nor its branch."""
        tree, heads = self.committed(name, *messages)
        with mock.patch("sd_db.ship.this_host", lambda: "build-2"):
            sd_lane.enqueue_entry(tree, item, f"fix: {name}", self.body, self.environ, manual=True, claim="deliver")
        git(self.repo, "worktree", "remove", "--force", str(tree))
        git(self.repo, "branch", "-D", name)
        return heads

    def made(self, item: int = 1) -> pathlib.Path:
        return self.store().lane / "worktrees" / str(item)

    def pushed_on_top(self, branch: str) -> None:
        other = self.tmp / "other"
        git(self.tmp, "clone", "-q", "-b", branch, str(self.origin), str(other))
        git(other, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "more")
        git(other, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    def test_an_entry_another_host_enqueued_runs_here_from_origin(self) -> None:
        """Failure table, claim and mixed versions: the runner makes a lane worktree from origin, then removes it."""
        [head] = self.enqueued_elsewhere(1, "topic", "work")
        seen: list[tuple[str, str]] = []

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            tree = pathlib.Path(argv[1])
            seen.append((git(tree, "rev-parse", "HEAD"), git(tree, "rev-parse", "--abbrev-ref", "@{upstream}")))
            return self.ship(argv, log)
        sd_lane.run_lane(self.repo, self.environ, ship)
        self.assertEqual([(c[2], c[1]) for c in self.calls], [("prepare", str(self.made())), ("merge", str(self.made()))])
        self.assertEqual((seen[0], self.entries()[0]["status"]), ((head, "origin/topic"), "merged"))
        self.assertEqual((self.made().exists(), git(self.repo, "branch", "--list", "topic")), (False, ""))

    def catch_ups(self, branch: str, head: str, count: int) -> list[str]:
        """`count` catch-up merges of a moving main onto `head`, each on the last, pushed to `origin/<branch>`."""
        scratch, merges = self.tmp / "scratch", []
        git(self.repo, "worktree", "add", "-q", "--detach", str(scratch), head)
        for number in range(count):
            git(self.repo, "commit", "-q", "--allow-empty", "-m", f"base {number}")
            git(self.repo, "push", "-q", "origin", "main")  # prepare merges `origin/<default>`
            git(scratch, "merge", "-q", "--no-ff", "-m", "Merge origin/main", "main")
            merges.append(git(scratch, "rev-parse", "HEAD"))
        git(scratch, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        git(self.repo, "worktree", "remove", "--force", str(scratch))
        return merges

    def foreign_merge(self, tree: pathlib.Path, edited: bool = False) -> str:
        """A merge with a catch-up's message on `tree`'s HEAD that prepare did not make; the new HEAD.

        Plain, it merges a feature branch that is not on origin's default branch. `edited`, it merges
        `origin/main` but adds a file no merge makes.
        """
        if edited:
            git(self.repo, "commit", "-q", "--allow-empty", "-m", "base moved")
            git(self.repo, "push", "-q", "origin", "main")
            git(tree, "merge", "-q", "--no-ff", "--no-commit", "main")
            (tree / "unreviewed.txt").write_text("unreviewed\n", encoding="utf-8")
            git(tree, "add", "unreviewed.txt")
            git(tree, "commit", "-q", "-m", "Merge origin/main")
        else:
            other = self.tmp / f"other-{tree.name}"
            git(self.repo, "worktree", "add", "-q", "-b", other.name, str(other), "main")
            (other / "feature.txt").write_text("unreviewed\n", encoding="utf-8")
            git(other, "add", "feature.txt")
            git(other, "commit", "-q", "-m", "an unrelated feature")
            git(tree, "merge", "-q", "--no-ff", "-m", "Merge origin/main", other.name)
        return git(tree, "rev-parse", "HEAD")

    def test_a_merge_the_lane_did_not_make_is_not_a_catch_up(self) -> None:
        """Review round 1: a merge on the queued head is a catch-up only when it is prepare's own.

        That is a clean merge of a commit on origin's default branch; an unrelated feature, or an edit inside
        the merge, is scope no review saw.
        """
        tree = self.worktree("unit")
        queued = git(tree, "rev-parse", "HEAD")
        for edited in (False, True):
            with self.subTest(edited=edited):
                git(tree, "reset", "-q", "--hard", queued)
                merge = self.foreign_merge(tree, edited)
                self.assertEqual((sd_lane.catch_up_of(tree, merge, queued), sd_lane.at_head(tree, queued),
                                  sd_lane.caught_up(tree, queued)), (False, False, queued))
        git(tree, "reset", "-q", "--hard", queued)
        [merge] = self.catch_ups("unit", queued, 1)
        git(tree, "reset", "-q", "--hard", merge)
        self.assertEqual((sd_lane.at_head(tree, queued), sd_lane.caught_up(tree, queued)), (True, merge))

    def test_a_merged_back_reviewed_catch_up_is_a_catch_up(self) -> None:
        """sd:4925: prepare merges back a reviewed catch-up the queued head lacks, then maybe the base again.

        That merge brings only the head's own history and the base, so a put-back or a retry still finds
        the entry's head. A merge back of a catch-up whose first parent the head lost (an amend) is not one.
        """
        tree = self.worktree("unit")
        start = git(tree, "rev-parse", "HEAD")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "base moved")
        git(self.repo, "push", "-q", "origin", "main")
        git(tree, "merge", "-q", "--no-ff", "-m", "Merge origin/main", "main")
        reviewed = git(tree, "rev-parse", "HEAD")
        git(tree, "reset", "-q", "--hard", start)
        git(tree, "commit", "-q", "--allow-empty", "-m", "the fix")
        queued = git(tree, "rev-parse", "HEAD")
        git(tree, "merge", "-q", "--ff", "-m", "Merge reviewed", reviewed)
        back = git(tree, "rev-parse", "HEAD")
        self.assertEqual((sd_lane.catch_up_of(tree, back, queued), sd_lane.caught_up(tree, queued)), (True, back))
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "base moved again")
        git(self.repo, "push", "-q", "origin", "main")
        git(tree, "merge", "-q", "--no-ff", "-m", "Merge origin/main", "main")
        again = git(tree, "rev-parse", "HEAD")
        self.assertTrue(sd_lane.catch_up_of(tree, again, queued))
        # A second round: that head reviewed and dropped, and merged back onto the next fix.
        git(tree, "reset", "-q", "--hard", queued)
        git(tree, "commit", "-q", "--allow-empty", "-m", "the second fix")
        requeued = git(tree, "rev-parse", "HEAD")
        git(tree, "merge", "-q", "--no-ff", "-m", "Merge reviewed", again)
        self.assertTrue(sd_lane.catch_up_of(tree, git(tree, "rev-parse", "HEAD"), requeued))
        git(tree, "reset", "-q", "--hard", start)
        git(tree, "commit", "-q", "--allow-empty", "-m", "an amended head")
        amended = git(tree, "rev-parse", "HEAD")
        git(tree, "merge", "-q", "--no-ff", "-m", "Merge origin/main", "main")
        lost = git(tree, "rev-parse", "HEAD")
        git(tree, "reset", "-q", "--hard", queued)
        git(tree, "merge", "-q", "--no-ff", "-m", "Merge reviewed", lost)
        self.assertNotEqual(amended, queued)
        self.assertFalse(sd_lane.catch_up_of(tree, git(tree, "rev-parse", "HEAD"), queued))
        # Codex round 1: a merged-back merge of a side commit is no catch-up, though the head holds that commit.
        git(tree, "reset", "-q", "--hard", start)
        git(tree, "commit", "-q", "--allow-empty", "-m", "a side commit")
        side = git(tree, "rev-parse", "HEAD")
        git(tree, "reset", "-q", "--hard", start)
        git(tree, "merge", "-q", "--no-ff", "-m", "Merge side", side)
        side_merge = git(tree, "rev-parse", "HEAD")
        git(tree, "reset", "-q", "--hard", queued)
        git(tree, "merge", "-q", "--no-ff", "-m", "Merge side again", side)
        keeps_side = git(tree, "rev-parse", "HEAD")
        git(tree, "merge", "-q", "--no-ff", "-m", "Merge reviewed", side_merge)
        self.assertFalse(sd_lane.catch_up_of(tree, git(tree, "rev-parse", "HEAD"), keeps_side))

    def test_a_foreign_merge_on_origin_skips_head_moved(self) -> None:
        """Review round 1, remote validation: origin/<branch> holds a merge the lane did not make."""
        for edited in (False, True):
            with self.subTest(edited=edited):
                item = 1 + edited
                [head] = self.enqueued_elsewhere(item, f"topic{item}", "work")
                scratch = self.tmp / f"scratch{item}"
                git(self.repo, "worktree", "add", "-q", "--detach", str(scratch), head)
                self.foreign_merge(scratch, edited)
                git(scratch, "push", "-q", "origin", f"HEAD:refs/heads/topic{item}")
                git(self.repo, "worktree", "remove", "--force", str(scratch))
                sd_lane.run_lane(self.repo, self.environ, self.ship)
                entry = self.entries()[-1]
                self.assertEqual((entry["status"], entry.get("code"), self.calls), ("skipped", "head_moved", []))

    def test_a_foreign_merge_in_the_own_worktree_is_not_run(self) -> None:
        """Review round 1, own worktree and the head check: the entry's worktree holds a merge the lane did not make."""
        for edited in (False, True):
            with self.subTest(edited=edited):
                item = 1 + edited
                tree = self.worktree(f"own{item}")
                sd_lane.enqueue_entry(tree, item, "fix: one", self.body, self.environ, manual=True, claim="deliver")
                self.foreign_merge(tree, edited)
                sd_lane.run_lane(self.repo, self.environ, self.ship)
                self.assertEqual((self.entries()[-1]["status"], self.calls), ("skipped", []))

    def test_a_put_back_never_adopts_a_foreign_merge_origin_holds(self) -> None:
        """Review round 1, put-back and the before-merge check: origin/<branch> gained a merge the lane did not make.

        A hub fault, a merge that answers `lane_elsewhere`, and a move seen by the before-merge write each put
        the entry back; `expected_head` stays the queued head.
        """
        for case in ("hub_fault", "merge_elsewhere", "moved"):
            with self.subTest(case=case):
                item = {"hub_fault": 1, "merge_elsewhere": 2, "moved": 3}[case]
                tree = self.worktree(f"back{item}")
                queued = sd_lane.enqueue_entry(tree, item, "fix: one", self.body, self.environ, manual=True,
                                               claim="deliver")["expected_head"]
                self.answers[(item, "merge")] = {"ok": False, "phase": "merge", "error": "runs on build-2",
                                                 "workflow": {"blocker": {"code": "lane_elsewhere"}}}

                def prepare(argv: list[str], log: pathlib.Path, tree: pathlib.Path = tree, case: str = case,
                            item: int = item) -> dict:
                    if argv[2] == "prepare":
                        self.foreign_merge(tree)
                        git(tree, "push", "-q", "origin", f"HEAD:refs/heads/back{item}")
                        if case == "moved":
                            self.repo_row("build-2")
                    answer = self.ship(argv, log)
                    return Runner.HUB_FAULT if case == "hub_fault" and argv[2] == "prepare" else answer
                sd_lane.run_lane(self.repo, self.environ, prepare)
                entry = self.entries()[-1]
                self.repo_row(None)
                sd_lane.cancel(self.repo, item, self.environ)  # before the check, so a failure leaves no entry behind
                self.assertEqual((entry["item"], entry["status"], entry["expected_head"]), (item, "pending", queued))

    def test_an_own_worktree_that_moved_on_its_branch_skips_head_moved(self) -> None:
        """sd:3413. The builder committed or amended after enqueue; the run never advises removing its branch."""
        for amended in (False, True):
            with self.subTest(amended=amended):
                item = 1 + amended
                tree, _ = self.committed(f"moved{item}", "work")
                sd_lane.enqueue_entry(tree, item, "fix: t", self.body, self.environ, manual=True, claim="deliver")
                git(tree, "commit", "-q", *(["--amend"] if amended else []), "--allow-empty", "-m", "more")
                sd_lane.run_lane(self.repo, self.environ, self.ship)
                entry = self.entries()[-1]
                self.assertEqual((entry["status"], entry.get("code"), self.calls), ("skipped", sd_lane.HEAD_MOVED, []))
                self.assertIn(str(tree), entry["reason"])
                self.assertFalse(self.made(item).exists())

    def test_a_failing_branch_read_of_a_moved_own_worktree_leaves_the_entry_pending(self) -> None:
        """sd:3413, unknown reads: the moved own worktree's branch read failed; not a skip, not a fall-through."""
        tree, _ = self.committed("moved", "work")
        sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, manual=True, claim="deliver")
        git(tree, "commit", "-q", "--allow-empty", "-m", "more")
        with self.failing("branch"):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], "holder" in entry, self.calls), ("pending", False, []))
        self.assertIn("not skipped", answer["stopped"])

    def test_an_entry_with_no_worktree_never_reads_the_runners_directory_as_its_own(self) -> None:
        """sd:3413, codex review: `Path("")` is the current directory, which may hold the branch at another head."""
        tree, [head] = self.committed("topic", "work")
        sd_lane.enqueue_entry(tree, 1, "fix: t", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.update(self.store(), lambda rows: rows[0].update(worktree=""))
        git(self.repo, "worktree", "remove", "--force", str(tree))
        git(self.repo, "branch", "-D", "topic")
        decoy = self.tmp / "decoy"
        git(self.tmp, "clone", "-q", "-b", "topic", str(self.origin), str(decoy))
        git(decoy, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(decoy)
        heads: list[str] = []

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            heads.append(git(pathlib.Path(argv[1]), "rev-parse", "HEAD"))
            return self.ship(argv, log)
        sd_lane.run_lane(self.repo, self.environ, ship)
        self.assertEqual((self.entries()[0]["status"], heads[:1]), ("merged", [head]))

    def test_a_retry_never_starts_from_a_foreign_merge(self) -> None:
        """Review round 1, retry: the worktree's HEAD is a merge the lane did not make; the retry never starts there.

        The runner would skip the old head too, so the retry refuses it as a
        moved head (sd:6057) and queues nothing.
        """
        tree = self.worktree("again")
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")

        def blocked(argv: list[str], log: pathlib.Path) -> dict:
            self.foreign_merge(tree)
            self.ship(argv, log)
            return {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        sd_lane.run_lane(self.repo, self.environ, blocked)
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, 1, self.environ)
        self.assertEqual((refused.exception.code, [row["status"] for row in self.entries()]),
                         (sd_lane.HEAD_MOVED, ["failed"]))

    def test_a_chain_of_pushed_catch_ups_after_two_reclaims_still_runs(self) -> None:
        """Codex review: prepare pushed a catch-up, died, was reclaimed, caught up again; both merges are its own."""
        [head] = self.enqueued_elsewhere(1, "topic", "work")
        [_, second] = self.catch_ups("topic", head, 2)
        seen: list[str] = []

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            seen.append(git(pathlib.Path(argv[1]), "rev-parse", "HEAD"))
            return self.ship(argv, log)
        sd_lane.run_lane(self.repo, self.environ, ship)
        self.assertEqual((self.entries()[0]["status"], seen[:1]), ("merged", [second]))

    def test_a_failed_read_of_a_catch_ups_parents_leaves_the_entry_pending(self) -> None:
        """Codex review: origin holds a valid catch-up, and the read of its parents fails; unknown, not head_moved."""
        [head] = self.enqueued_elsewhere(1, "topic", "work")
        [merge] = self.catch_ups("topic", head, 1)
        real_lane, real_publish = sd_lane.lane_git, sd_lane.publish_git

        def parents_read(args: tuple[str, ...]) -> bool:
            return any(merge in arg for arg in args) and args[0] in ("rev-parse", "rev-list", "cat-file", "log")

        def lane_git(worktree: pathlib.Path, *args: str) -> str | None:
            return None if parents_read(args) else real_lane(worktree, *args)

        def publish_git(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess | None:
            return None if parents_read(args) else real_publish(root, *args)
        with mock.patch.object(sd_lane, "lane_git", lane_git), mock.patch.object(sd_lane, "publish_git", publish_git):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], "holder" in entry, self.calls), ("pending", False, []))
        self.assertIn("stopped", answer)

    def test_a_lane_worktree_landing_names_the_builders_worktree_and_host(self) -> None:
        """Codex review: the builder's worktree stays on the host that enqueued it; the note says where and how."""
        self.enqueued_elsewhere(1, "topic", "work")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [(_, body, _)] = self.notes
        kept = self.tmp / "topic"
        self.assertIn(f"{kept} stays on build-2", body)
        self.assertTrue(body.endswith(f"Remove on build-2: git -C {kept} worktree remove {kept}"), body)

    def test_a_lane_worktree_is_removed_when_its_entry_fails(self) -> None:
        """Failure table, end: the runner removes the lane worktree and the branch it made, whatever the outcome."""
        self.enqueued_elsewhere(1, "topic", "work")
        self.answers[(1, "prepare")] = {"ok": False, "phase": "prepare", "error": "review blocked"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[0]["status"], "failed")
        self.assertEqual((self.made().exists(), git(self.repo, "branch", "--list", "topic")), (False, ""))

    def test_a_remote_branch_that_moved_skips_head_moved(self) -> None:
        """Failure table, worktree: the fetched head is neither `expected_head` nor its catch-up merge."""
        self.enqueued_elsewhere(1, "topic", "work")
        self.pushed_on_top("topic")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], entry.get("code"), self.calls), ("skipped", "head_moved", []))

    def test_a_deleted_remote_branch_skips_head_gone(self) -> None:
        """Failure table, worktree: `origin/<branch>` was deleted after enqueue."""
        self.enqueued_elsewhere(1, "topic", "work")
        git(self.origin, "update-ref", "-d", "refs/heads/topic")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], entry.get("code"), self.calls), ("skipped", sd_lane.HEAD_GONE, []))
        self.assertIn("lane retry", entry["reason"])

    def test_a_failing_read_or_fetch_of_origin_leaves_the_entry_pending(self) -> None:
        """Failure table, worktree: an unknown answer is not a skip; back to pending at its place, the run stops."""
        self.enqueued_elsewhere(1, "topic", "work")
        for verb in ("ls-remote", "fetch"):
            with self.subTest(verb=verb):
                with self.failing(verb):
                    answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
                entry = self.entries()[0]
                self.assertEqual((entry["status"], "holder" in entry, self.calls), ("pending", False, []))
                self.assertIn("origin", answer["stopped"])
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[0]["status"], "merged")

    def test_a_failing_local_read_leaves_the_entry_pending(self) -> None:
        """Codex class pass, unknown reads: a failed worktree list or local branch read is not a skip or a failure."""
        self.enqueued_elsewhere(1, "topic", "work")
        for verb in ("worktree", "rev-parse"):
            with self.subTest(verb=verb):
                with self.failing(verb):
                    answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
                entry = self.entries()[0]
                self.assertEqual((entry["status"], "holder" in entry, self.calls), ("pending", False, []))
                self.assertIn("not skipped", answer["stopped"])
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[0]["status"], "merged")

    def test_a_head_check_with_no_answer_before_prepare_leaves_the_entry_pending(self) -> None:
        """Codex class pass, unknown reads: the head check before prepare has no answer; no skip, no failure."""
        self.enqueued_elsewhere(1, "topic", "work")

        def unknown(tree: pathlib.Path, expected: str) -> bool:
            raise sd_lane.LaneError("git rev-parse failed; the entry is not skipped.", code=sd_lane.PUBLISH_UNKNOWN)
        with mock.patch.object(sd_lane, "at_head", unknown):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], "holder" in entry, self.calls), ("pending", False, []))
        self.assertIn("not skipped", answer["stopped"])
        self.assertFalse(self.made().exists())

    def test_a_local_branch_at_another_commit_skips_branch_busy(self) -> None:
        """Failure table, worktree: a local branch of that name at another commit, or checked out at another head."""
        for checked_out in (False, True):
            with self.subTest(checked_out=checked_out):
                item = 1 + checked_out
                self.enqueued_elsewhere(item, f"topic{item}", "work")
                if checked_out:
                    git(self.repo, "worktree", "add", "-q", "-b", f"topic{item}", str(self.tmp / "busy"), "main")
                else:
                    git(self.repo, "branch", f"topic{item}", "main")
                sd_lane.run_lane(self.repo, self.environ, self.ship)
                entry = self.entries()[-1]
                self.assertEqual((entry["status"], entry.get("code"), self.calls), ("skipped", "branch_busy", []))

    def test_a_leftover_lane_worktree_at_another_head_is_remade(self) -> None:
        """Failure table, worktree: a killed run left its lane worktree and branch at another head."""
        [head] = self.enqueued_elsewhere(1, "topic", "work")
        self.made().parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "-q", "-b", "topic", str(self.made()), "main")
        heads: list[str] = []

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            heads.append(git(pathlib.Path(argv[1]), "rev-parse", "HEAD"))
            return self.ship(argv, log)
        sd_lane.run_lane(self.repo, self.environ, ship)
        self.assertEqual((heads[:1], self.entries()[0]["status"]), ([head], "merged"))
        self.assertFalse(self.made().exists())

    def test_a_leftover_lane_worktree_at_the_head_is_reused_and_its_branch_kept(self) -> None:
        """sd:3483: a killed run's lane worktree at the head runs the entry; this run did not make its branch."""
        [head] = self.enqueued_elsewhere(1, "topic", "work")
        self.made().parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "fetch", "-q", "origin", "topic")
        git(self.repo, "worktree", "add", "-q", "-b", "topic", str(self.made()), head)
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[0]["status"], "merged")
        self.assertEqual((self.made().exists(), git(self.repo, "rev-parse", "refs/heads/topic")), (False, head))

    def test_a_leftover_lane_worktree_keeps_a_branch_origin_lacks(self) -> None:
        """sd:3483. A killed run's lane worktree went with `git branch -D`, though the lane may not have made the branch."""
        self.enqueued_elsewhere(1, "topic", "work")
        self.made().parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "-q", "-b", "topic", str(self.made()), "main")
        git(self.made(), "commit", "-q", "--allow-empty", "-m", "on no remote")
        unpushed = git(self.made(), "rev-parse", "HEAD")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], entry.get("code"), self.calls), ("skipped", sd_lane.BRANCH_BUSY, []))
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/topic"), unpushed)
        self.assertFalse(self.made().exists())

    def test_a_pending_entry_with_no_branch_fails_and_does_not_loop(self) -> None:
        """Failure table, worktree: an entry another host left with no branch has nothing to fetch; it fails once."""
        self.enqueued_elsewhere(1, "topic", "work")
        sd_lane.update(self.store(), lambda rows: rows[0].update(branch=None))
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], entry.get("code"), self.calls), ("failed", "no_branch", []))
        self.assertNotIn("stopped", answer)

    def test_a_leftover_lane_folder_git_no_longer_knows_is_replaced(self) -> None:
        """Failure table, worktree: the lane folder exists but is no registered worktree; the runner remakes it.

        Gate round 1: the stale folder is renamed aside, never deleted, so no new deletion path exists.
        """
        self.enqueued_elsewhere(1, "topic", "work")
        self.made().mkdir(parents=True)
        (self.made() / "stale.txt").write_text("left by a killed run\n", encoding="utf-8")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [aside] = self.made().parent.glob(f"{self.made().name}.stale-*")
        self.assertEqual((self.entries()[0]["status"], (aside / "stale.txt").read_text(encoding="utf-8")),
                         ("merged", "left by a killed run\n"))

    def test_an_expired_lease_on_another_host_is_no_proof_that_it_stopped(self) -> None:
        """Failure table, claim (sd:4660): a holder on another host may be asleep, not dead, once its lease passed.

        The lane host used to reclaim it at claim, so a satellite that woke could merge beside the lane host.
        Now the claim refuses and writes nothing; the sleeper's token still holds, and only a stop proof,
        here the operator's release, lets the lane host run the entry.
        """
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        entry = sd_lane.claim_entry(self.store(), "sleeper")
        sd_lane.update(self.store(), lambda rows: rows[0]["holder"].update(host="build-2")
                       or rows[0].update(lease_until="2020-01-01T00:00:00Z"))
        before = self.rows()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertNotIn("reclaimed", answer)
        self.assertIn("is no proof that build-2 stopped", answer["stopped"])
        self.assertIn("sd-ship lane cancel 1 --expected-claim sleeper:prepare", answer["stopped"])
        self.assertEqual((self.calls, self.rows()), ([], before))
        sd_lane.renew(self.store(), entry, "sleeper")  # the sleeper wakes: its claim still holds, so it alone runs
        sd_lane.cancel(self.repo, 1, self.environ, expected_claim="sleeper:prepare")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[0]["status"], "merged")

    def test_a_holder_on_another_host_is_settled_by_its_own_host_once_its_pid_is_gone(self) -> None:
        """Failure table, claim (sd:4660): the holder's own machine proves the death by its pid, and settles it."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.claim_entry(self.store(), "gone")
        runner = subprocess.Popen([sys.executable, "-c", ""])
        runner.wait()
        sd_lane.update(self.store(), lambda rows: rows[0]["holder"].update(host="build-2", pid=runner.pid))
        self.assertEqual(sd_lane.runner_write(self.store(), sd_lane.reclaim_dead), [])  # not this machine's to judge
        with mock.patch("sd_db.ship.this_host", lambda: "build-2"):
            settled = sd_lane.runner_write(self.store(), sd_lane.reclaim_dead)
        self.assertEqual(([row["status"] for row in settled], self.entries()[0]["status"]), (["pending"], "pending"))

    def test_a_move_during_prepare_puts_the_entry_back_and_the_new_host_claims_it(self) -> None:
        """Failure table, before merge: the lane moved mid-prepare; no merge here, the entry keeps its place."""
        self.repo_row()
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.enqueue_entry(self.worktree("second"), 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")

        def prepared_then_moved(argv: list[str], log: pathlib.Path) -> dict:
            answer = self.ship(argv, log)
            self.repo_row("build-2")
            return answer
        answer = sd_lane.run_lane(self.repo, self.environ, prepared_then_moved)
        self.assertEqual([c[2] for c in self.calls], ["prepare"])
        self.assertIn("runs on build-2", answer["stopped"])
        one = self.entries()[0]
        self.assertEqual((one["status"], "holder" in one, one["moved_off"]["host"]), ("pending", False, sd_lane.this_host()))
        with mock.patch("sd_db.ship.this_host", lambda: "build-2"):
            self.assertEqual((sd_lane.claim_entry(self.store(), "new-host") or {}).get("item"), 1)

    def test_a_demotion_during_prepare_puts_the_entry_back(self) -> None:
        """sd:4044, codex pass: the fence refuses before the merge; the entry keeps its place, as on a move."""
        self.repo_row()
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")

        def prepared_then_demoted(argv: list[str], log: pathlib.Path) -> dict:
            import sd_db
            from sd_db import writes
            connection = sd_db.connect(home=self.tmp)
            try:
                writes.set_setting(connection, "sd.hub_name", "hub.example.test")
            finally:
                connection.close()
            return self.ship(argv, log)
        with mock.patch("sd_db.tailnet.this_name", lambda: "old-hub.example.test"):
            answer = sd_lane.run_lane(self.repo, self.environ, prepared_then_demoted)
        self.assertEqual([c[2] for c in self.calls], ["prepare"])
        self.assertIn("runs on the hub, hub.example.test, not on this machine", answer["stopped"])
        one = self.entries()[0]
        self.assertEqual((one["status"], "holder" in one), ("pending", False))

    def test_a_lane_host_that_cannot_be_read_before_the_merge_stops_with_no_merge(self) -> None:
        """Failure table, before merge: clones that disagree on the host; no merge starts and the run stops."""
        self.repo_row()
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")

        def prepared_then_unknown(argv: list[str], log: pathlib.Path) -> dict:
            import sd_db
            connection = sd_db.connect(home=self.tmp)
            try:
                sd_db.upsert_repo(connection, str(self.tmp / "clone-2"), remote="https://github.com/example/pack.git",
                                  managed=1)
                connection.execute("UPDATE repo SET lane_host = 'build-2' WHERE path = ?", (str(self.tmp / "clone-2"),))
            finally:
                connection.close()
            return self.ship(argv, log)
        answer = sd_lane.run_lane(self.repo, self.environ, prepared_then_unknown)
        self.assertEqual([c[2] for c in self.calls], ["prepare"])
        self.assertIn("Cannot read the lane host before the merge", answer["stopped"])
        self.assertEqual((self.entries()[0]["status"], self.entries()[0]["step"]), ("running", "prepare"))

    def test_a_merge_that_answers_lane_elsewhere_puts_the_entry_back(self) -> None:
        """Failure table, merge: `sd-ship merge` refused `lane_elsewhere`; the runner treats it as a move."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "merge")] = {"ok": False, "phase": "merge", "error": "The lane for example/pack runs on build-2",
                                      "workflow": {"blocker": {"code": "lane_elsewhere"}}}
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        entry = self.entries()[0]
        self.assertEqual((entry["status"], "holder" in entry, entry["moved_off"]["host"]),
                         ("pending", False, sd_lane.this_host()))
        self.assertIn("runs on build-2", answer["stopped"])

    def test_the_ship_locks_lane_elsewhere_reaches_the_runner_as_that_code(self) -> None:
        """Failure table, merge: the lock's `LaneElsewhere` is a `WorkflowError`; its JSON names `lane_elsewhere`."""
        from sd_db.ship import LaneElsewhere
        answer = sd_lane.sd_ship_workflow.failure("merge", LaneElsewhere("example/pack", "build-2", None))
        self.assertEqual(answer["workflow"]["blocker"]["code"], "lane_elsewhere")

    def test_enqueue_off_the_lane_host_forwards_to_it(self) -> None:
        """Failure table, lane host routing: enqueue elsewhere records the entry for the lane host (sd:6884)."""
        self.repo_row("build-2")
        entry = sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        self.assertEqual((entry["status"], entry["forwarded_to"]), ("pending", "build-2"))
        self.assertEqual(self.entries()[0]["forwarded_to"], "build-2")

    def test_enqueue_on_the_lane_host_forwards_nothing(self) -> None:
        self.repo_row(sd_lane.this_host())
        entry = sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        self.assertNotIn("forwarded_to", entry)

    def test_every_verb_but_run_works_off_the_lane_host(self) -> None:
        """Failure table, verbs: with the lane on another host, the queue verbs work here; only `run` refuses."""
        import argparse
        self.repo_row("build-2")
        tree, _ = self.committed("topic", "work")
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        sd_lane.update(self.store(), lambda rows: rows[0].update(status="failed"))
        parser = argparse.ArgumentParser()
        sd_lane.add_lane_verbs(parser.add_subparsers(dest="command"))
        verbs = (["enqueue", "--item", "2", "--title", "fix: t", "--body-file", str(self.body), "--deliver"], ["list"],
                 ["move", "2", "top"], ["hold", "2"], ["release", "2"], ["cancel", "2"], ["retry", "1"], ["run"])
        codes = []
        for argv in verbs:
            with contextlib.chdir(tree), mock.patch.dict(os.environ, self.environ), \
                    contextlib.redirect_stdout(io.StringIO()) as said:
                code = sd_lane.lane_main(parser.parse_args(["lane", *argv]))
            codes.append((argv[0], code, json.loads(said.getvalue()).get("code")))
        self.assertEqual(codes, [(verb[0], 0, None) for verb in verbs[:-1]] + [("run", 3, "lane_elsewhere")])


class Retrying(SharedQueue):
    def imported_blocked(self, tree: pathlib.Path, head: str) -> None:
        """Item 1, failed in a file queue at `head`, imported; `retry` takes it from there."""
        path = sd_lane.queue_path(self.repo, self.environ)
        path.parent.mkdir(parents=True)
        body = path.parent.parent / "bodies" / "1-x.md"
        body.parent.mkdir()
        body.write_text("Item: sd:1\n", encoding="utf-8")
        path.write_text(json.dumps({"entries": [{
            "worktree": str(tree), "item": 1, "expected_head": head, "title": "t", "body_file": str(body),
            "authority": "manual", "claim": "deliver", "acceptance_file": None, "status": "failed",
            "enqueued_at": "2026-10-01T00:00:00Z", "finished_at": "2026-10-01T01:00:00Z"}]}), encoding="utf-8")
        sd_lane.import_file_queue(self.repo, self.store())

    def test_retry_of_an_imported_blocked_entry_publishes_its_head(self) -> None:
        """Failure table, retry: the commit is in this checkout, not on origin; retry publishes it, then queues."""
        tree, [head] = self.committed("topic", "work")
        self.imported_blocked(tree, head)
        self.assertEqual((self.remote_tip("topic"), self.git_verbs), (None, []))  # history is not published
        retried = sd_lane.retry(self.repo, 1, self.environ)
        self.assertEqual((retried["status"], retried["branch"], self.remote_tip("topic")), ("pending", "topic", head))

    def test_retry_of_an_entry_on_the_default_branch_refuses_before_any_push(self) -> None:
        """Failure table, retry: an older version queued a main checkout; retry pushes nothing to main."""
        before = self.remote_tip("main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "unreviewed")
        self.imported_blocked(self.repo, git(self.repo, "rev-parse", "HEAD"))
        rows = self.rows()
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, 1, self.environ)
        # The local head and branch reads are not publish steps.
        remote = [verb for verb in self.git_verbs if verb not in ("rev-parse", "branch")]
        self.assertEqual((refused.exception.code, self.rows(), self.remote_tip("main"), remote),
                         (sd_lane.DEFAULT_BRANCH, rows, before, ["ls-remote"]))

    def test_retry_after_the_branch_and_worktree_went_refuses_and_writes_no_row(self) -> None:
        """Failure table, retry: the commit is gone here and on origin; `head_gone` names where it was queued."""
        tree, [head] = self.committed("topic", "work")
        self.imported_blocked(tree, head)
        git(self.repo, "worktree", "remove", "--force", str(tree))
        git(self.repo, "branch", "-D", "topic")
        git(self.repo, "reflog", "expire", "--expire=now", "--all")
        git(self.repo, "gc", "-q", "--prune=now")
        before = self.rows()
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, 1, self.environ)
        self.assertEqual(refused.exception.code, sd_lane.HEAD_GONE)
        self.assertIn(f"run the retry on {sd_lane.this_host()}", str(refused.exception))
        self.assertEqual((self.rows(), self.entries()[0]["status"]), (before, "failed"))

    def test_a_rerun_after_a_kill_between_the_retry_push_and_its_row_queues_one_entry(self) -> None:
        """Failure table, retry: killed between the push and the row write."""
        tree, [head] = self.committed("topic", "work")
        self.imported_blocked(tree, head)
        with mock.patch.object(sd_lane, "add_entry", side_effect=Killed), self.assertRaises(Killed):
            sd_lane.retry(self.repo, 1, self.environ)
        self.assertEqual(self.remote_tip("topic"), head)
        self.git_verbs.clear()
        sd_lane.retry(self.repo, 1, self.environ)
        self.assertNotIn("push", self.git_verbs)
        self.assertEqual([row["status"] for row in self.entries()], ["failed", "pending"])


class RetryCancelled(SharedQueue):
    """sd:3389. `lane retry` takes a cancelled entry, bound to the head the caller read; one test per failure-table row."""

    def cancelled(self, item: int = 1) -> tuple[pathlib.Path, str]:
        """Item `item` enqueued from its own worktree with one commit, then cancelled; the worktree and its head."""
        tree, [head] = self.committed(f"topic-{item}", "work")
        sd_lane.enqueue_entry(tree, item, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.cancel(self.repo, item, self.environ)
        self.git_verbs.clear()  # the enqueue's own publish
        return tree, head

    def test_a_cancelled_entry_joins_the_queue_last_at_its_head(self) -> None:
        """Failure table, write: the retried entry is a plain pending entry after every other one; it jumps nothing."""
        self.cancelled(1)
        for item in (2, 3):
            sd_lane.enqueue_entry(self.committed(f"topic-{item}", "work")[0], item, "fix: t", self.body, self.environ,
                                  claim="deliver")
        head = self.entries()[0]["expected_head"]
        retried = sd_lane.retry(self.repo, 1, self.environ, expected_head=head)
        self.assertEqual((retried["status"], retried["expected_head"], retried["authority"], retried["retried"]["status"]),
                         ("pending", head, "manual", "cancelled"))
        pending = [row["item"] for row in self.entries() if row["status"] == "pending"]
        self.assertEqual(pending, [2, 3, 1])
        self.assertEqual([(row["item"], row["status"]) for row in self.entries() if row["item"] == 1],
                         [(1, "cancelled"), (1, "pending")])
        self.assertNotIn("held", retried)
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([int(call[call.index("--item") + 1]) for call in self.calls if call[2] == "prepare"], [2, 3, 1])

    def test_a_cancelled_entry_needs_the_expected_head_and_a_stale_one_is_refused(self) -> None:
        """Failure table, read: without the head the caller showed, or with another one, nothing is pushed or written."""
        _, head = self.cancelled()
        before = self.rows()
        with self.assertRaisesRegex(sd_lane.LaneError, "--expected-head"):
            sd_lane.retry(self.repo, 1, self.environ)
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, 1, self.environ, expected_head="0" * 40)
        self.assertEqual(refused.exception.code, sd_lane.STALE_HEAD)
        self.assertEqual((self.rows(), [verb for verb in self.git_verbs if verb == "push"]), (before, []))

    def test_a_cancelled_entry_whose_branch_moved_on_origin_is_refused(self) -> None:
        """Failure table, read: the builder pushed past the cancelled head; a retry would ship scope nobody saw."""
        tree, head = self.cancelled()
        git(tree, "commit", "-q", "--allow-empty", "-m", "more")
        git(tree, "push", "-q", "origin", "topic-1")
        before = self.rows()
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, 1, self.environ, expected_head=head)
        self.assertEqual(refused.exception.code, sd_lane.HEAD_MOVED)
        self.assertIn(git(tree, "rev-parse", "HEAD"), str(refused.exception))
        self.assertEqual((self.rows(), self.remote_tip("topic-1"), "push" in self.git_verbs),
                         (before, git(tree, "rev-parse", "HEAD"), False))

    def test_a_cancelled_entry_whose_worktree_moved_is_refused(self) -> None:
        """Failure table, read: the builder committed past the cancelled head but has not pushed."""
        tree, head = self.cancelled()
        git(tree, "commit", "-q", "--allow-empty", "-m", "more")
        before = self.rows()
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, 1, self.environ, expected_head=head)
        self.assertEqual(refused.exception.code, sd_lane.HEAD_MOVED)
        self.assertEqual((self.rows(), self.remote_tip("topic-1")), (before, head))

    def test_a_cancelled_entry_whose_head_already_merged_is_refused(self) -> None:
        """Failure table, read: `sd-ship` merged a head that holds the cancelled one; a retry would ship it twice."""
        tree, head = self.cancelled()
        child = git(tree, "commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "prepare's catch-up")
        for merged, phase in ((head, "merged"), (child, "merged"), (child, "merge_dispatch")):
            self.merged_receipt("topic-1", 1, merged, phase)
            before = self.rows()
            with self.assertRaises(sd_lane.LaneError) as refused:
                sd_lane.retry(self.repo, 1, self.environ, expected_head=head)
            self.assertEqual(refused.exception.code, sd_lane.ALREADY_MERGED, phase)
            self.assertIn("1" * 40, str(refused.exception))
            self.assertEqual((self.rows(), "push" in self.git_verbs), (before, False))
        # A merged head this checkout cannot read is unknown, not "not merged".
        self.merged_receipt("topic-1", 1, "2" * 40)
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, 1, self.environ, expected_head=head)
        self.assertEqual(refused.exception.code, sd_lane.PUBLISH_UNKNOWN)
        # A later head on the same branch is a follow-up nobody merged yet; it is queued again.
        self.merged_receipt("topic-1", 1, head)
        git(tree, "commit", "-q", "--allow-empty", "-m", "follow-up")
        later = git(tree, "rev-parse", "HEAD")
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual(sd_lane.retry(self.repo, 1, self.environ, expected_head=later)["expected_head"], later)

    def test_a_branch_that_moves_after_the_check_is_skipped_by_the_runner(self) -> None:
        """Failure table, publish: the builder pushes between the publish and the write; the run skips it.

        A push before the publish is refused there (`tip_past`, sd:6057).
        """
        tree, head = self.cancelled()
        real = sd_lane.require_published

        def builder_pushes_after(*args: Any) -> None:
            real(*args)
            git(tree, "commit", "-q", "--allow-empty", "-m", "more")
            git(tree, "push", "-q", "origin", "topic-1")
        with mock.patch.object(sd_lane, "require_published", builder_pushes_after):
            self.assertEqual(sd_lane.retry(self.repo, 1, self.environ, expected_head=head)["expected_head"], head)
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.entries()[-1]["status"], self.entries()[-1]["code"], self.calls),
                         ("skipped", sd_lane.HEAD_MOVED, []))

    def test_a_successor_moved_above_a_cancelled_entry_is_the_one_retried(self) -> None:
        """Failure table, read: a newer entry of the item ended at a position above the cancelled one.

        The retry takes that newer entry, the item's last by enqueue order (sd:3412), never the cancelled one.
        """
        sd_lane.enqueue_entry(self.committed("topic-2", "work")[0], 2, "fix: t", self.body, self.environ, claim="deliver")
        _, head = self.cancelled(1)
        sd_lane.enqueue_entry(self.tmp / "topic-1", 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.move(self.repo, 1, "top", self.environ)
        sd_lane.update(self.store(), lambda rows: [row.update(status="failed") for row in rows
                                                   if row["item"] == 1 and row["status"] == "pending"])
        self.assertEqual([(row["item"], row["status"]) for row in self.entries() if row["item"] == 1][-1],
                         (1, "cancelled"))
        retried = sd_lane.retry(self.repo, 1, self.environ, expected_head=head)
        self.assertEqual((retried["retried"]["status"], retried["expected_head"]), ("failed", head))
        self.assertEqual([row["status"] for row in self.entries() if row["item"] == 1],
                         ["failed", "cancelled", "pending"])

    def test_a_successor_written_between_the_read_and_the_write_is_refused(self) -> None:
        """Failure table, write: the transaction finds the item's entry it read is no longer its newest."""
        _, head = self.cancelled()
        real = sd_lane.require_published

        def another_entry_lands_first(*args: Any) -> None:
            real(*args)
            sd_lane.update(self.store(), lambda rows: rows.append(
                {**rows[0], "id": "later", "position": 9, "status": "merged", "merge_commit": "merged-1"}))
        with mock.patch.object(sd_lane, "require_published", another_entry_lands_first):
            with self.assertRaises(sd_lane.LaneError) as refused:
                sd_lane.retry(self.repo, 1, self.environ, expected_head=head)
        self.assertEqual(refused.exception.code, sd_lane.SUPERSEDED)
        self.assertEqual([row["status"] for row in self.entries()], ["cancelled", "merged"])

    def test_a_failed_branch_read_is_unknown_and_writes_nothing(self) -> None:
        """Failure table, read: `ls-remote` did not answer; that is not "the branch did not move"."""
        _, head = self.cancelled()
        before = self.rows()
        with self.failing("ls-remote"), self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, 1, self.environ, expected_head=head)
        self.assertEqual((refused.exception.code, self.rows(), "push" in self.git_verbs),
                         (sd_lane.PUBLISH_UNKNOWN, before, False))

    def test_a_rerun_after_a_kill_between_the_push_and_the_row_queues_one_entry(self) -> None:
        """Failure table, publish: origin lost the branch after the cancel; killed after the push, before the row."""
        tree, head = self.cancelled()
        git(tree, "push", "-q", "origin", "--delete", "topic-1")
        with mock.patch.object(sd_lane, "add_entry", side_effect=Killed), self.assertRaises(Killed):
            sd_lane.retry(self.repo, 1, self.environ, expected_head=head)
        self.assertEqual((self.remote_tip("topic-1"), [row["status"] for row in self.entries()]), (head, ["cancelled"]))
        self.git_verbs.clear()
        sd_lane.retry(self.repo, 1, self.environ, expected_head=head)
        self.assertNotIn("push", self.git_verbs)
        self.assertEqual([row["status"] for row in self.entries()], ["cancelled", "pending"])


class RetryMoved(SharedQueue):
    """sd:6057. `lane retry` refuses a failed entry whose branch moved, as enqueue does (sd:5989).

    The lane skipped such an entry as head_moved once it reached the head of
    the queue; the retry now refuses it before anything is pushed or queued.
    """

    def failed(self) -> tuple[pathlib.Path, str]:
        """Item 1 enqueued from its own worktree with one commit, then failed in prepare."""
        tree, [head] = self.committed("topic-1", "work")
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual(self.entries()[-1]["status"], "failed")
        self.git_verbs.clear()
        return tree, head

    def test_a_failed_entry_whose_worktree_moved_is_refused(self) -> None:
        tree, head = self.failed()
        git(tree, "commit", "-q", "--allow-empty", "-m", "review fix")
        before = self.rows()
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, 1, self.environ)
        self.assertEqual(refused.exception.code, sd_lane.HEAD_MOVED)
        self.assertIn("Enqueue the new head", str(refused.exception))
        self.assertEqual((self.rows(), self.remote_tip("topic-1"), "push" in self.git_verbs), (before, head, False))

    def test_a_failed_entry_whose_branch_moved_on_origin_is_refused(self) -> None:
        tree, head = self.failed()
        git(tree, "commit", "-q", "--allow-empty", "-m", "review fix")
        git(tree, "push", "-q", "origin", "topic-1")
        git(tree, "reset", "-q", "--hard", head)  # the worktree is back at the head; origin is not
        before = self.rows()
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.retry(self.repo, 1, self.environ)
        self.assertEqual(refused.exception.code, sd_lane.HEAD_MOVED)
        self.assertEqual((self.rows(), "push" in self.git_verbs), (before, False))

    def test_a_failed_entry_at_its_head_is_queued_again(self) -> None:
        _, head = self.failed()
        self.assertEqual(sd_lane.retry(self.repo, 1, self.environ)["expected_head"], head)


class CancelMerged(SharedQueue):
    """sd:5743. `lane cancel` clears a prepared entry whose head a ship receipt shows merged; nothing else changes."""

    def prepared(self) -> str:
        """Item 1 enqueued without merge authority from its own worktree, run to `prepared`; its queued head."""
        tree, [head] = self.committed("topic-1", "work")
        sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([row["status"] for row in self.entries()], ["prepared"])
        return head

    def test_a_prepared_entry_whose_head_merged_by_hand_is_cancelled(self) -> None:
        """sd:5743: merged by a manual `sd-ship merge` on another branch than its imported entry names."""
        head = self.prepared()
        self.merged_receipt("brief-by-hand", 1, head)
        cancelled = sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual((cancelled["status"], [row["status"] for row in self.entries()]), ("cancelled", ["cancelled"]))
        self.assertIn(f"merged at {'1' * 40}", cancelled["reason"])
        self.assertIn(sd_lane.this_host(), cancelled["reason"])

    def test_a_prepared_entry_whose_catch_up_merged_is_cancelled(self) -> None:
        """A receipt at a later head that holds the queued one, such as prepare's catch-up, is the same work landed."""
        head = self.prepared()
        tree = self.tmp / "topic-1"
        git(tree, "commit", "-q", "--allow-empty", "-m", "catch-up")
        self.merged_receipt("topic-1", 1, git(tree, "rev-parse", "HEAD"))
        self.assertEqual(sd_lane.cancel(self.repo, 1, self.environ)["status"], "cancelled")
        self.assertEqual(self.entries()[0]["expected_head"], head)

    def test_a_prepared_entry_without_a_merged_receipt_holding_its_head_is_refused(self) -> None:
        """Evidence: no receipt, a dispatch only, another item's receipt, and a merge of other work each refuse."""
        head = self.prepared()
        main = git(self.repo, "rev-parse", "main")
        for receipt in ((), ("topic-1", 1, head, "merge_dispatch"), ("topic-1", 2, head), ("main", 1, main)):
            if receipt:
                self.merged_receipt(*receipt)
            before = self.rows()
            with self.assertRaises(sd_lane.LaneError) as refused:
                sd_lane.cancel(self.repo, 1, self.environ)
            self.assertIn("no merged ship receipt", str(refused.exception), receipt)
            self.assertEqual(self.rows(), before, receipt)
        # A merged head this checkout cannot read is unknown, not "not merged".
        self.merged_receipt("other", 1, "2" * 40)
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual(refused.exception.code, sd_lane.PUBLISH_UNKNOWN)
        self.assertEqual(self.entries()[0]["status"], "prepared")

    def test_a_done_items_prepared_entry_whose_head_is_absent_here_is_cancelled(self) -> None:
        """pack sd:6805: a head another machine queued, never fetched here, cannot be read; a done item's
        merged receipt is the proof. While the item is open the absent head stays unknown and refuses."""
        import sd_db
        self.prepared()
        absent = "3" * 40
        sd_lane.update(self.store(), lambda rows: rows[0].update(expected_head=absent))
        self.merged_receipt("topic-1", 1, "2" * 40)
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual(refused.exception.code, sd_lane.PUBLISH_UNKNOWN)
        connection = sd_db.connect(home=self.tmp)
        try:
            if connection.execute("SELECT 1 FROM item WHERE id = 1").fetchone():
                connection.execute("UPDATE item SET status = 'done' WHERE id = 1")
            else:
                connection.execute("INSERT INTO item(id, kind, title, status, created_at, updated_at) VALUES "
                                   "(1, 'task', 'one', 'done', '2026-10-09T00:00:00Z', '2026-10-09T00:00:00Z')")
            connection.commit()
        finally:
            connection.close()
        cancelled = sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual((cancelled["status"], [row["status"] for row in self.entries()]), ("cancelled", ["cancelled"]))
        self.assertIn(f"sd:1 is done; its receipt merged at {'1' * 40}", cancelled["reason"])

    def test_an_unreadable_receipt_proves_no_merge_and_blocks_no_other_cancel(self) -> None:
        """Failures: a receipt that cannot be read is no proof, so it refuses, and a pending cancel is as before."""
        import sd_db
        self.prepared()
        connection = sd_db.connect(home=self.tmp)
        try:
            connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', 'ship:bad', "
                               "'2026-10-22T00:00:00Z', 'not json')")
        finally:
            connection.close()
        with self.assertRaisesRegex(sd_lane.LaneError, "no merged ship receipt"):
            sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual(self.entries()[0]["status"], "prepared")
        sd_lane.enqueue_entry(self.committed("topic-2", "work")[0], 2, "fix: two", self.body, self.environ, claim="deliver")
        self.assertEqual(sd_lane.cancel(self.repo, 2, self.environ)["status"], "cancelled")



class CancelEnded(SharedQueue):
    """pack sd:5838. `lane cancel` clears an item's newest failed or skipped entries, so Landing drops the item."""

    def ended(self, *statuses: str) -> None:
        """Item 1 enqueued once per status, each entry ended at its status before the next enqueue."""
        tree, _ = self.committed("topic-1", "work")
        for index, status in enumerate(statuses):
            sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, claim="deliver")
            sd_lane.update(self.store(), lambda rows, status=status, index=index: rows[-1].update(
                status=status, reason=f"{status} {index}", finished_at="2026-10-22T00:00:00Z"))

    def landing(self) -> list[int]:
        import sd_db
        from sd_db import today
        connection = sd_db.connect(home=self.tmp, write=False)
        try:
            return [entry["id"] for entry in today.landing(connection)["entries"]]
        finally:
            connection.close()

    def test_a_failed_entry_is_cancelled_and_leaves_landing(self) -> None:
        """Every failed entry since the item's last merge goes, else the older one would show in Landing."""
        self.ended("failed", "failed")
        self.assertEqual(self.landing(), [1])
        cancelled = sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual((cancelled["status"], [row["status"] for row in self.entries()]),
                         ("cancelled", ["cancelled", "cancelled"]))
        self.assertIn(sd_lane.this_host(), cancelled["reason"])
        self.assertIn("it was failed: failed 1", cancelled["reason"])  # the failure stays readable
        self.assertEqual(self.landing(), [])

    def test_a_fault_between_two_cancelled_rows_lands_neither(self) -> None:
        """Steps: the rows are one hub transaction; a fault after the first leaves both failed, and a rerun ends it."""
        self.ended("failed", "failed")
        real, written = sd_lane.store, []

        def store(connection: Any, repository: str, entry: dict) -> None:
            if written:
                raise sqlite3.OperationalError("the hub went away")
            written.append(entry["id"])
            real(connection, repository, entry)
        with mock.patch.object(sd_lane, "store", store), self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual((refused.exception.code, len(written), [row["status"] for row in self.entries()]),
                         (sd_lane.HUB_UNAVAILABLE, 1, ["failed", "failed"]))
        sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual(self.landing(), [])

    def test_a_skipped_entry_is_cancelled(self) -> None:
        self.ended("skipped")
        self.assertEqual(sd_lane.cancel(self.repo, 1, self.environ)["status"], "cancelled")
        self.assertEqual(self.landing(), [])

    def test_a_failed_entry_under_a_cancelled_retry_is_cancelled(self) -> None:
        """A retry cancelled while pending sits above the failure; Landing skips it and shows the failure."""
        self.ended("failed", "cancelled")
        self.assertEqual(self.landing(), [1])
        cancelled = sd_lane.cancel(self.repo, 1, self.environ)
        self.assertIn("it was failed: failed 0", cancelled["reason"])
        self.assertEqual([row["status"] for row in self.entries()], ["cancelled", "cancelled"])
        self.assertEqual(self.landing(), [])

    def test_a_failed_entry_before_a_merge_or_a_prepared_one_stays(self) -> None:
        """History before the item's newest merged or prepared entry is not rewritten; prepared keeps its proof."""
        self.ended("failed", "merged", "skipped")
        sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual([row["status"] for row in self.entries()], ["failed", "merged", "cancelled"])
        self.assertEqual(self.landing(), [])

    def test_a_failed_entry_after_a_prepared_one_is_cancelled_alone(self) -> None:
        self.ended("prepared", "failed")
        sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual([row["status"] for row in self.entries()], ["prepared", "cancelled"])

    def test_an_item_with_only_merged_or_cancelled_entries_is_refused(self) -> None:
        self.ended("merged", "cancelled")
        before = self.rows()
        with self.assertRaisesRegex(sd_lane.LaneError, "no pending entry"):
            sd_lane.cancel(self.repo, 1, self.environ)
        self.assertEqual(self.rows(), before)

    def test_a_bound_cancel_of_a_failed_entry_is_refused(self) -> None:
        """`--expected-claim` names a running claim; a failed entry has none, so it refuses as before (sd:3330)."""
        self.ended("failed")
        before = self.rows()
        with self.assertRaises(sd_lane.LaneError) as stale:
            sd_lane.cancel(self.repo, 1, self.environ, expected_claim="seen:prepare")
        self.assertEqual((stale.exception.code, self.rows()), (sd_lane.STALE_CLAIM, before))


class Importing(SharedQueue):
    """The file queue an earlier version left on this host goes into the hub database once."""

    def setUp(self) -> None:
        super().setUp()
        self.file = sd_lane.queue_path(self.repo, self.environ)
        self.bodies = self.file.parent.parent / "bodies"
        self.bodies.mkdir(parents=True)

    def old(self, item: int, tree: pathlib.Path | None, *, status: str = "pending", head: str | None = None,
            body: bool = True, **extra: Any) -> dict:
        """An entry as the file queue held it, with a body copy under `bodies/` unless `body` is False."""
        copy = self.bodies / f"{item}-x.md"
        if body:
            copy.write_text(f"Item: sd:{item}\n", encoding="utf-8")
        return {"worktree": str(tree or self.tmp / "gone"), "item": item, "title": f"t{item}",
                "expected_head": head or git(tree or self.repo, "rev-parse", "HEAD"), "body_file": str(copy),
                "authority": "manual", "claim": "deliver", "acceptance_file": None, "status": status,
                "enqueued_at": f"2026-10-0{item}T00:00:00Z", **extra}

    def write_file(self, *entries: dict) -> bytes:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"entries": list(entries)}, indent=2), encoding="utf-8")
        return self.file.read_bytes()

    def imported_files(self) -> list[pathlib.Path]:
        return sorted(self.file.parent.glob("queue.json.imported-*"))

    def import_now(self) -> dict:
        return sd_lane.import_file_queue(self.repo, self.store())

    def statuses(self) -> list[tuple[int, str]]:
        return [(row["item"], row["status"]) for row in self.entries()]

    def test_a_fault_inside_the_transaction_leaves_no_row_and_the_file(self) -> None:
        """Failure table, import: killed mid-transaction; nothing written; the next pass imports."""
        self.write_file(self.old(1, self.feature), self.old(2, self.repo, status="merged"))
        writes = [1, 2]
        real = sd_lane.store

        def second_fails(connection: Any, repository: str, entry: dict) -> None:
            writes.pop(0)
            if not writes:
                raise sqlite3.OperationalError("the hub went away")
            real(connection, repository, entry)
        with mock.patch.object(sd_lane, "store", second_fails), self.assertRaises(sd_lane.LaneError):
            self.import_now()
        self.assertEqual((self.rows(), self.file.exists()), ([], True))
        self.import_now()
        self.assertEqual(self.statuses(), [(1, "pending"), (2, "merged")])

    def test_a_rerun_after_a_kill_before_the_rename_gives_one_row_per_entry(self) -> None:
        """Failure table, import: rows written, killed before the rename; the fixed ids are skipped."""
        self.write_file(self.old(1, self.feature), self.old(2, self.repo, status="failed"))
        with mock.patch.object(pathlib.Path, "rename", side_effect=Killed), self.assertRaises(Killed):
            self.import_now()
        self.assertEqual((len(self.rows()), self.file.exists()), (2, True))
        self.import_now()
        self.assertEqual((len(self.rows()), self.file.exists(), len(self.imported_files())), (2, False, 1))
        self.assertTrue(all(row["id"].startswith("import-") for row in self.entries()))

    def test_leftover_named_bodies_go_and_an_unnamed_body_stays(self) -> None:
        """Failure table, import: killed before the named body files go; the next pass deletes only those."""
        self.write_file(self.old(1, self.feature), self.old(2, self.repo, status="failed"))
        unnamed = self.bodies / "3-y.md"
        unnamed.write_text("an older enqueue's copy\n", encoding="utf-8")
        with mock.patch.object(sd_lane, "drop_imported_bodies", side_effect=Killed), self.assertRaises(Killed):
            self.import_now()
        self.assertEqual(sorted(path.name for path in self.bodies.iterdir()), ["1-x.md", "2-x.md", "3-y.md"])
        self.import_now()
        self.assertEqual([path.name for path in self.bodies.iterdir()], ["3-y.md"])

    def test_a_body_an_older_enqueue_copied_before_its_queue_write_survives_and_imports(self) -> None:
        """Failure table, import: an older `sd-ship` copied a body, then appended a new file after the rename."""
        self.write_file(self.old(1, self.feature))
        late = self.old(2, self.worktree("second"))  # its body copy lands before its queue write
        self.import_now()
        self.assertTrue(pathlib.Path(late["body_file"]).is_file())
        self.write_file(late)  # the old flock ordered it after the import: a new file
        self.import_now()
        self.import_now()  # once
        self.assertEqual(self.statuses(), [(1, "pending"), (2, "pending")])
        self.assertEqual((self.entries()[1]["body"], pathlib.Path(late["body_file"]).exists()), ("Item: sd:2\n", False))
        self.assertEqual(len(self.imported_files()), 2)

    def test_a_failing_import_push_leaves_the_file_and_writes_no_row(self) -> None:
        """Failure table, import publish: a push fails; unknown, so nothing is written and the file stays."""
        tree, [head] = self.committed("topic", "work")
        original = self.write_file(self.old(1, tree, head=head))
        with self.failing("push"):
            report = self.import_now()
        self.assertIn("sd:1", report["stopped"])
        self.assertEqual((self.rows(), self.file.read_bytes()), ([], original))

    def test_a_rerun_after_a_kill_between_import_push_and_write_gives_one_row_per_entry(self) -> None:
        """Failure table, import publish: branches pushed, killed before the transaction."""
        tree, [head] = self.committed("topic", "work")
        self.write_file(self.old(1, tree, head=head), self.old(2, self.repo, status="merged"))
        with mock.patch.object(sd_lane, "update", side_effect=Killed), self.assertRaises(Killed):
            self.import_now()
        self.assertEqual((self.remote_tip("topic"), self.rows()), (head, []))
        self.git_verbs.clear()
        self.import_now()
        self.assertNotIn("push", self.git_verbs)
        self.assertEqual(self.statuses(), [(1, "pending"), (2, "merged")])

    def test_a_pending_entry_behind_its_branch_tip_publishes_its_expected_head(self) -> None:
        """Failure table, import publish: the builder moved the branch after enqueue; origin gets the queued head."""
        tree, [queued, _tip] = self.committed("topic", "queued", "later")
        self.write_file(self.old(1, tree, head=queued))
        self.import_now()
        self.assertEqual((self.remote_tip("topic"), self.entries()[0]["branch"]), (queued, "topic"))

    def test_one_dead_pending_branch_among_live_ones_imports_skipped(self) -> None:
        """Failure table, import publish: a gone commit and a diverged remote are definite; the rest import."""
        gone, _ = self.committed("gone", "work")
        diverged, [head] = self.committed("diverged", "mine")
        git(self.repo, "push", "-q", "origin", "main:refs/heads/diverged")
        git(self.origin, "update-ref", "refs/heads/diverged",
            git(self.origin, "commit-tree", git(self.origin, "rev-parse", "main^{tree}"), "-p", "main", "-m", "theirs"))
        live, [live_head] = self.committed("live", "work")
        self.write_file(self.old(1, gone, head="0" * 40), self.old(2, diverged, head=head),
                        self.old(3, live, head=live_head))
        self.import_now()
        self.assertEqual(self.statuses(), [(1, "skipped"), (2, "skipped"), (3, "pending")])
        self.assertEqual([row.get("code") for row in self.entries()], [sd_lane.HEAD_GONE, sd_lane.BRANCH_DIVERGED, None])
        self.assertEqual((self.file.exists(), self.remote_tip("live")), (False, live_head))

    def test_an_entry_the_import_fails_notes_its_item_and_one_failed_before_does_not(self) -> None:
        """pack sd:5963: the import's own failure is new; one the file already held was the older runner's."""
        self.write_file(self.old(1, None), self.old(2, self.repo, status="failed"))
        report = self.import_now()
        self.assertEqual(self.statuses(), [(1, "failed"), (2, "failed")])
        self.assertEqual(([item for item, _, _ in self.notes], report["noted"]), ([1], [{"sd:1": "written"}]))
        self.assertIn("in the lane at", self.notes[0][1])
        self.assertIn(": no branch and no worktree to read one from", self.notes[0][1])

    def test_a_pending_entry_on_the_default_branch_imports_skipped_without_a_push(self) -> None:
        """Failure table, import publish: an older version queued a main checkout ahead of origin; nothing is pushed."""
        before = self.remote_tip("main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "unreviewed")
        live, [live_head] = self.committed("live", "work")
        self.write_file(self.old(1, self.repo), self.old(2, live, head=live_head))
        self.import_now()
        self.assertEqual(self.statuses(), [(1, "skipped"), (2, "pending")])
        self.assertEqual((self.entries()[0]["code"], self.remote_tip("main"), self.remote_tip("live")),
                         (sd_lane.DEFAULT_BRANCH, before, live_head))

    def test_a_blocked_entry_with_no_branch_or_commit_imports_without_a_push(self) -> None:
        """Failure table, import: a blocked entry's worktree and branch are gone; it is history, not published."""
        tree, [head] = self.committed("topic", "work")
        self.write_file(self.old(1, None, status="failed", head="1" * 40), self.old(2, tree, head=head))
        self.import_now()
        self.assertEqual(self.statuses(), [(1, "failed"), (2, "pending")])
        self.assertEqual((self.git_verbs.count("push"), self.entries()[0]["branch"]), (1, None))

    def test_an_append_after_the_rename_is_imported_once(self) -> None:
        """Failure table, import: an older `sd-ship` enqueues during the import; it lands in a new file."""
        self.write_file(self.old(1, self.feature))
        release = threading.Event()
        locked = threading.Event()

        def older_enqueue() -> None:
            locked.wait(10)
            with sd_lane.queue_lock(self.file):  # waits for the import, as an older `sd-ship` would
                entries = json.loads(self.file.read_text(encoding="utf-8"))["entries"] if self.file.exists() else []
                self.write_file(*entries, self.old(2, self.worktree("second")))
            release.set()
        real = sd_lane.import_locked

        def signalled(root: pathlib.Path, queue: sd_lane.Queue, path: pathlib.Path) -> dict:
            locked.set()
            time.sleep(0.3)
            return real(root, queue, path)
        appender = threading.Thread(target=older_enqueue)
        appender.start()
        with mock.patch.object(sd_lane, "import_locked", signalled):
            self.import_now()
        appender.join(30)
        self.assertTrue(release.is_set())
        self.import_now()
        self.import_now()
        self.assertEqual(self.statuses(), [(1, "pending"), (2, "pending")])

    def test_older_rows_import_by_the_migration_rules(self) -> None:
        """Failure table, import: running, no branch, no body, `handed_back` and a duplicate item, one each."""
        sd_lane.enqueue_entry(self.feature, 6, "fix: queued here", self.body, self.environ, claim="deliver")
        tree = self.worktree("topic")
        self.write_file(
            self.old(1, tree, status="running", runner_pid=4242),
            self.old(2, None),  # pending, no worktree to read a branch from
            self.old(3, None, status="failed"),  # blocked, no branch: kept
            self.old(4, tree, body=False),  # pending, its body is gone
            self.old(5, tree, status="skipped", body=False),  # blocked, no body: kept
            self.old(7, tree, status="handed_back"),
            self.old(6, tree))  # already pending in the shared queue
        self.import_now()
        rows = {row["item"]: row for row in self.entries() if row.get("code") != "duplicate_on_import"}
        self.assertEqual({item: (row["status"], row.get("code")) for item, row in rows.items()},
                         {1: ("failed", None), 2: ("failed", "no_branch"), 3: ("failed", None), 4: ("failed", "no_body"),
                          5: ("skipped", None), 6: ("pending", None), 7: ("handed_back", None)})
        self.assertIn("runner pid 4242 died", rows[1]["reason"])
        duplicate = [row for row in self.entries() if row["item"] == 6 and row["id"].startswith("import-")]
        self.assertEqual([(row["status"], row["code"]) for row in duplicate], [("cancelled", "duplicate_on_import")])
        for item, words in ((3, "lane enqueue"), (5, "--body-file")):
            with self.subTest(item=item), self.assertRaisesRegex(sd_lane.LaneError, words):
                sd_lane.retry(self.repo, item, self.environ)

    def test_the_imported_file_holds_the_original_bytes(self) -> None:
        """Failure table, rollback: an earlier pack finds no queue file; renaming it back restores the queue."""
        original = self.write_file(self.old(1, self.feature), self.old(2, self.repo, status="merged"))
        self.import_now()
        [kept] = self.imported_files()
        self.assertEqual((kept.read_bytes(), self.file.exists()), (original, False))

    def test_a_busy_runner_lock_skips_the_import(self) -> None:
        """An older runner mid-entry holds the lock and keeps its file until the next pass."""
        self.write_file(self.old(1, self.feature))
        self.store().lock_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.store().lock_file, "a", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            report = self.import_now()
        self.assertEqual((report["skipped"][:24], self.rows(), self.file.exists()), ("a runner holds the lock;", [], True))


class HubDown(SharedQueue):
    def test_every_verb_refuses_hub_unavailable_and_writes_nothing(self) -> None:
        """Failure table, hub down: an unreachable hub refuses each lane verb with `hub_unavailable`."""
        import argparse
        tree, _ = self.committed("topic", "work")
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        before = self.rows()
        self.git_verbs.clear()
        parser = argparse.ArgumentParser()
        sd_lane.add_lane_verbs(parser.add_subparsers(dest="command"))
        verbs = (["enqueue", "--item", "2", "--title", "fix: t", "--body-file", str(self.body), "--deliver"], ["list"],
                 ["retry", "1"], ["cancel", "1"], ["move", "1", "top"], ["hold", "1"], ["release", "1"], ["run"])
        for argv in verbs:
            with self.subTest(verb=argv[0]), contextlib.chdir(tree), mock.patch.dict(os.environ, self.environ), \
                    mock.patch("sd_db.database.connect", side_effect=OSError(61, "Connection refused")), \
                    contextlib.redirect_stdout(io.StringIO()) as said:
                code = sd_lane.lane_main(parser.parse_args(["lane", *argv]))
                answer = json.loads(said.getvalue())
            self.assertEqual((code, answer.get("code")), (3, sd_lane.HUB_UNAVAILABLE), answer)
        self.assertEqual((self.rows(), "push" in self.git_verbs), (before, False))


class ShipProcess(Lane):
    def test_the_answer_is_parsed_and_the_whole_output_kept(self) -> None:
        from unittest import mock

        log = self.tmp / "logs/step.log"
        with mock.patch.dict(os.environ, self.environ):
            answer = sd_lane.ship_process(["-C", str(self.repo), "lane", "list"], log, 120)
        self.assertEqual((answer["ok"], answer["entries"]), (True, []))
        self.assertTrue(log.read_text(encoding="utf-8").startswith(f"$ sd-ship -C {self.repo} lane list\n{{"))

    def test_the_timeout_ends_what_sd_ship_started(self) -> None:
        """sd:3480. The timeout killed `sd-ship` alone; its gate's children ran on and held the gate slot."""
        fake, child = self.tmp / "bin", self.tmp / "child.pid"
        fake.mkdir()
        (fake / "sd-ship").write_text(
            "import pathlib, subprocess, sys, time\n"
            "spawned = subprocess.Popen(['sleep', '60'])\n"
            f"pathlib.Path({str(child)!r}).write_text(str(spawned.pid))\n"
            "print('started', flush=True)\n"
            "time.sleep(60)\n", encoding="utf-8")
        log = self.tmp / "logs/step.log"
        with mock.patch.object(sd_lane, "BIN", fake):
            answer = sd_lane.ship_process(["prepare", "--json"], log, 2)
        pid = int(child.read_text(encoding="utf-8"))
        alive = True
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.1)
        if alive:
            os.kill(pid, signal.SIGKILL)
        self.assertFalse(alive, "sd-ship's child outlived the step's timeout")
        self.assertEqual(answer["ok"], False)
        self.assertIn("timed out after 2", log.read_text(encoding="utf-8"))
        self.assertIn("started", log.read_text(encoding="utf-8"))


class Resolution(unittest.TestCase):
    def test_a_git_that_gave_no_answer_is_reported_as_itself(self) -> None:
        """sd:2986: a satellite lane run under load reported a git timeout as 'cwd is not inside a Git repository'."""
        import argparse  # noqa: PLC0415
        out = io.StringIO()
        stalled = subprocess.TimeoutExpired(["git"], sd_lane.sd_lib.GIT_TIMEOUT_SECONDS)
        with mock.patch.object(sd_lane.sd_lib.subprocess, "run", side_effect=stalled), contextlib.redirect_stdout(out):
            code = sd_lane.lane_main(argparse.Namespace(lane_command="list"))
        error = json.loads(out.getvalue())["error"]
        self.assertEqual(code, 3)
        self.assertIn("git rev-parse --show-toplevel did not finish within", error)


class Watch(Lane):
    def test_each_gate_end_is_printed_once(self) -> None:
        log = self.root / "pack/lane/make-check.log"
        log.parent.mkdir(parents=True)
        log.write_text("run-tests: start head=abc\nrun-tests: end head=abc exit=0\n", encoding="utf-8")
        seen: set[str] = set()
        self.assertEqual(sd_lane.gate_ends(self.root, seen), ["GATE-END lane/make-check.log: run-tests: end head=abc exit=0"])
        self.assertEqual(sd_lane.gate_ends(self.root, seen), [])
        with log.open("a", encoding="utf-8") as handle:
            handle.write("make: *** [test] Error 1\n")
        self.assertEqual(sd_lane.gate_ends(self.root, seen), ["GATE-END lane/make-check.log: make: *** [test] Error 1"])

    def watch_items(self, cwd: pathlib.Path, *items: int) -> tuple[int, list[str]]:
        import argparse  # noqa: PLC0415
        parser = argparse.ArgumentParser()
        sd_lane.add_lane_verbs(parser.add_subparsers(dest="command"))
        argv = ["lane", "watch", *[word for item in items for word in ("--item", str(item))]]
        with contextlib.chdir(cwd), mock.patch.dict(os.environ, self.environ), \
                contextlib.redirect_stdout(io.StringIO()) as said:
            code = sd_lane.lane_main(parser.parse_args(argv))
        return code, said.getvalue().splitlines()

    def test_watch_item_waits_for_each_item_and_outlives_the_worktree_it_started_in(self) -> None:
        """sd:3565. A hand-run loop of `lane list` from a builder worktree broke once the merge removed it."""
        one, two = self.worktree("one"), self.worktree("two")
        sd_lane.enqueue_entry(one, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        del self.answers[(1, "prepare")]
        sd_lane.retry(self.repo, 1, self.environ)  # the failed entry is older: it ends nothing
        sd_lane.enqueue_entry(two, 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(2, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        pauses: list[int] = []

        def pause() -> None:
            pauses.append(len(pauses))
            sd_lane.run_lane(self.repo, self.environ, self.ship)
            git(self.repo, "worktree", "remove", "--force", str(one))
        with mock.patch.object(sd_lane, "watch_pause", pause):
            code, lines = self.watch_items(one, 1, 2)
        self.assertEqual((code, pauses), (1, [0]))
        self.assertEqual(lines, ["pack sd:1 merged at merged-1", "pack sd:2 failed: a blocking finding"])
        self.assertEqual(self.watch_items(self.repo, 1), (0, ["pack sd:1 merged at merged-1"]))

    def test_watch_item_skips_cancelled_entries_and_ends_an_item_with_none_at_once(self) -> None:
        sd_lane.enqueue_entry(self.feature, 3, "fix: three", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        sd_lane.enqueue_entry(self.feature, 3, "fix: three", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.cancel(self.repo, 3, self.environ)
        sd_lane.enqueue_entry(self.feature, 4, "fix: four", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.cancel(self.repo, 4, self.environ)
        with mock.patch.object(sd_lane, "watch_pause", side_effect=AssertionError("waited")):
            code, lines = self.watch_items(self.repo, 3, 4, 5)
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["pack sd:3 merged at merged-3", "pack sd:4 none: no lane entry that is not cancelled",
                                 "pack sd:5 none: no lane entry that is not cancelled"])

    def test_watch_item_reads_again_after_a_hub_fault(self) -> None:
        """A read the hub did not answer is unknown, not an end."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        on_hub, faults = sd_lane.on_hub, [sd_lane.LaneError("down", code=sd_lane.HUB_UNAVAILABLE)]

        def flaky(queue: sd_lane.Queue, work: Any, **options: Any) -> Any:
            if faults:
                raise faults.pop()
            return on_hub(queue, work, **options)
        with mock.patch.object(sd_lane, "on_hub", flaky), mock.patch.object(
                sd_lane, "watch_pause", lambda: sd_lane.run_lane(self.repo, self.environ, self.ship)):
            code, lines = self.watch_items(self.repo, 1)
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["hub unavailable: down; reading again", "pack sd:1 merged at merged-1"])


class HostedOrder(Lane):
    def test_the_lane_that_waited_longest_runs_first(self) -> None:
        """sd:3481. Lanes ran in path order; one that kept refilling held a later lane's entries for 90 minutes."""
        later = self.tmp / "zeta"
        later.mkdir()
        git(later, "init", "-q", "-b", "main")
        git(later, "remote", "add", "origin", "https://github.com/example/zeta.git")
        import sd_db
        connection = sd_db.connect(home=self.tmp)
        try:
            for path, remote in ((self.repo, "pack"), (later, "zeta")):
                sd_db.upsert_repo(connection, str(path), remote=f"https://github.com/example/{remote}.git", managed=1)
        finally:
            connection.close()
        for checkout, item, stamp in ((self.repo, 1, "2026-10-12T21:00:00Z"), (later, 2, "2026-10-12T20:35:00Z")):
            sd_lane.add_entry(sd_lane.queue_for(checkout, self.environ),
                              {"id": f"e{item}", "item": item, "status": "pending", "enqueued_at": stamp})
        order: list[pathlib.Path] = []

        def recorded(checkout: pathlib.Path, *args: Any) -> dict:
            order.append(checkout)
            return {"ran": []}
        with mock.patch.object(sd_lane, "run_lane", recorded):
            sd_lane.run_hosted(self.environ, self.ship)
        self.assertEqual(order, [later, self.repo])

    def test_a_build_mismatch_skips_the_tick_and_exits_0(self) -> None:
        """sd:6425. Between a hub refresh and the satellite's follow, the repo-table read met a
        build mismatch, exited 3, and the health check read the job as failed. The tick skips."""
        import sd_db.database
        from sd_db import remote

        def refused(*args: Any, **kwargs: Any) -> Any:
            raise remote.BuildMismatch("build", "0000aaaa", "1111bbbb")
        args = mock.Mock(lane_command="run", hosted=True)
        with mock.patch.object(sd_db.database, "connect", refused), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = sd_lane.lane_main(args)
        self.assertEqual(code, 0, out.getvalue())
        answer = json.loads(out.getvalue())
        self.assertEqual(answer["lanes"], [])
        self.assertIn("build mismatch", answer["skipped"])


def thread_lane(run: Any) -> sd_lane.StartedLane:
    """A lane run on a thread, so a suite's stub ships reach it; `run_hosted` runs a `child_lane` from the CLI."""
    row: list[dict] = []

    def work() -> None:
        try:
            row.append(run())
        except sd_lane.LaneError as error:
            row.append(sd_lane.refusal(error))
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return sd_lane.StartedLane(lambda: None if thread.is_alive() else row[0])


class HostedParallel(Lane):
    """sd:5774. `lane run --hosted` ran its lanes one after another: a 4-entry lane held every other one 80 minutes."""

    def setUp(self) -> None:
        super().setUp()
        self.zeta = self.tmp / "zeta"
        self.zeta.mkdir()
        git(self.zeta, "init", "-q", "-b", "main")
        git(self.zeta, "config", "user.email", "t@example.test")
        git(self.zeta, "config", "user.name", "t")
        git(self.zeta, "commit", "-q", "--allow-empty", "-m", "a")
        origin = self.tmp / "zeta.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        git(self.zeta, "remote", "add", "origin", "https://github.com/example/zeta.git")
        git(self.zeta, "config", f"url.{origin}.insteadOf", "https://github.com/example/zeta.git")
        git(self.zeta, "push", "-q", "origin", "main")
        self.zeta_feature = self.zeta.parent / "zeta-feature"
        git(self.zeta, "worktree", "add", "-q", "-b", "zeta-feature", str(self.zeta_feature))
        import sd_db
        connection = sd_db.connect(home=self.tmp)
        try:
            for path, remote in ((self.repo, "pack"), (self.zeta, "zeta")):
                sd_db.upsert_repo(connection, str(path), remote=f"https://github.com/example/{remote}.git", managed=1)
        finally:
            connection.close()
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        sd_lane.enqueue_entry(self.zeta_feature, 2, "fix: two", self.body, self.environ, manual=True, claim="deliver")
        for name, value in (("START_POLL_SECONDS", 0.01), ("load_stop", lambda environ: None)):
            patcher = mock.patch.object(sd_lane, name, value, create=True)  # create: the suite fails first, not its setUp
            patcher.start()
            self.addCleanup(patcher.stop)
        self.slots(free=True)

    def slots(self, *, free: bool) -> None:
        import sd_gate_slots
        found = {"slots": 1, "holders": [] if free else [{"slot": 1}], "waiters": []}
        patcher = mock.patch.object(sd_gate_slots, "machine_snapshot", lambda environ: found)
        patcher.start()
        self.addCleanup(patcher.stop)

    def threaded(self, ship: Any) -> Any:
        return lambda checkout, budget: thread_lane(
            lambda: sd_lane.run_lane(checkout, self.environ, ship, deadline=sd_lane.clock() + budget))

    def statuses(self) -> list[str]:
        return [sd_lane.read_queue(sd_lane.queue_for(root, self.environ))[0]["status"] for root in (self.repo, self.zeta)]

    def test_two_lanes_with_work_run_at_once(self) -> None:
        """Each prepare waits for the other at a barrier: one after another, the first breaks it and fails."""
        barrier = threading.Barrier(2, timeout=20)

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            if argv[2] == "prepare":
                barrier.wait()
            return self.ship(argv, log)
        answer = sd_lane.run_hosted(self.environ, ship, start=self.threaded(ship))
        self.assertEqual(self.statuses(), ["merged", "merged"], answer)
        self.assertEqual(sorted(row["path"] for row in answer["lanes"]), sorted([str(self.repo), str(self.zeta)]))

    def test_a_lane_that_gets_work_while_another_runs_starts_before_it_ends(self) -> None:
        """sd:5809. An empty lane's worker ends at once; its later entry waited for the whole run, then a slot."""
        sd_lane.cancel(self.zeta, 2, self.environ)
        late = threading.Event()
        seen: list[str] = []

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            if argv[2] == "prepare":
                seen.append(f"start {argv[4]}")
                if argv[4] == "1":
                    sd_lane.enqueue_entry(self.zeta_feature, 3, "fix: three", self.body, self.environ, manual=True,
                                          claim="deliver")
                    late.wait(60)  # bounds a hang, not a speed: a loaded gate starts item 3 late
                else:
                    late.set()
                seen.append(f"end {argv[4]}")
            return self.ship(argv, log)
        with mock.patch.object(sd_lane, "RESCAN_SECONDS", 0.01, create=True):
            answer = sd_lane.run_hosted(self.environ, ship, start=self.threaded(ship))
        self.assertEqual(seen, ["start 1", "start 3", "end 3", "end 1"], answer)
        zeta = [[ran["item"] for ran in row["ran"]] for row in answer["lanes"] if row["path"] == str(self.zeta)]
        self.assertEqual(zeta, [[], [3]], answer)

    def test_work_a_lane_had_when_its_worker_started_starts_no_second_worker(self) -> None:
        """An entry its worker left pending is no new work: the run ends once the other lane does, no restart loop."""
        ended = threading.Event()
        starts: list[pathlib.Path] = []

        def start(checkout: pathlib.Path, budget: float) -> sd_lane.StartedLane:
            starts.append(checkout)
            if checkout == self.zeta:
                return sd_lane.StartedLane(lambda: {"ran": [], "stopped": "load"})  # its entry stays pending
            return sd_lane.StartedLane(lambda: {"ran": []} if ended.is_set() else None)
        timer = threading.Timer(0.3, ended.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(sd_lane, "RESCAN_SECONDS", 0.01, create=True):
            sd_lane.run_hosted(self.environ, self.ship, start=start)
        self.assertEqual(sorted(starts), sorted([self.repo, self.zeta]))

    def test_a_lane_that_ends_after_the_last_scan_read_is_read_again(self) -> None:
        """A scan that read A running, then saw A and B end, ended the run: A's new entry waited for a later run."""
        a, b = self.tmp / "a", self.tmp / "b"
        queues: dict[pathlib.Path, frozenset[str]] = {a: frozenset(), b: frozenset()}
        starts: list[str] = []

        def start(checkout: pathlib.Path, budget: float) -> sd_lane.StartedLane:
            starts.append(checkout.name)
            if starts == ["a"]:
                return sd_lane.StartedLane(lambda: {"ran": []} if starts.count("b") == 2 else None)
            if starts == ["a", "b"]:
                queues[b] = frozenset({"x"})  # enqueued once B's first worker read its queue
            elif starts == ["a", "b", "b"]:
                queues[a] = frozenset({"y"})  # enqueued after A's worker last read its queue
            return sd_lane.StartedLane(lambda: {"ran": []})

        def pending(seen: dict[pathlib.Path, frozenset[str]]) -> dict[pathlib.Path, frozenset[str]]:
            return {lane: queues[lane] for lane, ids in seen.items() if queues[lane] - ids}
        with mock.patch.object(sd_lane, "RESCAN_SECONDS", 0.0, create=True):
            sd_lane.run_parallel([("a", a), ("b", b)], start, sd_lane.clock() + 60, lambda: None, pending)
        self.assertEqual(starts, ["a", "b", "b", "a"])

    def test_no_free_gate_slot_defers_the_next_lane_until_one_ends(self) -> None:
        """With every gate slot held, the second lane starts only once the first ended; unknown is no room too."""
        self.slots(free=False)
        seen: list[str] = []

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            if argv[2] == "prepare":
                seen.append(f"start {argv[4]}")
                time.sleep(0.3)
                seen.append(f"end {argv[4]}")
            return self.ship(argv, log)
        sd_lane.run_hosted(self.environ, ship, start=self.threaded(ship))
        self.assertEqual(seen, ["start 1", "end 1", "start 2", "end 2"])
        self.assertEqual(self.statuses(), ["merged", "merged"])
        import sd_gate_slots
        with mock.patch.object(sd_gate_slots, "machine_snapshot", lambda environ: "no gate directory"):
            self.assertIn("cannot read the gate slots", sd_lane.lane_room(self.environ))

    def test_a_lane_waiting_past_the_budget_settles_and_claims_nothing(self) -> None:
        """The budget ends while the second lane waits for room: it starts, claims nothing, and its entry stays."""
        self.slots(free=False)
        now = [0.0]

        def ship(argv: list[str], log: pathlib.Path) -> dict:
            now[0] += sd_lane.RUN_BUDGET_SECONDS + 1
            return self.ship(argv, log)
        with mock.patch.object(sd_lane, "clock", lambda: now[0]):
            answer = sd_lane.run_hosted(self.environ, ship, start=self.threaded(ship))
        self.assertEqual(self.statuses(), ["merged", "pending"])
        later = next(row for row in answer["lanes"] if row["path"] == str(self.zeta))
        self.assertIn("budget", later["stopped"])

    def test_a_busy_lane_is_skipped_and_the_other_runs(self) -> None:
        lock = sd_lane.queue_for(self.zeta, self.environ).lock_file
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            answer = sd_lane.run_hosted(self.environ, self.ship, start=self.threaded(self.ship))
        self.assertEqual(self.statuses(), ["merged", "pending"])
        zeta = [row for row in answer["lanes"] if row["path"] == str(self.zeta)]
        self.assertEqual(len(zeta), 1, "a held lock starts no second worker")
        self.assertIn("another runner holds", zeta[0]["busy"])

    def test_one_lanes_failure_leaves_the_other_merged(self) -> None:
        """A hub fault in one lane is that lane's row; the other lane still merges."""
        def start(checkout: pathlib.Path, budget: float) -> sd_lane.StartedLane:
            if checkout == self.repo:
                def down() -> dict:
                    raise sd_lane.LaneError("the hub went away", code=sd_lane.HUB_UNAVAILABLE)
                return thread_lane(down)
            return self.threaded(self.ship)(checkout, budget)
        answer = sd_lane.run_hosted(self.environ, self.ship, start=start)
        self.assertEqual(self.statuses(), ["pending", "merged"])
        failed = next(row for row in answer["lanes"] if row["path"] == str(self.repo))
        self.assertEqual((failed["error"], failed["code"]), ("the hub went away", sd_lane.HUB_UNAVAILABLE))

    def test_a_termed_parent_ends_its_children(self) -> None:
        """An interruption while lanes run stops every lane still running; a child gets TERM, then KILL."""
        stopped: list[pathlib.Path] = []

        def hanging(checkout: pathlib.Path, budget: float) -> sd_lane.StartedLane:
            def answer() -> dict | None:
                if checkout == self.zeta:
                    raise KeyboardInterrupt
                return None
            return sd_lane.StartedLane(answer, lambda: stopped.append(checkout))
        order = [("a", self.repo), ("b", self.zeta)]
        with self.assertRaises(KeyboardInterrupt):
            sd_lane.run_parallel(order, hanging, sd_lane.clock() + 60, lambda: None)
        self.assertEqual(stopped, [self.repo, self.zeta])
        child = sd_lane.child_lane(self.repo, 60, argv=[sys.executable, "-c", "import time; time.sleep(60)"])
        child.end()
        self.assertIsNotNone(child.process.returncode)

    def test_a_worker_child_runs_the_lane_and_answers_its_row(self) -> None:
        """The CLI's lane: `sd-ship -C <checkout> lane run --budget`, whose answer is the row; no answer is an error."""
        with mock.patch.dict(os.environ, self.environ):
            child = sd_lane.child_lane(self.zeta, 0)
            child.process.wait(120)
            row = child.answer()
            self.assertEqual((row["ran"], "budget" in row["stopped"]), ([], True), row)
            broken = sd_lane.child_lane(self.zeta, 0, argv=[sys.executable, "-c", "print('half')"])
            broken.process.wait(60)
        self.assertIn("exited 0 without an answer: half", broken.answer()["error"])


class Bounds(unittest.TestCase):
    def test_an_entry_started_inside_the_budget_ends_before_the_job_limit(self) -> None:
        """sd:3481. The longest entry seen took 64.3 minutes; the lane-run job TERMs its runner at 120."""
        longest_seen, job_limit = 64.3 * 60, 120 * 60
        self.assertLess(sd_lane.RUN_BUDGET_SECONDS + longest_seen + sd_lane.POST_MERGE_SECONDS, job_limit)

    def test_prepare_and_merge_bounds_cover_a_gates_slot_wait(self) -> None:
        """sd:2611: the gate under them queues for up to `GATE_SLOT_SECONDS` before its check starts."""
        for bound in (sd_lane.PREPARE_SECONDS, sd_lane.MERGE_SECONDS):
            self.assertGreaterEqual(bound, 3 * 3600 + sd_lane.sd_lib.GATE_SLOT_SECONDS)

    def test_the_slot_wait_bound_is_a_whole_second_count_inside_its_limits_at_any_magnitude(self) -> None:
        """pack sd:7412. `sd-check --slot-timeout` takes a whole number of seconds above 0; the bound never passes
        `GATE_SLOT_SECONDS`, and an unknown time left (NaN) waits the least, since the job TERMs on its own clock."""
        cap, tiny = sd_lane.sd_lib.GATE_SLOT_SECONDS, 5e-324
        cases = {(0.0, 0.0): 1, (100.0, 0.0): 100, (100.9, 0.0): 100, (0.0, 100.0): 1, (tiny, 0.0): 1,
                 (-tiny, 0.0): 1, (math.inf, 0.0): cap, (-math.inf, 0.0): 1, (math.nan, 0.0): 1, (0.0, math.nan): 1,
                 (math.inf, math.inf): 1, (math.inf, -math.inf): cap, (1e308, -1e308): cap, (-1e308, 1e308): 1,
                 (float(cap) + 0.5, 0.0): cap, (float(cap) - 0.5, 0.0): cap - 1, (2.0 ** 63, 0.0): cap,
                 (1e15 + 30.0, 1e15): 30}
        for (deadline, now), expected in cases.items():
            with self.subTest(deadline=deadline, now=now):
                bound = sd_lane.slot_wait_seconds(deadline, now)
                self.assertIs(type(bound), int)
                self.assertEqual(bound, expected)
        for exponent in range(-1074, 1024, 7):  # every binary magnitude, both signs, against two clocks
            for sign in (1.0, -1.0):
                for now in (0.0, 1e9):
                    bound = sd_lane.slot_wait_seconds(now + sign * 2.0 ** exponent, now)
                    self.assertTrue(type(bound) is int and 1 <= bound <= cap, (exponent, sign, now, bound))

    def test_a_gate_child_that_starts_after_the_cancel_is_ended_at_once(self) -> None:
        """sd:3361, the step between the cancel and the gate's `sd-check` start: the late group is ended too."""
        go, answers = threading.Event(), []

        def target() -> None:
            go.wait(10)
            answers.append(sd_lane.gate_run()([sys.executable, "-c", "import time; time.sleep(60)"],
                                              dict(os.environ), pathlib.Path.cwd(), 120))
        gate = sd_lane.SpeculativeGate(target, "late")
        gate.start()
        cancelled: list[bool] = []
        canceller = threading.Thread(target=lambda: cancelled.append(gate.cancel_gate()))
        started = time.monotonic()
        canceller.start()
        for _ in range(1000):  # bounded: a cancel that never lands fails below rather than hangs
            if gate.cancelled:
                break
            time.sleep(0.01)
        go.set()
        canceller.join(60)
        self.assertEqual(cancelled, [True])
        self.assertLess(time.monotonic() - started, sd_lane.CANCEL_SECONDS)
        self.assertNotEqual(answers[0][0], 0)

    def test_a_gate_cancelled_before_it_starts_counts_as_ended(self) -> None:
        """sd:5776: a SIGTERM can land between the runner holding the gate and its start; `join` would raise."""
        gate = sd_lane.SpeculativeGate(lambda: None, "unstarted")
        self.assertTrue(gate.cancel_gate())
        self.assertTrue(gate.cancelled)

    def test_a_gate_child_that_ignores_sigterm_is_killed(self) -> None:
        """sd:3361, the SIGKILL step: a child that ignores SIGTERM is killed one bound later."""
        child = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        answers: list[Any] = []
        gate = sd_lane.SpeculativeGate(lambda: answers.append(sd_lane.gate_run()(
            [sys.executable, "-c", child], dict(os.environ), pathlib.Path.cwd(), 120)), "deaf")
        gate.start()
        for _ in range(500):  # bounded: the group is recorded once the child starts
            if gate.group is not None:
                break
            time.sleep(0.01)
        time.sleep(0.5)  # the child has installed its handler
        started = time.monotonic()
        with mock.patch.object(sd_lane, "CANCEL_SECONDS", 1):
            self.assertTrue(gate.cancel_gate())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(answers[0][0], -signal.SIGKILL)

    def test_a_drained_run_waits_for_its_speculative_gate_only_until_its_deadline(self) -> None:
        """pack sd:7412, failure table row 4: a run that ran out of entries joined its speculative gate with no
        bound, so a gate queued behind builders held the runner lock past the job's TERM."""
        release = threading.Event()
        gate = sd_lane.SpeculativeGate(lambda: release.wait(60), "queued")
        gate.start()
        try:
            started = time.monotonic()
            with mock.patch.object(sd_lane, "CANCEL_SECONDS", 0.2):
                answer = sd_lane.finish_speculation([gate], {}, sd_lane.clock() + 0.5)
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual(answer["speculative_gate"], "still ending 0.4s after SIGKILL; the lock is freed anyway")
        finally:
            release.set()
            gate.join(10)
        done = sd_lane.SpeculativeGate(lambda: None, "done")
        done.start()
        self.assertEqual(sd_lane.finish_speculation([done], {}, sd_lane.clock() + 60), {})

    def test_a_gate_that_outlives_both_signals_is_reported_and_left(self) -> None:
        """sd:3361, the last step: a gate still running after SIGKILL does not hold the run; the answer says so."""
        release = threading.Event()
        gate = sd_lane.SpeculativeGate(lambda: release.wait(60), "stuck")
        gate.start()
        try:
            with mock.patch.object(sd_lane, "CANCEL_SECONDS", 0.2):
                answer = sd_lane.finish_speculation([gate], {"stopped": "BuildMismatch"})
            self.assertEqual(answer["speculative_gate"], "still ending 0.4s after SIGKILL; the lock is freed anyway")
        finally:
            release.set()
            gate.join(10)


class FailureNotes(SharedQueue):
    """pack sd:5963: an entry that ends failed or skipped notes each item it carries; the entry holds the state."""

    def lane_notes(self) -> list[tuple[int, str]]:
        return [(item, body) for item, body, _ in self.notes if body.startswith("Lane ")]

    def test_a_failed_prepare_notes_each_item_with_its_step_reason_and_head(self) -> None:
        head = sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver",
                                     closes=(2,))["expected_head"]
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding\nmore"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        body = f"Lane failed in prepare at {head[:12]}: a blocking finding; full text: {entry['prepare_log']}"
        self.assertEqual((self.lane_notes(), set(self.note_kinds)), ([(1, body), (2, body)], {"comment"}))
        self.assertEqual((entry["status"], entry["note"]), ("failed", {"sd:1": "written", "sd:2": "written"}))

    def test_a_failed_prepare_note_points_at_the_prepare_log(self) -> None:
        """pack sd:7133. The note's reason is cut; the log holds the whole finding, so the note names it."""
        row = {"status": "failed", "step": "prepare", "item": 1, "expected_head": "a" * 40,
               "reason": "a blocking finding", "prepare_log": "/logs/prepare-1-x.log", "log_host": "hub"}
        notes: list[str] = []
        sd_lane.note_ended(row, self.repo, lambda item, body, main, kind: notes.append(body) or "written",
                           queue=self.store())
        self.assertEqual(notes, [f"Lane failed in prepare at {'a' * 12}: a blocking finding; full text: "
                                 "/logs/prepare-1-x.log"])

    def test_a_skip_notes_its_item(self) -> None:
        tree = self.worktree("topic")
        head = sd_lane.enqueue_entry(tree, 1, "fix: one", self.body, self.environ, claim="deliver")["expected_head"]
        git(tree, "commit", "-q", "--allow-empty", "-m", "moved")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [(item, body)] = self.lane_notes()
        self.assertEqual((self.entries()[0]["status"], item), ("skipped", 1))
        self.assertTrue(body.startswith(f"Lane skipped in prepare at {head[:12]}: {tree} moved on topic"), body)

    def test_a_note_that_fails_is_reported_and_the_entry_still_fails(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}

        def broken(item: int, body: str, main: pathlib.Path, kind: str) -> str:
            raise RuntimeError("the hub refused the note")
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship, note=broken)
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry["reason"], "stopped" in answer),
                         ("failed", "a blocking finding", False))
        self.assertEqual(entry["note"], {"sd:1": "failed: RuntimeError: the hub refused the note"})

    def test_a_merge_failure_notes_its_item(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")
        self.answers[(1, "merge")] = {"ok": False, "phase": "merging", "error": "the merge was refused"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual(self.lane_notes(), [(1, "Lane failed in merge at head-1: the merge was refused; "
                                                 f"full text: {entry['merge_log']}")])

    def test_an_exception_in_the_merge_step_notes_merge(self) -> None:
        """pack sd:5968: a raise after `advance` names merge in the note and the finish record, not prepare."""
        for error in (OSError("the merge log did not write"), sd_lane.LaneError("the merge read failed")):
            with self.subTest(error=type(error).__name__):
                self.notes.clear()
                sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, manual=True, claim="deliver")

                def ship(argv: list[str], log: pathlib.Path, error: Exception = error) -> dict:
                    if argv[2] == "merge":
                        raise error
                    return self.ship(argv, log)
                sd_lane.run_lane(self.repo, self.environ, ship)
                entry = self.entries()[-1]
                [(item, body)] = self.lane_notes()
                self.assertEqual((entry["status"], entry["step"], item), ("failed", "merge", 1))
                self.assertTrue(body.startswith(f"Lane failed in merge at {entry['expected_head'][:12]}: "
                                                f"{type(error).__name__}: "), body)

    def test_a_dead_runner_at_merge_notes_its_item_once(self) -> None:
        """Steps: the runner died with the entry at merge; the reclaim fails it and notes it, and no later run does."""
        head = sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")["expected_head"]
        entry = sd_lane.claim_entry(self.store(), "dead")
        sd_lane.advance(self.store(), entry, "dead")
        self.make_dead()
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [(item, body)] = self.lane_notes()
        self.assertEqual((answer["reclaimed"][0]["status"], item), ("failed", 1))
        self.assertTrue(body.startswith(f"Lane failed in runner at {head[:12]}: runner pid "), body)
        self.assertEqual(answer["noted"], [{"sd:1": "written"}])

    def test_an_operator_release_at_merge_notes_its_item(self) -> None:
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        entry = sd_lane.claim_entry(self.store(), "stuck")
        sd_lane.advance(self.store(), entry, "stuck")
        sd_lane.update(self.store(), lambda rows: rows[0]["holder"].update(host="build-2"))
        released = sd_lane.cancel(self.repo, 1, self.environ)
        [(item, body)] = self.lane_notes()
        self.assertEqual((released["status"], released["note"], item), ("failed", {"sd:1": "written"}, 1))
        self.assertIn("the operator released it from build-2", body)

    def test_a_failure_whose_finish_write_fails_is_noted_once(self) -> None:
        """Steps: the note lands, the finish write does not; the next run applies the record and notes nothing."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        with self.failing_store(lambda entry: entry.get("status") == "failed"):
            answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((answer["ran"][0]["recorded"], len(self.lane_notes())), (False, 1))
        self.make_dead()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.entries()[0]["status"], len(self.lane_notes())), ("failed", 1))

    def as_satellite(self) -> None:
        """This machine hosts the lane and is not the hub: a hub serves its database (sd:7357)."""
        self.repo_row(sd_lane.this_host())
        patcher = mock.patch("sd_db.database.served_by", lambda target, home=None: "hub.example.test:8765",
                             create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def failing_prepare(self) -> str:
        head = sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver",
                                     closes=(2,))["expected_head"]
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        return head

    def test_a_satellite_hosted_failure_writes_an_open_followup_on_each_item(self) -> None:
        """pack sd:7357, failure table S2 lane ends failed: the lane host's sessions see it in `sd today`."""
        self.as_satellite()
        self.failing_prepare()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertEqual(([item for item, _ in self.lane_notes()], self.note_kinds), ([1, 2], ["followup", "followup"]))
        self.assertEqual((entry["status"], entry["note"]), ("failed", {"sd:1": "written", "sd:2": "written"}))

    def test_a_hub_hosted_failure_writes_a_comment(self) -> None:
        """Failure table S2 hub lane fails: NULL or this hub's own name; neither gets a followup."""
        for host in (None, sd_lane.this_host()):
            with self.subTest(host=host):
                self.notes.clear()
                self.note_kinds.clear()
                self.repo_row(host)
                row = {"status": "failed", "step": "prepare", "item": 1, "head": "a" * 40, "reason": "no"}
                self.assertEqual(sd_lane.note_ended(row, self.repo, queue=self.store()), {"sd:1": "written"})
                self.assertEqual(self.note_kinds, ["comment"])

    def test_another_machines_lane_writes_a_followup_from_the_hub(self) -> None:
        """A cancel on the hub of a satellite lane's entry: the satellite's sessions must still see it."""
        self.repo_row("build-2")
        row = {"status": "failed", "step": "merge", "item": 1, "head": "a" * 40, "reason": "released"}
        sd_lane.note_ended(row, self.repo, queue=self.store())
        self.assertEqual(self.note_kinds, ["followup"])

    def test_a_lane_host_that_does_not_read_writes_a_followup(self) -> None:
        """Failures: an unread host is unknown, not the hub's; the failure stays open rather than lost."""
        row = {"status": "failed", "step": "prepare", "item": 1, "head": "a" * 40, "reason": "no"}
        with mock.patch.object(sd_lane.sd_lib, "lane_host", side_effect=sqlite3.OperationalError("the hub went away")):
            self.assertEqual(sd_lane.note_ended(row, self.repo, queue=self.store()), {"sd:1": "written"})
        self.assertEqual(self.note_kinds, ["followup"])

    def test_a_followup_that_fails_to_write_is_named_and_the_entry_still_fails(self) -> None:
        """Failure table S2 lane ends failed, the note write fails: the entry stays failed; the answer names it."""
        self.as_satellite()
        self.failing_prepare()
        kinds: list[str] = []

        def broken(item: int, body: str, main: pathlib.Path, kind: str) -> str:
            kinds.append(kind)
            raise RuntimeError("the hub refused the note")
        sd_lane.run_lane(self.repo, self.environ, self.ship, note=broken)
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry["reason"], kinds), ("failed", "a blocking finding", ["followup"] * 2))
        self.assertEqual(entry["note"], {"sd:1": "failed: RuntimeError: the hub refused the note",
                                         "sd:2": "failed: RuntimeError: the hub refused the note"})

    def followups(self) -> tuple[list[int], dict[int, str | None]]:
        """Items 1 and 2, each with a lane followup from an earlier failed run and one another session wrote."""
        import sd_db
        items = self.register_items(2)
        connection = sd_db.connect(home=self.tmp)
        try:
            notes = [sd_db.add_note(connection, item, "followup", body) for item in items
                     for body in ("Lane failed in prepare at aaaaaaaaaaaa: a blocking finding", "Chase the vendor")]
        finally:
            connection.close()
        return items, dict.fromkeys(notes)

    def resolved(self, notes: dict[int, str | None]) -> dict[int, bool]:
        import sd_db
        connection = sd_db.connect(home=self.tmp)
        try:
            return {number: connection.execute("SELECT resolved_at FROM note WHERE id = ?",
                                               (number,)).fetchone()[0] is not None for number in notes}
        finally:
            connection.close()

    def test_a_passing_run_resolves_only_the_lanes_open_followups(self) -> None:
        """Failure table S2 session resolves: a later pass closes the lane's followups on each item it carries."""
        items, notes = self.followups()
        lane, other, lane_2, other_2 = notes
        sd_lane.enqueue_entry(self.feature, items[0], "fix: one", self.body, self.environ, claim="deliver",
                              closes=(items[1],))
        answer = sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertIn(entry["status"], sd_lane.GATE_PASSED)
        self.assertEqual(self.resolved(notes), {lane: True, other: False, lane_2: True, other_2: False})
        self.assertEqual(entry["resolved"], {f"sd:{items[0]}": f"resolved #{lane}", f"sd:{items[1]}": f"resolved #{lane_2}"})
        self.assertEqual(answer["ran"][0]["resolved"], entry["resolved"])

    def test_a_resolve_that_fails_leaves_the_followups_open_and_the_entry_passed(self) -> None:
        """Failures: the resolve write fails; the followup stays open on every machine, and the answer names it."""
        items, notes = self.followups()
        sd_lane.enqueue_entry(self.feature, items[0], "fix: one", self.body, self.environ, claim="deliver")
        import sd_db.writes
        with mock.patch.object(sd_db.writes, "resolve_note", side_effect=sqlite3.OperationalError("the hub went away")):
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        self.assertIn(entry["status"], sd_lane.GATE_PASSED)
        self.assertEqual(entry["resolved"], {f"sd:{items[0]}": "failed: OperationalError: the hub went away"})
        self.assertFalse(any(self.resolved(notes).values()))

    def test_a_failed_run_resolves_nothing(self) -> None:
        """A failure after a failure keeps the earlier followup open beside the new note."""
        items, notes = self.followups()
        sd_lane.enqueue_entry(self.feature, items[0], "fix: one", self.body, self.environ, claim="deliver")
        self.answers[(items[0], "prepare")] = {"ok": False, "phase": "review_blocked", "error": "again"}
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual((self.entries()[0]["status"], "resolved" in self.entries()[0]), ("failed", False))
        self.assertFalse(any(self.resolved(notes).values()))

    def test_a_failure_an_older_version_recorded_is_noted_when_applied(self) -> None:
        """Older state: a hub-stop record from before sd:5963 holds a failure no note was written for."""
        sd_lane.enqueue_entry(self.feature, 1, "fix: one", self.body, self.environ, claim="deliver")
        self.answers[(1, "prepare")] = {"ok": False, "phase": "review_blocked", "error": "a blocking finding"}
        with self.failing_store(lambda entry: entry.get("status") == "failed"):
            sd_lane.run_lane(self.repo, self.environ, self.ship)
        [record] = (self.store().lane / "queue").glob("hub-stop-*.json")
        older = json.loads(record.read_text(encoding="utf-8"))
        del older["outcome"]["note"]
        record.write_text(json.dumps(older), encoding="utf-8")
        self.notes.clear()
        self.make_dead()
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        self.assertEqual([item for item, _ in self.lane_notes()], [1])



#: A `dashboard.sh` whose `shots --out DIR --changed BASE` draws home in two variants, or fails with FAKE_SHOTS=fail.
FAKE_SHOTS = """#!/bin/sh
[ "$1 $2 $4" = "shots --out --changed" ] || exit 9
[ "$FAKE_SHOTS" = fail ] && { echo "shots: no Chrome found" >&2; exit 1; }
mkdir -p "$3"
for variant in dark-1440 light-375; do : > "$3/home-$variant.png"; echo "$3/home-$variant.png"; done
"""


class Shots(Lane):
    """sd:6728: the lane draws the dashboard pages a branch changed, names them in the body, records the PNGs."""

    def setUp(self) -> None:
        super().setUp()
        self.bodies: list[str] = []

    def ship(self, argv: list[str], log: pathlib.Path) -> dict:
        if argv[2] == "prepare":
            self.bodies.append(pathlib.Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8"))
        return super().ship(argv, log)

    def dashboard(self) -> None:
        script = self.feature / "dashboard" / "dashboard.sh"
        script.parent.mkdir()
        script.write_text(FAKE_SHOTS, encoding="utf-8")
        script.chmod(0o755)
        git(self.feature, "add", ".")
        git(self.feature, "commit", "-q", "-m", "dashboard")

    def run_one(self) -> dict:
        sd_lane.enqueue_entry(self.feature, 7, "fix: Topic", self.body, self.environ, claim="deliver")
        sd_lane.run_lane(self.repo, self.environ, self.ship)
        [entry] = self.entries()
        return entry

    def test_a_changed_page_is_named_in_the_body_and_its_pngs_on_the_entry(self) -> None:
        self.dashboard()
        entry = self.run_one()
        self.assertEqual(entry["status"], "prepared")
        self.assertEqual([pathlib.Path(file).name for file in entry["shots"]], ["home-dark-1440.png", "home-light-375.png"])
        self.assertTrue(all(pathlib.Path(file).is_file() for file in entry["shots"]))
        [body] = self.bodies
        self.assertTrue(body.startswith("Item: sd:1\n\n## Screenshots\n"), body)
        self.assertIn("\n- home: dark 1440, light 375\n", body)
        self.assertNotIn(str(self.tmp), body)

    def test_a_failed_capture_is_a_warning_and_prepare_still_runs(self) -> None:
        self.dashboard()
        with mock.patch.dict(os.environ, {"FAKE_SHOTS": "fail"}):
            entry = self.run_one()
        self.assertEqual((entry["status"], entry["shots_warning"], "shots" in entry),
                         ("prepared", "shots: no Chrome found", False))
        self.assertEqual(self.bodies, ["Item: sd:1\n"])

    def test_a_repository_without_the_dashboard_draws_nothing(self) -> None:
        entry = self.run_one()
        self.assertEqual(("shots" in entry, "shots_warning" in entry, self.bodies), (False, False, ["Item: sd:1\n"]))


if __name__ == "__main__":
    unittest.main()
