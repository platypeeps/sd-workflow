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
import hashlib
import importlib
import io
import json
import os
import re
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
from datetime import UTC, datetime, timedelta
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
    paths,
    remote,
    runner,
    sessions,
    writes,
)
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.ship import this_host
from sd_db.workflow import item_state
from sd_db.writes import add_note, create_item, record_state, resolve_note, upsert_repo

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
        log.write(json.dumps({"role": role, "argv": sys.argv[1:], "cwd": os.getcwd(),
                             "run": os.environ.get("SD_BUILD_RUN")}) + "\\n")
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
        if mode == "rate_limited":  # pack sd:9907: the account's weekly limit, as Claude Code ended two builds
            reset = int(time.time()) + 3 * 86400
            (plans / "reset.txt").write_text(str(reset))
            line({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                  "permission_denials": [], "session_id": session,
                  "result": "You've hit your weekly limit \\u00b7 resets Nov 24 at 11am",
                  "rate_limit_info": {"rateLimitType": "seven_day", "resetsAt": reset, "overageStatus": "disabled"}})
            sys.exit(1)
        if mode == "asked-dirty":  # asked, then left an uncommitted file: a block the question did not cause
            Path("stray.txt").write_text("stray")
        if mode.startswith("asked"):  # skipped every item with a question: a passing reply, no commit (sd:9219)
            line({"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
                  "session_id": session, "result": "skipped (asked)"})
            sys.exit(0)
        if mode == "deny":  # denied a call, the builder stopped with its work unfinished
            line({"type": "result", "subtype": "success", "is_error": False, "session_id": session,
                  "result": "denied", "permission_denials": [{"tool_name": "Bash",
                                                              "tool_input": {"command": "git push"}}]})
            sys.exit(3)
        # A function per round: a change outside any function holds every later blocker (sd:9769).
        added = f"def {mode}(): pass\\n"
        Path("work.txt").write_text(Path("work.txt").read_text() + added if Path("work.txt").exists() else added)
        git("add", "work.txt")
        # `extra`: plans/extra.json adds files (name to hex bytes) and message lines, as a builder may write.
        extra = json.loads((plans / "extra.json").read_text()) if mode == "extra" else {}
        for name, data in extra.get("files", {}).items():
            Path(name).parent.mkdir(parents=True, exist_ok=True)
            Path(name).write_bytes(bytes.fromhex(data))
            git("add", "--", name)
        for name, data in extra.get("unadded", {}).items():  # written, never added: ignored or untracked
            Path(name).parent.mkdir(parents=True, exist_ok=True)
            Path(name).write_bytes(bytes.fromhex(data))
        for old, new in extra.get("move", {}).items():
            git("mv", old, new)
        for name in extra.get("remove", []):
            git("rm", "-q", "--", name)
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
        if mode == "deny-read":  # sd:9054: a read denied on the way, the work finished all the same
            final["permission_denials"] = [{"tool_name": "Bash", "tool_input": {"command": "rtk git show HEAD"}}]
        if mode == "max_turns":
            final.update(subtype="error_max_turns", is_error=True)
        if mode == "other":
            final["session_id"] = "session-of-another-run"
        line(final)
        sys.exit(3 if mode == "exit3" else 0)
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
        if mode == "rate_limited":  # sd:9882: the provider's quota, which `sd-review` exits 4 on
            print(json.dumps({"status": "rate_limited", "findings": [],
                              "subject": {"head": git("-C", work, "rev-parse", "HEAD")}}))
            sys.exit(4)
        raw = {"path": "work.txt", "line": 1, "severity": "high", "instances": ["src.py:1"], "summary": "a blocker", "family": "correctness"}
        if mode == "blocking-long":  # two findings past a note's 4000 characters, the first over three lines
            first = dict(raw, summary="a long blocker\\n- an example bullet\\n" + "x" * 4500)
            second = dict(raw, path="later.txt", line=9, summary="a later blocker")
            findings = review.dispose([review._finding(first), review._finding(second)], "high")
            print(json.dumps({"status": "blocking", "subject": {"head": git("-C", work, "rev-parse", "HEAD")},
                              "findings": findings}))
            sys.exit(0)
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
        if mode in ("offdelta", "offdelta_short", "offdelta_held", "offdelta_uncovered"):  # pack sd:9769: a blocker on a file nobody touched
            far = {**raw, "path": "far.txt", "instances": ["far.txt:1"], "summary": "an old defect"}
            held = [review._finding(raw)] if mode == "offdelta_held" else []  # and one the fix's file still holds
            print(json.dumps({"status": "blocking", "scope": "branch", "requested_reviews": 2,
                              "completed_reviews": 1 if mode == "offdelta_short" else 2,
                              "covers_from": None if mode == "offdelta_uncovered" else "c" * 40,
                              "subject": {"head": git("-C", work, "rev-parse", "HEAD")},
                              "findings": review.dispose([review._finding(far), *held], "high")}))
            sys.exit(0)
        if mode == "absolute":  # pack sd:9769: a blocker spelled as an absolute path inside the checkout
            findings = review.dispose([review._finding({**raw, "path": os.path.join(work, "work.txt")})], "high")
        print(json.dumps({"status": "blocking" if mode in ("blocking", "absolute") else "clean", "scope": "branch",
                          "requested_reviews": 2, "completed_reviews": 2,
                          "covers_from": None if mode == "clean_uncovered" else "c" * 40,
                          "subject": {"head": git("-C", work, "rev-parse", "HEAD")},
                          "findings": findings if mode in ("blocking", "absolute") else []}))
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


def fake_grep(files: dict[str, str], head: str, argv: list[str]) -> str:
    """`git grep -n -z` over `files` at `head`: `-w -e NAME` as a whole word, or `-E` with each `-e` pattern
    in POSIX classes; `head:path\0line\0text` per matching line."""
    patterns = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-e"]
    if "-E" in argv:
        tests = [re.compile(pattern.replace("[^[:alnum:]_]", r"\W").replace("[[:space:]]", r"\s").replace("[[:alnum:]_]", r"\w")) for pattern in patterns]
    else:
        tests = [re.compile(rf"\b{re.escape(pattern)}\b") for pattern in patterns]
    return "".join(f"{head}:{path}\0{number}\0{text}\n" for path, body in files.items()
                   for number, text in enumerate(body.splitlines(), 1) if any(test.search(text) for test in tests))


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

    def test_a_usage_limit_line_is_rate_limited(self):
        """Pack sd:9907: builds bc013189 and 3d301374 ended in one turn on this line, and the run read it as a
        plain failure, so each item blocked and parked. It is `rate_limited`, as `sd-review` classes it."""
        weekly = self.lines(is_error=True, api_error_status=429,
                            result="You've hit your weekly limit · resets Nov 24 at 11am",
                            rate_limit_info={"rateLimitType": "seven_day", "resetsAt": 1795543200,
                                             "overageStatus": "disabled"})
        self.assertEqual(build_run.judge(weekly)["state"], "rate_limited")
        marked = self.lines(is_error=True, result="Claude AI usage limit reached")
        self.assertEqual(build_run.judge(marked)["state"], "rate_limited", "a marker sd-review knows, no status")
        for final in ({"is_error": True, "api_error_status": 500}, {"is_error": True, "api_error_status": "429"},
                      {"is_error": False, "api_error_status": 429, "subtype": "error_during_execution"},
                      {"is_error": True, "result": "the build failed"}):
            with self.subTest(final=final):
                self.assertEqual(build_run.judge(self.lines(**final))["state"], "failed")

    def test_the_hold_ends_at_a_sane_reset_or_in_an_hour(self):
        """Pack sd:9907: the hold ends at the limit's `resetsAt` when it is ahead, within `HOLD_MAX_SECONDS`;
        any other value, magnitude or shape holds `HOLD_SECONDS`, never forever and never not at all."""
        stamp, hour = "2026-11-21T18:00:00+00:00", "2026-11-21T19:00:00+00:00"
        start = int(datetime.fromisoformat(stamp).timestamp())
        reset = 1795543200  # 2026-11-24T18:00:00Z, the line builds bc013189 and 3d301374 ended on

        def final(value):
            return json.dumps({"type": "result", "rate_limit_info": {"resetsAt": value}})

        self.assertEqual(build.hold_until(final(reset), stamp), "2026-11-24T18:00:00+00:00")
        self.assertEqual(build.hold_until(final(reset + 0.5), stamp), "2026-11-24T18:00:00+00:00")
        for value in (None, True, False, 0, -1, 5e-324, 1e308, 10 ** 400, -(10 ** 400), float("inf"), float("-inf"),
                      float("nan"), str(reset), [reset], {"at": reset}, reset * 1000, start, start - 86400,
                      start + build.HOLD_MAX_SECONDS + 1):
            with self.subTest(value=value):
                self.assertEqual(build.hold_until(final(value), stamp), hour)
        for text in (None, "", "not json", "[" * 100000, "]", json.dumps([1]), json.dumps({"rate_limit_info": 5}),
                     final(reset)[:40]):
            with self.subTest(text=text[:20] if text else text):
                self.assertEqual(build.hold_until(text, stamp), hour)

    def test_the_markers_are_sd_reviews(self):
        """Pack sd:9907: one vocabulary; `sd-review`'s markers, read from its source, are the run's."""
        import ast
        tree = ast.parse((BIN / "sd-review").read_text(encoding="utf-8"))
        found = [ast.literal_eval(node.value) for node in ast.walk(tree) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "RATE_LIMIT_MARKERS" for target in node.targets)]
        self.assertEqual([build_run.RATE_LIMIT_MARKERS], found)


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

    def test_the_claude_child_env_names_the_run(self):
        """pack sd:9715: machine hooks tell a headless builder by SD_BUILD_RUN."""
        self.press(2)
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.supervisor().run()
        runs = [call["run"] for call in self.calls("claude")]
        self.assertTrue(runs)
        self.assertEqual(set(runs), {self.bundle["id"]})

    def test_the_supervisor_env_drops_a_hooks_git_location(self):
        """pack sd:9709 round 26: the supervisor's git calls and the builder start from this env; a hook's
        `GIT_DIR` or `GIT_WORK_TREE` would point them at the hook's repository, not the run's worktree."""
        self.press(1)
        self.env.update({"GIT_DIR": "/decoy/.git", "GIT_WORK_TREE": "/decoy", "GIT_INDEX_FILE": "/decoy/index",
                         "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "url.file:///decoy.git.insteadOf",
                         "GIT_CONFIG_VALUE_0": "origin", "GIT_CONFIG_KEY_1": "maintenance.auto",
                         "GIT_CONFIG_VALUE_1": "false"})
        env = self.supervisor().env
        # Only the suite's maintenance keys ride on, renumbered; a one-off `insteadOf` does not (pack sd:9863).
        self.assertEqual({key for key in env if key in paths.GIT_LOCAL} - {"GIT_CONFIG_COUNT"}, set())
        self.assertEqual((env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]),
                         ("1", "maintenance.auto", "false"))
        self.assertNotIn("GIT_CONFIG_KEY_1", env)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], self.env["GIT_CONFIG_NOSYSTEM"], "config isolation stays")

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
        """Review round 2: only the members a branch commit's `Closes:` trailer cites are enqueued and done; the
        other, skipped with no question, ends and returns to planning, not blocked (sd:8960)."""
        self.press(2)
        self.write_pr()
        self.plan(claude=["skip"], sd_review=["clean"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        enqueue = next(call["argv"] for call in self.calls("sd-ship") if "enqueue" in call["argv"])
        self.assertEqual([enqueue[n + 1] for n, arg in enumerate(enqueue) if arg == "--item"], [str(self.items[0])])
        self.assertEqual(self.statuses(), ["in_progress", "planning"])
        self.assertEqual([m["status"] for m in build.run(self.db, self.bundle["id"])["members"]], ["done", "cancelled"])
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
        cited = LANE.cited_closes(self.db, self.items[0], body, LANE.branch_commits(work, "main", head), (),
                                  branch=build.branch(self.bundle["assignment"]))
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

    def test_a_builder_that_skips_every_item_with_a_question_waits_on_the_operator(self):
        """sd:9219: no commit, and every item has a question since the run started. The run says so, not
        'left no commit'; no item gets a requeue followup, and its release parks none."""
        self.press(2)
        self.plan(claude=["asked"], sd_review=["clean"])
        for item in self.items:
            add_note(self.db, item, "question", "Which option?\nOption: Yes (Recommended)\nOption: No")
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertNotIn("left no commit", ended["blocked"])
        self.assertIn(f"every item has an open question: sd:{self.items[0]}, sd:{self.items[1]}", ended["blocked"])
        marks = ", ".join("?" for _ in self.items)
        followups = f"SELECT COUNT(*) FROM note WHERE item IN ({marks}) AND kind = 'followup'"
        self.assertEqual(self.db.execute(followups, self.items).fetchone()[0], 0)
        build.reap(self.db, probe=GoneProbe(alive=False, group=False))
        self.assertIsNotNone(build.run(self.db, self.bundle["id"])["released_at"])
        self.assertEqual([item_state(self.db, item)["item"]["parked_at"] for item in self.items], [None, None])

    def test_a_resolved_question_is_not_open(self):
        """sd:9219 review round 2: a question closed with `sd note resolve` asks nothing, so a run with no
        commit left no commit: each item gets the followup."""
        self.press(2)
        self.plan(claude=["asked"], sd_review=["clean"])
        for item in self.items:
            resolve_note(self.db, add_note(self.db, item, "question", "Which?\nOption: Yes (Recommended)\nOption: No"))
        self.assertIn("left no commit", self.supervisor().run()["blocked"])
        counts = [self.db.execute("SELECT COUNT(*) FROM note WHERE item = ? AND kind = 'followup'",
                                  (item,)).fetchone()[0] for item in self.items]
        self.assertEqual(counts, [1, 1])

    def test_a_question_keeps_the_followup_and_park_of_a_block_it_did_not_cause(self):
        """sd:9219 review: every item asked, but the builder left its worktree dirty. The dirty worktree blocks
        the run, so each item still gets the followup, and the release parks it."""
        self.press(2)
        self.plan(claude=["asked-dirty"], sd_review=["clean"])
        for item in self.items:
            add_note(self.db, item, "question", "Which option?\nOption: Yes (Recommended)\nOption: No")
        self.assertIn("dirty", self.supervisor().run()["blocked"])
        counts = [self.db.execute("SELECT COUNT(*) FROM note WHERE item = ? AND kind = 'followup'",
                                  (item,)).fetchone()[0] for item in self.items]
        self.assertEqual(counts, [1, 1])
        build.reap(self.db, probe=GoneProbe(alive=False, group=False))
        parked = [item_state(self.db, item)["item"]["parked_at"] is not None for item in self.items]
        self.assertEqual(parked, [True, True])

    def test_a_builder_that_skips_one_item_with_a_question_blocks_the_other(self):
        """sd:9219, the other side: no commit, one item asked. The run left no commit, a block the question did
        not cause, so each item gets the followup and the park."""
        self.press(2)
        self.plan(claude=["asked"], sd_review=["clean"])
        add_note(self.db, self.items[0], "question", "Which option?\nOption: Yes (Recommended)\nOption: No")
        self.assertIn("left no commit", self.supervisor().run()["blocked"])
        counts = [self.db.execute("SELECT COUNT(*) FROM note WHERE item = ? AND kind = 'followup'",
                                  (item,)).fetchone()[0] for item in self.items]
        self.assertEqual(counts, [1, 1])
        build.reap(self.db, probe=GoneProbe(alive=False, group=False))
        parked = [item_state(self.db, item)["item"]["parked_at"] is not None for item in self.items]
        self.assertEqual(parked, [True, True])

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

    def test_a_group_whose_leader_dies_between_two_reads_stays_ours(self):
        """Gate flake at 257aeff94: the check's leader exited after `alive` and was reaped before `started`, so
        `ours` dropped its group, and the `sleep` it left ran on beside the retry. A leader gone at a second read
        frees no number while the group has a member: the group stays ours."""
        self.press()

        class ReapedAtTheStart(build.Probe):
            lives = [True, False]

            @staticmethod
            def group(pgid):
                return True

            @classmethod
            def alive(cls, pid):
                return cls.lives.pop(0)

            @staticmethod
            def started(pid):
                return None

        self.assertTrue(self.supervisor(probe=ReapedAtTheStart).ours(4242, "Thu Nov 19 13:16:42 2026"))

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

    def test_a_denial_the_builder_got_past_with_its_work_finished_passes(self):
        """sd:9054: a denied read-only call, then the work finished, is no loss: the run goes on, and the
        report names the denied call. A denial that left the work unfinished still loses (pack sd:6723)."""
        self.press()
        self.write_pr()
        self.plan(claude=["deny-read"], sd_review=["clean"])
        ended = self.supervisor().run()
        self.assertEqual((ended.get("outcome"), ended.get("step")), (None, "enqueued"))
        report = self.db.execute("SELECT body FROM note WHERE item = ? AND body LIKE 'Build run % report:%'",
                                 (self.items[0],)).fetchone()["body"]
        self.assertIn("Denied on the way: Bash `rtk git show HEAD`", report)

    def test_a_denied_fix_round_that_committed_nothing_is_a_denial(self):
        """sd:9054 review: on a fix round the branch is ahead already; a denial passes only when the child
        itself committed, so one that stopped the fix is a denial, not another review round."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "deny"], sd_review=["blocking", "clean"])
        ended = self.supervisor().run()
        self.assertEqual((ended.get("outcome"), len(self.calls("sd-review"))), ("denied", 1))

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

    def test_a_usage_limit_requeues_the_bundle_and_holds_this_host(self):
        """Pack sd:9907: a run that hit the account's usage limit blocked and parked its items, and each new
        claim on that host died in one turn. It requeues the whole bundle, with no block, park or followup,
        and this host claims nothing until the limit's `resetsAt`."""
        self.press(2)
        self.plan(claude=["rate_limited"])
        ended = self.supervisor().run()
        self.assertEqual((ended["outcome"], ended["retry"]), ("rate_limited", None))
        self.assertEqual(self.statuses(), ["ready", "ready"])
        rows = self.db.execute("SELECT status FROM assignment WHERE id IN (?, ?) ORDER BY id",
                               (self.bundle["assignment"], self.bundle["members"][1]["id"])).fetchall()
        self.assertEqual([row["status"] for row in rows], ["queued", "queued"])
        self.assertEqual([item_state(self.db, item)["item"]["parked_at"] for item in self.items], [None, None])
        kinds = {row["kind"] for row in self.db.execute(
            f"SELECT kind FROM note WHERE item IN ({', '.join('?' for _ in self.items)})", self.items)}
        self.assertNotIn("followup", kinds)
        self.assertIsNotNone(build.run(self.db, self.bundle["id"])["released_at"])
        reset = datetime.fromtimestamp(int((self.plans / "reset.txt").read_text()), UTC)
        until = reset.isoformat(timespec="seconds")
        self.assertEqual(writes.read_settings(self.db, ["build.rate_hold.hub"]), {"build.rate_hold.hub": until})
        self.assertIsNone(build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                      root=self.root / "worktrees"), "this host claims nothing before the reset")
        later = (reset + timedelta(seconds=1)).isoformat(timespec="seconds")
        with mock.patch.object(build, "clock", return_value=later):
            again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                root=self.root / "worktrees")
        assert again is not None
        self.assertEqual((again["assignment"], again["work_path"], again["run"]),
                         (self.bundle["assignment"], self.bundle["work_path"], 2), "the same bundle, in its worktree")

    def test_a_usage_limit_claim_resumes_the_step_session_and_round(self):
        """Advisory of PR #1960 (sd:9973): the claim after a usage limit opened at `claimed` with no session and
        round 0, so a bundle limited in its third fix round rebuilt from scratch with a fresh round budget. It
        resumes its session and round, and a fix its own step (review of build/sd-1557), not `RESUME`'s."""
        self.press()
        ident = self.bundle["id"]
        reset = int(datetime.now(UTC).timestamp()) + 60
        self.assertTrue(build.write(self.db, ident, step="fixing", session_id="session-limited", review_round=2,
                                    final=json.dumps({"rate_limit_info": {"resetsAt": reset}})))
        build.end(self.db, ident, build.RATE_LIMITED, "rate limited")
        later = datetime.fromtimestamp(reset + 1, UTC).isoformat(timespec="seconds")
        with mock.patch.object(build, "clock", return_value=later):
            again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                root=self.root / "worktrees")
        assert again is not None
        row = build.run(self.db, again["id"])
        self.assertEqual((row["step"], row["session_id"], row["review_round"]), ("fixing", "session-limited", 2))

    def test_a_usage_limit_claim_keeps_the_question_cutoff(self):
        """Lane review of build/sd-1557: the reclaim's new run moved the time open questions are read from, so a
        member skipped with a question asked before the limit returned to planning, its question unseen."""
        self.press(2)
        ident = self.bundle["id"]
        member = self.items[1]
        reset = int(datetime.now(UTC).timestamp()) + 60
        self.assertTrue(build.write(self.db, ident, step="fixing", session_id="session-limited", review_round=1,
                                    final=json.dumps({"rate_limit_info": {"resetsAt": reset}})))
        add_note(self.db, member, "question", "skipped: which parser?\nOption: a (Recommended)\nOption: b")
        self.db.execute("UPDATE note SET timestamp = ? WHERE item = ? AND kind = 'question'",
                        (build.run(self.db, ident)["created_at"], member))
        self.db.commit()
        build.end(self.db, ident, build.RATE_LIMITED, "rate limited")
        later = datetime.fromtimestamp(reset + 1, UTC).isoformat(timespec="seconds")
        with mock.patch.object(build, "clock", return_value=later):
            again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                root=self.root / "worktrees")
        assert again is not None
        self.assertTrue(build.enqueued(self.db, again["id"], [self.items[0]]))
        status = self.db.execute("SELECT status FROM assignment WHERE item = ? AND id <> ? ORDER BY id DESC",
                                 (member, self.bundle["assignment"])).fetchone()["status"]
        self.assertEqual(status, "blocked", "the question asked before the limit still holds the member")

    def test_a_usage_limit_claim_refuses_a_bundle_whose_lane_moved_off_its_host(self):
        """Review of build/sd-1557: the claim after a usage limit resumed the old session on whichever host now
        runs the lane, though the worktree and session stay where the run ran: it refuses, naming that host."""
        self.press(2)
        ident = self.bundle["id"]
        reset = int(datetime.now(UTC).timestamp()) + 60
        self.assertTrue(build.write(self.db, ident, step="fixing", session_id="session-limited", review_round=1,
                                    final=json.dumps({"rate_limit_info": {"resetsAt": reset}})))
        build.end(self.db, ident, build.RATE_LIMITED, "rate limited")
        # It ran on the satellite `work`; the lane moved to the hub during the hold.
        self.db.execute("UPDATE runner_run SET owner = 'fixture@work' WHERE id = ?", (ident,))
        self.db.commit()
        later = datetime.fromtimestamp(reset + 1, UTC).isoformat(timespec="seconds")
        with mock.patch.object(build, "clock", return_value=later):
            self.assertIsNone(build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                          root=self.root / "worktrees"))
        rows = self.db.execute("SELECT status, result FROM assignment WHERE id = ? OR parent = ? ORDER BY id",
                               (self.bundle["assignment"], self.bundle["assignment"])).fetchall()
        self.assertEqual([row["status"] for row in rows], ["blocked", "blocked"])
        self.assertIn(f"build run {ident} hit the usage limit on work", rows[0]["result"])
        self.assertIn("move its lane back to work", rows[0]["result"])

    def test_a_fault_inside_the_usage_limit_end_writes_nothing_and_the_next_end_finishes(self):
        """Pack sd:9907, failure table row 2: the outcome, the release, the requeue and the hold are one
        transaction. A fault at its last write leaves the run open and its bundle running, as the
        resumed supervisor finds it; its end then settles all of it."""
        self.press(2)
        ident = self.bundle["id"]
        reset = int(datetime.now(UTC).timestamp()) + 7200
        self.assertTrue(build.write(self.db, ident, step="building", final=json.dumps(
            {"type": "result", "is_error": True, "api_error_status": 429, "rate_limit_info": {"resetsAt": reset}})))
        with mock.patch.object(build, "set_setting", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertRaises(sqlite3.OperationalError):
            build.end(self.db, ident, build.RATE_LIMITED, "rate limited")
        row = build.run(self.db, ident)
        self.assertEqual((row["outcome"], row["released_at"]), (None, None))
        self.assertEqual([member["status"] for member in row["members"]], ["running", "running"])
        self.assertEqual((self.statuses(), writes.read_settings(self.db, ["build.rate_hold.hub"])),
                         (["in_progress", "in_progress"], {}))
        ended = build.end(self.db, ident, build.RATE_LIMITED, "rate limited")
        assert ended is not None
        self.assertEqual(ended["outcome"], "rate_limited")
        self.assertEqual(self.statuses(), ["ready", "ready"])
        self.assertEqual(writes.read_settings(self.db, ["build.rate_hold.hub"]),
                         {"build.rate_hold.hub": datetime.fromtimestamp(reset, UTC).isoformat(timespec="seconds")})

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
        """Failure table, Reviewing: blockers after REVIEW_ROUNDS rounds and one class pass block the items; each fix resumes."""
        R = build_run.REVIEW_ROUNDS
        self.press()
        self.plan(claude=["pass", *(f"fix{n}" for n in range(1, R + 1))], sd_review=["blocking"] * (R + 1))
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn(f"{R + 1} review rounds left blockers: work.txt:1 a blocker", self.latest()["detail"])
        self.assertEqual(len(self.calls("sd-review")), R + 1)
        fixes = [call["argv"] for call in self.calls("claude")][1:]
        self.assertEqual([argv[argv.index("--resume") + 1] for argv in fixes], ["session-fixture"] * R)
        prompts = [argv[argv.index("-p") + 1] for argv in fixes]
        self.assertIn("- work.txt:1 a blocker", prompts[0], "the fix names each blocker")
        self.assertFalse(any("class pass" in prompt for prompt in prompts[1:-1]))
        self.assertIn("class pass", prompts[-1], "sd:9263: the last round is a class pass")
        self.assertIn("- work.txt:1 a blocker", prompts[-1])
        self.assertEqual(self.statuses(), ["blocked"])

    def test_fix_rounds_past_the_third_escalate(self):
        """Pack sd:9264: early fix rounds run on the bundle's model; the class-pass round runs on the strongest
        model at medium effort (sd:10192), its brief naming the blockers' class; the park stays."""
        R = build_run.REVIEW_ROUNDS
        self.press()
        self.plan(claude=["pass", *(f"fix{n}" for n in range(1, R + 1))], sd_review=["blocking"] * (R + 1))
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertEqual(len(self.calls("sd-review")), R + 1, "the park moved")
        fixes = [call["argv"] for call in self.calls("claude")][1:]
        models = [(argv[argv.index("--model") + 1], "--effort" in argv) for argv in fixes]
        E = build_run.ESCALATE_AFTER
        self.assertLess(E, R, "no fix escalates before the park (sd:10111)")
        self.assertEqual(models[:E], [("opus", False)] * E, "an early round escalated")
        self.assertEqual(models[E:], [(build_run.STRONGEST, True)] * (R - E), "a late round did not escalate")
        self.assertEqual(fixes[E][fixes[E].index("--effort") + 1], "medium", "sd:10192")
        prompts = [argv[argv.index("-p") + 1] for argv in fixes]
        self.assertNotIn("name the class", prompts[E - 1])
        self.assertIn("name the class", prompts[E])
        escalated = [body for (body,) in self.db.execute("SELECT body FROM note WHERE item = ?", (self.items[0],))
                     if "escalated" in body]
        self.assertEqual(len(escalated), 1, "the escalation is not recorded once on the run's item")

    def test_review_rounds_cap_is_two(self):
        """Operator 2026-11-23 (sd:10128): two rounds with blockers, then the class pass: a park after 3 reviews."""
        self.assertEqual(build_run.REVIEW_ROUNDS, 2)
        self.assertEqual(build_run.ESCALATE_AFTER, 1)

    def test_review_rounds_rule_reaches_the_builder(self):
        """Pack sd:9584: the brief asks for a failure table on a parser or event matching; the class pass keeps scope."""
        self.assertIn("a parser, a journal or event matching", build_run.BRIEF)
        self.assertIn("within this change's own scope", build_run.CLASS_PASS)
        self.assertIn("becomes its own item", build_run.CLASS_PASS)

    def test_the_brief_lists_the_blocker_classes_reviews_found_most(self):
        """sd:9770: the brief carries the classes recorded review rounds found most, top first, with counts."""
        self.press()
        item = self.items[0]
        for _ in range(2):
            add_note(self.db, item, "comment", build.ROUND_BLOCKERS + "- a.py:3 a race between two writers\n"
                     "- b.py:4 a retry charges the cost twice")
        add_note(self.db, item, "comment", build.ROUND_BLOCKERS + "- c.py:1 a race on the lease")
        add_note(self.db, item, "comment", "Some other comment about a stale head")
        old = add_note(self.db, item, "comment", build.ROUND_BLOCKERS + "- d.py:1 a stale head")
        self.db.execute("UPDATE note SET timestamp = '2000-01-01T00:00:00+00:00' WHERE id = ?", (old,))
        prompt = build_run.BRIEF.format(**self.supervisor().brief())
        race = "- two writers or a read-then-write that another process can interleave (3)"
        retry = "- a step run twice: a retry or replay that writes or charges again (2)"
        self.assertIn("Before the first review, check the change against the blocker classes", prompt)
        self.assertIn(race, prompt)
        self.assertIn(retry, prompt)
        self.assertLess(prompt.index(race), prompt.index(retry), "the most found class comes first")
        self.assertNotIn("went stale", prompt, "neither an old round nor another note counts")

    def test_a_brief_with_no_recorded_round_has_no_checklist(self):
        """sd:9770: no recorded blocker, no checklist."""
        self.press()
        self.assertEqual(self.supervisor().brief()["checklist"], "")

    def test_a_blocked_round_records_its_blockers_for_the_checklist(self):
        """sd:9770: each blocked round notes its blockers on the lead, once, as the checklist's record."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix"], sd_review=["blocking", "clean"])
        ended = self.supervisor().run()
        self.assertEqual(ended.get("step"), "enqueued", ended)
        notes = [body for (body,) in self.db.execute("SELECT body FROM note WHERE item = ? AND body LIKE ?",
                                                     (self.items[0], build.ROUND_BLOCKERS + "%"))]
        self.assertEqual(notes, [build.ROUND_BLOCKERS + "- work.txt:1 a blocker (class correctness; instances: src.py:1)"])

    def design(self, body):
        """A builder commit that adds a docs/work design.md with no failure table, and `body` as the PR body."""
        (self.plans / "extra.json").write_text(json.dumps({"files": {
            "docs/work/2026-11-21-example/design.md": b"# Example\n\nitem: sd:1\n\nSteps move state.\n".hex()}}))
        self.press()
        self.write_pr(body=body)
        self.plan(claude=["extra"], sd_review=["clean"])

    def test_a_state_moving_change_with_no_failure_table_does_not_reach_review(self):
        """sd:9770: a change with a docs/work design.md and no failure table never starts review; the refusal
        names the rule."""
        self.design("## Summary\n\nx\n\n## Test plan\n\ny\n")
        with mock.patch.object(build_run, "REVIEW_ROUNDS", 1):
            ended = self.supervisor().run()
        self.assertEqual(ended.get("outcome"), "blocked", ended)
        self.assertEqual(self.calls("sd-review"), [], "no review ran")
        self.assertIn("failure table", self.latest()["detail"])
        self.assertIn("WORKFLOW.md, Review rounds", self.latest()["detail"])
        fix = self.calls("claude")[1]["argv"]
        self.assertIn("docs/work/2026-11-21-example/design.md", fix[fix.index("-p") + 1])

    def untabled_body_blocks(self, table):
        """Review of sd:9770: a table is a header naming the five columns and at least one data row."""
        self.design(f"## Summary\n\n{table}\n## Test plan\n\ny\n")
        with mock.patch.object(build_run, "REVIEW_ROUNDS", 1):
            ended = self.supervisor().run()
        self.assertEqual(ended.get("outcome"), "blocked", ended)
        self.assertEqual(self.calls("sd-review"), [], "no review ran")

    def test_prose_naming_the_columns_is_no_failure_table(self):
        self.untabled_body_blocks("Each step needs state, failure, recovery and test.\n")

    def test_a_header_with_no_rows_is_no_failure_table(self):
        self.untabled_body_blocks("```\n| Step | State moved | Failure | Recovery | Test |\n"
                                  "| --- | --- | --- | --- | --- |\n```\n")

    def test_a_table_is_a_header_and_a_data_row(self):
        header = "| Step | State moved | Failure | Recovery | Test |\n"
        row = "| write | note | crash | replay | test_x |\n"
        plain = "step   | state moved | failure | recovery | test\nwrite  | note        | crash   | replay   | test_x\n"
        for text, expected in (("Each step needs state, failure, recovery and test.", False), (header, False),
                               (header + "| --- | --- | --- | --- | --- |\n", False),
                               (header + "| --- | --- | --- | --- | --- |\n" + row, True), (header + row, True),
                               (plain, True), (header + "\n" + row, False)):
            with self.subTest(text=text):
                self.assertIs(build_run.failure_table(text), expected)

    def test_a_state_moving_change_with_a_failure_table_reaches_review(self):
        """sd:9770: the same change with the table in the body is reviewed and enqueued."""
        self.design("## Summary\n\nx\n\n```\n| Step | State moved | Failure | Recovery | Test |\n"
                    "| --- | --- | --- | --- | --- |\n| write | note | crash | replay | test_x |\n```\n\n"
                    "## Test plan\n\ny\n")
        ended = self.supervisor().run()
        self.assertEqual(ended.get("step"), "enqueued", ended)
        self.assertEqual(len(self.calls("sd-review")), 1)

    TABLE = "| Step | State moved | Failure | Recovery | Test |\n| --- | --- | --- | --- | --- |\n| w | n | c | r | t |\n"

    def designs_block(self, files, unadded=None, body="## Summary\n\nx\n\n## Test plan\n\ny\n"):
        (self.plans / "extra.json").write_text(json.dumps({
            "files": {name: text.encode().hex() for name, text in files.items()},
            "unadded": {name: text.encode().hex() for name, text in (unadded or {}).items()}}))
        self.press()
        self.write_pr(body=body)
        self.plan(claude=["extra"], sd_review=["clean"])
        with mock.patch.object(build_run, "REVIEW_ROUNDS", 1):
            ended = self.supervisor().run()
        self.assertEqual(ended.get("outcome"), "blocked", ended)
        self.assertEqual(self.calls("sd-review"), [], "no review ran")
        return self.latest()["detail"]

    def test_each_changed_design_needs_its_own_failure_table(self):
        """Review of sd:9770: one design's table does not clear another design in the same bundle."""
        detail = self.designs_block({"docs/work/2026-11-21-a/design.md": f"# A\n\n{self.TABLE}",
                                     "docs/work/2026-11-21-b/design.md": "# B\n\nSteps move state.\n"})
        self.assertIn("docs/work/2026-11-21-b/design.md", detail)
        self.assertNotIn("docs/work/2026-11-21-a/design.md", detail)

    def test_a_non_ascii_design_path_needs_its_failure_table(self):
        """Lane review of sd:9770: git quotes a non-ASCII path in its listings unless told not to; a design in
        a folder named with one is still read, and blocks without a table."""
        detail = self.designs_block({"docs/work/2026-11-21-café/design.md": "# Café\n\nSteps move state.\n"})
        self.assertIn("docs/work/2026-11-21-café/design.md", detail)

    def test_a_body_table_covers_a_bundles_design_only_when_it_names_it(self):
        """Review of sd:9770: with two designs, the body's table covers only the design it names."""
        body = f"## Summary\n\nx\n\ndocs/work/2026-11-21-a\n\n```\n{self.TABLE}```\n\n## Test plan\n\ny\n"
        detail = self.designs_block({"docs/work/2026-11-21-a/design.md": "# A\n",
                                     "docs/work/2026-11-21-b/design.md": "# B\n"}, body=body)
        self.assertIn("docs/work/2026-11-21-b/design.md", detail)
        self.assertNotIn("docs/work/2026-11-21-a/design.md", detail)

    def test_an_uncommitted_design_does_not_satisfy_the_table(self):
        """Review of sd:9770: the precheck reads the committed head; an ignored design with a table, which the
        clean review worktree never sees, clears nothing."""
        detail = self.designs_block(
            {"docs/work/2026-11-21-a/design.md": "# A\n\nitem: sd:1\n", ".gitignore": "docs/work/2026-11-21-z/\n"},
            unadded={"docs/work/2026-11-21-z/design.md": f"# Z\n\nitem: sd:1\n\n{self.TABLE}"})
        self.assertIn("docs/work/2026-11-21-a/design.md", detail)
        self.assertNotIn("2026-11-21-z", detail)

    def test_deleting_a_finished_design_md_needs_no_failure_table(self):
        """Review of sd:9770: removing a done item's design.md moves no state; it reaches review untabled."""
        done = "docs/work/2026-10-01-done/design.md"
        (self.repo / done).parent.mkdir(parents=True)
        (self.repo / done).write_text("# Done\n\nitem: sd:1\n", encoding="utf-8")
        support.git(self.repo, "add", done)
        support.git(self.repo, "commit", "-qm", "docs: a finished design")
        support.git(self.repo, "push", "-q", "origin", "main")
        (self.plans / "extra.json").write_text(json.dumps({"remove": [done]}))
        self.press()
        self.write_pr(body="## Summary\n\nx\n\n## Test plan\n\ny\n")
        self.plan(claude=["extra"], sd_review=["clean"])
        ended = self.supervisor().run()
        self.assertEqual(ended.get("step"), "enqueued", ended)
        self.assertEqual(len(self.calls("sd-review")), 1)

    def test_a_question_on_one_member_keeps_the_class_pass_for_the_other(self):
        """Review of sd:9263: a question on a skipped member B must not park A, which the class pass can still fix."""
        R = build_run.REVIEW_ROUNDS
        self.press(2)
        self.plan(claude=["pass", *(f"fix{n}" for n in range(1, R + 1))], sd_review=["blocking"] * (R + 1))
        add_note(self.db, self.items[1], "question", "Which option?\nOption: Yes (Recommended)\nOption: No")
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn(f"{R + 1} review rounds left blockers", self.latest()["detail"])
        self.assertEqual(len(self.calls("sd-review")), R + 1)

    def test_an_open_question_skips_the_class_pass(self):
        """sd:9263: no fix round answers a question, so a build that asked one parks at REVIEW_ROUNDS."""
        R = build_run.REVIEW_ROUNDS
        self.press()
        self.plan(claude=["pass", *(f"fix{n}" for n in range(1, R + 1))], sd_review=["blocking"] * (R + 1))
        add_note(self.db, self.items[0], "question", "Which option?\nOption: Yes (Recommended)\nOption: No")
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn(f"{R} review rounds left blockers: work.txt:1 a blocker", self.latest()["detail"])
        self.assertEqual(len(self.calls("sd-review")), R)

    def asked_and_resolved(self, item):
        """A question on `item` the operator closed with `sd note resolve`: it waits on no one, but it is still
        the evidence that the builder skipped the item (review round 3 of sd:9219)."""
        resolve_note(self.db, add_note(self.db, item, "question", "Which option?\nOption: Yes (Recommended)\nOption: No"))

    def test_a_resolved_question_still_skips_the_class_pass(self):
        """Class pass, instance `blocked_round`: the skipped item is still skipped, so no class pass fixes it."""
        R = build_run.REVIEW_ROUNDS
        self.press()
        self.plan(claude=["pass", *(f"fix{n}" for n in range(1, R + 1))], sd_review=["blocking"] * (R + 1))
        self.asked_and_resolved(self.items[0])
        self.supervisor().run()
        self.assertEqual(len(self.calls("sd-review")), R)

    def test_a_resolved_question_still_keeps_an_older_trailer_from_delivering(self):
        """Class pass, instance `enqueue` delivery: an older run's `Closes:` trailer cites the item, but this
        run's builder skipped it with a question since resolved; the merge must not close it."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        self.asked_and_resolved(self.items[0])
        ended = self.supervisor().run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertEqual([call for call in self.calls("sd-ship") if "enqueue" in call["argv"]], [])

    def test_a_resolved_question_still_blocks_a_skipped_member(self):
        """Class pass, instance `build.enqueued`: a member skipped with a question since resolved stays blocked
        for the operator, not returned to planning as one skipped with no word."""
        self.press(2)
        self.write_pr()
        self.plan(claude=["skip"], sd_review=["clean"])
        self.asked_and_resolved(self.items[1])
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        self.assertEqual(self.statuses(), ["in_progress", "blocked"])

    def test_a_fix_then_a_clean_review_enqueues(self):
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1"], sd_review=["blocking", "clean"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        self.assertEqual(self.latest()["review_round"], 1)

    def test_a_verify_review_with_no_answer_retries_in_full(self):
        """Review of sd:9769: `sd-review` can refuse a verify plan (a prior finding's file past its input bound);
        the retry reviews the branch in full instead of repeating the same refused plan."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1"], sd_review=["blocking", "garbage", "clean"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued", self.latest()["detail"])
        _first, verify, retry = [call["argv"] for call in self.calls("sd-review")]
        self.assertIn("--verify-report", verify)
        self.assertNotIn("--verify-report", retry, "the retry repeated the refused verify plan")
        self.assertNotIn("--base", retry)

    def test_a_clean_delta_that_covers_no_branch_reviews_in_full(self):
        """Class pass, sd:9769 (a delta verdict stands for the branch only on a report that covers it): a clean
        verify whose own report covers nothing (another binding, a short round) is not the branch's pass."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1"], sd_review=["blocking", "clean_uncovered", "clean"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued", self.latest()["detail"])
        _first, verify, full = [call["argv"] for call in self.calls("sd-review")]
        self.assertIn("--verify-report", verify)
        self.assertNotIn("--verify-report", full, "a clean delta covering no branch was taken as the branch's pass")

    def test_a_skipped_delta_after_a_blocking_round_reviews_in_full_and_stays_blocked(self):
        """Lead ruling on sd:9769 (deferral eligibility): a docs-only fix delta the router skips proves no
        prior code blocker fixed; the branch is reviewed in full, and its blocker still blocks."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1", "fix2"], sd_review=["blocking", "skip_route", "blocking", "clean"])
        self.supervisor().run()
        argvs = [call["argv"] for call in self.calls("sd-review")]
        self.assertGreater(len(argvs), 2, "a skipped delta was taken as the branch's pass")
        self.assertIn("--verify-report", argvs[1])
        self.assertNotIn("--verify-report", argvs[2], "the skipped delta was not reviewed in full")
        self.assertEqual(self.latest()["review_round"], 2, "the prior blocker did not stay blocked")

    def test_a_complete_blocking_delta_that_covers_no_branch_defers_nothing(self):
        """Lead ruling on sd:9769 (deferral eligibility): `sd-review` clears `covers_from` after a binding
        change; a full reader count does not restore branch coverage, so no blocker is filed away."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1", "fix2"], sd_review=["blocking", "offdelta_uncovered", "clean"])
        self.supervisor().run()
        filed = self.db.execute("SELECT id FROM item WHERE title LIKE 'Review follow-up%'").fetchall()
        self.assertEqual(filed, [], "a blocker from a delta covering no branch was filed as a follow-up")
        self.assertEqual(self.latest()["review_round"], 2, "the uncovered blocker did not block")

    def test_round_one_reviews_the_whole_branch_and_round_two_the_findings_and_delta(self):
        """Pack sd:9769: round 1 is the full branch review; round 2 verifies round 1's findings over the delta."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1"], sd_review=["blocking", "clean"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        first, second = [call["argv"] for call in self.calls("sd-review")]
        self.assertNotIn("--base", first, "round 1 is not the full branch review")
        self.assertNotIn("--verify-report", first)
        prior = json.loads(Path(second[second.index("--verify-report") + 1]).read_text(encoding="utf-8"))
        self.assertEqual(prior["status"], "blocking", "round 2 does not verify round 1's findings")
        self.assertEqual(second[second.index("--base") + 1], prior["subject"]["head"],
                         "round 2 does not review the delta since the last reviewed head")
        fix = [call["argv"] for call in self.calls("claude")][1]
        self.assertIn("instances: src.py:1", fix[fix.index("-p") + 1], "the fix brief lacks the instance list")

    def test_a_new_round_two_finding_on_unchanged_code_files_a_follow_up(self):
        """Pack sd:9769: a later round's finding off the delta and off the earlier findings is filed, not a block."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1"], sd_review=["blocking", "offdelta"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued", "an off-delta finding blocked")
        self.assertEqual(self.latest()["review_round"], 1)
        filed = self.db.execute("SELECT id, title, repo FROM item WHERE title LIKE 'Review follow-up%'").fetchall()
        self.assertEqual(len(filed), 1, "no follow-up item was filed")
        self.assertIn("far.txt:1", filed[0]["title"])

    def test_a_filed_follow_up_is_a_rebuttal_the_lane_review_honours(self):
        """Review of sd:9769: the lane reviews the branch again; the same finding must not block it there."""
        import importlib.machinery
        import types
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1"], sd_review=["blocking", "offdelta"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        bodies = [(row["body"],) for row in self.db.execute(
            "SELECT body FROM state WHERE kind = 'checkpoint' AND key LIKE 'ship-adjudication:%'")]
        bin_dir = str(Path(build_run.__file__).resolve().parents[2] / "bin")
        sys.path.insert(0, bin_dir)
        try:
            loader = importlib.machinery.SourceFileLoader("sd_review_lane", os.path.join(bin_dir, "sd-review"))
            review = types.ModuleType(loader.name)
            review.__file__ = loader.path
            loader.exec_module(review)
        finally:
            sys.path.remove(bin_dir)
        recorded = review.rebuttals(bodies, lambda head: True, lambda head, path: True)
        far = {"path": "far.txt", "line": 1, "severity": "high", "instances": ["far.txt:1"],
               "summary": "an old defect", "family": "correctness"}
        self.assertEqual([row["disposition"] for row in review.dispose([far], "high", recorded)], ["advisory"],
                         "the lane would block on the finding the build filed")

    def test_a_round_that_still_blocks_records_no_rebuttal(self):
        """Review of sd:9769: a fix round follows a round that holds a blocker; a rebuttal written there would
        outlive the code its deferral judged, so the follow-up is filed and the lane review stays free to block."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1", "fix2"], sd_review=["blocking", "offdelta_held", "clean"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        filed = self.db.execute("SELECT title FROM item WHERE title LIKE 'Review follow-up%'").fetchall()
        self.assertEqual(len(filed), 1, "the deferred blocker was not filed")
        rebuttals = self.db.execute(
            "SELECT COUNT(*) FROM state WHERE kind = 'checkpoint' AND key LIKE 'ship-adjudication:%'").fetchone()[0]
        self.assertEqual(rebuttals, 0, "a round that still blocked recorded a rebuttal a later fix outlives")

    def test_a_short_blocking_report_is_never_made_advisory(self):
        """Review of sd:9769: a blocking report missing a reader's answer blocks, whatever the delta."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1", "fix2"], sd_review=["blocking", "offdelta_short", "clean"])
        self.supervisor().run()
        self.assertEqual(len(self.calls("sd-review")), 3, "a short report was taken as a pass")

    def test_an_absolute_blocker_path_reaches_the_next_round_relative(self):
        """Review of sd:9769: `sd-review` refuses an absolute prior path, so the kept report holds it relative."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "fix1"], sd_review=["absolute", "clean"])
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        second = self.calls("sd-review")[1]["argv"]
        prior = json.loads(Path(second[second.index("--verify-report") + 1]).read_text(encoding="utf-8"))
        self.assertEqual(prior["findings"][0]["path"], "work.txt", "an absolute path reached the next round")

    def scratch_repo(self, **config):
        """A throwaway repository with `config` set in it, and a `git` runner over it."""
        repo = Path(tempfile.mkdtemp(dir=self.root))

        def run(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator",
                                   "-c", "user.email=operator@example.test", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()

        run("init", "-q")
        for name, value in config.items():
            run("config", name.replace("_", "."), value)
        return repo, run

    def test_an_external_diff_or_textconv_does_not_reach_the_delta(self):
        """Class pass, sd:9769: the operator's diff.external or a textconv driver must not shape the delta."""
        repo, run = self.scratch_repo(diff_external="true", diff_drop_textconv="sed 1d")
        (repo / ".gitattributes").write_text("conv.txt diff=drop\n")
        (repo / "plain.txt").write_text("a\nb\n")
        (repo / "conv.txt").write_text("a\nb\nc\n")
        run("add", "-A")
        run("commit", "-q", "-m", "base")
        base = run("rev-parse", "HEAD")
        (repo / "plain.txt").write_text("a\nB\n")
        (repo / "conv.txt").write_text("a\nb\nC\n")
        run("commit", "-qam", "change")
        self.assertEqual(build_run.changed_lines(["git"], str(repo), base, run("rev-parse", "HEAD")),
                         {"plain.txt": {2}, "conv.txt": {3}})

    def test_the_operator_s_diff_prefixes_do_not_reach_the_delta(self):
        """Class pass, sd:9769: diff.noprefix or diff.mnemonicPrefix must not make the delta unreadable."""
        for config in ({"diff_noprefix": "true"}, {"diff_mnemonicPrefix": "true"}):
            with self.subTest(config=config):
                repo, run = self.scratch_repo(**config)
                (repo / "a.txt").write_text("a\n")
                run("add", "-A")
                run("commit", "-q", "-m", "base")
                base = run("rev-parse", "HEAD")
                (repo / "a.txt").write_text("b\n")
                run("commit", "-qam", "change")
                self.assertEqual(build_run.changed_lines(["git"], str(repo), base, run("rev-parse", "HEAD")),
                                 {"a.txt": {1}})

    def test_output_with_no_file_header_reads_as_unknown(self):
        """Class pass, sd:9769: git output the parser names no file in is unknown, never an empty delta."""
        with mock.patch.object(build_run, "_read", return_value="some driver's own rendering\n"):
            self.assertIsNone(build_run.changed_lines(["git"], "work", "a" * 40, "b" * 40))
        with mock.patch.object(build_run, "_read", return_value=""):
            self.assertEqual(build_run.changed_lines(["git"], "work", "a" * 40, "b" * 40), {})

    def test_a_diff_in_a_legacy_encoding_reads_as_unknown(self):
        """Review of sd:9769: a raw Latin-1 byte in a hunk is an unknown delta that holds every blocker,
        never a decode error that stops the supervisor."""
        latin1 = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'diff --git a/x b/x\\n+caf\\xe9\\n')"]
        self.assertIsNone(build_run.changed_lines(latin1, "work", "a" * 40, "b" * 40))

    def test_a_mode_or_binary_change_reads_as_the_whole_file(self):
        """Review of sd:9769: git gives a mode-only or binary change no line range; any line of it is changed."""
        repo = Path(tempfile.mkdtemp(dir=self.root))

        def run(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator",
                                   "-c", "user.email=operator@example.test", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()

        run("init", "-q")
        (repo / "run.sh").write_text("#!/bin/sh\necho\n")
        (repo / "run.sh").chmod(0o755)
        (repo / "blob.bin").write_bytes(b"\0one")
        run("add", "-A")
        run("commit", "-q", "-m", "base")
        base = run("rev-parse", "HEAD")
        (repo / "run.sh").chmod(0o644)
        (repo / "blob.bin").write_bytes(b"\0two")
        run("add", "-A")
        run("commit", "-q", "-m", "mode")
        self.assertEqual(build_run.changed_lines(["git"], str(repo), base, run("rev-parse", "HEAD")),
                         {"run.sh": set(), "blob.bin": set()})
        self.press()
        _, kept = self.deferred([{"path": "run.sh", "line": 2, "summary": "lost its executable bit"}],
                                {"run.sh": set()})
        self.assertEqual(len(kept["blockers"]), 1, "a blocker on a mode change was deferred")

    def test_a_mode_change_with_a_content_hunk_reads_as_the_whole_file(self):
        """Review of sd:9769: a fix that edits a script and drops its executable bit changes every line of it,
        not only the edited ones: a blocker on the unchanged shebang is the change's own."""
        repo = Path(tempfile.mkdtemp(dir=self.root))

        def run(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator",
                                   "-c", "user.email=operator@example.test", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()

        run("init", "-q")
        (repo / "run.sh").write_text("#!/bin/sh\necho one\n")
        (repo / "run.sh").chmod(0o755)
        run("add", "-A")
        run("commit", "-q", "-m", "base")
        base = run("rev-parse", "HEAD")
        (repo / "run.sh").write_text("#!/bin/sh\necho two\n")
        (repo / "run.sh").chmod(0o644)
        run("add", "-A")
        run("commit", "-q", "-m", "edit and mode")
        self.assertEqual(build_run.changed_lines(["git"], str(repo), base, run("rev-parse", "HEAD")),
                         {"run.sh": set()}, "a mode change with a content hunk lost whole-file coverage")

    def test_a_path_with_a_space_reads_without_git_s_tab(self):
        """Review of sd:9769: git ends a header naming a path with a space with a tab."""
        repo = Path(tempfile.mkdtemp(dir=self.root))

        def run(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator",
                                   "-c", "user.email=operator@example.test", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()

        run("init", "-q")
        (repo / "a b.txt").write_text("x\n")
        (repo / "gone b.txt").write_text("y\n")
        run("add", "-A")
        run("commit", "-q", "-m", "base")
        base = run("rev-parse", "HEAD")
        (repo / "a b.txt").write_text("z\n")
        (repo / "gone b.txt").unlink()
        run("add", "-A")
        run("commit", "-q", "-m", "change")
        self.assertEqual(build_run.changed_lines(["git"], str(repo), base, run("rev-parse", "HEAD")),
                         {"a b.txt": {1}, "gone b.txt": {1}})

    PRIOR = {"subject": {"head": "b" * 40}, "findings": [{"path": "named.py", "line": 1}],
             "requested_reviews": 2, "completed_reviews": 2, "covers_from": "c" * 40}

    def test_a_blocker_on_a_line_the_branch_added_before_the_fix_still_blocks(self):
        """Class pass, sd:9769: deferral is for code the branch does not change, not code the fix did not touch."""
        self.press()
        supervisor = self.supervisor()
        early = {"path": "early.py", "line": 5, "summary": "round one's own defect"}

        def delta(_git, _work, base, _head, env=None):
            return {} if base == "b" * 40 else {"early.py": {5}, "whole.bin": set()}
        with mock.patch.object(build_run, "changed_lines", side_effect=delta), \
                mock.patch.object(build_run, "_read", return_value="f" * 40):  # the fork reads
            self.assertEqual(supervisor.branch_delta("b" * 40, "a" * 40),
                             {"early.py": {5}, "whole.bin": set()}, "the branch's own lines are not in the delta")
            kept = supervisor.file_new_findings({"status": "blocking", "blockers": [early]}, self.PRIOR, "a" * 40)
        self.assertEqual(kept["blockers"], [early], "a blocker on the branch's own line was deferred")

    def test_a_short_prior_report_defers_nothing(self):
        """Class pass, sd:9769: deferral trusts the last round's findings; a round short of a reader proves none."""
        self.press()
        supervisor = self.supervisor()
        row = {"path": "far.py", "line": 1, "summary": "a"}
        for short in ({"completed_reviews": 1}, {"requested_reviews": 0, "completed_reviews": 0},
                      {"requested_reviews": 1, "completed_reviews": 1,
                       "review_shortfall": {"requested": 2, "eligible": 1}}):
            with self.subTest(short=short), mock.patch.object(supervisor, "branch_delta", return_value={}):
                kept = supervisor.file_new_findings({"status": "blocking", "blockers": [row]},
                                                    {**self.PRIOR, **short}, "a" * 40)
                self.assertEqual(kept["blockers"], [row], "a short prior report deferred a blocker")

    def test_a_prior_report_covering_no_branch_defers_nothing(self):
        """Review of sd:9769: a patch-only reader's pass completes every review yet `sd-review` clears its
        `covers_from`; it read no whole file, so it proves no finding absent and every blocker holds."""
        self.press()
        supervisor = self.supervisor()
        row = {"path": "far.py", "line": 1, "summary": "a"}
        with mock.patch.object(supervisor, "branch_delta", return_value={}):
            kept = supervisor.file_new_findings({"status": "blocking", "blockers": [row]},
                                                {**self.PRIOR, "covers_from": None}, "a" * 40)
        self.assertEqual(kept["blockers"], [row], "a prior report covering no branch deferred a blocker")

    def test_a_blocking_answer_short_of_its_policy_is_not_complete(self):
        """Class pass, sd:9769: the capped `requested_reviews` hides a chain shorter than the policy asked."""
        self.press()
        supervisor = self.supervisor()
        out = Path(tempfile.mkdtemp(dir=self.root)) / "review.out"
        out.write_text(json.dumps({"status": "blocking", "requested_reviews": 1, "completed_reviews": 1,
                                   "review_shortfall": {"requested": 2, "eligible": 1},
                                   "subject": {"head": "a" * 40}, "findings": []}), encoding="utf-8")
        self.assertFalse(supervisor.review_answer(out, "a" * 40)["complete"],
                         "a round one reader short of its policy counts as complete")

    def test_a_replayed_delta_review_keeps_its_verify_arguments(self):
        """Review of sd:9769: a hub fault after a delta review wrote its report replays the step. The report at
        HEAD must not read as a fix with no commit: the held review's own arguments run, so it answers from its
        record instead of a full review appending a second JSON object."""
        self.press()
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        head, earlier = "a" * 40, "b" * 40
        prior = {**self.PRIOR, "status": "blocking", "scope": "branch", "subject": {"head": earlier}}
        supervisor.out("prior").write_text(json.dumps(prior), encoding="utf-8")
        supervisor.out("review").write_text(json.dumps(
            {**prior, "status": "advisory", "subject": {"head": head}}), encoding="utf-8")
        argv = [*supervisor.tools.review, "-C", str(supervisor.work), "--scope", "branch", "--json",
                "--verify-report", str(supervisor.out("prior")), "--base", earlier]
        supervisor.held = {"name": "review", "argv": argv, "exit": 0}
        with mock.patch.object(supervisor, "fresh", return_value={"review_round": 1}), \
                mock.patch.object(build_run, "_read", return_value=""):
            planned, chosen = supervisor.review_plan(head)
        self.assertEqual(planned, argv, "a replayed delta review dropped its verify arguments")
        self.assertEqual(chosen["subject"]["head"], earlier)

    def test_a_replay_held_at_the_checks_keeps_the_delta_review_s_prior(self):
        """Review of sd:9769: a hub fault during an unmanaged repo's checks replays the review step. The review's
        report sits at HEAD; the prior it verified must still decide, or deferred findings go back to the
        builder. A full review's report (no verified digest) keeps no prior."""
        self.press()
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        head, earlier = "a" * 40, "b" * 40
        prior = {**self.PRIOR, "status": "blocking", "scope": "branch", "subject": {"head": earlier}}
        supervisor.out("prior").write_text(json.dumps(prior), encoding="utf-8")
        digest = hashlib.sha256(json.dumps(prior, sort_keys=True).encode()).hexdigest()
        supervisor.held = {"name": "checks", "argv": ["checks"], "exit": 1}
        for verified, expected in ((digest, earlier), (None, None)):
            supervisor.out("review").write_text(json.dumps(
                {**prior, "subject": {"head": head}, "verification_report_digest": verified}), encoding="utf-8")
            with self.subTest(verified=bool(verified)), \
                    mock.patch.object(supervisor, "fresh", return_value={"review_round": 1}), \
                    mock.patch.object(build_run, "_read", return_value=""):
                _planned, chosen = supervisor.review_plan(head)
                self.assertEqual((chosen or {}).get("subject", {}).get("head"), expected,
                                 "a replay held at the checks lost the delta review's prior")

    def test_a_short_blocking_round_is_no_prior_to_verify(self):
        """Review of sd:9769: a blocking round short of a reader read part of the patch; the next round reviews
        the branch in full, not the delta since it."""
        self.press()
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        for short in ({"requested_reviews": 1, "completed_reviews": 1,
                       "review_shortfall": {"requested": 2, "eligible": 1}},
                      {"requested_reviews": 2, "completed_reviews": 1}):
            supervisor.out("prior").write_text("", encoding="utf-8")
            supervisor.out("review").write_text(json.dumps(
                {**self.PRIOR, **short, "status": "blocking", "scope": "branch", "subject": {"head": "b" * 40}}),
                encoding="utf-8")
            with self.subTest(short=short), mock.patch.object(supervisor, "fresh", return_value={"review_round": 1}), \
                    mock.patch.object(build_run, "_read", return_value=""):
                self.assertIsNone(supervisor.prior_review("a" * 40), "a short blocking round seeded a delta review")

    def test_a_round_covering_no_branch_is_no_prior_to_verify(self):
        """Class pass, sd:9769: a last round with no `covers_from` (patch-only, or under another binding) stands
        for no branch; a delta since it would leave the earlier commits unreviewed, so the next round is full."""
        self.press()
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        for status in ("blocking", "clean"):
            supervisor.out("prior").write_text("", encoding="utf-8")
            supervisor.out("review").write_text(json.dumps(
                {**self.PRIOR, "covers_from": None, "status": status, "scope": "branch", "subject": {"head": "b" * 40}}),
                encoding="utf-8")
            with self.subTest(status=status), mock.patch.object(supervisor, "fresh", return_value={"review_round": 1}), \
                    mock.patch.object(build_run, "_read", return_value=""):
                self.assertIsNone(supervisor.prior_review("a" * 40), "a round covering no branch seeded a delta review")

    def test_a_skipped_last_round_is_no_prior_to_verify(self):
        """Review of sd:9769: `sd-review` refuses a `skipped` report as `--verify-report`; the next round
        reviews in full instead of parking on that refusal twice."""
        self.press()
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        supervisor.out("review").write_text(json.dumps(
            {"status": "skipped", "scope": "branch", "subject": {"head": "b" * 40}, "findings": [],
             "route": {"tier": "skip"}}), encoding="utf-8")
        with mock.patch.object(supervisor, "fresh", return_value={"review_round": 1}), \
                mock.patch.object(build_run, "_read", return_value=""):
            self.assertIsNone(supervisor.prior_review("a" * 40), "a skipped report was chosen to verify")

    def test_a_prior_report_past_sd_reviews_bound_drops_to_a_full_review(self):
        """Review of sd:9769: `sd-review` refuses a prior report over its 2 MB input bound before it writes any
        JSON; the next round reviews in full rather than failing twice on that refusal."""
        self.press()
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        supervisor.out("review").write_text(json.dumps(
            {"status": "advisory", "scope": "branch", "subject": {"head": "b" * 40},
             "findings": [{"path": "src.py", "line": 1, "summary": "x" * 2_000_000}]}), encoding="utf-8")
        with mock.patch.object(supervisor, "fresh", return_value={"review_round": 1}), \
                mock.patch.object(build_run, "_read", return_value=""):
            self.assertTrue(supervisor.prior_review("a" * 40) is None, "a report sd-review refuses was chosen to verify")

    def test_a_prior_blocker_sd_review_would_refuse_drops_to_a_full_review(self):
        """Review of sd:9769: `sd-review` refuses a prior blocking path outside the checkout or not a tracked
        regular file; the next round reviews in full rather than failing twice on that refusal."""
        self.press()
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)

        def read(argv, env=None):
            return "040000 tree 0123\tsub\0" if "ls-tree" in argv and argv[-1].endswith("sub") else ""
        for path in ("/elsewhere/x.py", "../up.py", "sub"):
            supervisor.out("review").write_text(json.dumps(
                {"status": "blocking", "scope": "branch", "subject": {"head": "b" * 40},
                 "findings": [{"path": path, "line": 1, "disposition": "blocking"}]}), encoding="utf-8")
            with self.subTest(path=path), mock.patch.object(supervisor, "fresh", return_value={"review_round": 1}), \
                    mock.patch.object(build_run, "_read", side_effect=read):
                self.assertIsNone(supervisor.prior_review("a" * 40), f"{path} was handed to --verify-report")
        supervisor.out("review").write_text(json.dumps(
            {"status": "blocking", "scope": "branch", "subject": {"head": "b" * 40},
             "requested_reviews": 1, "completed_reviews": 1, "covers_from": "c" * 40,
             "findings": [{"path": "src.py", "line": 1, "disposition": "blocking"}]}), encoding="utf-8")
        with mock.patch.object(supervisor, "fresh", return_value={"review_round": 1}), \
                mock.patch.object(build_run, "_read", return_value="100644 blob 0123\tsrc.py\0"):
            self.assertIsNotNone(supervisor.prior_review("a" * 40), "a tracked file's blocker dropped the chain")
        # Each instance's file too: `sd-review` attaches and checks every one (lead ruling on sd:9769).
        supervisor.out("review").write_text(json.dumps(
            {"status": "blocking", "scope": "branch", "subject": {"head": "b" * 40},
             "requested_reviews": 1, "completed_reviews": 1, "covers_from": "c" * 40,
             "findings": [{"path": "src.py", "line": 1, "disposition": "blocking",
                           "instances": ["src.py:1", "/elsewhere/x.py:2"]}]}), encoding="utf-8")
        with mock.patch.object(supervisor, "fresh", return_value={"review_round": 1}), \
                mock.patch.object(build_run, "_read", return_value="100644 blob 0123\tsrc.py\0"):
            self.assertIsNone(supervisor.prior_review("a" * 40), "an instance outside the checkout was handed on")

    def test_a_non_ascii_digit_line_reads_as_no_line(self):
        """Review of sd:9769: `str.isdigit` takes `²`, which `int` refuses; the place reads with no line."""
        self.press()
        self.assertEqual(self.supervisor().locations({"path": "src/file.py", "line": 1, "instances": ["src/file.py:\u00b2"]}),
                         [("src/file.py", 1), ("src/file.py:\u00b2", None)])

    def test_a_last_round_at_this_head_drops_to_a_full_review(self):
        """Review of sd:9769: a fix round with no commit leaves the last report at this head; the older kept
        report omits that round's blockers, so the next round reviews in full."""
        self.press()
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        for name, reviewed in (("review", "a" * 40), ("prior", "b" * 40)):
            supervisor.out(name).write_text(json.dumps(
                {"status": "blocking", "scope": "branch", "subject": {"head": reviewed}, "findings": []}),
                encoding="utf-8")
        with mock.patch.object(supervisor, "fresh", return_value={"review_round": 2}), \
                mock.patch.object(build_run, "_read", return_value=""):
            self.assertIsNone(supervisor.prior_review("a" * 40), "the older report stood in for this head's")

    def test_a_last_report_that_fails_its_checks_drops_to_a_full_review(self):
        """Review of sd:9769: a last report off the ancestry (an amended fix) or with two JSON objects (a
        replay) must not hand the round to the older kept report, which omits the last round's blockers."""
        self.press()
        supervisor = self.supervisor()
        supervisor.logs.mkdir(parents=True, exist_ok=True)
        report = {"status": "blocking", "scope": "branch", "findings": []}
        supervisor.out("prior").write_text(json.dumps({**report, "subject": {"head": "b" * 40}}),
                                           encoding="utf-8")
        last = json.dumps({**report, "subject": {"head": "c" * 40}})

        def ancestor(argv, **_kwargs):
            if "--is-ancestor" in argv and "c" * 40 in argv:
                raise build_run.Unread("not an ancestor")
            return ""

        for text, why in ((last, "an amended fix"), (last + "\n" + last, "two JSON objects")):
            with self.subTest(why=why), \
                    mock.patch.object(supervisor, "fresh", return_value={"review_round": 2}), \
                    mock.patch.object(build_run, "_read", side_effect=ancestor):
                supervisor.out("review").write_text(text, encoding="utf-8")
                self.assertIsNone(supervisor.prior_review("a" * 40),
                                  "the older kept report stood in for a last report that failed its checks")

    def deferred(self, blockers, changed, words=frozenset(), defined=frozenset(), callees=False):
        """`file_new_findings` over `blockers` with the delta `changed`: the blockers it keeps. The places'
        enclosing functions call nothing changed unless `callees` reads them from git."""
        supervisor = self.supervisor()
        with mock.patch.object(supervisor, "branch_delta", return_value=changed), \
                (contextlib.nullcontext() if callees else mock.patch.object(supervisor, "scope", return_value=(set(), []))), \
                mock.patch.object(supervisor, "branch_words",
                                  return_value=None if words is None else (set(words), set(defined))):
            return supervisor, supervisor.file_new_findings({"status": "blocking", "blockers": blockers}, self.PRIOR,
                                                            "a" * 40)

    def test_a_blocker_on_a_changed_line_by_any_spelling_or_instance_still_blocks(self):
        """Review of sd:9769: an instance on the delta, `./` or an absolute spelling of a changed path, blocks."""
        self.press()
        work = str(self.supervisor().work)
        rows = [{"path": "far.py", "line": 9, "summary": "a", "instances": ["far.py:9", "src.py:5"]},
                {"path": "./src.py", "line": 5, "summary": "b"},
                {"path": f"{work}/src.py", "line": 5, "summary": "c"},
                {"path": "./named.py", "line": 40, "summary": "d"}]
        _, kept = self.deferred(rows, {"src.py": {5}})
        self.assertEqual(kept["blockers"], rows, "a blocker on changed code was deferred")

    def test_a_blocker_on_an_unchanged_line_of_a_changed_file_still_blocks(self):
        """Review of sd:9769: a reviewer's line can be off by a few, and a change can break unchanged code in a
        file it touches; a file the branch changes is the change's own at every line."""
        self.press()
        rows = [{"path": "src.py", "line": 8, "summary": "two lines below the edit"},
                {"path": "src.py", "line": 200, "summary": "an unchanged reader the edit broke"}]
        _, kept = self.deferred(rows, {"src.py": {5, 6}})
        self.assertEqual(kept["blockers"], rows, "a blocker in a changed file was deferred")

    def test_a_blocker_at_an_unchanged_callee_naming_a_changed_caller_still_blocks(self):
        """Class pass, sd:9769 (deferral by location, not causation): a reviewer that cites an unchanged callee
        but names the changed caller in its own words ties the defect to the branch, so it holds."""
        self.press()
        rows = [{"path": "lib/helper.py", "line": 3, "summary": "src/caller.py now passes None to parse()"},
                {"path": "lib/helper.py", "line": 7, "summary": "the new call in caller.py breaks this contract"},
                {"path": "lib/helper.py", "line": 9, "summary": "the branch's change breaks this",
                 "message": "src/caller.py:12 drops the default"}]
        _, kept = self.deferred(rows, {"src/caller.py": {12}})
        self.assertEqual(kept["blockers"], rows, "a blocker naming a changed caller was deferred")
        far = {"path": "lib/helper.py", "line": 3, "summary": "an old defect in parse()"}
        _, kept = self.deferred([far], {"src/caller.py": {12}})
        self.assertEqual(kept["blockers"], [], "a blocker naming no changed file was held")

    def test_a_conversion_broken_by_a_dropped_check_holds_on_its_repro(self):
        """Sd:9900 (ruling #68857): a fix drops validation in lib/check.py, and the unchanged conversion in
        lib/convert.py, which no call ties to it, raises. The reviewer cites the conversion; its `evidence`
        repro (pack sd:9867) routes the failing input through the dropped check, so the blocker holds."""
        self.press()
        row = {"path": "lib/convert.py", "line": 12, "severity": "high", "family": "correctness",
               "summary": "int(row[1]) raises ValueError on a non-numeric count",
               "evidence": "input count='abc' passes lib/check.py:4, where the fix dropped the digit check, "
                           "then lib/convert.py:12 int(row[1]) raises ValueError",
               "instances": ["lib/convert.py:12"]}
        _, kept = self.deferred([row], {"lib/check.py": {4}})
        self.assertEqual(kept["blockers"], [row], "a conversion the dropped check breaks was filed")
        uncited = dict(row, evidence="input count='abc': lib/convert.py:12 int(row[1]) raises ValueError")
        _, kept = self.deferred([uncited], {"lib/check.py": {4}})
        self.assertEqual(kept["blockers"], [], "a repro whose route skips the change held")

    def test_a_changed_caller_named_by_function_holds(self):
        """Lead ruling on sd:9769 (deferral eligibility): a reviewer can name the changed caller by its function
        alone; a name the branch's changed functions hold ties the blocker to the branch, so it is not filed."""
        self.press()
        rows = [{"path": "lib/helper.py", "line": 3, "summary": "review_plan now passes None to parse()"},
                {"path": "lib/helper.py", "line": 4, "summary": "the new call in `Supervisor.plan` breaks this"},
                {"path": "lib/helper.py", "line": 5, "summary": "buildPlan drops the default"}]
        _, kept = self.deferred(rows, {"src/caller.py": {12}}, {"review_plan", "plan", "buildPlan", "parse"})
        self.assertEqual(kept["blockers"], rows, "a blocker naming a changed caller by function was deferred")
        far = {"path": "lib/helper.py", "line": 3, "summary": "an old defect in old_reader()"}
        _, kept = self.deferred([far], {"src/caller.py": {12}}, {"review_plan"})
        self.assertEqual(kept["blockers"], [], "a blocker naming no changed function was held")
        _, kept = self.deferred([far], {"src/caller.py": {12}}, None)
        self.assertEqual(kept["blockers"], [far], "a function context git could not read deferred a blocker")

    def test_a_blocker_in_an_unchanged_callee_of_a_changed_caller_still_blocks(self):
        """Lead ruling on sd:9769 (15:20 MST): a changed caller can break an unchanged callee, as a crash on
        an empty value; a finding cited inside a function the changed code calls is in scope, even when its
        words name only the failing input, so its path does not defer it."""
        self.press()
        source = ("import os\n\n\ndef parse_row(value):\n    return value[0]\n\n\n"
                  "def old_reader():\n    return 1\n")

        def show(argv, **_kwargs):
            if "show" in argv and argv[-1] == f"{'a' * 40}:lib/parse.py":
                return source
            if "show" in argv and argv[-1] == f"{'a' * 40}:src/caller.py":
                return "def export():\n    return 1\n"
            raise build_run.Unread("no such file")

        rows = [{"path": "lib/parse.py", "line": 5, "summary": "an empty value crashes here"},
                {"path": "lib/other.py", "line": 1, "summary": "an empty value crashes here",
                 "instances": ["lib/parse.py:5"]},
                {"path": "lib/parse.py", "line": None, "summary": "an empty value crashes this module"}]
        far = {"path": "lib/parse.py", "line": 9, "summary": "an old defect"}
        with mock.patch.object(build_run, "_read", side_effect=show), \
                mock.patch.object(build_run, "_grep", return_value=""):
            _, kept = self.deferred([*rows, far], {"src/caller.py": {12}}, {"export", "parse_row", "row"},
                                    {"export"}, callees=True)
        self.assertEqual(kept["blockers"], rows, "a blocker in a callee of the changed caller was deferred")

        def broken(argv, **_kwargs):
            raise build_run.Unread("git gave no answer")

        with mock.patch.object(build_run, "_read", side_effect=broken):
            _, kept = self.deferred([far], {"src/caller.py": {12}}, {"parse_row"}, {"export"}, callees=True)
        self.assertEqual(kept["blockers"], [far], "a callee git could not read deferred its blocker")

    def test_a_blocker_in_a_transitive_callee_of_a_changed_caller_still_blocks(self):
        """Review of sd:9769: a changed caller passes an empty value to an unchanged wrapper, which forwards
        it to a parser that crashes; a finding naming only the parser is in scope through the wrapper."""
        self.press()
        head = "a" * 40
        files = {"lib/parse.py": "def parse_row(value):\n    return value[0]\n\n\ndef old_reader():\n    return 1\n",
                 "lib/wrap.py": "from lib.parse import parse_row\n\n\ndef wrap(value):\n    return parse_row(value)\n",
                 "src/caller.py": "def export():\n    return 1\n"}

        def show(argv, **_kwargs):
            path = argv[-1].removeprefix(f"{head}:")
            if "show" in argv and path in files:
                return files[path]
            raise build_run.Unread("no such file")

        def grep(argv, **_kwargs):  # `git grep -n -z -w`: `head:path\0line\0text`
            return fake_grep(files, head, argv)

        deep = {"path": "lib/parse.py", "line": 2, "summary": "an empty value crashes the parser"}
        far = {"path": "lib/parse.py", "line": 6, "summary": "an old defect"}
        with mock.patch.object(build_run, "_read", side_effect=show), \
                mock.patch.object(build_run, "_grep", side_effect=grep):
            _, kept = self.deferred([deep, far], {"src/caller.py": {12}}, {"export", "wrap", "value"},
                                    {"export"}, callees=True)
        self.assertEqual(kept["blockers"], [deep], "a blocker in a transitive callee of the changed caller was deferred")

        def lost(argv, **_kwargs):
            raise build_run.Unread("git grep gave no answer")

        with mock.patch.object(build_run, "_read", side_effect=show), \
                mock.patch.object(build_run, "_grep", side_effect=lost):
            _, kept = self.deferred([far], {"src/caller.py": {12}}, {"wrap"}, {"export"}, callees=True)
        self.assertEqual(kept["blockers"], [far], "a caller search git could not answer deferred its blocker")

    def test_a_blocker_in_an_unchanged_consumer_of_a_changed_function_still_blocks(self):
        """Review of sd:9769: a fix makes `lookup()` return None; the unchanged consumer at `lookup().name`
        crashes, directly or through an unchanged helper, and a finding naming only the failing input is in
        scope through what the consumer calls."""
        self.press()
        head = "a" * 40
        files = {"lib/consume.py": "def show_name(key):\n    return lookup(key).name\n\n\n"
                                   "def label(key):\n    return middle(key).name\n\n\ndef plain():\n    return 1\n",
                 "lib/mid.py": "def middle(key):\n    return lookup(key)\n",
                 "lib/store.py": "def lookup(key):\n    return None\n"}

        def show(argv, **_kwargs):
            path = argv[-1].removeprefix(f"{head}:")
            if "show" in argv and path in files:
                return files[path]
            raise build_run.Unread("no such file")

        def grep(argv, **_kwargs):
            return fake_grep(files, head, argv)

        rows = [{"path": "lib/consume.py", "line": 2, "summary": "a missing key crashes here"},
                {"path": "lib/consume.py", "line": 6, "summary": "a missing key crashes here"}]
        far = {"path": "lib/consume.py", "line": 10, "summary": "an old defect"}
        with mock.patch.object(build_run, "_read", side_effect=show), \
                mock.patch.object(build_run, "_grep", side_effect=grep):
            _, kept = self.deferred([*rows, far], {"lib/store.py": {3}}, {"lookup", "key", "None"},
                                    {"lookup", "None"}, callees=True)
        self.assertEqual(kept["blockers"], rows, "a blocker in a consumer of the changed function was deferred")

    def test_a_module_level_line_below_a_function_is_not_inside_it(self):
        """Review of sd:9769: a changed API starts returning None; an unchanged module-level
        `result = changed_api().value` below an unrelated function is not that function's line, so the
        finding there holds through what the line calls."""
        self.press()
        head = "a" * 40
        body = "def unrelated():\n    return 1\n\n\nresult = changed_api().value\n"

        def show(argv, **_kwargs):
            if "show" in argv and argv[-1] == f"{head}:lib/mod.py":
                return body
            raise build_run.Unread("no such file")

        supervisor = self.supervisor()
        with mock.patch.object(build_run, "_read", side_effect=show):
            self.assertEqual(supervisor.scope("lib/mod.py", 5, head, {}), (set(), ["result = changed_api().value"]),
                             "a module-level line read as part of the function above it")
            self.assertEqual(supervisor.scope("lib/mod.py", 2, head, {})[0], {"unrelated"})
        row = {"path": "lib/mod.py", "line": 5, "summary": "a None value crashes here"}
        with mock.patch.object(build_run, "_read", side_effect=show), \
                mock.patch.object(build_run, "_grep", return_value=""):
            _, kept = self.deferred([row], {"lib/api.py": {3}}, {"changed_api", "value"}, {"changed_api"},
                                    callees=True)
        self.assertEqual(kept["blockers"], [row], "a module-level consumer of the changed API was deferred")

    def test_an_import_alias_links_a_changed_function_both_ways(self):
        """Review of sd:9769: `from parser import parse as decode` hides the link between `decode()` and the
        changed `parse()`; the walk down from a consumer and the walk up from a callee both follow it."""
        self.press()
        head = "a" * 40
        files = {"lib/consume.py": "from parser import parse as decode\n\n\ndef show(text):\n    return decode(text).name\n",
                 "lib/rebind.py": "def show(text):\n    return load(text).name\n\n\nload = parse\n",
                 "lib/parser.py": "def parse(text):\n    return text[0]\n"}

        def show(argv, **_kwargs):
            path = argv[-1].removeprefix(f"{head}:")
            if "show" in argv and path in files:
                return files[path]
            raise build_run.Unread("no such file")

        def grep(argv, **_kwargs):
            return fake_grep(files, head, argv)

        consumers = [{"path": "lib/consume.py", "line": 5, "summary": "a None result crashes here"},
                     {"path": "lib/rebind.py", "line": 2, "summary": "a None result crashes here"}]
        with mock.patch.object(build_run, "_read", side_effect=show), \
                mock.patch.object(build_run, "_grep", side_effect=grep):
            _, kept = self.deferred(consumers, {"lib/store.py": {3}}, {"parse", "text"}, {"parse"}, callees=True)
        self.assertEqual(kept["blockers"], consumers, "a consumer calling the changed function by alias was deferred")
        callee = {"path": "lib/parser.py", "line": 2, "summary": "an empty value crashes here"}
        with mock.patch.object(build_run, "_read", side_effect=show), \
                mock.patch.object(build_run, "_grep", side_effect=grep):
            _, kept = self.deferred([callee], {"src/caller.py": {12}}, {"decode", "value"}, {"export"},
                                    callees=True)
        self.assertEqual(kept["blockers"], [callee], "a callee the changed code calls by alias was deferred")

    def test_a_multiline_signature_does_not_end_the_function(self):
        """Review of sd:9769: a Python signature split over lines closes with `):` at the definition's own
        indent; the function runs on past it, so `value = lookup(item)` above `return value.name` counts."""
        self.press()
        head = "a" * 40
        body = "def show(\n    item,\n):\n    value = lookup(item)\n    return value.name\n"

        def show(argv, **_kwargs):
            if "show" in argv and argv[-1] == f"{head}:lib/consume.py":
                return body
            raise build_run.Unread("no such file")

        supervisor = self.supervisor()
        with mock.patch.object(build_run, "_read", side_effect=show):
            names, lines = supervisor.scope("lib/consume.py", 5, head, {})
        self.assertEqual((names, lines[-1]), ({"show"}, "    return value.name"),
                         "a multiline signature ended the function at its closing line")
        row = {"path": "lib/consume.py", "line": 5, "summary": "a missing item crashes here"}
        with mock.patch.object(build_run, "_read", side_effect=show), \
                mock.patch.object(build_run, "_grep", return_value=""):
            _, kept = self.deferred([row], {"lib/store.py": {3}}, {"lookup", "key"}, {"lookup"}, callees=True)
        self.assertEqual(kept["blockers"], [row], "a consumer with a multiline signature was deferred")

    def edges(self, files, rows, changed, words, defined):
        """`deferred` over `rows` with `git show` and `git grep` answered from `files` at one head."""
        head = "a" * 40

        def show(argv, **_kwargs):
            path = argv[-1].removeprefix(f"{head}:")
            if "show" in argv and path in files:
                return files[path]
            raise build_run.Unread("no such file")

        with mock.patch.object(build_run, "_read", side_effect=show), \
                mock.patch.object(build_run, "_grep", side_effect=lambda argv, **_: fake_grep(files, head, argv)):
            return self.deferred(rows, changed, words, defined, callees=True)[1]["blockers"]

    def test_a_changed_function_passed_as_a_value_is_an_edge(self):
        """Class pass, review of sd:9769 (a missed edge read as independence): a consumer that passes the
        changed handler as a value (`apply(rows, handler_a)`, `getattr(mod, "handler_a")`) depends on it."""
        self.press()
        files = {"lib/run.py": "def run(rows):\n    return apply(rows, handler_a).name\n\n\n"
                               "def named(mod):\n    return getattr(mod, \"handler_a\")().name\n"}
        rows = [{"path": "lib/run.py", "line": 2, "summary": "a missing result crashes here"},
                {"path": "lib/run.py", "line": 6, "summary": "a missing result crashes here"}]
        self.assertEqual(self.edges(files, rows, {"lib/handlers.py": {3}}, {"handler_a"}, {"handler_a"}), rows,
                         "a consumer referencing the changed function as a value was deferred")

    def test_a_table_bound_by_assignment_is_an_edge(self):
        """Class pass, review of sd:9769: `HANDLERS[key]()` reaches the changed handler through a table an
        assignment binds; the walk follows the assignment as it follows a definition."""
        self.press()
        files = {"lib/table.py": "HANDLERS = {\"a\": handler_a}\n",
                 "lib/dispatch.py": "def dispatch(key):\n    return HANDLERS[key]().name\n"}
        rows = [{"path": "lib/dispatch.py", "line": 2, "summary": "a missing result crashes here"}]
        self.assertEqual(self.edges(files, rows, {"lib/handlers.py": {3}}, {"handler_a"}, {"handler_a"}), rows,
                         "a dispatcher reaching the changed handler through a table was deferred")

    def test_a_statement_outside_any_function_spans_its_open_brackets(self):
        """Class pass, review of sd:9769: a table written over several lines is one statement; the scope of
        any of its lines is the whole statement, so the entry naming the changed handler is read."""
        self.press()
        files = {"lib/dispatch.py": "HANDLERS = {\n    \"a\": handler_a,\n    \"b\": handler_b,\n}\n\n\n"
                                    "def dispatch(key):\n    return HANDLERS[key]().name\n"}
        supervisor = self.supervisor()
        head = "a" * 40
        with mock.patch.object(build_run, "_read", return_value=files["lib/dispatch.py"]):
            self.assertEqual(supervisor.scope("lib/dispatch.py", 1, head, {}),
                             (set(), files["lib/dispatch.py"].splitlines()[:4]),
                             "a statement over several lines read as its first line")
            self.assertEqual(supervisor.scope("lib/dispatch.py", 3, head, {})[1][0], "HANDLERS = {",
                             "a line inside a table read without its statement")
        rows = [{"path": "lib/dispatch.py", "line": 8, "summary": "a missing result crashes here"}]
        self.assertEqual(self.edges(files, rows, {"lib/handlers.py": {3}}, {"handler_a"}, {"handler_a"}), rows,
                         "a dispatcher reaching the changed handler through a multiline table was deferred")

    def test_a_reference_outside_any_function_passes_on_what_it_binds(self):
        """Class pass, review of sd:9769: walking up from an unchanged handler, a table entry outside any
        function names no caller; the name the table binds does, so the walk reaches the changed dispatcher."""
        self.press()
        files = {"lib/handlers.py": "def handler_a():\n    return lookup()\n",
                 "lib/table.py": "HANDLERS = {\n    \"a\": handler_a,\n}\n",
                 "lib/dispatch.py": "def dispatch(key):\n    return HANDLERS[key]().name\n"}
        rows = [{"path": "lib/handlers.py", "line": 2, "summary": "an empty value crashes here"}]
        self.assertEqual(self.edges(files, rows, {"lib/dispatch.py": {2}}, {"dispatch", "key"}, {"export"}), rows,
                         "a handler the changed dispatcher reaches through a table was deferred")

    def test_a_table_filled_by_mutation_is_an_edge(self):
        """Class pass, review of sd:9769: `HANDLERS["a"] = handler_a` or `HANDLERS.update(...)` fills the table
        `HANDLERS[key]()` reads; both walks follow the statement that mutates it, as one that binds it."""
        self.press()
        files = {"lib/table.py": "HANDLERS = {}\nHANDLERS[\"a\"] = handler_a\nHANDLERS.update({\"b\": handler_b})\n",
                 "lib/handlers.py": "def handler_b():\n    return lookup()\n",
                 "lib/dispatch.py": "def dispatch(key):\n    return HANDLERS[key]().name\n"}
        down = [{"path": "lib/dispatch.py", "line": 2, "summary": "a missing result crashes here"}]
        self.assertEqual(self.edges(files, down, {"lib/handlers.py": {3}}, {"handler_a"}, {"handler_a"}), down,
                         "a dispatcher reaching the changed handler through a mutated table was deferred")
        up = [{"path": "lib/handlers.py", "line": 2, "summary": "an empty value crashes here"}]
        self.assertEqual(self.edges(files, up, {"lib/dispatch.py": {2}}, {"dispatch", "key"}, {"export"}), up,
                         "a handler the changed dispatcher reaches through a mutated table was deferred")

    REGISTRY = {"lib/registry.py": "REGISTRY = {}\n\n\ndef register(name):\n    def wrap(fn):\n"
                                   "        REGISTRY[name] = fn\n        return fn\n    return wrap\n",
                "lib/handlers.py": "import functools\n\n\n@register(\n    \"a\",\n)\ndef handler_a():\n"
                                   "    return lookup()\n\n\n@functools.cache\ndef cached():\n    return lookup()\n",
                "lib/dispatch.py": "def dispatch(key):\n    return REGISTRY[key]().name\n"}

    def test_a_decorator_registry_is_an_edge(self):
        """sd:10027: `@register("a")` files `handler_a` in a table `dispatch` reads, an edge no name walk
        sees; both directions hold. A pure wrapper (`@functools.cache`) adds no edge."""
        self.press()
        up = [{"path": "lib/handlers.py", "line": 8, "summary": "an empty value crashes here"}]
        self.assertEqual(self.edges(self.REGISTRY, up, {"lib/dispatch.py": {2}}, {"dispatch", "key"}, {"dispatch"}),
                         up, "a handler the changed dispatcher reaches through a decorator registry was deferred")
        down = [{"path": "lib/dispatch.py", "line": 2, "summary": "a missing result crashes here"}]
        self.assertEqual(self.edges(self.REGISTRY, down, {"lib/handlers.py": {8}}, {"handler_a", "lookup"},
                                    {"handler_a"}),
                         down, "a dispatcher reaching a changed registered handler was deferred")
        wrapped = [{"path": "lib/handlers.py", "line": 13, "summary": "an empty value crashes here"}]
        self.assertEqual(self.edges(self.REGISTRY, wrapped, {"lib/dispatch.py": {2}}, {"dispatch", "key"},
                                    {"dispatch"}),
                         [], "a function behind a pure wrapper the change never reaches still held")

    def test_an_implicit_call_is_an_edge(self):
        """Lead scope on sd:10027: `table[key]` or `a == b` calls `__getitem__` or `__eq__` by no name; a
        finding in an unchanged one, or past a changed one, holds. A changed `__init__` is reached by name."""
        self.press()
        files = {"lib/table.py": "class Table:\n    def __getitem__(self, key):\n        return self.rows[key]\n\n"
                                 "    def __init__(self):\n        self.rows = {}\n\n    def size(self):\n"
                                 "        return len(self.rows)\n",
                 "lib/use.py": "def same(a, b):\n    return a == b\n"}
        up = [{"path": "lib/table.py", "line": 3, "summary": "a missing key crashes here"}]
        self.assertEqual(self.edges(files, up, {"lib/load.py": {4}}, {"load", "table"}, {"load"}), up,
                         "a finding in an unchanged __getitem__ was deferred")
        down = [{"path": "lib/use.py", "line": 2, "summary": "a None row now compares equal"}]
        self.assertEqual(self.edges(files, down, {"lib/table.py": {3}}, {"__getitem__", "rows"}, {"__getitem__"}),
                         down, "a finding past a changed __getitem__ was deferred")
        plain = [{"path": "lib/use.py", "line": 2, "summary": "a None row now compares equal"}]
        self.assertEqual(self.edges(files, plain, {"lib/table.py": {6}}, {"__init__", "rows"}, {"__init__"}), [],
                         "a changed __init__ held a finding that never names its class")

    def test_a_shared_artifact_key_is_an_edge(self):
        """Lead scope on sd:10027: an exporter that writes `"covers_from"` and an unchanged importer that reads
        it share a file format, not a name; the quoted key is the edge. Another key is none."""
        self.press()
        files = {"lib/importer.py": "def load(prior):\n    return prior.get(\"covers_from\").strip()\n",
                 "lib/other.py": "def load(prior):\n    return prior.get(\"unrelated\").strip()\n",
                 "lib/exporter.py": "def export(head):\n    row = {}\n    row[\"covers_from\"] = head\n"
                                    "    return row\n"}
        rows = [{"path": "lib/importer.py", "line": 2, "summary": "a missing key crashes here"}]
        self.assertEqual(self.edges(files, rows, {"lib/exporter.py": {4}}, {"export", "covers_from", '"covers_from"'},
                                    {"export"}), rows, "an importer of the key the changed exporter writes was deferred")
        other = [{"path": "lib/other.py", "line": 2, "summary": "a missing key crashes here"}]
        self.assertEqual(self.edges(files, other, {"lib/exporter.py": {4}}, {"export", "covers_from", '"covers_from"'},
                                    {"export"}), [], "an importer of another key held")

    def test_function_words_mark_each_quoted_literal(self):
        """sd:10027: the changed functions' quoted literals read as `"text"` among the words, so an unchanged
        reader of the same key is tied to the branch; an identifier never carries quotes."""
        repo = Path(tempfile.mkdtemp(dir=self.root))

        def run(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator",
                                   "-c", "user.email=operator@example.test", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()

        run("init", "-q")
        body = "def export(head):\n    return {'covers_from': head, \"x\": 1}\n"
        (repo / "exporter.py").write_text(body)
        run("add", "-A")
        run("commit", "-q", "-m", "base")
        base = run("rev-parse", "HEAD")
        (repo / "exporter.py").write_text(body.replace("1}", "2}"))
        run("add", "-A")
        run("commit", "-q", "-m", "change")
        read = build_run.function_words(["git"], str(repo), base, run("rev-parse", "HEAD"))
        assert read is not None, "git's function context did not read"
        self.assertIn('"covers_from"', read[0], read[0])

    def test_a_short_function_name_still_names_the_changed_caller(self):
        """Review of sd:9769: `go()` names a changed function; no length filter drops it."""
        self.press()
        rows = [{"path": "lib/helper.py", "line": 3, "summary": "go() now passes None here"},
                {"path": "lib/helper.py", "line": 4, "summary": "the new call in `go` crashes this"}]
        _, kept = self.deferred(rows, {"src/caller.py": {12}}, {"go"})
        self.assertEqual(kept["blockers"], rows, "a blocker naming a short changed function was deferred")

    def test_a_rebuttal_records_every_instance_of_the_finding(self):
        """Review of sd:9769: the lane holds a rebuttal back once any file the finding names changes, so the
        rebuttal carries each instance, not the primary path alone."""
        self.press()
        supervisor = self.supervisor()
        self.db.execute("UPDATE repo SET remote = 'https://github.com/example/repo.git'")
        row = {"path": "far.py", "line": 2, "summary": "a", "instances": ["far.py:2", "other.py:7"]}
        supervisor.file_follow_up(row, "a" * 40)
        body = self.db.execute("SELECT body FROM state WHERE key LIKE 'ship-adjudication:build-run:%'").fetchone()
        self.assertIsNotNone(body, "no rebuttal was recorded")
        raw = json.loads(body[0])["proposal"]["findings"][0]["raw_finding"]
        self.assertEqual(raw.get("instances"), ["far.py:2", "other.py:7"], "the rebuttal dropped the instances")

    def test_a_deferred_rebuttal_names_every_file_the_branch_changes(self):
        """Review of sd:9769: a managed build files a rebuttal before the lane gate runs; a gate fix that
        changes only the branch's own caller must lapse it, so the rebuttal names each file it was judged on."""
        self.press()
        self.db.execute("UPDATE repo SET remote = 'https://github.com/example/repo.git'")
        _, kept = self.deferred([{"path": "far.py", "line": 2, "summary": "a"}], {"src/caller.py": {12}})
        self.assertEqual(kept["blockers"], [])
        body = self.db.execute("SELECT body FROM state WHERE key LIKE 'ship-adjudication:build-run:%'").fetchone()
        self.assertIsNotNone(body, "no rebuttal was recorded")
        raw = json.loads(body[0])["proposal"]["findings"][0]["raw_finding"]
        self.assertIn("src/caller.py", raw.get("instances"), "a fix to the branch's caller keeps the rebuttal")

    def test_a_changed_function_named_in_plain_words_holds(self):
        """Review of sd:9769: a reviewer can name the changed caller as a plain word (`export`); a plain word
        that is the name of a function the branch changes ties the blocker to the branch."""
        self.press()
        rows = [{"path": "lib/parse.py", "line": 3, "summary": "export now passes an empty value; the parser crashes"}]
        _, kept = self.deferred(rows, {"src/caller.py": {12}}, {"export", "value"}, {"export"})
        self.assertEqual(kept["blockers"], rows, "a blocker naming a changed function in plain words was deferred")
        far = {"path": "lib/parse.py", "line": 3, "summary": "the parser crashes on an empty value"}
        _, kept = self.deferred([far], {"src/caller.py": {12}}, {"the", "parser", "value"}, {"export"})
        self.assertEqual(kept["blockers"], [], "a plain word that names no changed function held a blocker")

    def test_every_definition_form_and_changed_line_name_reads_as_defined(self):
        """Class pass, review of sd:9769: a caller defined by assignment (`const serialize = (row) => ...`),
        by any language's keyword, or named on a changed line reads as a name the branch defines."""
        repo = Path(tempfile.mkdtemp(dir=self.root))

        def run(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator",
                                   "-c", "user.email=operator@example.test", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()

        run("init", "-q")
        body = ("const serialize = (row) => sink(row)\nlet render = function (x) { return x }\n"
                "pick := func() {}\nloader = lambda path: open(path)\n")
        (repo / "caller.js").write_text(body)
        run("add", "-A")
        run("commit", "-q", "-m", "base")
        base = run("rev-parse", "HEAD")
        (repo / "caller.js").write_text(body.replace("sink(row)", "sink(row, flush)"))
        run("add", "-A")
        run("commit", "-q", "-m", "change")
        read = build_run.function_words(["git"], str(repo), base, run("rev-parse", "HEAD"))
        assert read is not None, "git's function context did not read"
        self.assertTrue({"serialize", "sink", "flush"} <= read[1], read[1])
        for line in ("let render = function (x) {", "pick := func() {}", "loader = lambda path: open(path)",
                     "export const build = async (a) => a", "fn emit(x: u8) {", "sub report {"):
            with self.subTest(line=line):
                self.assertTrue(build_run.defined_names(line), f"no name defined by {line!r}")

    def test_branch_words_hold_the_changed_function_and_what_it_calls(self):
        """Lead ruling on sd:9769: the branch's changed functions, read whole, name the caller and its callee."""
        repo = Path(tempfile.mkdtemp(dir=self.root))

        def run(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator",
                                   "-c", "user.email=operator@example.test", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()

        run("init", "-q")
        body = "class Plan:\n    def review_plan(self):\n        x = 1\n        return parse_row(x)\n\n\n" \
               "def untouched():\n    return 2\n"
        (repo / "caller.py").write_text(body)
        run("add", "-A")
        run("commit", "-q", "-m", "base")
        base = run("rev-parse", "HEAD")
        (repo / "caller.py").write_text(body.replace("x = 1", "x = None"))
        run("add", "-A")
        run("commit", "-q", "-m", "change")
        read = build_run.function_words(["git"], str(repo), base, run("rev-parse", "HEAD"))
        assert read is not None, "git's function context did not read"
        words, defined = read
        self.assertIn("review_plan", defined, "the changed function's own name is not a defined name")
        self.assertNotIn("parse_row", defined, "a called name read as a defined one")
        self.assertTrue({"review_plan", "parse_row"} <= words, words)
        self.assertNotIn("untouched", words)

    def test_a_change_outside_any_function_reads_as_no_function_words(self):
        """Class pass on sd:9769: a changed config value, module constant, binary or mode leaves a consumer's
        finding nothing to name, so the branch's words do not read and every blocker holds."""
        body = "def f():\n    return LIMIT\n"
        for name, before, after in (("conf.yaml", "items:\n  - a\nother: 1\n", "items: []\nother: 1\n"),
                                    ("mod.py", f"import os\nLIMIT = 3\n\n\n{body}",
                                     f"import os\nLIMIT = 0\n\n\n{body}"),
                                    ("blob.bin", b"\x00\x01", b"\x00\x02"), ("run.sh", "echo\n", None)):
            with self.subTest(name=name):
                repo = Path(tempfile.mkdtemp(dir=self.root))

                def run(*args, repo=repo):
                    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator",
                                           "-c", "user.email=operator@example.test", *args],
                                          check=True, capture_output=True, text=True).stdout.strip()

                run("init", "-q")
                path = repo / name
                path.write_bytes(before) if isinstance(before, bytes) else path.write_text(before)
                run("add", "-A")
                run("commit", "-q", "-m", "base")
                base = run("rev-parse", "HEAD")
                if after is None:
                    path.chmod(0o755)
                else:
                    path.write_bytes(after) if isinstance(after, bytes) else path.write_text(after)
                run("commit", "-q", "-am", "change")
                self.assertIsNone(build_run.function_words(["git"], str(repo), base, run("rev-parse", "HEAD")),
                                  "a change outside any function let a consumer's blocker be filed")

    def test_a_changed_caller_named_by_any_spelling_holds_and_a_lookalike_does_not(self):
        """Review of sd:9769: `./src/caller.py` or an absolute path names the changed caller and holds; a
        `src/caller.py.bak` is another file and does not."""
        self.press()
        work = str(self.supervisor().work)
        rows = [{"path": "lib/helper.py", "line": 3, "summary": "./src/caller.py now passes None to parse()"},
                {"path": "lib/helper.py", "line": 4, "summary": f"{work}/src/caller.py passes None."}]
        _, kept = self.deferred(rows, {"src/caller.py": {12}})
        self.assertEqual(kept["blockers"], rows, "a blocker naming a changed caller by another spelling was deferred")
        spaced = [{"path": "lib/parse.py", "line": 2, "summary": "bin/my tool now passes None to parse()"},
                  {"path": "lib/parse.py", "line": 3, "summary": f"{work}/bin/my tool passes None."}]
        _, kept = self.deferred(spaced, {"bin/my tool": {4}})
        self.assertEqual(kept["blockers"], spaced, "a blocker naming a changed caller with a space was deferred")
        _, kept = self.deferred([{"path": "lib/parse.py", "line": 2, "summary": "bin/my tool.bak is stale"}],
                                {"bin/my tool": {4}})
        self.assertEqual(kept["blockers"], [], "a lookalike of a spaced name held a blocker")
        far = {"path": "lib/helper.py", "line": 5, "summary": "src/caller.py.bak still holds the old parse()"}
        _, kept = self.deferred([far], {"src/caller.py": {12}})
        self.assertEqual(kept["blockers"], [], "a lookalike file name held a blocker")

    def test_a_blocker_outside_the_checkout_still_blocks(self):
        """Class pass, sd:9769: no delta of this checkout can show a path outside it unchanged."""
        self.press()
        rows = [{"path": "/elsewhere/x.py", "line": 1, "summary": "a"}, {"path": "../up.py", "line": 2, "summary": "b"},
                {"path": "src.py", "line": 9, "summary": "c", "instances": ["/elsewhere/y.py:3"]}]
        _, kept = self.deferred(rows, {})
        self.assertEqual(kept["blockers"], rows, "a blocker outside the checkout was deferred")

    def test_a_quoted_path_in_the_delta_reads_as_unknown(self):
        """Review of sd:9769: a non-ASCII path reads as itself; one git still quotes reads as unknown."""
        repo = Path(tempfile.mkdtemp(dir=self.root))

        def run(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator",
                                   "-c", "user.email=operator@example.test", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()

        run("init", "-q")
        run("commit", "-q", "--allow-empty", "-m", "base")
        base = run("rev-parse", "HEAD")
        (repo / "na\u00efve.txt").write_text("x\n")
        run("add", "-A")
        run("commit", "-q", "-m", "plain")
        plain = run("rev-parse", "HEAD")
        self.assertEqual(build_run.changed_lines(["git"], str(repo), base, plain), {"na\u00efve.txt": {1}})
        (repo / 'q"uote.txt').write_text("y\n")
        run("add", "-A")
        run("commit", "-q", "-m", "quoted")
        self.assertIsNone(build_run.changed_lines(["git"], str(repo), base, run("rev-parse", "HEAD")),
                          "a quoted path was read")

    def test_a_deletion_marks_the_surviving_line_and_a_deleted_file(self):
        """Review of sd:9769: git's zero-length range names the line before a deletion; a deleted file's
        new path is /dev/null; a deleted line reading `-- x` is not a header."""
        repo = Path(tempfile.mkdtemp(dir=self.root))

        def run(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator",
                                   "-c", "user.email=operator@example.test", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()

        run("init", "-q")
        (repo / "kept.txt").write_text("one\ntwo\n-- x\nfour\n")
        (repo / "gone.txt").write_text("a\nb\n")
        run("add", "-A")
        run("commit", "-q", "-m", "base")
        base = run("rev-parse", "HEAD")
        (repo / "kept.txt").write_text("one\ntwo\nfour\n")
        (repo / "gone.txt").unlink()
        run("add", "-A")
        run("commit", "-q", "-m", "delete")
        self.assertEqual(build_run.changed_lines(["git"], str(repo), base, run("rev-parse", "HEAD")),
                         {"kept.txt": {2, 3}, "gone.txt": {1, 2}})

    def test_each_follow_up_is_its_own_write(self):
        """Review of sd:9769: a lost answer on the first follow-up must not stand for the second."""
        rows = [{"path": "a.txt", "line": 1, "summary": "first"}, {"path": "b.txt", "line": 2, "summary": "second"}]
        self.press()
        supervisor = self.supervisor()
        real, lost = supervisor.file_follow_up, list[dict]()

        def committed_then_lost(row, head, **options):  # the first follow-up's transaction commits; its answer is lost
            real(row, head, **options)
            if not lost:
                lost.append(row)
                raise remote.UnknownOutcome("r" * 26, "the answer to COMMIT was lost")

        supervisor.db.outcome = mock.Mock(return_value="recorded")
        answer = {"status": "blocking", "blockers": rows}
        with mock.patch.object(supervisor, "branch_delta", return_value={}), \
                mock.patch.object(supervisor, "branch_words", return_value=(set(), set())), \
                mock.patch.object(supervisor, "scope", return_value=(set(), [])), \
                mock.patch.object(supervisor, "file_follow_up", committed_then_lost):
            with self.assertRaises(remote.UnknownOutcome):
                supervisor.file_new_findings(answer, self.PRIOR, "a" * 40)
            kept = supervisor.file_new_findings(answer, self.PRIOR, "a" * 40)
        self.assertEqual(kept["status"], "advisory")
        titles = [row[0] for row in self.db.execute("SELECT title FROM item WHERE title LIKE 'Review follow-up%'")]
        self.assertEqual(len(titles), 2, f"a deferred blocker was never filed: {titles}")

    def test_a_round_two_repeat_of_a_round_one_finding_still_blocks(self):
        self.press()
        self.plan(claude=["pass", "fix1", "fix2"], sd_review=["blocking", "blocking", "clean"])
        self.write_pr()
        self.assertEqual(self.supervisor().run()["step"], "enqueued")
        self.assertEqual(self.latest()["review_round"], 2, "a repeated blocker did not block")

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

    def test_a_rate_limited_review_waits_and_asks_again(self):
        """sd:9882: a rate limit blocked the build and parked its item; it waits the window out instead."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["rate_limited", "rate_limited", "clean"])
        with mock.patch.object(build_run, "RATE_LIMIT_WAIT_SECONDS", 0.3):
            self.assertEqual(self.supervisor().run().get("step"), "enqueued", self.latest()["detail"])
        self.assertEqual(len(self.calls("sd-review")), 3)

    def test_a_review_still_rate_limited_after_every_wait_blocks(self):
        self.press()
        waits = build_run.RATE_LIMIT_WAITS
        self.plan(claude=["pass"], sd_review=["rate_limited"] * (waits + 1))
        with mock.patch.object(build_run, "RATE_LIMIT_WAIT_SECONDS", 0.05):
            self.assertEqual(self.supervisor().run()["outcome"], "blocked")
        self.assertEqual(len(self.calls("sd-review")), waits + 1)
        self.assertIn("the review stayed rate_limited", self.latest()["detail"])

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

    def test_a_member_cancelled_on_its_own_is_neither_briefed_nor_delivered(self):
        """Review round 3 of sd:9222: run 1 commits `Closes:` for both items and ends cancelled; Retry queues both;
        the operator cancels the child, then the lead; Retry of the lead runs again on the same branch. Since
        pack sd:9709 round 21 the child's commits on that branch hold the run before a builder starts, as a
        subset Retry's do, so neither its work nor its trailer ships with the lead; Build takes the lead fresh."""
        self.press(2)
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["cancel"])
        self.assertEqual(self.supervisor().run()["outcome"], "cancelled")
        build.queue_again(self.db, [self.items[0]], who="operator")
        lead, child = (member["id"] for member in build.run(self.db, self.bundle["id"])["members"])
        for assignment in (child, lead):
            state = operations.assignment_state(self.db, assignment)
            operations.cancel_assignment(self.db, assignment, expected_revision=state["revision"], who="operator")
        self.assertEqual([entry["id"] for entry in build.queue_again(self.db, [self.items[0]], who="operator")],
                         [lead])
        again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                            root=self.root / "worktrees")
        assert again is not None
        self.bundle = again
        self.plan(claude=["pass"], sd_review=["clean"])
        self.items = [self.items[0], self.items[0], self.items[1]]  # `assert_split_first` names the third
        self.assert_split_first(self.supervisor())
        self.assertNotIn("sd-ship", [call["argv"][0].rsplit("/", 1)[-1] for call in self.calls("sd-ship")
                                     if "enqueue" in call["argv"]])
        self.release_all()
        fresh = build.queue_again(self.db, [self.items[0]], who="operator")
        self.assertNotEqual([entry["id"] for entry in fresh], [lead], "Build takes the lead fresh")

    def left_out_of_a_retry(self, prepare=None, *, run=True):
        """A three-item bundle ends cancelled, `run` once its builder committed for all three, else before any
        build; `prepare(worktree, commit)` then shapes the branch as a builder might; a Retry names the first two,
        so the third leaves the bundle; the next run is claimed. (The next run's supervisor.)"""
        self.press(3)
        self.write_pr()
        if run:
            self.plan(claude=["pass"], sd_review=["cancel"])
            self.assertEqual(self.supervisor().run()["outcome"], "cancelled")
        else:
            build.end(self.db, self.bundle["id"], "cancelled", "cancelled by operator")
        work = Path(self.bundle["work_path"])
        if prepare:
            prepare(work, support.git(work, "rev-parse", "HEAD").strip())
        self.assertEqual(sorted(entry["item"] for entry in build.queue_again(self.db, self.items[:2], who="operator")),
                         sorted(self.items[:2]))
        again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                            root=self.root / "worktrees")
        assert again is not None
        self.bundle = again
        self.plan(claude=["pass"], sd_review=["clean"])
        return self.supervisor()

    def assert_split_first(self, supervisor):
        """The run blocks at its worktree step, before a builder starts, and names the left item."""
        builders = len(self.calls("claude"))
        ended = supervisor.run()
        self.assertEqual((ended["outcome"], len(self.calls("claude"))), ("blocked", builders), ended)
        detail = build.run(self.db, self.bundle["id"])["detail"]
        self.assertIn(f"sd:{self.items[2]} left the bundle at a Retry", detail)

    def test_a_subset_retry_on_a_branch_with_earlier_work_blocks_before_the_builder(self):
        """Pack sd:9581 class pass, row 1: run 1's commit closes all three items; no commit message can say which
        change is whose, so the run blocks for the operator's split instead of shipping the left item's work."""
        self.assert_split_first(self.left_out_of_a_retry())

    def test_a_reverted_earlier_commit_still_blocks(self):
        """Row 2: a revert message proved nothing about what it removed (a conflict may keep a change)."""
        def revert(work, commit):
            support.git(work, "-c", "commit.gpgsign=false", "revert", "--no-edit", commit)
        self.assert_split_first(self.left_out_of_a_retry(revert))

    def test_an_earlier_commit_naming_a_kept_item_in_its_body_still_blocks(self):
        """Row 3: a body `Refs:` paragraph is no trailer, and no trailer binds the content anyway."""
        def refs(work, commit):
            support.git(work, "-c", "commit.gpgsign=false", "revert", "--no-edit", commit)
            (work / "dropped.txt").write_text("the left item's work\n")
            support.git(work, "add", "dropped.txt")
            support.git(work, "-c", "commit.gpgsign=false", "commit", "-qm",
                        f"fix: tidy\n\nRefs: sd:{self.items[0]}\n\nThen the left item's file.")
        self.assert_split_first(self.left_out_of_a_retry(refs))

    def test_an_earlier_merge_resolution_still_blocks(self):
        """Row 4: a merge commit's own resolution carried work that a `--no-merges` read never saw."""
        def merge(work, commit):
            support.git(work, "-c", "commit.gpgsign=false", "revert", "--no-edit", commit)
            support.git(work, "checkout", "-q", "-b", "side", "origin/main")
            (work / "side.txt").write_text("kept\n")
            support.git(work, "add", "side.txt")
            support.git(work, "-c", "commit.gpgsign=false", "commit", "-qm", f"fix: kept\n\nCloses: sd:{self.items[0]}")
            support.git(work, "checkout", "-q", "-")
            support.git(work, "-c", "commit.gpgsign=false", "merge", "-q", "--no-ff", "--no-commit", "side")
            (work / "dropped.txt").write_text("the left item's work\n")
            support.git(work, "add", "dropped.txt")
            support.git(work, "-c", "commit.gpgsign=false", "commit", "-qm", "chore: merge side")
            support.git(work, "branch", "-q", "-D", "side")
        self.assert_split_first(self.left_out_of_a_retry(merge))

    def release_all(self):
        self.db.execute("UPDATE runner_run SET released_at = COALESCE(released_at, ?)", ("2026-01-01T00:00:00Z",))
        self.db.execute("UPDATE runner_lease SET released_at = COALESCE(released_at, ?)", ("2026-01-01T00:00:00Z",))

    def test_a_left_item_done_elsewhere_still_splits(self):
        """Row 20: the left item landed through another branch before the Retry; the branch still holds its
        earlier work, so the split is read against the bundle as it ended, done members included."""
        def landed(_work, _commit):
            self.db.execute("UPDATE item SET status = 'done' WHERE id = ?", (self.items[2],))
        self.assert_split_first(self.left_out_of_a_retry(landed))

    def moved_then_retried(self, target, *, cancelled=False, retried=2):
        """Round 11 class pass: who leaves the bundle is read from a filtered view of it, not from the members the
        retried run built. A three-item bundle commits for all three and ends blocked (or `cancelled`); the
        operator moves the third item to `target` through `transition`, which cancels a blocked member's
        assignment (`UNBUILT`); a Retry names the first `retried` items (one: the whole bundle)."""
        self.press(3)
        self.write_pr()
        if cancelled:
            self.plan(claude=["pass"], sd_review=["cancel"])
        else:
            self.plan(claude=["pass"] * 9, sd_review=["blocking"] * 9)
        self.assertEqual(self.supervisor().run()["outcome"], "cancelled" if cancelled else "blocked")
        self.release_all()
        writes.transition(self.db, self.items[2], target, who="operator")
        if not cancelled:
            self.assertEqual(self.db.execute("SELECT status FROM assignment WHERE item = ?",
                                             (self.items[2],)).fetchone()[0], "cancelled")
        build.queue_again(self.db, self.items[:retried], who="operator")
        again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                            root=self.root / "worktrees")
        assert again is not None
        self.bundle = again
        self.plan(claude=["pass"], sd_review=["clean"])
        return self.supervisor()

    def test_a_subset_retry_after_a_move_to_planning_splits(self):
        """Row 21: the move cancelled the third member's assignment, so the blocked bundle's view lacks it."""
        self.assert_split_first(self.moved_then_retried("planning"))

    def test_a_subset_retry_after_a_move_to_done_splits(self):
        """Row 22: the same through done, the reviewer's case."""
        self.assert_split_first(self.moved_then_retried("done"))

    def test_a_whole_retry_after_a_move_to_planning_splits(self):
        """Row 23: Retry of the whole bundle drops the cancelled member the same way, with no `only` at all."""
        self.assert_split_first(self.moved_then_retried("planning", retried=1))

    def test_a_whole_retry_after_a_move_to_done_splits(self):
        """Row 24: a done member is dropped from a whole Retry too (sd:9015); its work is still on the branch."""
        self.assert_split_first(self.moved_then_retried("done", retried=1))

    def test_a_whole_retry_of_a_cancelled_bundle_with_a_done_member_splits(self):
        """Row 25: a cancelled bundle keeps the member's assignment, but the done filter drops it."""
        self.assert_split_first(self.moved_then_retried("done", cancelled=True, retried=1))

    def test_a_rebased_branch_after_a_split_cannot_be_retried(self):
        """Round 6, row 10: a rebase onto newer main gives the earlier commits new ids and keeps their changes, so
        no git state proves a split; once the guard found earlier work, Retry of that bundle refuses."""
        self.assert_split_first(self.left_out_of_a_retry())
        work = Path(self.bundle["work_path"])
        old = support.git(work, "rev-parse", "HEAD")
        support.git(work, "reset", "-q", "--hard", "origin/main")
        support.git(work, "-c", "commit.gpgsign=false", "cherry-pick", "--allow-empty", f"origin/main..{old}")
        self.release_all()
        with self.assertRaisesRegex(build.BuildRefused, "fresh"):
            build.retry(self.db, self.bundle["assignment"], who="operator")

    def test_a_retry_after_a_split_builds_the_kept_items_fresh(self):
        """Row 11: the kept items build on a new branch from main, never the held branch, whose tip may also be
        published (an unmanaged bundle parked after its push), so a rebuilt head would not push."""
        self.assert_split_first(self.left_out_of_a_retry())
        held = (self.bundle["branch"], self.bundle["work_path"])
        self.release_all()
        queued = build.queue_again(self.db, self.items[:2], who="operator")
        self.assertEqual(sorted(entry["item"] for entry in queued), sorted(self.items[:2]))
        again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                            root=self.root / "worktrees")
        assert again is not None
        self.assertNotEqual((again["branch"], again["work_path"]), held)
        self.bundle = again
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor().run()
        self.assertEqual(ended.get("step"), "enqueued", ended)
        prompt = self.calls("claude")[-1]["argv"]
        self.assertNotIn(f"- sd:{self.items[2]}", prompt[prompt.index("-p") + 1])

    def test_the_build_press_takes_a_held_bundles_items_fresh(self):
        """Row 12: the held bundle's refusal says Build; the dashboard's Build dialog checks each item (`_item`) and
        its press calls `queue`, not `queue_again`, so both take the blocked items and build them on a new branch."""
        self.assert_split_first(self.left_out_of_a_retry())
        held = self.bundle["branch"]
        self.release_all()
        for item in self.items[:2]:
            build._item(self.db, item)
        queued = build.queue(self.db, self.items[:2], who="dashboard")
        self.assertEqual(sorted(row["item"] for row in queued), sorted(self.items[:2]))
        again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                            root=self.root / "worktrees")
        assert again is not None
        self.assertNotEqual(again["branch"], held)

    def blocked_elsewhere(self, index):
        """A bundle of three ends cancelled; its item `index` is then built on its own, and that newer build blocks."""
        self.press(3)
        item = self.items[index]
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["cancel"])
        self.assertEqual(self.supervisor().run()["outcome"], "cancelled")
        self.release_all()
        self.db.execute("UPDATE item SET status = 'ready' WHERE id = ?", (item,))
        newer = build.queue(self.db, [item], who="operator")[0]["id"]
        self.db.execute("UPDATE assignment SET status = 'blocked', run_count = 1 WHERE id = ?", (newer,))
        self.db.execute("UPDATE item SET status = 'blocked' WHERE id = ?", (item,))
        return newer

    def test_a_subset_retry_leaves_a_left_items_newer_block_alone(self):
        """Round 8 class pass, row 13: a left-out member whose item a newer build owns keeps that build's state; the
        Retry ends the old membership only, never the newer block, and parks nothing."""
        self.blocked_elsewhere(2)
        build.retry(self.db, self.bundle["assignment"], who="operator", only=self.items[:2])
        row = self.db.execute("SELECT status, parked_at FROM item WHERE id = ?", (self.items[2],)).fetchone()
        self.assertEqual((row["status"], row["parked_at"]), ("blocked", None))

    def test_a_subset_retry_refuses_a_kept_item_a_newer_build_owns(self):
        """Row 14: a kept member whose item a newer build owns would be queued twice; the Retry refuses."""
        newer = self.blocked_elsewhere(1)
        with self.assertRaisesRegex(build.BuildRefused, f"assignment {newer}"):
            build.retry(self.db, self.bundle["assignment"], who="operator", only=self.items[:2])
        self.assertEqual(self.db.execute("SELECT status FROM item WHERE id = ?", (self.items[1],)).fetchone()[0],
                         "blocked")

    def leftover(self, kind):
        """`prepare` for a bundle cancelled before any commit that left `kind` of uncommitted state behind."""
        def shape(work, _commit):
            support.git(work, "reset", "-q", "--hard", "origin/main")
            if kind == "ignored":
                exclude(work, "local.conf\n")
                (work / "local.conf").write_text("the left item's setting\n")
            elif kind == "cache":
                exclude(work, "__pycache__/\n")
                (work / "__pycache__").mkdir()
                (work / "__pycache__" / "x.pyc").write_bytes(b"\0")
            elif kind == "unstaged":
                tracked = work / support.git(work, "ls-files", "--deduplicate").split()[0]
                tracked.write_text(tracked.read_text() + "the left item's work\n")
            else:
                (work / "dropped.txt").write_text("the left item's work\n")
                if kind == "staged":
                    support.git(work, "add", "dropped.txt")
        return shape

    def test_a_split_guard_holds_a_staged_leftover(self):
        """Round 9 class pass, the guard reads less than the worktree holds; row 15: a staged change."""
        self.assert_split_first(self.left_out_of_a_retry(self.leftover("staged")))

    def test_a_split_guard_holds_an_unstaged_leftover(self):
        """Row 16: a tracked file changed but not staged."""
        self.assert_split_first(self.left_out_of_a_retry(self.leftover("unstaged")))

    def test_a_split_guard_holds_an_untracked_leftover(self):
        """Row 17: a new file never added."""
        self.assert_split_first(self.left_out_of_a_retry(self.leftover("untracked")))

    def test_a_split_guard_holds_an_ignored_leftover(self):
        """Row 18: an ignored local config outside a `DISPOSABLE` folder, which `kept_files` counts."""
        self.assert_split_first(self.left_out_of_a_retry(self.leftover("ignored")))

    def pending(self, state):
        """`prepare` that leaves `state`, an unfinished Git operation, over a clean tree at the base: HEAD and
        `git status --porcelain` match a fresh start, so only the operation's state file tells."""
        def shape(work, commit):
            support.git(work, "reset", "-q", "--hard", "origin/main")
            if state == "MERGE_HEAD":
                # The finding's own form: `-s ours` keeps the tree; the next commit would take `commit` as a parent.
                support.git(work, "merge", "--no-commit", "--no-ff", "-s", "ours", commit)
            else:
                path = Path(work, support.git(work, "rev-parse", "--git-path", state).strip())
                if state.startswith("rebase-"):
                    path.mkdir()
                    (path / "head-name").write_text("refs/heads/example\n")
                else:
                    path.write_text(commit + "\n")
            self.assertEqual(support.git(work, "status", "--porcelain").strip(), "", "the tree must look clean")
        return shape

    def test_a_split_guard_holds_a_pending_merge(self):
        """Round 12 class pass, an unfinished Git operation the clean tree hides; row 20: MERGE_HEAD."""
        self.assert_split_first(self.left_out_of_a_retry(self.pending("MERGE_HEAD")))

    def test_a_split_guard_holds_a_pending_cherry_pick(self):
        """Row 21: CHERRY_PICK_HEAD."""
        self.assert_split_first(self.left_out_of_a_retry(self.pending("CHERRY_PICK_HEAD")))

    def test_a_split_guard_holds_a_pending_revert(self):
        """Row 22: REVERT_HEAD."""
        self.assert_split_first(self.left_out_of_a_retry(self.pending("REVERT_HEAD")))

    def test_a_split_guard_holds_a_pending_rebase_merge(self):
        """Row 23: rebase-merge/, an interactive or merge-backend rebase."""
        self.assert_split_first(self.left_out_of_a_retry(self.pending("rebase-merge")))

    def test_a_split_guard_holds_a_pending_rebase_apply(self):
        """Row 24: rebase-apply/, an apply-backend rebase or `git am`."""
        self.assert_split_first(self.left_out_of_a_retry(self.pending("rebase-apply")))

    def test_a_split_guard_holds_a_pending_bisect(self):
        """Row 25: BISECT_LOG, a bisect that checks out other commits."""
        self.assert_split_first(self.left_out_of_a_retry(self.pending("BISECT_LOG")))

    def test_a_split_guard_holds_a_real_rebase_off_its_branch(self):
        """Review of build/sd-1242: a real rebase stopped midway detaches HEAD, and the branch check ended the run
        before the guard, so no hold was recorded and each Retry blocked again. The guard reads first and holds."""
        def stopped(work, _commit):
            support.git(work, "reset", "-q", "--hard", "origin/main")
            done = subprocess.run(["git", "-C", str(work), "rebase", "--exec", "false", "--root"],
                                  capture_output=True, check=False)
            self.assertNotEqual(done.returncode, 0, "the rebase must stop")
            self.assertEqual(support.git(work, "branch", "--show-current").strip(), "", "HEAD is detached")
        self.assert_split_first(self.left_out_of_a_retry(stopped))
        self.assertEqual(build.split(self.db, self.bundle["assignment"]), "held")

    def test_a_split_guard_passes_an_ignored_cache(self):
        """Row 19: an ignored file under a `DISPOSABLE` folder is a cache no builder inherits as work."""
        supervisor = self.left_out_of_a_retry(self.leftover("cache"))
        self.assertEqual(supervisor.run()["step"], "enqueued")

    def test_a_subset_retry_with_no_earlier_work_builds_on(self):
        """Row 5, the guard: a bundle cancelled before any build holds no earlier commit, so the kept items build."""
        supervisor = self.left_out_of_a_retry(run=False)
        self.assertEqual(supervisor.run()["step"], "enqueued")
        prompt = self.calls("claude")[-1]["argv"]
        self.assertNotIn(f"- sd:{self.items[2]}", prompt[prompt.index("-p") + 1])

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

    def test_a_lane_blocker_with_no_disposition_is_left_to_prepare(self):
        """Lane review of build/sd-1557: a preflight disposition read spent fix rounds on a finding main may
        fix, as prepare's catch-up moves the head. Prepare alone, at the final head, checks dispositions."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor().run()
        self.assertEqual(ended.get("step"), "enqueued", ended.get("outcome"))
        # The fake `sd-ship` answers only `lane`: a disposition read would fail the run.
        self.assertEqual([call for call in self.calls("sd-ship") if "adjudicate" in call["argv"]], [])

    def test_a_usage_limit_in_a_fix_resumes_that_fix_without_a_round(self):
        """Review of build/sd-1557: a limit in the fix resumed at `reviewing`, which charged the same blockers
        another round. The claim resumes the fix itself, with the round's blockers and session."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "rate_limited"], sd_review=["blocking"])
        self.assertEqual(self.supervisor().run()["outcome"], "rate_limited")
        reset = int((self.plans / "reset.txt").read_text())
        later = datetime.fromtimestamp(reset + 1, UTC).isoformat(timespec="seconds")
        with mock.patch.object(build, "clock", return_value=later):
            again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                root=self.root / "worktrees")
        assert again is not None
        self.assertEqual(build.run(self.db, again["id"])["step"], "fixing")
        self.plan(claude=["fix1"], sd_review=["clean"])
        self.assertEqual(self.supervisor(again["id"]).run()["step"], "enqueued")
        fix = [call["argv"] for call in self.calls("claude")][-1]
        self.assertEqual(fix[fix.index("--resume") + 1], "session-fixture")
        self.assertIn("- work.txt:1 a blocker", fix[fix.index("-p") + 1])
        self.assertEqual(build.run(self.db, again["id"])["review_round"], 1, "the resumed fix charged no round")

    def test_a_resumed_fix_keeps_every_blocker_whole(self):
        """Review of build/sd-1557: the resumed fix read its blockers back from the 4000-character note, so a
        multiline finding lost its tail, its bullet read as a blocker of its own and a later finding was cut.
        The resumed prompt is the interrupted one."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "rate_limited"], sd_review=["blocking-long"])
        self.assertEqual(self.supervisor().run()["outcome"], "rate_limited")
        reset = int((self.plans / "reset.txt").read_text())
        later = datetime.fromtimestamp(reset + 1, UTC).isoformat(timespec="seconds")
        with mock.patch.object(build, "clock", return_value=later):
            again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                root=self.root / "worktrees")
        assert again is not None
        self.plan(claude=["fix1"], sd_review=["clean"])
        self.assertEqual(self.supervisor(again["id"]).run()["step"], "enqueued")
        limited, resumed = ([call["argv"] for call in self.calls("claude")][n] for n in (1, 2))
        said = resumed[resumed.index("-p") + 1]
        self.assertIn("later.txt:9 a later blocker", said)
        self.assertIn("x" * 4500, said)
        self.assertEqual(said, limited[limited.index("-p") + 1], "the same brief as the interrupted fix")

    def test_a_usage_limit_in_the_class_pass_resumes_the_class_pass(self):
        """Review of build/sd-1557: a limit in the last round's class pass resumed with the plain FIX brief, so
        the class pass never ran before the bound parked the run. It resumes with CLASS_PASS."""
        R = build_run.REVIEW_ROUNDS
        self.press()
        self.write_pr()
        self.plan(claude=["pass", *(f"fix{n}" for n in range(1, R)), "rate_limited"], sd_review=["blocking"] * R)
        self.assertEqual(self.supervisor().run()["outcome"], "rate_limited")
        reset = int((self.plans / "reset.txt").read_text())
        later = datetime.fromtimestamp(reset + 1, UTC).isoformat(timespec="seconds")
        with mock.patch.object(build, "clock", return_value=later):
            again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                root=self.root / "worktrees")
        assert again is not None
        self.plan(claude=["fixc"], sd_review=["clean"])
        self.assertEqual(self.supervisor(again["id"]).run()["step"], "enqueued")
        fix = [call["argv"] for call in self.calls("claude")][-1]
        self.assertIn("do one class pass", fix[fix.index("-p") + 1])
        self.assertEqual(build.run(self.db, again["id"])["review_round"], R)

    def test_a_usage_limit_reclaim_refuses_a_worktree_on_another_branch(self):
        """Review of build/sd-1557: the reclaim resumed at building or fixing, past the worktree step's branch
        guard, so a checkout switched during the hold took the builder's commits. The guard runs first."""
        self.press()
        self.write_pr()
        self.plan(claude=["rate_limited"])
        self.assertEqual(self.supervisor().run()["outcome"], "rate_limited")
        support.git(Path(self.bundle["work_path"]), "checkout", "-q", "-b", "operator-side")
        reset = int((self.plans / "reset.txt").read_text())
        later = datetime.fromtimestamp(reset + 1, UTC).isoformat(timespec="seconds")
        with mock.patch.object(build, "clock", return_value=later):
            again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                root=self.root / "worktrees")
        assert again is not None
        self.assertEqual(build.run(self.db, again["id"])["step"], "building")
        self.plan(claude=["pass"], sd_review=["clean"])
        ended = self.supervisor(again["id"]).run()
        self.assertEqual(ended["outcome"], "blocked")
        self.assertIn("is on operator-side, not build/sd-", ended["blocked"])
        self.assertEqual(len(self.calls("claude")), 1, "no builder ran on the switched checkout")

    def test_a_sent_finding_briefs_how_to_record_a_rejection(self):
        """sd:10010: a Send to builder note may reject a review finding; the brief says how to record it."""
        self.press()
        add_note(self.db, self.items[0], "comment", "Send to builder: reject a.py:2, the pin is checked in b.py:9")
        # Noted before this run started, as a Send to builder's note is.
        self.db.execute("UPDATE note SET timestamp = (SELECT created_at FROM runner_run WHERE id = ?) "
                        "WHERE body LIKE 'Send to builder: reject%'", (self.bundle["id"],))
        prompt = build_run.BRIEF.format(**self.supervisor().brief())
        self.assertIn("reject a.py:2", prompt)
        self.assertIn(f"adjudicate --item {self.items[0]} --expected-head <HEAD> --json", prompt)
        self.assertIn("`--dispositions-file /dev/stdin`", prompt)

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

    def test_a_slow_leak_guard_adds_nothing(self):
        """Pack sd:9483: a guard scan past its timeout raised and ended the supervisor; the push still checks."""
        system = self.root / "system"
        guard = system / "local-leak-guard" / "leak-guard.sh"
        guard.parent.mkdir(parents=True)
        guard.write_text("exit 1\n")
        (self.repo / ".git" / "hooks" / "pre-push").write_text("#!/bin/sh\n# installed by local-leak-guard\n")
        head = support.git(self.repo, "rev-parse", "HEAD")
        environ = {**os.environ, "SYSTEM_TOOLS_ROOT": str(system)}
        real = subprocess.run

        def run(argv, *args, **kwargs):
            if argv[:2] == ["sh", str(guard)]:
                raise subprocess.TimeoutExpired(argv, 60)
            return real(argv, *args, **kwargs)
        with mock.patch.object(subprocess, "run", side_effect=run):
            self.assertEqual(ADAPTER.enqueue_refusals(self.repo, "origin/main", head, "fix: x", "",
                                                      environ=environ), [])

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

    def test_the_brief_cites_an_item_on_every_commit(self):
        """sd:10319: builders committed review fix-ups with no trailer, so a bundle drop refused: 'commit ... cites
        no item'. Each fix-up carries its item's `Closes:` trailer again, not a `Refs:` line, which names a related
        item, not the owner (review); a fix brief says so again."""
        self.assertIn("that item's `Closes: sd:N` trailer again", build_run.BRIEF)
        for brief in (build_run.BRIEF, build_run.FIX, build_run.CLASS_PASS):
            self.assertNotIn("Refs: sd:N", brief)
        self.assertIn("its item's `Closes: sd:N` trailer on it", build_run.FIX)
        self.assertIn("its item's `Closes: sd:N` trailer on it", build_run.CLASS_PASS)

    def test_the_brief_forbids_a_kill_by_pattern(self):
        """Pack sd:6815: build 268's `pkill -f "sd-check --json"` ended the vault job's queued gate."""
        self.assertIn("never `pkill` or `killall`", build_run.BRIEF)

    def test_the_brief_keeps_test_runs_in_the_foreground(self):
        """Pack sd:9183: build 1105's suite hit the 120 s Bash default and moved to the background."""
        self.assertIn("`timeout: 600000`", build_run.BRIEF)
        self.assertIn("never `run_in_background`", build_run.BRIEF)

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

    def test_a_fresh_build_with_a_finding_briefs_it(self):
        """sd:10320: `sd assignments build N --finding TEXT` on a ready item with no build refused: "would be a
        fresh build, with no builder to send it to". It queues the build and its builder reads TEXT."""
        item = create_item(self.db, kind="task", title="Build me", repo=str(self.repo), status="ready")
        queued = build.queue_again(self.db, [item], who="operator", finding="pin the clock\n<b>naïve</b>")
        self.assertEqual([(entry["item"], entry["status"]) for entry in queued], [(item, "queued")])
        claimed = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                              root=self.root / "worktrees")
        assert claimed is not None
        prompt = build_run.BRIEF.format(**self.supervisor(claimed["id"]).brief())
        self.assertIn(f"- sd:{item}: pin the clock\n<b>naïve</b>", prompt)

    def test_a_plain_retry_briefs_review_blockers_as_the_reviewer_s_not_the_operator_s(self):
        """Lead review of pack sd:9498: a plain Retry noted the parked run's blockers as Send to builder, which the
        brief presents as the operator's findings. They are the reviewer's text; only Send to builder is the
        operator's."""
        self.press()
        item = self.items[0]
        build.end(self.db, self.bundle["id"], "blocked", "3 review rounds left blockers: a.py:2 pin check; "
                                                       "check the group, then requeue or cancel; worktree kept")
        # The block holds the run until a reap reads its supervisor gone: release it as that reap does.
        self.db.execute("UPDATE runner_run SET released_at = created_at WHERE id = ?", (self.bundle["id"],))
        self.db.execute("UPDATE runner_lease SET released_at = (SELECT created_at FROM runner_run WHERE id = ?) "
                        "WHERE run = ?", (self.bundle["id"], self.bundle["id"]))
        build.retry(self.db, self.bundle["assignment"], who="operator")
        add_note(self.db, item, "comment", "Send to builder: the Retry label reads wrong on phones")
        again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                            root=self.root / "worktrees")
        assert again is not None
        prompt = build_run.BRIEF.format(**self.supervisor(again["id"]).brief())
        operator = prompt.index("The operator sent these findings")
        reviewer = prompt.index("They are the reviewer's text, not an operator")
        self.assertIn(f"- sd:{item}: the Retry label reads wrong on phones", prompt[operator:reviewer])
        self.assertNotIn("a.py:2 pin check", prompt[operator:reviewer])
        self.assertIn(f"- sd:{item}: The last review round left these blockers: a.py:2 pin check", prompt[reviewer:])

    def test_a_second_retry_briefs_only_the_latest_park_s_blockers(self):
        """Pack sd:9593: the brief replayed the REVIEWED notes of every earlier Retry under "the last review round"."""
        self.press()
        item = self.items[0]

        def park_and_retry(run: dict, blockers: str) -> dict:
            build.end(self.db, run["id"], "blocked", f"3 review rounds left blockers: {blockers}; "
                                                     "check the group, then requeue or cancel; worktree kept")
            self.db.execute("UPDATE runner_run SET released_at = created_at WHERE id = ?", (run["id"],))
            self.db.execute("UPDATE runner_lease SET released_at = (SELECT created_at FROM runner_run WHERE id = ?) "
                            "WHERE run = ?", (run["id"], run["id"]))
            build.retry(self.db, run["assignment"], who="operator")
            again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                root=self.root / "worktrees")
            assert again is not None
            return again

        second = park_and_retry(self.bundle, "a.py:2 old pin check")
        # The first round's run, its Retry note, then the second run, each a second apart.
        self.db.execute("UPDATE runner_run SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                        (self.bundle["id"],))
        self.db.execute("UPDATE note SET timestamp = '2000-01-01T00:00:01+00:00' WHERE body LIKE '%old pin check%'")
        self.db.execute("UPDATE runner_run SET created_at = '2000-01-01T00:00:02+00:00' WHERE id = ?", (second["id"],))
        third = park_and_retry(second, "b.py:3 new pin check")
        prompt = build_run.BRIEF.format(**self.supervisor(third["id"]).brief())
        self.assertIn(f"- sd:{item}: The last review round left these blockers: b.py:3 new pin check", prompt)
        self.assertNotIn("old pin check", prompt)

    def test_a_run_recovered_after_a_retry_keeps_that_retry_s_blockers(self):
        """Review of sd:9593: a run that replaces a Retry's lost run, with no Retry between, briefed no blockers: its
        cutoff was the lost run's release, after the Retry's notes."""
        self.press()
        item = self.items[0]

        def release(run: dict) -> None:
            self.db.execute("UPDATE runner_run SET released_at = created_at WHERE id = ?", (run["id"],))
            self.db.execute("UPDATE runner_lease SET released_at = (SELECT created_at FROM runner_run WHERE id = ?) "
                            "WHERE run = ?", (run["id"], run["id"]))

        def claim() -> dict:
            again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                root=self.root / "worktrees")
            assert again is not None
            return again

        build.end(self.db, self.bundle["id"], "blocked", "3 review rounds left blockers: c.py:4 pin check; "
                                                       "check the group, then requeue or cancel; worktree kept")
        release(self.bundle)
        build.retry(self.db, self.bundle["assignment"], who="operator")
        lost = claim()
        # The Retry, then the lost run, each a second apart; the run that replaces it reads after both.
        self.db.execute("UPDATE runner_run SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                        (self.bundle["id"],))
        self.db.execute("UPDATE note SET timestamp = '2000-01-01T00:00:01+00:00' WHERE body LIKE '%c.py:4%'")
        self.db.execute("UPDATE runner_run SET created_at = '2000-01-01T00:00:02+00:00' WHERE id = ?", (lost["id"],))
        build.end(self.db, lost["id"], "cancelled", "the supervisor was lost before a builder session started")
        release(lost)
        lead = self.bundle["assignment"]
        runner.requeue(self.db, lead, expected_revision=runner.queue_state(self.db, lead)["revision"], who="recovery")
        prompt = build_run.BRIEF.format(**self.supervisor(claim()["id"]).brief())
        self.assertIn(f"- sd:{item}: The last review round left these blockers: c.py:4 pin check", prompt)

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

    def test_a_failing_check_after_a_deferral_records_no_rebuttal(self):
        """Review of sd:9769: an unmanaged repository's own check runs after the review; when it fails, a fix
        round follows, so the round's deferral records no rebuttal that the fix would outlive."""
        self.press()
        self.write_pr()
        self.plan(claude=["pass", "mend", "fix"], sd_review=["blocking", "offdelta", "clean"])
        self.assertEqual(self.supervisor(checks=lambda work: [CHECK]).run()["step"], "enqueued")
        self.assertEqual(len(self.calls("claude")), 3, "the failing check did not go back to the builder")
        filed = self.db.execute("SELECT title FROM item WHERE title LIKE 'Review follow-up%'").fetchall()
        self.assertEqual(len(filed), 1, "the deferred blocker was not filed")
        rebuttals = self.db.execute(
            "SELECT COUNT(*) FROM state WHERE kind = 'checkpoint' AND key LIKE 'ship-adjudication:%'").fetchone()[0]
        self.assertEqual(rebuttals, 0, "a round whose check failed recorded a rebuttal a later fix outlives")

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

    def test_only_the_check_gets_github_token_from_the_pat(self):
        """sd:8515: the check sees GITHUB_TOKEN; the supervisor env, which gh and git push read, does not."""
        self.env["GITHUB_PERSONAL_ACCESS_TOKEN"] = "example-pat"
        self.env.pop("GITHUB_TOKEN", None)
        token = [sys.executable, "-c", "import os, sys; sys.exit(os.environ.get('GITHUB_TOKEN') != 'example-pat')"]
        self.press()
        self.write_pr()
        self.plan(claude=["pass"], sd_review=["clean"])
        supervisor = self.supervisor(checks=lambda work: [token])
        self.assertIsNone(supervisor.env.get("GITHUB_TOKEN"))  # not assertNotIn: it would print the env
        self.assertEqual(supervisor.run()["step"], "enqueued")

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
        if answer is not None:  # the lane's own store holds what `sd-ship lane list` reads (pack sd:8601)
            self.store(answer, row["branch"])
        return answer

    def store(self, entry, branch):
        """A lane entry as `sd_lane.store` writes it, a new `checkpoint` row of its key, unless that row is newest."""
        body = json.dumps({"branch": branch, **entry}, sort_keys=True)
        key = f"lane:v1:example/repo:{entry['id']}"
        newest = self.db.execute("SELECT body FROM state WHERE key = ? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        if newest is None or newest[0] != body and json.loads(newest[0]).get("status") != "cancelled":
            with self.db:
                self.db.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                (key, build.clock(), body))

    def stored(self, ident):
        found = self.db.execute("SELECT body FROM state WHERE key = ? ORDER BY id DESC LIMIT 1",
                                (f"lane:v1:example/repo:{ident}",)).fetchone()
        return json.loads(found[0]) if found else None

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

    def test_a_tick_starts_one_bundle_so_the_next_load_read_sees_it(self):
        """Pack sd:8747: one tick claimed a bundle per repository on one quiet load read, eight builds at once, and
        the load went to 300. A tick claims one; the next tick reads the load again before it claims another."""
        repos = [str(self.repo)]
        for name in ("second", "third"):
            other = support.repository(self.root / name, bare=self.root / f"{name}.git")
            upsert_repo(self.db, str(other), remote=f"https://github.com/fixture/{name}.git", managed=1)
            repos.append(str(other))
        build.queue(self.db, [create_item(self.db, kind="task", title="Built", repo=repo, status="ready")
                              for repo in repos], who="operator")
        self.assertEqual(len(self.tick()["claimed"]), 1, "one start per tick")
        self.assertEqual(len(self.tick()["claimed"]), 1, "the next tick takes the next")
        self.assertEqual(self.tick(load=50.0)["claimed"], [], "a load above the cap starts none")

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

    def test_build_again_on_a_failed_lane_entry_is_fix(self):
        """pack sd:8791: `sd assignments build N` on a lane_failed run is the panel's Fix: the entry handed back,
        the bundle queued again under its lead with the lane's reason as the finding. It refused before."""
        run = self.enqueued()
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": "gate red"}
        self.tick()
        queued = build.queue_again(self.db, self.items, who="operator")
        self.assertEqual([(entry["id"], entry["item"], entry["status"]) for entry in queued],
                         [(run["assignment"], self.items[0], "queued")])
        self.assertEqual(self.statuses(), ["ready"])
        handed = self.stored("e1")
        self.assertEqual((handed["status"], handed["handed_back"]["by"]), ("cancelled", "operator"))
        notes = [row[0] for row in self.db.execute("SELECT body FROM note WHERE item = ?", self.items)]
        self.assertTrue(any(body.startswith(build.FINDING) and "gate red" in body for body in notes), notes)

    def test_build_again_with_a_finding_on_a_failed_lane_entry_sends_it(self):
        """pack sd:8792: `--finding` on a Fix is the note the builder reads, in place of the lane's reason."""
        self.enqueued()
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": "gate red"}
        self.tick()
        build.queue_again(self.db, self.items, who="operator", finding="pin the fixture clock")
        notes = [row[0] for row in self.db.execute("SELECT body FROM note WHERE item = ?", self.items)]
        self.assertIn(build.FINDING + "pin the fixture clock", notes)

    def reopened_dependency(self):
        """The bundle's lead waits on an item reopened since its build: the dependency is open again."""
        first = create_item(self.db, kind="task", title="Prerequisite", repo=str(self.repo), status="ready")
        with self.db:
            self.db.execute("UPDATE item SET fields = ? WHERE id = ?", (json.dumps({"blocked_by": [first]}),
                                                                        self.items[0]))
        return first

    def test_fix_on_a_failed_lane_entry_refuses_while_a_dependency_is_open(self):
        """Review of build/sd-1306: Fix handed back the lane entry and queued the bundle past the check Retry
        runs; dispatch then blocked and parked it. It refuses first and writes nothing."""
        run = self.enqueued()
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": "gate red"}
        self.tick()
        first = self.reopened_dependency()
        with self.assertRaisesRegex(build.BuildRefused, f"item {self.items[0]} waits on sd:{first}"):
            build.lane_fix_check(self.db, run["id"])
        with self.assertRaisesRegex(build.BuildRefused, f"waits on sd:{first}"):
            build.queue_again(self.db, self.items, who="operator")
        self.assertNotIn("handed_back", self.stored("e1"))
        self.assertEqual(self.statuses(), ["in_progress"])

    def lane_failed_bundle(self, waiter, on):
        """A two-item bundle whose lane entry failed; then item `waiter` waits on item `on`, both its members."""
        run = self.enqueued(2)
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": "gate red"}
        self.tick()
        with self.db:
            self.db.execute("UPDATE item SET fields = ? WHERE id = ?",
                            (json.dumps({"blocked_by": [self.items[on]]}), self.items[waiter]))
        return run

    def queued_builds(self, item):
        return self.db.execute("SELECT COUNT(*) FROM assignment WHERE item = ? AND status = 'queued'",
                               (item,)).fetchone()[0]

    def lead_waits_on_its_member(self, pressed):
        """Review of build/sd-1306: the lead waits on its member. Fix refused the bundle until the member was
        done, and a Build of the member refused the same way. The member leaves and builds on its own."""
        run = self.lane_failed_bundle(0, 1)
        build.queue_again(self.db, [self.items[one] for one in ([pressed] if type(pressed) is int else pressed)],
                          who="operator")
        self.assertEqual(self.queued_builds(self.items[1]), 1, "the member builds")
        self.assertEqual(self.queued_builds(self.items[0]), 0, "the lead waits")
        self.assertEqual(build.run(self.db, run["id"])["outcome"], "lane_failed")
        self.assertEqual(self.statuses(), ["in_progress", "ready"])

    def test_a_build_of_the_member_a_lane_failed_lead_waits_on_builds_it_alone(self):
        self.lead_waits_on_its_member(1)

    def test_a_build_of_a_lane_failed_lead_that_waits_on_its_member_builds_the_member(self):
        self.lead_waits_on_its_member(0)

    def test_a_build_of_the_lead_and_its_member_frees_the_member_in_either_order(self):
        """Review of build/sd-1306: selecting B then A freed B, then refused A as it waits on B, and rolled
        both back. The lead's run freed once stays lane_failed for Fix, whatever the order."""
        for order in ([0, 1], [1, 0]):
            with self.subTest(order=order):
                if order[0]:
                    self.setUp()
                self.lead_waits_on_its_member(order)

    def test_a_freed_member_that_waited_builds_once_its_own_dependency_is_done(self):
        """Review of build/sd-1306: A waits on B, B on C outside the bundle. A Build of A freed B without
        queueing it; once C was done, a Build of B went to A's Fix again and refused, as A waits on B."""
        self.lane_failed_bundle(0, 1)
        third = create_item(self.db, kind="task", title="Outside", repo=str(self.repo), status="ready")
        with self.db:
            self.db.execute("UPDATE item SET fields = ? WHERE id = ?",
                            (json.dumps({"blocked_by": [third]}), self.items[1]))
        build.queue_again(self.db, [self.items[0]], who="operator")
        self.assertEqual(self.queued_builds(self.items[1]), 0, "B waits on C")
        with self.db:
            self.db.execute("UPDATE item SET status = 'done' WHERE id = ?", (third,))
        build.queue_again(self.db, [self.items[1]], who="operator")
        self.assertEqual(self.queued_builds(self.items[1]), 1, "B builds on its own")

    def test_a_build_of_a_waiting_chain_frees_it_and_queues_only_its_runnable_member(self):
        """Review of build/sd-1306: lead A waits on B and B on C, all members of a lane-failed bundle.
        Selecting A, B, C freed B and C and queued C; the loop read B as left alone and queued it too, which
        refused, as B waits on C, and rolled the split back. B is freed once and waits, in any order."""
        import itertools

        for number, order in enumerate(itertools.permutations(range(3))):
            with self.subTest(order=order):
                if number:
                    self.setUp()
                run = self.enqueued(3)
                self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": "gate red"}
                self.tick()
                with self.db:
                    for waiter, on in ((0, 1), (1, 2)):
                        self.db.execute("UPDATE item SET fields = ? WHERE id = ?",
                                        (json.dumps({"blocked_by": [self.items[on]]}), self.items[waiter]))
                build.queue_again(self.db, [self.items[one] for one in order], who="operator")
                self.assertEqual([self.queued_builds(item) for item in self.items], [0, 0, 1], "only C builds")
                self.assertEqual(build.run(self.db, run["id"])["outcome"], "lane_failed")

    def test_a_build_of_a_split_bundle_settles_every_selected_member(self):
        """Review of build/sd-1306: lead A waits on member B, and C waits on nothing. Selecting A, B, C freed B,
        then read C as the bundle's Fix, which refused, as A still waits on B, and rolled the split back. The
        split settles every selected member: B builds alone, A and C stay for Fix, in any order."""
        import itertools

        for number, order in enumerate(itertools.permutations(range(3))):
            with self.subTest(order=order):
                if number:
                    self.setUp()
                run = self.enqueued(3)
                self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": "gate red"}
                self.tick()
                with self.db:
                    self.db.execute("UPDATE item SET fields = ? WHERE id = ?",
                                    (json.dumps({"blocked_by": [self.items[1]]}), self.items[0]))
                build.queue_again(self.db, [self.items[one] for one in order], who="operator")
                self.assertEqual([self.queued_builds(item) for item in self.items], [0, 1, 0], "only B builds")
                self.assertEqual(build.run(self.db, run["id"])["outcome"], "lane_failed")

    def test_a_build_of_the_lead_and_an_unrelated_member_frees_the_one_it_waits_on(self):
        """Class pass over the selection of `queue_again`: A and C selected, A waits on B. The press of C alone
        refuses, but with A in the selection the split frees B, whichever comes first."""
        for order in ((0, 2), (2, 0)):
            with self.subTest(order=order):
                self.setUp()
                self.enqueued(3)
                self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": "gate red"}
                self.tick()
                with self.db:
                    self.db.execute("UPDATE item SET fields = ? WHERE id = ?",
                                    (json.dumps({"blocked_by": [self.items[1]]}), self.items[0]))
                build.queue_again(self.db, [self.items[one] for one in order], who="operator")
                self.assertEqual([self.queued_builds(item) for item in self.items], [0, 1, 0], "only B builds")

    def newer_build_of_member(self):
        """Lead A and member B; the bundle fails in the lane; B builds fresh and its new build claims it, so
        B's item is in_progress under the newer build. Then B waits on A."""
        run = self.enqueued(2)
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": "gate red"}
        self.tick()
        with self.db:  # the operator moved B back to ready, then built it on its own
            self.db.execute("UPDATE item SET status = 'ready' WHERE id = ?", (self.items[1],))
        newer = build.queue(self.db, [self.items[1]], who="operator")[0]["id"]
        with self.db:
            self.db.execute("UPDATE assignment SET status = 'running' WHERE id = ?", (newer,))
            self.db.execute("UPDATE item SET status = 'in_progress', fields = ? WHERE id = ?",
                            (json.dumps({"blocked_by": [self.items[0]]}), self.items[1]))
        return run, newer

    def assert_newer_build_owns_member(self, newer):
        state = item_state(self.db, self.items[1])["item"]
        self.assertEqual((state["status"], state.get("parked_at")), ("in_progress", None),
                         "the newer build owns B's status")
        self.assertEqual(self.db.execute("SELECT status FROM assignment WHERE id = ?", (newer,)).fetchone()[0],
                         "running")

    def test_a_fix_leaves_a_member_a_newer_build_owns(self):
        """Review of build/sd-1306: Fix of the old bundle read B by its in_progress status alone, then `_leave`
        moved B to ready and parked it while the newer build ran it. Fix sends back only the members it owns."""
        run, newer = self.newer_build_of_member()
        build.queue_again(self.db, [self.items[0]], who="operator")
        self.assert_newer_build_owns_member(newer)
        self.assertEqual(self.queued_builds(self.items[0]), 1, "Fix sends the lead back")

    def test_a_conflict_rebuild_leaves_a_member_a_newer_build_owns(self):
        """Class pass: the conflict rebuild reads the same members (`_sendable`)."""
        run, newer = self.newer_build_of_member()
        with self.db:
            build._rebuild(self.db, build.run(self.db, run["id"]), "conflict", build.clock(), "origin/main", "e1")
        self.assert_newer_build_owns_member(newer)

    def test_a_build_frees_no_member_a_newer_build_owns(self):
        """Class pass: the Build path frees the members the lead waits on (`_free`); one a newer build owns is
        not its to free, so A waits for that build and the refusal says so."""
        run, newer = self.newer_build_of_member()
        with self.db:
            self.db.execute("UPDATE item SET fields = '{}' WHERE id = ?", (self.items[1],))
            self.db.execute("UPDATE item SET fields = ? WHERE id = ?",
                            (json.dumps({"blocked_by": [self.items[1]]}), self.items[0]))
        with self.assertRaisesRegex(build.BuildRefused, f"item {self.items[0]} waits on sd:{self.items[1]}"):
            build.queue_again(self.db, [self.items[0]], who="operator")
        self.assert_newer_build_owns_member(newer)

    def test_a_build_frees_no_member_while_the_lane_entry_is_back_in_the_lane(self):
        """Review of build/sd-1306: lead A waits on member B of a lane-failed bundle, and the operator sent the
        lane entry back to the lane. Build A freed and queued B while the bundle's PR ran in the lane. It
        refuses, as Fix does, and writes nothing."""
        for status in ("pending", "running"):
            with self.subTest(status=status):
                self.setUp()
                run = self.lane_failed_bundle(0, 1)
                self.store({"id": "e1", "status": status}, run["branch"])
                with self.assertRaisesRegex(build.BuildRefused, f"the lane holds branch .* again \\({status}\\)"):
                    build.queue_again(self.db, [self.items[0]], who="operator")
                self.assertEqual([self.queued_builds(item) for item in self.items], [0, 0])
                self.assertEqual(self.statuses(), ["in_progress", "in_progress"])

    def build_rows(self):
        return self.db.execute("SELECT id, status, result FROM assignment ORDER BY id").fetchall()

    def test_a_finding_refuses_to_free_a_member_that_waits_outside_the_bundle(self):
        """Lead triage of build/sd-1306: the lead waits on its member, which waits on an open item outside the
        bundle. A Fix with a finding freed the member, queued nothing and reported success: the finding was lost.
        It refuses before it frees, and writes nothing."""
        outside = create_item(self.db, kind="task", title="Outside", repo=str(self.repo), status="ready")
        self.lane_failed_bundle(0, 1)
        with self.db:
            self.db.execute("UPDATE item SET fields = ? WHERE id = ?",
                            (json.dumps({"blocked_by": [outside]}), self.items[1]))
        before = [tuple(row) for row in self.build_rows()]
        with self.assertRaisesRegex(build.BuildRefused, "finding"):
            build.queue_again(self.db, [self.items[0]], who="operator", finding="fix the gate")
        self.assertEqual([tuple(row) for row in self.build_rows()], before)

    def test_a_build_of_another_member_names_the_press_that_frees_the_waited_on_one(self):
        """Lead triage of build/sd-1306: the lead waits on one member and a third member is pressed. The press
        freed the needed member, then refused with a hint to Fix once it is done, which no press could reach.
        It refuses first, writes nothing, and names the lead and the member it waits on."""
        run = self.enqueued(3)
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": "gate red"}
        self.tick()
        with self.db:
            self.db.execute("UPDATE item SET fields = ? WHERE id = ?",
                            (json.dumps({"blocked_by": [self.items[1]]}), self.items[0]))
        before = [tuple(row) for row in self.build_rows()]
        with self.assertRaises(build.BuildRefused) as refused:
            build.queue_again(self.db, [self.items[2]], who="operator")
        self.assertIn(f"sd:{self.items[0]}", str(refused.exception))
        self.assertIn(f"sd:{self.items[1]}", str(refused.exception))
        self.assertNotIn("Fix sends the bundle back", str(refused.exception))
        self.assertEqual([tuple(row) for row in self.build_rows()], before)
        self.assertEqual(build.run(self.db, run["id"])["outcome"], "lane_failed")

    def test_fix_of_a_lane_failed_lead_leaves_out_the_member_that_waits_on_it(self):
        """Review of build/sd-1306: the member waits on the lead, so Fix refused the whole bundle. The member
        leaves the Fix, parked, and the lead goes back to its builder."""
        run = self.lane_failed_bundle(1, 0)
        self.assertEqual([member["item"] for member in build.lane_fix_check(self.db, run["id"])[0]],
                         [self.items[0]])
        build.queue_again(self.db, [self.items[0]], who="operator")
        self.assertEqual(self.queued_builds(self.items[0]), 1)
        self.assertEqual(self.statuses(), ["ready", "ready"])
        self.assertIsNotNone(item_state(self.db, self.items[1])["item"]["parked_at"], "the member waits, parked")

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
        # pack sd:8601: the failed entry is handed back to the builder, as Fix hands it, so `lane retry` refuses it.
        handed = self.stored("e1")
        self.assertEqual((handed["status"], handed["handed_back"]["run"], handed["handed_back"]["by"]),
                         ("cancelled", run["id"], "build"), handed)

    def test_an_unreadable_lane_entry_of_another_repository_holds_no_rebuild(self):
        """pack sd:8615: `_rebuild` read every repository's lane entries and refused on any one not JSON, so one bad
        row in another repository's lane stopped every conflict rebuild. It reads the read entry's repository."""
        run = self.enqueued()
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": self.CONFLICT}
        with self.db:
            self.db.execute("INSERT INTO state(kind, key, timestamp, body) VALUES "
                            "('checkpoint', 'lane:v1:example/other:bad', ?, 'not json')", (build.clock(),))
        result = self.tick()
        self.assertEqual(len(result["claimed"]), 1, "the same tick claims it again")
        self.assertEqual(self.stored("e1")["handed_back"]["by"], "build")
        self.assertEqual(build.run(self.db, run["id"])["outcome"], "lane_failed")

    def test_a_conflict_whose_entry_is_not_the_one_read_is_left_to_the_operator(self):
        """pack sd:8601, failure table rows 1 to 3: the rebuild hands back the entry the watch read, or none. A
        newer entry on the branch (a Retry since), a lane store not read, or no entry stored leaves the run
        lane_failed, its bundle unqueued and no entry handed back, with a note that says why."""
        for case in ("newer", "unread", "none"):
            with self.subTest(case=case):
                run = self.enqueued()
                failed = {"id": f"f-{case}", "status": "failed", "reason": self.CONFLICT}
                self.entries[self.items[0]] = failed
                if case == "newer":  # a Retry since: the hub took it after the failed one
                    self.store(failed, run["branch"])
                    self.store({"id": f"g-{case}", "status": "pending"}, run["branch"])
                if case == "unread":  # pack sd:8615: the read entry is stored; its own lane holds the bad row
                    self.store(failed, run["branch"])
                with mock.patch.object(self, "lane", lambda row, items, failed=failed: failed) if case != "newer" else contextlib.nullcontext():
                    if case == "unread":
                        with self.db:
                            self.db.execute("INSERT INTO state(kind, key, timestamp, body) VALUES "
                                            "('checkpoint', 'lane:v1:example/repo:bad', ?, 'not json')", (build.clock(),))
                    self.assertEqual(self.tick()["claimed"], [])
                with self.db:
                    self.db.execute("DELETE FROM state WHERE key = 'lane:v1:example/repo:bad'")
                self.assertEqual(build.run(self.db, run["id"])["outcome"], "lane_failed")
                self.assertEqual(self.statuses(), ["in_progress"])
                for ident in (f"f-{case}", f"g-{case}"):
                    entry = self.stored(ident)
                    self.assertTrue(entry is None or "handed_back" not in entry, entry)
                notes = [row["body"] for row in self.db.execute("SELECT body FROM note WHERE item = ?", self.items)]
                self.assertTrue(any(body.startswith(f"Build run {run['id']}: no rebuild") for body in notes), notes)
                if case == "unread":
                    self.assertTrue(any("lane:v1:example/repo:bad was not read" in body for body in notes), notes)
                # pack sd:8636: the refusal is the rebuild's, so it names the rebuild and not Fix.
                said = {"newer": "the rebuild waits until it ends", "unread": "the rebuild waits for a readable lane",
                        "none": "so the rebuild writes nothing"}[case]
                refusals = [body for body in notes if body.startswith(f"Build run {run['id']}: no rebuild")]
                self.assertTrue(all(said in body and "Fix writes" not in body and "Fix waits" not in body
                                    for body in refusals), refusals)

    def test_a_rebuild_stopped_after_the_hand_back_write_leaves_nothing_and_the_next_tick_finishes(self):
        """pack sd:8601, failure table row 4: the hand-back and the send-back share lane_failed's one transaction."""
        run = self.enqueued()
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": self.CONFLICT}
        self.store(self.entries[self.items[0]], run["branch"])
        with mock.patch.object(build, "_send_back", side_effect=RuntimeError("stopped between the steps")):
            with self.assertRaises(RuntimeError):
                build.lane_failed(self.db, build.run(self.db, run["id"]), self.CONFLICT, entry="e1")
        row = build.run(self.db, run["id"])
        self.assertEqual((row["step"], row["outcome"], "handed_back" in self.stored("e1")), ("enqueued", None, False))
        self.assertEqual(len(self.tick()["claimed"]), 1)
        self.assertEqual(self.stored("e1")["handed_back"]["by"], "build")

    def test_a_conflict_rebuild_waits_while_a_dependency_is_open(self):
        """Class pass of the Fix finding: the conflict rebuild sends the bundle back as Fix does, so it reads the
        dependencies too, and leaves the run lane_failed with a note that names the wait."""
        run = self.enqueued()
        first = self.reopened_dependency()
        self.entries[self.items[0]] = {"id": "e1", "status": "failed", "reason": self.CONFLICT}
        self.assertEqual(self.tick()["claimed"], [])
        self.assertEqual(build.run(self.db, run["id"])["outcome"], "lane_failed")
        self.assertNotIn("handed_back", self.stored("e1"))
        self.assertEqual(self.statuses(), ["in_progress"])
        notes = [row["body"] for row in self.db.execute("SELECT body FROM note WHERE item = ?", self.items)]
        self.assertTrue(any(body.startswith(f"Build run {run['id']}: no rebuild") and f"waits on sd:{first}" in body
                            for body in notes), notes)

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
                run = self.enqueued(2, skip_lead=case == "skipped")  # a skipped lead ends cancelled
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
        for n in range(build.CONFLICT_REBUILDS):  # each enqueue is a new lane entry
            self.entries[self.items[0]] = {"id": f"e{n}", "status": "failed", "reason": self.CONFLICT}
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

    def test_a_kept_non_ascii_path_reads_as_its_name(self):
        """Lane review of 0e395efb, class pass: git quotes a non-ASCII path unless told not to; the kept
        line names the file, and a cache folder beside it is still disposable."""
        work = self.blocked()
        exclude(work, "__pycache__/\n")
        (work / "café" / "__pycache__").mkdir(parents=True)
        (work / "café" / "__pycache__" / "x.pyc").write_bytes(b"x")
        (work / "café" / "notes.txt").write_text("x\n")
        self.assertEqual(build_run.kept_files(("git",), str(work)), "?? café/notes.txt")

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
        path = folder / name.format(run=self.bundle["id"], assignment=self.bundle["assignment"])
        path.write_text("x")
        os.utime(path, (0, 0))
        self.db.execute("UPDATE runner_run SET outcome = 'blocked', released_at = ?, log_path = ?, detail = ? "
                        "WHERE id = ?", (build_run.now(), str(path), f"refused; log {path}", self.bundle["id"]))
        self.db.commit()
        return path, build_run.log_dir(self.logs, str(self.repo)) / path.name

    def test_a_rate_limited_bundles_files_wait_for_its_resume(self):
        """Review of build/sd-1557: a usage limit releases the run and queues the bundle again; the archive
        moved its blockers file, so the resumed fix found none and reviewed again, charging another round."""
        path, target = self.ended("sd-{assignment}.blockers.json")
        paths = [path, *(path.with_name(f"sd-{self.bundle['assignment']}.{name}") for name in ("body.md", "title"))]
        for each in paths[1:]:
            each.write_text("x")
            os.utime(each, (0, 0))
        self.db.execute("UPDATE runner_run SET outcome = ? WHERE id = ?", (build.RATE_LIMITED, self.bundle["id"]))
        self.db.commit()
        self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [])
        self.assertTrue(all(each.exists() for each in paths), "the resume reads the local files")
        self.db.execute("UPDATE runner_run SET outcome = 'done' WHERE id = ?", (self.bundle["id"],))
        self.db.commit()
        self.assertEqual(sorted(build_run.archive(self.db, self.root / "local", self.logs)),
                         sorted(str(target.with_name(each.name)) for each in paths))

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

    def test_a_copy_past_the_deadline_lands_while_the_tick_waits(self):
        """sd:8339: the tick exited at the 20 s deadline and killed its copier, so a longer copy never landed.
        `archive_wait` gives it the rest of the tick; the next tick collects the copy."""
        path, target = self.ended("{run}.jsonl")
        real = shutil.copy2

        def slow(source, destination, *args, **kwargs):
            time.sleep(0.6)
            return real(source, destination, *args, **kwargs)

        with mock.patch.object(build_run.shutil, "copy2", side_effect=slow):
            self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs, deadline=0.1), [])
            build_run.archive_wait(5)
        self.assertEqual(target.read_text(), "x")
        with mock.patch.object(build_run.shutil, "copy2", side_effect=OSError("no copy expected")):
            self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [str(target)])
        self.assertFalse(path.exists())

    def test_a_file_a_killed_copier_left_goes_after_the_others(self):
        """sd:8339: a file whose copy outlasts every tick restarted first each time and held the files after it."""
        path, target = self.ended("{run}.jsonl")
        later = path.with_name(f"{self.bundle['id']}.supervisor.log")
        later.write_text("y")
        os.utime(later, (0, 0))
        target.parent.mkdir(parents=True)
        target.with_name(f".{target.name}.1.part").write_text("half")
        release = threading.Event()
        self.addCleanup(release.set)
        real = shutil.copy2

        def stuck(source, destination, *args, **kwargs):
            if Path(source) == path:
                release.wait(30)
                raise OSError("still too slow")
            return real(source, destination, *args, **kwargs)

        with mock.patch.object(build_run.shutil, "copy2", side_effect=stuck):
            moved = build_run.archive(self.db, self.root / "local", self.logs, deadline=0.5)
            release.set()
            self.join_archive_workers()
        self.assertEqual(moved, [str(target.with_name(later.name))])

    def test_a_retrys_new_body_survives_the_archive(self):
        """sd:8826: the body moved once every run of its assignment ended; a retry that started and wrote a
        new body while the copy ran lost it to the unlink after the copy."""
        path, target = self.ended("sd-{assignment}.body.md")
        real = shutil.copy2

        def retried(source, destination, *args, **kwargs):
            real(source, destination, *args, **kwargs)
            with contextlib.closing(connect(self.database)) as other, other:
                other.execute("INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, "
                              "retained_path, created_at, updated_at) SELECT 'retry', assignment, run + 1, "
                              "repo, branch, owner, work_path, retained_path, created_at, updated_at "
                              "FROM runner_run WHERE id = ?", (self.bundle["id"],))
            path.write_text("## Summary\n\nthe retry's body\n")

        with mock.patch.object(build_run.shutil, "copy2", side_effect=retried):
            self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [])
        self.assertEqual(path.read_text(), "## Summary\n\nthe retry's body\n")

    def test_a_source_rewritten_mid_copy_leaves_no_torn_copy(self):
        """sd:8989: the copy of a source rewritten mid-copy stayed in place, and its size and mtime could match
        the new source, so the next tick's `_same_copy` took the torn copy as finished and removed the source."""
        path, target = self.ended("sd-{assignment}.body.md")

        def torn(source, destination, *args, **kwargs):
            Path(source).write_text("new")
            Path(destination).write_text("old")  # the old bytes, as long as the new source
            shutil.copystat(source, destination)

        with mock.patch.object(build_run.shutil, "copy2", side_effect=torn):
            self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [])
        self.assertFalse(target.exists(), "a copy whose source moved is removed")
        os.utime(path, (0, 0))
        self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [str(target)])
        self.assertEqual(target.read_text(), "new")

    @staticmethod
    def landed_copy_stat(target: Path, landed):
        """Patch `Path.stat` so a stat of `target` once its copy landed calls `landed()` instead."""
        real = Path.stat

        def stat(self_, *args, **kwargs):
            if self_ == target and os.path.exists(target):
                return landed()
            return real(self_, *args, **kwargs)

        return mock.patch.object(Path, "stat", stat)

    def test_a_bulk_io_error_after_the_copy_keeps_the_local_file(self):
        """sd:8826 lane round 1: the tick checked the copy with `target.stat()` outside the worker, so an
        I/O error from the bulk root escaped and aborted the tick."""
        path, target = self.ended("sd-{assignment}.body.md")

        def failed():
            raise OSError(5, "Input/output error")

        with self.landed_copy_stat(target, failed):
            self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [])
        self.assertTrue(path.exists(), "an unread copy leaves the local file")
        self.assertEqual(self.db.execute("SELECT log_path FROM runner_run WHERE id = ?",
                                         (self.bundle["id"],)).fetchone()[0], str(path))

    def test_a_bulk_stall_after_the_copy_keeps_the_tick_in_its_deadline(self):
        """sd:8826 lane round 1: the tick's `target.stat()` after the copy hung on a stalled bulk root, past
        the worker's deadline."""
        path, target = self.ended("sd-{assignment}.body.md")
        release = threading.Event()
        self.addCleanup(release.set)

        def stalled():
            release.wait(30)
            raise OSError(5, "Input/output error")

        result: list = []

        def run_tick():
            with contextlib.closing(connect(self.database)) as connection:  # a connection is one thread's
                result.append(build_run.archive(connection, self.root / "local", self.logs, deadline=0.3))

        with self.landed_copy_stat(target, stalled):
            tick = threading.Thread(target=run_tick, daemon=True)
            tick.start()
            tick.join(5)
            alive = tick.is_alive()
            release.set()
            tick.join(5)
            self.join_archive_workers()
        self.assertFalse(alive, "the tick returns within the worker's deadline")
        self.assertEqual(result, [[]])
        self.assertTrue(path.exists(), "a copy the worker could not check leaves the local file")

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

    def test_a_killed_copiers_partial_file_goes(self):
        """sd:8275 lane round 1: a tick that exits at its deadline kills its copier mid-copy; its part file
        must not pile up beside the archive."""
        path, target = self.ended("{run}.jsonl")
        target.parent.mkdir(parents=True)
        stale = target.with_name(f".{target.name}.1.part")
        stale.write_text("half")
        self.assertEqual(build_run.archive(self.db, self.root / "local", self.logs), [str(target)])
        self.assertEqual(sorted(entry.name for entry in target.parent.iterdir()), [target.name])

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

    def test_a_usage_limit_requeues_the_draft_and_holds_this_host(self):
        """sd:9938: a draft's 429 ended it blocked (sd:9907 left `drafting` out). It queues again, with no
        block or followup, and this host drafts nothing until the limit's `resetsAt`."""
        run = self.claimed()
        self.plan(claude=["rate_limited"])
        ended = self.drafter(run["id"]).run()
        self.assertEqual((ended["outcome"], ended["retry"]), ("rate_limited", None))
        self.assertEqual((self.assignment_status(), self.statuses()), ("queued", ["planning"]))
        self.assertNotIn("followup", [note["kind"] for note in self.notes()])
        self.assertIsNotNone(build.run(self.db, run["id"])["released_at"])
        reset = datetime.fromtimestamp(int((self.plans / "reset.txt").read_text()), UTC)
        until = reset.isoformat(timespec="seconds")
        self.assertEqual(writes.read_settings(self.db, ["build.rate_hold.hub"]), {"build.rate_hold.hub": until})
        room = build.room(self.db, load=1.0, load_cap=40.0, interactive=[])
        self.assertIsNone(draft.claim(self.db, owner="fixture", available=room), "no draft before the reset")
        later = (reset + timedelta(seconds=1)).isoformat(timespec="seconds")
        with mock.patch.object(build, "clock", return_value=later):
            room = build.room(self.db, load=1.0, load_cap=40.0, interactive=[])
        again = draft.claim(self.db, owner="fixture", available=room)
        assert again is not None
        self.assertEqual((again["assignment"], again["run"]), (self.assignment, 2), "the same draft, again")

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

    def test_the_hub_builds_a_satellite_lane_while_its_host_is_over_the_load_cap(self):
        """sd:8991: a satellite over its load cap claimed nothing, and the hub left its rows queued; the hub now
        builds them itself, and the repository's one slot holds the satellite's claim off meanwhile."""
        (self.satellite / ".git").mkdir(parents=True)  # the hub builds only a repository checked out here
        _, sat = self.both()
        with self.on("work"):
            self.assertEqual(self.tick(load=30.0, load_cap=24.0)["claimed"], [])
        with self.on("hub"):
            hub = self.tick()
            taken = [build.run(self.db, ident) for ident in hub["claimed"]]
            self.assertEqual([(row["repo"], row["owner"]) for row in taken], [(str(self.repo), "fixture")])
            taken = [build.run(self.db, ident) for ident in self.tick()["claimed"]]
            self.assertEqual([(row["repo"], row["owner"]) for row in taken], [(str(self.satellite), "fixture")])
        later = create_item(self.db, kind="task", title="Later", repo=str(self.satellite), status="ready")
        build.queue(self.db, [later], who="operator")
        with self.on("work"):
            self.assertEqual(self.tick()["claimed"], [], "the hub's bundle holds the repository's slot")

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

    def hold(self, host, seconds=3600):
        """`host`'s usage-limit hold, `seconds` from now, as a rate-limited run of its own records it."""
        until = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        writes.set_setting(self.db, f"build.rate_hold.{host}", until)
        return until

    def after(self, until):
        """The build clock one second past `until`."""
        later = (datetime.fromisoformat(until) + timedelta(seconds=1)).isoformat(timespec="seconds")
        return mock.patch.object(build, "clock", return_value=later)

    def test_a_held_host_claims_nothing_until_its_reset_and_others_claim(self):
        """Pack sd:9907: the hub's usage-limit hold stops its claims, pickup and drafts and names the reset; the
        satellite claims meanwhile, and the hub claims again once the reset passed."""
        until = self.hold("hub")
        hub_item, _ = self.both()
        with self.on("hub"):
            held = self.tick()
            self.assertEqual(held["claimed"], [])
            self.assertIn(f"hold until {until}", held["why"])
            self.assertIn(f"hold until {until}", build.room(self.db, load=1.0, load_cap=40.0, interactive=[])["why"])
        with self.on("work"):
            work = self.tick()
        self.assertEqual([build.run(self.db, ident)["repo"] for ident in work["claimed"]], [str(self.satellite)])
        with self.on("hub"), self.after(until):
            hub = self.tick()
        self.assertEqual([build.run(self.db, ident)["members"][0]["item"] for ident in hub["claimed"]], [hub_item])

    def test_a_held_satellite_is_not_stuck_and_its_press_queues(self):
        """Pack sd:9907: a held satellite beats its hold for days; the hub must not read that as a stuck
        dispatcher and block its rows (sd:8081), nor refuse its press, and the Builds panel names the reset."""
        until = self.hold("work")
        _, sat = self.both()
        with self.on("work"):
            for _ in range(build.STUCK_TICKS + 1):
                self.assertIn(f"hold until {until}", self.tick()["why"])
        with self.on("hub"):
            self.assertEqual(len(self.tick()["claimed"]), 1, "the hub claims its own")
            self.assertIn(f"hold until {until}", build.unclaimed(self.db)[str(self.queued(sat))])
            later = create_item(self.db, kind="task", title="Later", repo=str(self.satellite), status="ready")
            self.assertEqual(len(build.queue(self.db, [later], who="operator")), 1, "the press queues")
        self.assertEqual(self.db.execute("SELECT status FROM assignment WHERE item = ?", (sat,)).fetchone()[0],
                         "queued")
        with self.on("work"), self.after(until):
            taken = self.tick()["claimed"]
        self.assertEqual([build.run(self.db, ident)["members"][0]["item"] for ident in taken], [sat])

    def queued(self, item):
        return self.db.execute("SELECT id FROM assignment WHERE item = ? AND status = 'queued'", (item,)).fetchone()[0]

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

    def test_a_satellite_pick_that_merges_cleans_and_leaves_pickup_on(self):
        """Review of sd:8393: `checking` runs the hub's refresh and dashboard health; on a satellite they fail
        and would turn auto-pickup off for every host. A satellite's merged pick goes to `cleaning`."""
        build.set_pickup(self.db, True)
        sat = create_item(self.db, kind="task", title="Satellite", repo=str(self.satellite), status="ready")
        with self.on("work"):
            result = self.tick()
        self.assertEqual(result["picked"], [sat])
        run = build.run(self.db, result["claimed"][0])
        build.write(self.db, run["id"], step="enqueuing")
        self.assertTrue(build.enqueued(self.db, run["id"]))
        self.entries[sat] = {"id": "e1", "status": "merged"}
        with self.on("work"):
            self.assertEqual(self.tick()["moved"], [{"run": run["id"], "step": "cleaning"}])
        self.assertTrue(build.pickup_state(self.db)["on"])

    def test_a_satellite_tick_picks_its_own_lanes_and_drafts_nothing(self):
        """Sd:8393: a satellite's tick picks and claims its own lane's ready item; drafts stay the hub's."""
        build.set_pickup(self.db, True)
        ready = create_item(self.db, kind="task", title="Ready", repo=str(self.repo), status="ready")
        mine = create_item(self.db, kind="task", title="Mine", repo=str(self.satellite), status="ready")
        planned = create_item(self.db, kind="task", title="Draft", repo=str(self.repo), status="planning")
        self.assertIsNotNone(draft.queue_if_eligible(self.db, planned, who="dashboard"))
        with self.on("work"):
            result = self.tick()
        self.assertEqual((result["picked"], result["drafted"], len(result["claimed"])), ([mine], [], 1))
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

    def test_a_hub_fault_in_a_resumed_fix_resumes_the_fix(self):
        """Review of build/sd-1557: a fault before a resumed fix's child started walked again with its blockers
        already read, so the walk took the review for the fix: the fix never ran and a round was spent."""
        self.plan(claude=["pass", "rate_limited"], sd_review=["blocking"])
        self.assertEqual(self.supervisor().run()["outcome"], "rate_limited")
        reset = int((self.plans / "reset.txt").read_text())
        later = datetime.fromtimestamp(reset + 1, UTC).isoformat(timespec="seconds")
        with mock.patch.object(build, "clock", return_value=later):
            again = build.claim(self.db, owner="fixture", load=1.0, load_cap=40.0, interactive=[],
                                root=self.root / "worktrees")
        assert again is not None
        self.plan(claude=["fix1", "fix2"], sd_review=["clean", "clean"])
        before = len(self.calls("claude"))
        patch, fired = self.faults_once("write", self.UNREACHABLE, fields=("step",),
                                        where=lambda kwargs: kwargs.get("step") == "fixing")
        with patch:
            ended = self.supervisor(again["id"]).run()
        self.assertEqual((fired, ended.get("step")), (["write"], "enqueued"))
        fixes = [call["argv"] for call in self.calls("claude")][before:]
        self.assertEqual(len(fixes), 1, "the resumed fix did not run once")
        self.assertIn("- work.txt:1 a blocker", fixes[0][fixes[0].index("-p") + 1])
        self.assertEqual(len(self.calls("sd-review")), 2, "a review ran in place of the fix")

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

    def test_a_supervisor_reads_its_runs_claimed_budget(self):
        """sd:9109 review: the child timeout read the machine's cap; it is now the budget the claim fixed."""
        with mock.patch.object(build_run.build, "bundle_max", return_value=1), \
                mock.patch.object(build_run.build, "run", return_value={"repo": "~/repos/a", "work_path": "/w",
                                                                       "assignment": 1, "budget_minutes": 200}), \
                mock.patch.object(build_run, "_lead_item", return_value=(1, [])), \
                mock.patch.object(build_run.Supervisor, "take_resume"):
            supervisor = build_run.Supervisor(mock.Mock(), "r", tools=mock.Mock(), log_root=Path("/l"),
                                              cited=lambda *_: None)
        self.assertEqual(supervisor.timeout, 12000)


class ChildEnvironment(unittest.TestCase):
    """Only a repository's own checks get GITHUB_TOKEN from the operator's PAT (sd:8506, sd:8515)."""

    def test_the_supervisor_env_holds_no_github_token_from_the_pat(self) -> None:
        """sd:8515: gh and its credential helper would act as the PAT, not the gh login."""
        env = build_run._environ({"GITHUB_PERSONAL_ACCESS_TOKEN": "example-pat"}, "run")
        self.assertIsNone(env.get("GITHUB_TOKEN"))

    def test_a_check_gets_github_token_from_the_pat(self) -> None:
        env = build_run._checks_environ({"GITHUB_PERSONAL_ACCESS_TOKEN": "example-pat"})
        self.assertEqual(env.get("GITHUB_TOKEN"), "example-pat")

    def test_a_set_github_token_wins(self) -> None:
        env = build_run._checks_environ({"GITHUB_PERSONAL_ACCESS_TOKEN": "example-pat",
                                         "GITHUB_TOKEN": "example-own"})
        self.assertEqual(env["GITHUB_TOKEN"], "example-own")


if __name__ == "__main__":
    unittest.main()
