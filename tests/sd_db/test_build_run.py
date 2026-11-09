"""The dispatcher's tick and the supervisor of a headless builder run (pack sd:6094).

The design's failure table names most of these tests: system
`docs/work/2026-10-24-build-dispatcher/design.md`, step 2. The supervisor
runs real git in a fixture repository with a bare origin; `claude`,
`sd-review`, `sd-ship` and `sd-docs-lint` are fakes that read their next
answer from a plan file and log each call. Each fake but `claude` first
parses its argv with the real tool's own parser, so a call the tool would
refuse fails here too.
"""
import contextlib
import dataclasses
import importlib
import io
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from sd_db import (
    build,
    build_run,
    database,
    draft,
    follow,
    hub,
    operations,
    remote,
    runner,
    sessions,
    writes,
)
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.ship import this_host
from sd_db.workflow import item_state
from sd_db.writes import add_note, create_item, record_state, upsert_repo

from . import support
from .test_build import Probe as GoneProbe

BIN = Path(__file__).resolve().parents[2] / "bin"
with mock.patch("sys.path", [str(BIN), *sys.path]):
    #: The adapter, for its `cited`: the lane's own reader of what a branch delivers.
    ADAPTER = importlib.import_module("sd_operations")
    LANE = importlib.import_module("sd_lane")  # loaded now: `cited` imports it, and the lane's body reader, by name
    importlib.import_module("sd_ship_body")
    SD_LIB = importlib.import_module("sd_lib")  # the adapter's entrypoint detection, for its git fault

#: One fake for every tool: its role is its file name, its answers come from `<plans>/<role>.json`.
FAKE = textwrap.dedent('''
    import json, os, re, subprocess, sys, time
    from pathlib import Path
    role, plans = Path(sys.argv[0]).stem, Path(os.environ["FAKE_PLANS"])
    with open(plans / "calls.jsonl", "a") as log:
        log.write(json.dumps({"role": role, "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
    plan = plans / f"{role}.json"
    queue = json.loads(plan.read_text()) if plan.exists() else []
    # `gh repo view` (the visibility read) takes no plan: plans/visibility.txt answers it.
    viewing = role == "gh" and sys.argv[1:3] == ["repo", "view"]
    mode = queue.pop(0) if queue and not viewing else "default"
    plan.write_text(json.dumps(queue))
    if viewing:  # `gh repo view R --json isPrivate`: public unless plans/visibility.txt says otherwise
        said = (plans / "visibility.txt").read_text().strip() if (plans / "visibility.txt").exists() else "public"
        if said == "unread":
            sys.exit("gh: HTTP 502")
        print(json.dumps({"isPrivate": said == "private"}))
        sys.exit(0)
    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()
    def real(name):
        """The real tool's module and `sys.argv` as its own parser reads it; a parse error exits 2 as it does."""
        import argparse, importlib.machinery, importlib.util
        bin_dir = os.environ["FAKE_BIN"]
        sys.path.insert(0, bin_dir)
        loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), os.path.join(bin_dir, name))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        class Parsed(Exception):
            pass
        original = argparse.ArgumentParser.parse_args
        def parse(self, args=None, namespace=None):
            raise Parsed(original(self, args, namespace))
        argparse.ArgumentParser.parse_args = parse
        try:
            module.main(sys.argv[1:])
        except Parsed as parsed:
            return module, parsed.args[0]
        finally:
            argparse.ArgumentParser.parse_args = original
        sys.exit(f"{name} never parsed its arguments")
    def line(record):
        print(json.dumps(record), flush=True)
    if role == "claude":
        session = "session-fixture"
        if "--resume" in sys.argv:
            session = sys.argv[sys.argv.index("--resume") + 1]
        line({"type": "system", "subtype": "init", "session_id": session})
        if mode == "hang":
            time.sleep(120)
        if mode.startswith("draft"):
            # A draft run's reply (pack sd:6361): no file, no commit; `draft-bad` has no `## Spec`.
            reply = ("## Spec\\n\\nProblem: x\\n\\n## Questions\\n\\nWhich?\\nOption: A (Recommended)\\nOption: B\\n"
                     if mode != "draft-bad" else "Here is the spec you asked for.")
            final = {"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
                     "session_id": session, "result": reply, "total_cost_usd": 0.31,
                     "usage": {"input_tokens": 5, "output_tokens": 7}}
            if mode == "draft-deny":
                final["permission_denials"] = [{"tool_name": "Write", "tool_input": {"file_path": "x"}}]
            line(final)
            sys.exit(0)
        if mode == "slow":
            time.sleep(3)
        if mode == "silent":
            sys.exit(0)
        Path("work.txt").write_text(Path("work.txt").read_text() + mode + "\\n" if Path("work.txt").exists()
                                    else mode + "\\n")
        git("add", "work.txt")
        # `extra`: plans/extra.json adds files (name to hex bytes) and message lines, as a builder may write.
        extra = json.loads((plans / "extra.json").read_text()) if mode == "extra" else {}
        for name, data in extra.get("files", {}).items():
            Path(name).write_bytes(bytes.fromhex(data))
            git("add", "--", name)
        for old, new in extra.get("move", {}).items():
            git("mv", old, new)
        # One `Closes: sd:N` trailer per item the brief lists, as the brief asks; `skip` leaves out the last
        # one, as a builder that skips an item for the operator does, and `none` delivers nothing.
        prompt = sys.argv[sys.argv.index("-p") + 1]
        items = re.findall(r"^- sd:(\\d+)", prompt, re.M)
        items = [] if mode == "none" else items[:-1] if mode == "skip" else items
        trailers = "".join(f"\\nCloses: sd:{item}" for item in items)
        message = f"fix: {mode}" + "".join("\\n\\n" + text for text in extra.get("message", []))
        git("-c", "commit.gpgsign=false", "commit", "-qm", message + (f"\\n{trailers}" if items else ""))
        # `merge`: main moves `file` to `theirs`, and the builder merges it, resolving the conflict with
        # `resolved`: bytes that only the merge commit adds, as a conflict resolution does.
        merge = extra.get("merge")
        if merge:
            git("checkout", "-q", "-b", "sd-side", "origin/main")
            Path(merge["file"]).write_bytes(bytes.fromhex(merge["theirs"]))
            git("add", "--", merge["file"])
            git("-c", "commit.gpgsign=false", "commit", "-qm", "chore: main moves on")
            git("push", "-q", "origin", "sd-side:main")
            git("checkout", "-q", "-")
            git("branch", "-q", "-D", "sd-side")
            git("fetch", "-q", "origin")
            git("-c", "commit.gpgsign=false", "merge", "-q", "--no-edit", "origin/main")
            Path(merge["file"]).write_bytes(bytes.fromhex(merge["resolved"]))
            git("add", "--", merge["file"])
            git("-c", "commit.gpgsign=false", "commit", "-q", "--no-edit")
        final = {"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
                 "session_id": session, "result": f"built ({mode})", "total_cost_usd": 0.42,
                 "usage": {"input_tokens": 10, "output_tokens": 20}}
        if mode == "deny":
            final["permission_denials"] = [{"tool_name": "Bash", "tool_input": {"command": "git push"}}]
        if mode == "max_turns":
            final.update(subtype="error_max_turns", is_error=True)
        if mode == "other":
            final["session_id"] = "session-of-another-run"
        line(final)
        sys.exit(3 if mode in ("deny", "exit3") else 0)
    if role == "sd-review":
        work = sys.argv[sys.argv.index("-C") + 1]
        review, args = real("sd-review")
        assert (args.scope, args.json) == ("branch", True), args
        if mode == "cancel":
            import sqlite3
            db = sqlite3.connect(os.environ["FAKE_DB"])
            db.execute("UPDATE runner_run SET cancel_requested = 'cancelled by fixture' WHERE released_at IS NULL")
            db.commit()
            mode = "clean"
        if mode == "garbage":
            print("not json")
            sys.exit(4)
        raw = {"path": "work.txt", "line": 1, "severity": "high", "summary": "a blocker", "family": "correctness"}
        findings = review.dispose([review._finding(raw)], "high")  # the record shape `--json` prints
        if mode == "gate_failed":
            print(json.dumps({"status": "gate_failed", "cleared_status": "clean", "findings": [],
                              "subject": {"head": git("-C", work, "rev-parse", "HEAD")},
                              "check": {"status": "fail", "checks": [
                                  {"command": ["make", "check"], "exit_code": 2, "failed_steps": ["make target audit"],
                                   "failure": "audit: a long line"}]}}))
            sys.exit(0)
        if mode in ("skip_route", "skip_other"):  # pack sd:8291: a docs-only change the router skips, or not
            print(json.dumps({"status": "skipped", "completed_reviews": 0, "findings": [],
                              "route": {"tier": "skip" if mode == "skip_route" else "cheap", "category": "docs"},
                              "subject": {"head": git("-C", work, "rev-parse", "HEAD")}}))
            sys.exit(0)
        print(json.dumps({"status": "blocking" if mode == "blocking" else "clean",
                          "subject": {"head": git("-C", work, "rev-parse", "HEAD")},
                          "findings": findings if mode == "blocking" else []}))
    if role == "sd-ship":
        lane = plans / "lane.json"
        entries = json.loads(lane.read_text()) if lane.exists() else []
        _, args = real("sd-ship")
        assert args.command == "lane", args
        if "list" in sys.argv:
            print(json.dumps({"ok": mode != "unread", "entries": entries}))
            sys.exit(3 if mode == "unread" else 0)
        import sd_lane
        if args.claim not in sd_lane.CLAIMS:  # enqueue_entry's first refusal, which the parser leaves to it
            print(json.dumps({"ok": False, "error": "name the delivery claim prepare needs"}))
            sys.exit(3)
        import sd_ship_body, sd_ship_remote
        try:  # the lane's own title and line checks (`check_lines`, `check_item`), with no commits read
            body = args.body_file.read_text()
            sd_ship_body.refuse_long_lines(args.title, body, [])
            sd_ship_body.refuse_unconventional_title(args.title)
            sd_ship_body.normalize(body, args.items[0])
        except sd_ship_remote.Refusal as error:
            print(json.dumps({"ok": False, "error": f"{error}; nothing was queued"}))
            sys.exit(3)
        argv = sys.argv
        items = [int(argv[n + 1]) for n, arg in enumerate(argv) if arg == "--item"]
        entry = {"id": f"e{len(entries) + 1}", "item": items[0], "status": "pending", "enqueue_seq": len(entries) + 1,
                 "expected_head": argv[argv.index("--expected-head") + 1]}
        lane.write_text(json.dumps(entries + [entry]))
        print(json.dumps({"ok": True, **entry}))
    if role == "gh":  # pack sd:7868: an unmanaged repository's draft pull request, kept in `<plans>/pulls.json`
        pulls_file = plans / "pulls.json"
        pulls = json.loads(pulls_file.read_text()) if pulls_file.exists() else []
        argv = sys.argv[1:]
        def opt(name):
            return argv[argv.index(name) + 1]
        if argv[:2] == ["pr", "list"]:
            # As GitHub answers: `--head` matches the name in forks too; the head is the branch's tip now.
            def tip(name):
                done = subprocess.run(["git", "-C", str(plans.parent / "origin.git"), "rev-parse", "--verify",
                                       "--quiet", f"refs/heads/{name}"], capture_output=True, text=True)
                return done.stdout.strip() or None
            print(json.dumps([{"url": row["url"], "isCrossRepository": row.get("fork", False),
                               "baseRefName": row["base"], "headRefOid": row.get("sha") or tip(row["head"])}
                              for row in pulls if row["head"] == opt("--head") and row["state"] == "open"]))
            sys.exit(0)
        if argv[:2] == ["pr", "create"]:
            assert "--draft" in argv and Path(opt("--body-file")).is_file(), argv
            if mode == "refuse":
                print("pull request create failed: GraphQL: not allowed", file=sys.stderr)
                sys.exit(1)
            url = f"https://github.com/{opt('--repo')}/pull/{len(pulls) + 1}"
            pulls_file.write_text(json.dumps(pulls + [{"url": url, "head": opt("--head"), "state": "open",
                                                       "base": opt("--base"), "title": opt("--title")}]))
            print("Creating draft pull request", file=sys.stderr)
            if mode != "mute":
                print(url)
            sys.exit(0)
        sys.exit(f"gh fake: no such call {argv}")
    if role == "gate":  # `sd gate run --label L -- <command>`: its exit code the command's own
        # As `run_gated` does: the command in a process group of its own, INT, TERM and HUP sent on to that
        # group, and a wait for the command to end.
        argv = sys.argv[1:]
        assert argv[0] == "--label" and argv[2] == "--", argv
        import signal
        child = subprocess.Popen(argv[3:], process_group=0)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, lambda number, _frame: os.killpg(child.pid, number))
        code = child.wait()
        sys.exit(128 - code if code < 0 else code)
    if role == "guard":  # `leak-guard.sh scan`: bytes on stdin, lines numbered on LF as `grep -a -n` does
        text = sys.stdin.buffer.read()
        (plans / "guard.txt").write_bytes(text)
        if mode == "none":  # no pattern file: unknown, exit 3
            sys.exit(3)
        hits = [str(number) for number, line in enumerate(text.split(b"\\n"), 1) if b"PRIVATE" in line]
        print(" ".join(hits))
        sys.exit(1 if hits else 0)
    if role == "sd-docs-lint":
        _, args = real("sd-docs-lint")
        assert args.pr_body is not None and args.pr_body.is_file(), args
        if mode == "refuse":  # the real tool's failure lines, on stderr, its stdout only notes
            print("PR privacy: 1 pattern(s) from /x/privacy-patterns, no match")
            print("FAIL pull request body: line 3 matches a privacy pattern in /x/privacy-patterns",
                  file=sys.stderr)
            print("sd-docs-lint: 1 failure(s)", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
''')


def end(process):
    """SIGKILL a process the test started, and reap it."""
    process.kill()
    process.wait()


class Refuses:
    """A connection whose `at`th statement raises `fault`; `seen` lists every statement, so a new read is covered."""

    def __init__(self, real, at, fault, seen):
        self.real, self.at, self.fault, self.seen = real, at, fault, seen

    def execute(self, *args):
        self.seen.append(args[0])
        if len(self.seen) == self.at:
            raise self.fault
        return self.real.execute(*args)

    def __getattr__(self, name):
        return getattr(self.real, name)


def resume_state(run, child=424242):
    """What an earlier image of this process hands over while `child` still runs."""
    return {"run": run, "pid": os.getpid(), "reviewed": None, "ending": None, "marks": [], "groups": None,
            "child": {"name": "claude", "argv": [], "pid": child, "exit": None, "session": False, "offset": 0,
                      "left": 30.0}}


def moving_clock():
    """`time.monotonic`, five seconds further on at every read: a bound passes without a real wait."""
    clock = iter(range(0, 10 ** 9, 5))
    return mock.patch("time.monotonic", side_effect=lambda: next(clock))


def group_gone(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def exclude(work: Path, patterns: str) -> None:
    """Ignore `patterns` in every worktree of `work`'s repository, through its common `info/exclude`."""
    common = Path(support.git(work, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    (common / "info").mkdir(exist_ok=True)
    (common / "info" / "exclude").write_text(patterns)


class Case(unittest.TestCase):
    bundle: dict
    items: list[int]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        # The fixtures name a satellite lane `work`, which a real machine may be called too: each test runs as the
        # hub `hub` unless it says otherwise (review round 3). A real `hub.json` in this HOME would make it a
        # satellite under the wire harness, so it reads none (pack sd:7955).
        for pinned in (mock.patch.object(build, "this_host", return_value="hub"),
                       mock.patch("sd_db.hub.configured", return_value=False)):
            pinned.start()
            self.addCleanup(pinned.stop)
        self.database = self.root / "sd.db"
        initialise(self.database)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)
        self.repo = support.repository(self.root / "fixture", bare=self.root / "origin.git")
        upsert_repo(self.db, str(self.repo), remote="https://github.com/fixture/repo.git", managed=1)
        self.plans = self.root / "plans"
        self.plans.mkdir()
        tools = self.root / "tools"
        tools.mkdir()
        for role in ("claude", "sd-review", "sd-ship", "sd-docs-lint"):
            (tools / f"{role}.py").write_text(FAKE, encoding="utf-8")
        self.tools = build_run.Tools(**{name: [sys.executable, str(tools / f"{role}.py")] for name, role in
                                        (("claude", "claude"), ("review", "sd-review"), ("ship", "sd-ship"),
                                         ("lint", "sd-docs-lint"))})
        self.env = {**os.environ, "FAKE_PLANS": str(self.plans), "FAKE_DB": str(self.database),
                    "FAKE_BIN": str(BIN)}
        self.logs = self.root / "bulk"

    def plan(self, **roles):
        for role, modes in roles.items():
            (self.plans / f"{role.replace('_', '-')}.json").write_text(json.dumps(modes), encoding="utf-8")

    def calls(self, role=None):
        path = self.plans / "calls.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if role is None or row["role"] == role]

    def press(self, count=1):
        self.items = [create_item(self.db, kind="task", title=f"Build me {n}", repo=str(self.repo), status="ready")
                      for n in range(count)]
        build.queue(self.db, self.items, who="operator")
        bundle = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                             root=self.root / "worktrees")
        assert bundle is not None
        self.bundle = bundle
        return bundle

    def assert_start_recovers(self, run):
        """Every statement from the connection to `run()`, failing once: the start retries and keeps the child."""
        state, statements = resume_state(run), []

        def supervise(at, fault, seen):
            attempts, resumed = [], []

            def opened(write=False):
                attempts.append(write)
                return Refuses(connect(self.database), at if len(attempts) == 1 else 0, fault, seen)

            def reached(supervisor):
                resumed.append(supervisor.resumed)
                return {"run": run}

            with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(state)}), \
                    mock.patch("sd_db.connect", opened), mock.patch("time.sleep"), \
                    mock.patch("sys.path", [str(BIN), *sys.path]), \
                    mock.patch.object(ADAPTER, "log_root", return_value=self.logs), \
                    mock.patch.object(build_run.Supervisor, "run", reached), \
                    mock.patch.object(build, "kill_group") as kill, mock.patch("builtins.print"):
                ADAPTER.supervise_build(mock.Mock(run=run))
            return attempts, resumed, kill

        supervise(0, None, statements)
        self.assertGreaterEqual(len(statements), 4, statements)
        for at in range(1, len(statements) + 1):
            for fault in (HubFaults.UNREACHABLE, HubFaults.MISMATCH):
                with self.subTest(statement=statements[at - 1], fault=type(fault).__name__):
                    attempts, resumed, kill = supervise(at, fault, [])
                    self.assertEqual(len(attempts), 2, "the failed start was tried again")
                    self.assertEqual(resumed, [True], "the supervisor took the child over")
                    kill.assert_not_called()

    def supervisor(self, ident=None, **options):
        options = {"beat_seconds": 0.2, "timeout": 30, "grace": 0.5, **options}
        options = {"cited": ADAPTER.cited, "environ": self.env, **options}
        options.setdefault("log_root", self.logs)
        return build_run.Supervisor(self.db, ident or self.bundle["id"], tools=self.tools, **options)

    def write_pr(self, title="feat(fixture): build the fixture", body="## Summary\n\nx\n\n## Test plan\n\ny\n"):
        logs = build_run.log_dir(self.logs, self.bundle["repo"])
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"sd-{self.bundle['assignment']}.title").write_text(title + "\n", encoding="utf-8")
        (logs / f"sd-{self.bundle['assignment']}.body.md").write_text(body)

    def wait_for_builder(self, connection):
        """Until the builder (not the worktree's git) runs: the claude call is logged and its group recorded."""
        for _ in range(200):
            row = build.run(connection, self.bundle["id"])
            if row["step"] == "building" and self.calls("claude") and row["builder_pgid"]:
                return
            time.sleep(0.05)
        raise AssertionError("the builder never started")

    def statuses(self):
        return [item_state(self.db, item)["item"]["status"] for item in self.items]

    def latest(self):
        return dict(self.db.execute("SELECT * FROM runner_run ORDER BY run DESC LIMIT 1").fetchone())

    def remote_branches(self):
        return support.git(self.root / "origin.git", "for-each-ref", "--format=%(refname:short)", "refs/heads")


class Judge(unittest.TestCase):
    def lines(self, **final):
        record = {"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
                  "session_id": "s1", **final}
        return [json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}), json.dumps(record)]

    def test_judge_reads_the_last_line(self):
        self.assertEqual(build_run.judge(self.lines())["state"], "pass")
        self.assertEqual(build_run.judge(self.lines(subtype="error_max_turns", is_error=True))["state"], "max_turns")
        self.assertEqual(build_run.judge(self.lines(is_error=True))["state"], "failed")
        denied = build_run.judge(self.lines(permission_denials=[{"tool_name": "Bash"}]))
        self.assertEqual((denied["state"], denied["tools"]), ("denied", ["Bash"]))
        for lines in ([], self.lines()[:1], ["not json"], self.lines() + ["{\"type\": \"assistant\"}"]):
            with self.subTest(lines=lines):
                self.assertEqual(build_run.judge(lines)["state"], "unknown", "no final result is not a pass")
        missing = self.lines()
        missing[-1] = json.dumps({k: v for k, v in json.loads(missing[-1]).items() if k != "permission_denials"})
        self.assertEqual(build_run.judge(missing)["state"], "unknown")

    def test_judge_skips_a_trailing_lifecycle_line(self):
        """Claude Code writes a command_lifecycle line after the result (build 168 read as unknown)."""
        tail = json.dumps({"type": "command_lifecycle", "state": "completed", "session_id": "s1"})
        self.assertEqual(build_run.judge(self.lines() + [tail])["state"], "pass")
        self.assertEqual(build_run.judge(self.lines(is_error=True) + [tail, tail])["state"], "failed")
        self.assertEqual(build_run.judge(self.lines()[:1] + [tail])["state"], "unknown")

    def test_judge_refuses_another_sessions_line(self):
        """A judge pass is tied to the run's own session: a result of another session is unknown."""
        self.assertEqual(build_run.judge(self.lines(session_id="s2"))["state"], "unknown")
        self.assertEqual(build_run.judge(self.lines(), session="s9")["state"], "unknown")
        self.assertEqual(build_run.judge(self.lines(), session="s1")["state"], "pass")


class Supervise(Case):
    def test_the_fakes_refuse_what_the_real_tools_refuse(self):
        """Review round 1: the fake took an enqueue the real `sd-ship` refuses; now each parses as its tool does."""
        body = self.root / "body.md"
        body.write_text("## Summary\n", encoding="utf-8")
        enqueue = ["-C", str(self.repo), "lane", "enqueue", "--item", "1", "--title", "fix: x", "--body-file",
                   str(body), "--expected-head", "HEAD"]
        for argv, code in ((enqueue, 3), ([*enqueue, "--deliver"], 0), (["-C", str(self.repo), "lane", "lsit"], 2)):
            with self.subTest(argv=argv):
                done = subprocess.run([*self.tools.ship, *argv], capture_output=True, text=True, env=self.env,
                                      check=False)
                self.assertEqual(done.returncode, code, done.stdout + done.stderr)
        done = subprocess.run([*self.tools.review, "-C", str(self.repo), "--scope", "brnach", "--json"],
                              capture_output=True, text=True, env=self.env, check=False)
        self.assertEqual(done.returncode, 2, done.stderr)

    def test_the_builder_env_carries_the_run_and_items_for_otel(self):
        """pack sd:6943: Claude Code's OTel names the run; a value already there stays first."""
        self.press(2)
        self.env["OTEL_RESOURCE_ATTRIBUTES"] = "host.name=fixture"
        env = self.supervisor().env
        self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"], f"host.name=fixture,sd.build_run={self.bundle['id']},"
                         f"sd.items={self.items[0]};{self.items[1]}")
        self.assertNotIn("CLAUDE_CODE_ENABLE_TELEMETRY", {key for key in env if key not in os.environ})

    def test_a_press_runs_to_the_lane(self):
        """Two items, one run, one branch, one lane entry; the members end done and the items stay in progress."""
        self.press(2)
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor().run()
        self.assertEqual(ended, {"run": self.bundle["id"], "outcome": None, "step": "enqueued"})
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((row["step"], row["session_id"], row["outcome"]), ("enqueued", "session-fixture", None))
        self.assertEqual([m["status"] for m in row["members"]], ["done", "done"])
        self.assertEqual(self.statuses(), ["in_progress", "in_progress"])
        enqueue = [call["argv"] for call in self.calls("sd-ship") if "enqueue" in call["argv"]]
        self.assertEqual(len(enqueue), 1)
        self.assertEqual([enqueue[0][n + 1] for n, arg in enumerate(enqueue[0]) if arg == "--item"],
                         [str(item) for item in self.items])
        self.assertEqual(json.loads(row["final"])["result"], "built (pass)")
        cost = self.db.execute("SELECT * FROM cost WHERE assignment = ?", (row["assignment"],)).fetchone()
        self.assertEqual((cost["source"], cost["usd"], cost["tokens_in"], cost["tokens_out"]), ("run", 0.42, 10, 20))
        note = self.db.execute("SELECT body FROM note WHERE item = ? AND body LIKE '%report%'", (self.items[0],))
        self.assertIn("built (pass)", note.fetchone()["body"])
        self.assertEqual(self.remote_branches(), "main", "the fake lane pushed nothing; the supervisor neither")
        self.assertIsNone(self.latest()["cancel_requested"])

    def test_a_skipped_member_is_not_delivered(self):
        """Review round 2: only the members a branch commit's `Closes:` trailer cites are enqueued and done."""
        self.press(2)
        self.write_pr()
        self.plan(claude=["skip"], sd_review=["clean"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        enqueue = next(call["argv"] for call in self.calls("sd-ship") if "enqueue" in call["argv"])
        self.assertEqual([enqueue[n + 1] for n, arg in enumerate(enqueue) if arg == "--item"], [str(self.items[0])])
        self.assertEqual(self.statuses(), ["in_progress", "blocked"])
        self.assertEqual([m["status"] for m in build.run(self.db, self.bundle["id"])["members"]], ["done", "blocked"])
        note = self.db.execute("SELECT body FROM note WHERE item = ? AND body LIKE '%Closes: sd:%'", (self.items[1],))
        self.assertIsNotNone(note.fetchone(), "the skipped item says why it stays open")

    def test_a_member_with_an_open_question_is_not_enqueued(self):
        """sd:6571: an earlier run's commit cites the member, but this run's builder skipped it with a question."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        add_note(self.db, self.items[0], "question", "Which option?\nOption: Yes (Recommended)\nOption: No")
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn("open question", ended["blocked"])
        self.assertEqual([call for call in self.calls("sd-ship") if "enqueue" in call["argv"]], [])

    def test_only_the_member_with_an_open_question_stays_out(self):
        self.press(2)
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        add_note(self.db, self.items[1], "question", "Which option?\nOption: Yes (Recommended)\nOption: No")
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        enqueue = next(call["argv"] for call in self.calls("sd-ship") if "enqueue" in call["argv"])
        self.assertEqual([enqueue[n + 1] for n, arg in enumerate(enqueue) if arg == "--item"], [str(self.items[0])])
        self.assertEqual(self.statuses(), ["in_progress", "blocked"])
        # The lane adds each open item a branch commit's trailer cites to the closing set, unless a `Refs:`
        # line keeps it open: the questioned member's old trailer must not close it.
        work = Path(build.run(self.db, self.bundle["id"])["work_path"])
        head = enqueue[enqueue.index("--expected-head") + 1]
        body = Path(enqueue[enqueue.index("--body-file") + 1]).read_text(encoding="utf-8")
        self.db.execute("UPDATE item SET status = 'in_progress' WHERE id = ?", (self.items[1],))
        cited, _ = LANE.cited_closes(self.db, self.items[0], body, LANE.branch_commits(work, "main", head), ())
        self.assertEqual([row["item"] for row in cited], [])

    def test_the_lane_entry_is_found_by_any_delivered_member(self):
        """The entry's item is the first member delivered, so the lookups (resume, watch) read every member."""
        (self.plans / "lane.json").write_text(json.dumps([{"id": "e1", "item": 7, "status": "merged",
                                                           "enqueue_seq": 1}]))
        found = build_run.lane_entry(self.tools.ship, self.repo, [5, 7], env=self.env)
        self.assertEqual(found and found["id"], "e1")
        self.assertIsNone(build_run.lane_entry(self.tools.ship, self.repo, [5], env=self.env))

    def test_an_unread_delivery_writes_nothing(self):
        """`cited` None (the commits were not read) is unknown: no enqueue, no outcome; the reap retries."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.assertIsNone(ADAPTER.cited(self.repo, "origin/no-such-branch", "HEAD", ""))
        ended = self.supervisor(cited=lambda *_: None).run()
        self.assertEqual((ended["outcome"], self.latest()["outcome"]), (None, None))
        self.assertEqual([call for call in self.calls("sd-ship") if "enqueue" in call["argv"]], [])

    def test_a_branch_that_delivers_nothing_blocks(self):
        self.press()
        self.write_pr()
        self.plan(claude=["none"], sd_review=["clean"])
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn("delivers no item", ended["blocked"])
        self.assertEqual([call for call in self.calls("sd-ship") if "enqueue" in call["argv"]], [])

    def test_a_group_of_zombies_reads_gone(self):
        """sd:6406: under load a git grandchild's zombie waits for launchd to reap it, and macOS answers
        EPERM to `killpg` on a group that holds only zombies; that group runs nothing, so it is gone."""
        self.press()
        leader = subprocess.Popen([sys.executable, "-c", "import os\nif os.fork() == 0: os._exit(0)\nos._exit(0)"],
                                  start_new_session=True)
        self.addCleanup(leader.wait)
        # Every member, not only the leader: under gate load the forked child still ran when the leader was
        # already a zombie, so the group read alive (2026-10-30 04:07 MDT).
        def states():
            rows = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True).stdout.split("\n")
            return [row.split()[1] for row in rows if row.split() and row.split()[0] == str(leader.pid)]

        deadline = time.monotonic() + 30
        while not states() or any(state[:1] != "Z" for state in states()):
            self.assertLess(time.monotonic(), deadline, f"the group never became zombies: {states()}")
            time.sleep(0.05)
        self.assertIs(build.Probe.group(leader.pid), False)
        self.assertTrue(self.supervisor().end_group(leader.pid))

    def test_a_leader_that_dies_between_the_poll_and_the_read_is_reaped(self):
        """Gate at e97e9f626: a leader dead after the poll read gone as a zombie, and stayed unreaped."""
        self.press()
        leader = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(end, leader)

        class DiesAtTheRead(build.Probe):
            @staticmethod
            def group(pgid):
                os.kill(pgid, signal.SIGKILL)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pgid)], capture_output=True, text=True)
                    if state.stdout.strip().startswith("Z"):
                        return False
                    time.sleep(0.02)
                raise AssertionError("the leader never became a zombie")

        self.assertTrue(self.supervisor(probe=DiesAtTheRead).end_group(leader.pid, leader))
        self.assertIsNotNone(leader.returncode, "the supervisor reaps the leader it read as gone")
        self.assertTrue(group_gone(leader.pid))

    def test_an_unread_ps_does_not_fail_an_end_once_the_group_is_gone(self):
        """Review of b34af431e: an unread `ps` set a flag no later read cleared, so a run whose group read
        ESRCH ended a survivor, managed builds too. Its known groups' ESRCH is True, as `kill_group` reads it."""
        self.press()
        leader = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(end, leader)
        with mock.patch.object(build, "descendant_groups", return_value=None):
            self.assertTrue(self.supervisor().end_group(leader.pid, leader))
        self.assertIsNotNone(leader.poll(), "the group was signalled and is gone")

    def questions(self):
        return [row["body"] for row in self.db.execute("SELECT body FROM note WHERE item = ? AND kind = 'question'",
                                                       (self.items[0],))]

    def test_judge_last_line_not_exit(self):
        """Failure table, Judge: exit 0 (here 3) with a permission denial is a denial; the exit code is no verdict.

        pack sd:6723: the first denial requeues the run once at its step, with no question, and the retry's
        brief carries the denied call; the second blocks with the question."""
        self.press()
        self.plan(claude=["deny"])
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "denied")
        self.assertEqual((self.statuses(), self.questions()), (["in_progress"], []))
        self.assertEqual(self.calls("sd-review"), [], "no review after a denial")
        retry = build.run(self.db, ended["retry"])
        self.assertEqual((retry["step"], retry["session_id"], retry["run"]), ("building", "session-fixture", 2))
        self.plan(claude=["deny"])
        again = self.supervisor(retry["id"]).run()
        prompt = self.calls("claude")[-1]["argv"]
        prompt = prompt[prompt.index("-p") + 1]
        self.assertIn("permission denied: Bash `git push`", prompt)
        self.assertIn("never amend", prompt)
        self.assertEqual(again["outcome"], "blocked")
        self.assertEqual(self.statuses(), ["blocked"])
        self.assertEqual(len(self.questions()), 1)
        self.assertIn("denied Bash", self.questions()[0])
        # pack sd:6708: the same denial after a requeue needs the brief changed.
        _, options = writes.parse(self.questions()[0])
        self.assertEqual(writes.recommended(self.questions()[0], options),
                         options.index("Change the brief, then requeue (Recommended)"))

    def test_the_brief_keeps_the_builder_in_its_worktree(self):
        """pack sd:6723: the brief holds the three lines, and the builder may commit with -m or -F."""
        self.press()
        self.plan(claude=["pass"])
        self.supervisor().run()
        argv = self.calls("claude")[0]["argv"]
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("Write no file outside the worktree", prompt)
        self.assertIn("never amend", prompt)
        self.assertIn("Bash(git commit -F:*)", argv[argv.index("--allowedTools"):argv.index("--permission-mode")])

    def test_a_success_line_passes_whatever_the_exit_code(self):
        self.press()
        self.write_pr()
        self.plan(claude=["exit3"], sd_review=["clean"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued")

    def test_a_line_of_another_session_blocks(self):
        self.press()
        self.plan(claude=["other"])
        self.assertEqual(self.supervisor().run()["outcome"], "blocked")
        self.assertIn("session-of-another-run", self.latest()["detail"])

    def test_max_turns_retries_once(self):
        """Failure table, Judge: `error_max_turns` is a loss at building: a fresh row there, the session kept."""
        self.press()
        self.plan(claude=["max_turns"])
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "error_max_turns")
        retry = build.run(self.db, ended["retry"])
        self.assertEqual((retry["step"], retry["session_id"], retry["run"]), ("building", "session-fixture", 2))
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.assertEqual(self.supervisor(retry["id"]).run()["step"], "enqueued")
        resumed = [call["argv"] for call in self.calls("claude")][-1]
        self.assertEqual(resumed[resumed.index("--resume") + 1], "session-fixture")

    def test_a_pass_needs_a_clean_worktree_ahead(self):
        self.press()
        self.plan(claude=["pass"])
        supervisor = self.supervisor()
        real = supervisor.unfinished

        def dirty():
            (Path(self.bundle["work_path"]) / "stray.txt").write_text("x")
            return real()

        supervisor.unfinished = dirty
        self.assertEqual(supervisor.run()["outcome"], "blocked")
        self.assertIn("dirty", self.latest()["detail"])

    def test_timeout_kills_group(self):
        """Failure table, Building: a hung builder's group is ended at the timeout; a fresh row retries once."""
        self.press()
        self.plan(claude=["hang"])
        # A git call slower than the timeout, as on a loaded machine: the short timeout starts at
        # Building, so the run times out there and not in Worktree.
        self.tools = dataclasses.replace(self.tools, git=["sh", "-c", 'sleep 2; exec git "$@"', "git"])
        supervisor = self.supervisor()
        building = supervisor.building

        def hung() -> str | dict:
            supervisor.timeout = 1.5
            return building()

        supervisor.building = hung
        ended = supervisor.run()
        old = build.run(self.db, self.bundle["id"])
        self.assertEqual(ended["outcome"], "timed_out")
        self.assertTrue(group_gone(old["builder_pgid"]))
        self.assertEqual(build.run(self.db, ended["retry"])["step"], "building")

    def test_review_rounds_bound(self):
        """Failure table, Reviewing: blockers after 3 rounds block the items with the blockers; each fix resumes."""
        self.press()
        self.plan(claude=["pass", "fix1", "fix2"], sd_review=["blocking", "blocking", "blocking"])
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn("3 review rounds left blockers: work.txt:1 a blocker", self.latest()["detail"])
        self.assertEqual(len(self.calls("sd-review")), 3)
        fixes = [call["argv"] for call in self.calls("claude")][1:]
        self.assertEqual([argv[argv.index("--resume") + 1] for argv in fixes], ["session-fixture"] * 2)
        self.assertIn("- work.txt:1 a blocker", fixes[0][fixes[0].index("-p") + 1], "the fix names each blocker")
        self.assertEqual(self.statuses(), ["blocked"])

    def test_a_fix_then_a_clean_review_enqueues(self):
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1"], sd_review=["blocking", "clean"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        self.assertEqual(self.latest()["review_round"], 1)

    def test_a_failed_gate_goes_back_to_the_builder(self):
        """A review that cleared over a failed gate is a blocker to fix, not an unknown answer (runs 155-203)."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1"], sd_review=["gate_failed", "clean"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        self.assertEqual(len(self.calls("sd-review")), 2)
        fix = [call["argv"] for call in self.calls("claude")][1]
        self.assertIn("make target audit", fix[fix.index("-p") + 1], "the fix names the failed gate step")

    def test_a_review_the_router_skips_passes(self):
        """pack sd:8291: a README-only build ended blocked on a deliberate route skip."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["skip_route", "skip_route"])
        self.assertEqual(self.supervisor().run().get("step"), "enqueued", self.latest()["detail"])
        self.assertEqual(len(self.calls("sd-review")), 1)

    def test_a_skip_for_another_reason_still_blocks(self):
        self.press()
        self.plan(claude=["pass"], sd_review=["skip_other", "skip_other"])
        self.assertEqual(self.supervisor().run()["outcome"], "blocked")
        self.assertIn("the review gave no answer twice", self.latest()["detail"])

    def test_an_unknown_review_is_retried_once(self):
        self.press()
        self.plan(claude=["pass"], sd_review=["garbage", "garbage"])
        self.assertEqual(self.supervisor().run()["outcome"], "blocked")
        self.assertEqual(len(self.calls("sd-review")), 2)
        self.assertIn("the review gave no answer twice", self.latest()["detail"])

    def test_cancel_before_enqueuing_wins(self):
        """Failure table, Enqueuing CAS: a cancel asked during reviewing wins; no `sd-ship`, nothing pushed."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["cancel"])
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "cancelled")
        self.assertEqual(self.statuses(), ["ready"])
        self.assertEqual([m["status"] for m in build.run(self.db, self.bundle["id"])["members"]], ["cancelled"])
        self.assertEqual(self.calls("sd-ship"), [])
        self.assertEqual(self.remote_branches(), "main")
        self.assertTrue(Path(self.bundle["work_path"]).is_dir(), "the worktree is kept")

    def test_cancel_kills_the_active_group(self):
        """Failure table, Cancel: a cancel during building kills the builder's group at the next beat."""
        self.press()
        self.plan(claude=["hang"])

        def cancel():
            other = connect(self.database)
            try:
                self.wait_for_builder(other)
                state = operations.assignment_state(other, self.bundle["assignment"])
                operations.cancel_assignment(other, self.bundle["assignment"], expected_revision=state["revision"],
                                             who="operator")
            finally:
                other.close()

        thread = threading.Thread(target=cancel)
        thread.start()
        started = time.monotonic()
        ended = self.supervisor().run()
        thread.join()
        self.assertLess(time.monotonic() - started, 20, "the hang was cut short")
        self.assertEqual(ended["outcome"], "cancelled", self.latest()["detail"])
        self.assertTrue(group_gone(build.run(self.db, self.bundle["id"])["builder_pgid"]))
        self.assertEqual(self.statuses(), ["ready"])
        self.assertEqual(self.calls("sd-review"), [])

    def test_a_lost_run_stops_before_the_next_step(self):
        """Failure table, Any step: a run the reap ended mid-step; the beat changes no row, so the group is killed
        and nothing after it runs."""
        self.press()
        self.plan(claude=["slow"])

        def reaped():
            other = connect(self.database)
            try:
                self.wait_for_builder(other)
                other.execute("UPDATE runner_run SET outcome = 'interrupted' WHERE id = ?", (self.bundle["id"],))
                other.commit()
            finally:
                other.close()

        thread = threading.Thread(target=reaped)
        thread.start()
        ended = self.supervisor().run()
        thread.join()
        self.assertIsNone(ended["outcome"], ended)
        self.assertTrue(group_gone(build.run(self.db, self.bundle["id"])["builder_pgid"]), ended)
        self.assertEqual(self.calls("sd-review"), [])

    def test_a_child_runs_only_after_its_group_is_recorded(self):
        """Failure table, Child: a write that fails closes the gate; the child exits unrun."""
        self.press()
        build.write(self.db, self.bundle["id"], step="building")
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(build, "write", return_value=False):
            with self.assertRaises(build_run.Lost):
                supervisor.child([*self.tools.claude, "-p", "x"], cwd=self.root, name="gated")
        self.assertEqual(self.calls("claude"), [], "the fake never started")

    def test_a_locked_database_once_keeps_the_run_at_its_step(self):
        """pack sd:6588: the supervisor's group write meets one `database is locked`, retries, and the child runs."""
        self.press()
        build.write(self.db, self.bundle["id"], step="reviewing")

        class Locked:
            """The fixture connection whose next `BEGIN IMMEDIATE` fails once, as a held write lock does."""
            def __init__(self, connection):
                self.connection, self.armed = connection, False

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def execute(self, sql, parameters=()):
                if self.armed and sql == "BEGIN IMMEDIATE":
                    self.armed = False
                    raise sqlite3.OperationalError("database is locked")
                return self.connection.execute(sql, parameters)

        locked = Locked(self.db)
        supervisor = build_run.Supervisor(locked, self.bundle["id"], tools=self.tools, log_root=self.logs,
                                          environ=self.env, cited=ADAPTER.cited, beat_seconds=0.2, timeout=30,
                                          grace=0.5)
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        locked.armed = True
        with mock.patch.object(database, "LOCK_WAITS", (0.01,)):
            code = supervisor.child([*self.tools.claude, "-p", "x"], cwd=self.root, name="locked")
        self.assertFalse(locked.armed, "the lock was met")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls("claude")), 1)
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((row["step"], row["outcome"]), ("reviewing", None))

    def test_a_beat_the_lock_refuses_skips_the_beat(self):
        """pack sd:6741: a beat whose lock outlasts every retry is skipped, not raised. Before, the
        `database is locked` escaped `child` and the supervisor died at reviewing (6 of 9 losses)."""
        self.press()
        build.write(self.db, self.bundle["id"], step="reviewing")
        self.plan(claude=["slow"])
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        real = build.beat
        refused: list[str] = []

        def beat(connection, ident):
            if not refused:
                refused.append(ident)
                raise sqlite3.OperationalError("database is locked")
            return real(connection, ident)

        with mock.patch.object(build, "beat", beat):
            code = supervisor.child([*self.tools.claude, "-p", "x"], cwd=self.root, name="locked")
        self.assertEqual((refused, code), ([self.bundle["id"]], 0))
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((row["step"], row["outcome"]), ("reviewing", None))

    def test_a_second_supervisor_registers_nothing(self):
        self.press()
        self.assertTrue(build.register(self.db, self.bundle["id"], pid=4242, pgid=4242, start="x"))
        ended = self.supervisor().run()
        self.assertIn("another supervisor", ended["why"])
        self.assertEqual(build.run(self.db, self.bundle["id"])["supervisor_pid"], 4242)
        self.assertEqual(self.calls(), [])

    def test_a_long_title_blocks_before_the_lane(self):
        self.press()
        self.write_pr("feat(fixture): " + "x" * 60)
        self.plan(claude=["pass"], sd_review=["clean"])
        self.assertEqual(self.supervisor().run()["outcome"], "blocked")
        self.assertEqual([call for call in self.calls("sd-ship") if "enqueue" in call["argv"]], [])

    def test_a_body_the_lane_refuses_blocks_with_its_reason(self):
        """Review round 1: the lane's own line check refuses a long body line; the run blocks naming it."""
        self.press()
        self.write_pr(body="## Summary\n\n" + "x" * 80 + "\n")
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn("limit 72", ended["blocked"])
        self.assertIn("72 characters or less outside a code fence", build_run.BRIEF)

    def test_an_enqueue_refusal_goes_back_to_the_builder(self):
        """Pack sd:6926: run 14299dfe passed review, then enqueue refused its body lines; the builder fixes it."""
        self.press()
        self.write_pr(body="## Summary\n\n" + "x" * 80 + "\n")
        self.plan(claude=["pass", "fix1", "fix2"], sd_review=["clean"])
        ended = self.supervisor(checked=ADAPTER.enqueue_refusals).run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertEqual(self.calls("sd-review"), [], "no review is spent on a branch enqueue refuses")
        fix = [call["argv"] for call in self.calls("claude")][1]
        self.assertIn("body line 3 is 80 characters (limit 72)", fix[fix.index("-p") + 1])
        self.assertIn("Fence each failure table", build_run.BRIEF)
        self.assertIn("`example.test`", build_run.BRIEF)

    def test_the_leak_guard_is_checked_before_the_review(self):
        """Pack sd:6926: the pre-push leak guard refused a test's real first name only at enqueue."""
        system = self.root / "system"
        guard = system / "local-leak-guard" / "leak-guard.sh"
        guard.parent.mkdir(parents=True)
        guard.write_text('echo "  $(git rev-parse --short HEAD) added (path withheld: it matches)" >&2\nexit 1\n')
        head = support.git(self.repo, "rev-parse", "HEAD")
        environ = {**os.environ, "SYSTEM_TOOLS_ROOT": str(system)}
        self.assertEqual(ADAPTER.enqueue_refusals(self.repo, "origin/main", head, "fix: x", "", environ=environ), [],
                         "no leak-guard hook, no leak-guard check")
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\n# installed by local-leak-guard\n")
        found = ADAPTER.enqueue_refusals(self.repo, "origin/main", head, "fix: x", "", environ=environ)
        self.assertEqual(len(found), 1, found)
        self.assertIn("leak guard refuses these commits", found[0])
        self.assertIn("path withheld", found[0])

    def test_a_failed_build_is_pushed_once(self):
        """Pack sd:6946: the tick pushes a blocked run once on ntfy; a failed send is tried again."""
        self.press()
        build.end(self.db, self.bundle["id"], "blocked", "the review left blockers")
        sent = self.root / "sent.txt"
        notify = self.root / "notify"
        notify.write_text(f'#!/bin/sh\necho "$@" >> {sent}\nexit "${{FAKE_EXIT:-0}}"\n')
        notify.chmod(0o755)
        environ = {**os.environ, "SD_NOTIFY": str(notify)}
        self.assertEqual(ADAPTER.push_failures(self.db, {**environ, "FAKE_EXIT": "1"}), [])
        self.assertEqual(ADAPTER.push_failures(self.db, environ), [f"build:{self.bundle['id']}"])
        self.assertEqual(ADAPTER.push_failures(self.db, environ), [])
        lines = sent.read_text().splitlines()
        self.assertEqual(len(lines), 2, "one failed send, one that landed, none after")
        self.assertIn(f"-t Build blocked: sd:{self.items[0]} -c ntfy", lines[1])

    def test_a_body_the_lint_refuses_blocks_naming_the_line(self):
        """Pack sd:6402: the lint's failures go to stderr; the block names them, and the brief warns."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"], sd_docs_lint=["refuse"])
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn("line 3 matches a privacy pattern", ended["blocked"])
        self.assertIn(".lint.err.out", ended["blocked"])
        self.assertEqual([call for call in self.calls("sd-ship") if "enqueue" in call["argv"]], [])
        self.assertIn("names no local path", build_run.BRIEF)

    def test_the_brief_keeps_closes_lines_out_of_the_body(self):
        """Pack sd:6626: run 137 copied its commit's Closes: trailer into the body; enqueue refuses it."""
        self.assertIn("The body carries no `Closes:` line", build_run.BRIEF)

    def test_the_brief_forbids_a_kill_by_pattern(self):
        """Pack sd:6815: build 268's `pkill -f "sd-check --json"` ended the vault job's queued gate."""
        self.assertIn("never `pkill` or `killall`", build_run.BRIEF)

    def test_a_requeued_bundle_briefs_the_findings_sent_to_its_branch(self):
        """Pack sd:6020: Send to builder writes a finding note and queues the bundle again; the fresh builder's brief
        names the finding and the branch it resumes, and leaves out a note from before the bundle's first run."""
        self.press()
        item = self.items[0]
        add_note(self.db, item, "comment", "Send to builder: an older finding for another branch")
        self.db.execute("UPDATE note SET timestamp = '2000-01-01T00:00:00+00:00' WHERE body LIKE '%older finding%'")
        build.end(self.db, self.bundle["id"], "cancelled", "the operator cancelled it")
        add_note(self.db, item, "comment", "Send to builder: the Retry label reads wrong on phones")
        lead = self.bundle["assignment"]
        runner.requeue(self.db, lead, expected_revision=runner.queue_state(self.db, lead)["revision"], who="operator")
        again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                            root=self.root / "worktrees")
        assert again is not None
        prompt = build_run.BRIEF.format(**self.supervisor(again["id"]).brief())
        self.assertIn(f"- sd:{item}: the Retry label reads wrong on phones", prompt)
        self.assertIn(f"already holds the commits of an earlier run on {self.bundle['branch']}", prompt)
        self.assertNotIn("older finding", prompt)
        first = build_run.BRIEF.format(**self.supervisor(self.bundle["id"]).brief())
        self.assertNotIn("Send to builder", first)

    def test_enqueue_resume_no_duplicate(self):
        """Failure table, Enqueuing (pack sd:6094 (a)): a run lost after the lane wrote its entry resumes at
        enqueuing, reads `lane list` first, finds the entry at its head and records `enqueued`; no second entry."""
        self.press()
        self.write_pr()
        work = Path(self.bundle["work_path"])
        support.git(self.repo, "worktree", "add", "-q", "-b", self.bundle["branch"], str(work), "origin/main")
        (work / "work.txt").write_text("done\n")
        support.git(work, "add", "work.txt")
        support.git(work, "-c", "commit.gpgsign=false", "commit", "-qm", f"fix: done\n\nCloses: sd:{self.items[0]}")
        head = support.git(work, "rev-parse", "HEAD")
        (self.plans / "lane.json").write_text(json.dumps([{"id": "e1", "item": self.items[0], "status": "pending",
                                                           "enqueue_seq": 1, "expected_head": head}]))
        build.write(self.db, self.bundle["id"], step="enqueuing")
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        self.assertEqual([call["argv"][-2:] for call in self.calls("sd-ship")], [["lane", "list"]])
        self.assertEqual(len(json.loads((self.plans / "lane.json").read_text())), 1)

    def test_an_unread_lane_at_enqueuing_writes_nothing(self):
        """An unknown `lane list` is no answer: the supervisor stops unwritten, and the reap retries it."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"], sd_ship=["unread"])
        ended = self.supervisor().run()
        self.assertEqual((ended["outcome"], self.latest()["step"]), (None, "enqueuing"))
        self.assertEqual([call for call in self.calls("sd-ship") if "enqueue" in call["argv"]], [])

    def test_clean_keeps_dirty_worktree(self):
        """Failure table, Cleaning: a dirty worktree stays, with a note; the run ends done and releases."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.supervisor().run()
        work = Path(self.bundle["work_path"])
        (work / "stray.txt").write_text("x")
        self.merge_to_cleaning()
        ended = self.supervisor().run()
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((ended["outcome"], row["outcome"], row["released_at"] is not None), ("done", "done", True))
        self.assertTrue(work.is_dir())
        note = self.db.execute("SELECT body FROM note WHERE item = ? ORDER BY id DESC", (self.items[0],)).fetchone()
        self.assertIn("kept worktree", note["body"])

    def test_cleaning_keeps_an_ignored_file(self):
        """pack sd:6979: `git worktree remove` deletes ignored files, which plain `status --porcelain` omits."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.supervisor().run()
        work = Path(self.bundle["work_path"])
        exclude(work, "__pycache__/\n.env\n")
        (work / "__pycache__").mkdir()
        (work / "__pycache__" / "x.pyc").write_bytes(b"x")
        (work / ".env").write_text("TOKEN=change-me\n")
        self.merge_to_cleaning()
        self.assertEqual(self.supervisor().run()["outcome"], "done")
        self.assertEqual((work / ".env").read_text(), "TOKEN=change-me\n")
        note = self.db.execute("SELECT body FROM note WHERE item = ? ORDER BY id DESC", (self.items[0],)).fetchone()
        self.assertIn("!! .env", note["body"])

    def test_cleaning_removes_a_clean_worktree(self):
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.supervisor().run()
        self.merge_to_cleaning()
        self.assertEqual(self.supervisor().run()["outcome"], "done")
        self.assertFalse(Path(self.bundle["work_path"]).exists())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM runner_run WHERE released_at IS NULL").fetchone()[0], 0)

    def checks(self, sync=0, health=0):
        """`checking`'s tools: each logs its argv to `checks.log` and exits with the code given."""
        log = self.root / "checks.log"
        for name, code in (("sync", sync), ("health", health)):
            script = self.root / "tools" / f"{name}.py"
            script.write_text(f"import sys\nopen({str(log)!r}, 'a').write({name!r} + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n"
                              f"sys.exit({code})\n")
            setattr(self.tools, name, [sys.executable, str(script)])
        return log

    def picked_to_checking(self):
        self.items = [create_item(self.db, kind="task", title="Picked", repo=str(self.repo), status="ready")]
        build.set_pickup(self.db, True)
        build.queue(self.db, self.items, who=build.PICKUP)
        bundle = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                             root=self.root / "worktrees")
        assert bundle is not None
        self.bundle = bundle
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.supervisor().run()
        entries = json.loads((self.plans / "lane.json").read_text())
        (self.plans / "lane.json").write_text(json.dumps([{**row, "status": "merged"} for row in entries]))
        self.assertTrue(build.merged(self.db, build.run(self.db, self.bundle["id"])))
        self.assertEqual(build.run(self.db, self.bundle["id"])["step"], "checking")

    def test_checking_refreshes_then_checks_health_then_cleans(self):
        """pack sd:6097: after an auto-pickup merge, repo-sync refresh, then dashboard health, then cleaning."""
        log = self.checks()
        self.picked_to_checking()
        self.assertEqual(self.supervisor().run()["outcome"], "done")
        self.assertEqual(log.read_text().splitlines(), [f"sync refresh {self.repo}", "health health"])
        self.assertTrue(build.pickup_state(self.db)["on"], "a passing check leaves auto-pickup on")
        self.assertFalse(Path(self.bundle["work_path"]).exists())

    def test_a_failed_health_check_turns_auto_pickup_off_and_files(self):
        """Failure table, Checking: a non-zero exit turns auto-pickup off and files an item; no revert."""
        log = self.checks(health=1)
        self.picked_to_checking()
        self.assertEqual(self.supervisor().run()["outcome"], "done")
        self.assertEqual(len(log.read_text().splitlines()), 2)
        self.assertFalse(build.pickup_state(self.db)["on"])
        filed = self.db.execute("SELECT title FROM item WHERE title LIKE ?", (build.HEALTH + "%",)).fetchall()
        self.assertEqual([row["title"] for row in filed], [build.HEALTH + self.bundle["branch"]])
        self.assertFalse(Path(self.bundle["work_path"]).exists(), "the run still cleans up")

    def test_a_failed_refresh_skips_the_health_check(self):
        log = self.checks(sync=2)
        self.picked_to_checking()
        self.supervisor().run()
        self.assertEqual(log.read_text().splitlines(), [f"sync refresh {self.repo}"])
        self.assertFalse(build.pickup_state(self.db)["on"])

    def hold(self, session: str) -> None:
        work = Path(self.bundle["work_path"])
        sessions.beat(self.db, session, machine=this_host(), cwd=None, found=sessions.worktrees([str(work)]))

    def test_a_held_build_worktree_is_kept(self):
        """pack sd:6024: a session other than the run's builder holds the worktree, so cleaning keeps it."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.supervisor().run()
        self.hold("session-other")
        self.merge_to_cleaning()
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "done")
        self.assertTrue(Path(self.bundle["work_path"]).is_dir())
        note = self.db.execute("SELECT body FROM note WHERE item = ? ORDER BY id DESC", (self.items[0],)).fetchone()
        self.assertIn("a live session holds it: session-", note["body"])

    def test_the_builders_own_session_does_not_keep_its_worktree(self):
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.supervisor().run()
        self.hold("session-fixture")
        self.merge_to_cleaning()
        self.assertEqual(self.supervisor().run()["outcome"], "done")
        self.assertFalse(Path(self.bundle["work_path"]).exists())

    def merge_to_cleaning(self):
        entries = json.loads((self.plans / "lane.json").read_text())
        (self.plans / "lane.json").write_text(json.dumps([{**row, "status": "merged"} for row in entries]))
        lane = lambda row, items: build_run.lane_entry(self.tools.ship, self.repo, items)  # noqa: E731
        exited = build.Probe()
        exited.alive = lambda pid: False if pid == os.getpid() else build.Probe.alive(pid)  # type: ignore[method-assign]
        exited.group = lambda pgid: False if pgid == os.getpgrp() else build.Probe.group(pgid)  # type: ignore[method-assign]
        with mock.patch.dict(os.environ, {"FAKE_PLANS": str(self.plans), "FAKE_BIN": str(BIN)}):
            moved = build_run.watch(self.db, lane=lane, probe=exited)
        self.assertEqual(moved, [{"run": self.bundle["id"], "step": "cleaning"}])


#: An unmanaged repository's check: it passes once the builder's fix round wrote `fix` into work.txt.
CHECK = [sys.executable, "-c", "import sys; sys.exit(0 if 'fix' in open('work.txt').read() else 1)"]
PASS = [sys.executable, "-c", "print('ok')"]
#: A supervisor in a process of its own, as `spawn` starts one, so a test can SIGKILL or pause it mid-check.
SUPERVISE = textwrap.dedent("""\
    import json, os, sys
    from pathlib import Path
    spec = json.loads(sys.argv[1])
    sys.path[:0] = spec["path"]
    import sd_operations
    from sd_db import build_run
    from sd_db.database import connect
    build_run.Supervisor(connect(spec["db"]), spec["ident"], tools=build_run.Tools(**spec["tools"]),
                         log_root=Path(spec["logs"]), environ=dict(os.environ), cited=sd_operations.cited,
                         checks=lambda work: [spec["check"]], beat_seconds=0.2, timeout=90, grace=0.5).run()
""")


class Unmanaged(Case):
    """Pack sd:7868: an unmanaged repository's run ends in a pushed branch and a draft pull request, no lane."""

    def setUp(self):
        super().setUp()
        self.db.execute("UPDATE repo SET managed = 0")
        tools = self.root / "tools"
        for role in ("gh", "gate", "guard"):
            (tools / f"{role}.py").write_text(FAKE, encoding="utf-8")
        self.tools = dataclasses.replace(self.tools, gh=[sys.executable, str(tools / "gh.py")],
                                         gate=[sys.executable, str(tools / "gate.py")],
                                         guard=[sys.executable, str(tools / "guard.py")])

    def pulls(self):
        path = self.plans / "pulls.json"
        return json.loads(path.read_text()) if path.exists() else []

    def contribution(self, item):
        return json.loads(item_state(self.db, item)["item"]["fields"] or "{}").get("contribution") or {}

    def test_a_clean_run_pushes_the_branch_and_opens_one_draft(self):
        self.press(2)
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended, {"run": self.bundle["id"], "outcome": None, "step": "enqueued"})
        branch = self.bundle["branch"]
        self.assertEqual(self.calls("sd-ship"), [], "no lane: not even its list")
        self.assertIn(branch, self.remote_branches().split())
        url = "https://github.com/fixture/repo/pull/1"
        self.assertEqual([(row["url"], row["head"], row["base"]) for row in self.pulls()], [(url, branch, "main")])
        create = next(call["argv"] for call in self.calls("gh") if call["argv"][:2] == ["pr", "create"])
        self.assertEqual(create[create.index("--title") + 1], "feat(fixture): build the fixture")
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((row["step"], row["detail"], build.draft_pull(row)), ("enqueued", build.DRAFT_PULL + url, url))
        self.assertEqual([m["status"] for m in row["members"]], ["done", "done"])
        self.assertEqual(self.statuses(), ["in_progress", "in_progress"], "open until the owners merge")
        self.assertEqual(self.contribution(self.items[0]).get("pull_url"), url)
        self.assertEqual(build.pull_url(self.db, self.bundle["repo"], branch, self.items), url)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL").fetchone()[0], 0)
        notes = [r["body"] for r in self.db.execute("SELECT body FROM note WHERE item = ?", (self.items[0],))]
        self.assertTrue(any(f"`{sd_quote(PASS)}` passed at" in body for body in notes), notes)
        self.assertTrue(any(url in body and "owners merge" in body for body in notes), notes)

    def test_a_failing_check_goes_back_to_the_builder(self):
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix"], sd_review=["clean", "clean"])
        self.assertEqual(self.supervisor(checks=lambda work: [CHECK]).run()["step"], "enqueued")
        self.assertEqual(len(self.calls("claude")), 2)
        fix = self.calls("claude")[1]["argv"]
        self.assertIn("exit 1", fix[fix.index("-p") + 1])
        self.assertEqual(build.run(self.db, self.bundle["id"])["review_round"], 1)
        self.assertEqual(len(self.pulls()), 1)

    def test_a_check_that_edits_a_tracked_file_is_a_failure_not_a_pass(self):
        """Lane review of b64873f13: a check that rewrites a tracked file and exits 0 tested a tree the head does
        not hold; it goes back to the builder naming the path, and nothing is published."""
        edit = [sys.executable, "-c", "open('README.md', 'a').write('regenerated\\n')"]
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix", "fix", "fix"], sd_review=["clean"] * 4)
        ended = self.supervisor(checks=lambda work: [edit]).run()
        self.assertNotEqual(ended.get("step"), "enqueued", ended)
        self.assertEqual(self.pulls(), [])
        self.assertNotIn(self.bundle["branch"], self.remote_branches().split())
        fix = self.calls("claude")[1]["argv"]
        self.assertIn("README.md", fix[fix.index("-p") + 1])
        notes = [row["body"] for row in self.db.execute("SELECT body FROM note WHERE item = ?", (self.items[0],))]
        self.assertFalse([body for body in notes if "passed at" in body], notes)

    def test_a_check_that_ignores_sigterm_ends_with_the_run(self):
        """Review of 8fa8c7cca: `sd gate run` puts the check in a group of its own; a timeout ends that group too."""
        pid_file = self.root / "check.pid"
        hang = [sys.executable, "-c", "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(120)"]
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [hang], timeout=4).run()
        pid = int(pid_file.read_text())
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True, check=False))
        self.assertEqual(ended["outcome"], "timed_out")
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        self.assertFalse(alive, f"the check {pid} outlived the run it was ended with")
        self.assertEqual(self.pulls(), [])

    def lose_the_supervisor_during_a_check(self, number):
        """Run a supervisor in a process of its own up to a check that ignores SIGTERM, send it `number`, then
        reap past a stale beat as the dispatcher's tick does; the check's pid and the reap's answer."""
        pid_file = self.root / "check.pid"
        hang = [sys.executable, "-c", "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(120)"]
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        script = self.root / "supervise.py"
        script.write_text(SUPERVISE, encoding="utf-8")
        spec = {"path": [str(Path(build_run.__file__).resolve().parents[1]), str(BIN)], "db": str(self.database),
                "ident": self.bundle["id"], "tools": dataclasses.asdict(self.tools), "logs": str(self.logs),
                "check": hang}
        with open(self.root / "supervise.err", "wb") as errors:
            supervisor = subprocess.Popen([sys.executable, str(script), json.dumps(spec)], env=self.env,
                                          stdin=subprocess.DEVNULL, stdout=errors, stderr=errors,
                                          start_new_session=True)
        self.addCleanup(end, supervisor)
        deadline = time.monotonic() + 60
        while not (pid_file.exists() and pid_file.read_text()):
            self.assertIsNone(supervisor.poll(), (self.root / "supervise.err").read_text())
            self.assertLess(time.monotonic(), deadline, "the check never started")
            time.sleep(0.05)
        check = int(pid_file.read_text())
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(check)], capture_output=True, check=False))
        os.kill(supervisor.pid, number)
        if number == signal.SIGKILL:
            supervisor.wait()
        beat = datetime.fromisoformat(self.latest()["heartbeat_at"])
        settled = build.reap(self.db, now=(beat + timedelta(seconds=build.STALE_SECONDS + 30)).isoformat())
        return check, settled

    def assert_ended(self, pid):
        """`pid` ends within 5 s: a killed check is a zombie until its reparented parent reaps it."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            done = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
            if done.returncode != 0 or done.stdout.strip().startswith("Z"):
                return
            time.sleep(0.05)
        self.fail(f"the check {pid} outlived the reaped run, beside its retry")

    def test_a_supervisor_killed_during_a_check_leaves_no_check_for_the_retry(self):
        """Review of 4773ed48a: SIGKILL on the supervisor, then the reap; the check's own group goes too."""
        check, settled = self.lose_the_supervisor_during_a_check(signal.SIGKILL)
        self.assert_ended(check)
        self.assertIsNotNone(settled[0]["retry"], settled)

    def test_a_supervisor_whose_beat_stopped_during_a_check_leaves_no_check(self):
        """Review of 4773ed48a: a paused supervisor (no beat), then the reap; the check's own group goes too."""
        check, settled = self.lose_the_supervisor_during_a_check(signal.SIGSTOP)
        self.assert_ended(check)
        self.assertIsNotNone(settled[0]["retry"], settled)

    def test_a_group_the_check_started_and_that_ended_is_not_signalled(self):
        """Review of 4773ed48a: a pgid read during the run is no proof at its end, when the pid may be reused."""
        pid_file = self.root / "short.pid"
        hang = [sys.executable, "-c", "import os, signal, subprocess, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "short = subprocess.Popen(['sleep', '1.5'], process_group=0); "
                f"open({str(pid_file)!r}, 'w').write(str(short.pid)); short.wait(); time.sleep(120)"]
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        with mock.patch("os.killpg", wraps=os.killpg) as killpg:
            ended = self.supervisor(checks=lambda work: [hang], timeout=5).run()
        self.assertEqual(ended["outcome"], "timed_out")
        short = int(pid_file.read_text())
        self.assertEqual([call.args for call in killpg.call_args_list if call.args[0] == short and call.args[1]],
                         [], "the ended group was signalled")

    def test_a_recorded_group_now_led_by_another_process_is_left_alone(self):
        """Review of 4773ed48a: a recorded group whose leader started at another time is not the run's."""
        self.press()
        other = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(end, other)
        leader = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(end, leader)
        supervisor = self.supervisor()
        supervisor.groups = {other.pid: "Thu Jan  1 00:00:00 2026"}
        self.assertTrue(supervisor.end_group(leader.pid, leader))
        self.assertIsNone(other.poll(), "a process the run did not start was signalled")

    def test_a_recorded_group_that_ended_leaves_at_the_next_poll(self):
        """sd:7909: a recorded group is dropped once it ends, so a later pid wrap cannot make it a Survivor."""
        self.press()
        ended = subprocess.Popen(["true"], start_new_session=True)
        ended.wait()
        reads = iter([{ended.pid: ""}])
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(build, "descendant_groups", side_effect=lambda pgid: next(reads, {})):
            code = supervisor.child([sys.executable, "-c", "import time; time.sleep(2.5)"], cwd=self.root,
                                    name="poll", descendants=True)
        self.assertEqual(code, 0)
        self.assertNotIn(ended.pid, supervisor.groups)

    def test_a_cancel_during_a_check_ends_the_checks_own_group(self):
        """End paths, cancel: the beat's cancel ends the gate's group and the check's, which ignores SIGTERM."""
        pid_file = self.root / "check.pid"
        hang = [sys.executable, "-c", "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(120)"]
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])

        def cancel():
            other = connect(self.database)
            try:
                for _ in range(600):
                    if pid_file.exists() and pid_file.read_text():
                        break
                    time.sleep(0.05)
                state = operations.assignment_state(other, self.bundle["assignment"])
                operations.cancel_assignment(other, self.bundle["assignment"], expected_revision=state["revision"],
                                             who="operator")
            finally:
                other.close()

        thread = threading.Thread(target=cancel)
        thread.start()
        ended = self.supervisor(checks=lambda work: [hang]).run()
        thread.join()
        check = int(pid_file.read_text())
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(check)], capture_output=True, check=False))
        self.assertEqual(ended["outcome"], "cancelled", self.latest()["detail"])
        self.assert_ended(check)
        self.assertEqual(self.pulls(), [])

    def test_a_check_that_exits_leaves_no_process_of_its_group(self):
        """End paths, normal exit: a process the check left in its own group goes once the gate returns."""
        pid_file = self.root / "left.pid"
        leave = [sys.executable, "-c", "import subprocess, time; left = subprocess.Popen(['sleep', '60']); "
                 f"open({str(pid_file)!r}, 'w').write(str(left.pid)); time.sleep(1)"]
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [leave]).run()
        left = int(pid_file.read_text())
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(left)], capture_output=True, check=False))
        self.assertEqual(ended["step"], "enqueued", self.latest()["detail"])
        self.assert_ended(left)

    def test_a_gate_that_starts_no_check_leaves_no_group(self):
        """End paths, gate refusal: `sd gate run` exits 125 with nothing started; its group reads gone."""
        pid_file = self.root / "check.pid"
        refuse = f"import sys; print({build_run.GATE_NOT_STARTED!r}, file=sys.stderr); sys.exit(125)"
        self.tools = dataclasses.replace(self.tools, gate=[sys.executable, "-c", refuse])
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [[sys.executable, "-c",
                                                      f"open({str(pid_file)!r}, 'w').write('ran')"]]).run()
        self.assertFalse(pid_file.exists(), "the check ran")
        self.assertTrue(group_gone(self.latest()["builder_pgid"]), ended)
        self.assertEqual(self.pulls(), [])

    def test_a_check_that_itself_exits_125_goes_back_to_the_builder(self):
        """Pack sd:7899: the gate passes a check's own 125 through. With no refusal on the gate's stderr it
        is a failure of the code, sent to the builder, not an unknown the run retries."""
        own = [sys.executable, "-c", "import sys; sys.exit(0 if 'fix' in open('work.txt').read() else 125)"]
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix"], sd_review=["clean", "clean"])
        ended = self.supervisor(checks=lambda work: [own]).run()
        self.assertEqual(ended.get("step"), "enqueued", ended)
        fix = self.calls("claude")[1]["argv"]
        self.assertIn("exit 125", fix[fix.index("-p") + 1])

    def test_no_check_entrypoint_runs_none_and_says_so(self):
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.assertEqual(self.supervisor(checks=lambda work: []).run()["step"], "enqueued")
        self.assertEqual(self.calls("gate"), [])
        note = self.db.execute("SELECT body FROM note WHERE item = ? AND body LIKE '%no check entrypoint%'",
                               (self.items[0],)).fetchone()
        self.assertIsNotNone(note)

    def test_a_refused_push_blocks_and_opens_nothing(self):
        self.press()
        self.write_pr()
        support.git(self.repo, "remote", "set-url", "--push", "origin", str(self.root / "missing.git"))
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn("git push", ended["blocked"])
        self.assertEqual(([c for c in self.calls("gh") if c["argv"][0] == "pr"], self.pulls()), ([], []))

    def test_a_refused_create_blocks(self):
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"], gh=["list", "refuse"])
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn("gh pr create refused", ended["blocked"])
        self.assertEqual(self.contribution(self.items[0]), {})

    def test_a_retry_reuses_the_draft_a_lost_run_opened(self):
        """Lost after the create (its url unread): the retry lists the branch's open pull request, opens none."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"], gh=["list", "mute"])
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended["outcome"], None)
        self.assertIn("printed no pull-request url", ended["why"])
        self.assertEqual(len(self.pulls()), 1)
        row = self.latest()
        build.end(self.db, row["id"], "interrupted", "lost after the create")
        retry = self.latest()
        self.assertEqual((retry["step"], retry["id"] != row["id"]), ("enqueuing", True))
        self.assertEqual(self.supervisor(retry["id"], checks=lambda work: [PASS]).run()["step"], "enqueued")
        self.assertEqual(len(self.pulls()), 1, "one draft, never two")
        self.assertEqual(self.contribution(self.items[0]).get("pull_url"), self.pulls()[0]["url"])

    def seed_pull(self, **row):
        """An open pull request already on GitHub for the run's branch: the fake gh's `pulls.json` row."""
        url = f"https://github.com/fixture/repo/pull/{len(self.pulls()) + 1}"
        (self.plans / "pulls.json").write_text(json.dumps(self.pulls() + [
            {"url": url, "head": self.bundle["branch"], "state": "open", "base": "main", "title": "x", **row}]))
        return url

    def test_a_forks_pull_request_on_the_branch_name_is_never_linked(self):
        """Review of b34af431e: `gh pr list --head` matches a fork's branch of the same name; its merge
        must not close the tasks. The run opens its own draft and links that."""
        self.press()
        fork = self.seed_pull(fork=True, sha="0" * 40)
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended["step"], "enqueued", ended)
        own = self.pulls()[-1]["url"]
        self.assertNotEqual(own, fork)
        self.assertEqual(self.contribution(self.items[0]).get("pull_url"), own)
        self.observed(fork, "merged")
        self.assertEqual(self.watched(), [])
        self.assertEqual(self.statuses(), ["in_progress"])

    def test_a_pull_request_into_another_base_is_not_linked(self):
        """Review of b34af431e: the sole pull request on the branch targets another base: blocked, unlinked."""
        self.press()
        other = self.seed_pull(base="release")
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended["outcome"], "blocked", ended)
        self.assertIn("targets 'release', not 'main'; it was not linked", ended["blocked"])
        self.assertEqual((self.contribution(self.items[0]), len(self.pulls())), ({}, 1))
        self.observed(other, "merged")
        self.assertEqual(self.watched(), [])
        self.assertEqual(self.statuses(), ["blocked"], "the blocked run's task, never done")

    def test_a_pull_request_on_another_head_is_unread_and_not_linked(self):
        """Review of b34af431e: the pull request reads a head that is not the pushed one: unknown, unlinked."""
        self.press()
        self.seed_pull(sha="f" * 40)
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended["outcome"], None, ended)
        self.assertIn("not the pushed", ended["why"])
        self.assertEqual(self.contribution(self.items[0]), {})

    def test_check_entrypoints_that_do_not_read_end_the_run_not_a_fix_round(self):
        """Review of b34af431e: a declaration fault (CLAUDE.local.md) is the operator's, not the builder's."""
        def broken(work):
            raise ValueError("CLAUDE.local.md: the sd-check block does not parse")
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix"], sd_review=["clean", "clean"])
        ended = self.supervisor(checks=broken).run()
        self.assertEqual(ended["outcome"], "blocked", ended)
        self.assertIn("configuration fault", ended["blocked"])
        self.assertIn("CLAUDE.local.md", ended["blocked"])
        self.assertEqual((len(self.calls("claude")), self.pulls()), (1, []), "no paid fix round, nothing published")

    def test_a_git_that_gave_no_answer_to_the_entrypoints_is_unread(self):
        """Review of b34af431e: `GitUnavailable` is transient: Unread, for the reap's retry, not a fix round."""
        with mock.patch.object(SD_LIB, "detect_entrypoints", side_effect=SD_LIB.GitUnavailable("git timed out")):
            with self.assertRaises(build_run.Unread):
                ADAPTER.own_checks(self.root)
            self.press()
            self.write_pr()
            self.plan(claude=["pass", "fix"], sd_review=["clean", "clean"])
            ended = self.supervisor(checks=ADAPTER.own_checks).run()
        self.assertEqual(ended["outcome"], None, ended)
        self.assertIn("git timed out", ended["why"])
        self.assertEqual((len(self.calls("claude")), self.pulls()), (1, []))

    def test_a_private_value_in_a_commit_blocks_before_the_push(self):
        """Review of 8fa8c7cca: an unmanaged repository has no leak-guard hook; the scan runs before the push."""
        (self.plans / "visibility.txt").write_text("public")
        self.press()
        self.write_pr()
        self.plan(claude=["PRIVATE"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn("nothing was pushed: a privacy pattern matches the commits", ended["blocked"])
        self.assertNotIn(self.bundle["branch"], self.remote_branches().split())
        self.assertEqual((self.pulls(), [c for c in self.calls("gh") if c["argv"][:2] == ["pr", "create"]]), ([], []))
        scanned = (self.plans / "guard.txt").read_text()
        self.assertIn("feat(fixture): build the fixture", scanned.splitlines()[0], "the title is line 1")
        self.assertIn("work.txt", scanned, "the paths each commit touches")

    def extra(self, files=None, message=None, move=None, merge=None):
        """A builder commit with `files` (name to bytes), `message` lines and `move`s (old to new path), then a
        `merge` of main (`file`, `theirs`, `resolved` bytes) when given, through the public scan."""
        (self.plans / "extra.json").write_text(json.dumps({
            "files": {name: data.hex() for name, data in (files or {}).items()}, "message": message or [],
            "move": move or {},
            "merge": merge and {"file": merge["file"], "theirs": merge["theirs"].hex(),
                                "resolved": merge["resolved"].hex()}}))
        self.press()
        self.write_pr()
        self.plan(claude=["extra"], sd_review=["clean"])
        return self.supervisor(checks=lambda work: [PASS]).run()

    def scan_blocks(self, **case):
        ended = self.extra(**case)
        self.assertEqual(ended.get("outcome"), "blocked", ended)
        self.assertIn("nothing was pushed: a privacy pattern matches the commits", ended["blocked"])
        self.assertNotIn(self.bundle["branch"], self.remote_branches().split())

    # Review of ac8175265, class: the scan's diff parse. Each holds PRIVATE where only a misread drops it.

    def test_the_scan_reads_an_added_line_that_starts_with_plus_plus(self):
        """An added `++PRIVATE` shows as `+++PRIVATE`: a hunk line, not the `+++ ` file header."""
        self.scan_blocks(files={"plus.txt": b"++PRIVATE\n"})

    def test_the_scan_reads_a_file_that_is_not_utf8(self):
        """A Latin-1 file: `git log -p` is no UTF-8, and a strict decode crashed the supervisor."""
        self.scan_blocks(files={"latin.txt": b"caf\xe9 PRIVATE\n"})

    def test_the_scan_keeps_a_line_whole_across_a_cr_or_form_feed(self):
        """splitlines cut a line at CR or form feed, and the part after lost its `+`; only LF ends a line now."""
        self.scan_blocks(files={"cr.txt": b"ok\rPRIVATE\n"})
        self.assertIn(b"ok\rPRIVATE", (self.plans / "guard.txt").read_bytes())

    def test_the_scan_reads_a_message_line_that_looks_like_a_diff_header(self):
        """A message line `diff --git ...` ended the message for the mixed parse; messages are read apart now."""
        self.scan_blocks(message=["diff --git a/x b/x\nPRIVATE"])

    def test_the_scan_reads_a_binary_files_path(self):
        """A binary file has no `+++` line; its path still reaches the scan."""
        self.scan_blocks(files={"PRIVATE.bin": b"\x00\xff\x00"})

    # Review of 5a5adca64, class: every git read the scan makes, for merge, root and renamed commits.

    def test_the_scan_reads_a_value_only_a_conflict_resolution_adds(self):
        """`git log -p` and `--name-only` print no diff for a merge, so a value typed while resolving a
        conflict with main was pushed unscanned."""
        self.scan_blocks(files={"conflict.txt": b"ours\n"},
                         merge={"file": "conflict.txt", "theirs": b"theirs\n", "resolved": b"PRIVATE\n"})
        commits = support.git(Path(self.bundle["work_path"]), "rev-list", "--merges", "origin/main..HEAD")
        self.assertTrue(commits, "the branch holds a merge commit")

    def test_a_merge_that_takes_one_side_passes(self):
        """A clean resolution adds no new bytes beyond main's, which is the target repository's own text."""
        ended = self.extra(files={"conflict.txt": b"ours\n"},
                           merge={"file": "conflict.txt", "theirs": b"theirs\n", "resolved": b"ours\n"})
        self.assertEqual(ended.get("step"), "enqueued", ended)

    def test_the_scan_reads_a_renamed_files_new_path(self):
        """With renames off a move reads as a delete and an add: the new path reaches the scan."""
        self.scan_blocks(move={"README.md": "PRIVATE.md"})

    def test_binary_content_and_a_missing_final_newline_are_not_scanned(self):
        """Binary content is not read, as `leak-guard.sh check` reads none; `\\ No newline` is git's marker."""
        ended = self.extra(files={"bin.dat": b"\x00PRIVATE\x00", "tail.txt": b"no newline"})
        self.assertEqual(ended.get("step"), "enqueued", ended)

    def test_a_body_that_is_not_utf8_blocks_and_does_not_crash(self):
        """Review of ac8175265: the body read caught only OSError, so a Latin-1 body killed the supervisor."""
        self.press()
        self.write_pr()
        logs = build_run.log_dir(self.logs, self.bundle["repo"])
        (logs / f"sd-{self.bundle['assignment']}.body.md").write_bytes(b"## Summary\n\ncaf\xe9\n")
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended.get("outcome"), "blocked", ended)
        self.assertIn("is not UTF-8 text", ended["blocked"])
        self.assertEqual(self.pulls(), [])

    def test_a_private_repository_publishes_without_the_scan(self):
        """Operator ruling on sd:7868: a private repository's owners see work names already, so no scan runs."""
        (self.plans / "visibility.txt").write_text("private")
        self.press()
        self.write_pr()
        self.plan(claude=["PRIVATE"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended.get("step"), "enqueued", ended)
        self.assertEqual(self.calls("guard"), [])
        self.assertIn(self.bundle["branch"], self.remote_branches().split())
        self.assertEqual(len(self.pulls()), 1)

    def test_an_unread_visibility_scans_as_public(self):
        """Fail closed: a visibility `gh repo view` did not read is public, so the scan runs and blocks."""
        (self.plans / "visibility.txt").write_text("unread")
        self.press()
        self.write_pr()
        self.plan(claude=["PRIVATE"], sd_review=["clean"])
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn("nothing was pushed: a privacy pattern matches the commits", ended["blocked"])
        self.assertNotIn(self.bundle["branch"], self.remote_branches().split())

    def scan_refused(self, title, guard, said):
        self.press()
        self.write_pr(title=title)
        self.plan(claude=["pass"], sd_review=["clean"], guard=guard)
        ended = self.supervisor(checks=lambda work: [PASS]).run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn(said, ended["blocked"])
        self.assertNotIn(self.bundle["branch"], self.remote_branches().split())
        self.assertEqual(self.pulls(), [])

    def test_a_private_title_blocks_before_the_push(self):
        self.scan_refused("feat(fixture): PRIVATE", [], "nothing was pushed: a privacy pattern matches the title")

    def test_a_scan_with_no_pattern_file_blocks_before_the_push(self):
        """Unknown is not clean: a scan that cannot check (no pattern file, exit 3) is a refusal, not a pass."""
        self.scan_refused("feat(fixture): build the fixture", ["none"], "could not check the text (exit 3")

    def test_a_retry_at_enqueuing_reviews_and_tests_before_it_publishes(self):
        """Review of 8fa8c7cca: no lane gate re-checks an unmanaged head, so a retry never publishes it untested."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"], gh=["list", "mute"])
        self.supervisor(checks=lambda work: [PASS]).run()
        row = self.latest()
        build.end(self.db, row["id"], "interrupted", "lost after the create")
        retry = self.latest()
        self.assertEqual(retry["step"], "enqueuing")
        self.assertEqual(self.supervisor(retry["id"], checks=lambda work: [PASS]).run()["step"], "enqueued")
        self.assertEqual((len(self.calls("sd-review")), len(self.calls("gate"))), (2, 2),
                         "the retry reviewed and tested the head it published")

    def test_the_body_lint_skips_a_foreign_docs_tree(self):
        """Review of 8fa8c7cca: `--body-only` in an unmanaged repository; a managed one keeps the tree rules."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.assertEqual(self.supervisor(checks=lambda work: [PASS]).run()["step"], "enqueued")
        (lint,) = self.calls("sd-docs-lint")
        self.assertIn("--body-only", lint["argv"])

    def test_a_managed_run_never_calls_gh_or_its_checks(self):
        self.db.execute("UPDATE repo SET managed = 1")
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.assertEqual(self.supervisor(checks=lambda work: [CHECK]).run()["step"], "enqueued")
        self.assertEqual((self.calls("gh"), self.calls("gate"), self.calls("guard")), ([], [], []))
        self.assertIsNone(build.run(self.db, self.bundle["id"])["detail"])
        self.assertTrue(all("--body-only" not in call["argv"] for call in self.calls("sd-docs-lint")))

    def observed(self, url, state):
        from sd_db import contributions
        with database.transaction(self.db):
            snapshot = contributions.snapshot(self.db, "github:" + url)
            snapshot["observation"] = {"state": state}
            contributions._save(self.db, snapshot)

    def watched(self):
        def lane(row, items):
            raise AssertionError("an unmanaged run reads no lane")
        exited = build.Probe()
        exited.alive = lambda pid: False if pid == os.getpid() else build.Probe.alive(pid)  # type: ignore[method-assign]
        exited.group = lambda pgid: False if pgid == os.getpgrp() else build.Probe.group(pgid)  # type: ignore[method-assign]
        return build_run.watch(self.db, lane=lane, probe=exited)

    def test_the_owners_merge_closes_the_tasks_and_cleans(self):
        self.press(2)
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.supervisor(checks=lambda work: [PASS]).run()
        url = self.pulls()[0]["url"]
        self.assertEqual(self.watched(), [], "unobserved: nothing moves")
        self.observed(url, "open")
        self.assertEqual(self.watched(), [])
        self.observed(url, "merged")
        self.assertEqual(self.watched(), [{"run": self.bundle["id"], "step": "cleaning"}])
        self.assertEqual(self.statuses(), ["done", "done"])
        self.assertEqual(build.run(self.db, self.bundle["id"])["step"], "cleaning", "never checking")
        self.assertEqual(self.supervisor().run()["outcome"], "done")
        self.assertFalse(Path(self.bundle["work_path"]).exists())

    def test_another_pull_requests_merge_moves_nothing(self):
        """Evidence: the watch reads the url the run recorded; a merge observed on any other url is not this run's."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.supervisor(checks=lambda work: [PASS]).run()
        url = self.pulls()[0]["url"]
        self.observed(url.replace("/pull/1", "/pull/2"), "merged")
        self.observed("https://github.com/other/repo/pull/1", "merged")
        self.assertEqual(self.watched(), [])
        self.assertEqual((build.run(self.db, self.bundle["id"])["step"], self.statuses()), ("enqueued", ["in_progress"]))

    def test_a_draft_closed_without_a_merge_ends_lane_failed(self):
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.supervisor(checks=lambda work: [PASS]).run()
        url = self.pulls()[0]["url"]
        self.observed(url, "closed")
        self.assertEqual(self.watched(), [{"run": self.bundle["id"], "outcome": "lane_failed"}])
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((row["outcome"], self.statuses()), ("lane_failed", ["in_progress"]))
        self.assertIn("closed without a merge", row["detail"])

    def test_own_checks_pick_as_sd_check_does(self):
        """The adapter's `own_checks`: the aggregate `check` alone, else each of `test` and `lint`, else none."""
        work = self.root / "checks"
        work.mkdir()
        self.assertEqual(ADAPTER.own_checks(work), [])
        (work / "package.json").write_text(json.dumps({"scripts": {"test": "node t.js", "lint": "eslint ."}}))
        self.assertEqual(len(ADAPTER.own_checks(work)), 2)
        (work / "Makefile").write_text("check:\n\ttrue\ntest:\n\ttrue\n")
        self.assertEqual(ADAPTER.own_checks(work), [["make", "check"]])


def sd_quote(argv):
    import shlex
    return shlex.join(argv)


class Tick(Case):
    def setUp(self):
        super().setUp()
        self.spawned: list[str] = []
        self.entries: dict[int, Any] = {}

    def lane(self, row, items):
        answer = next((self.entries[item] for item in items if item in self.entries), None)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def tick(self, **inputs):
        return build_run.dispatch(self.db, owner="fixture", spawn=self.spawned.append, lane=self.lane,
                                  root=self.root / "worktrees",
                                  **{"load": 1.0, "load_cap": 40.0, "interactive": [], **inputs})

    def enqueued(self, count=1, skip_lead=False):
        self.items = [create_item(self.db, kind="task", title="Built", repo=str(self.repo), status="ready")
                      for _ in range(count)]
        build.queue(self.db, self.items, who="operator")
        self.tick()
        run = next(row for row in build.open_runs(self.db) if row["step"] != "enqueued")  # this item's, not an older
        build.write(self.db, run["id"], step="enqueuing")
        self.assertTrue(build.enqueued(self.db, run["id"], self.items[1:] if skip_lead else None))
        self.spawned.clear()
        return run

    def test_a_tick_claims_spawns_and_beats(self):
        items = [create_item(self.db, kind="task", title="Built", repo=str(self.repo), status="ready")
                 for _ in range(2)]
        build.queue(self.db, items, who="operator")
        result = self.tick()
        self.assertEqual((len(result["claimed"]), result["spawned"]), (1, result["claimed"]))
        beat = self.db.execute("SELECT * FROM state WHERE kind = 'heartbeat' AND key = ?",
                               (build_run.HEARTBEAT,)).fetchone()
        self.assertIsNotNone(beat)
        build.register(self.db, result["claimed"][0], pid=os.getpid(), pgid=os.getpgrp(),
                       start=build.Probe.started(os.getpid()))
        again = self.tick()
        self.assertEqual((again["claimed"], again["spawned"]), ([], []), "a registered run is not spawned again")

    def test_an_unread_load_says_why(self):
        """Pack sd:7933, failure table Claim, load unread: no claim, and the tick names the unread input."""
        build.queue(self.db, [create_item(self.db, kind="task", title="Built", repo=str(self.repo),
                                          status="ready")], who="operator")
        for inputs, why in (({"load": None}, "the load was not read"),
                            ({"interactive": None}, "the interactive builders were not counted")):
            with self.subTest(inputs=inputs):
                result = self.tick(**inputs)
                self.assertEqual((result["claimed"], result["why"]), ([], why))

    def test_an_unread_input_claims_nothing(self):
        build.queue(self.db, [create_item(self.db, kind="task", title="Built", repo=str(self.repo),
                                          status="ready")], who="operator")
        for inputs in ({"load": None}, {"interactive": None}, {"why": "the log folder is not writable"}):
            with self.subTest(inputs=inputs):
                self.assertEqual(self.tick(**inputs)["claimed"], [])
        self.assertEqual(self.spawned, [])

    def test_a_tick_picks_up_ready_items_while_on_and_claims_them(self):
        """pack sd:6097: each tick while on queues ready items as auto-pickup, and the same tick claims them."""
        items = [create_item(self.db, kind="task", title="Ready", repo=str(self.repo), status="ready")
                 for _ in range(2)]
        self.assertEqual(self.tick()["picked"], [], "off picks nothing")
        build.set_pickup(self.db, True)
        self.assertEqual(self.tick(load=None)["picked"], [], "an unread load picks nothing")
        result = self.tick()
        self.assertEqual((result["picked"], len(result["claimed"])), (items, 1))
        self.assertEqual([item_state(self.db, item)["item"]["status"] for item in items], ["in_progress"] * 2)

    def test_a_merged_entry_moves_the_run_to_cleaning(self):
        """Failure table, Lane watch: on `merged` the run goes to cleaning with a fresh beat and gets a supervisor."""
        run = self.enqueued()
        self.entries[self.items[0]] = {"id": "e1", "status": "merged"}
        result = self.tick()
        row = build.run(self.db, run["id"])
        self.assertEqual((row["step"], row["supervisor_pid"], row["outcome"]), ("cleaning", None, None))
        self.assertEqual((result["moved"], self.spawned), ([{"run": run["id"], "step": "cleaning"}], [run["id"]]))

    def test_clean_only_after_merge(self):
        """Failure table, Cleaning removal: a pending or running entry leaves the run at enqueued, unspawned."""
        run = self.enqueued()
        for status in ("pending", "running"):
            self.entries[self.items[0]] = {"id": "e1", "status": status}
            self.tick()
            self.assertEqual(build.run(self.db, run["id"])["step"], "enqueued")
        self.assertEqual(self.spawned, [])

    def test_a_failed_entry_ends_the_run_lane_failed(self):
        run = self.enqueued()
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": "gate red"}
        self.tick()
        row = build.run(self.db, run["id"])
        self.assertEqual((row["outcome"], row["released_at"] is not None), ("lane_failed", True))
        self.assertIn("gate red", row["detail"])
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.statuses(), ["in_progress"], "only a catch-up conflict goes back to a builder")

    CONFLICT = ("merging origin/main into build/sd-1 conflicts; the branch is unchanged; "
                "sd-ship lane retry --item 1 queues it again")

    def test_a_catch_up_conflict_goes_back_to_its_builder(self):
        """pack sd:6940: a conflict with main alone queues the bundle again under its lead, with a finding."""
        run = self.enqueued()
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": self.CONFLICT}
        result = self.tick()
        self.assertEqual(build.run(self.db, run["id"])["outcome"], "lane_failed")
        self.assertEqual(self.statuses(), ["in_progress"], "the same tick claims it again")
        again = build.run(self.db, result["claimed"][0])
        self.assertEqual((again["assignment"], again["branch"]), (run["assignment"], run["branch"]))
        notes = [row["body"] for row in self.db.execute("SELECT body FROM note WHERE item = ?", self.items)]
        self.assertTrue(any(body.startswith(build.REBUILD) for body in notes), notes)

    def test_a_rebuild_names_the_base_that_conflicted(self):
        """pack sd:7008: the finding said origin/main whatever base the lane merged."""
        self.enqueued()
        self.entries[self.items[0]] = {"id": "e1", "status": "failed",
                                       "reason": self.CONFLICT.replace("origin/main", "origin/release")}
        self.tick()
        notes = [row["body"] for row in self.db.execute("SELECT body FROM note WHERE item = ?", self.items)]
        finding = next(body for body in notes if body.startswith(build.REBUILD))
        self.assertIn("Merge origin/release into build/sd-1", finding)
        self.assertNotIn("origin/main", finding.split("the lane said:")[0])

    def test_a_conflict_waits_until_its_supervisor_is_gone(self):
        """Review: a requeue while the old supervisor still ran let a new builder share its worktree."""
        run = self.enqueued()
        build.register(self.db, run["id"], pid=os.getpid(), pgid=os.getpgrp(),
                       start=build.Probe.started(os.getpid()))
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": self.CONFLICT}
        self.assertEqual(self.tick()["claimed"], [], "a live supervisor holds the worktree")
        self.assertIsNone(build.run(self.db, run["id"])["outcome"])
        gone = build.Probe()
        gone.alive = lambda pid: False  # type: ignore[method-assign]
        gone.group = lambda pgid: False  # type: ignore[method-assign]
        self.assertNotEqual(self.tick(probe=gone)["claimed"], [], "once gone, the bundle goes back to a builder")
        self.assertEqual(build.run(self.db, run["id"])["outcome"], "lane_failed")

    def test_a_conflict_without_its_lead_is_left_to_the_operator(self):
        """Review: a rebuild that lost its lead would claim under a child's branch, so none is queued."""
        for case in ("moved", "skipped"):
            with self.subTest(case=case):
                run = self.enqueued(2, skip_lead=case == "skipped")  # a skipped lead ends blocked
                lead, child = self.items
                if case == "moved":
                    writes.transition(self.db, lead, "planning", who="operator", reason="moved on")
                self.entries = {child: {"id": "e1", "status": "failed", "reason": self.CONFLICT}}
                self.assertEqual(self.tick()["claimed"], [])
                self.assertEqual(build.run(self.db, run["id"])["outcome"], "lane_failed")
                self.assertEqual(item_state(self.db, child)["item"]["status"], "in_progress")
                self.assertEqual(self.db.execute("SELECT status FROM assignment WHERE item = ?",
                                                 (child,)).fetchone()[0], "done")

    def test_conflict_rebuilds_stop_at_their_cap(self):
        """pack sd:6940: past `CONFLICT_REBUILDS` the bundle stays lane_failed for the operator."""
        run = self.enqueued()
        for _ in range(build.CONFLICT_REBUILDS):
            self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": self.CONFLICT}
            claimed = self.tick()["claimed"][0]
            build.write(self.db, claimed, step="enqueuing")
            self.assertTrue(build.enqueued(self.db, claimed))
        self.tick()
        self.assertEqual(self.tick()["claimed"], [])
        self.assertEqual(self.statuses(), ["in_progress"])
        self.assertEqual(self.db.execute("SELECT status FROM assignment WHERE id = ?",
                                         (run["assignment"],)).fetchone()[0], "done")

    def test_every_lane_end_short_of_a_merge_ends_the_run(self):
        """Review round 1: the lane's own list of ends short of a merge, and a cancel, each end the run."""
        for status in (*LANE.BLOCKED, "cancelled"):
            with self.subTest(status=status):
                run = self.enqueued()
                self.entries[self.items[0]] = {"id": "e1", "status": status, "reason": "stopped"}
                self.tick()
                self.assertEqual(build.run(self.db, run["id"])["outcome"], "lane_failed")

    def test_an_unread_lane_changes_nothing(self):
        run = self.enqueued()
        self.entries[self.items[0]] = build_run.Unread("hub unavailable")
        self.tick()
        row = build.run(self.db, run["id"])
        self.assertEqual((row["step"], row["outcome"], row["released_at"]), ("enqueued", None, None))


class Sweep(Case):
    """pack sd:6909: a blocked or cancelled build's worktree goes once every item it names is done."""

    def blocked(self):
        self.press()
        self.plan(claude=["deny"])
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "denied")  # pack sd:6723: the first denial requeues once
        self.plan(claude=["deny"])
        self.assertEqual(self.supervisor(ended["retry"]).run()["outcome"], "blocked")
        self.db.execute("UPDATE runner_run SET released_at = ?", (build_run.now(),))
        self.db.commit()
        return Path(self.bundle["work_path"])

    def done(self):
        self.db.execute("UPDATE item SET status = 'done' WHERE id = ?", (self.items[0],))
        self.db.commit()

    def branches(self):
        return support.git(self.repo, "branch", "--list", self.bundle["branch"])

    def test_a_blocked_build_whose_items_are_done_is_removed(self):
        work = self.blocked()
        self.done()
        head = support.git(self.repo, "rev-parse", self.bundle["branch"])
        self.assertEqual(build_run.sweep(self.db), [str(work)])
        self.assertFalse(work.exists())
        self.assertEqual(self.branches(), "")
        self.assertEqual(build_run.sweep(self.db), [], "a removed worktree is not swept again")
        # Review round 1: item status is no proof the commits landed, so the head stays reachable.
        kept = f"refs/sd-swept/{self.bundle['branch']}"
        self.assertEqual(support.git(self.repo, "rev-parse", kept), head)
        note = self.db.execute("SELECT body FROM note WHERE item = ? ORDER BY id DESC", (self.items[0],)).fetchone()
        self.assertIn(kept, note["body"])

    def test_a_head_off_its_branch_is_not_removed(self):
        """A worktree whose HEAD left its branch holds commits the branch's ref would not keep."""
        work = self.blocked()
        self.done()
        support.git(work, "checkout", "-q", "--detach")
        support.git(work, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "fix: stray")
        self.assertEqual(build_run.sweep(self.db), [])
        self.assertTrue(work.is_dir())

    def test_a_branch_that_cannot_be_kept_is_not_removed(self):
        work = self.blocked()
        self.done()
        support.git(self.repo, "update-ref", f"refs/sd-swept/{self.bundle['branch']}/x", "HEAD")
        self.assertEqual(build_run.sweep(self.db), [], "a ref in the way: nothing is removed")
        self.assertTrue(work.is_dir())
        self.assertNotEqual(self.branches(), "")

    def test_the_tick_sweeps(self):
        work = self.blocked()
        self.done()
        result = build_run.dispatch(self.db, owner="fixture", spawn=lambda ident: None, lane=lambda row, items: None,
                                    load=1.0, load_cap=40.0, interactive=[], root=self.root / "worktrees")
        self.assertEqual(result["swept"], [str(work)])

    def test_sweep_leaves_another_hosts_worktree(self):
        """Pack sd:7933: a done bundle's worktree another host's run names is on that host's disk."""
        work = self.blocked()
        self.done()
        self.db.execute("UPDATE runner_run SET owner = 'fixture@work'")
        self.db.commit()
        self.assertEqual(build_run.sweep(self.db, hosts={None, "hub"}), [])
        self.assertTrue(work.is_dir())
        self.assertEqual(build_run.sweep(self.db, hosts={"work"}), [str(work)])

    def test_an_open_item_keeps_the_worktree(self):
        work = self.blocked()
        self.assertEqual(build_run.sweep(self.db), [])
        self.assertTrue(work.is_dir())
        self.assertNotEqual(self.branches(), "")

    def test_a_dirty_or_held_or_open_worktree_is_kept(self):
        work = self.blocked()
        self.done()
        (work / "stray.txt").write_text("x")
        self.assertEqual(build_run.sweep(self.db), [])
        (work / "stray.txt").unlink()
        sessions.beat(self.db, "session-other", machine=this_host(), cwd=None,
                      found=sessions.worktrees([str(work)]))
        self.assertEqual(build_run.sweep(self.db), [])
        self.db.execute("DELETE FROM state WHERE kind = 'heartbeat'")
        self.db.execute("UPDATE runner_run SET released_at = NULL")
        self.db.commit()
        self.assertEqual(build_run.sweep(self.db), [], "an unreleased run still owns it")
        self.assertTrue(work.is_dir())

    def test_an_ignored_file_keeps_the_worktree(self):
        """Lane review: `git worktree remove` deletes ignored files, and plain `status --porcelain` omits them."""
        work = self.blocked()
        self.done()
        common = Path(support.git(work, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        (common / "info").mkdir(exist_ok=True)
        (common / "info" / "exclude").write_text(".env\n")
        (work / ".env").write_text("TOKEN=change-me\n")
        self.assertEqual(support.git(work, "status", "--porcelain"), "", "the plain read sees a clean tree")
        self.assertEqual(build_run.sweep(self.db), [])
        self.assertEqual((work / ".env").read_text(), "TOKEN=change-me\n")
        self.assertNotEqual(self.branches(), "")

    def test_a_tree_with_only_caches_is_swept(self):
        """pack sd:6979: a disposable cache is no reason to keep a tree; another ignored file still is."""
        work = self.blocked()
        self.done()
        exclude(work, "__pycache__/\n.ruff_cache/\n.env\n")
        (work / "lib" / "__pycache__").mkdir(parents=True)
        (work / "lib" / "__pycache__" / "x.pyc").write_bytes(b"x")
        (work / ".ruff_cache").mkdir()
        (work / ".ruff_cache" / "CACHEDIR.TAG").write_text("x")
        (work / ".env").write_text("TOKEN=change-me\n")
        self.assertEqual(build_run.sweep(self.db), [], "the .env keeps it")
        (work / ".env").unlink()
        self.assertEqual(build_run.sweep(self.db), [str(work)])
        self.assertFalse(work.exists())

    def test_an_ignored_file_named_like_a_cache_still_keeps_the_tree(self):
        """Lane review of pack sd:6979: only a folder of that name is disposable, not a file."""
        work = self.blocked()
        exclude(work, ".pytest_cache\n")
        (work / ".pytest_cache").write_text("notes\n")
        self.assertEqual(build_run.kept_files(("git",), str(work)), "!! .pytest_cache")

    def test_an_untracked_file_hidden_by_config_keeps_the_worktree(self):
        work = self.blocked()
        self.done()
        support.git(self.repo, "config", "status.showUntrackedFiles", "no")
        (work / "notes.txt").write_text("x")
        self.assertEqual(build_run.sweep(self.db), [])
        self.assertTrue((work / "notes.txt").exists())

    def test_a_locked_worktree_is_kept(self):
        work = self.blocked()
        self.done()
        support.git(self.repo, "worktree", "lock", str(work))
        self.assertEqual(build_run.sweep(self.db), [])
        self.assertTrue(work.is_dir())


class Archive(Case):
    """pack sd:8246: a run writes its logs to a local folder; the tick moves an ended run's files to the bulk root."""

    def test_the_default_local_root_is_the_state_folder(self):
        self.assertEqual(build_run.local_root({"HOME": "/home/operator"}),
                         Path("/home/operator/.local/state/sd/builds"))
        self.assertEqual(build_run.local_root({"XDG_STATE_HOME": "/state"}), Path("/state/sd/builds"))

    def test_an_ended_runs_quiet_files_move_and_the_row_names_the_new_log(self):
        self.press()
        ident, lead = self.bundle["id"], self.bundle["assignment"]
        local = self.root / "local"
        folder = build_run.log_dir(local, str(self.repo))
        folder.mkdir(parents=True)
        names = [f"{ident}.jsonl", f"{ident}.supervisor.log", f"sd-{lead}.title", f"sd-{lead}.body.md"]
        for name in [*names, "stray.log"]:
            (folder / name).write_text("x")
            os.utime(folder / name, (0, 0))
        self.db.execute("UPDATE runner_run SET log_path = ? WHERE id = ?", (str(folder / names[0]), ident))
        self.db.commit()
        self.assertEqual(build_run.archive(self.db, local, self.logs), [], "an open run's files stay")
        self.db.execute("UPDATE runner_run SET outcome = 'blocked' WHERE id = ?", (ident,))
        self.db.commit()
        # Review round 2: a blocked run holds its lease, and maybe live groups, until the reap releases it.
        self.assertEqual(build_run.archive(self.db, local, self.logs), [], "an unreleased run's files stay")
        self.db.execute("UPDATE runner_run SET released_at = ? WHERE id = ?", (build_run.now(), ident))
        self.db.commit()
        (folder / names[1]).touch()  # written just now: the supervisor may still be printing
        target = build_run.log_dir(self.logs, str(self.repo))
        moved = build_run.archive(self.db, local, self.logs)
        self.assertEqual(sorted(moved), sorted(str(target / name) for name in names if name != names[1]))
        self.assertEqual(sorted(path.name for path in folder.iterdir()), sorted([names[1], "stray.log"]))
        log = self.db.execute("SELECT log_path FROM runner_run WHERE id = ?", (ident,)).fetchone()[0]
        self.assertEqual(log, str(target / names[0]))

    def test_a_failed_row_update_keeps_the_local_log_for_the_next_tick(self):
        """Review round 1: the move deleted the local file before `log_path` changed, so a failed write left
        the row naming a file no later tick could find."""
        self.press()
        ident, local = self.bundle["id"], self.root / "local"
        folder = build_run.log_dir(local, str(self.repo))
        folder.mkdir(parents=True)
        log = folder / f"{ident}.jsonl"
        log.write_text("x")
        os.utime(log, (0, 0))
        self.db.execute("UPDATE runner_run SET outcome = 'done', released_at = ?, log_path = ? WHERE id = ?",
                        (build_run.now(), str(log), ident))
        self.db.commit()
        with mock.patch.object(build_run, "transaction", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                build_run.archive(self.db, local, self.logs)
        self.assertTrue(log.exists(), "the local log stays until the row names the moved one")
        target = build_run.log_dir(self.logs, str(self.repo)) / log.name
        self.assertEqual(build_run.archive(self.db, local, self.logs), [str(target)])
        self.assertFalse(log.exists())
        self.assertEqual(self.db.execute("SELECT log_path FROM runner_run WHERE id = ?", (ident,)).fetchone()[0],
                         str(target))

    def test_a_resumed_supervisor_keeps_the_log_root_it_started_with(self):
        """Review round 2: a supervisor that execs in place keeps its child, which writes to the first image's
        files; the new image must read the same ones, and one from before pack sd:8246 used the bulk root."""
        self.press()
        ident, local = self.bundle["id"], self.root / "local"
        self.assertEqual(self.supervisor().state()["root"], str(self.logs))
        with mock.patch.object(build_run, "local_root", return_value=local):
            self.assertEqual(build_run.resume_root(ident, self.logs), local, "a fresh start logs locally")
            for handed, root in ((resume_state(ident), self.logs), ({**resume_state(ident), "root": "/kept"},
                                                                     Path("/kept"))):
                with mock.patch.dict(os.environ, {build_run.RESUME_ENV: json.dumps(handed)}):
                    self.assertEqual(build_run.resume_root(ident, self.logs), root)
                    supervisor = self.supervisor(log_root=build_run.resume_root(ident, self.logs))
                self.assertEqual(supervisor.log, build_run.log_dir(root, str(self.repo)) / f"{ident}.jsonl")

    def ended(self, name: str) -> tuple[Path, Path]:
        """An ended, released run with one quiet local file `name`; (that file, where the archive puts it)."""
        self.press()
        local = self.root / "local"
        folder = build_run.log_dir(local, str(self.repo))
        folder.mkdir(parents=True)
        path = folder / name.format(run=self.bundle["id"])
        path.write_text("x")
        os.utime(path, (0, 0))
        self.db.execute("UPDATE runner_run SET outcome = 'blocked', released_at = ?, log_path = ?, detail = ? "
                        "WHERE id = ?", (build_run.now(), str(path), f"refused; log {path}", self.bundle["id"]))
        self.db.commit()
        return path, build_run.log_dir(self.logs, str(self.repo)) / path.name

    def test_notes_and_end_messages_name_the_archived_log(self):
        """sd:8275: the move rewrote only `log_path`; a note or end message still named the local file it removed."""
        path, target = self.ended("{run}.jsonl")
        item = self.items[0]
        add_note(self.db, item, "followup", f"Build blocked: refused; log {path}. Look at the log.")
        self.db.execute("UPDATE assignment SET result = ? WHERE id = ?", (f"refused; log {path}",
                                                                           self.bundle["assignment"]))
        self.db.commit()
        self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [str(target)])
        bodies = [row[0] for row in self.db.execute("SELECT body FROM note WHERE item = ?", (item,))]
        self.assertNotIn(str(path), "\n".join(bodies))
        self.assertIn(f"Build blocked: refused; log {target}. Look at the log.", bodies)
        for sql in ("SELECT detail FROM runner_run WHERE id = ?", "SELECT result FROM assignment WHERE id = ?"):
            key = self.bundle["id"] if "runner_run" in sql else self.bundle["assignment"]
            self.assertEqual(self.db.execute(sql, (key,)).fetchone()[0], f"refused; log {target}")

    def test_a_hung_bulk_root_does_not_stall_the_tick(self):
        """sd:8275: the copy ran on the tick's thread, so a hung network mount held dispatch past its timer."""
        path, target = self.ended("{run}.jsonl")
        release = threading.Event()
        self.addCleanup(release.set)

        def hung(*_args, **_kwargs):
            release.wait(30)
            raise OSError("the mount came back too late")

        with mock.patch.object(build_run.shutil, "copy2", side_effect=hung):
            started = time.monotonic()
            self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs, deadline=0.3), [])
            self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(path.exists(), "a copy that did not finish leaves the local file")
        self.assertEqual(self.db.execute("SELECT log_path FROM runner_run WHERE id = ?",
                                         (self.bundle["id"],)).fetchone()[0], str(path))
        release.set()
        self.join_archive_workers()
        self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [str(target)])

    @staticmethod
    def join_archive_workers():
        for worker in threading.enumerate():
            if worker.name == "sd-build-archive":
                worker.join(5)

    def test_a_tick_starts_no_copy_while_an_earlier_one_runs(self):
        """sd:8275 lane round 1: a copy past the deadline kept running, and the next tick started a second
        copy of the same file to the same destination: two writers, each opening it with truncation."""
        path, target = self.ended("{run}.jsonl")
        release = threading.Event()
        self.addCleanup(release.set)
        calls = []

        def hung(source, destination, *_args, **_kwargs):
            calls.append(destination)
            release.wait(30)
            raise OSError("the mount came back too late")

        with mock.patch.object(build_run.shutil, "copy2", side_effect=hung):
            self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs, deadline=0.3), [])
            self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs, deadline=0.3), [])
            self.assertEqual(len(calls), 1, "a tick that finds a copy in flight starts no other")
            release.set()
            self.join_archive_workers()
        self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [str(target)])
        self.assertEqual(target.read_text(), "x")
        self.assertFalse(path.exists())

    def test_a_failed_copy_never_truncates_the_archived_log(self):
        """sd:8275 lane round 1: a copy opened the archive itself with truncation, so a late or failed copy
        could empty the one complete log after the rows named it and the local file went."""
        path, target = self.ended("{run}.jsonl")
        target.parent.mkdir(parents=True)
        target.write_text("the complete log")

        def broken(source, destination, *_args, **_kwargs):
            Path(destination).write_text("")  # opened with truncation, then the mount fails
            raise OSError("the mount went away")

        with mock.patch.object(build_run.shutil, "copy2", side_effect=broken):
            self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [])
        self.assertEqual(target.read_text(), "the complete log")
        self.assertTrue(path.exists())
        self.assertEqual(sorted(entry.name for entry in target.parent.iterdir()), [target.name],
                         "a failed copy leaves no partial file beside the archive")

    def test_a_copy_an_earlier_tick_finished_is_collected_not_copied_again(self):
        """sd:8275 lane round 1: a copy done after its tick ended was copied again by every later tick; a slow
        mount then never caught up, and each copy reopened the archive."""
        path, target = self.ended("{run}.jsonl")
        target.parent.mkdir(parents=True)
        shutil.copy2(path, target)
        with mock.patch.object(build_run.shutil, "copy2", side_effect=OSError("no copy expected")):
            self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [str(target)])
        self.assertFalse(path.exists())
        self.assertEqual(self.db.execute("SELECT log_path FROM runner_run WHERE id = ?",
                                         (self.bundle["id"],)).fetchone()[0], str(target))

    def test_an_unwritable_bulk_root_leaves_the_files_for_the_next_tick(self):
        self.press()
        local = self.root / "local"
        folder = build_run.log_dir(local, str(self.repo))
        folder.mkdir(parents=True)
        log = folder / f"{self.bundle['id']}.jsonl"
        log.write_text("x")
        os.utime(log, (0, 0))
        self.db.execute("UPDATE runner_run SET outcome = 'done', released_at = ?", (build_run.now(),))
        self.db.commit()
        self.logs.write_text("a file, not a folder")
        self.assertEqual(build_run.archive(self.db, local, self.logs), [])
        self.assertTrue(log.exists())


class Drafts(Case):
    """The draft supervisor and the tick's draft claim (pack sd:6361), with the fake `claude`."""

    def queue(self):
        self.items = [create_item(self.db, kind="task", title="Draft me", body={"text": "The capture's one line."},
                                  repo=str(self.repo), status="planning")]
        self.assignment = draft.queue_if_eligible(self.db, self.items[0], who="dashboard")
        assert self.assignment is not None

    def claimed(self):
        self.queue()
        run = draft.claim(self.db, owner="fixture", available=build.room(self.db, load=1.0, load_cap=40.0,
                                                                         interactive=[]))
        assert run is not None
        return run

    def drafter(self, ident, **options):
        options = {"beat_seconds": 0.2, "timeout": 30, "grace": 0.5, **options}
        return build_run.DraftSupervisor(self.db, ident, tools=self.tools, log_root=self.logs, environ=self.env,
                                         **options)

    def notes(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM note WHERE item = ? ORDER BY id", self.items)]

    def assignment_status(self):
        return self.db.execute("SELECT status FROM assignment WHERE id = ?", (self.assignment,)).fetchone()[0]

    def test_a_draft_run_writes_its_spec_and_questions(self):
        run = self.claimed()
        self.plan(claude=["draft"])
        ended = self.drafter(run["id"]).run()
        self.assertEqual(ended, {"run": run["id"], "outcome": "done", "questions": 1})
        notes = [note for note in self.notes() if note["session"] == f"draft:{run['id']}"]
        self.assertEqual([note["kind"] for note in notes], ["comment", "question"])
        self.assertTrue(notes[0]["body"].startswith("Draft spec:"))
        self.assertEqual(notes[1]["body"], "Which?\nOption: A (Recommended)\nOption: B")
        self.assertEqual((self.assignment_status(), self.statuses()), ("done", ["planning"]))
        row = build.run(self.db, run["id"])
        self.assertEqual((row["outcome"], row["step"], row["session_id"]), ("done", "drafting", "session-fixture"))
        cost = self.db.execute("SELECT * FROM cost WHERE pass = ?", (run["id"],)).fetchone()
        self.assertEqual((cost["usd"], cost["assignment"]), (0.31, self.assignment))
        prompt = self.calls("claude")[0]["argv"][1]
        self.assertIn(f"sd:{self.items[0]}", prompt)
        self.assertIn("The capture's one line.", prompt)

    def test_a_draft_run_denies_write_tools(self):
        """Failure table, Drafting: the run holds the read tools only, and a denial writes nothing."""
        run = self.claimed()
        self.plan(claude=["draft-deny"])
        before = self.notes()
        ended = self.drafter(run["id"]).run()
        call = self.calls("claude")[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--tools"):argv.index("--tools") + 4], ["--tools", "Read", "Grep", "Glob"])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("auto", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual((argv[argv.index("--max-turns") + 1], argv[argv.index("--model") + 1]), ("60", "opus"))
        self.assertEqual(Path(call["cwd"]).resolve(), self.repo.resolve(), "it reads the repository's checkout")
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn("permission denied: Write", build.run(self.db, run["id"])["detail"])
        self.assertEqual((self.notes(), self.assignment_status(), self.statuses()), (before, "blocked", ["planning"]))

    def test_an_unparsed_reply_writes_no_note(self):
        """Failure table, Write: a reply with no `## Spec` is no draft; the run ends blocked with its log."""
        run = self.claimed()
        self.plan(claude=["draft-bad"])
        before = self.notes()
        ended = self.drafter(run["id"]).run()
        self.assertEqual(ended["outcome"], "blocked")
        row = build.run(self.db, run["id"])
        self.assertIn(str(build_run.log_dir(self.logs, run["repo"]) / f"{run['id']}.jsonl"), row["detail"])
        self.assertEqual((self.notes(), self.assignment_status(), self.statuses()), (before, "blocked", ["planning"]))
        self.assertIsNone(row["released_at"], "held until the reap reads the supervisor gone")
        self.assertEqual(draft.reap(self.db, probe=GoneProbe(alive=False, group=False)), [])
        self.assertIsNotNone(build.run(self.db, run["id"])["released_at"])

    def test_a_hung_draft_is_killed_and_retried_once(self):
        """Failure table, Drafting: the bound kills the group; a fresh run retries once, then a followup blocks it."""
        run = self.claimed()
        self.plan(claude=["hang", "hang"])
        ended = self.drafter(run["id"], timeout=1.5).run()
        self.assertEqual(ended["outcome"], "timed_out")
        self.assertTrue(group_gone(build.run(self.db, run["id"])["builder_pgid"]))
        retry = build.run(self.db, ended["retry"])
        self.assertEqual((retry["step"], retry["run"], retry["session_id"]), ("claimed", 2, None))
        again = self.drafter(retry["id"], timeout=1.5).run()
        self.assertEqual((again["outcome"], again["retry"]), ("timed_out", None))
        self.assertEqual((self.assignment_status(), self.statuses()), ("blocked", ["planning"]))
        followups = [note["body"] for note in self.notes() if note["kind"] == "followup"]
        self.assertEqual(len(followups), 1)
        self.assertIn("Draft blocked:", followups[0])
        self.assertEqual(draft.open_runs(self.db), [])

    def test_a_draft_run_keeps_its_child_through_a_hub_fault(self):
        """pack sd:8182: the draft supervisor shares the child and the beat, so it shares the fault."""
        run = self.claimed()
        self.plan(claude=["draft"])
        fired: list[str] = []
        real = build.beat

        def beat(connection, ident):
            if not fired:
                fired.append(ident)
                raise remote.HubUnreachable("hub.example.test", 8769, "timed out")
            return real(connection, ident)

        with mock.patch.object(build, "beat", beat):
            ended = self.drafter(run["id"], beat_seconds=0.01).run()
        self.assertEqual(ended["outcome"], "done")

    def test_a_tick_claims_and_spawns_one_draft(self):
        self.queue()
        spawned: list[str] = []
        tick: dict[str, Any] = {"owner": "fixture", "spawn": spawned.append, "lane": lambda *_: None, "root": self.root / "worktrees",
                "load": 1.0, "load_cap": 40.0, "interactive": []}
        result = build_run.dispatch(self.db, **tick)
        self.assertEqual((result["claimed"], len(result["drafted"]), spawned), ([], 1, result["drafted"]))
        self.assertEqual(build_run.dispatch(self.db, **tick)["drafted"], [], "one draft at a time")
        self.assertEqual(build_run.dispatch(self.db, **{**tick, "load": None})["drafted"], [])

    def test_every_hub_read_before_the_draft_run_loop_is_retried_with_the_child_kept(self):
        """Round 2: `DraftSupervisor` has its own constructor and its own reads."""
        self.assert_start_recovers(self.claimed()["id"])

    def test_supervise_routes_a_draft_run_to_the_draft_supervisor(self):
        run = self.claimed()
        seen = []

        class Fake:
            def __init__(self, connection, ident, **options):
                seen.append((type(self).__name__, ident, sorted(options)))

            def run(self):
                return {"run": seen[-1][1]}

        args = mock.Mock(run=run["id"])
        with mock.patch("sys.path", [str(BIN), *sys.path]), \
                mock.patch.object(ADAPTER, "log_root", return_value=self.logs), \
                mock.patch("sd_db.connect", return_value=connect(self.database)), \
                mock.patch.object(build_run, "DraftSupervisor", Fake), \
                mock.patch("builtins.print"):
            ADAPTER.supervise_build(args)
        self.assertEqual(seen, [("Fake", run["id"], ["log_root", "tools"])])


class Spawn(unittest.TestCase):
    def test_a_supervisor_survives_the_job_group(self):
        """Risk, launchd: a spawn leaves the job's process group, so killing that group spares it."""
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "supervisor.log"
            job = subprocess.Popen(
                [sys.executable, "-c", textwrap.dedent(f'''
                    import sys, time
                    sys.path[:0] = {sys.path!r}
                    from pathlib import Path
                    from sd_db import build_run
                    print(build_run.spawn([sys.executable, "-c", "import time; time.sleep(30)"],
                                          log=Path({str(log)!r})), flush=True)
                    time.sleep(30)
                ''')], stdout=subprocess.PIPE, text=True, start_new_session=True)
            assert job.stdout is not None
            try:
                pid = int(job.stdout.readline())
            finally:
                os.killpg(job.pid, signal.SIGKILL)
                job.wait()
                job.stdout.close()
            try:
                time.sleep(0.2)
                os.kill(pid, 0)
                self.assertNotEqual(os.getpgid(pid), job.pid)
            finally:
                os.killpg(pid, signal.SIGKILL)


class Hosts(Case):
    """Pack sd:7933: the hub's tick and a satellite's each act on their own lanes' runs only."""

    def setUp(self):
        super().setUp()
        self.spawned: list[str] = []
        self.entries: dict[int, Any] = {}
        self.satellite = self.root / "satellite"
        upsert_repo(self.db, str(self.satellite), remote="https://github.com/fixture/sat.git", managed=1)
        self.db.execute("UPDATE repo SET lane_host = 'work' WHERE path = ?", (str(self.satellite),))
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={})
        self.db.commit()
        self.asked: list[int] = []

    def lane(self, row, items):
        self.asked += items
        return next((self.entries[item] for item in items if item in self.entries), None)

    def tick(self, **inputs):
        return build_run.dispatch(self.db, owner="fixture", spawn=self.spawned.append, lane=self.lane,
                                  root=self.root / "worktrees",
                                  **{"load": 1.0, "load_cap": 40.0, "interactive": [], **inputs})

    def on(self, host):
        """Run the next tick as the hub (`hub`) or as the satellite `work`: this_host, hub.json and served_by stubbed.

        `hub.read` too: `build.hub_host` reads the file, and the satellite's must not be whatever this HOME holds
        (pack sd:7955)."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(build, "this_host", return_value=host))
        stack.enter_context(mock.patch("sd_db.hub.configured", return_value=host != "hub"))
        satellite = hub.Hub(host="hub.example.test", port=7000, config=self.root / "hub.json")
        stack.enter_context(mock.patch("sd_db.hub.read", return_value=None if host == "hub" else satellite))
        stack.enter_context(mock.patch.object(build, "served_by",
                                              return_value=None if host == "hub" else "hub.example.test:7000"))
        return stack

    def both(self):
        hub = create_item(self.db, kind="task", title="Hub", repo=str(self.repo), status="ready")
        sat = create_item(self.db, kind="task", title="Satellite", repo=str(self.satellite), status="ready")
        build.queue(self.db, [hub, sat], who="operator")
        return hub, sat

    def test_each_tick_spawns_only_its_own_runs(self):
        self.both()
        with self.on("hub"):
            hub = self.tick()
        self.assertEqual((len(hub["claimed"]), hub["spawned"]), (1, hub["claimed"]))
        self.assertEqual(build.run(self.db, hub["claimed"][0])["repo"], str(self.repo))
        self.spawned.clear()
        with self.on("work"):
            work = self.tick()
        self.assertEqual((len(work["claimed"]), work["spawned"]), (1, work["claimed"]), "the hub's run is not spawned")
        self.assertEqual(build.run(self.db, work["claimed"][0])["owner"], "fixture@work")
        with self.on("hub"):
            self.assertEqual(self.tick()["spawned"], hub["claimed"], "nor the satellite's on the hub")

    def test_a_tick_that_cannot_claim_says_why_in_its_beat(self):
        """Review round 4: a satellite whose tick claims nothing beats with the reason, and the hub refuses its
        presses with it, rather than read the beat as a dispatcher that would claim. An unread input lasts one
        tick, so its press queues (sd:8055)."""
        def beat():
            row = self.db.execute("SELECT body FROM state WHERE kind = 'heartbeat' AND key = 'build-dispatch@work' "
                                  "ORDER BY id DESC LIMIT 1").fetchone()
            return json.loads(row["body"]).get("why")
        unset = "sd.bulk_storage_root is unset, so a build run has no log folder"
        for inputs, why in (({"why": unset}, unset), ({"load": None}, build.UNREAD[0]),
                            ({"interactive": None}, build.UNREAD[1]), ({}, "")):
            with self.subTest(inputs=inputs):
                sat = create_item(self.db, kind="task", title="Satellite", repo=str(self.satellite), status="ready")
                with self.on("work"):
                    self.assertEqual(self.tick(**inputs)["why"], why)
                self.assertEqual(beat(), why)
                with self.on("hub"):
                    if why and why not in build.UNREAD:
                        with self.assertRaisesRegex(build.BuildRefused, f"build-dispatch@work claims nothing: {why}"):
                            build.queue(self.db, [sat], who="operator")
                    else:
                        self.assertEqual(len(build.queue(self.db, [sat], who="operator")), 1)
        record_state(self.db, "restore", body={})
        with self.on("work"):
            self.assertEqual(self.tick()["why"], build.RESTORING)
        self.assertEqual(beat(), build.RESTORING)

    def test_each_tick_watches_only_its_own_runs(self):
        sat = create_item(self.db, kind="task", title="Satellite", repo=str(self.satellite), status="ready")
        build.queue(self.db, [sat], who="operator")
        with self.on("work"):
            run = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                              root=self.root / "worktrees")
        assert run is not None
        build.write(self.db, run["id"], step="enqueuing")
        self.assertTrue(build.enqueued(self.db, run["id"]))
        self.entries[sat] = {"id": "e1", "status": "merged"}
        with self.on("hub"):
            self.assertEqual(self.tick()["moved"], [])
        self.assertEqual((self.asked, build.run(self.db, run["id"])["step"]), ([], "enqueued"))
        with self.on("work"):
            self.assertEqual(self.tick()["moved"], [{"run": run["id"], "step": "cleaning"}])

    def test_a_satellite_tick_runs_no_draft_or_pickup(self):
        build.set_pickup(self.db, True)
        ready = create_item(self.db, kind="task", title="Ready", repo=str(self.repo), status="ready")
        planned = create_item(self.db, kind="task", title="Draft", repo=str(self.repo), status="planning")
        self.assertIsNotNone(draft.queue_if_eligible(self.db, planned, who="dashboard"))
        with self.on("work"):
            result = self.tick()
        self.assertEqual((result["picked"], result["drafted"], result["claimed"]), ([], [], []))
        self.assertEqual(item_state(self.db, ready)["item"]["status"], "ready")
        self.assertIsNone(draft.queue_if_eligible(
            self.db, create_item(self.db, kind="task", title="Sat", repo=str(self.satellite), status="planning"),
            who="dashboard"), "a satellite lane takes no draft")

    def test_a_satellite_tick_beats_its_own_key(self):
        with self.on("work"):
            self.tick()
        keys = [row[0] for row in self.db.execute("SELECT key FROM state WHERE kind = 'heartbeat'")]
        self.assertEqual(set(keys), {f"{build_run.HEARTBEAT}@work"}, "the hub's beat is the hub's job's alone")

    def test_the_hub_over_the_wire_still_pushes(self):
        """Review round 1: a served connection on a machine with no hub.json is the hub's, and the hub pushes."""
        hub, _ = self.both()
        with self.on("hub"):
            run = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                              root=self.root / "worktrees")
        assert run is not None
        build.end(self.db, run["id"], "blocked", "the review left blockers")
        notify = self.root / "notify"
        notify.write_text("#!/bin/sh\nexit 0\n")
        notify.chmod(0o755)
        environ = {**os.environ, "SD_NOTIFY": str(notify)}
        with mock.patch.object(database, "served_by", return_value="127.0.0.1:7000"), \
                mock.patch.object(build, "served_by", return_value="127.0.0.1:7000"), \
                mock.patch("sd_db.hub.configured", return_value=False):
            self.assertEqual(ADAPTER.push_failures(self.db, environ), [f"build:{run['id']}"])

    def test_a_satellite_pushes_nothing(self):
        """The hub pushes every host's failures once; a satellite's tick would send each a second time."""
        hub, _ = self.both()
        with self.on("hub"):
            run = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                              root=self.root / "worktrees")
        assert run is not None
        build.end(self.db, run["id"], "blocked", "the review left blockers")
        notify = self.root / "notify"
        notify.write_text("#!/bin/sh\nexit 0\n")
        notify.chmod(0o755)
        environ = {**os.environ, "SD_NOTIFY": str(notify)}
        with self.on("work"):
            self.assertEqual(ADAPTER.push_failures(self.db, environ), [])
        self.assertEqual(ADAPTER.push_failures(self.db, environ), [f"build:{run['id']}"])


#: A process replaced in place: the first image runs this script, is refused at a beat, and execs itself; the
#: second image holds the same pid and the same child (pack sd:8182). `--help` is the exec's preflight.
EXEC_DRIVER = textwrap.dedent('''
    import json, os, sys
    from pathlib import Path
    from unittest import mock
    if "--help" in sys.argv:
        sys.exit(0)
    from sd_db import build, build_run, remote
    from sd_db.database import connect
    plan = json.loads(Path(sys.argv[1]).read_text())
    with open(plan["pids"], "a") as out:
        out.write(f"{os.getpid()}\\n")
    db = connect(plan["database"])
    first = os.environ.get("SD_BUILD_RESUME") is None
    patches = [mock.patch.object(build, "this_host", return_value="hub"),
               mock.patch("sd_db.hub.configured", return_value=False)]
    if first:
        def refused(connection, ident):
            raise remote.BuildMismatch("build", "old", "new", hub_build="new")
        patches += [mock.patch.object(build, "beat", refused), mock.patch.object(remote, "disk_holds", return_value=True)]
    for patch in patches:
        patch.start()
    tools = build_run.Tools(**plan["tools"])
    ended = build_run.Supervisor(db, plan["run"], tools=tools, log_root=Path(plan["logs"]), environ=plan["env"],
                                 cited=lambda *_: set(plan["items"]), beat_seconds=0.2, timeout=60, grace=0.5).run()
    Path(plan["result"]).write_text(json.dumps(ended))
''')


class Replaced(BaseException):
    """What the fake `exec_in_place` raises in place of replacing the test's process."""

    def __init__(self, state):
        super().__init__(state)
        self.state = state


def replaced(state):
    raise Replaced(state)


class HubFaults(Case):
    """Pack sd:8182: a hub refresh drops the session, then refuses the build this process loaded.

    Each `HUB` raised from the beat, a step's write or the last write used to end the supervisor beside
    its live child. The mismatch cannot clear in this process, so it ends in `exec_in_place`.
    """

    MISMATCH = remote.BuildMismatch("build", "old-digest", "hub-digest", hub_build="hub-digest")
    UNREACHABLE = remote.HubUnreachable("hub.example.test", 8769, "timed out")

    def setUp(self):
        super().setUp()
        self.press()
        self.write_pr()

    def row(self):
        return build.run(self.db, self.bundle["id"])

    def faults_once(self, name, fault, fields=None, after=0, where=None):
        """`build.<name>` raising `fault` once, on the call after `after` that writes one of `fields`, when named."""
        real, fired, passed = getattr(build, name), [], []

        def faulty(*args, **kwargs):
            if not fired and (fields is None or set(fields) & set(kwargs)) and (where is None or where(kwargs)):
                if len(passed) == after:
                    fired.append(name)
                    raise fault
                passed.append(1)
            return real(*args, **kwargs)

        return mock.patch.object(build, name, faulty), fired

    def test_a_hub_gone_at_a_beat_keeps_the_child_and_beats_again(self):
        """The 3fb55b02 death: `hub ... is unreachable: timed out` at a beat ended the supervisor."""
        build.write(self.db, self.bundle["id"], step="reviewing")
        self.plan(claude=["slow"])
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        real, calls = build.beat, []

        def beat(connection, ident):
            calls.append(ident)
            if len(calls) == 1:
                raise self.UNREACHABLE
            return real(connection, ident)

        before = self.row()["heartbeat_at"]
        with mock.patch.object(build, "beat", beat):
            code = supervisor.child([*self.tools.claude, "-p", "x"], cwd=self.root, name="gone")
        self.assertEqual(code, 0)
        self.assertGreater(len(calls), 1, "the next beat came")
        row = self.row()
        self.assertEqual((row["step"], row["outcome"]), ("reviewing", None))
        self.assertGreaterEqual(row["heartbeat_at"], before or "")

    def test_a_mismatch_at_a_beat_hands_the_child_to_the_followed_build(self):
        """The b671669f death: the hub refuses this build for life, so the process replaces itself."""
        self.plan(claude=["slow"], sd_review=["clean"])
        held = [False, True]

        def beat(connection, ident):
            raise self.MISMATCH

        state = None
        with (mock.patch.object(build, "beat", beat),
              mock.patch.object(remote, "disk_holds", side_effect=lambda _: held.pop(0) if held else True, create=True),
              mock.patch.object(build_run, "exec_in_place", replaced, create=True)):
            try:
                self.supervisor().run()
            except Replaced as image:
                state = image.state
                kept = image  # its traceback holds the old image's Popen, as a real exec keeps the child
        self.assertIsNotNone(state, "the process replaced itself once the disk held the hub's build")
        self.assertIsNone(state["child"]["exit"], "the child still ran at the exec")
        self.assertEqual(state["run"], self.bundle["id"])
        self.assertEqual(self.row()["builder_pgid"], state["child"]["pid"])
        with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(state)}):
            ended = self.supervisor().run()
        del kept
        self.assertEqual(ended["step"], "enqueued")
        self.assertEqual(len(self.calls("claude")), 1, "the live child was kept, not run again")
        row = self.row()
        self.assertIsNotNone(row["final"], "its result was judged from its own log lines")
        self.assertTrue(group_gone(state["child"]["pid"]))
        self.assertIsNone(row["outcome"])

    def test_a_real_exec_keeps_the_pid_and_the_child(self):
        """Exec, not its fake: the same pid and its child live on, and the second image finishes the run."""
        self.plan(claude=["slow"], sd_review=["clean"])
        (self.root / "driver.py").write_text(EXEC_DRIVER, encoding="utf-8")
        plan = {"database": str(self.database), "run": self.bundle["id"], "logs": str(self.logs),
                "tools": dataclasses.asdict(self.tools), "items": self.items, "env": self.env,
                "pids": str(self.root / "pids"), "result": str(self.root / "result.json")}
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(BIN.parent / "lib"), str(BIN)])}
        done = subprocess.run([sys.executable, str(self.root / "driver.py"), str(self.root / "plan.json")],
                              capture_output=True, text=True, timeout=120, env=env, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        pids = (self.root / "pids").read_text().split()
        self.assertEqual(len(pids), 2, "the driver ran twice under one pid")
        self.assertEqual(pids[0], pids[1])
        self.assertEqual(json.loads((self.root / "result.json").read_text())["step"], "enqueued")
        self.assertEqual(len(self.calls("claude")), 1)
        self.assertEqual(self.row()["supervisor_pid"], int(pids[0]))

    def test_a_mismatch_that_never_clears_ends_the_group_and_the_run_cleanly(self):
        """No follow comes: the budget ends the child, nothing hangs, and the reap owns the row."""
        self.plan(claude=["hang"])

        def beat(connection, ident):
            raise self.MISMATCH

        supervisor = self.supervisor(timeout=1.0)
        supervisor.settle_seconds = 0.3
        with (mock.patch.object(build, "beat", beat),
              mock.patch.object(remote, "disk_holds", return_value=False, create=True),
              mock.patch.object(build, "end", side_effect=self.MISMATCH),
              mock.patch.object(build_run, "exec_in_place", replaced, create=True)):
            ended = supervisor.run()
        self.assertIsNone(ended["outcome"])
        self.assertIn("build mismatch", ended["why"])
        self.assertTrue(group_gone(self.row()["builder_pgid"]), "no child outlives the supervisor")
        self.assertIsNone(self.row()["outcome"], "nothing was written: the row is the reap's")

    def test_a_result_written_in_the_window_is_not_lost(self):
        """b671669f's result: the child ended, then its first write met the fault; no second child runs."""
        self.plan(claude=["pass"], sd_review=["clean"])
        patch, fired = self.faults_once("write", self.UNREACHABLE, fields=("final",))
        with patch:
            ended = self.supervisor().run()
        self.assertEqual((fired, ended["step"]), (["write"], "enqueued"))
        self.assertEqual(len(self.calls("claude")), 1, "the result was not paid for twice")
        self.assertIsNotNone(self.row()["final"])

    def test_a_mismatch_after_the_child_replays_its_result_in_the_new_image(self):
        self.plan(claude=["pass"], sd_review=["clean"])
        patch, fired = self.faults_once("write", self.MISMATCH, fields=("final",))
        state = None
        with (patch, mock.patch.object(remote, "disk_holds", return_value=True, create=True),
              mock.patch.object(build_run, "exec_in_place", replaced, create=True)):
            try:
                self.supervisor().run()
            except Replaced as image:
                state = image.state
        self.assertEqual(fired, ["write"])
        self.assertEqual(state["child"]["exit"], 0)
        with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(state)}):
            self.assertEqual(self.supervisor().run()["step"], "enqueued")
        self.assertEqual(len(self.calls("claude")), 1)

    def test_a_group_write_in_the_window_runs_the_child_after_it(self):
        """A failed group write closes the gate: the child exits unrun, and the walk starts it again."""
        self.plan(claude=["pass"], sd_review=["clean"])
        patch, fired = self.faults_once("write", self.UNREACHABLE, fields=("builder_pgid",))
        with patch:
            ended = self.supervisor().run()
        self.assertEqual((fired, ended["step"]), (["write"], "enqueued"))
        self.assertEqual(len(self.calls("claude")), 1, "the gated child never ran")

    def test_a_step_write_in_the_window_is_written_after_it(self):
        self.plan(claude=["pass"], sd_review=["clean"])
        patch, fired = self.faults_once("write", self.UNREACHABLE, fields=("step",))
        with patch:
            ended = self.supervisor().run()
        self.assertEqual((fired, ended["step"]), (["write"], "enqueued"))

    def test_the_last_write_in_the_window_is_written_after_it(self):
        """The run blocked while the hub restarted: its end lands once the hub answers."""
        self.plan(claude=["silent"])
        patch, fired = self.faults_once("end", self.UNREACHABLE)
        with patch:
            ended = self.supervisor().run()
        self.assertEqual((fired, ended["outcome"]), (["end"], "blocked"))
        self.assertEqual(self.row()["outcome"], "blocked")

    def test_a_cost_and_a_report_are_not_written_twice_on_a_replay(self):
        self.plan(claude=["pass"], sd_review=["clean"])
        real, faults = add_note, []

        def note(connection, item, kind, body, *args, **kwargs):
            if "report:" in body and not faults:
                faults.append(body)  # the cost of this child was written before the note
                raise self.UNREACHABLE
            return real(connection, item, kind, body, *args, **kwargs)

        with mock.patch.object(build_run, "add_note", note):
            self.assertEqual(self.supervisor().run()["step"], "enqueued")
        reports = self.db.execute("SELECT COUNT(*) FROM note WHERE item = ? AND body LIKE '%report:%'",
                                  (self.items[0],)).fetchone()[0]
        self.assertEqual(reports, 1)
        costs = self.db.execute("SELECT COUNT(*) FROM cost WHERE pass = ?", (self.bundle["id"],)).fetchone()[0]
        self.assertEqual(costs, 1)

    def run_with_lost_answer(self, target, answers, *, committed):
        """The first `build_run.<target>` loses its answer, after its commit when `committed`; what `outcome` said."""
        self.plan(claude=["pass"], sd_review=["clean"])
        real, faults, asked = getattr(build_run, target), [], []
        lost = remote.UnknownOutcome("r" * 26, "the answer to COMMIT was lost")

        def write(*args, **kwargs):
            if faults:
                return real(*args, **kwargs)
            faults.append(target)
            if committed:
                real(*args, **kwargs)
            raise lost

        def outcome(rid):
            asked.append(rid)
            return answers.pop(0)

        supervisor = self.supervisor()
        supervisor.db.outcome = outcome
        with mock.patch.object(build_run, target, write), mock.patch("time.sleep"):
            self.assertEqual(supervisor.run()["step"], "enqueued")
        return asked

    def costs(self):
        return self.db.execute("SELECT COUNT(*) FROM cost WHERE pass = ?", (self.bundle["id"],)).fetchone()[0]

    def test_a_committed_cost_whose_answer_was_lost_is_not_written_again(self):
        """`UnknownOutcome`: the transaction may have committed; the hub says, then the walk goes on."""
        self.assertEqual(self.run_with_lost_answer("record_cost", ["recorded"], committed=True), ["r" * 26])
        self.assertEqual(self.costs(), 1)

    def test_an_absent_cost_whose_answer_was_lost_is_written_on_the_replay(self):
        self.run_with_lost_answer("record_cost", ["absent"], committed=False)
        self.assertEqual(self.costs(), 1)

    def test_a_write_still_in_flight_is_asked_again(self):
        """A live session still owns it: no replay yet, whichever way it ends."""
        asked = self.run_with_lost_answer("record_cost", ["in_flight", "recorded"], committed=True)
        self.assertEqual(len(asked), 2)
        self.assertEqual(self.costs(), 1)

    def test_a_committed_report_whose_answer_was_lost_is_not_written_again(self):
        self.run_with_lost_answer("add_note", ["recorded"], committed=True)
        reports = self.db.execute("SELECT COUNT(*) FROM note WHERE item = ? AND body LIKE '%report:%'",
                                  (self.items[0],)).fetchone()[0]
        self.assertEqual(reports, 1)

    def test_a_lost_answer_is_handed_over_to_the_next_image(self):
        """The rid travels with the marks, so the new image asks the hub and does not write again."""
        supervisor = self.supervisor()
        supervisor.last = {"name": "claude", "argv": [], "pid": 1, "exit": 0, "session": False, "offset": 0}
        lost = remote.UnknownOutcome("r" * 26, "lost")
        with self.assertRaises(remote.UnknownOutcome):
            supervisor.once("cost", mock.Mock(side_effect=lost))
        write = mock.Mock()
        with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(supervisor.state())}):
            image = self.supervisor()
        image.db.outcome = mock.Mock(return_value="recorded")
        image.once("cost", write)
        write.assert_not_called()
        self.assertIn("cost", image.marks)

    def test_a_note_whose_answer_was_lost_is_not_written_again(self):
        """Each note the walk reaches again: `own_checks`' here, the rest go through the same call."""
        real, faults = add_note, []

        def note(*args, **kwargs):
            real(*args, **kwargs)
            if not faults:
                faults.append(1)
                raise remote.UnknownOutcome("r" * 26, "lost")

        supervisor = self.supervisor(checks=lambda _: [])
        supervisor.db.outcome = mock.Mock(return_value="recorded")
        with mock.patch.object(build_run, "add_note", note):
            with self.assertRaises(remote.UnknownOutcome):
                supervisor.own_checks("a" * 40)
            supervisor.own_checks("a" * 40)
        said = self.db.execute("SELECT COUNT(*) FROM note WHERE item = ? AND body LIKE '%no check entrypoint%'",
                               (self.items[0],)).fetchone()[0]
        self.assertEqual(said, 1)

    def test_a_note_after_the_step_moved_is_not_written_again(self):
        """`enqueued` landed before its note: the replay reads the step and stops, so no second note."""
        real, faults = add_note, []

        def note(connection, item, kind, body, *args, **kwargs):
            real(connection, item, kind, body, *args, **kwargs)
            if "in the lane" in body and not faults:
                faults.append(body)
                raise remote.UnknownOutcome("r" * 26, "lost")

        self.plan(claude=["pass"], sd_review=["clean"])
        with mock.patch.object(build_run, "add_note", note):
            ended = self.supervisor().run()
        self.assertEqual((len(faults), ended["step"]), (1, "enqueued"))
        said = self.db.execute("SELECT COUNT(*) FROM note WHERE item = ? AND body LIKE '%in the lane%'",
                               (self.items[0],)).fetchone()[0]
        self.assertEqual(said, 1)

    def test_a_replayed_review_counts_one_round_and_keeps_its_blockers(self):
        """A fault after the round's write re-walked the held review: one review counted as two."""
        self.plan(claude=["pass", "pass"], sd_review=["blocking", "clean"])
        patch, fired = self.faults_once("write", self.UNREACHABLE, fields=("step",),
                                        where=lambda fields: fields.get("step") == "fixing")
        with patch:
            ended = self.supervisor().run()
        self.assertEqual((fired, ended["step"]), (["write"], "enqueued"))
        self.assertEqual(self.row()["review_round"], 1)
        self.assertEqual(len(self.calls("sd-review")), 2, "the held review answered; only the fix's review ran")
        fix = self.calls("claude")[1]["argv"]
        self.assertIn("a blocker", fix[fix.index("-p") + 1])

    def test_a_fix_whose_start_faulted_is_not_answered_by_the_builder(self):
        """The prechecked path: a fault on the fix's group write left the builder as `last`, and it stood in."""
        self.plan(claude=["pass", "pass"], sd_review=["clean"])
        patch, fired = self.faults_once("write", self.UNREACHABLE, fields=("builder_pgid",), after=3)  # fetch, add, builder
        with patch:
            ended = self.supervisor(checked=lambda *_: [] if len(self.calls("claude")) >= 2 else ["not ready"]).run()
        self.assertEqual((fired, ended.get("step")), (["write"], "enqueued"), ended)
        self.assertEqual(len(self.calls("claude")), 2, "the fix ran")
        self.assertEqual(self.row()["review_round"], 1, "the builder did not answer for the fix")

    def test_a_second_fix_whose_start_faulted_is_not_answered_by_the_first(self):
        """Two fixes in a row at the same step: the first must not stand in for the second."""
        self.plan(claude=["pass", "pass", "pass"], sd_review=["clean"])
        patch, fired = self.faults_once("write", self.UNREACHABLE, fields=("builder_pgid",), after=4)  # fetch, add, builder, fix
        with patch:
            ended = self.supervisor(checked=lambda *_: [] if len(self.calls("claude")) >= 3 else ["not ready"]).run()
        self.assertEqual((fired, ended.get("step")), (["write"], "enqueued"), ended)
        self.assertEqual(len(self.calls("claude")), 3, "both fixes ran")
        self.assertEqual(self.row()["review_round"], 2, "the first fix did not answer for the second")

    def test_a_replay_skips_earlier_children_and_runs_the_next(self):
        """A held second check: the first is not run again, the second answers from its record, then a child runs."""
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        build.write(self.db, self.bundle["id"], step="reviewing")
        first, second, third = [[sys.executable, "-c", f"print({n})"] for n in (1, 2, 3)]
        supervisor.held = {"name": "checks", "argv": second, "exit": 7}
        self.assertEqual(supervisor.child(first, cwd=self.root, name="checks"), 0)
        self.assertEqual(supervisor.child(second, cwd=self.root, name="checks"), 7)
        self.assertIsNone(supervisor.held)
        self.assertEqual(supervisor.child(third, cwd=self.root, name="checks"), 0)
        self.assertEqual(self.row()["builder_pgid"], supervisor.last["pid"], "the third child ran")

    def test_a_replay_that_leaves_the_held_childs_path_starts_its_child(self):
        """The walk took another way: nothing is skipped as passed, the child runs."""
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        build.write(self.db, self.bundle["id"], step="reviewing")
        supervisor.held = {"name": "review", "argv": ["old"], "exit": 0}
        self.assertEqual(supervisor.child([sys.executable, "-c", "print(1)"], cwd=self.root, name="lint"), 0)
        self.assertIsNone(supervisor.held)
        self.assertEqual(self.row()["builder_pgid"], supervisor.last["pid"], "the child ran")

    def test_a_replay_keeps_the_output_of_a_child_that_ran(self):
        supervisor = self.supervisor()
        output = self.root / "review.out"
        output.write_text("answer")
        supervisor.held = {"name": "review", "argv": ["x"], "exit": 0}
        supervisor.empty(output, "review")
        self.assertEqual(output.read_text(), "answer", "a replay keeps the answer it needs")
        supervisor.held = None
        supervisor.empty(output, "review")
        self.assertEqual(output.read_text(), "", "a fresh child starts with an empty file")

    def test_a_held_child_keeps_only_the_output_of_a_child_that_will_replay(self):
        """sd:8194: a new review after a held builder appended to the old JSON, and a second review ran."""
        supervisor = self.supervisor()
        output = self.root / "review.out"
        for held, name, kept in (({"name": "claude", "argv": [], "exit": 0, "as": "building"}, "review", False),
                                 ({"name": "review", "argv": ["x"], "exit": 0}, "lint", False),
                                 ({"name": "checks", "argv": ["x"], "exit": 0}, "review", True),
                                 ({"name": "enqueue", "argv": ["x"], "exit": 0}, "lint", True)):
            with self.subTest(held=held["name"], name=name):
                output.write_text("old answer")
                supervisor.held = held
                supervisor.empty(output, name)
                self.assertEqual(output.read_text(), "old answer" if kept else "")

    def test_a_held_claude_of_another_role_gives_no_offset(self):
        """sd:8194: a new fix was judged from the held builder's lines, so its cost and report repeated."""
        self.plan(claude=["pass"], sd_review=["clean"])
        logs = build_run.log_dir(self.logs, self.bundle["repo"])
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"{self.bundle['id']}.jsonl").write_text("12345678")
        supervisor = self.supervisor()
        supervisor.held = {"name": "claude", "argv": [], "exit": 0, "offset": 5, "as": "building"}
        self.assertEqual(supervisor.start_offset("fix:0"), 8)
        self.assertEqual(supervisor.start_offset("building"), 5)

    def test_a_held_claude_reads_its_own_log_lines(self):
        self.plan(claude=["pass"], sd_review=["clean"])
        logs = build_run.log_dir(self.logs, self.bundle["repo"])
        logs.mkdir(parents=True, exist_ok=True)
        supervisor = self.supervisor()
        supervisor.held = {"name": "claude", "argv": [], "exit": 0, "offset": 5}
        self.assertEqual(supervisor.start_offset(None), 5)
        supervisor.held = None
        (logs / f"{self.bundle['id']}.jsonl").write_text("12345678")
        self.assertEqual(supervisor.start_offset(None), 8)

    def test_a_walk_after_a_fault_starts_at_the_row_step_not_the_read_one(self):
        """A walk again in this process: the row moved past what `__init__` read."""
        self.plan(claude=["pass"], sd_review=["clean"])
        supervisor = self.supervisor()
        build.write(self.db, self.bundle["id"], step="enqueued")
        self.assertEqual(supervisor.walk(), {"run": self.bundle["id"], "outcome": None, "step": "enqueued"})

    def test_a_fix_interrupted_at_its_builder_resumes_at_the_fix(self):
        """The `fixing` step normally resumes at its review; a held builder is the fix's own continuation."""
        self.plan(claude=["pass"], sd_review=["clean"])
        supervisor = self.supervisor()
        build.write(self.db, self.bundle["id"], step="fixing", session_id="session-fixture")
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        supervisor.held = {"name": "claude", "argv": [], "exit": 0, "offset": 0, "session": False,
                           "descendants": False, "pid": 1, "as": "fix:0"}
        with mock.patch.object(supervisor, "fix", return_value={"run": "r"}) as fix:
            self.assertEqual(supervisor.walk(), {"run": "r"})
        fix.assert_called_once_with()

    def test_state_for_another_run_or_process_is_ignored(self):
        """Evidence: a handed-over state is this run's in this process, or nothing."""
        build.register(self.db, self.bundle["id"], pid=4242, pgid=4242, start="x")
        for state in ({"run": "other", "pid": os.getpid()}, {"run": self.bundle["id"], "pid": 4242}):
            with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(state)}):
                ended = self.supervisor().run()
            self.assertIn("another supervisor", ended["why"])

    def test_no_child_inherits_the_handed_over_state(self):
        state = {"run": self.bundle["id"], "pid": os.getpid(), "child": None, "groups": None, "reviewed": None,
                 "ending": None, "marks": []}
        with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(state)}):
            supervisor = self.supervisor(environ=None)
            self.assertNotIn("SD_BUILD_RESUME", os.environ)
        self.assertNotIn("SD_BUILD_RESUME", supervisor.env)

    def test_every_hub_fault_at_a_beat_keeps_the_child(self):
        """Each way the hub can refuse, the real types: `Fenced` and `HubConfigError` used to end the supervisor."""
        rid = "r" * 26
        faults = (self.UNREACHABLE, self.MISMATCH, remote.HubRestartNeeded("a", "b"), remote.UnknownOutcome(rid, "lost"),
                  remote.TransactionLost(rid), build_run.SchemaTooNew(9, 8), hub.Fenced("not the hub"),
                  hub.HubConfigError("no token"), remote.HubOnly("migrate", "hub.example.test:8769"),
                  hub.HubConflict(Path("/x/hub.json"), Path("/x/db"), "hub.example.test"))
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        build.write(self.db, self.bundle["id"], step="reviewing")
        short = [sys.executable, "-c", "import time; time.sleep(0.5)"]
        for fault in faults:
            with self.subTest(kind=type(fault).__name__), \
                    mock.patch.object(build, "beat", side_effect=fault), \
                    mock.patch.object(remote, "disk_holds", return_value=False, create=True):
                self.assertEqual(supervisor.child(short, cwd=self.root, name=type(fault).__name__), 0)

    def test_a_refusal_that_never_clears_stops_at_the_bounds(self):
        """A permanent refusal: the budget ends the child, `settle_seconds` ends the supervisor, the row is the reap's."""
        self.plan(claude=["hang"])
        refused = hub.Fenced("not the hub")

        supervisor = self.supervisor(timeout=1.0)
        supervisor.settle_seconds = 0.3
        with mock.patch.object(build, "beat", side_effect=refused), mock.patch.object(build, "end", side_effect=refused):
            ended = supervisor.run()
        self.assertIsNone(ended["outcome"])
        self.assertIn("no answer from the hub", ended["why"])
        self.assertTrue(group_gone(self.row()["builder_pgid"]))
        self.assertIsNone(self.row()["outcome"])

    def test_a_reaped_run_ends_the_kept_child_at_the_first_beat(self):
        """The new image finds the run moved: nothing else writes, and the kept child goes with it."""
        kept = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(end, kept)
        build.write(self.db, self.bundle["id"], step="building", builder_pgid=kept.pid, builder_start="x",
                    session_id="session-fixture")
        build.register(self.db, self.bundle["id"], pid=os.getpid(), pgid=os.getpgrp(), start="x")
        build.end(self.db, self.bundle["id"], "interrupted", "reaped while the image changed")
        state = {"run": self.bundle["id"], "pid": os.getpid(), "reviewed": None, "ending": None, "marks": [],
                 "groups": None, "child": {"name": "claude", "argv": [], "pid": kept.pid, "exit": None,
                                           "session": False, "offset": 0, "left": 30.0}}
        with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(state)}):
            ended = self.supervisor().run()
        self.assertIn("ended or moved", ended["why"])
        self.assertTrue(group_gone(kept.pid))

    def test_the_budget_survives_the_exec(self):
        """The new image keeps what was left of the child's budget, so a refresh cannot extend it."""
        kept = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(end, kept)
        build.write(self.db, self.bundle["id"], step="building", builder_pgid=kept.pid, builder_start="x",
                    session_id="session-fixture", log_path="x")
        build.register(self.db, self.bundle["id"], pid=os.getpid(), pgid=os.getpgrp(), start="x")
        state = {"run": self.bundle["id"], "pid": os.getpid(), "reviewed": None, "ending": None, "marks": [],
                 "groups": None, "child": {"name": "claude", "argv": [], "pid": kept.pid, "exit": None,
                                           "session": False, "offset": 0, "left": 0.3}}
        with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(state)}):
            ended = self.supervisor(timeout=500).run()
        self.assertEqual(ended["outcome"], "timed_out")
        self.assertTrue(group_gone(kept.pid))

    def test_an_unwarranted_disk_build_is_no_match(self):
        """A failed read or an older hub's mismatch is unknown: no exec."""
        current = remote.tree_digest(Path(remote.__file__).resolve().parent)
        self.assertTrue(remote.disk_holds(remote.BuildMismatch("build", "x", current, hub_build=current)))
        self.assertTrue(remote.disk_holds(remote.BuildMismatch("build", "x", current)))
        self.assertFalse(remote.disk_holds(remote.BuildMismatch("schema", 1, 2)))
        self.assertFalse(remote.disk_holds(remote.BuildMismatch("build", "x", "another")))
        with mock.patch.object(remote, "_hash_files", side_effect=OSError("moving")):
            self.assertFalse(remote.disk_holds(remote.BuildMismatch("build", "x", current, hub_build=current)))

    def test_the_exec_stays_put_when_the_new_image_does_not_load(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)), \
                mock.patch("os.execve") as execve:
            build_run.exec_in_place({"run": "r"})
        execve.assert_not_called()

    def test_a_connect_after_the_exec_waits_for_the_hub(self):
        state = {"run": "r", "pid": os.getpid(), "child": {"pid": 999999, "exit": None}}
        tries = []

        def connect_once():
            tries.append(1)
            if len(tries) == 1:
                raise OSError("no hub")
            return "session"

        with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(state)}), \
                mock.patch("time.sleep"):
            self.assertEqual(build_run.start_again("r", connect_once), "session")
        self.assertEqual(len(tries), 2)

    def test_every_hub_read_before_the_run_loop_is_retried_with_the_child_kept(self):
        """Round 2: only the connect was covered; a fault in a later read exited beside the live child."""
        self.assert_start_recovers(self.bundle["id"])

    def supervise_faulting(self, opened, *, state=None, **patches):
        state = resume_state(self.bundle["id"]) if state is None else state
        raw = state if isinstance(state, str) else json.dumps(state)
        with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": raw}), mock.patch("sd_db.connect", opened), \
                mock.patch("time.sleep"), mock.patch("sys.path", [str(BIN), *sys.path]), \
                mock.patch.object(ADAPTER, "log_root", return_value=self.logs), \
                mock.patch.object(build, "kill_group") as kill, mock.patch("builtins.print"), moving_clock():
            with mock.patch.multiple(remote, **patches) if patches else contextlib.nullcontext():
                try:
                    ADAPTER.supervise_build(mock.Mock(run=self.bundle["id"]))
                except ADAPTER.WorkRefusal as refusal:
                    return refusal, kill
        return None, kill

    def test_a_start_that_never_succeeds_ends_the_kept_child(self):
        """Past the bound the child goes too: none outlives its supervisor, and the row stays the reap's."""
        closed, seen = [], []

        def opened(write=False):
            real = connect(self.database)
            refusing = Refuses(real, 1, self.UNREACHABLE, [])
            refusing.close = lambda: (closed.append(1), real.close())
            seen.append(refusing)
            return refusing

        refusal, kill = self.supervise_faulting(opened)
        self.assertIsNotNone(refusal, "the failure stands")
        kill.assert_called_once_with(424242)
        self.assertEqual(len(closed), len(seen), "each failed start closed its connection")

    def test_a_mismatch_in_the_start_follows_the_build(self):
        """The hub refuses this image for life: a retry cannot pass, at the connect or at a read after it."""
        def at_connect(write=False):
            raise self.MISMATCH

        def at_a_read(write=False):
            return Refuses(connect(self.database), 1, self.MISMATCH, [])

        for name, opened in (("connect", at_connect), ("read", at_a_read)):
            with self.subTest(name), mock.patch.object(build_run, "exec_in_place", replaced, create=True):
                with self.assertRaises(Replaced) as image:
                    self.supervise_faulting(opened, disk_holds=lambda _: True)
                self.assertEqual(image.exception.state["child"]["pid"], 424242)
                self.assertEqual(image.exception.state["run"], self.bundle["id"])

    def test_a_wait_in_the_start_spends_the_childs_budget(self):
        """A hub outage at the start must not give the child its budget again."""
        lefts = []

        def opened(write=False):
            lefts.append(json.loads(os.environ["SD_BUILD_RESUME"])["child"]["left"])
            if len(lefts) == 1:
                raise self.UNREACHABLE
            return connect(self.database)

        with mock.patch.object(build_run.Supervisor, "run", return_value={"run": "r"}):
            self.supervise_faulting(opened)
        self.assertLess(lefts[1], lefts[0])

    def test_a_state_of_another_process_or_run_is_no_held_child(self):
        """Only this process's state for this run names a child to wait for or end; anything else is one try."""
        other = resume_state(self.bundle["id"])
        other["pid"] = os.getpid() + 1
        for state in (other, resume_state("another-run"), "{not json"):
            with self.subTest(state=str(state)[:24]):
                opened = mock.Mock(side_effect=self.UNREACHABLE)
                refusal, kill = self.supervise_faulting(opened, state=state)
                self.assertIsNotNone(refusal)
                self.assertEqual(opened.call_count, 1)
                kill.assert_not_called()

    def test_the_connects_follow_keeps_the_handed_over_state(self):
        """`bin/sd`'s hook follows at a connect's mismatch, then execs: the pid and this state go with it."""
        reruns = []
        with mock.patch.object(follow, "script", return_value=Path("/x/repo-sync.sh")), \
                mock.patch.object(follow, "_run_follow", return_value=(0, "", False)), \
                mock.patch.object(follow, "stdin_consumed", return_value=""), \
                mock.patch.object(remote, "sessions_opened", return_value=0):
            follow.attempt(self.MISMATCH, [], environ={"SD_BUILD_RESUME": "state"}, argv=["sd"], out=io.StringIO(),
                           rerun=lambda _, env: reruns.append(env))
        self.assertEqual(reruns[0]["SD_BUILD_RESUME"], "state")

    def test_a_resumed_supervisor_registers_nothing(self):
        """The one hub call `run()` makes outside its loop is the registration, which a resumed run skips."""
        state = resume_state(self.bundle["id"])
        with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(state)}), \
                mock.patch.object(build, "register") as register, \
                mock.patch.object(build_run.Supervisor, "ended", return_value={"run": self.bundle["id"]}):
            self.supervisor().run()
        register.assert_not_called()

    def test_a_fault_at_the_first_beat_of_a_kept_child_keeps_it(self):
        """The first hub call inside `run()`'s loop for an adopted child: the child stays until its budget."""
        kept = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(end, kept)
        build.write(self.db, self.bundle["id"], step="building", builder_pgid=kept.pid, builder_start="x",
                    session_id="session-fixture", log_path="x")
        build.register(self.db, self.bundle["id"], pid=os.getpid(), pgid=os.getpgrp(), start="x")
        state = resume_state(self.bundle["id"], kept.pid)
        state["child"]["left"] = 1.0
        patch, fired = self.faults_once("beat", self.UNREACHABLE)
        with patch, mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(state)}):
            ended = self.supervisor(timeout=500).run()
        self.assertEqual(fired, ["beat"])
        self.assertEqual(ended["outcome"], "timed_out")
        self.assertTrue(group_gone(kept.pid))

    def test_a_connect_that_never_returns_ends_the_kept_child(self):
        state = {"run": "r", "pid": os.getpid(), "child": {"pid": 424242, "exit": None}}
        with mock.patch.dict(os.environ, {"SD_BUILD_RESUME": json.dumps(state)}), \
                mock.patch("time.sleep"), mock.patch.object(build, "kill_group") as kill:
            with self.assertRaises(OSError):
                build_run.start_again("r", mock.Mock(side_effect=OSError("no hub")), bound=0)
        kill.assert_called_once_with(424242)

    def test_a_connect_with_no_handed_over_state_is_one_try(self):
        connect = mock.Mock(side_effect=OSError("no hub"))
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("SD_BUILD_RESUME", None)
            with self.assertRaises(OSError):
                build_run.start_again("r", connect)
        connect.assert_called_once_with()


class Budget(unittest.TestCase):
    def test_a_build_child_gets_two_hours(self):
        """25 of 263 runs died at 3600 s, 13 building and 12 reviewing (sd:8204)."""
        self.assertEqual(build_run.TIMEOUT_SECONDS, 7200)


if __name__ == "__main__":
    unittest.main()
