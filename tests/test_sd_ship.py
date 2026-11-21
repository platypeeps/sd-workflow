"""Real bare Git, real CLI children, and a GitHubDouble; no model or GitHub traffic."""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import sd_db
from sd_db import connect, create_assignment, create_item, initialise, upsert_repo
from sd_db import ship as receipts
from sd_db.repos import set_declaration, set_runner_merge
from sd_db.testing.github import GitHubDouble
from sd_db.testing.remote import FixtureRemote, RemoteRefusal, _git
from sd_db.writes import set_setting

from tests.clean_env import clean_environment

ROOT = pathlib.Path(__file__).resolve().parents[1]
#: The bound on one real `sd-ship` command (sd:1539): a hang guard, not a
#: speed claim. At 30 s it failed gates at load average 157 on runs that take
#: about 7 s alone.
CLI_TIMEOUT = 300
#: The directory holding the library this run imported: the checkout's `lib/`
#: (sd:3280). A run that got this far has the library by definition, so this
#: path always exists.
SITE_PACKAGES = pathlib.Path(sd_db.__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
loader = importlib.machinery.SourceFileLoader("sd_ship_tested", str(ROOT / "bin/sd-ship"))
spec = importlib.util.spec_from_loader(loader.name, loader)
ship = importlib.util.module_from_spec(spec)
loader.exec_module(ship)

#: The *Development / Code, before merge* cap. Read from the module that
#: owns it, so these fixtures follow the row instead of restating it.
CAP = importlib.import_module("sd_ship_history").AUTOMATIC_CODE_REVIEW_PASSES
sd_ship_review = importlib.import_module("sd_ship_review")


#: Hold `argv[2]`'s ship lock over the database at `argv[1]` for `argv[3]` seconds.
SHIP_LOCK_HOLDER = """
import sys, time
from pathlib import Path
from sd_db import ship
with ship.repository_lock(Path(sys.argv[1]), sys.argv[2],
                          holder={"command": "sd-ship prepare --item 1872", "item": 1872}):
    print("held", flush=True)
    time.sleep(float(sys.argv[3]))
"""


#: The head a lagging pull object still reports: the pre-push one on #1145.
LAGGING_HEAD = "a3b265f4" * 5


def pull_head_waits(sleep):
    """The waits a patched `time.sleep` saw from the post-push re-read.

    The patch is process-wide, so it also sees the sub-second lock backoffs of
    other threads and the Copilot materialization wait; neither is this one.
    """
    return [call.args[0] for call in sleep.call_args_list
            if call.args[0] >= 1 and call.args[0] != ship.COPILOT_MATERIALIZATION_INTERVAL_SECONDS]


#: A reviewer that exits nonzero and still reports a located blocker: sd-review
#: keeps the finding, so the pass reviewed something and is spent (sd:2147).
FAILED_WITH_BLOCKER = ("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({'type':'result','subtype':'success',"
                       "'structured_output':{'findings':[{'path':'src.py','line':1,'severity':'high','family':'correctness',"
                       "'summary':'kept from a failed answer'}]}}))\nsys.exit(1)\n")


def review_thread(*, resolved=False, outdated=False, path="src.py", line=1, original=None,
                  author="reviewer", body="A finding."):
    """One GraphQL `reviewThreads` node, with its first comment, as GitHub answers it."""
    return {"isResolved": resolved, "isOutdated": outdated, "path": path, "line": line,
            "originalLine": line if original is None else original,
            "comments": {"nodes": [{"author": {"login": author}, "body": body}]}}


class ShipDouble(GitHubDouble):
    """Adds precisely the write/read surfaces this adapter calls to the shared double."""
    def __init__(self, remote):
        super().__init__(remote)
        # `serve_forever` polls for shutdown every 0.5 s by default, so every
        # `close` in cleanup waited up to half a second: about a quarter of
        # this module's wall clock (sd:1605). Same server, shorter poll.
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.admin = True
        self.moved = False
        self.lose_merge = False
        self.lose_create = False
        self.no_create_result = False
        self.statuses = []
        #: Workflow runs `GET /actions/runs` serves, filtered by the query's
        #: `head_sha` and `event` as GitHub filters them (sd:1110).
        self.workflow_runs = []
        self.review_payload = {"reviews": [], "comments": []}
        self.review_sequences = {"reviews": [], "comments": []}
        self.copilot_requests = []
        self.copilot_pending = set()
        self.copilot_requested_login = "copilot-pull-request-reviewer[bot]"
        self.lose_copilot_request = False
        self.create_draft = False
        #: What `GET /rules/branches/{b}` and `GET /rulesets/{id}` answer: no
        #: ruleset unless a test says so, the way the fixture's `main` had no
        #: classic protection before sd:1110 said so (sd:1327).
        self.rules = []
        self.rulesets = {}
        #: How many `GET /pulls/{n}` reads still answer the pre-push head, the
        #: way GitHub does for a second or so after a push lands (sd:1394).
        self.lagging_pull_reads = 0
        #: How many `GET /pulls/{n}` reads answer `mergeable_state: blocked`
        #: after a status post, the way GitHub did on #580 while it recomputed
        #: the merge rules for a just-posted `sd/local-gate` (sd:2050).
        self.blocked_after_status = 0
        self.blocked_pull_reads = 0
        #: What `GET /pulls/{n}/files` answers, by pull number (sd:1151).
        self.pull_files = {}
        #: The review threads `POST /graphql` pages a hundred at a time, and
        #: what it answers instead when a test sets one (sd:3098).
        self.review_threads = []
        self.threads_answer = None

    def _pull(self, pull):
        head = getattr(pull, "merged_head", None) or pull.head_sha(self.remote)
        return {"number": pull.number, "title": pull.title, "body": pull.body,
                "state": "closed" if pull.state == "MERGED" else pull.state.lower(),
                "merged": pull.state == "MERGED", "draft": pull.draft,
                "html_url": f"https://github.com/{self.remote.slug}/pull/{pull.number}",
                "head": {"ref": pull.head, "sha": head, "repo": {"full_name": self.remote.slug}},
                "base": {"ref": pull.base},
                "requested_reviewers": ([{"login": self.copilot_requested_login}]
                                          if pull.number in self.copilot_pending else []),
                "mergeable": pull.mergeable == "MERGEABLE",
                "mergeable_state": pull.merge_state_status.lower(), "merge_commit_sha": pull.merge_commit_sha}

    def _route(self, method, path, body):
        query = parse_qs(urlsplit(path).query)
        path = urlsplit(path).path
        prefix = f"/repos/{self.remote.slug}"
        if method == "GET" and path == "/user":
            return 200, {"login": "fixture"}
        if method == "GET" and path == prefix:
            code, value = super()._route(method, path, body)
            value.update(allow_squash_merge=True, allow_rebase_merge=True, allow_merge_commit=True)
            value["permissions"]["admin"] = self.admin
            return code, value
        if method == "GET" and path == f"{prefix}/collaborators":
            page = int(query.get("page", [1])[0])
            return 200, self.remote.collaborators[(page - 1) * 100:page * 100]
        if path == f"{prefix}/pulls":
            if method == "POST":
                if self.no_create_result:
                    raise RemoteRefusal(503, "create transport failed")
                pull = self.remote.open_pull_request(body["head"], base=body["base"], title=body["title"], body=body["body"])
                pull.draft = self.create_draft
                pull.checks = [{"name": "check", "status": "completed", "conclusion": "success",
                                "head_sha": pull.head_sha(self.remote), "app": {"id": 7}}]
                if self.lose_create:
                    raise RemoteRefusal(503, "create response lost")
                return 201, self._pull(pull)
            # GitHub filters by `state` and defaults to open (sd:2656).
            wanted = query.get("state", ["open"])[0]
            pulls = [self._pull(pull) for pull in self.remote.pull_requests.values()]
            return 200, [pull for pull in pulls if wanted == "all" or pull["state"] == wanted]
        if method == "POST" and path.startswith(f"{prefix}/pulls/") and path.endswith("/requested_reviewers"):
            number = int(path.split("/")[-2])
            if self.lose_copilot_request:
                raise RemoteRefusal(503, "review request transport failed")
            self.copilot_requests.append({"number": number, "body": body})
            self.copilot_pending.add(number)
            return 201, self._pull(self.remote.pull(number))
        if method == "POST" and path.startswith(f"{prefix}/statuses/"):
            # The local gate's write (sd:1843), newest first as GitHub lists.
            record = {**body, "sha": path.rsplit("/", 1)[1], "creator": {"login": "fixture"}}
            self.statuses.insert(0, record)
            self.blocked_pull_reads = self.blocked_after_status
            return 201, record
        if path.endswith("/statuses"):
            return 200, self.statuses
        if method == "GET" and path.endswith("/protection") and isinstance(self.remote.protection, RemoteRefusal):
            raise self.remote.protection  # a 403 or 5xx, which is not "absent" (sd:1110)
        if method == "GET" and "/rules/branches/" in path:
            # Paged as the endpoint pages: thirty a page unless asked otherwise.
            size, page = int(query.get("per_page", [30])[0]), int(query.get("page", [1])[0])
            return 200, self.rules[(page - 1) * size:page * size]
        if method == "GET" and "/rulesets/" in path:
            ruleset_id = int(path.rsplit("/", 1)[1])
            if ruleset_id not in self.rulesets:
                raise RemoteRefusal(404, f"no ruleset {ruleset_id}")
            return 200, self.rulesets[ruleset_id]
        if method == "GET" and path == f"{prefix}/actions/runs":
            wanted_sha = query.get("head_sha", [None])[0]
            wanted_event = query.get("event", [None])[0]
            runs = [run for run in self.workflow_runs
                    if (wanted_sha is None or run.get("head_sha") == wanted_sha)
                    and (wanted_event is None or run.get("event") == wanted_event)]
            return 200, {"total_count": len(runs), "workflow_runs": runs}
        if method == "POST" and path == "/graphql":
            if isinstance(self.threads_answer, RemoteRefusal):
                raise self.threads_answer
            if self.threads_answer is not None:
                return 200, self.threads_answer
            start = int(body["variables"]["after"] or 0)
            nodes = self.review_threads[start:start + 100]
            return 200, {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": start + 100 < len(self.review_threads),
                             "endCursor": str(start + len(nodes))},
                "nodes": nodes}}}}}
        # Reviews and review comments: empty unless a test says otherwise.
        if method == "GET" and path.startswith(f"{prefix}/pulls/") and path.endswith("/files"):
            return 200, [{"filename": name} for name in self.pull_files.get(int(path.split("/")[-2]), [])]
        if method == "GET" and path.startswith(f"{prefix}/pulls/") and path.rsplit("/", 1)[1] in self.review_payload:
            kind = path.rsplit("/", 1)[1]
            sequence = self.review_sequences[kind]
            if sequence:
                return 200, sequence.pop(0) if len(sequence) > 1 else sequence[0]
            return 200, self.review_payload[kind]
        if method == "GET" and path.startswith(f"{prefix}/pulls/"):
            value = self._pull(self.remote.pull(int(path.rsplit("/", 1)[1])))
            if self.lagging_pull_reads:
                self.lagging_pull_reads -= 1
                value["head"]["sha"] = LAGGING_HEAD
            if self.blocked_pull_reads:
                self.blocked_pull_reads -= 1
                value["mergeable_state"] = "blocked"
            return 200, value
        if method == "PUT" and path.endswith("/merge"):
            number = int(path.split("/")[-2])
            pull = self.remote.pull(number)
            if self.moved:
                self.remote.commit_on(pull.head, "raced\n\nAuthored-with: human", files={"raced.py": "changed\n"})
            pull.merged_head = pull.head_sha(self.remote)
            if "commit_title" in body:  # a rebase takes no message (sd:6790)
                pull.title = body["commit_title"] + "\n\n" + body["commit_message"]
            commit = self.remote.merge(number, sha=body["sha"], method=body["merge_method"])
            if self.lose_merge:
                raise RemoteRefusal(503, "merge response lost")
            return 200, {"merged": True, "sha": commit}
        return super()._route(method, path, body)

    def _handler(self):
        double = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def serve(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                status, value = double.answer(self.command, self.path, json.loads(raw) if raw else None, "gh")
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = serve
            do_POST = serve
            do_PUT = serve
        return Handler


#: The fixture `gh`. It answers the way the real one does: the body on stdout,
#: and with `--include` the response line and headers ahead of it, on a
#: refusal too (`HTTP/2.0 404 Not Found` then the JSON), which is what
#: `GitHub.api_status` reads the status from. Exit 1 on a non-2xx, as gh.
#:
#: It speaks HTTP/1.0 over a bare socket rather than through urllib: every
#: `gh` call starts this interpreter, and importing urllib.request cost about
#: twenty of the fifty milliseconds a call took (sd:1605). The double's
#: handler is HTTP/1.0 too, so the answer ends when the server closes.
SHIM = '''#!/usr/bin/env python3
import json,os,socket,sys
args=sys.argv[1:]
assert args[0]=='api',args
include='--include' in args
args=[a for a in args if a!='--include']
path=args[1]
method=args[args.index('--method')+1]
data=sys.stdin.read().encode() if '--input' in args else b''
host,port=os.environ['SHIP_DOUBLE'].split('//',1)[1].rsplit(':',1)
head='%s /%s HTTP/1.0\\r\\nHost: %s:%s\\r\\nContent-Type: application/json\\r\\nContent-Length: %d\\r\\n\\r\\n' % (method,path,host,port,len(data))
chunks=[]
with socket.create_connection((host,int(port))) as connection:
 connection.sendall(head.encode()+data)
 while True:
  chunk=connection.recv(65536)
  if not chunk: break
  chunks.append(chunk)
response,_,body=b''.join(chunks).partition(b'\\r\\n\\r\\n')
_,status,reason=(response.split(b'\\r\\n',1)[0].decode()+'  ').split(' ',2)
status,reason,body=int(status),reason.strip(),body.decode()
def emit(status,reason,body):
 if include: print('HTTP/2.0 %d %s\\nContent-Type: application/json\\n' % (status,reason))
 print(body)
emit(status,reason,body)
if not 200<=status<300:
 message=json.loads(body).get('message','') if body.startswith('{') else ''
 print('gh: %s (HTTP %d)' % (message,status),file=sys.stderr);sys.exit(1)
'''


REMOTE_URL = "https://github.com/fixture/repo.git"
_TEMPLATE: pathlib.Path | None = None


def fixture_template() -> pathlib.Path:
    """The bare remote, its seed, the clone and the operator clone, built once.

    `ShipCase.setUp` built these with about twenty git children a test, which
    at 227 tests was minutes of the gate (sd:1605). Built lazily on the first
    setUp rather than in setUpModule or setUpClass, which run-tests.sh refuses
    in a module it splits by test id; each shard process builds its own.
    Every test gets a copy, so nothing one test does reaches another.
    """
    global _TEMPLATE
    if _TEMPLATE is None:
        directory = pathlib.Path(tempfile.mkdtemp(prefix="sd-ship-template-")).resolve()
        atexit.register(shutil.rmtree, directory, True)
        remote = FixtureRemote(directory / "fixture")
        remote.commit_on("topic", "change\n\nAuthored-with: human", files={"src.py": "value = 1\n", "Makefile": "check:\n\t@echo fixture-check-pass\n"})
        root = directory / "clone"
        subprocess.run(["git", "clone", "-q", str(remote.path), str(root)], check=True)
        _git(root, "checkout", "topic")
        _git(root, "config", "user.name", "Fixture")
        _git(root, "config", "user.email", "fixture@example.invalid")
        _git(root, "remote", "set-url", "origin", REMOTE_URL)
        operator = directory / "operator"
        subprocess.run(["git", "clone", "-q", str(remote.path), str(operator)], check=True)
        _git(operator, "remote", "set-url", "origin", REMOTE_URL)
        _TEMPLATE = directory
    return _TEMPLATE


class CopiedRemote(FixtureRemote):
    """A `FixtureRemote` whose bare repository and seed are a template's copy."""

    def __init__(self, root: pathlib.Path, template: pathlib.Path) -> None:
        self._template = template
        super().__init__(root)

    def _make_bare(self) -> None:
        shutil.copytree(self._template, self.root, symlinks=True)
        self.seed = self.root / "seed"
        # The seed pushes to its own bare copy, never to the template's.
        _git(self.seed, "remote", "set-url", "origin", str(self.path))
        _git(self.seed, "update-index", "-q", "--refresh")

    def rev_parse(self, ref: str) -> str:
        """GitHub keeps a merged pull request's head after the merge deletes its branch (sd:3006)."""
        try:
            return super().rev_parse(ref)
        except RuntimeError:
            merged = [pull.merged_head for pull in self.pull_requests.values()
                      if pull.head == ref and getattr(pull, "merged_head", None)]
            if not merged:
                raise
            return merged[-1]


def git_transport(binary: str, remote: pathlib.Path, url: str) -> str:
    """Keep real Git and local transport without starting Python for every call."""
    network = shlex.join([binary, "-c", f"url.{remote}.insteadOf={url}"])
    return (
        '#!/bin/sh\nfor arg in "$@"; do\n'
        '  case "$arg" in\n'
        f'    fetch|push|ls-remote) exec {network} "$@" ;;\n'
        '  esac\ndone\n'
        f'exec {shlex.quote(binary)} "$@"\n'
    )


class ShipCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = pathlib.Path(self.temp.name).resolve()
        # The three repositories come from one template per process, copied;
        # `fixture_template` below builds it the way this setUp used to.
        template = fixture_template()
        self.remote = CopiedRemote(self.directory / "fixture", template / "fixture")
        self.remote.protection = {"enforce_admins": {"enabled": True}, "required_pull_request_reviews": {"required_approving_review_count": 0},
                                  "required_status_checks": {"strict": True, "contexts": ["check"], "checks": [{"context": "check", "app_id": 7}]}}
        self.remote_url = REMOTE_URL
        self.root = self.directory / "clone"
        # Real Git resolves the GitHub-looking identity to this fixture's bare
        # remote. The adapter cannot read or mutate an operator repository.
        self.operator = self.directory / "operator"
        for clone in (self.root, self.operator):
            shutil.copytree(template / clone.name, clone, symlinks=True)
            # A copy has new inodes and ctimes; refresh the index so plumbing
            # that skips the refresh sees the clean tree the clone left.
            _git(clone, "update-index", "-q", "--refresh")
        (self.operator / "operator.txt").write_text("uncommitted operator work")
        self.home = self.directory / "home"
        self.database = self.home / ".local/share/sd/sd.db"
        initialise(self.database)
        self.connection = connect(self.database)
        self.addCleanup(self.connection.close)
        upsert_repo(self.connection, str(self.operator), remote=self.remote_url, runner_merge="auto",
                    managed=1)
        self.item = create_item(self.connection, kind="work", title="fixture work", status="in_progress", repo=str(self.operator), branch="topic")
        self.double = ShipDouble(self.remote)
        self.double.__enter__()
        self.addCleanup(self.double.close)
        self.programs = self.directory / "programs"
        self.programs.mkdir()
        (self.programs / "gh").write_text(SHIM)
        (self.programs / "gh").chmod(0o755)
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        transport = self.programs / "git"
        transport.write_text(git_transport(real_git, self.remote.path, self.remote_url))
        transport.chmod(0o755)
        provider = self.programs / "review-fixture"
        provider.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps({'type':'result','subtype':'success','structured_output':{'findings':[]}}))\n")
        provider.chmod(0o755)
        registry = self.database.parent / "providers.yaml"
        registry.write_text(f"""bills:
  fixture: {{ cost: subscription }}
providers:
  reviewer: {{ start: '{provider}', vendor: secondvendor, bill: fixture, roles: [reviewer], reader: claude-json }}
  reviewer2: {{ start: '{provider}', vendor: thirdvendor, bill: fixture, roles: [reviewer], reader: claude-json }}
  author: {{ start: 'unused-author', vendor: firstvendor, bill: fixture, roles: [author], reader: claude-json }}
roles:
  author: [author]
  reviewer: [reviewer, reviewer2]
""")
        import sd_registry
        allowed = ", ".join(str(sd_registry.recipient(sd_registry.read_file(registry).providers[name])) for name in ("reviewer", "reviewer2"))
        local = self.root / "CLAUDE.local.md"
        local.write_text("<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\nmode: full\n"
                         f"reviewers: {allowed}\n<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n")
        with (self.root / ".git/info/exclude").open("a") as stream:
            stream.write("\nCLAUDE.local.md\n")
        # `XDG_CONFIG_HOME` wins over `HOME` in `sd_lib.machine_config_path`, and
        # a GitHub runner sets it: pointed at the fixture here, or a machine
        # config written under `self.home` is never read (sd:1328).
        # Both Jev stages off: with a keyed `jev` on PATH every `sd-review`
        # child asked the live endpoint, about 0.85 s a prepare, and a CI
        # runner has no `jev`, so the fixture now answers the same everywhere
        # (sd:1605).
        # The body lint fails with no privacy-pattern file (sd:3490), so the
        # fixture holds its own and never reads the operator's.
        patterns = self.home / ".config/system/privacy-patterns"
        patterns.parent.mkdir(parents=True, exist_ok=True)
        patterns.write_text("secret-host\\.example\\.test\n", encoding="utf-8")
        self.environment = {**os.environ, "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
                            "SYSTEM_TOOLS_CONFIG": str(patterns.parent),
                            "JEV_SD_DOCS_LINT": "0", "JEV_SD_REVIEW_TRIAGE": "0",
                            "SHIP_DOUBLE": self.double.base_url,
                            "PATH": str(self.programs) + os.pathsep + os.environ["PATH"]}
        self.patch = patch.dict(os.environ, self.environment, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def args(self, command="prepare", *extra):
        return ship.parser().parse_args([command, "--item", str(self.item), "--json", *extra])

    def operation(self, command="prepare", *extra):
        operation = ship.Ship(self.root, self.connection, self.database, self.args(command, *extra))
        # A first prepare must say whether it delivers (sd:1928). Most cases
        # here are about something else, so they take the associate-only
        # answer unless they give one; `unanswered` below asks without it.
        if command == "prepare" and operation.args.deliver is None and "deliver" not in operation.state:
            operation.args.deliver = False
        # A branch of two or more commits needs a title (sd:2097). Cases that
        # add commits are about something else, so they take the fixture
        # commit's subject; `untitled` cases exercise the default itself.
        if (command == "prepare" and operation.args.title is None and "title" not in operation.state
                and not getattr(self, "untitled", False)):
            operation.args.title = "change"
        return operation

    def prepare(self, *extra):
        return self.operation("prepare", *extra).prepare()

    def merge(self, *extra):
        return self.operation("merge", "--manual", "--expected-head", _git(self.root, "rev-parse", "HEAD"), *extra).merge()

    def declare_review(self, policy):
        """`policy` as the fixture repository's review declaration, on its row (sd:5109)."""
        set_declaration(self.connection, str(self.operator), "review", policy)

    def enable_automatic_copilot(self):
        self.declare_review({"sensitive": ["src.py"], "copilot_review": {"automatic_deep": True}})

    def disable_automatic_copilot(self):
        """The same declaration, opting the repository out of paid Copilot review.

        `sensitive` still escalates `src.py` to the deep tier, so the tier is
        never what stops the request in the test below -- the repository's own
        word is.
        """
        self.declare_review({"sensitive": ["src.py"], "copilot_review": {"automatic_deep": False}})

    def cli(self, command, *extra):
        return subprocess.run([sys.executable, str(ROOT / "bin/sd-ship"), command, "--item", str(self.item), "--json", *extra],
                              cwd=self.root, env=self.environment, text=True, capture_output=True, timeout=CLI_TIMEOUT)


    # -- criterion 21: a ship leaves `docs/work/archive/` untouched ---------
    #
    # The criterion's other clauses are greps: no deletion verb outside a
    # frozen set, no sweep or park code path. Those say the pack has no way
    # to touch the archive. These run the pack and look, which is the
    # different question -- a grep over the source cannot see a path reached
    # through a helper, nor one reached by a tool the pack shells out to.
    #
    # `sd-plan` is the criterion's other named command and has no
    # executable; it is `contrib/sd-plan/SKILL.md` (retired, pack sd:7368). Its own words are the
    # assertion for that half, checked below.

    def archive_digest(self) -> dict[str, str]:
        """Every file under the archive, by path, as a content hash.

        Hashes and not mtimes: a rewrite that restores the same bytes is not
        a touch worth failing, and a mtime comparison would call it one on
        every checkout.
        """
        root = self.root / "docs" / "work" / "archive"
        return {
            str(path.relative_to(self.root)):
                hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()
        }

    def seed_archive(self) -> dict[str, str]:
        buried = self.root / "docs" / "work" / "archive" / "2026-01" / "2026-01-02-shipped"
        buried.mkdir(parents=True)
        (buried / "prd.md").write_text(
            "---\ntitle: a shipped thing\ncreated: 2026-01-02\n"
            "status: done\n---\n\n# shipped\n", encoding="utf-8")
        (buried / "implement.md").write_text("done\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        # The trailer is not decoration: `sd-review` refuses to plan a review
        # of a commit that does not say who wrote it, and the refusal surfaces
        # here as an invalid timing plan rather than as anything about the
        # archive. Seeding without it tests the trailer rule, not criterion 21.
        _git(self.root, "commit", "-q", "-m",
             "archive a done item\n\nAuthored-with: human")
        return self.archive_digest()

    def test_a_prepare_leaves_every_archived_file_byte_identical(self) -> None:
        before = self.seed_archive()
        self.assertEqual(len(before), 2, "the fixture archive did not get written")
        self.prepare()
        self.assertEqual(self.archive_digest(), before)

    def test_a_prepare_stages_and_commits_nothing_under_the_archive(self) -> None:
        """The digest above would miss a file added and then removed again.

        `git status` over the one path is the check that sees the whole
        working tree rather than the files that happen to exist at the end.
        """
        self.seed_archive()
        head = _git(self.root, "rev-parse", "HEAD")
        self.prepare()
        self.assertEqual(
            _git(self.root, "status", "--porcelain", "--", "docs/work/archive").strip(), "")
        self.assertEqual(
            _git(self.root, "diff", "--name-only", head, "HEAD",
                 "--", "docs/work/archive").strip(), "")

    def test_the_plan_skill_still_disclaims_every_archive_step(self) -> None:
        page = (ROOT / "contrib" / "sd-plan" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("There is no automatic archive, parking or sweep step.", page)

    def test_custom_database_controls_ship_receipts_and_the_actual_reviewer(self):
        from sd_db import set_provider_state
        from sd_db.registry import read, seed
        custom = self.directory / "custom.db"
        alternate = sqlite3.connect(custom)
        self.connection.backup(alternate)
        alternate.close()
        connection = connect(custom)
        self.addCleanup(connection.close)
        seed(connection, read(self.database.with_name("providers.yaml")))
        set_provider_state(connection, "reviewer", enabled=False)
        set_provider_state(connection, "reviewer2", enabled=False)
        result = self.cli("prepare", "--database", str(custom))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("no provider pass was reserved", json.loads(result.stdout)["error"])
        key = receipts.receipt_key(self.remote.slug, "topic", self.item)
        state = receipts.read(connection, key)[1]
        self.assertFalse(state.get("passes"))
        self.assertEqual(state["review_preflight_error"]["kind"], "invalid_timing_plan")
        self.assertEqual(receipts.read(self.connection, key), (0, {}))

    def test_reprepare_preserves_whole_item_acceptance_and_reviewable_description(self):
        acceptance = self.directory / "acceptance.json"
        acceptance.write_text(json.dumps({"item": self.item, "complete": True, "criteria": [
            {"criterion": "actual scope", "passed": True, "evidence": "fixture-check-pass"}]}))
        body = self.directory / "body.md"
        body.write_text("Concrete change, behavior and validation.")
        self.prepare("--deliver", "--acceptance-file", str(acceptance), "--body-file", str(body))
        before = self.operation().state
        _git(self.root, "commit", "--allow-empty", "-m", "verified integration\n\nAuthored-with: human")
        self.prepare()
        after = self.operation().state
        self.assertTrue(after["deliver"])
        self.assertEqual(after["acceptance"], before["acceptance"])
        self.assertEqual(after["body"], before["body"])
        self.assertEqual(len(after["passes"]), 2)

    def test_a_branch_carrying_a_squashed_head_is_told_to_resolve_by_hand(self):
        """sd:1409. A squash leaves the merged head off main's history, so a
        branch built on that head conflicts on ancestry when it merges main.
        The warning names the carried head and the squash's paths, and calls
        none of them safe for `--ours`: entry equality cannot prove that
        (`tests/test_sd_ship_squash.py` holds the histories that break it)."""
        self.prepare()
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")
        tip = _git(self.root, "rev-parse", "HEAD")
        self.remote.commit_on("main", "later main work\n\nAuthored-with: human", files={"src.py": "value = 3\n"})
        _git(self.root, "checkout", "-q", "-b", "second")
        (self.root / "more.py").write_text("more = 1\n")
        _git(self.root, "add", "more.py")
        _git(self.root, "commit", "-qm", "build on the first\n\nAuthored-with: human")
        second = create_item(self.connection, kind="work", title="second", status="in_progress",
                             repo=str(self.operator), branch="second")

        def operation():
            args = ship.parser().parse_args(["prepare", "--item", str(second), "--associate-only", "--json",
                                             "--title", "build on the first"])
            return ship.Ship(self.root, self.connection, self.database, args)

        # sd:1346 refuses the branch until it merges main, and that merge is
        # where the ancestry conflict lands, so the refusal names the remedy.
        with self.assertRaisesRegex(ship.Refusal, "behind the current default branch") as caught:
            operation().prepare()
        self.assertIn(tip, str(caught.exception))
        self.assertIn("No path is proven safe for `git checkout --ours`", str(caught.exception))
        self.assertRegex(str(caught.exception), r"The squash changed: [^()]*\bsrc\.py\b")
        _git(self.root, "merge", "-q", "--no-ff", "-X", "ours", "-m",
             "Merge origin/main into second\n\nAuthored-with: human", "origin/main")
        operation().prepare()
        warnings = operation().state.get("warnings") or []
        notes = [warning for warning in warnings if tip in warning]
        self.assertEqual(len(notes), 1, warnings)
        self.assertIn("No path is proven safe for `git checkout --ours`", notes[0])
        self.assertRegex(notes[0], r"The squash changed: [^()]*\bsrc\.py\b")

    def test_a_merge_forward_before_the_first_prepare_does_not_become_the_title(self):
        # sd:1377: the merge-forward sd-ship demands left HEAD's subject naming
        # a branch operation, and it was stored as the title and landed on main.
        self.untitled = True
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"other.txt": "main\n"})
        _git(self.root, "fetch", "-q", "origin", "main")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge origin/main into topic\n\nAuthored-with: human", "FETCH_HEAD")
        result = self.prepare()
        self.assertEqual(self.operation().state["title"], "change")
        self.assertEqual(self.remote.pull(result["pull_request"]["number"]).title, "change")

    def test_one_commit_without_a_title_uses_its_subject(self):
        # sd:2097: a one-commit branch keeps the default; the subject is the change.
        self.untitled = True
        self.assertEqual(_git(self.root, "rev-list", "--count", "--no-merges", "origin/main..HEAD"), "1")
        result = self.prepare()
        self.assertEqual(self.operation().state["title"], "change")
        self.assertEqual(self.remote.pull(result["pull_request"]["number"]).title, "change")

    def test_several_commits_without_a_title_are_refused(self):
        # sd:2097: the newest subject of an 11-commit branch became the PR
        # title (ui-design PR #16) and described only the last commit.
        self.untitled = True
        _git(self.root, "commit", "--allow-empty", "-qm", "fix a typo\n\nAuthored-with: human")
        with patch.object(ship.Ship, "review") as review:
            with self.assertRaisesRegex(ship.Refusal, r'provide --title; the branch has 2 commits, '
                                                      r'and the newest subject \("fix a typo"\) may describe only the last'):
                self.prepare()
        review.assert_not_called()
        self.assertIsNone(self.operation().state.get("pull_request"))

    def test_several_commits_with_a_title_use_it(self):
        _git(self.root, "commit", "--allow-empty", "-qm", "fix a typo\n\nAuthored-with: human")
        result = self.prepare("--title", "Switch the widget to local CI")
        self.assertEqual(self.operation().state["title"], "Switch the widget to local CI")
        self.assertEqual(self.remote.pull(result["pull_request"]["number"]).title, "Switch the widget to local CI")

    def test_the_squash_subject_is_the_pull_requests_title(self):
        # sd:1876: prepare stored the newest commit subject, and the merge
        # used it even when the pull request carried a different title.
        number = self.prepare()["pull_request"]["number"]
        self.remote.pull(number).title = "Switch the widget to local CI"
        self.assertEqual(self.merge()["phase"], "merged")
        self.assertEqual(self.remote.pull(number).title.split("\n")[0], f"Switch the widget to local CI (#{number})")

    def test_a_title_matching_the_retitled_pull_request_is_accepted_and_stored(self):
        # sd:1378: the guard compared a new --title with the stale stored one
        # and refused the very title the pull request carried on GitHub.
        number = self.prepare()["pull_request"]["number"]
        self.remote.pull(number).title = "Retitled on GitHub"
        (self.root / "src.py").write_text("value = 2\n")
        _git(self.root, "commit", "-qam", "follow-up\n\nAuthored-with: human")
        with patch.object(ship.Ship, "review"), patch.object(ship.Ship, "check_review"):
            with self.assertRaisesRegex(ship.Refusal, r'the title "Elsewhere" differs from the stored "change" '
                                                      r'and the pull request\'s "Retitled on GitHub"'):
                self.prepare("--title", "Elsewhere")
            self.assertEqual(self.prepare("--title", "Retitled on GitHub")["phase"], "ready_to_send")
        self.assertEqual(self.operation().state["title"], "Retitled on GitHub")

    def test_a_wip_pull_request_title_never_reaches_the_default_branch(self):
        number = self.prepare()["pull_request"]["number"]
        self.remote.pull(number).title = "WIP switch the widget"
        with self.assertRaisesRegex(ship.Refusal, "title starts with wip"):
            self.merge()
        self.assertFalse(any(call.method == "PUT" for call in self.remote.calls))

    def test_a_branch_behind_the_default_branch_is_refused_before_the_review(self):
        # sd:1346, sd:1367: merge refuses a branch behind the default branch,
        # so prepare refused nothing, spent the review and answered
        # ready_to_send. It now refuses first and spends nothing.
        first = self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"other.txt": "main\n"})
        second = self.remote.commit_on("main", "more main work\n\nAuthored-with: human", files={"other.txt": "more\n"})
        with patch.object(ship.Ship, "review") as review:
            with self.assertRaisesRegex(ship.Refusal, "behind the current default branch") as caught:
                self.prepare()
        review.assert_not_called()
        # sd:2339: the refusal names what the branch lacks, oldest first.
        self.assertIn(f"origin/main by 2 commits ({first[:12]}, {second[:12]})", str(caught.exception))
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "base_moved")
        self.assertIn("git merge origin/main", caught.exception.workflow["next_action"])
        self.assertIn("--catch-up", caught.exception.workflow["next_action"])
        self.assertIsNone(self.operation().state.get("pull_request"))
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge origin/main into topic\n\nAuthored-with: human", "origin/main")
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        self.assertEqual(self.merge()["phase"], "merged")

    def shared_context(self) -> str:
        """The branch edits a file main shares, then is reviewed; the reviewed head.

        Line 1 is the branch's and line 3 main's next edit: the two merge
        cleanly, but line 3 sits in the branch's diff context, so the merge
        moves the branch's own patch-id (sd:1485).
        """
        self.remote.commit_on("main", "shared\n\nAuthored-with: human", files={"shared.txt": "a\nb\nc\nd\ne\n"})
        _git(self.root, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge origin/main into topic\n\nAuthored-with: human", "origin/main")
        (self.root / "shared.txt").write_text("A\nb\nc\nd\ne\n")
        _git(self.root, "commit", "-qam", "edit shared\n\nAuthored-with: human")
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        self.remote.commit_on("main", "near the branch's hunk\n\nAuthored-with: human", files={"shared.txt": "a\nb\nC\nd\ne\n"})
        return _git(self.root, "rev-parse", "HEAD")

    def test_catch_up_merges_the_base_and_re_reviews_only_the_branchs_own_diff(self):
        # sd:2023: a branch left behind by another landing needed a hand merge
        # of origin/main, a push, a review and a prepare. `--catch-up` merges
        # (no rebase, no force push) and the new head's pass is a full-branch
        # one, so it reviews merge-base..head and not the code main brought.
        # sd:1485: main's change here moves the branch's own patch-id, so the
        # earlier review is not carried forward.
        before = self.shared_context()
        self.assertEqual(self.prepare("--catch-up")["phase"], "ready_to_send")
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertEqual(_git(self.root, "rev-list", "--parents", "-n", "1", head).split()[1:],
                         [before, _git(self.root, "rev-parse", "refs/remotes/origin/main")])
        self.assertEqual(_git(self.remote.path, "rev-parse", "refs/heads/topic"), head)
        fresh = self.operation().state["passes"][-1]
        self.assertEqual(fresh["head"], head)
        self.assertIsNone(fresh["base"], "a fix verification from the old head reviews main's commits too")
        self.assertEqual(fresh["catch_up"]["from"], before)
        self.assertNotIn("review_carry_forward", self.operation().state)
        # The merge gate runs at the new head or not at all.
        with self.assertRaisesRegex(ship.Refusal, "--expected-head must name the current exact reviewed commit"):
            self.operation("merge", "--manual", "--expected-head", before).merge()

    def test_a_clean_catch_up_carries_the_review_forward(self):
        # sd:1485: every catch-up used to spend a full-branch pass on a branch
        # whose own change the merge did not touch. The reviewed head is an
        # ancestor, the merge is git's own clean result, and the branch's
        # patch-id against the base is the one the review read.
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        before = _git(self.root, "rev-parse", "HEAD")
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        result = self.prepare("--catch-up")
        self.assertEqual(result["phase"], "ready_to_send")
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertNotEqual(head, before)
        state = self.operation().state
        self.assertEqual([entry["head"] for entry in state["passes"]], [before], "no pass is spent")
        self.assertEqual(state["reviewed_head"], head)
        carried = state["review_carry_forward"]
        self.assertEqual((carried["from"], carried["to"]), (before, head))
        self.assertEqual(carried["merges"], [head])
        self.assertRegex(carried["patch_id"], r"^[0-9a-f]{40}$")
        self.assertEqual(result["review_carry_forward"], carried)
        pull = self.remote.pull(result["pull_request"]["number"])
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        self.assertEqual(self.merge()["phase"], "merged")

    def test_a_hand_merge_of_the_base_is_carried_like_a_catch_up(self):
        # A prepare that failed after its merge, or the merge sd-ship used to
        # ask for by hand, leaves the same history; the carry follows it.
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        before = _git(self.root, "rev-parse", "HEAD")
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        _git(self.root, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge origin/main into topic\n\nAuthored-with: human", "origin/main")
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        state = self.operation().state
        self.assertEqual(len(state["passes"]), 1)
        self.assertEqual(state["review_carry_forward"]["from"], before)

    def test_merge_after_a_hand_merge_of_the_base_names_it(self):
        # sd:2339: a merge after a hand merge of main refused with "does not
        # name this exact head" alone, and an extra review round followed.
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        reviewed = _git(self.root, "rev-parse", "HEAD")
        landed = self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        _git(self.root, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge origin/main into topic\n\nAuthored-with: human", "origin/main")
        with self.assertRaisesRegex(ship.Refusal, "does not name this exact head") as caught:
            self.merge()
        self.assertIn(f"adds only a merge of origin/main, 1 commit ({landed[:12]}), to the reviewed {reviewed[:12]}",
                      str(caught.exception))
        self.assertIn("sd-ship prepare", str(caught.exception))
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        self.assertEqual(self.operation().state["review_carry_forward"]["from"], reviewed)

    def test_merge_after_an_ordinary_commit_names_no_base_merge(self):
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        _git(self.root, "commit", "--allow-empty", "-qm", "later\n\nAuthored-with: human")
        with self.assertRaisesRegex(ship.Refusal, "does not name this exact head") as caught:
            self.merge()
        self.assertNotIn("origin/main", str(caught.exception))

    def test_a_hand_merge_of_the_base_is_scoped_like_a_catch_up(self):
        before = self.shared_context()
        _git(self.root, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge origin/main into topic\n\nAuthored-with: human", "origin/main")
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        fresh = self.operation().state["passes"][-1]
        self.assertIsNone(fresh["base"])
        self.assertEqual(fresh["catch_up"]["from"], before)

    def test_a_merge_with_content_of_its_own_is_not_carried(self):
        # An edit folded into the merge commit is not git's merge result, so
        # nothing reviewed it, whatever the patch-id says: `--stable` ignores
        # whitespace, and this one breaks the file.
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        before = _git(self.root, "rev-parse", "HEAD")
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        _git(self.root, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
        _git(self.root, "merge", "-q", "--no-ff", "--no-commit", "origin/main")
        (self.root / "src.py").write_text("    value = 1\n")
        _git(self.root, "commit", "-qam", "Merge origin/main into topic\n\nAuthored-with: human")
        operation = self.operation()
        base = "refs/remotes/origin/main"
        self.assertEqual(sd_ship_review.own_patch_id(self.root, before, base),
                         sd_ship_review.own_patch_id(self.root, "HEAD", base),
                         "the premise: patch-id cannot see a whitespace edit")
        self.assertEqual(operation.prepare()["phase"], "ready_to_send")
        state = self.operation().state
        self.assertNotIn("review_carry_forward", state)
        self.assertEqual(state["passes"][-1]["catch_up"]["from"], before)

    def test_a_commit_after_the_merge_is_not_carried(self):
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        _git(self.root, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge origin/main into topic\n\nAuthored-with: human", "origin/main")
        _git(self.root, "commit", "--allow-empty", "-qm", "more\n\nAuthored-with: human")
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        state = self.operation().state
        self.assertNotIn("review_carry_forward", state)
        self.assertEqual(len(state["passes"]), 2)

    def test_a_base_change_beside_the_branchs_files_is_not_carried(self):
        # sd:1485 review: an unchanged patch-id does not show the branch still
        # works on the new base -- main can change a callee the branch calls
        # from another file. Main's commit here shares the branch's directory,
        # so the branch is reviewed again although its patch-id is unchanged.
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        before = _git(self.root, "rev-parse", "HEAD")
        self.remote.commit_on("main", "change the callee\n\nAuthored-with: human", files={"callee.py": "def value():\n    return 2\n"})
        _git(self.root, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge origin/main into topic\n\nAuthored-with: human", "origin/main")
        base = "refs/remotes/origin/main"
        self.assertEqual(sd_ship_review.own_patch_id(self.root, before, base),
                         sd_ship_review.own_patch_id(self.root, "HEAD", base),
                         "the premise: the branch's own patch-id is unchanged")
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        state = self.operation().state
        self.assertNotIn("review_carry_forward", state)
        self.assertEqual(state["passes"][-1]["catch_up"]["from"], before)

    def other_reviewer(self) -> str:
        first = self.operation().state["passes"][0]["report"]["reviewed_by"]
        return "reviewer" if first == ["reviewer2"] else "reviewer2"

    def test_a_named_reviewer_the_receipt_lacks_is_not_carried(self):
        # sd:1485 review: a catch-up naming another reviewer reused the
        # receipt of the first; the selection is honoured instead.
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        other = self.other_reviewer()
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        self.assertEqual(self.prepare("--catch-up", "--provider", other)["phase"], "ready_to_send")
        state = self.operation().state
        self.assertNotIn("review_carry_forward", state)
        self.assertEqual(state["passes"][-1]["report"]["reviewed_by"], [other])

    def test_a_carried_head_refuses_a_named_reviewer_the_receipt_lacks(self):
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        other = self.other_reviewer()
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        self.assertEqual(self.prepare("--catch-up")["phase"], "ready_to_send")
        self.assertIn("review_carry_forward", self.operation().state)
        with self.assertRaisesRegex(ship.Refusal, "does not match the requested reviewer") as caught:
            self.prepare("--provider", other)
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "review_provider_mismatch")

    def test_a_fix_after_a_review_stays_a_fix_verification(self):
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        before = _git(self.root, "rev-parse", "HEAD")
        (self.root / "src.py").write_text("value = 3\n")
        _git(self.root, "commit", "-q", "-am", "fix\n\nAuthored-with: human")
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        fresh = self.operation().state["passes"][-1]
        self.assertEqual(fresh["base"], before)
        self.assertNotIn("catch_up", fresh)

    def test_a_catch_up_before_any_review_is_an_ordinary_first_pass(self):
        # Nothing was reviewed, so there is no history for the pass to cover.
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"other.txt": "main\n"})
        self.assertEqual(self.prepare("--catch-up")["phase"], "ready_to_send")
        [first] = self.operation().state["passes"]
        self.assertNotIn("catch_up", first)
        self.assertEqual(self.merge()["phase"], "merged")

    def test_a_catch_up_that_conflicts_leaves_the_branch_as_it_was(self):
        before = _git(self.root, "rev-parse", "HEAD")
        self.remote.commit_on("main", "rival\n\nAuthored-with: human", files={"src.py": "value = 2\n"})
        with patch.object(ship.Ship, "review") as review:
            with self.assertRaisesRegex(ship.Refusal, "conflicts") as caught:
                self.prepare("--catch-up")
        review.assert_not_called()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "merge_conflict")
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), before)
        self.assertFalse((self.root / ".git/MERGE_HEAD").exists())
        self.assertEqual(_git(self.root, "status", "--porcelain", "--untracked-files=no"), "")

    def blocking_review(self):
        """The fixture's reviewer answers one high finding from now on: (program, payload)."""
        program = self.programs / "review-fixture"
        payload = {"type": "result", "subtype": "success", "structured_output": {"findings": [
            {"path": "src.py", "line": 1, "severity": "high", "family": "correctness", "summary": "value is wrong"}]}}
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        return program, payload

    def blocked_lane_catch_up(self):
        """A lane prepare's catch-up merge reviewed blocking, then the lane worktree gone: (reviewed, program, payload).

        The lane prepares with `--catch-up` in a worktree of its own and drops
        it after the failure, so the reviewed merge is on no branch, and the
        branch is back at the merge's first parent (sd:4925).
        """
        program, payload = self.blocking_review()
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        with self.assertRaisesRegex(ship.Refusal, "local review blocking"):
            self.prepare("--catch-up")
        reviewed = self.operation().state["passes"][0]["head"]
        self.assertEqual(len(_git(self.root, "rev-list", "--parents", "-n", "1", reviewed).split()), 3, "the premise: a merge")
        _git(self.root, "reset", "-q", "--hard", f"{reviewed}^1")
        return reviewed, program, payload

    def fix_and_clear(self, program, payload):
        (self.root / "src.py").write_text("value = 2\n")
        _git(self.root, "commit", "-qam", "correct value\n\nAuthored-with: human")
        payload["structured_output"]["findings"] = []
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")

    def test_a_fix_after_a_reviewed_lane_catch_up_verifies_only_the_fix(self):
        # sd:4925, live on sd:4853 and sd:4920: the retry merged main again
        # into the fixed branch, so the reviewed merge was no ancestor and
        # prepare refused reviewed_head_orphaned on a branch that only added a
        # commit. The catch-up now merges the reviewed merge back in first.
        reviewed, program, payload = self.blocked_lane_catch_up()
        self.fix_and_clear(program, payload)
        self.assertEqual(self.prepare("--catch-up")["phase"], "ready_to_send")
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertEqual(_git(self.root, "merge-base", reviewed, head), reviewed)
        state = self.operation().state
        self.assertEqual(len(state["passes"]), 2)
        fresh = state["passes"][-1]
        self.assertEqual(fresh["base"], reviewed, "a fix verification of what changed since the reviewed merge")
        self.assertEqual(fresh["report"]["subject"]["base"], reviewed)
        self.assertNotIn("catch_up", fresh)
        self.assertEqual(self.remote.rev_parse("topic"), head)

    def test_a_fix_after_a_reviewed_lane_catch_up_with_main_moved_again_is_scoped_as_a_catch_up(self):
        reviewed, program, payload = self.blocked_lane_catch_up()
        self.remote.commit_on("main", "more main work\n\nAuthored-with: human", files={"lib/more.txt": "main\n"})
        self.fix_and_clear(program, payload)
        self.assertEqual(self.prepare("--catch-up")["phase"], "ready_to_send")
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertEqual(_git(self.root, "merge-base", reviewed, head), reviewed)
        self.assertEqual(_git(self.root, "merge-base", "refs/remotes/origin/main", head),
                         _git(self.root, "rev-parse", "refs/remotes/origin/main"))
        state = self.operation().state
        self.assertEqual(len(state["passes"]), 2, "no restart: the blocking pass stays in the history")
        self.assertNotIn("superseded_reviews", state)
        self.assertEqual(state["passes"][-1]["catch_up"]["from"], reviewed)

    def test_a_clean_reviewed_lane_catch_up_carries_forward_when_main_moves(self):
        # The same orphan follows a clean review whose lane run failed later.
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        with patch.object(ship.Ship, "commit_warnings", side_effect=ship.Refusal("the lane run failed later")):
            with self.assertRaisesRegex(ship.Refusal, "failed later"):
                self.prepare("--catch-up")
        reviewed = self.operation().state["passes"][0]["head"]
        _git(self.root, "reset", "-q", "--hard", f"{reviewed}^1")
        self.remote.commit_on("main", "more main work\n\nAuthored-with: human", files={"lib/more.txt": "main\n"})
        self.assertEqual(self.prepare("--catch-up")["phase"], "ready_to_send")
        state = self.operation().state
        self.assertEqual([entry["head"] for entry in state["passes"]], [reviewed], "no pass is spent")
        self.assertEqual(state["review_carry_forward"]["from"], reviewed)

    def assert_still_orphaned(self, reviewed):
        with self.assertRaises(ship.Refusal) as caught:
            self.prepare("--catch-up")
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "reviewed_head_orphaned")
        self.assertIn(reviewed, str(caught.exception))
        self.assertEqual(len(self.operation().state["passes"]), 1)

    def test_an_amend_after_a_reviewed_lane_catch_up_still_refuses(self):
        reviewed, _program, _payload = self.blocked_lane_catch_up()
        (self.root / "src.py").write_text("value = 2\n")
        _git(self.root, "commit", "-q", "--amend", "-a", "--no-edit")
        self.assert_still_orphaned(reviewed)

    def test_a_rebase_after_a_reviewed_lane_catch_up_still_refuses(self):
        reviewed, program, payload = self.blocked_lane_catch_up()
        _git(self.root, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
        _git(self.root, "rebase", "-q", "refs/remotes/origin/main")
        self.fix_and_clear(program, payload)
        self.assert_still_orphaned(reviewed)

    def test_a_reviewed_merge_of_a_side_branch_still_refuses(self):
        # The second parent must be on the default branch: a merge of anything
        # else is not a catch-up, and its side commit is scope of its own.
        program, payload = self.blocking_review()
        before = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "checkout", "-q", "-b", "side")
        (self.root / "side.txt").write_text("side\n")
        _git(self.root, "add", "side.txt")
        _git(self.root, "commit", "-qm", "side work\n\nAuthored-with: human")
        _git(self.root, "checkout", "-q", "topic")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge side into topic\n\nAuthored-with: human", "side")
        with self.assertRaisesRegex(ship.Refusal, "local review blocking"):
            self.prepare("--catch-up")
        reviewed = self.operation().state["passes"][0]["head"]
        _git(self.root, "reset", "-q", "--hard", before)
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        self.fix_and_clear(program, payload)
        self.assert_still_orphaned(reviewed)

    def test_a_reviewed_catch_up_with_an_edit_of_its_own_still_refuses(self):
        # Merging it back would bring in an edit no later head carries.
        program, payload = self.blocking_review()
        before = _git(self.root, "rev-parse", "HEAD")
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        _git(self.root, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
        _git(self.root, "merge", "-q", "--no-ff", "--no-commit", "refs/remotes/origin/main")
        (self.root / "extra.txt").write_text("folded into the merge\n")
        _git(self.root, "add", "extra.txt")
        _git(self.root, "commit", "-qm", "Merge origin/main into topic\n\nAuthored-with: human")
        with self.assertRaisesRegex(ship.Refusal, "local review blocking"):
            self.prepare("--catch-up")
        reviewed = self.operation().state["passes"][0]["head"]
        _git(self.root, "reset", "-q", "--hard", before)
        self.fix_and_clear(program, payload)
        self.assert_still_orphaned(reviewed)

    def test_an_amended_reviewed_catch_up_merge_still_refuses(self):
        # Codex round 1: the amend keeps the merge's parents, so they still
        # reach the branch. Merging the old merge back would publish what the
        # amend removed (sd:2600); a replacement with its parents refuses.
        program, payload = self.blocking_review()
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        with self.assertRaisesRegex(ship.Refusal, "local review blocking"):
            self.prepare("--catch-up")
        reviewed = self.operation().state["passes"][0]["head"]
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), reviewed)
        _git(self.root, "commit", "-q", "--amend", "-m", "Merge origin/main, message amended\n\nAuthored-with: human")
        self.fix_and_clear(program, payload)
        self.assert_still_orphaned(reviewed)

    def test_a_reviewed_side_merge_whose_side_commit_the_branch_kept_still_refuses(self):
        # Codex round 1: the side commit reaches the branch through a new merge
        # of it, but it is not on the default branch and not a catch-up merge.
        program, payload = self.blocking_review()
        before = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "checkout", "-q", "-b", "side")
        (self.root / "side.txt").write_text("side\n")
        _git(self.root, "add", "side.txt")
        _git(self.root, "commit", "-qm", "side work\n\nAuthored-with: human")
        _git(self.root, "checkout", "-q", "topic")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge side into topic\n\nAuthored-with: human", "side")
        with self.assertRaisesRegex(ship.Refusal, "local review blocking"):
            self.prepare("--catch-up")
        reviewed = self.operation().state["passes"][0]["head"]
        _git(self.root, "reset", "-q", "--hard", before)
        self.fix_and_clear(program, payload)
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge side into topic again\n\nAuthored-with: human", "side")
        self.assert_still_orphaned(reviewed)

    def test_a_reviewed_lane_catch_up_that_conflicts_with_the_fix_leaves_the_branch_as_it_was(self):
        reviewed, _program, _payload = self.blocked_lane_catch_up()
        (self.root / "lib").mkdir(exist_ok=True)
        (self.root / "lib/other.txt").write_text("rival\n")
        _git(self.root, "add", "lib/other.txt")
        _git(self.root, "commit", "-qm", "rival\n\nAuthored-with: human")
        before = _git(self.root, "rev-parse", "HEAD")
        with patch.object(ship.Ship, "review") as review:
            with self.assertRaisesRegex(ship.Refusal, "conflicts") as caught:
                self.prepare("--catch-up")
        review.assert_not_called()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "merge_conflict")
        self.assertIn(reviewed, str(caught.exception))
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), before)
        self.assertFalse((self.root / ".git/MERGE_HEAD").exists())
        self.assertEqual(_git(self.root, "status", "--porcelain", "--untracked-files=no"), "")

    def test_a_base_merge_that_conflicts_after_a_merge_back_leaves_the_branch_as_it_was(self):
        # The merge back went in, the base merge after it did not: both go.
        _reviewed, program, payload = self.blocked_lane_catch_up()
        self.fix_and_clear(program, payload)
        before = _git(self.root, "rev-parse", "HEAD")
        self.remote.commit_on("main", "rival\n\nAuthored-with: human", files={"src.py": "value = 4\n"})
        with patch.object(ship.Ship, "review") as review:
            with self.assertRaisesRegex(ship.Refusal, "merging origin/main into topic conflicts") as caught:
                self.prepare("--catch-up")
        review.assert_not_called()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "merge_conflict")
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), before)
        self.assertFalse((self.root / ".git/MERGE_HEAD").exists())
        self.assertEqual(_git(self.root, "status", "--porcelain", "--untracked-files=no"), "")

    def test_a_prepare_stopped_after_the_merge_back_finishes_on_the_next_run(self):
        # Killed between the merge back and the review: the next run finds the
        # reviewed merge in the branch and goes on as a fix verification.
        reviewed, program, payload = self.blocked_lane_catch_up()
        self.fix_and_clear(program, payload)
        _git(self.root, "merge", "-q", "--ff", "-m", "Merge reviewed\n\nAuthored-with: human", reviewed)
        stopped = _git(self.root, "rev-parse", "HEAD")
        self.assertEqual(self.prepare("--catch-up")["phase"], "ready_to_send")
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), stopped, "nothing left to merge")
        self.assertEqual(self.operation().state["passes"][-1]["base"], reviewed)

    def test_a_second_blocking_lane_round_after_a_merge_back_merges_back_again(self):
        # The second review covers the merge back itself, whose second parent
        # is the first reviewed merge, not the base; the lane drops it too.
        first, program, payload = self.blocked_lane_catch_up()
        (self.root / "src.py").write_text("value = 2\n")
        _git(self.root, "commit", "-qam", "first fix\n\nAuthored-with: human")
        with self.assertRaisesRegex(ship.Refusal, "local review blocking"):
            self.prepare("--catch-up")
        state = self.operation().state
        second = state["passes"][1]["head"]
        self.assertEqual(state["passes"][1]["base"], first)
        _git(self.root, "reset", "-q", "--hard", f"{second}^1")
        self.remote.commit_on("main", "more main work\n\nAuthored-with: human", files={"lib/more.txt": "main\n"})
        (self.root / "src.py").write_text("value = 5\n")
        _git(self.root, "commit", "-qam", "second fix\n\nAuthored-with: human")
        payload["structured_output"]["findings"] = []
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        self.assertEqual(self.prepare("--catch-up")["phase"], "ready_to_send")
        head = _git(self.root, "rev-parse", "HEAD")
        for reviewed in (first, second):
            self.assertEqual(_git(self.root, "merge-base", reviewed, head), reviewed)
        state = self.operation().state
        self.assertEqual(len(state["passes"]), 3)
        self.assertEqual(state["passes"][-1]["catch_up"]["from"], second)

    def test_a_moved_binding_re_reviews_the_same_head_instead_of_bricking_it(self):
        # sd:1390, live on #1140. Reuse was decided on head equality and the
        # receipt then rejected on the binding, with nothing between them, so
        # a branch that already contained the default branch -- and therefore
        # never needed a merge-forward to move its head -- had no verb left
        # once an unrelated landing touched a review tool file. Prepare and
        # merge both refused, `--retry-review` refused a completed review, and
        # the post-cap operator request refuses below the cap.
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        before = self.operation().state["passes"]
        self.assertEqual(len(before), 1)
        operation = self.operation()
        operation.state["binding"] = "a review tool file landed on the default branch"
        operation.save()
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        state = self.operation().state
        self.assertEqual(state["binding"], ship.binding(self.root))
        self.assertEqual(len(state["passes"]), 2, "the re-review spends one pass")
        fresh = state["passes"][-1]
        # A full-branch pass at the same head, not a fix verification of it:
        # `--base <previous head>` would name this head and review nothing.
        self.assertEqual(fresh["head"], before[0]["head"])
        self.assertIsNone(fresh["base"])
        self.assertFalse(fresh["retry"])
        self.assertEqual(fresh["review_binding_change"]["superseded_binding"],
                         "a review tool file landed on the default branch")
        self.assertEqual(fresh["report"]["resume_report_digest"],
                         ship.digest(ship.review_history(before)))
        # The receipt reads back through the same coverage walk that merge uses.
        self.assertEqual(self.merge()["phase"], "merged")

    def test_a_re_review_records_what_moved_and_stores_a_fresh_manifest(self):
        """sd:1834, sd:1246. The pass a moved binding spends says which file spent it."""
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        self.assertEqual(self.operation().state["binding_manifest"], ship.binding_manifest(self.root))
        for stored, changed in ((None, [["receipt predates the per-file manifest", "legacy"]]),
                                ("verdict", [["sd-review", "verdict"]])):
            with self.subTest(stored=stored):
                operation = self.operation()
                operation.state["binding"] = "a review tool file landed on the default branch"
                if stored is None:
                    operation.state.pop("binding_manifest")
                else:
                    operation.state["binding_manifest"]["verdict"]["sd-review"] = "ast:before"
                    # sd:1397. The reviewers would now be asked something else, so no replay keeps it.
                    operation.state["passes"][-1]["review_request"] = {"sha256": "another request", "verify": None, "resume": None}
                operation.save()
                self.assertEqual(self.prepare()["phase"], "ready_to_send")
                state = self.operation().state
                self.assertEqual(state["passes"][-1]["review_binding_change"]["changed"], changed)
                self.assertEqual(state["binding_manifest"], ship.binding_manifest(self.root))

    def test_a_landing_that_leaves_the_request_unchanged_keeps_the_receipt(self):
        """sd:1397, option E. A verdict file moved, but `sd-review --explain` replayed for the
        stored pass asks the reviewers the same thing, so the receipt stands and no pass is spent."""
        self.assertEqual(self.prepare()["phase"], "ready_to_send")

        def land():
            operation = self.operation()
            operation.state["binding"] = "a review tool file landed on the default branch"
            operation.state["binding_manifest"]["verdict"]["sd_route.py"] = "ast:before"
            operation.save()

        land()
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        state = self.operation().state
        self.assertEqual(len(state["passes"]), 1, "a kept receipt spends no pass")
        self.assertEqual(state["binding"], ship.binding(self.root))
        self.assertEqual(state["binding_manifest"], ship.binding_manifest(self.root))
        self.assertEqual(state["review_binding_kept"][0]["changed"], [["sd_route.py", "verdict"]])
        self.assertEqual(state["review_binding_kept"][0]["request_sha256"], state["passes"][0]["review_request"]["sha256"])
        land()
        self.assertEqual(self.merge()["phase"], "merged")

    def test_a_landing_that_moves_finding_parsing_re_reviews_though_the_request_is_unchanged(self):
        """sd:1397, operator ruling (option A). `sd-review` parses and disposes findings, so a
        landing that moves it re-reviews even when `--explain` would ask the reviewers the same."""
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        operation = self.operation()
        operation.state["binding"] = "a review tool file landed on the default branch"
        operation.state["binding_manifest"]["verdict"]["sd-review"] = "ast:before"
        operation.save()
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        state = self.operation().state
        self.assertEqual(len(state["passes"]), 2, "moved finding code spends a re-review")
        self.assertNotIn("review_binding_kept", state)
        self.assertEqual(state["passes"][-1]["review_binding_change"]["changed"], [["sd-review", "verdict"]])

    def test_prepare_rereads_a_pull_object_that_lags_the_push(self):
        # sd:1394, live on #1145 and system #572: the pull object still named
        # the pre-push head one second after the push, and one read refused.
        self.double.lagging_pull_reads = 2
        with patch.object(ship.time, "sleep") as sleep:
            result = self.prepare()
        self.assertEqual(result["phase"], "ready_to_send")
        self.assertEqual(self.operation().state["pull_request"]["number"], 1)
        self.assertEqual(self.double.lagging_pull_reads, 0)
        waits = pull_head_waits(sleep)
        self.assertEqual(waits, [ship.PULL_HEAD_INTERVAL_SECONDS, 2 * ship.PULL_HEAD_INTERVAL_SECONDS])

    def test_a_head_that_never_arrives_is_a_retryable_refusal(self):
        self.double.lagging_pull_reads = ship.PULL_HEAD_ATTEMPTS
        with patch.object(ship.time, "sleep") as sleep:
            with self.assertRaisesRegex(ship.Refusal, "PR head changed after push") as caught:
                self.prepare()
        self.assertIn(LAGGING_HEAD, str(caught.exception))
        waits = pull_head_waits(sleep)
        self.assertEqual(waits, [ship.PULL_HEAD_INTERVAL_SECONDS * n for n in range(1, ship.PULL_HEAD_ATTEMPTS)])
        workflow = caught.exception.workflow
        self.assertEqual(workflow["state"], "retryable_failure")
        self.assertTrue(workflow["blocker"]["retryable"])
        self.assertEqual(workflow["blocker"]["code"], "pull_head_mismatch")
        self.assertIsNone(self.operation().state.get("pull_request"))
        # The rerun adopts the PR the push already updated.
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.prepare()["phase"], "ready_to_send")

    def test_a_merged_pull_request_on_the_branch_name_is_not_bound(self):
        # sd:2656, live on sd:1912: the item's recorded branch had carried #1242,
        # merged on 2026-09-28. The lookup read closed pulls too, bound #1242,
        # and its frozen head refused the push as "PR head changed after push".
        merged = self.remote.open_pull_request("topic", base="main", title="Earlier work (sd:1912)", body="")
        merged.state, merged.merged_head = "MERGED", "b1e4a7c0" * 5
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.prepare()["phase"], "ready_to_send")
        number = self.operation().state["pull_request"]["number"]
        self.assertNotEqual(number, merged.number)
        self.assertEqual(self.remote.pull(number).state, "OPEN")
        self.assertEqual(merged.state, "MERGED")

    def test_prepare_hands_the_pull_request_body_to_the_docs_lint(self):
        # Rule 5 only runs with a body. The skill says the PR link is checked
        # locally in step 2, and until this test the call passed no body, so
        # the rule reported itself not run on every prepare.
        (self.root / "docs/work").mkdir(parents=True)
        calls = []
        original = ship.sd_ship_body.completed_process

        def recording(root, argv, **kwargs):
            calls.append(argv)
            return original(root, argv, **kwargs)

        with patch.object(ship.sd_ship_body, "completed_process", recording):
            self.assertEqual(self.prepare()["phase"], "ready_to_send")
        lint = [argv for argv in calls if any(str(word).endswith("sd-docs-lint") for word in argv)]
        self.assertEqual(len(lint), 1, calls)
        body = pathlib.Path(lint[0][lint[0].index("--pr-body") + 1])
        self.assertFalse(body.exists(), "the body file is temporary")

    def test_standard_change_does_not_request_copilot_automatically(self):
        result = self.prepare()
        self.assertEqual(result["copilot_review"]["decision"], "not_selected")
        self.assertEqual(self.double.copilot_requests, [])

    # -- sd:1328: the Copilot decision is made at dispatch, from the policy as
    # it stands then and the tier the retained review recorded. A review that
    # already happened is evidence of the tier; its own `automatic` verdict is
    # not authoritative, or the operator's cost lever would not move until
    # the next review was paid for.

    def machine_copilot(self, value: str | None) -> None:
        """The fleet's `sd.copilot_review` row (sd:5109), or no row at all."""
        set_setting(self.connection, "sd.copilot_review", value)

    def route_deep_saying_nothing_about_copilot(self) -> None:
        """A review declaration that escalates `src.py` and leaves Copilot to the machine."""
        self.declare_review({"sensitive": ["src.py"]})

    def test_never_set_after_a_deep_review_stops_the_automatic_request(self):
        """The reviewer's scenario: a deep review is retained under the default,
        the operator sets `never` because they just saw the bill, and the ship
        must not request Copilot on the strength of the old report."""
        self.route_deep_saying_nothing_about_copilot()
        self.machine_copilot(None)
        head = _git(self.root, "rev-parse", "HEAD")
        self.operation().review(head)
        recorded = self.operation().state["passes"][-1]["report"]["remote_reviews"]["copilot"]
        self.assertEqual((recorded["tier"], recorded["automatic"]), ("deep", True))
        self.machine_copilot("never")
        prepared = self.prepare()
        self.assertEqual(prepared["copilot_review"]["decision"], "not_selected")
        self.assertEqual(self.double.copilot_requests, [])

    def test_always_set_after_a_review_under_never_requests_at_dispatch(self):
        """The converse: the retained report said no, the policy now says
        always, and the standard-tier change is requested at dispatch."""
        self.machine_copilot("never")
        head = _git(self.root, "rev-parse", "HEAD")
        self.operation().review(head)
        recorded = self.operation().state["passes"][-1]["report"]["remote_reviews"]["copilot"]
        self.assertEqual((recorded["tier"], recorded["automatic"]), ("standard", False))
        self.machine_copilot("always")
        prepared = self.prepare()
        self.assertEqual(prepared["copilot_review"]["selection"], "automatic")
        self.assertEqual(len(self.double.copilot_requests), 1)

    def test_always_leaves_a_retained_skip_tier_pass_alone(self):
        """`always` means every reviewing tier; a `skip` pass has no local
        reviewer, and the selector reads the recorded depth, not the word."""
        self.machine_copilot("always")
        def pass_at(tier, depth):
            return [{"report": {"route": {"tier": tier, "depth": depth},
                                "remote_reviews": {"copilot": {"tier": tier, "repository": None}}}}]
        self.assertFalse(ship.Ship.copilot_selected(pass_at("skip", 0)))
        self.assertTrue(ship.Ship.copilot_selected(pass_at("cheap", 1)))
        self.assertTrue(ship.Ship.copilot_selected(pass_at("deep", 1)))
        self.machine_copilot("never")
        self.assertFalse(ship.Ship.copilot_selected(pass_at("deep", 1)))

    def test_a_delta_pass_of_a_later_push_does_not_hide_the_branch_tier(self):
        """A later push is reviewed as a delta whose own tier says what the push
        changed, not what Copilot would read: the deep pass behind it still
        selects, two skip passes do not, and `never` stops the pair."""
        def pass_at(tier, depth):
            return {"report": {"route": {"tier": tier, "depth": depth},
                               "remote_reviews": {"copilot": {"tier": tier, "repository": None}}}}
        self.assertTrue(ship.Ship.copilot_selected([pass_at("deep", 1), pass_at("skip", 0)]))
        self.assertFalse(ship.Ship.copilot_selected([pass_at("skip", 0), pass_at("skip", 0)]))
        self.machine_copilot("never")
        self.assertFalse(ship.Ship.copilot_selected([pass_at("deep", 1), pass_at("skip", 0)]))

    def test_an_unreadable_database_refuses_the_dispatch_instead_of_guessing(self):
        """sd:3989: the machine's Copilot word is a database row. A database that
        does not open is an unknown word, so the paid request refuses; it is not
        read as unset, which would select by the default."""
        self.machine_copilot("always")
        broken = self.directory / "broken-home"
        (broken / ".local/share/sd").mkdir(parents=True)
        (broken / ".local/share/sd/sd.db").write_bytes(b"invalid fixture database")
        deep = [{"report": {"route": {"tier": "deep", "depth": 1},
                            "remote_reviews": {"copilot": {"tier": "deep", "repository": None}}}}]
        with patch.dict(os.environ, {"HOME": str(broken)}):
            with self.assertRaisesRegex(ship.sd_lib.ConfigError, "the workflow database did not open"):
                ship.Ship.copilot_selected(deep)

    def test_a_report_from_before_the_key_keeps_its_recorded_opt_out(self):
        """A report written before `repository` travelled in it cannot say
        whether its `automatic: false` was the repository opting out, so that
        verdict is a ceiling: the machine default does not turn it into a
        request, and a report with no verdict at all grants nothing."""
        self.route_deep_saying_nothing_about_copilot()
        self.machine_copilot("never")
        head = _git(self.root, "rev-parse", "HEAD")
        self.operation().review(head)
        operation = self.operation()
        recorded = operation.state["passes"][-1]["report"]["remote_reviews"]["copilot"]
        self.assertEqual((recorded["tier"], recorded["automatic"]), ("deep", False))
        recorded.pop("repository", None)
        self.machine_copilot(None)
        prepared = operation.prepare()
        self.assertEqual(prepared["copilot_review"]["decision"], "not_selected")
        self.assertEqual(self.double.copilot_requests, [])
        bare = [{"report": {"route": {"tier": "deep", "depth": 1}}}]
        self.assertFalse(ship.Ship.copilot_selected(bare))

    def test_a_report_from_before_the_key_still_yields_to_never(self):
        """The verdict of its day is a ceiling and not a grant: `never` set
        since subtracts from a recorded `true`."""
        self.route_deep_saying_nothing_about_copilot()
        self.machine_copilot(None)
        head = _git(self.root, "rev-parse", "HEAD")
        self.operation().review(head)
        operation = self.operation()
        recorded = operation.state["passes"][-1]["report"]["remote_reviews"]["copilot"]
        self.assertEqual((recorded["tier"], recorded["automatic"]), ("deep", True))
        recorded.pop("repository", None)
        self.machine_copilot("never")
        prepared = operation.prepare()
        self.assertEqual(prepared["copilot_review"]["decision"], "not_selected")
        self.assertEqual(self.double.copilot_requests, [])

    def test_a_repository_that_named_the_key_still_wins_over_always_at_dispatch(self):
        """The repository's say travels in the report; a machine `always` does
        not override a file that named `automatic_deep: false`."""
        self.machine_copilot("always")
        self.disable_automatic_copilot()
        prepared = self.prepare()
        self.assertEqual(prepared["copilot_review"]["decision"], "not_selected")
        self.assertEqual(self.double.copilot_requests, [])

    def test_machine_never_beats_a_repository_opt_in_at_dispatch(self):
        """sd:1444, inverting the sd:1328 precedence for `never` alone: the
        operator's `never` stops the request even when the file names
        `automatic_deep: true`, at review time and set after it."""
        self.machine_copilot("never")
        self.enable_automatic_copilot()
        prepared = self.prepare()
        self.assertEqual(prepared["copilot_review"]["decision"], "not_selected")
        self.assertEqual(self.double.copilot_requests, [])
        recorded = [{"report": {"route": {"tier": "deep", "depth": 1},
                                "remote_reviews": {"copilot": {"tier": "deep", "repository": True}}}}]
        self.assertFalse(ship.Ship.copilot_selected(recorded))
        self.machine_copilot("deep")
        self.assertTrue(ship.Ship.copilot_selected(recorded))

    def test_a_silent_pass_cannot_outvote_a_later_repository_opt_out(self):
        """sd:1369, the reviewer's three-step sequence against committed source.

        Review once while the repository names no Copilot key, commit
        `automatic_deep: false` and verify, then set the machine to `always`.
        The first pass recorded `repository: None`, which falls through to the
        machine setting *as it stands now*; the second recorded the file's
        explicit `False`. Resolving the policy per pass and taking `any()`
        let the first outvote the second, and a paid Copilot request fired
        against a repository that had said no.
        """
        self.machine_copilot("never")
        self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        silent = self.operation().state["passes"][-1]["report"]["remote_reviews"]["copilot"]
        self.assertIsNone(silent["repository"])
        self.disable_automatic_copilot()
        self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        state = self.operation().state
        opted_out = state["passes"][-1]["report"]["remote_reviews"]["copilot"]
        self.assertIs(opted_out["repository"], False)
        self.assertEqual(len(state["passes"]), 2, "both passes have to be retained")
        self.machine_copilot("always")
        prepared = self.prepare()
        self.assertEqual(prepared["copilot_review"]["decision"], "not_selected")
        self.assertEqual(self.double.copilot_requests, [])

    def test_the_repository_word_is_read_from_the_latest_pass_that_carries_it(self):
        """The converse of the opt-out, and the reason `latest` rather than
        `any no wins`: a file that dropped `automatic_deep` since an earlier
        pass hands the question back to the machine setting, and a pass from
        before the key never speaks for the repository at all."""
        def recorded(repository):
            return {"report": {"route": {"tier": "deep", "depth": 1},
                               "remote_reviews": {"copilot": {"repository": repository}}}}
        legacy = {"report": {"route": {"tier": "deep", "depth": 1},
                             "remote_reviews": {"copilot": {"automatic": True}}}}
        self.assertIs(ship.Ship.copilot_repository_now([recorded(None), recorded(False)]), False)
        self.assertIsNone(ship.Ship.copilot_repository_now([recorded(False), recorded(None)]))
        self.assertIs(ship.Ship.copilot_repository_now([recorded(False), legacy]), False)
        self.assertIsNone(ship.Ship.copilot_repository_now([legacy]))
        self.machine_copilot("always")
        self.assertFalse(ship.Ship.copilot_selected([recorded(None), recorded(False)]))
        self.assertTrue(ship.Ship.copilot_selected([recorded(False), recorded(None)]))

    def test_a_legacy_grant_cannot_outvote_the_repository_opt_out_at_dispatch(self):
        """sd:1369, the legacy half. Two passes written before `repository`
        travelled in the report: the first granted a deep change under the
        default, and the second, after the repository committed
        `automatic_deep: false`, recorded `automatic: false`. The ceiling drops
        the second pass's tier and keeps the first, no pass speaks for the
        repository, and the machine setting of the day bought a paid review of
        a repository that had said no. The file as it stands at dispatch is the
        repository's current word, and a current opt-out stops the request
        whatever an older pass recorded."""
        self.route_deep_saying_nothing_about_copilot()
        self.machine_copilot(None)
        self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        self.disable_automatic_copilot()
        self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        operation = self.operation()
        passes = operation.state["passes"]
        self.assertEqual(len(passes), 2, "both passes have to be retained")
        for review_pass in passes:
            review_pass["report"]["remote_reviews"]["copilot"].pop("repository", None)
        verdicts = [(p["report"]["route"]["tier"], p["report"]["remote_reviews"]["copilot"]["automatic"])
                    for p in passes]
        self.assertEqual(verdicts, [("deep", True), ("deep", False)])
        # `prepare` would re-validate the first pass's digest, which the
        # simulated upgrade just changed; the dispatch decision itself is
        # `automatic_copilot_review`, and `copilot_selection` reads only it.
        for machine in (None, "deep", "always"):
            with self.subTest(machine=machine):
                self.machine_copilot(machine)
                self.assertFalse(operation.automatic_copilot_review())

    def test_a_current_machine_never_stops_a_request_a_retained_opt_in_recorded(self):
        """The machine half: a pass recorded the repository's opt-in, the file
        has since stopped naming the key, and the machine now says `never`.
        The recorded `true` is history; the current word is the machine's."""
        self.enable_automatic_copilot()
        self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        operation = self.operation()
        recorded = operation.state["passes"][-1]["report"]["remote_reviews"]["copilot"]
        self.assertIs(recorded["repository"], True)
        self.declare_review({"sensitive": ["src.py"]})
        self.machine_copilot("never")
        self.assertFalse(operation.automatic_copilot_review())

    def test_explicit_copilot_review_is_idempotent_while_the_request_is_present(self):
        first = self.prepare("--copilot-review", "request")
        second = self.prepare("--copilot-review", "request")
        self.assertEqual(first["copilot_review"]["selection"], "explicit")
        self.assertEqual(second["copilot_review"]["decision"], "already_recorded")
        self.assertEqual(len(self.double.copilot_requests), 1)
        self.assertEqual(self.double.copilot_requests[0]["body"], {
            "reviewers": ["copilot-pull-request-reviewer[bot]"]})

    def test_copilot_login_variant_keeps_an_explicit_request_idempotent(self):
        self.prepare("--copilot-review", "request")
        self.double.copilot_requested_login = "Copilot"
        repeated = self.prepare("--copilot-review", "request")
        self.assertEqual(repeated["copilot_review"]["decision"], "already_recorded")
        self.assertEqual(len(self.double.copilot_requests), 1)

    def test_deep_change_requests_one_copilot_review_and_waits_for_completion(self):
        self.enable_automatic_copilot()
        prepared = self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertEqual(prepared["copilot_review"]["selection"], "automatic")
        self.assertEqual(len(self.double.copilot_requests), 1)
        repeated = self.prepare()
        self.assertEqual(repeated["copilot_review"]["decision"], "not_repeated")
        self.assertEqual(len(self.double.copilot_requests), 1)
        with self.assertRaisesRegex(ship.Refusal, "has not completed"):
            self.merge()
        self.double.review_payload["reviews"] = [{
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
            "commit_id": head,
            "state": "COMMENTED",
            "submitted_at": "2026-09-19T20:00:00Z",
            "body": "",
        }]
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")
        self.assertEqual(self.operation().state["copilot_reviews"][0]["status"], "completed")

    def test_a_freeze_window_refuses_the_merge_and_a_lift_lets_it_land(self):
        """sd:6879. sd-ship merge reads the repository's freeze window before the gate and refuses inside it."""
        from sd_db.writes import set_freeze
        self.prepare()
        repository = self.operation().repository
        set_freeze(self.connection, repository, "2099-01-01T00:00:00Z", "release 4.2", now="2098-12-31T00:00:00Z")
        with patch.object(ship.time, "sleep"):
            with self.assertRaisesRegex(ship.Refusal, "is frozen until 2099-01-01T00:00:00\\+00:00: release 4.2") as caught:
                self.merge()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "freeze_window")
        self.assertNotEqual(self.remote.pull(1).state, "MERGED")
        set_freeze(self.connection, repository, None)
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")

    def delivering(self):
        """`prepare --deliver` with the acceptance file a delivery needs."""
        acceptance = self.directory / "acceptance.json"
        acceptance.write_text(json.dumps({"item": self.item, "complete": True, "criteria": [
            {"criterion": "actual scope", "passed": True, "evidence": "fixture-check-pass"}]}))
        return self.prepare("--deliver", "--acceptance-file", str(acceptance))

    def rebase_branch(self):
        """The fixture commit named for the item, a second commit, and a `fixup!` of the first (sd:6790)."""
        set_declaration(self.connection, str(self.operator), "policy", {"merge_method": "rebase"})
        _git(self.root, "commit", "--amend", "-q", "-m", f"change\n\nRefs: sd:{self.item}\nAuthored-with: human")
        _git(self.root, "push", "-q", "--force", "origin", "topic")  # as `lane enqueue` pushes the tip
        (self.root / "second.py").write_text("second = 1\n")
        _git(self.root, "add", "second.py")
        _git(self.root, "commit", "-q", "-m", f"fix: second\n\nCloses: sd:{self.item}\nAuthored-with: human")
        (self.root / "src.py").write_text("value = 2\n")
        _git(self.root, "commit", "-q", "-am", "fixup! change\n\nAuthored-with: human")
        return _git(self.root, "rev-parse", "origin/main")

    def test_a_rebase_policy_lands_each_commit_with_the_trailers_on_the_tip(self):
        """sd:6790. Under merge_method rebase prepare folds the fixup, puts Item:, Delivers: and
        `Reviewed-base: <base> ~2` on the tip, and force-pushes it with a lease; the merge lands both
        commits by rebase and reconcile delivers the item at the landed tip."""
        base = self.rebase_branch()
        self.delivering()
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertEqual(_git(self.remote.path, "rev-parse", "topic"), head)
        self.assertEqual(_git(self.root, "rev-list", "--count", f"{base}..{head}"), "2")
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assertEqual(result["phase"], "merged")
        landed = _git(self.remote.path, "log", "--format=%B%x1e", f"{base}..main").split("\x1e")
        tip, first = landed[0].strip(), landed[1].strip()
        self.assertTrue(tip.startswith("fix: second\n\n"), tip)
        self.assertIn(f"Item: sd:{self.item}\nDelivers: sd:{self.item}\nReviewed-base: {base} ~2", tip)
        self.assertNotIn("Closes:", ship.sd_lib.trailer_block(tip), tip)  # the builder's became Refs:
        self.assertIn(f"Refs: sd:{self.item}", ship.sd_lib.trailer_block(tip), tip)
        self.assertTrue(first.startswith("change\n\n") and "Delivers:" not in first, first)
        self.assertEqual(_git(self.remote.path, "show", "main:src.py"), "value = 2")
        self.assertEqual(result["merge_commit"], _git(self.remote.path, "rev-parse", "main"))
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id = ?", (self.item,)).fetchone()[0],
                         "done")

    def test_a_rebase_lands_no_closing_trailer_prepare_did_not_validate(self):
        """sd:6790 review. A rebase lands every commit's message on main, so a `Closes:` or `Delivers:`
        a builder wrote becomes `Refs:`; only the trailers prepare composes close anything."""
        set_declaration(self.connection, str(self.operator), "policy", {"merge_method": "rebase"})
        _git(self.root, "commit", "--amend", "-q", "-m", "change\n\nDelivers: sd:43\nAuthored-with: human")
        _git(self.root, "commit", "--allow-empty", "-q", "-m", "fix: tip\n\nCloses: sd:42\nAuthored-with: human")
        _git(self.root, "push", "-q", "--force", "origin", "topic")
        self.prepare()
        base = _git(self.root, "rev-parse", "origin/main")
        messages = _git(self.root, "log", "--format=%B%x1e", f"{base}..HEAD").split("\x1e")
        for message in messages:
            for number in ("sd:42", "sd:43"):
                self.assertFalse(ship.sd_lib._closes(message, number), message)
        tip = messages[0].strip()
        self.assertIn("Refs: sd:42", tip)
        self.assertIn(f"Item: sd:{self.item}", tip)
        self.assertIn("Refs: sd:43", messages[1])

    def test_a_whitespace_only_blank_line_still_ends_the_trailer_block_a_rebase_rewrites(self):
        """sd:6790 review. Git and `trailer_block` take a space- or tab-only line as the blank one before
        the trailers; sanitizing must split there too, or a builder's `Closes:` lands and closes."""
        for blank in (" ", "\t", " \t", "\r"):
            with self.subTest(blank=repr(blank)):
                message = f"fix: partial sd:1\n{blank}\nCloses: sd:43\nDelivers: sd:44"
                self.assertTrue(ship.sd_lib._closes(message, "sd:43"), "the unsanitized message closes")
                cleaned = ship.without_closes(message)
                for number in ("sd:43", "sd:44"):
                    self.assertFalse(ship.sd_lib._closes(cleaned, number), cleaned)
                self.assertIn("Refs: sd:43", cleaned)
        set_declaration(self.connection, str(self.operator), "policy", {"merge_method": "rebase"})
        _git(self.root, "commit", "--amend", "-q", "--cleanup=verbatim", "-m",
             "change\n\t\nDelivers: sd:43\nAuthored-with: human")
        _git(self.root, "commit", "--allow-empty", "-q", "--cleanup=verbatim", "-m",
             "fix: tip\n \nCloses: sd:42\nAuthored-with: human")
        _git(self.root, "push", "-q", "--force", "origin", "topic")
        self.prepare()
        base = _git(self.root, "rev-parse", "origin/main")
        messages = _git(self.root, "log", "--format=%B%x1e", f"{base}..HEAD").split("\x1e")
        for message in messages:
            for number in ("sd:42", "sd:43"):
                self.assertFalse(ship.sd_lib._closes(message, number), message)
        tip = ship.sd_lib.trailer_block(messages[0])
        self.assertIn("Refs: sd:42", tip)
        self.assertIn(f"Item: sd:{self.item}", tip)

    def test_a_rebase_that_would_drop_a_merge_commits_own_edit_refuses(self):
        """sd:6790 review. Linearizing drops merge commits; a catch-up merge that also edited a file would
        lose that edit, so prepare refuses when the replayed tree differs and pushes nothing."""
        set_declaration(self.connection, str(self.operator), "policy", {"merge_method": "rebase"})
        _git(self.root, "commit", "--amend", "-q", "-m", f"change\n\nRefs: sd:{self.item}\nAuthored-with: human")
        _git(self.root, "push", "-q", "--force", "origin", "topic")
        pushed = _git(self.root, "rev-parse", "HEAD")
        self.remote.commit_on("main", "later main work\n\nAuthored-with: human", files={"main.py": "x = 1\n"})
        _git(self.root, "fetch", "-q", "origin", "main")
        _git(self.root, "merge", "-q", "--no-ff", "--no-commit", "origin/main")
        (self.root / "resolution.py").write_text("edited = 1\n")
        _git(self.root, "add", "resolution.py")
        _git(self.root, "commit", "-q", "-m", f"Merge origin/main\n\nRefs: sd:{self.item}\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ship.Refusal, "would change the tree") as caught:
            self.prepare()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "rebase_changes_tree")
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(_git(self.remote.path, "rev-parse", "topic"), pushed)

    def test_a_buried_prepared_tip_closes_nothing_the_new_claim_dropped(self):
        """sd:6790 review. A prepare delivering this item and a second one writes both on the tip; a later
        commit buries it, and a prepare naming this item alone must leave no `Delivers:` of the second on
        any landed commit. That rewrites the reviewed tip, so the review restarts."""
        self.rebase_branch()
        other = create_item(self.connection, kind="work", title="other", status="in_progress",
                            repo=str(self.operator), branch="topic")
        acceptance = self.directory / "acceptance.json"
        acceptance.write_text(json.dumps({"item": self.item, "complete": True, "criteria": [
            {"criterion": "actual scope", "passed": True, "evidence": "fixture-check-pass"}]}))
        self.prepare("--deliver", "--acceptance-file", str(acceptance), "--item", str(other))
        self.assertIn(f"Delivers: sd:{other}", _git(self.root, "show", "-s", "--format=%B", "HEAD"))
        _git(self.root, "commit", "--allow-empty", "-q", "-m", f"fix: third\n\nRefs: sd:{self.item}\nAuthored-with: human")
        with self.assertRaises(ship.Refusal) as caught:
            self.prepare()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "reviewed_head_orphaned")
        self.prepare("--restart-review", "the claim dropped an item")
        base = _git(self.root, "rev-parse", "origin/main")
        messages = [part.strip() for part in _git(self.root, "log", "--format=%B%x1e", f"{base}..HEAD").split("\x1e")]
        for message in messages:
            self.assertFalse(ship.sd_lib._closes(message, f"sd:{other}"), message)
        for buried in messages[1:]:
            self.assertNotIn("Reviewed-base:", buried)

    def test_a_rebase_branch_commit_naming_no_item_refuses_at_prepare(self):
        """sd:6790. A rebase lands every commit on main as it is, so one that names no item refuses before
        the review or a push."""
        set_declaration(self.connection, str(self.operator), "policy", {"merge_method": "rebase"})
        _git(self.root, "commit", "--allow-empty", "-q", "-m", "fix: tip\n\nAuthored-with: human")
        with self.assertRaisesRegex(ship.Refusal, r"commit [0-9a-f]{12} \('change'\) names no item") as caught:
            self.prepare()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "commit_convention")
        self.assertEqual(self.remote.pull_requests, {})

    def test_a_rebase_tip_a_later_commit_buried_is_written_again(self):
        """sd:6790. A commit after prepare leaves the old tip's trailers mid-branch and a tip without them;
        merge refuses that head, and the next prepare writes the trailers on the new tip."""
        self.rebase_branch()
        self.delivering()
        _git(self.root, "commit", "--allow-empty", "-q", "-m", f"fix: third\n\nRefs: sd:{self.item}\nAuthored-with: human")
        with patch.object(ship.time, "sleep"):
            with self.assertRaises(ship.Refusal):
                self.merge()
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertIn(" ~3", _git(self.root, "show", "-s", "--format=%B", head))
        pull = self.remote.pull(1)
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")

    def test_a_rebase_push_refuses_a_remote_head_this_checkout_never_held(self):
        """sd:6790. The force push is leased to a head this branch had; another writer's push refuses."""
        self.rebase_branch()
        self.remote.commit_on("topic", "someone else\n\nAuthored-with: human", files={"other.py": "x = 1\n"})
        with self.assertRaisesRegex(ship.Refusal, "which this checkout never held"):
            self.delivering()
        self.assertEqual(self.remote.pull_requests, {})

    def test_a_merge_policy_lands_a_merge_commit_carrying_the_trailers(self):
        """sd:6790. merge_method merge sends a merge commit whose message carries Item: and Delivers:."""
        set_declaration(self.connection, str(self.operator), "policy", {"merge_method": "merge"})
        self.delivering()
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")
        parents = _git(self.remote.path, "rev-list", "--parents", "-n", "1", "main").split()
        self.assertEqual(len(parents), 3)
        self.assertIn(f"Delivers: sd:{self.item}", _git(self.remote.path, "show", "-s", "--format=%B", "main"))

    def test_a_policy_method_the_repository_disallows_refuses_before_any_merge(self):
        """sd:6790. A policy that says rebase where GitHub's repository setting forbids it refuses."""
        self.rebase_branch()
        self.prepare()
        route = self.double._route
        def forbid(method, path, body):
            code, value = route(method, path, body)
            if method == "GET" and path == f"/repos/{self.remote.slug}":
                value["allow_rebase_merge"] = False
            return code, value
        with patch.object(self.double, "_route", forbid), patch.object(ship.time, "sleep"):
            with self.assertRaisesRegex(ship.Refusal, "rebase merging is disabled"):
                self.merge()
        self.assertNotEqual(self.remote.pull(1).state, "MERGED")

    def test_the_squash_message_takes_the_declared_convention(self):
        """sd:6715, run 210 blocker. The squash subject's alias type is rewritten before GitHub writes it; a
        squash message the convention still refuses (no footer) refuses the merge, naming the rule."""
        set_declaration(self.connection, str(self.operator), "policy", {"commit_convention": {
            "types": ["doc", "fix"], "aliases": {"docs": "doc"}, "footer": r"^Ref: LOG-\d+$"}})
        self.prepare("--title", "docs: words")
        with patch.object(ship.time, "sleep"):
            with self.assertRaisesRegex(ship.Refusal, r"no footer line matches") as caught:
                self.merge()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "commit_convention")
        self.assertNotEqual(self.remote.pull(1).state, "MERGED")
        set_declaration(self.connection, str(self.operator), "policy", {"commit_convention": {
            "types": ["doc", "fix"], "aliases": {"docs": "doc"}}})
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")
        self.assertTrue(self.remote.pull(1).title.startswith("doc: words (#1)\n\n"), self.remote.pull(1).title)

    def test_a_declared_footer_in_the_body_moves_into_the_trailer_block(self):
        """sd:6715, review of build 244. squash_body adds a paragraph of Item:/Delivers: trailers after the body,
        so the author's `Ref: LOG-1` footer sat in the second-last paragraph and the convention refused it."""
        set_declaration(self.connection, str(self.operator), "policy", {"commit_convention": {
            "footer": r"^Ref: LOG-\d+$"}})
        body = self.directory / "body.md"
        body.write_text("Concrete change.\n\nRef: LOG-1\n")
        self.prepare("--title", "fix: words", "--body-file", str(body))
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")
        message = self.remote.pull(1).title
        self.assertEqual(message.count("Ref: LOG-1"), 1, message)
        self.assertIn("Ref: LOG-1", message.rstrip("\n").split("\n\n")[-1], message)

    def test_dismissed_approval_holds_the_merge(self):
        """sd:6715, design § Failure table. review.human is read at merge time, bound to the head commit: an
        approval of an earlier commit (dismissed by a push) or by a bot holds the merge; one of the head merges."""
        set_declaration(self.connection, str(self.operator), "policy", {"review": {"human": 1}})
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        review = {"state": "APPROVED", "submitted_at": "2026-09-19T20:00:00Z", "body": ""}
        self.double.review_payload["reviews"] = [
            {**review, "user": {"login": "reviewer-a", "type": "User"}, "commit_id": "0" * 40},
            {**review, "user": {"login": "helper[bot]", "type": "Bot"}, "commit_id": head},
            {**review, "user": {"login": "reviewer-b", "type": "User"}, "commit_id": head},
            {**review, "state": "DISMISSED", "user": {"login": "reviewer-b", "type": "User"}, "commit_id": head}]
        with patch.object(ship.time, "sleep"):
            with self.assertRaisesRegex(ship.Refusal, r"requires 1 human approval of .*; 0 approve it") as caught:
                self.merge()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "approval_required")
        self.assertNotEqual(self.remote.pull(1).state, "MERGED")
        self.double.review_payload["reviews"].append(
            {**review, "user": {"login": "reviewer-a", "type": "User"}, "commit_id": head})
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")

    def test_a_jenkins_policy_holds_the_merge_until_the_head_build_is_green(self):
        """sd:6880, design § Failure table. A policy naming Jenkins makes its job the CI: an unreachable Jenkins
        holds the merge as ci_unreachable, never green; a green build of the head merges."""
        ci = {"provider": "jenkins", "url": "http://127.0.0.1:9", "job": "team/repo/PR-{pr}"}
        set_declaration(self.connection, str(self.operator), "policy", {"ci": ci})
        self.prepare()
        with patch.object(ship.time, "sleep"):
            with self.assertRaises(ship.Refusal) as caught:
                self.merge()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "ci_unreachable")
        self.assertNotEqual(self.remote.pull(1).state, "MERGED")
        head = _git(self.root, "rev-parse", "HEAD")
        green = {"result": "SUCCESS", "building": False, "actions": [{"lastBuiltRevision": {"SHA1": head}}]}
        with patch.object(ship.time, "sleep"), \
                patch.object(ship.sd_ship_jenkins, "head_build", return_value=green) as read:
            self.assertEqual(self.merge()["phase"], "merged")
        self.assertEqual(read.call_args.args[:3], (ci, 1, head))

    def test_a_pending_refusal_names_who_did_review_the_head(self):
        """sd:3499. A renamed Copilot bot's review read as pending forever,
        with "wait, then retry"; the refusal names the logins that reviewed the head."""
        self.enable_automatic_copilot()
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        review = {"state": "COMMENTED", "submitted_at": "2026-09-19T20:00:00Z", "body": ""}
        self.double.review_payload["reviews"] = [
            {**review, "user": {"login": "copilot-renamed[bot]"}, "commit_id": head},
            {**review, "user": {"login": "earlier-reviewer"}, "commit_id": "0" * 40}]
        with self.assertRaisesRegex(ship.Refusal, r"has not completed.*copilot-renamed\[bot\]") as caught:
            self.merge()
        self.assertNotIn("earlier-reviewer", str(caught.exception))

    def test_later_push_keeps_one_automatic_review_but_requires_exact_head_review(self):
        # The later push edits `src.py`, the shipped surface. It was an empty
        # commit until sd:1366, which is now the case a reviewed ancestor
        # clears (`test_a_docs_only_push_clears_the_gate_on_the_reviewed_ancestor`);
        # what this test pins is the other half, that a shipped-surface change
        # since the reviewed head still refuses the merge.
        self.enable_automatic_copilot()
        self.prepare()
        reviewed = _git(self.root, "rev-parse", "HEAD")
        self.double.review_payload["reviews"] = [{
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
            "commit_id": reviewed,
            "state": "COMMENTED",
            "submitted_at": "2026-09-19T20:00:00Z",
            "body": "",
        }]
        (self.root / "src.py").write_text("value = 2\n")
        _git(self.root, "add", "src.py")
        _git(self.root, "commit", "-m", "later fix\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        repeated = self.prepare()
        self.assertNotEqual(head, reviewed)
        self.assertEqual(self.operation().state["reviewed_head"], head)
        self.assertEqual(repeated["copilot_review"]["decision"], "not_repeated")
        self.assertEqual(len(self.double.copilot_requests), 1)
        pull = self.remote.pull(1)
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        self.assertTrue(all(row["head_sha"] == head for row in pull.checks))
        with patch.object(ship.time, "sleep"):
            with self.assertRaisesRegex(ship.Refusal, "change the shipped surface: src.py"):
                self.merge()
        self.double.copilot_pending.clear()
        requested = self.prepare("--copilot-review", "request")
        self.assertEqual(requested["copilot_review"]["selection"], "explicit")
        self.assertEqual(len(self.double.copilot_requests), 2)
        self.double.review_payload["reviews"] = [{
            "user": {"login": "Copilot"},
            "commit_id": head,
            "state": "COMMENTED",
            "submitted_at": "2026-09-19T20:01:00Z",
            "body": "",
        }]
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")

    def reviewed_at(self, head):
        """The pull's one completed Copilot review, submitted against `head`."""
        self.double.review_payload["reviews"] = [{
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
            "commit_id": head,
            "state": "COMMENTED",
            "submitted_at": "2026-09-19T20:00:00Z",
            "body": "",
        }]

    def test_a_docs_only_push_clears_the_gate_on_the_reviewed_ancestor(self):
        """sd:1366. `prepare_copilot_review` buys one automatic review per pull
        request and returns `not_repeated` at the new head; `copilot_reviewed_head`
        demanded the exact merge head. A deep branch taking a second push could
        satisfy neither, and answering a Copilot finding *is* a second push --
        so the branch that did what the review asked was the one that could
        never clear the gate on its own. A reviewed ancestor now stands when
        the commits since it touch nothing outside `docs/`, and the merge
        receipt carries a warning saying so rather than passing in silence.
        """
        self.enable_automatic_copilot()
        self.prepare()
        reviewed = _git(self.root, "rev-parse", "HEAD")
        self.reviewed_at(reviewed)
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs/note.md").write_text("what the review asked for\n")
        _git(self.root, "add", "docs/note.md")
        _git(self.root, "commit", "-m", "answer the review\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        repeated = self.prepare()
        self.assertNotEqual(head, reviewed)
        self.assertEqual(repeated["copilot_review"]["decision"], "not_repeated")
        pull = self.remote.pull(1)
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")
        self.assertEqual(len(self.double.copilot_requests), 1)
        self.assertTrue(any(reviewed in warning and "ancestor of the merge head" in warning
                            for warning in self.operation().state["warnings"]),
                        self.operation().state.get("warnings"))

    def test_a_clean_catch_up_keeps_the_copilot_review_of_the_branch(self):
        """sd:1485. Main's commits are shipped surface the Copilot review never
        read, so every catch-up merge made that review stale and cost a new
        request and a wait. When the commits since the reviewed head are clean
        merges of the base and the branch's own patch-id is unchanged, the
        review still covers the branch, and the merge receipt says why."""
        self.enable_automatic_copilot()
        self.prepare()
        reviewed = _git(self.root, "rev-parse", "HEAD")
        self.reviewed_at(reviewed)
        self.remote.commit_on("main", "unrelated main work\n\nAuthored-with: human", files={"lib/other.txt": "main\n"})
        self.assertEqual(self.prepare("--catch-up")["phase"], "ready_to_send")
        head = _git(self.root, "rev-parse", "HEAD")
        pull = self.remote.pull(1)
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")
        self.assertEqual(len(self.double.copilot_requests), 1)
        self.assertTrue(any(reviewed in warning and "patch-id is unchanged" in warning
                            for warning in self.operation().state["warnings"]),
                        self.operation().state.get("warnings"))

    def test_a_catch_up_that_moves_the_branchs_patch_leaves_the_copilot_review_stale(self):
        self.enable_automatic_copilot()
        self.prepare()
        reviewed = _git(self.root, "rev-parse", "HEAD")
        self.reviewed_at(reviewed)
        self.remote.commit_on("main", "rival src\n\nAuthored-with: human", files={"other.txt": "main\n"})
        _git(self.root, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
        _git(self.root, "merge", "-q", "--no-ff", "--no-commit", "origin/main")
        (self.root / "src.py").write_text("value = 5\n")
        _git(self.root, "commit", "-qam", "Merge origin/main into topic\n\nAuthored-with: human")
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        pull = self.remote.pull(1)
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        with patch.object(ship.time, "sleep"):
            with self.assertRaisesRegex(ship.Refusal, "change the shipped surface") as caught:
                self.merge()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "copilot_review_stale")

    def test_a_merge_refused_after_the_copilot_gate_records_no_ancestor_clearance(self):
        """sd:1373. The ancestor note says the gate cleared a merge, so the step
        that dispatches the merge writes it. Written when the gate cleared, it
        survived every later refusal, and each retry appended another copy."""
        self.enable_automatic_copilot()
        self.prepare()
        reviewed = _git(self.root, "rev-parse", "HEAD")
        self.reviewed_at(reviewed)
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs/note.md").write_text("what the review asked for\n")
        _git(self.root, "add", "docs/note.md")
        _git(self.root, "commit", "-m", "answer the review\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.prepare()
        pull = self.remote.pull(1)
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        def ancestor_notes():
            return [warning for warning in (self.operation().state.get("warnings") or [])
                    if "ancestor of the merge head" in warning]

        refusal = ship.Refusal("refused after the Copilot gate")
        with patch.object(ship.Ship, "reviewed_base", side_effect=refusal), patch.object(ship.time, "sleep"):
            for _ in range(2):
                with self.assertRaisesRegex(ship.Refusal, "after the Copilot gate"):
                    self.merge()
        self.assertEqual(ancestor_notes(), [])
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")
        self.assertEqual(len(ancestor_notes()), 1, ancestor_notes())
        self.assertIn(reviewed, ancestor_notes()[0])

    def test_an_exact_head_review_landing_mid_wait_drops_the_ancestor_note(self):
        """`stable_copilot_material` reads the reviews once per attempt, so the
        exact-head review can land between two of them. The merge receipt has
        to say what the last reading found: an ancestor that cleared the first
        attempt must not still be named after the second matched exactly."""
        self.enable_automatic_copilot()
        self.prepare()
        reviewed = _git(self.root, "rev-parse", "HEAD")
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs/note.md").write_text("what the review asked for\n")
        _git(self.root, "add", "docs/note.md")
        _git(self.root, "commit", "-m", "answer the review\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.prepare()
        base = {"user": {"login": "copilot-pull-request-reviewer[bot]"},
                "state": "COMMENTED", "body": ""}
        exact = {**base, "commit_id": head, "submitted_at": "2026-09-19T20:01:00Z"}
        self.double.review_sequences["reviews"] = [
            [{**base, "commit_id": reviewed, "submitted_at": "2026-09-19T20:00:00Z"}],
            [exact], [exact],
        ]
        pull = self.remote.pull(1)
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")
        self.assertEqual([warning for warning in (self.operation().state.get("warnings") or [])
                          if "ancestor of the merge head" in warning], [])

    def test_the_shipped_surface_is_every_path_outside_docs(self):
        """The definition, stated once and checked against the tree rather than
        asserted in prose. `tests/` is deliberately inside the surface: CI
        re-running a test proves it passes, not that it still asserts what the
        reviewer approved, and this pack's own caps and shell guard live there.
        """
        operation = self.operation()
        base = _git(self.root, "rev-parse", "HEAD")
        for name, body in (("docs/note.md", "prose\n"),
                           ("tests/test_answer.py", "def test_answer():\n    assert True\n"),
                           ("skills/thing/SKILL.md", "payload\n"),
                           ("README.md", "root prose\n"),
                           ("src/docs/inner.md", "nested\n")):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "a bit of everything\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertEqual(
            operation.unreviewed_shipped_surface(base, head),
            ["README.md", "skills/thing/SKILL.md", "src/docs/inner.md", "tests/test_answer.py"])
        _git(self.root, "commit", "--allow-empty", "-m", "nothing at all\n\nAuthored-with: human")
        self.assertEqual(
            operation.unreviewed_shipped_surface(head, _git(self.root, "rev-parse", "HEAD")), [])

    def test_a_non_ascii_planning_path_still_reaches_the_guest_refusal(self):
        """`core.quotePath` is on by default, so `--name-only` prints a
        non-ASCII path as a C-quoted string: `docs/work/é.md` came out as
        `"docs/work/\\303\\251.md"`, missed every planning prefix, and a guest
        destination received the planning file (sd:1440). The first assertion
        pins the premise, so this test cannot pass because git stopped quoting.
        """
        operation = self.operation()
        base = _git(self.root, "rev-parse", "HEAD")
        for name in ("docs/work/é.md", "docs/spec/naïve.md", "src/ok.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("body\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "plan in a non-ASCII name\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertIn('"docs/work/\\303\\251.md"',
                      _git(self.root, "diff", "--name-only", base, head).splitlines(),
                      "the premise: git quotes a non-ASCII path by default")
        self.assertEqual(operation.planning_artifacts(base, head),
                         ["docs/spec/naïve.md", "docs/work/é.md"])

    def test_a_guest_opt_out_takes_decision_records_out_of_the_push_check(self):
        """`guest_allow: docs/decisions` in the local block, and only that tree
        leaves the planning paths a guest push refuses (sd:2168)."""
        base = _git(self.root, "rev-parse", "HEAD")
        for name in ("docs/decisions/0001-a-choice.md", "docs/work/plan.md", "docs/spec/a.md"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("body\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "plan and decide\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        everything = ["docs/decisions/0001-a-choice.md", "docs/spec/a.md", "docs/work/plan.md"]
        self.assertEqual(self.operation().planning_artifacts(base, head), everything)
        local = self.root / "CLAUDE.local.md"
        local.write_text(local.read_text().replace("mode: full\n", "mode: full\nguest_allow: docs/decisions\n"))
        self.assertEqual(self.operation().planning_artifacts(base, head), everything[1:])

    def test_a_move_out_of_the_shipped_surface_still_names_its_source(self):
        """A rename reports its destination only, and the source vanished.

        Rename detection is git's default, and `--name-only` renders a
        detected rename as the destination path alone. So `git mv bin/tool.py
        docs/tool.py` yielded exactly `docs/tool.py`, the prefix filter
        dropped it as prose, and the gate cleared on a commit that deleted a
        shipped file the reviewer had read. `--no-renames` is what puts the
        deletion back in the set. The first assertion pins the premise: if
        git ever stops detecting the rename, this test has to say so rather
        than pass for the wrong reason.
        """
        operation = self.operation()
        shipped = self.root / "bin/tool.py"
        shipped.parent.mkdir(parents=True, exist_ok=True)
        shipped.write_text("A = 1\nB = 2\nC = 3\nD = 4\nE = 5\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "ship a tool\n\nAuthored-with: human")
        base = _git(self.root, "rev-parse", "HEAD")
        (self.root / "docs").mkdir(exist_ok=True)
        _git(self.root, "mv", "bin/tool.py", "docs/tool.py")
        _git(self.root, "commit", "-m", "move it out of the surface\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertEqual(
            _git(self.root, "diff", "--name-only", base, head).splitlines(), ["docs/tool.py"],
            "the premise: git renders a detected rename as its destination alone")
        self.assertEqual(operation.unreviewed_shipped_surface(base, head), ["bin/tool.py"])

    def test_a_path_the_repository_never_skips_stays_in_the_surface(self):
        """`UNSHIPPED_PREFIXES` is this gate's blanket; `never_skip` is the
        repository's deny-list over it.

        `bin/sd_route.py` makes that list beat `docs_skip` outright -- a change
        touching one of its paths is never routed to `skip`, which is the whole
        reason the allow-list is safe to widen. A merge gate that exempted
        those paths anyway would skip exactly what the repository said may not
        be skipped. The default carries `docs/spec/**` even when the file does
        not name the key, and a file that names its own list is read instead
        of the constant.
        """
        self.enable_automatic_copilot()
        operation = self.operation()
        base = _git(self.root, "rev-parse", "HEAD")
        for name in ("docs/spec/contract.md", "docs/note.md", "docs/handbook/how.md"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("prose\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "spec and prose\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertEqual(operation.unreviewed_shipped_surface(base, head),
                         ["docs/spec/contract.md"])
        self.declare_review({
            "sensitive": ["src.py"],
            "never_skip": ["docs/handbook/**"],
            "copilot_review": {"automatic_deep": True},
        })
        self.assertEqual(operation.unreviewed_shipped_surface(base, head),
                         ["docs/handbook/how.md"])

    def test_a_docs_path_the_repository_does_not_skip_stays_in_the_surface(self):
        """sd:1383. `never_skip` is one half of the router's word; `docs_skip`
        is the other.

        `bin/sd_route.py` plans `skip` only when every path is in `docs_skip`
        and none is in `never_skip`. The gate honoured the second list and
        not the first, so a repository that narrowed `docs_skip` -- saying
        its `docs/` is reviewed -- still had every `docs/` path cleared on a
        reviewed ancestor. A path is exempt now only when it is under `docs/`
        *and* the repository's own policy would let it skip.
        """
        self.enable_automatic_copilot()
        operation = self.operation()
        base = _git(self.root, "rev-parse", "HEAD")
        for name in ("docs/note.md", "docs/handbook/how.md"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("prose\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "prose\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.declare_review({
            "sensitive": ["src.py"],
            "docs_skip": ["docs/handbook/**"],
            "copilot_review": {"automatic_deep": True},
        })
        self.assertEqual(operation.unreviewed_shipped_surface(base, head), ["docs/note.md"])
        self.declare_review({
            "sensitive": ["src.py"],
            "docs_skip": [],
            "copilot_review": {"automatic_deep": True},
        })
        self.assertEqual(operation.unreviewed_shipped_surface(base, head),
                         ["docs/handbook/how.md", "docs/note.md"])

    def test_a_policy_that_does_not_parse_refuses_the_gate_rather_than_guessing(self):
        """The deny-list read is the first `load_policy` call in `sd-ship`, and
        `PolicyError` is not in `main`'s caught tuple, so an unreadable file
        would have escaped as a traceback. It refuses as a receipt instead --
        and it refuses rather than falling back to the built-in list, because a
        repository whose policy will not parse has said nothing this gate may
        rely on.
        """
        self.enable_automatic_copilot()
        operation = self.operation()
        base = _git(self.root, "rev-parse", "HEAD")
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs/note.md").write_text("prose\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "prose\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.declare_review({"default_tier": "gold"})
        with self.assertRaisesRegex(ship.Refusal, "review policy does not parse") as raised:
            operation.unreviewed_shipped_surface(base, head)
        self.assertEqual(raised.exception.workflow["blocker"]["code"], "review_policy_malformed")

    def test_later_push_accepts_the_single_automatic_review_on_the_new_head(self):
        self.enable_automatic_copilot()
        self.prepare()
        recorded = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "commit", "--allow-empty", "-m", "later fix\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.prepare()
        self.assertNotEqual(head, recorded)
        self.double.review_payload["reviews"] = [{
            "user": {"login": "Copilot"},
            "commit_id": head,
            "state": "COMMENTED",
            "submitted_at": "2026-09-19T20:00:00Z",
            "body": "",
        }]
        pull = self.remote.pull(1)
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge()["phase"], "merged")
        self.assertEqual(self.operation().state["copilot_reviews"][0]["status"], "requested")

    def test_explicit_skip_suppresses_a_deep_automatic_review(self):
        self.enable_automatic_copilot()
        result = self.prepare("--copilot-review", "skip")
        self.assertEqual(result["copilot_review"], {
            "decision": "skipped", "reason": "task-scoped suppression"})
        self.assertEqual(self.double.copilot_requests, [])

    def test_explicit_skip_suppresses_later_automatic_prepares(self):
        self.enable_automatic_copilot()
        self.prepare("--copilot-review", "skip")
        repeated = self.prepare()
        self.assertEqual(repeated["copilot_review"], {
            "decision": "skipped", "reason": "task-scoped suppression"})
        self.assertEqual(self.double.copilot_requests, [])

    def test_explicit_request_replaces_persisted_automatic_suppression(self):
        self.enable_automatic_copilot()
        self.prepare("--copilot-review", "skip")
        requested = self.prepare("--copilot-review", "request")
        self.assertEqual(requested["copilot_review"]["selection"], "explicit")
        self.assertFalse(self.operation().state["copilot_review_suppressed"])
        repeated = self.prepare()
        self.assertEqual(repeated["copilot_review"]["decision"], "not_repeated")
        self.assertEqual(len(self.double.copilot_requests), 1)

    def test_refused_explicit_request_preserves_persisted_suppression(self):
        self.enable_automatic_copilot()
        self.prepare("--copilot-review", "skip")
        with self.assertRaisesRegex(ship.Refusal, "provide a final --title"):
            self.prepare("--copilot-review", "request", "--title", "WIP incomplete")
        repeated = self.prepare()
        self.assertEqual(repeated["copilot_review"], {
            "decision": "skipped", "reason": "task-scoped suppression"})
        self.assertEqual(self.double.copilot_requests, [])

    def test_failed_explicit_dispatch_preserves_persisted_suppression(self):
        self.enable_automatic_copilot()
        self.prepare("--copilot-review", "skip")
        self.double.lose_copilot_request = True
        with self.assertRaisesRegex(ship.Refusal, "review request transport failed"):
            self.prepare("--copilot-review", "request")
        state = self.operation().state
        self.assertTrue(state["copilot_review_suppressed"])
        self.assertEqual(state["phase"], "ready_to_send")
        self.assertEqual(state["pull_request"]["number"], 1)
        self.assertEqual(self.double.copilot_requests, [])

    def test_failed_automatic_dispatch_warns_without_recording_a_request(self):
        self.enable_automatic_copilot()
        self.double.lose_copilot_request = True
        result = self.prepare()
        self.assertEqual(result["phase"], "ready_to_send")
        self.assertEqual(result["copilot_review"]["decision"], "request_failed")
        self.assertIn("automatic Copilot review request failed", "\n".join(result["warnings"]))
        self.assertEqual(self.operation().state.get("copilot_reviews"), None)
        self.assertEqual(self.double.copilot_requests, [])

    def test_invalid_new_prepare_creates_no_receipt_or_suppression(self):
        self.enable_automatic_copilot()
        with self.assertRaisesRegex(ship.Refusal, "provide a final --title"):
            self.prepare("--copilot-review", "skip", "--title", "WIP incomplete")
        self.assertEqual(self.operation().state, {})

    def test_invalid_retry_does_not_mutate_the_existing_receipt(self):
        self.enable_automatic_copilot()
        self.prepare("--copilot-review", "skip")
        before = self.operation().state
        with self.assertRaisesRegex(ship.Refusal, "provide a final --title"):
            self.prepare("--copilot-review", "request", "--title", "WIP incomplete")
        self.assertEqual(self.operation().state, before)

    def test_automatic_copilot_excludes_guest_mode(self):
        self.enable_automatic_copilot()
        self.set_written_mode("guest")
        result = self.prepare()
        self.assertEqual(result["copilot_review"]["reason"], "repository mode is guest")
        self.assertEqual(self.double.copilot_requests, [])

    def test_automatic_copilot_excludes_minimal_mode(self):
        self.enable_automatic_copilot()
        self.set_written_mode("minimal")
        result = self.prepare()
        self.assertEqual(result["copilot_review"]["reason"], "repository mode is minimal")
        self.assertEqual(self.double.copilot_requests, [])

    def test_explicit_copilot_requires_full_mode(self):
        self.set_written_mode("guest")
        with self.assertRaisesRegex(ship.Refusal, "requires a full-owned repository"):
            self.prepare("--copilot-review", "request")
        self.assertEqual(self.double.copilot_requests, [])
        self.assertEqual(self.operation().state, {})

    def test_automatic_copilot_excludes_draft_pull_requests(self):
        self.enable_automatic_copilot()
        self.double.create_draft = True
        result = self.prepare()
        self.assertEqual(result["copilot_review"]["reason"], "automatic Copilot review excludes drafts")
        self.assertEqual(self.double.copilot_requests, [])

    def test_failing_local_review_prevents_automatic_copilot_dispatch(self):
        self.enable_automatic_copilot()
        provider = self.programs / "review-fixture"
        payload = {"type": "result", "subtype": "success", "structured_output": {"findings": [{
            "path": "src.py", "line": 1, "severity": "high",
            "family": "correctness", "summary": "value is wrong",
        }]}}
        provider.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        with self.assertRaisesRegex(ship.Refusal, "local review blocking"):
            self.prepare()
        self.assertEqual(self.double.copilot_requests, [])
        self.assertEqual(self.remote.pull_requests, {})

    def test_explicit_request_recovers_a_disappeared_request(self):
        first = self.prepare("--copilot-review", "request")
        self.double.copilot_pending.clear()
        second = self.prepare("--copilot-review", "request")
        self.assertEqual(first["copilot_review"]["decision"], "recorded")
        self.assertEqual(second["copilot_review"]["decision"], "recovered")
        self.assertEqual(len(self.double.copilot_requests), 2)

    def test_copilot_request_count_is_bounded_per_pull_request(self):
        operation = self.operation("prepare", "--copilot-review", "request")
        operation.state["copilot_reviews"] = [{
            "pull": 1,
            "head": str(index) * 40,
            "selection": "explicit",
            "status": "completed",
        } for index in (1, 2, 3)]
        with self.assertRaisesRegex(ship.Refusal, "limit of 3"):
            operation.prepare_copilot_review(1, {"draft": False}, "4" * 40, "full")

    def test_remote_copilot_request_does_not_gate_without_a_local_request_receipt(self):
        self.enable_automatic_copilot()
        self.prepare()
        self.operation().save(copilot_reviews=[])
        self.assertEqual(self.merge()["phase"], "merged")

    def test_selected_automatic_review_adopts_an_existing_remote_request(self):
        self.enable_automatic_copilot()
        self.double.copilot_pending.add(1)
        prepared = self.prepare()
        self.assertEqual(prepared["copilot_review"]["status"], "pending")
        self.assertEqual(len(self.operation().state["copilot_reviews"]), 1)
        with self.assertRaisesRegex(ship.Refusal, "has not completed"):
            self.merge()

    def test_explicit_review_adopts_an_existing_remote_request(self):
        self.double.copilot_pending.add(1)
        prepared = self.prepare("--copilot-review", "request")
        self.assertEqual(prepared["copilot_review"]["selection"], "explicit")
        self.assertEqual(prepared["copilot_review"]["status"], "pending")
        with self.assertRaisesRegex(ship.Refusal, "has not completed"):
            self.merge()

    def test_abandonment_stops_completion_wait_and_preserves_request_history(self):
        self.enable_automatic_copilot()
        self.prepare()
        requests = self.operation().state["copilot_reviews"]
        result = self.merge("--abandon-copilot-review", "provider did not complete")
        self.assertEqual(result["phase"], "merged")
        state = self.operation().state
        self.assertEqual(state["copilot_reviews"], requests)
        self.assertEqual(state["copilot_review_abandonments"][0]["pull"], 1)
        self.assertEqual(state["copilot_review_abandonments"][0]["head"], _git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(state["copilot_review_abandonments"][0]["reason"], "provider did not complete")
        self.assertRegex(state["copilot_review_abandonments"][0]["request_digest"], r"^[0-9a-f]{64}$")

    def test_abandonment_requires_a_nonempty_reason(self):
        self.enable_automatic_copilot()
        self.prepare()
        with self.assertRaisesRegex(ship.Refusal, "requires a nonempty reason"):
            self.merge("--abandon-copilot-review", "   ")
        self.assertNotIn("copilot_review_abandonments", self.operation().state)

    def test_completion_during_failed_merge_does_not_duplicate_abandonment(self):
        self.enable_automatic_copilot()
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        self.double.review_payload["reviews"] = [{
            "user": {"login": "Copilot"}, "commit_id": head, "state": "COMMENTED",
            "submitted_at": "2026-09-19T20:00:00Z",
            "body": "| File | Summary |\n|---|---|\n| `src.py` | Moderate finding (2 votes): verify. |\n",
        }]
        reason = "provider result is no longer required"
        refusal = ship.Refusal("refused after the Copilot gate")
        for _ in range(2):
            with self.assertRaisesRegex(ship.Refusal, "after the Copilot gate"):
                with patch.object(ship.Ship, "reviewed_base", side_effect=refusal), patch.object(ship.time, "sleep"):
                    self.merge("--abandon-copilot-review", reason)
            state = self.operation().state
            self.assertEqual(state["copilot_reviews"][0]["status"], "completed")
            self.assertEqual(len(state["copilot_review_abandonments"]), 1)

    def test_later_head_can_abandon_the_latest_request_after_dispatch_failure(self):
        self.enable_automatic_copilot()
        self.prepare()
        _git(self.root, "commit", "--allow-empty", "-m", "later fix\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.prepare()
        pull = self.remote.pull(1)
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        self.double.copilot_pending.clear()
        self.double.lose_copilot_request = True
        with self.assertRaisesRegex(ship.Refusal, "review request transport failed"):
            self.prepare("--copilot-review", "request")
        result = self.merge("--abandon-copilot-review", "later-head dispatch failed")
        self.assertEqual(result["phase"], "merged")
        state = self.operation().state
        self.assertEqual(len(state["copilot_reviews"]), 1)
        self.assertEqual(state["copilot_review_abandonments"][0]["head"], head)

    def test_later_head_can_abandon_the_latest_request_after_request_cap(self):
        self.enable_automatic_copilot()
        self.prepare()
        operation = self.operation()
        requests = operation.state["copilot_reviews"]
        operation.save(copilot_reviews=[*requests, *[{
            "pull": 1, "head": str(index) * 40, "selection": "explicit",
            "status": "completed", "requested_at": f"2026-09-19T20:0{index}:00Z",
        } for index in (2, 3)]])
        _git(self.root, "commit", "--allow-empty", "-m", "later fix\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.prepare()
        pull = self.remote.pull(1)
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        self.double.copilot_pending.clear()
        with self.assertRaisesRegex(ship.Refusal, "limit of 3") as caught:
            self.prepare("--copilot-review", "request")
        self.assertIn("abandon", caught.exception.workflow["next_action"])
        result = self.merge("--abandon-copilot-review", "request cap reached")
        self.assertEqual(result["phase"], "merged")
        state = self.operation().state
        abandonment = state["copilot_review_abandonments"][0]
        self.assertEqual(abandonment["head"], head)
        self.assertEqual(abandonment["request_digest"], self.operation().copilot_abandonment_digest(
            state["copilot_reviews"], head))

    def test_abandonment_refuses_without_a_local_request_receipt(self):
        self.prepare()
        with self.assertRaisesRegex(ship.Refusal, "no locally recorded Copilot review request"):
            self.merge("--abandon-copilot-review", "there is no request")
        self.assertNotIn("copilot_review_abandonments", self.operation().state)

    def test_later_request_on_same_head_supersedes_an_abandonment(self):
        self.enable_automatic_copilot()
        self.prepare()
        operation = self.operation("merge", "--manual", "--expected-head", _git(self.root, "rev-parse", "HEAD"),
                                   "--abandon-copilot-review", "first request did not complete")
        operation.abandon_copilot_review(1, _git(self.root, "rev-parse", "HEAD"))
        self.double.copilot_pending.clear()
        self.prepare("--copilot-review", "request")
        with self.assertRaisesRegex(ship.Refusal, "has not completed"):
            self.merge()

    def test_only_manual_authority_can_abandon_a_copilot_request(self):
        self.enable_automatic_copilot()
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        operation = self.operation("merge", "--expected-head", head,
                                   "--abandon-copilot-review", "chose to stop waiting")
        with self.assertRaisesRegex(ship.Refusal, "requires explicit manual merge authority"):
            operation.merge()

    def test_a_locked_database_once_does_not_fail_the_merge(self):
        """pack sd:6855: merge's first write meets one `database is locked`, retries, and merges."""
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")

        met = []

        class Locked:
            """A connection whose first `BEGIN IMMEDIATE` fails once, as a held write lock does."""
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def execute(self, sql, parameters=()):
                if not met and sql == "BEGIN IMMEDIATE":
                    met.append(sql)
                    raise sqlite3.OperationalError("database is locked")
                return self.connection.execute(sql, parameters)

        def locked_connect(*args, **kwargs):
            return Locked(connect(*args, **kwargs))

        output, previous = io.StringIO(), pathlib.Path.cwd()
        try:
            with (patch("sd_db.database.connect", locked_connect), patch("sd_db.database.LOCK_WAITS", (0.01,)),
                  contextlib.redirect_stdout(output)):
                code = ship.main(["-C", str(self.root), "merge", "--item", str(self.item), "--manual",
                                  "--expected-head", head, "--database", str(self.database), "--json"])
        finally:
            os.chdir(previous)
        self.assertEqual(met, ["BEGIN IMMEDIATE"], "the lock was met")
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["phase"], "merged")

    def test_merge_takes_no_runner_lease(self):
        """sd:3085. The sd runner is gone, so `--run` and its lease went with it."""
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        result = self.cli("merge", "--run", "fixture-run", "--expected-head", head)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("unrecognized arguments: --run", result.stderr)
        with self.assertRaisesRegex(ship.Refusal, "merge needs an explicit --manual invocation"):
            self.operation("merge", "--expected-head", head).merge()
        self.assertEqual(self.merge()["phase"], "merged")

    def test_abandoned_completed_review_marks_matching_receipt_complete(self):
        self.enable_automatic_copilot()
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        self.double.review_payload["reviews"] = [{
            "user": {"login": "Copilot"}, "commit_id": head, "state": "COMMENTED",
            "submitted_at": "2026-09-19T20:00:00Z", "body": "",
        }]
        with patch.object(ship.time, "sleep"):
            self.assertEqual(self.merge(
                "--abandon-copilot-review", "completion arrived after the decision")["phase"], "merged")
        state = self.operation().state
        self.assertEqual(state["copilot_reviews"][0]["status"], "completed")
        self.assertEqual(len(state["copilot_review_abandonments"]), 1)

    def test_pending_copilot_review_does_not_clear_merge(self):
        self.enable_automatic_copilot()
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        self.double.review_payload["reviews"] = [{
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
            "commit_id": head,
            "state": "PENDING",
            "submitted_at": None,
            "body": "",
        }]
        with self.assertRaisesRegex(ship.Refusal, "has not completed"):
            self.merge()

    def test_missing_copilot_review_head_has_a_precise_refusal(self):
        self.enable_automatic_copilot()
        self.prepare()
        self.double.review_payload["reviews"] = [{
            "user": {"login": "Copilot"},
            "commit_id": "a" * 40,
            "state": "COMMENTED",
            "submitted_at": "2026-09-19T20:00:00Z",
            "body": "",
        }]
        with self.assertRaisesRegex(ship.Refusal, "unavailable in local Git history"):
            self.merge()

    def test_latest_nonancestor_copilot_review_cannot_fall_back_to_an_older_one(self):
        self.enable_automatic_copilot()
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "checkout", "-b", "unrelated", "origin/main")
        _git(self.root, "commit", "--allow-empty", "-m", "unrelated\n\nAuthored-with: human")
        unrelated = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "checkout", "topic")
        self.double.review_payload["reviews"] = [{
            "user": {"login": "Copilot"},
            "commit_id": head,
            "state": "COMMENTED",
            "submitted_at": "2026-09-19T20:00:00Z",
            "body": "",
        }, {
            "user": {"login": "Copilot"},
            "commit_id": unrelated,
            "state": "COMMENTED",
            "submitted_at": "2026-09-19T20:01:00Z",
            "body": "",
        }]
        with self.assertRaisesRegex(ship.Refusal, "not an ancestor"):
            self.merge()

    def test_final_late_update_refuses_an_unstable_review_snapshot(self):
        self.enable_automatic_copilot()
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        base = {"user": {"login": "copilot-pull-request-reviewer[bot]"},
                "commit_id": head, "state": "COMMENTED",
                "submitted_at": "2026-09-19T20:00:00Z"}
        late = "| File | Summary |\n|---|---|\n| `src.py` | Moderate finding (2 votes): final result. |\n"
        self.double.review_sequences["reviews"] = [
            [{**base, "body": ""}],
            [{**base, "body": ""}],
            [{**base, "body": late}],
        ]
        with self.assertRaisesRegex(ship.Refusal, "did not stabilize"):
            with patch.object(ship.time, "sleep"):
                self.merge()

    def test_an_unresolved_review_thread_refuses_the_merge_by_name(self):
        """sd:3098. A COMMENTED review can report data loss; its open thread, outdated or not, holds the merge."""
        self.prepare()
        self.double.review_threads = [
            review_thread(path="src.py", line=3, author="reviewer", body="Data loss on retry.\nDetails."),
            review_thread(path="lib.py", line=None, original=7, outdated=True, author="copilot-pull-request-reviewer",
                          body="Stale cursor."),
            review_thread(resolved=True, path="done.py", body="Fixed."),
        ]
        with self.assertRaises(ship.Refusal) as caught:
            self.merge()
        message = str(caught.exception)
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "review_threads_open")
        self.assertIn("2 unresolved review thread(s) on #1", message)
        self.assertIn("src.py:3 reviewer: Data loss on retry.", message)
        self.assertIn("lib.py:7 copilot-pull-request-reviewer: Stale cursor.", message)
        self.assertNotIn("done.py", message)
        self.assertNotIn("Details.", message)
        self.assertEqual(self.remote.pull(1).state, "OPEN")

    def test_resolved_review_threads_let_the_merge_go_out(self):
        """sd:3098. A rebuttal is a reply plus a resolve; a resolved thread does not hold the merge."""
        self.prepare()
        self.double.review_threads = [review_thread(resolved=True), review_thread(resolved=True, outdated=True)]
        self.assertEqual(self.merge()["phase"], "merged")

    def test_a_failed_review_thread_read_refuses_the_merge(self):
        """sd:3098. A thread list that could not be read is never an empty one."""
        self.prepare()
        for answer in (RemoteRefusal(502, "bad gateway"),
                       {"errors": [{"message": "rate limited"}]},
                       {"data": {"repository": {"pullRequest": None}}},
                       {"data": {"repository": {"pullRequest": {"reviewThreads": {
                           "pageInfo": {"hasNextPage": False, "endCursor": None},
                           "nodes": [{"path": "src.py"}]}}}}},
                       {"data": {"repository": {"pullRequest": {"reviewThreads": {
                           "pageInfo": {"hasNextPage": True, "endCursor": None}, "nodes": []}}}}}):
            with self.subTest(answer=answer):
                self.double.threads_answer = answer
                with self.assertRaises(ship.Refusal) as caught:
                    self.merge()
                self.assertEqual(caught.exception.workflow["blocker"]["code"], "review_threads_unread")
                self.assertIn("review threads on #1 could not be read", str(caught.exception))
        self.assertEqual(self.remote.pull(1).state, "OPEN")

    def test_an_unresolved_thread_past_the_first_page_refuses_the_merge(self):
        """sd:3098. A hundred resolved threads fill page one; the open one on page two still holds the merge."""
        self.prepare()
        self.double.review_threads = [review_thread(resolved=True) for _ in range(100)] + [
            review_thread(path="late.py", line=9, author="reviewer", body="Second page.")]
        with self.assertRaisesRegex(ship.Refusal, r"1 unresolved review thread\(s\) on #1: late\.py:9 reviewer: Second page\."):
            self.merge()

    def test_the_real_cli_bound_guards_a_hang_not_a_load_average(self):
        """sd:1539. A 30 s bound failed three gates on one loaded afternoon; alone the run takes about 7 s."""
        self.assertGreaterEqual(CLI_TIMEOUT, 300)
        with patch.object(subprocess, "run") as run:
            self.cli("prepare")
        self.assertEqual(run.call_args.kwargs["timeout"], CLI_TIMEOUT)

    def test_real_cli_review_prepare_slice_merge_and_repeat_reconcile(self):
        prepared = self.cli("prepare", "--associate-only")
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        self.assertEqual(json.loads(prepared.stdout)["phase"], "ready_to_send")
        body = self.directory / "prepared-pr.md"
        body.write_text(self.remote.pull(1).body)
        (self.root / "docs/work").mkdir(parents=True)
        linted = subprocess.run([sys.executable, str(ROOT / "bin/sd-docs-lint"), "--pr-body", str(body)],
                                cwd=self.root, env=self.environment, text=True, capture_output=True, timeout=CLI_TIMEOUT)
        self.assertEqual(linted.returncode, 0, linted.stdout + linted.stderr)
        self.assertIn("sd-docs-lint: clean", linted.stdout)  # rule 5 is retired (sd:2999)
        result = self.merge()
        self.assertEqual(result["phase"], "merged")
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()[0], "in_progress")
        self.assertIn("Item: sd:", _git(self.root, "show", "-s", "--format=%B", result["merge_commit"]))
        self.assertNotIn("Delivers:", _git(self.root, "show", "-s", "--format=%B", result["merge_commit"]))
        count = len([call for call in self.remote.calls if call.method == "PUT"])
        self.operation("reconcile").reconcile()
        self.assertEqual(len([call for call in self.remote.calls if call.method == "PUT"]), count)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM note WHERE body LIKE 'Code delivery %'").fetchone()[0], 1)
        self.assertEqual(_git(self.root, "branch", "--show-current"), "topic")
        self.assertTrue(self.root.exists())

    def test_a_coowned_remote_merges_when_the_repository_row_says_auto(self):
        """sd:1347 -- the defect, end to end: a merge that used to be impossible.

        `sd-ship merge` refused sd:1337 in `example-org/benchmark` with
        "lets adrianfurlong, ... push too" -- repository ownership, asked at
        merge time, and its third question is false of every co-authored
        repository. The row said `auto` and nothing read it, so the Mezmo lane
        ended at `ready_to_send` and a human merged through the API by hand.

        Here the same remote names a second collaborator, the row says `auto`,
        and the merge lands. The receipt has to say why: a guard was loosened,
        and a reader must be able to tell this from a merge this account owned
        outright, without going back to the remote to find out who else may
        push.
        """

        self.prepare()
        self.remote.collaborators = [{"login": "fixture", "permissions": {"push": True}},
                                     {"login": "someone", "permissions": {"push": True}}]
        result = self.merge()
        self.assertEqual(result["phase"], "merged")
        authority = result["row_authorized_merge"]
        self.assertEqual(authority["runner_merge"], "auto")
        self.assertEqual(authority["other_pushers"], ["someone"])
        self.assertIn("someone", authority["remote_said"])
        self.assertEqual(authority["repository"], ship.slug(self.remote_url))
        # And the same sentence when a separate process reconciles the receipt.
        self.assertEqual(self.operation("reconcile").reconcile()["row_authorized_merge"], authority)

    def test_a_merge_this_account_owns_outright_claims_no_row_authority(self):
        """The regression guard: three yeses consult no row and claim none."""

        self.prepare()
        result = self.merge()
        self.assertEqual(result["phase"], "merged")
        self.assertNotIn("row_authorized_merge", result)

    def test_an_attempt_that_died_after_ownership_authorizes_no_later_merge(self):
        """Durable authority describes the merge dispatched, not the attempt allowed.

        Ownership clearing is not merging. CI, the review gate and the
        protection re-read all run after it and any of them can refuse -- on
        2026-09-22 both branches in flight came back blocking, so this is the
        ordinary case and not a corner. Recording the authority where the
        answer is read leaves it in the receipt of an attempt that never
        merged, and nothing on the ownership-only path takes it back: the next
        merge then reports a row authorization the row never gave for it, and
        `sd-ship reconcile` repeats it in a later process. A false line in an
        audit trail is worse than an absent one, because an absent one sends
        the reader to look.

        Here the first attempt clears ownership under the row and dies at the
        required check. The collaborator then goes, so the retry is owned
        outright and consults no row at all.
        """

        self.prepare()
        self.remote.collaborators = [{"login": "fixture", "permissions": {"push": True}},
                                     {"login": "someone", "permissions": {"push": True}}]
        pull = self.remote.pull(1)
        healthy = [dict(check) for check in pull.checks]
        pull.checks = [{**healthy[0], "conclusion": "failure"}]
        with self.assertRaises(ship.Refusal):
            self.merge()
        pull.checks = healthy
        self.remote.collaborators = [{"login": "fixture", "permissions": {"push": True}}]
        result = self.merge()
        self.assertEqual(result["phase"], "merged")
        self.assertNotIn("row_authorized_merge", result,
                         "an ownership-only merge must not inherit a failed attempt's authority")
        self.assertNotIn("row_authorized_merge", self.operation("reconcile").reconcile())

    def test_required_ci_failure_missing_wrong_head_or_app_cannot_merge(self):
        self.prepare()
        pull = self.remote.pull(1)
        valid = dict(pull.checks[0])
        for changes in ({"conclusion": "failure"}, {"status": "queued"}, {"head_sha": "0" * 40}, {"app": {"id": 8}}):
            with self.subTest(changes=changes):
                pull.checks = [{**valid, **changes}]
                with self.assertRaises(ship.Refusal):
                    self.merge()
        pull.checks = []
        with self.assertRaisesRegex(ship.Refusal, "no current result"):
            self.merge()
        self.assertFalse(any(call.method == "PUT" for call in self.remote.calls))

    def test_two_suites_that_disagree_still_refuse_and_name_the_fresh_commit(self):
        """sd:1779. A body edit or a close/reopen starts a second check suite on
        the same head; its pass does not clear the first suite's failure, and
        a re-run of that failure replays its stale event (#1221 at 59f8fde:
        suites 98314641878 and 98314947143). The gate still refuses with the
        same code; the next action names the remedy, a fresh commit. One
        failing run alone, two failures, or a pass inside the failing suite
        gets no such hint."""
        self.prepare()
        pull = self.remote.pull(1)
        valid = dict(pull.checks[0])
        failed = {**valid, "conclusion": "failure", "check_suite": {"id": 98314641878}}
        passed = {**valid, "check_suite": {"id": 98314947143}}
        for label, checks, hinted in (
            ("two suites disagree", [failed, passed], True),
            ("two suites disagree, listed the other way", [passed, failed], True),
            ("one failing run", [failed], False),
            ("both suites fail", [failed, {**passed, "conclusion": "failure"}], False),
            ("one suite, no suite ids", [{**valid, "conclusion": "failure"}, dict(valid)], False),
            ("one suite both fails and passes", [failed, {**valid, "check_suite": failed["check_suite"]}], False),
        ):
            with self.subTest(label=label):
                pull.checks = checks
                with self.assertRaisesRegex(ship.Refusal, "required CI is not passing") as caught:
                    self.merge()
                workflow = caught.exception.workflow
                self.assertEqual(workflow["blocker"]["code"], "ci_not_passing")
                self.assertEqual("push a fresh commit" in workflow["next_action"], hinted, workflow["next_action"])
        self.assertFalse(any(call.method == "PUT" for call in self.remote.calls))

    def test_remote_ownership_protection_and_rules_are_fresh(self):
        self.prepare()
        self.double.admin = False
        with self.assertRaisesRegex(ship.Refusal, "administer"):
            self.merge()
        self.double.admin = True
        # sd:1347 -- co-ownership is the one answer the repository row may
        # override, and this fixture's row says `auto`. `manual` is what makes
        # it a refusal again; the override's own case is
        # `test_a_coowned_remote_merges_when_the_repository_row_says_auto`.
        set_runner_merge(self.connection, str(self.operator), "manual")
        self.remote.collaborators = [{"login": "fixture", "permissions": {"push": True}}] * 100 + [{"login": "someone", "permissions": {"push": True}}]
        with self.assertRaisesRegex(ship.Refusal, "someone"):
            self.merge()
        set_runner_merge(self.connection, str(self.operator), "auto")
        self.remote.collaborators = []
        saved = self.remote.protection
        self.remote.protection = None
        # An unprotected branch is a 404 from GitHub. `GitHub.gate` reads the
        # status and, with no declaration at the reviewed head (this fixture
        # commits none), refuses with GitHub's message and the status; the
        # guard that refuses a body that is not an object is reached only
        # from `tests/test_sd_ship_remote.py` (sd:929). The declared-gap path
        # is `DeclaredGapCase` below (sd:1110).
        with self.assertRaisesRegex(ship.Refusal, "Branch not protected"):
            self.merge()
        self.remote.protection = saved
        self.remote.protection["enforce_admins"]["enabled"] = False
        with self.assertRaisesRegex(ship.Refusal, "administrators"):
            self.merge()

    def test_moved_head_at_remote_merge_is_refused_atomically(self):
        self.prepare()
        original = self.remote.rev_parse("main")
        self.double.moved = True
        with self.assertRaisesRegex(ship.Refusal, "not confirmed"):
            self.merge()
        self.assertEqual(self.remote.rev_parse("main"), original)
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()[0], "in_progress")

    def test_a_remote_head_that_moved_before_merge_is_refused_with_no_merge_call(self):
        """The shared readiness guard answers before a merge is ever dispatched.

        `test_moved_head_at_remote_merge_is_refused_atomically` races the head
        *inside* the merge call, so the remote's own 405 is what refuses there.
        This is the earlier question: the branch already moved at the remote
        when the merge is asked for. `GitHub.ready` is the one guard both the
        item-backed and the no-item flow reach, and it has to refuse on the
        pull request's own head without spending a merge call to find out.
        """
        self.prepare()
        self.remote.commit_on("topic", "raced\n\nAuthored-with: human", files={"raced.py": "changed\n"})
        with self.assertRaisesRegex(ship.Refusal, "head or default base moved after local review"):
            self.merge()
        self.assertFalse(any(call.method == "PUT" for call in self.remote.calls))

    def test_the_manual_merge_request_names_the_expected_head_and_a_stale_one_is_refused(self):
        """`--expected-head` has to reach GitHub as the request's own `sha`.

        The documented manual form is `sd-ship merge --item ID --expected-head
        SHA --manual`, and the head it names guards nothing unless the
        dispatched request carries it: GitHub answers 405 when `sha` is not the
        pull request's current head. Both halves are asserted here, that the
        body names the reviewed head, and that a server-side head which no
        longer matches it refuses rather than merges.
        """
        self.prepare()
        head = _git(self.root, "rev-parse", "HEAD")
        self.double.moved = True
        with self.assertRaisesRegex(ship.Refusal, "has not confirmed this pull request merged"):
            self.merge()
        dispatched = [call for call in self.remote.calls if call.method == "PUT"]
        self.assertEqual([call.body["sha"] for call in dispatched], [head])
        self.assertNotEqual(self.remote.pull(1).state, "MERGED")
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()[0], "in_progress")

    def test_lost_merge_response_reconciles_without_a_second_merge(self):
        self.prepare()
        self.double.lose_merge = True
        self.assertEqual(self.merge()["phase"], "merged")
        self.assertEqual(self.merge()["phase"], "merged")
        self.assertEqual(len([call for call in self.remote.calls if call.method == "PUT"]), 1)

    def test_prepare_and_merge_receipts_name_their_invoker(self):
        """sd:2078. A merge nobody claimed could not be traced: its receipt
        named no pid, checkout, authority or lock holder."""
        prepared = self.prepare()
        self.assertEqual((prepared["invoker"]["pid"], prepared["invoker"]["checkout"]), (os.getpid(), str(self.root)))
        self.assertIsNone(prepared["invoker"]["lock_holder"])  # prepare takes no ship lock (sd:1938)
        merged = self.merge()
        invoker = merged["invoker"]
        self.assertEqual((invoker["pid"], invoker["ppid"], invoker["authority"]), (os.getpid(), os.getppid(), "--manual"))
        self.assertEqual(invoker["lock_holder"]["command"], f"sd-ship merge --item {self.item}")
        self.assertEqual(invoker["started_at"], ship.PROCESS_STARTED)
        [evidence] = self.connection.execute("SELECT body FROM state WHERE kind='verified' AND key=?",
                                             (f"code-delivery:{self.item}:{merged['merge_commit']}",)).fetchall()
        self.assertEqual(json.loads(evidence[0])["invoker"], invoker)  # the note holds only its line (sd:3508)

    def test_a_merge_commit_outside_the_default_branch_names_both_commits(self):
        """`--is-ancestor` says "no" by exit status alone; the reconcile check
        read that as an empty, retryable "git failed" (sd:1461)."""
        self.prepare()
        saved = self.double._route

        def route(method, path, body):
            answer = saved(method, path, body)
            if method == "PUT" and path.endswith("/merge"):
                # The clone holds the merge commit; origin's main no longer does.
                _git(self.root, "fetch", "-q", str(self.remote.path), "main")
                _git(self.remote.path, "update-ref", "refs/heads/main", f"{answer[1]['sha']}^")
            return answer
        self.double._route = route
        with self.assertRaises(ship.Refusal) as caught:
            self.merge()
        commit = self.remote.pull(1).merge_commit_sha
        tip = _git(self.remote.path, "rev-parse", "refs/heads/main")
        self.assertIn(f"merge commit {commit} is not an ancestor of origin/main at {tip}", str(caught.exception))
        blocker = caught.exception.workflow["blocker"]
        self.assertEqual((blocker["code"], blocker["retryable"]), ("merge_commit_unreachable", False))
        self.assertIn(f"restore {commit}", caught.exception.workflow["next_action"])

    def test_a_merge_touching_the_library_installs_nothing(self):
        """sd:3280. `bin/` reads `lib/` at the checkout's commit, so a library
        merge has no copy to refresh: the reconcile records no `library` and the
        installer has no post-merge door left (sd:2108's reprovision is gone)."""
        import sd_install
        library = self.root / "lib/sd_db/writing.py"
        library.parent.mkdir(parents=True)
        library.write_text("STAGES = ()\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "touch the library\n\nAuthored-with: human")
        self.prepare()
        merged = self.merge()
        self.assertNotIn("library", merged)
        self.assertFalse(hasattr(sd_install, "reprovision_after_merge"))

    def test_a_base_that_advanced_under_the_put_holds_delivery(self):
        """GitHub squashes onto the base it holds at the `PUT`, not the one the
        freshness reads saw. A same-file advance between them lands a combined
        tree nobody reviewed, so the merge is recorded and delivery waits (sd:1089)."""
        self.remote.commit_on("main", "shared\n\nAuthored-with: human", files={"shared.py": "a = 1\nb = 2\nc = 3\nd = 4\n"})
        _git(self.root, "pull", "-q", "--no-rebase", "--no-edit", str(self.remote.path), "main")
        (self.root / "shared.py").write_text("a = 10\nb = 2\nc = 3\nd = 4\n")
        _git(self.root, "commit", "-qam", "topic edits shared\n\nAuthored-with: human")
        acceptance = self.directory / "acceptance.json"
        acceptance.write_text(json.dumps({"item": self.item, "complete": True, "criteria": [{"criterion": "actual scope", "passed": True, "evidence": "fixture-check-pass"}]}))
        self.prepare("--deliver", "--acceptance-file", str(acceptance))
        saved = self.double._route

        def route(method, path, body):
            if method == "PUT" and path.endswith("/merge"):
                self.remote.commit_on("main", "raced\n\nAuthored-with: human", files={"shared.py": "a = 1\nb = 2\nc = 3\nd = 40\n"})
            return saved(method, path, body)
        self.double._route = route
        result = self.merge()
        self.assertEqual(result["phase"], "merged")
        self.assertTrue(result["delivery_pending"])
        self.assertEqual(result["workflow"]["blocker"]["code"], "base_advanced_at_merge")
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()[0], "in_progress")
        self.assertEqual(len([call for call in self.remote.calls if call.method == "PUT"]), 1)

    def held_merge(self) -> dict:
        """A `--deliver` merge whose base advances under the `PUT` (sd:1089)."""
        acceptance = self.directory / "acceptance.json"
        acceptance.write_text(json.dumps({"item": self.item, "complete": True, "criteria": [{"criterion": "actual scope", "passed": True, "evidence": "fixture-check-pass"}]}))
        self.prepare("--deliver", "--acceptance-file", str(acceptance))
        saved = self.double._route

        def route(method, path, body):
            if method == "PUT" and path.endswith("/merge"):
                self.remote.commit_on("main", "raced\n\nAuthored-with: human", files={"raced.py": "x = 1\n"})
            return saved(method, path, body)
        self.double._route = route
        result = self.merge()
        self.assertEqual(result["workflow"]["blocker"]["code"], "base_advanced_at_merge")
        return result

    def test_a_held_squash_does_not_read_as_delivered_in_git(self):
        """The squash carries `Delivers:` because its message is fixed before the
        `PUT`. Git readers must see the same hold the row does, or a checkout
        with no database reads the held item as done (#1179)."""
        self.held_merge()
        self.assertNotEqual(ship.sd_lib.delivered(self.root, f"sd:{self.item}"), ship.sd_lib.YES)

    def test_a_held_merge_names_each_co_delivered_item_with_its_repository(self):
        """sd:5124: the next step a person reads names each item it asks them to close as `operator sd:N`."""
        closes = self.closes_items()
        acceptance = self.directory / "acceptance.json"
        acceptance.write_text(json.dumps({"item": self.item, "complete": True, "criteria": [{"criterion": "actual scope", "passed": True, "evidence": "fixture-check-pass"}]}))
        self.prepare("--deliver", "--acceptance-file", str(acceptance), *self.also(*closes))
        saved = self.double._route

        def route(method, path, body):
            if method == "PUT" and path.endswith("/merge"):
                self.remote.commit_on("main", "raced\n\nAuthored-with: human", files={"raced.py": "x = 1\n"})
            return saved(method, path, body)
        self.double._route = route
        result = self.merge()
        self.assertEqual(result["workflow"]["blocker"]["code"], "base_advanced_at_merge")
        named = ", ".join(f"{self.operator.name} sd:{number}" for number in closes)
        self.assertIn(f"close the co-delivered items {named} the same way", result["workflow"]["next_action"])

    def test_an_unheld_squash_still_reads_as_delivered_in_git(self):
        acceptance = self.directory / "acceptance.json"
        acceptance.write_text(json.dumps({"item": self.item, "complete": True, "criteria": [{"criterion": "actual scope", "passed": True, "evidence": "fixture-check-pass"}]}))
        self.prepare("--deliver", "--acceptance-file", str(acceptance))
        self.assertFalse(self.merge()["delivery_pending"])
        self.assertEqual(ship.sd_lib.delivered(self.root, f"sd:{self.item}"), ship.sd_lib.YES)

    def test_a_hand_delivery_clears_the_hold(self):
        """The operator verifies the combined tree and delivers by hand; a later
        reconcile reads that completion instead of reissuing the hold (#1179)."""
        from sd_db.progress import deliver_work
        commit = self.held_merge()["merge_commit"]
        deliver_work(self.connection, self.item, commit, who="operator", verification_root=self.root)
        again = self.merge()
        self.assertFalse(again["delivery_pending"])
        self.assertIsNone(again["workflow"]["blocker"])

    # -- sd:1928: a first prepare says whether it delivers ------------------

    def unanswered(self, *extra):
        """A prepare exactly as the command line builds it, with no default claim."""
        return ship.Ship(self.root, self.connection, self.database, self.args("prepare", *extra))

    def test_a_first_prepare_without_a_claim_refuses_before_any_write(self):
        """sd:1910 merged with `Item:` only because associate-only was the
        silent default. The first prepare now asks, and asks before it commits,
        pushes or opens anything."""
        head = _git(self.root, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ship.Refusal, r"--deliver.*--associate-only") as caught:
            self.unanswered().prepare()
        self.assertIn("last", str(caught.exception))
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(receipts.read(self.connection, receipts.receipt_key(self.remote.slug, "topic", self.item)), (0, {}))
        self.assertFalse([call for call in self.remote.calls if call.method in ("POST", "PUT", "PATCH")])

    def test_the_command_line_takes_one_claim_only(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.args("prepare", "--deliver", "--associate-only")

    def test_associate_only_is_an_answer_and_lands_item_without_delivers(self):
        self.unanswered("--associate-only").prepare()
        self.assertFalse(self.operation().state["deliver"])
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        message = _git(self.remote.path, "log", "-1", "--format=%B", "main")
        self.assertIn(f"Item: sd:{self.item}", message)
        self.assertNotIn("Delivers:", message)
        self.assertFalse(result["delivery_pending"])
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()[0], "in_progress")

    def test_a_reprepare_keeps_the_claim_the_first_prepare_stored(self):
        self.unanswered("--associate-only").prepare()
        _git(self.root, "commit", "--allow-empty", "-m", "more\n\nAuthored-with: human")
        self.unanswered().prepare()
        self.assertFalse(self.operation().state["deliver"])

    def test_an_owned_runner_prepare_keeps_the_associate_only_default(self):
        """The runner passes `--database` and no claim flag; it records its own
        delivery proof, so its prepare is unchanged."""
        self.unanswered("--database", str(self.database)).prepare()
        self.assertFalse(self.operation().state["deliver"])

    def task_item(self, kind: str) -> None:
        self.connection.execute("UPDATE item SET branch = NULL WHERE id = ?", (self.item,))
        self.connection.commit()
        self.item = create_item(self.connection, kind=kind, title=f"fixture {kind}", status="in_progress",
                                repo=str(self.operator), branch="topic")

    def assert_closed_by_merge(self, result: dict) -> None:
        self.assertFalse(result["delivery_pending"], result)
        commit = result["merge_commit"]
        message = _git(self.remote.path, "log", "-1", "--format=%B", "main")
        self.assertIn(f"Delivers: sd:{self.item}", message)
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()[0], "done")
        reasons = [row[0] for row in self.connection.execute(
            "SELECT body FROM note WHERE item = ? AND kind = 'status_change'", (self.item,))]
        import sd_work
        # The sentence `sd task status N done --delivered-by` writes, spelled once there.
        self.assertIn(f"in_progress -> done by sd-ship: {sd_work.DELIVERY_REASON.format(commit=commit, ref='origin/main')}",
                      reasons)

    def test_a_task_delivers_on_merge_with_the_delivered_by_sentence(self):
        """Tasks refused `--deliver`, so every task merge needed a hand
        `sd task status N done`, and `--delivered-by` then refused the squash
        for want of `Delivers:`. A task takes `--deliver` without criteria and
        closes on the verified merge."""
        self.task_item("task")
        self.unanswered("--deliver").prepare()
        with patch.object(ship.time, "sleep"):
            self.assert_closed_by_merge(self.merge())

    def test_a_delivery_resolves_only_the_followups_it_names_or_the_lanes(self):
        """sd:9869. A merge closed an item and resolved two followups that
        still waited on an outside reply. A delivery resolves a followup its
        message names as `#<id>`, and the lane's own blockers; others stay."""
        self.task_item("task")
        waiting = sd_db.add_note(self.connection, self.item, "followup", "Waits on the reviewer's reply")
        sd_db.add_note(self.connection, self.item, "followup", "Lane failed in prepare: gate red")
        named = sd_db.add_note(self.connection, self.item, "followup", "Rename the flag")
        self.connection.commit()
        body = self.directory / "body.md"
        body.write_text(f"Renames the flag; settles followup #{named}.\n")
        self.unanswered("--deliver", "--title", "Rename", "--body-file", str(body)).prepare()
        with patch.object(ship.time, "sleep"):
            self.merge()
        self.assertEqual(self.status_of(self.item), "done")
        still = {row[0] for row in self.connection.execute(
            "SELECT id FROM note WHERE item = ? AND kind = 'followup' AND resolved_at IS NULL", (self.item,))}
        self.assertEqual(still, {waiting})

    def test_a_design_only_branch_cannot_deliver_its_build_item(self):
        """sd:3519. PR #260 changed docs/work/ and CLAUDE.md, merged with
        `--deliver`, and closed two build items nobody built. A branch of
        design records and instruction files associates; the build delivers."""
        self.tiered_branch({"docs/work/2026-10-12-example/design.md": "---\nitem: sd:1\n---\n\ndesign\n",
                            "CLAUDE.md": "rules\n"})
        self.task_item("task")
        self.connection.execute("UPDATE item SET branch = 'tiered' WHERE id = ?", (self.item,))
        self.connection.commit()
        with self.assertRaisesRegex(ship.Refusal, r"design records.*--associate-only") as caught:
            self.unanswered("--deliver", "--title", "design").prepare()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "design_only_delivery")
        self.assertEqual(receipts.read(self.connection, receipts.receipt_key(self.remote.slug, "tiered", self.item)), (0, {}))
        self.assertFalse([call for call in self.remote.calls if call.method in ("POST", "PUT", "PATCH")])
        with contextlib.redirect_stderr(io.StringIO()):
            self.unanswered("--associate-only", "--title", "design").prepare()
        self.assertFalse(self.operation().state["deliver"])

    def test_design_only_needs_a_design_record_and_nothing_built(self):
        self.assertTrue(ship.design_only(["docs/work/x/design.md", "AGENTS.md", ".claude/rules/a.md"]))
        for paths in (["CLAUDE.md"], ["docs/work/x/design.md", "bin/sd"], ["docs/work/x/design.md", "README.md"], []):
            with self.subTest(paths=paths):
                self.assertFalse(ship.design_only(paths))

    def test_prepare_names_other_work_on_the_item_or_its_files_and_refuses_nothing(self):
        """sd:1151. #1120 and #1122 fixed one defect from two sessions; both
        branches were on origin first. Prepare names each overlap before the
        review spends a pass, and publishes anyway."""
        self.remote.commit_on(f"sd-{self.item}-other-session", "same fix\n\nAuthored-with: human", files={"other.py": "x = 1\n"})
        self.remote.open_pull_request(f"sd-{self.item}-other-session", base="main", title=f"Same fix (sd:{self.item})", body="")
        self.remote.commit_on("shares-a-file", "touches src\n\nAuthored-with: human", files={"src.py": "value = 9\n"})
        sharing = self.remote.open_pull_request("shares-a-file", base="main", title="Unrelated", body="")
        self.double.pull_files[sharing.number] = ["src.py"]
        self.remote.commit_on(f"fix/sd{self.item}-draft", "claimed\n\nAuthored-with: human", files={"draft.py": "y = 1\n"})
        self.remote.commit_on("unrelated", "nothing shared\n\nAuthored-with: human", files={"elsewhere.py": "z = 1\n"})
        self.remote.open_pull_request("unrelated", base="main", title=f"Mentions sd:{self.item}0 only", body="")
        result = self.prepare()
        self.assertEqual(result["phase"], "ready_to_send")
        claims = [line for line in self.operation().state["warnings"] if line.startswith("claim check:")]
        self.assertEqual(claims, [
            f"claim check: open pull request #1 (sd-{self.item}-other-session) names sd:{self.item}",
            f"claim check: open pull request #{sharing.number} (shares-a-file) also changes src.py",
            f"claim check: origin branch fix/sd{self.item}-draft names sd:{self.item} and has no open pull request",
        ])

    def closes_items(self) -> tuple[int, ...]:
        """A task, a followup and a work item in the claimed item's repository."""
        return tuple(create_item(self.connection, kind=kind, title=f"co-delivered {kind}", status="planning",
                                 repo=str(self.operator)) for kind in ("task", "followup", "work"))

    def test_a_merge_removes_the_remote_branch_and_keeps_the_main_checkout(self):
        """sd:3006. The fixture ships from its main checkout, which is never removed,
        so the branch it has checked out stays too; the remote branch goes."""
        self.prepare()
        cleanup = self.merge()["cleanup"]
        self.assertEqual(cleanup["removed"], ["origin/topic"])
        self.assertEqual(cleanup["kept"], {str(self.root): "the main checkout", "topic": "a kept worktree has it checked out"})
        self.assertEqual("", _git(self.root, "ls-remote", "origin", "refs/heads/topic"))

    def test_a_merge_cleanup_keeps_a_remote_branch_pushed_after_its_check(self):
        """sd:8827: a push between `ls-remote` and the delete lost that commit; the delete carries a lease."""
        head = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "push", "-q", "origin", "HEAD:refs/heads/raced")
        real = ship.git

        def racing(root, *args):
            answer = real(root, *args)
            if args[:1] == ("ls-remote",):  # another session pushes right after the check
                _git(self.root, "commit", "-q", "--allow-empty", "-m", "late\n\nAuthored-with: human")
                _git(self.root, "push", "-q", "origin", "HEAD:refs/heads/raced")
                _git(self.root, "reset", "-q", "--hard", head)
            return answer

        with patch.object(ship, "git", racing):
            cleanup = ship.clean_up_merged(self.root, "raced", head)
        self.assertNotIn("origin/raced", cleanup.get("removed", []), cleanup)
        self.assertNotEqual("", _git(self.root, "ls-remote", "origin", "refs/heads/raced"), "the late commit stays")

    def merged_worktree(self, name: str) -> pathlib.Path:
        """A linked worktree on a branch `name` whose head origin carries, as a merged pull request's is."""
        tree = self.directory / name
        _git(self.root, "worktree", "add", "-q", "-b", name, str(tree))
        (tree / "target").mkdir()
        (tree / "target/build.o").write_text("output")
        _git(self.root, "push", "-q", "origin", name)
        return tree

    def test_a_merge_cleanup_removes_a_clean_worktree_and_keeps_a_dirty_or_busy_one(self):
        """sd:3006, operator ruling 2026-10-08: leads removed these by hand after each merge."""
        head = _git(self.root, "rev-parse", "HEAD")
        with (self.root / ".git/info/exclude").open("a") as stream:
            stream.write("target/\n")
        clean = self.merged_worktree("clean")
        self.assertEqual(ship.clean_up_merged(self.root, "clean", head),
                         {"removed": [str(clean), "clean", "origin/clean"], "kept": {}})
        self.assertFalse(clean.exists())
        dirty = self.merged_worktree("dirty")
        (dirty / "notes.txt").write_text("unsaved")
        busy = self.merged_worktree("busy")
        ahead = self.merged_worktree("ahead")
        _git(ahead, "commit", "-q", "--allow-empty", "-m", "not pushed\n\nAuthored-with: human")
        sleeper = subprocess.Popen(["sleep", "60"], cwd=busy)
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        for name, tree, why in (("dirty", dirty, "it has uncommitted changes"),
                                ("busy", busy, f"process {sleeper.pid} works inside it"),
                                ("ahead", ahead, "it has commits past the merged head")):
            with self.subTest(name):
                cleanup = ship.clean_up_merged(self.root, name, head)
                self.assertEqual(cleanup["kept"], {str(tree): why, name: "a kept worktree has it checked out"})
                self.assertEqual(cleanup["removed"], [f"origin/{name}"])
                self.assertTrue(tree.exists())

    def test_a_merge_cleanup_keeps_the_worktree_it_runs_in(self):
        """sd:3477. The lane merges in the builder's own worktree (`-C <entry worktree>`), and a
        harness that resets its cwd between calls left no process inside: an idle builder lost its
        directory mid-task. The entry's `remove` command takes it once the builder stops."""
        head = _git(self.root, "rev-parse", "HEAD")
        with (self.root / ".git/info/exclude").open("a") as stream:
            stream.write("target/\n")
        tree = self.merged_worktree("builder")
        with contextlib.chdir(self.root):  # no process inside the tree, as between a builder's tool calls
            cleanup = ship.clean_up_merged(tree, "builder", head)
        self.assertTrue(tree.exists(), cleanup)
        self.assertEqual(cleanup["removed"], ["origin/builder"])
        self.assertEqual(cleanup["kept"], {str(tree): ship.MERGE_CHECKOUT,
                                           "builder": "a kept worktree has it checked out"})

    def test_a_merge_cleanup_keeps_a_worktree_holding_ignored_data_that_is_not_build_output(self):
        """Review of sd:3013: `git status --porcelain` hides ignored files, and
        `git worktree remove` deleted ignored local data with the tree."""
        head = _git(self.root, "rev-parse", "HEAD")
        with (self.root / ".git/info/exclude").open("a") as stream:
            stream.write("target/\n*.env\n")
        tree = self.merged_worktree("ignored")
        (tree / "local.env").write_text("TOKEN=change-me\n")
        cleanup = ship.clean_up_merged(self.root, "ignored", head)
        self.assertEqual(cleanup["kept"], {str(tree): "it holds ignored local.env",
                                           "ignored": "a kept worktree has it checked out"})
        self.assertTrue((tree / "local.env").exists())

    def cleanup_keeps(self, name: str, configure, why: str) -> pathlib.Path:
        """Run the merge cleanup on a worktree `configure(tree)` set up, and assert it stays for `why`."""
        head = _git(self.root, "rev-parse", "HEAD")
        with (self.root / ".git/info/exclude").open("a") as stream:
            stream.write("target/\n")
        tree = self.merged_worktree(name)
        configure(tree)
        cleanup = ship.clean_up_merged(self.root, name, head)
        self.assertEqual(cleanup.get("kept"), {str(tree): why, name: "a kept worktree has it checked out"}, cleanup)
        self.assertTrue(tree.exists())
        return tree

    def test_cleanup_sees_an_untracked_file_that_status_config_hides(self):
        """Cleanup failure table, row S1: `status.showUntrackedFiles=no` hid it, and `worktree remove` deleted it."""
        def configure(tree):
            _git(tree, "config", "status.showUntrackedFiles", "no")
            (tree / "notes.txt").write_text("unsaved")
        self.cleanup_keeps("hidden-untracked", configure, "it has uncommitted changes")

    def test_cleanup_sees_ignored_data_that_status_config_hides(self):
        """Cleanup failure table, row S2: review round 2 of sd:3013."""
        def configure(tree):
            _git(tree, "config", "status.showUntrackedFiles", "no")
            with (self.root / ".git/info/exclude").open("a") as stream:
                stream.write("*.env\n")
            (tree / "local.env").write_text("TOKEN=change-me\n")
        self.cleanup_keeps("hidden-ignored", configure, "it holds ignored local.env")

    def test_cleanup_sees_data_a_global_excludes_file_ignores(self):
        """Cleanup failure table, row S3: `core.excludesFile` turns untracked data into ignored data."""
        def configure(tree):
            excludes = self.directory / "global-excludes"
            excludes.write_text("*.env\n")
            _git(tree, "config", "core.excludesFile", str(excludes))
            (tree / "local.env").write_text("TOKEN=change-me\n")
        self.cleanup_keeps("global-ignored", configure, "it holds ignored local.env")

    def test_cleanup_never_consults_a_repositorys_fsmonitor(self):
        """Cleanup failure table, row S4: a daemon's stale answer is the one status would read."""
        head = _git(self.root, "rev-parse", "HEAD")
        with (self.root / ".git/info/exclude").open("a") as stream:
            stream.write("target/\n")
        tree = self.merged_worktree("monitored")
        marker, hook = self.directory / "fsmonitor-ran", self.directory / "fsmonitor.sh"
        hook.write_text(f'#!/bin/sh\necho "$PWD" >> "{marker}"\nexit 1\n')
        hook.chmod(0o755)
        _git(tree, "config", "core.fsmonitor", str(hook))
        self.assertEqual(ship.clean_up_merged(self.root, "monitored", head)["kept"], {})
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")

    def test_cleanup_keeps_a_worktree_with_a_submodule(self):
        """Cleanup failure table, row S5: `submodule.*.ignore=all` hides a dirty submodule from status."""
        _git(self.root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(self.remote.path), "sub")
        _git(self.root, "commit", "-qm", "submodule\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        tree = self.merged_worktree("with-submodule")
        _git(tree, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
        _git(tree, "config", "submodule.sub.ignore", "all")
        (tree / "sub/scratch.txt").write_text("unsaved")
        cleanup = ship.clean_up_merged(self.root, "with-submodule", head)
        self.assertNotIn(str(tree), cleanup.get("removed", []), cleanup)
        self.assertTrue((tree / "sub/scratch.txt").exists())
        self.assertEqual(cleanup.get("kept", {}).get(str(tree)), "it has uncommitted changes", cleanup)

    def status_of(self, number: int) -> str:
        return self.connection.execute("SELECT status FROM item WHERE id=?", (number,)).fetchone()[0]

    def also(self, *numbers: int) -> list[str]:
        """The later `--item` flags the lane passes for the items its enqueue named (sd:3704)."""
        return [part for number in numbers for part in ("--item", str(number))]

    def test_prepare_takes_the_first_item_as_its_claim_and_the_rest_as_its_closing_set(self):
        args = ship.parser().parse_args(["prepare", "--item", "4", "--item", "5", "--item", "6", "--deliver"])
        self.assertEqual((args.item, args.closes), (4, [5, 6]))
        self.assertEqual(ship.parser().parse_args(["prepare", "--item", "4", "--deliver"]).closes, [])
        lane = ship.parser().parse_args(["lane", "enqueue", "--item", "4", "--item", "5", "--title", "t", "--body-file", "b"])
        self.assertEqual(lane.items, [4, 5])

    def test_a_merge_closes_every_item_its_enqueue_named(self):
        """sd:3704, operator ruling 2026-10-13. A consumer repo's PR #366 and
        ship-fixes 48e4ccfd5 each left delivered items open: neither the title nor
        the commit lines named them all. The `--item` list on the final head is the
        one confirmation; prepare stores it with that head and prints it."""
        self.task_item("task")
        batched = self.closes_items()
        related = create_item(self.connection, kind="task", title="related", status="planning", repo=str(self.operator))
        body = self.directory / "body.md"
        body.write_text(f"Three fixes in one change.\n\nRefs: sd:{related}\n")
        head = _git(self.root, "rev-parse", "HEAD")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = self.unanswered("--deliver", "--title", "One change", "--body-file", str(body),
                                     *self.also(*batched)).prepare()
        self.assertEqual(self.operation().state["closing"], {"head": head, "items": list(batched)})
        named = [f"sd:{number}" for number in (self.item, *batched)]
        self.assertEqual(result["closes"], named)
        self.assertIn(f"sd-ship: at {head[:12]} this merge closes {', '.join(named)}", stderr.getvalue())
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assert_closed_by_merge(result)
        self.assertEqual(result.get("closed_items"), named[1:])
        landed = _git(self.remote.path, "rev-parse", "main")
        block = ship.sd_lib.trailer_block(_git(self.remote.path, "log", "-1", "--format=%B", "main")).splitlines()
        for number in batched:
            with self.subTest(item=number):
                # One generated `Delivers:` per closed item, so --delivered-by reads the squash.
                self.assertIn(f"Delivers: sd:{number}", block)
                self.assertTrue(ship.sd_lib._closes(_git(self.remote.path, "log", "-1", "--format=%B", landed),
                                                    f"sd:{number}"))
                self.assertEqual(self.status_of(number), "done")
        for number in batched[:2]:  # the task and the followup; a work item records its acceptance instead
            reasons = [row[0] for row in self.connection.execute(
                "SELECT body FROM note WHERE item = ? AND kind = 'status_change'", (number,))]
            self.assertIn(f"planning -> done by sd-ship: delivered at {result['merge_commit']} on origin/main", reasons)
        # A `Refs:` item is related, not delivered: no trailer, still open.
        self.assertNotIn(f"Delivers: sd:{related}", block)
        self.assertEqual(self.status_of(related), "planning")
        again = self.operation("reconcile").reconcile()
        self.assertFalse(again["delivery_pending"], again)

    def test_an_item_named_only_in_a_commit_line_or_the_title_stays_open(self):
        """sd:3704, replacing the sd:3014 title close and the commit-trailer rule.
        A title is prose (#353), and a commit line is history the final head may
        have undone; neither closes. Prepare warns once about the title's ids."""
        self.task_item("task")
        in_title, in_commit = self.closes_items()[:2]
        _git(self.root, "commit", "--allow-empty", "-m", f"fix for sd:{in_commit}\n\nDelivers: sd:{in_commit}")
        self.unanswered("--deliver", "--title", f"Two fixes (sd:{self.item}, sd:{in_title})").prepare()
        self.assertIn(f"the title names sd:{in_title}; a title closes nothing, so name each with --item",
                      self.operation().state["warnings"])
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assert_closed_by_merge(result)
        self.assertFalse(result.get("closed_items"))
        block = ship.sd_lib.trailer_block(_git(self.remote.path, "log", "-1", "--format=%B", "main")).splitlines()
        for number in (in_title, in_commit):
            with self.subTest(item=number):
                self.assertNotIn(f"Delivers: sd:{number}", block)
                self.assertEqual(self.status_of(number), "planning")

    def test_an_associate_only_prepare_refuses_a_second_item(self):
        """sd:3704, as sd:3365 refuses a `Closes:` line: an associate-only claim closes nothing."""
        self.task_item("task")
        other = self.closes_items()[0]
        with self.assertRaises(ship.Refusal) as caught:
            self.unanswered("--associate-only", "--title", "A slice", *self.also(other)).prepare()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "closes_need_delivery")
        self.assertIn(f"--item {other}", str(caught.exception))
        self.assertFalse(self.remote.pull_requests)

    def test_a_second_item_that_is_no_item_or_another_repositorys_refuses(self):
        self.task_item("task")
        elsewhere = self.directory / "elsewhere"
        upsert_repo(self.connection, str(elsewhere), remote="https://github.com/example/elsewhere")
        foreign = create_item(self.connection, kind="task", title="another repository", status="planning", repo=str(elsewhere))
        for number, code in ((99999, "closes_item_unknown"), (foreign, "closes_item_foreign")):
            with self.subTest(code=code):
                with self.assertRaises(ship.Refusal) as caught:
                    self.unanswered("--deliver", "--title", "A change", *self.also(number)).prepare()
                self.assertEqual(caught.exception.workflow["blocker"]["code"], code)
        self.assertFalse(self.remote.pull_requests)

    def test_a_co_delivered_item_a_teammate_run_holds_refuses_prepare_and_the_merge_before_it_publishes(self):
        """pack sd:7221: a lead's in-session builder may still write; only its lead's `--end` hands the item over."""
        from sd_db import teammate

        self.task_item("task")
        other = self.closes_items()[0]

        def start():
            return teammate.start(self.connection, [other], worktree="/w/teammate", branch="feat/t",
                                  budget_minutes=60, owner="lead")
        start()
        with self.assertRaises(ship.Refusal) as caught:
            self.unanswered("--deliver", "--title", "A change", *self.also(other)).prepare()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "teammate_held")
        self.assertIn(f"sd assignments beat {other} --end", caught.exception.workflow["next_action"])
        self.assertFalse(self.remote.pull_requests)
        teammate.end(self.connection, other)
        number = self.unanswered("--deliver", "--title", "A change", *self.also(other)).prepare()["pull_request"]["number"]

        def started_during_the_gate(body):
            start()
            return []
        with patch.object(ship.sd_lib, "demoted_trailers", side_effect=started_during_the_gate), \
                patch.object(ship.time, "sleep"), self.assertRaises(ship.Refusal) as caught:
            self.merge()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "teammate_held")
        self.assertIsNone(getattr(self.remote.pull(number), "merged_head", None))

    def test_the_merge_refuses_a_member_cancelled_alone_after_prepare_before_it_publishes(self):
        """Review round 8 of pack sd:9222: a prepared pull request left for a hand merge kept its closing set, so a
        member the operator cancelled on its own after prepare still closed. The merge checks the set against the
        branch's build bundle right before it publishes; nothing merges."""
        import sd_db
        from sd_db import build, operations

        self.task_item("task")
        other = self.closes_items()[0]
        branch = _git(self.root, "symbolic-ref", "--short", "HEAD")
        lead, member = (sd_db.create_assignment(self.connection, item=number, role="author", status="queued",
                                                lane="parallel") for number in (self.item, other))
        for ident in (lead, member):
            self.connection.execute("UPDATE assignment SET scope = ?, parent = ? WHERE id = ?",
                                    (build.SCOPE, None if ident == lead else lead, ident))
        self.connection.execute(
            "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, created_at, "
            "updated_at) VALUES ('run-1', ?, 1, ?, ?, 'fixture', '/w', '/w', '2026-11-17T00:00:00+00:00', "
            "'2026-11-17T00:00:00+00:00')",
            (lead, self.connection.execute("SELECT repo FROM item WHERE id = ?", (self.item,)).fetchone()[0], branch))
        self.connection.commit()
        number = self.unanswered("--deliver", "--title", "A change", *self.also(other)).prepare()["pull_request"]["number"]
        state = operations.assignment_state(self.connection, member)
        operations.cancel_assignment(self.connection, member, expected_revision=state["revision"], who="operator")
        with patch.object(ship.time, "sleep"), self.assertRaises(ship.Refusal) as caught:
            self.merge()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "member_cancelled_alone")
        self.assertIn(f"sd:{other} was cancelled on its own", str(caught.exception))
        self.assertIsNone(getattr(self.remote.pull(number), "merged_head", None))

    def test_a_held_item_refuses_prepare_before_it_commits_or_catches_up(self):
        """pack sd:7221 round 4: the builder may still write its worktree, so prepare writes none of it first."""
        from sd_db import teammate

        before = self.shared_context()  # the branch is behind main, so --catch-up would merge
        teammate.start(self.connection, [self.item], worktree="/w/teammate", branch="feat/t",
                       budget_minutes=60, owner="lead")
        message = self.directory / "message.txt"
        message.write_text("focused change")
        for extra in (["--catch-up"], ["--path", "src.py", "--message-file", str(message)]):
            if "--path" in extra:
                (self.root / "src.py").write_text("held edit\n")
            status = _git(self.root, "status", "--porcelain")
            with self.subTest(extra=extra), self.assertRaises(ship.Refusal) as caught:
                self.prepare(*extra)
            self.assertEqual(caught.exception.workflow["blocker"]["code"], "teammate_held")
            self.assertEqual(_git(self.root, "rev-parse", "HEAD"), before)
            self.assertEqual(_git(self.root, "status", "--porcelain"), status)

    def test_a_closing_set_read_at_another_head_refuses_the_merge(self):
        """Failure table, row 3: the stored set names the head it was read at."""
        self.task_item("task")
        number = self.unanswered("--deliver", "--title", "A change").prepare()["pull_request"]["number"]
        operation = self.operation()
        operation.state["closing"] = {**operation.state["closing"], "head": "0" * 40}
        operation.save()
        with patch.object(ship.time, "sleep"), self.assertRaises(ship.Refusal) as caught:
            self.merge()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "closing_set_stale")
        self.assertIsNone(getattr(self.remote.pull(number), "merged_head", None))

    def test_an_older_unsent_receipt_without_a_closing_set_refuses_the_merge(self):
        """Failure table, row 4: a receipt prepared under the title rule is prepared again, not merged."""
        self.task_item("task")
        number = self.unanswered("--deliver", "--title", "A change").prepare()["pull_request"]["number"]
        operation = self.operation()
        del operation.state["closing"]
        operation.save()
        with patch.object(ship.time, "sleep"), self.assertRaises(ship.Refusal) as caught:
            self.merge()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "closing_set_stale")
        self.assertIn("prepare", caught.exception.workflow["next_action"])
        self.assertIsNone(getattr(self.remote.pull(number), "merged_head", None))

    def test_an_older_receipt_at_merge_dispatch_closes_what_its_title_named(self):
        """Failure table, row 5: prepare routes `merge_dispatch` to reconcile, so it
        cannot add the field. That receipt keeps the title rule it was sent under."""
        self.task_item("task")
        batched = self.closes_items()[:1]
        self.unanswered("--deliver", "--title", f"A change (sd:{batched[0]})").prepare()
        operation = self.operation()
        del operation.state["closing"]
        operation.state["phase"] = "merge_dispatch"
        operation.save()
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assert_closed_by_merge(result)
        self.assertEqual(result.get("closed_items"), [f"sd:{batched[0]}"])

    def test_an_older_associate_only_receipt_at_merge_dispatch_still_closes_nothing(self):
        """Codex review of sd:3704: under the old rule an associate-only title named
        context only (sd:3365), so its ids are no delivery claim to refuse."""
        self.task_item("task")
        other = self.closes_items()[0]
        self.unanswered("--associate-only", "--title", f"A slice (sd:{other})").prepare()
        operation = self.operation()
        del operation.state["closing"]
        operation.state["phase"] = "merge_dispatch"
        operation.save()
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assertEqual(result["phase"], "merged")
        self.assertFalse(result.get("closed_items"))
        self.assertEqual(self.status_of(other), "planning")

    def test_a_lost_merge_response_still_closes_the_named_items(self):
        """Failure table: the `PUT` lands and its answer is lost; reconcile closes the stored set."""
        self.task_item("task")
        batched = self.closes_items()[:1]
        self.unanswered("--deliver", "--title", "A change", *self.also(*batched)).prepare()
        self.double.lose_merge = True
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assertEqual(result["phase"], "merged")
        self.assertEqual(self.status_of(batched[0]), "done")

    def test_a_web_merge_without_the_closes_trailers_leaves_those_items_open(self):
        """A pull request merged on GitHub lands whatever message the web form
        held. Each co-delivered item is verified against the landed message, as
        the claimed item is: one whose `Delivers:` did not land stays open,
        and one already done by hand needs delivery evidence for this commit."""
        from sd_db.workflow import change_status
        self.task_item("task")
        closes = self.closes_items()
        by_hand = create_item(self.connection, kind="task", title="closed by hand", status="planning",
                              repo=str(self.operator))
        trailed = create_item(self.connection, kind="task", title="named last", status="planning",
                              repo=str(self.operator))
        named = (*closes, by_hand, trailed)
        self.unanswered("--deliver", "--title", "Fixes", *self.also(*named)).prepare()
        change_status(self.connection, by_hand, "done", who="operator")
        saved = self.double._route

        def route(method, path, body):
            if method == "PUT" and path.endswith("/merge"):
                # The web form kept the claimed item's trailer and dropped the rest.
                body = {**body, "commit_message": "\n".join(
                    line for line in body["commit_message"].splitlines()
                    if not any(line == f"Delivers: sd:{number}" for number in named))}
            return saved(method, path, body)
        self.double._route = route
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        commit = result["merge_commit"]
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()[0], "done")
        self.assertTrue(result["delivery_pending"], result)
        self.assertEqual(result["workflow"]["blocker"]["code"], "closes_delivery_pending")
        self.assertEqual(result["closed_items"], [])
        failed = result["closes_failed"]
        for number in closes[:2]:
            with self.subTest(item=number):
                self.assertEqual(failed[f"sd:{number}"], f"{commit} carries no `Delivers: sd:{number}` trailer")
                self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (number,)).fetchone()[0], "planning")
        self.assertIn("Delivers", failed[f"sd:{closes[2]}"])
        self.assertNotEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (closes[2],)).fetchone()[0], "done")
        self.assertIn(f"item {by_hand} is done without delivery evidence for {commit}", failed[f"sd:{by_hand}"])
        self.assertEqual(failed[f"sd:{trailed}"], f"{commit} carries no `Delivers: sd:{trailed}` trailer")
        self.assertEqual(self.status_of(trailed), "planning")

    def test_an_associate_only_merge_closes_no_item_its_title_names(self):
        """sd:3365, owner ruling 2026-10-11. A link-only pull request whose title
        named another item squashed with `Delivers:` for it and closed it. A
        title names context; only a `--deliver` claim closes what it names."""
        self.task_item("task")
        other = self.closes_items()[:1]
        self.unanswered("--associate-only", "--title", f"Slice of sd:{self.item}, after sd:{other[0]} and sd:99999").prepare()
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assertFalse(result["delivery_pending"], result)
        self.assertFalse(result.get("closed_items"))
        block = ship.sd_lib.trailer_block(_git(self.remote.path, "log", "-1", "--format=%B", "main")).splitlines()
        self.assertIn(f"Item: sd:{self.item}", block)
        self.assertFalse([line for line in block if line.startswith("Delivers:")], block)
        for number in (self.item, *other):
            with self.subTest(item=number):
                self.assertNotEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (number,)).fetchone()[0], "done")

    def test_prepare_refuses_a_body_closes_line(self):
        """sd:3704, operator ruling 2026-10-13, replacing the sd:1481 body line:
        the enqueue's --item list is the one closing set, so a `Closes:` line is
        refused before anything is published, with either claim."""
        self.task_item("task")
        other = self.closes_items()[0]
        body = self.directory / "body.md"
        body.write_text(f"A change that also finishes another row.\n\nCloses: sd:{other}\n")
        for claim in ("--deliver", "--associate-only"):
            with self.subTest(claim=claim), self.assertRaises(ship.Refusal) as caught:
                self.unanswered(claim, "--body-file", str(body)).prepare()
            self.assertEqual(caught.exception.workflow["blocker"]["code"], "body_trailer_refused")
            self.assertIn("name each item the merge closes with --item", str(caught.exception))
        self.assertFalse(self.remote.pull_requests)

    def test_a_title_closes_only_a_whole_sd_token(self):
        """Review of sd:3013: `sd:123abc` and `sd:123_legacy` closed item 123."""
        self.task_item("task")
        named = self.closes_items()[:2]
        title = f"Fix sd:{named[0]}abc and sd:{named[1]}_legacy parsing (sd:{self.item})"
        self.unanswered("--deliver", "--title", title).prepare()
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assertFalse(result.get("closed_items"))
        for number in named:
            with self.subTest(item=number):
                self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (number,)).fetchone()[0], "planning")

    def test_a_live_title_edited_after_prepare_closes_nothing(self):
        """Review of sd:3013 refused a title edited after prepare that named another
        repository's item. Since sd:3704 a title closes nothing, so the edit cannot
        reach the closing set: the merge goes out and the item stays open."""
        self.task_item("task")
        elsewhere = self.directory / "elsewhere"
        upsert_repo(self.connection, str(elsewhere), remote="https://github.com/example/elsewhere")
        foreign = create_item(self.connection, kind="task", title="another repository", status="planning", repo=str(elsewhere))
        number = self.unanswered("--deliver").prepare()["pull_request"]["number"]
        self.remote.pull(number).title = f"A change (sd:{foreign})"
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assert_closed_by_merge(result)
        self.assertNotIn(f"Delivers: sd:{foreign}", _git(self.remote.path, "log", "-1", "--format=%B", "main"))
        self.assertEqual(self.status_of(foreign), "planning")

    def test_a_refs_item_stays_open_after_the_merge(self):
        """`Refs:` names a related or partial item, which stays open (sd:1481).
        The lane writes it for rows a pull request touches without finishing,
        so the merge closes only the claimed item, adds no `Delivers:` for a
        `Refs:` item, and prepare refuses none, not even one it cannot find."""
        self.task_item("task")
        related = create_item(self.connection, kind="task", title="related, still open", status="planning",
                              repo=str(self.operator))
        body = self.directory / "body.md"
        body.write_text(f"A change.\n\nRefs: sd:{related}, sd:99999\n")
        self.unanswered("--deliver", "--body-file", str(body)).prepare()
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assert_closed_by_merge(result)
        block = ship.sd_lib.trailer_block(_git(self.remote.path, "log", "-1", "--format=%B", "main")).splitlines()
        self.assertIn(f"Delivers: sd:{self.item}", block)
        self.assertNotIn(f"Delivers: sd:{related}", block)
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (related,)).fetchone()[0], "planning")

    def test_a_followup_delivers_on_merge(self):
        self.task_item("followup")
        self.unanswered("--deliver").prepare()
        with patch.object(ship.time, "sleep"):
            self.assert_closed_by_merge(self.merge())

    def test_a_task_closed_by_hand_is_not_closed_again(self):
        self.task_item("task")
        self.unanswered("--deliver").prepare()
        with patch.object(ship.time, "sleep"):
            first = self.merge()
        again = self.operation("reconcile").reconcile()
        self.assertFalse(again["delivery_pending"])
        self.assertEqual(first["merge_commit"], again["merge_commit"])
        closes = self.connection.execute(
            "SELECT COUNT(*) FROM note WHERE item = ? AND kind = 'status_change' AND body LIKE '%delivered at%'", (self.item,)).fetchone()[0]
        self.assertEqual(closes, 1)

    def test_a_plain_close_does_not_clear_a_held_task_merge(self):
        """A close that names no commit says nothing about the combined tree a
        held merge landed, so only a close naming this merge clears the hold."""
        from sd_db.workflow import change_status
        self.task_item("task")
        commit = self.held_merge()["merge_commit"]
        change_status(self.connection, self.item, "done", who="operator")
        held = self.merge()
        self.assertTrue(held["delivery_pending"])
        self.assertEqual(held["workflow"]["blocker"]["code"], "base_advanced_at_merge")
        self.assertIn(f"--delivered-by {commit}", held["workflow"]["next_action"])
        change_status(self.connection, self.item, "in_progress", who="operator")
        change_status(self.connection, self.item, "done", who="operator", reason=f"delivered at {commit} on origin/main")
        self.assertFalse(self.merge()["delivery_pending"])

    def hand_delivered_held_squash(self) -> int:
        """A held task squash the operator closed by hand; reconcile owes it a `Closes:`."""
        from sd_db.workflow import change_status
        self.task_item("task")
        racing, self.double._route = self.double._route, self.double._route
        commit = self.held_merge()["merge_commit"]
        self.double._route = racing
        change_status(self.connection, self.item, "done", who="operator", reason=f"delivered at {commit} on origin/main")
        reconciled = self.merge()
        self.assertFalse(reconciled["delivery_pending"])
        self.assertEqual(reconciled["closing_owed"]["commit"], commit)
        return self.item

    def carrier_merge(self, name: str = "second") -> dict:
        """Another task's unheld `--deliver` merge in the same repository."""
        _git(self.root, "fetch", "-q", "origin", "main")
        _git(self.root, "checkout", "-q", "-b", name, "FETCH_HEAD")
        (self.root / f"{name}.py").write_text("y = 2\n")
        _git(self.root, "add", f"{name}.py")
        _git(self.root, "commit", "-qm", f"{name}\n\nAuthored-with: human")
        self.item = create_item(self.connection, kind="task", title=f"{name} task", status="in_progress",
                                repo=str(self.operator), branch=name)
        self.unanswered("--deliver").prepare()
        with patch.object(ship.time, "sleep"):
            return self.merge()

    def test_a_hand_delivered_held_squash_reads_as_closed_in_git_after_the_next_merge(self):
        """Reconcile clears the hold once the row records the hand delivery, and
        git-only readers must agree: they read the squash's parent, which never
        changes, so the next squash here carries the close (sd:1600)."""
        held = self.hand_delivered_held_squash()
        self.assertNotEqual(ship.sd_lib.delivered(self.root, f"sd:{held}"), ship.sd_lib.YES)
        carrier = self.carrier_merge()
        self.assertFalse(carrier["delivery_pending"], carrier)
        self.assertEqual([debt["item"] for debt in carrier["carried_closes"]], [held])
        message = _git(self.remote.path, "log", "-1", "--format=%B", "main")
        parsed = subprocess.run(["git", "interpret-trailers", "--parse"], input=message, capture_output=True,
                                text=True, check=True).stdout
        self.assertIn(f"Closes: sd:{held}\n", parsed)
        self.assertNotIn("Authored-with:", parsed)
        self.assertEqual(ship.sd_lib.delivered(self.root, f"sd:{held}"), ship.sd_lib.YES)
        receipt = receipts.read(self.connection, receipts.receipt_key(self.remote.slug, "topic", held))[1]
        self.assertEqual(receipt["closing_paid"], carrier["merge_commit"])

    def test_a_paid_close_is_not_carried_again(self):
        held = self.hand_delivered_held_squash()
        self.carrier_merge()
        third = self.carrier_merge("third")
        self.assertNotIn("carried_closes", third)
        self.assertNotIn(f"Closes: sd:{held}", _git(self.remote.path, "log", "-1", "--format=%B", "main"))

    def owe(self, repository: str, item: int, branch: str) -> None:
        """A merged receipt in `repository` that owes `item` a `Closes:`."""
        key = receipts.receipt_key(repository, branch, item)
        revision, _ = receipts.read(self.connection, key)
        receipts.save(self.connection, key, revision, {
            "repository": repository, "branch": branch, "head": "a" * 40, "merge_commit": "b" * 40,
            "closing_owed": {"item": item, "commit": "b" * 40, "branch": branch}})

    def test_another_repository_s_item_is_refused_with_the_working_path(self):
        """sd:2576: the first refusal names the itemless path, not only the mismatch.

        sd:2300 shipped PRs in two other repositories; four refusals, each shown
        only after the previous one was fixed, came before the first working call.
        """
        other = self.directory / "other"
        upsert_repo(self.connection, str(other), remote="https://github.com/example-org/other.git")
        self.item = create_item(self.connection, kind="task", title="elsewhere", status="in_progress", repo=str(other))
        allocate = "sd-ship review --no-item --create-record --assert-new-work"
        for command, extra in (("prepare", ()), ("merge", ("--manual", "--expected-head", "a" * 40))):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ship.Refusal, "item repository does not match this checkout's origin") as caught:
                    ship.Ship(self.root, self.connection, self.database, self.args(command, *extra))
                message, workflow = str(caught.exception), caught.exception.workflow
                self.assertIn(f"sd:{self.item} belongs to example-org/other", message)
                self.assertIn("fixture/repo", message)
                self.assertEqual((workflow["blocker"]["code"], workflow["blocker"]["boundary"]),
                                 ("item_repository_mismatch", "input"))
                self.assertIn(allocate, workflow["next_action"])
                self.assertIn(f"sd-ship {command} --no-item --review-id", workflow["next_action"])
                self.assertIn(f"sd task note {self.item}", workflow["next_action"])
        held = ship.parser().parse_args(["hold", "--item", str(self.item), "--json"])
        with self.assertRaisesRegex(ship.Refusal, "item repository does not match") as caught:
            ship.hold_command(self.root, self.connection, self.database, held, receipts)
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "item_repository_mismatch")
        self.assertIn("example-org/other", caught.exception.workflow["next_action"])
        self.assertNotIn("--no-item", caught.exception.workflow["next_action"])

    def test_an_owed_close_never_crosses_into_another_repository(self):
        """Two repositories in one database: a carrier pays only its own
        repository's debts, and only for an item whose row names it (sd:1600)."""
        other = self.directory / "other"
        upsert_repo(self.connection, str(other), remote="https://github.com/example-org/other.git")
        here = create_item(self.connection, kind="task", title="owed here", status="done", repo=str(self.operator))
        there = create_item(self.connection, kind="task", title="owed there", status="done", repo=str(other))
        moved = create_item(self.connection, kind="task", title="row elsewhere", status="done", repo=str(other))
        self.owe(self.remote.slug, here, "here")
        self.owe("example-org/other", there, "there")
        self.owe(self.remote.slug, moved, "moved")
        carrier = self.carrier_merge()
        self.assertEqual([debt["item"] for debt in carrier["carried_closes"]], [here])
        message = _git(self.remote.path, "log", "-1", "--format=%B", "main")
        self.assertNotIn(f"Closes: sd:{there}", message)
        self.assertNotIn(f"Closes: sd:{moved}", message)
        self.assertNotIn("closing_paid", receipts.read(self.connection, receipts.receipt_key("example-org/other", "there", there))[1])

    def test_a_carrier_pays_at_most_the_cap(self):
        owed = [create_item(self.connection, kind="task", title=f"owed {n}", status="done", repo=str(self.operator))
                for n in range(12)]
        for item in owed:
            self.owe(self.remote.slug, item, f"b{item}")
        carried, left = ship.sd_ship_squash.owed_closes(self.connection, self.remote.slug, "", lambda item: self.remote.slug)
        self.assertEqual(([d["item"] for d in carried], [d["item"] for d in left]), (sorted(owed)[:10], sorted(owed)[10:]))

    def test_owed_closes_past_the_cap_stay_owed_and_the_merge_says_so(self):
        held = self.hand_delivered_held_squash()
        with patch.object(ship.sd_ship_squash, "OWED_CLOSES_CAP", 0):
            carrier = self.carrier_merge()
        self.assertEqual((carrier.get("carried_closes"), carrier["closes_left"]), (None, [held]))
        self.assertNotEqual(ship.sd_lib.delivered(self.root, f"sd:{held}"), ship.sd_lib.YES)
        receipt = receipts.read(self.connection, receipts.receipt_key(self.remote.slug, "topic", held))[1]
        self.assertNotIn("closing_paid", receipt)

    def test_a_held_followup_names_a_close_it_can_take(self):
        """A followup takes no `--delivered-by` (sd:809); its hold names the reason form."""
        self.task_item("followup")
        result = self.held_merge()
        self.assertIn(f"--reason 'delivered at {result['merge_commit']} on origin/main'", result["workflow"]["next_action"])

    def test_a_task_done_without_evidence_is_not_counted_as_delivered(self):
        from sd_db.workflow import change_status
        self.task_item("task")
        self.unanswered("--deliver").prepare()
        # sd_db refuses a reasonless close of a row on its own branch (sd:2570);
        # a reason that names no commit is still no delivery evidence.
        change_status(self.connection, self.item, "done", who="operator", reason="closed by hand")
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assertTrue(result["delivery_pending"])
        self.assertIn("without delivery evidence", result["delivery_error"])

    # -- sd:1913: an associate-only merge is repaired by `sd work deliver` ---

    def associated_merge(self) -> str:
        self.unanswered("--associate-only").prepare()
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assertFalse(result["delivery_pending"])
        self.assertNotEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()[0], "done")
        return result["merge_commit"]

    def test_prepare_on_an_associate_only_merge_names_the_repair(self):
        commit = self.associated_merge()
        with self.assertRaisesRegex(
                ship.Refusal, rf"sd work deliver {self.item} {commit} --associated --reason TEXT") as caught:
            self.unanswered("--deliver").prepare()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "delivery_after_merge")

    def test_work_still_needs_acceptance_evidence_to_deliver(self):
        with self.assertRaisesRegex(ship.Refusal, "--acceptance-file"):
            self.unanswered("--deliver").prepare()

    def test_a_create_a_5xx_left_uncertain_is_recovered_by_one_more_create(self):
        # sd:6858: GitHub answered 5xx and made no pull request; the next
        # prepare finds none, open or closed, at this head, so it creates
        # again. GitHub refuses a second open one per head and base.
        self.double.no_create_result = True
        with self.assertRaises(ship.Refusal):
            self.prepare()
        self.double.no_create_result = False
        self.assertEqual(self.prepare()["pull_request"]["number"], 1)
        self.assertEqual(len(self.remote.pull_requests), 1)
        self.assertEqual(len([call for call in self.remote.calls
                              if call.method == "POST" and call.path.endswith("/pulls")]), 2)

    def test_a_create_whose_pr_was_closed_since_still_refuses(self):
        # The uncertain create did make one, and someone closed it: that is
        # an operator's act, so no second pull request is made.
        self.double.lose_create = True
        with self.assertRaises(ship.Refusal):
            self.prepare()
        self.remote.pull(1).state = "CLOSED"
        with self.assertRaisesRegex(ship.Refusal, "closed"):
            self.prepare()
        self.assertEqual(len(self.remote.pull_requests), 1)

    def test_a_retried_create_github_refuses_reads_the_one_it_made(self):
        # The list missed the pull request the lost create made, and GitHub
        # refuses the second create (422): the re-read binds that one.
        self.double.lose_create = True
        with self.assertRaises(ship.Refusal):
            self.prepare()
        self.double.lose_create = False
        original, seen = self.double._route, []

        def lagging(method, path, body):
            if method == "GET" and urlsplit(path).path.endswith("/pulls") and "base=" in path and not seen:
                seen.append(path)
                return 200, []
            if method == "POST" and urlsplit(path).path.endswith("/pulls"):
                raise RemoteRefusal(422, "A pull request already exists")
            return original(method, path, body)
        with patch.object(self.double, "_route", lagging):
            self.assertEqual(self.prepare()["pull_request"]["number"], 1)
        self.assertEqual(len(self.remote.pull_requests), 1)

    def test_lost_create_response_finds_one_matching_pr(self):
        self.double.lose_create = True
        with self.assertRaises(ship.Refusal):
            self.prepare()
        self.assertEqual(self.prepare()["pull_request"]["number"], 1)
        self.assertEqual(len([call for call in self.remote.calls if call.method == "POST"]), 1)

    def test_invented_or_stale_review_receipt_refuses(self):
        self.prepare()
        operation = self.operation()
        operation.state["passes"][-1]["report"]["check"] = None
        operation.save()
        with self.assertRaises(ship.Refusal):
            self.merge()

    def test_head_change_after_review_prevents_push(self):
        original = ship.Ship.review
        def changed(operation, head):
            original(operation, head)
            _git(operation.root, "commit", "--allow-empty", "-m", "changed\n\nAuthored-with: human")
        with patch.object(ship.Ship, "review", changed), self.assertRaisesRegex(ship.Refusal, "HEAD moved"):
            self.prepare()
        self.assertFalse(any(call.method == "POST" for call in self.remote.calls))

    def test_delivery_requires_acceptance_and_reconciles_failed_database_write(self):
        acceptance = self.directory / "acceptance.json"
        acceptance.write_text(json.dumps({"item": self.item, "complete": True, "criteria": [{"criterion": "actual scope", "passed": True, "evidence": "fixture-check-pass"}]}))
        self.prepare("--deliver", "--acceptance-file", str(acceptance))
        before_refs = _git(self.operator, "show-ref")
        before_status = _git(self.operator, "status", "--porcelain")
        with patch("sd_db.progress.deliver_work", side_effect=sqlite3.OperationalError("DB write unavailable")):
            result = self.merge()
        self.assertTrue(result["delivery_pending"])
        missing = subprocess.run(["git", "cat-file", "-e", result["merge_commit"]], cwd=self.operator, capture_output=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()[0], "in_progress")
        fixed = self.operation("reconcile").reconcile()
        self.assertFalse(fixed["delivery_pending"])
        self.assertEqual(_git(self.operator, "show-ref"), before_refs)
        self.assertEqual(_git(self.operator, "status", "--porcelain"), before_status)
        self.assertEqual((self.operator / "operator.txt").read_text(), "uncommitted operator work")
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()[0], "done")
        self.assertEqual(len([call for call in self.remote.calls if call.method == "PUT"]), 1)

    def waiver_file(self, *rows: dict) -> pathlib.Path:
        """An acceptance file for the claimed item: a passing row, then `rows`."""
        acceptance = self.directory / "acceptance.json"
        acceptance.write_text(json.dumps({"item": self.item, "complete": True, "criteria": [
            {"criterion": "actual scope", "passed": True, "evidence": "fixture-check-pass"}, *rows]}))
        return acceptance

    def decision(self, body: str, *, item: int | None = None, kind: str = "decision") -> int:
        from sd_db.writes import add_note
        return add_note(self.connection, self.item if item is None else item, kind, body, session="fixture-session")

    @staticmethod
    def waived_row(criterion: str, decision: object) -> dict:
        return {"criterion": criterion, "passed": False, "evidence": "not measured on this machine",
                "waived": {"reason": "the owner waived it", "decision": decision}}

    def delivered_reason(self) -> str:
        return self.connection.execute("SELECT body FROM note WHERE item = ? AND kind = 'status_change' "
                                       "ORDER BY id DESC LIMIT 1", (self.item,)).fetchone()[0]

    def test_a_waived_row_with_its_decision_note_delivers_and_names_the_waiver(self):
        """sd:3704 part 2: `passed: false` with a reason and the decision note
        that waived it. Whitespace is collapsed on both sides before the
        exact compare."""
        note = self.decision("Ruling: no load rig here.\nWaive:  p95   under 2 s ")
        self.prepare("--deliver", "--acceptance-file", str(self.waiver_file(self.waived_row("p95 under 2 s", note))))
        stored = self.operation().state["acceptance"]["claim"]["criteria"][1]["waived"]
        self.assertEqual(stored, {"reason": "the owner waived it", "decision": note, "note": {
            "id": note, "body": "Ruling: no load rig here.\nWaive:  p95   under 2 s ", "session": "fixture-session"}})
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assertFalse(result["delivery_pending"], result)
        self.assertEqual(self.status_of(self.item), "done")
        self.assertTrue(self.delivered_reason().endswith(f"; waived: 1 criteria (decision #{note})"),
                        self.delivered_reason())
        fields = json.loads(self.connection.execute("SELECT fields FROM item WHERE id = ?", (self.item,)).fetchone()[0])
        self.assertEqual(fields["completion"]["waived"], [{"criterion": "p95 under 2 s", "decision": note}])

    def test_a_task_closed_with_a_waiver_names_it(self):
        self.task_item("task")
        note = self.decision("Waive: runs offline")
        self.prepare("--deliver", "--acceptance-file", str(self.waiver_file(self.waived_row("runs offline", note))))
        with patch.object(ship.time, "sleep"):
            result = self.merge()
        self.assertFalse(result["delivery_pending"], result)
        self.assertTrue(self.delivered_reason().endswith(
            f"on origin/main; waived: 1 criteria (decision #{note})"), self.delivered_reason())

    def test_a_waiver_without_an_exact_decision_note_on_this_item_refuses(self):
        """A mention is not a waiver, a shorter or longer criterion is not
        this one, and a note on another item or of another kind waives
        nothing. Each refuses by name and stores no claim."""
        other = create_item(self.connection, kind="task", title="another item", repo=str(self.operator))
        cases = [
            ("Do not waive X", "X", {}, "waiver_not_exact"),
            ("Waive: X", "X on Y", {}, "waiver_not_exact"),
            ("Waive: X on Y", "X", {}, "waiver_not_exact"),
            ("waive: X", "X", {}, "waiver_not_exact"),
            ("Waive: X", "X", {"item": other}, "waiver_refused"),
            ("Waive: X", "X", {"kind": "comment"}, "waiver_refused"),
        ]
        for body, criterion, where, code in cases:
            with self.subTest(body=body, criterion=criterion, where=where):
                row = self.waived_row(criterion, self.decision(body, **where))
                with self.assertRaises(ship.Refusal) as caught:
                    self.unanswered("--deliver", "--acceptance-file", str(self.waiver_file(row))).prepare()
                self.assertEqual(caught.exception.workflow["blocker"]["code"], code)
                self.assertNotIn("acceptance", self.operation().state)
        for waived in ({"reason": "r", "decision": 999999}, {"reason": "", "decision": 1}, {"reason": "r"}, "r"):
            with self.subTest(waived=waived):
                row = {"criterion": "X", "passed": False, "evidence": "e", "waived": waived}
                with self.assertRaises(ship.Refusal) as caught:
                    self.unanswered("--deliver", "--acceptance-file", str(self.waiver_file(row))).prepare()
                self.assertEqual(caught.exception.workflow["blocker"]["code"], "waiver_refused")
        row = {"criterion": "X", "passed": False, "evidence": "e"}
        with self.assertRaisesRegex(ship.Refusal, "waiver"):
            self.unanswered("--deliver", "--acceptance-file", str(self.waiver_file(row))).prepare()

    def test_a_row_prepare_cannot_deliver_as_waived_refuses(self):
        """A passed row that also claims a waiver, and a waived row whose
        criterion is not text, refuse at prepare: the first would deliver
        with its waiver unread and unrecorded, the second would merge and
        then fail every delivery."""
        rows = [
            {**self.waived_row("X", 999999), "passed": True},
            self.waived_row(123, self.decision("Waive: 123")),  # type: ignore[arg-type]
            self.waived_row("  ", self.decision("Waive: ")),
        ]
        for row in rows:
            with self.subTest(row=row):
                with self.assertRaisesRegex(ship.Refusal, "waiver"):
                    self.unanswered("--deliver", "--acceptance-file", str(self.waiver_file(row))).prepare()
                self.assertNotIn("acceptance", self.operation().state)

    def test_a_failed_read_of_the_decision_note_refuses(self):
        class Failing:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def execute(self, sql, *args):
                if sql.startswith("SELECT id, item, kind, body, session FROM note"):
                    raise sqlite3.OperationalError("database is locked")
                return self.connection.execute(sql, *args)

        row = self.waived_row("X", self.decision("Waive: X"))
        operation = self.unanswered("--deliver", "--acceptance-file", str(self.waiver_file(row)))
        operation.connection = Failing(self.connection)
        with self.assertRaises(ship.Refusal) as caught:
            operation.prepare()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "waiver_unread")
        self.assertNotIn("acceptance", self.operation().state)

    def test_dirty_checkout_and_preexisting_index_are_unchanged(self):
        (self.root / "unrelated.txt").write_text("operator work")
        before = _git(self.root, "status", "--porcelain")
        with self.assertRaisesRegex(ship.Refusal, "uncommitted"):
            self.prepare()
        self.assertEqual(_git(self.root, "status", "--porcelain"), before)
        _git(self.root, "add", "unrelated.txt")
        message = self.directory / "message.txt"
        message.write_text("focused change")
        before = _git(self.root, "diff", "--cached")
        with self.assertRaisesRegex(ship.Refusal, "index already"):
            self.prepare("--path", "src.py", "--message-file", str(message))
        self.assertEqual(_git(self.root, "diff", "--cached"), before)

    def test_no_repository_path_flag_or_unreviewed_head_override(self):
        for flag in ("--repo", "--reviewed-head"):
            result = self.cli("prepare", flag, "elsewhere")
            self.assertEqual(result.returncode, 2)

    def test_only_enumerated_files_commit_and_unrelated_work_stays(self):
        (self.root / "src.py").write_text("intended = True\n")
        (self.root / "unrelated.txt").write_text("operator work")
        message = self.directory / "message.txt"
        message.write_text("Ship only the intended file")
        args = self.args("prepare", "--path", "src.py", "--message-file", str(message))
        ship.commit_paths(self.root, args)
        self.assertEqual(_git(self.root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "src.py")
        self.assertEqual((self.root / "unrelated.txt").read_text(), "operator work")

    def test_observe_uses_item_branch_without_mutating_operator_checkout_or_receipts(self):
        self.prepare()
        before_refs = _git(self.operator, "show-ref")
        before_status = _git(self.operator, "status", "--porcelain")
        before_rows = self.connection.execute("SELECT COUNT(*) FROM state").fetchone()[0]
        operation = ship.Ship(self.operator, self.connection, self.database, self.args("observe"))
        observed = operation.observe()
        self.assertEqual(observed["phase"], "ready_to_send")
        self.assertFalse(observed["merged"])
        self.assertFalse(observed["verified"])
        self.assertEqual(_git(self.operator, "show-ref"), before_refs)
        self.assertEqual(_git(self.operator, "status", "--porcelain"), before_status)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM state").fetchone()[0], before_rows)

    def test_observe_without_receipt_returns_cli_refusal_without_writes(self):
        before = list(self.connection.iterdump())
        refs = _git(self.root, "show-ref")
        calls = len(self.remote.calls)
        result = self.cli("observe")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertFalse(value["ok"])
        self.assertTrue(value["manualRequired"])
        self.assertIn("no unique ship receipt", value["error"])
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(list(self.connection.iterdump()), before)
        self.assertEqual(_git(self.root, "show-ref"), refs)
        self.assertEqual(len(self.remote.calls), calls)

    def test_observe_reads_the_checkpoint_of_a_prepare_that_stopped_before_a_pull_request(self):
        """sd:2021. A blocked prepare stores no pull-request reference, and observe
        answered "there is no durable pull-request receipt to observe"."""
        program = self.programs / "review-fixture"
        payload = {"type": "result", "subtype": "success", "structured_output": {"findings": [
            {"path": "src.py", "line": 1, "severity": "high", "family": "correctness", "summary": "sd2021 finding"}]}}
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        with self.assertRaises(ship.Refusal):
            self.prepare()
        result = self.cli("observe")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout)
        self.assertIsNone(observed["pull_request"])
        self.assertEqual((observed["phase"], observed["receipt"]["phase"]), ("reviewed", "reviewed"))
        self.assertEqual(observed["receipt"]["review"]["status"], "blocking")
        self.assertEqual([row["summary"] for row in observed["receipt"]["review"]["findings"]], ["sd2021 finding"])

    def unsited(self, command, *extra, library="provisioned"):
        """`bin/sd-ship` under an interpreter that can see no site-packages at all.

        `-S` is the point of the helper. The test run itself necessarily has
        `sd_db` importable, so it is the only honest way to describe the
        machine every developer actually has: a `python3` on PATH that is not
        the pack's virtualenv. Patching `sys.modules` cannot describe it,
        because a name bound to `None` defeats the second attempt as well as
        the first, which is exactly the attempt under test.

        The run happens in a copy of the pack rather than in this checkout so
        that `library` is the only thing that varies between the cases below.
        Pointed at the real `.venv` there would be no way to write the absent
        case at all, and the provisioned case would quietly depend on whether
        CI had run `make setup` before the tests.

        `PYTHONPATH` is set rather than inherited, and `-S` is why that is not
        redundant: no `site` module runs, but the interpreter still reads the
        variable, so a developer or a harness with one pointing at another
        `sd_db` hands the child a library the case did not put there. That is
        not hypothetical -- `.github/scripts/run-tests.sh` exports one, and
        the tests below turned green for the wrong reason under any value of
        it that holds the package. The copied pack's `bin` is what the child
        needs and all it needs, which is the same override the analogous
        `tests/test_status_source.py` cases make.
        """
        pack = self.directory / f"pack-{library}"
        shutil.copytree(ROOT / "bin", pack / "bin")
        # The pack's own `lib/` is where `import_sd_db` looks, and the only place (sd:3280).
        site = pack / "lib"
        site.mkdir()
        # A real copy of the package, and deliberately NOT a symlink to
        # `SITE_PACKAGES`. Symlinking the whole directory worked -- the only
        # writes below are in the `broken` arm, which never symlinks -- but it
        # handed a live path into the developer's own virtualenv to a
        # subprocess, one careless future case away from writing through it.
        # That is not hypothetical: it happened while this change was being
        # reviewed, through exactly this shape, and truncated the installed
        # `sd_db/__init__.py`. Copying costs 36ms for 2.1MB and the fixture
        # then owns every byte under `site`. Only `sd_db` is copied, not the
        # 92MB directory around it, which is enough because its dist-info
        # declares no `Requires-Dist`.
        if library == "provisioned":
            # `__pycache__` is excluded rather than copied: `copy2` preserves
            # mtime, so a copied `.pyc` validates against its copied `.py` and
            # the child would load bytecode whose embedded `co_filename` names
            # the original absolute path. Harmless for these three cases --
            # `__file__` comes from the loader, not the bytecode -- but the
            # fixture exists to make the child's library unambiguous, and
            # leaving the question open costs more than the one argument.
            shutil.copytree(SITE_PACKAGES / "sd_db", site / "sd_db",
                            ignore=shutil.ignore_patterns("__pycache__"))
        elif library == "broken":
            # `elif`, so the exclusivity the safety of this rests on is stated
            # rather than left to the caller's three string literals.
            (site / "sd_db").mkdir()
            (site / "sd_db/__init__.py").write_text("raise ImportError('the provisioned copy is broken')\n")
        environment = {**self.environment, "PYTHONPATH": str(pack / "bin")}
        return subprocess.run([sys.executable, "-S", str(pack / "bin/sd-ship"), command, "--item", str(self.item), "--json", *extra],
                              cwd=self.root, env=environment, text=True, capture_output=True, timeout=60)

    def test_a_python3_without_the_library_ships_off_the_provisioned_copy(self):
        """The defect was every verb on every machine, not a verb on a rare one.

        `make setup` provisions `sd_db` into the pack's virtualenv and this
        entrypoint starts `#!/usr/bin/env python3`, so the import at the top
        of `main` failed under every interpreter anyone actually invokes it
        with, and the tool answered "install matching sd_db" about a library
        that was installed. `bin/sd` never showed it because `sd_lib` has
        always made the second attempt; the difference between the two tools
        was six lines, and this asserts they no longer differ.

        Asserting the happy path proves nothing here: run under an
        interpreter that already has the library, the one-try code passes.
        """
        self.prepare()
        result = self.unsited("observe")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout)
        self.assertTrue(observed["ok"], result.stdout)
        self.assertEqual(observed["phase"], "ready_to_send")

    def test_a_library_absent_everywhere_still_refuses_in_the_shape_callers_read(self):
        """A machine before `make setup` must get a refusal, not a traceback.

        The fallback is allowed to find the library; it is not allowed to
        change what happens when there is nothing to find. Hooks and skills
        branch on this object rather than on the exit status alone, so the
        shape is the contract and the sentence has to name the library.
        """
        self.prepare()
        result = self.unsited("observe", library="absent")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        refusal = json.loads(result.stdout)
        self.assertFalse(refusal["ok"])
        self.assertTrue(refusal["manualRequired"])
        self.assertIn("sd_db is not installed here", refusal["error"])
        self.assertNotIn("Traceback", result.stderr)

    def test_a_provisioned_copy_that_will_not_import_is_not_called_absent(self):
        """Two faults, two remedies, and the old message named the wrong one.

        A virtualenv holding an `sd_db` that raises on import is not a machine
        without the library, and telling its reader to install one sends them
        to `make setup` for a package already sitting there. The refusal is
        the same shape as the one above -- what differs is the sentence.
        """
        self.prepare()
        result = self.unsited("observe", library="broken")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        refusal = json.loads(result.stdout)
        self.assertFalse(refusal["ok"])
        self.assertTrue(refusal["manualRequired"])
        self.assertIn("the provisioned copy is broken", refusal["error"])
        self.assertIn("will not import", refusal["error"])
        self.assertNotIn("is not installed here", refusal["error"])

    def test_the_three_library_cases_do_not_read_an_sd_db_from_the_run_around_them(self):
        """The three above describe machines, so no ambient variable may pick one.

        `-S` stops `site` from running; it does not stop the interpreter from
        reading `PYTHONPATH`. Inherit that from the test run and the child's
        library is whatever the developer or the harness happened to export:
        with an `sd_db` on it the absent case imports one and stops being
        absent, the broken case never reaches the copy it broke, and the
        provisioned case passes without the fallback it exists to prove --
        three green tests asserting nothing about the code under them.

        A stub is enough to show it. The cases turn on *which* `sd_db`
        answers, and a package with no `ship` submodule is a different answer
        from the pack's copy in every one of the three.
        """
        decoy = self.directory / "decoy"
        (decoy / "sd_db").mkdir(parents=True)
        (decoy / "sd_db/__init__.py").write_text("# importable, and not the pack's library\n")
        self.environment["PYTHONPATH"] = str(decoy)
        self.prepare()

        absent = self.unsited("observe", library="absent")
        self.assertEqual(absent.returncode, 3, absent.stdout + absent.stderr)
        self.assertIn("sd_db is not installed here", json.loads(absent.stdout)["error"])

        broken = self.unsited("observe", library="broken")
        self.assertEqual(broken.returncode, 3, broken.stdout + broken.stderr)
        self.assertIn("the provisioned copy is broken", json.loads(broken.stdout)["error"])

        provisioned = self.unsited("observe")
        self.assertEqual(provisioned.returncode, 0, provisioned.stdout + provisioned.stderr)
        self.assertEqual(json.loads(provisioned.stdout)["phase"], "ready_to_send")

    def test_repository_lock_cannot_be_owned_by_two_clones(self):
        from sd_db.workflow import WorkflowError
        with receipts.repository_lock(self.database, "fixture/repo"):
            with self.assertRaisesRegex(WorkflowError, "another ship"):
                with receipts.repository_lock(self.database, "fixture/repo"):
                    self.fail("second owner")

    def hold_ship_lock(self, repository, seconds=60.0):
        """A child process holding the real flock on `repository`'s ship lock until killed."""
        child = subprocess.Popen([sys.executable, "-c", SHIP_LOCK_HOLDER, str(self.database), repository, str(seconds)],
                                 stdout=subprocess.PIPE, text=True,
                                 env={**os.environ, "PYTHONPATH": str(SITE_PACKAGES)})

        def stop():
            child.kill()
            child.wait()
            child.stdout.close()

        self.addCleanup(stop)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    def locked_merge(self, *extra):
        """Merge's arguments: since sd:1938 prepare takes no ship lock, so merge is the verb that waits for it."""
        return self.args("merge", "--manual", "--expected-head", "0" * 40, *extra)

    def test_the_ship_lock_records_this_merge_as_its_holder(self):
        """sd:1936. The lock names this command and item while dispatch holds it."""
        seen = []

        def merge(operation):
            seen.extend(receipts.held_locks(self.database))
            return {"ok": True}

        with patch.object(ship.Ship, "merge", merge):
            ship.dispatch(self.root, self.connection, self.database, self.locked_merge(), receipts, False)
        (record,) = seen
        self.assertEqual(record["pid"], os.getpid())
        self.assertEqual(record["item"], self.item)
        self.assertEqual(record["command"], f"sd-ship merge --item {self.item}")
        self.assertEqual(receipts.held_locks(self.database), [])

    def test_a_held_ship_lock_refusal_names_the_holder(self):
        """sd:1936. A merge is refused with the holder's pid and command."""
        from sd_db.workflow import WorkflowError
        child = self.hold_ship_lock(self.operation().repository)
        with patch.object(ship.Ship, "merge", side_effect=AssertionError("ran under a held lock")):
            with self.assertRaises(WorkflowError) as refused:
                ship.dispatch(self.root, self.connection, self.database, self.locked_merge(), receipts, False)
        self.assertIn("another ship operation owns this repository", str(refused.exception))
        self.assertIn(f"pid {child.pid}", str(refused.exception))
        self.assertIn("sd-ship prepare --item 1872", str(refused.exception))

    def test_merge_wait_refuses_at_its_deadline_and_names_the_holder(self):
        """sd:1937. `--wait 5` against a held lock refuses after about 5 s, naming the holder."""
        from sd_db.workflow import WorkflowError
        child = self.hold_ship_lock(self.operation().repository)
        started = time.monotonic()
        with patch.object(ship.Ship, "merge", side_effect=AssertionError("ran under a held lock")):
            with self.assertRaises(WorkflowError) as refused:
                ship.dispatch(self.root, self.connection, self.database, self.locked_merge("--wait", "5"), receipts, False)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 5)
        self.assertLess(elapsed, 5 + receipts.WAIT_POLL_SECONDS + 2)
        self.assertIn(f"pid {child.pid}", str(refused.exception))
        self.assertIn("waited 5s", str(refused.exception))

    def test_merge_wait_runs_once_the_holder_releases(self):
        """sd:1937. With the lock released at 1 s, `--wait 60` runs the merge once.

        The wait is far past the release: a 5 s wait against a 2 s holder lost the race
        when gate load slowed the holder's release (sd:6855 review).
        """
        self.hold_ship_lock(self.operation().repository, seconds=1)
        started = time.monotonic()
        with patch.object(ship.Ship, "merge", return_value={"ok": True}) as merge:
            ship.dispatch(self.root, self.connection, self.database, self.locked_merge("--wait", "60"), receipts, False)
        self.assertEqual(merge.call_count, 1)
        self.assertGreaterEqual(time.monotonic() - started, 0.5, "the merge waited for the holder")

    def test_without_wait_a_held_lock_refuses_at_once(self):
        self.hold_ship_lock(self.operation().repository)
        # sd:6489: at once means no poll, not a wall-clock bound that flakes under load. Only the lock helper's
        # sleep counts: a process-wide patch also caught an unrelated sleep under gate load (2026-10-30 03:08 MDT).
        from unittest import mock

        from sd_db import runner_journal

        polled = mock.Mock(wraps=runner_journal.time, sleep=mock.Mock(side_effect=AssertionError("the lock polled")))
        with patch.object(ship.Ship, "merge", side_effect=AssertionError("ran under a held lock")), \
                patch.object(runner_journal, "time", polled):
            with self.assertRaisesRegex(Exception, "another ship operation owns this repository"):
                ship.dispatch(self.root, self.connection, self.database,
                              self.args("merge", "--manual", "--expected-head", "0" * 40), receipts, False)

    def test_wait_takes_finite_non_negative_seconds_on_prepare_and_merge(self):
        for command in ("prepare", "merge"):
            extra = ("--expected-head", "0" * 40) if command == "merge" else ()
            self.assertEqual(self.args(command, *extra, "--wait", "2.5").lock_wait, 2.5)
            self.assertEqual(self.args(command, *extra).lock_wait, 0)
            for bad in ("-1", "inf", "nan"):
                with self.subTest(command=command, value=bad), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        self.args(command, *extra, "--wait", bad)

    def test_a_branch_switch_while_waiting_refuses_before_the_command_runs(self):
        """sd:1937 review. The identity `Ship` read before the lock is checked again under it."""
        real = receipts.repository_lock

        @contextlib.contextmanager
        def switching(*args, **options):
            with real(*args, **options):
                _git(self.root, "checkout", "-q", "-b", "elsewhere")
                yield

        with patch.object(receipts, "repository_lock", switching), \
                patch.object(ship.Ship, "merge", side_effect=AssertionError("ran on a switched checkout")):
            with self.assertRaisesRegex(ship.Refusal, "left .* while this command waited"):
                ship.dispatch(self.root, self.connection, self.database, self.locked_merge(), receipts, False)

    def test_an_item_reassigned_while_waiting_refuses_before_the_command_runs(self):
        """sd:1937 review. The item row is read again under the lock, not trusted from before it."""
        real = receipts.repository_lock

        @contextlib.contextmanager
        def reassigning(*args, **options):
            with real(*args, **options):
                self.connection.execute("UPDATE item SET branch = 'elsewhere' WHERE id = ?", (self.item,))
                self.connection.commit()
                yield

        with patch.object(receipts, "repository_lock", reassigning), \
                patch.object(ship.Ship, "merge", side_effect=AssertionError("ran for a reassigned item")):
            with self.assertRaisesRegex(ship.Refusal, "changed repository or branch while this command waited"):
                ship.dispatch(self.root, self.connection, self.database, self.locked_merge(), receipts, False)

    def test_a_repository_remote_changed_while_waiting_refuses_before_the_command_runs(self):
        """sd:1937 review. The repository row's remote is read again under the lock, as `Ship.__init__` reads it."""
        real = receipts.repository_lock

        @contextlib.contextmanager
        def re_registering(*args, **options):
            with real(*args, **options):
                self.connection.execute("UPDATE repo SET remote = 'https://github.com/example/elsewhere.git'")
                self.connection.commit()
                yield

        with patch.object(receipts, "repository_lock", re_registering), \
                patch.object(ship.Ship, "merge", side_effect=AssertionError("ran for a re-registered repository")):
            with self.assertRaisesRegex(ship.Refusal, "repository remote changed while this command waited"):
                ship.dispatch(self.root, self.connection, self.database, self.locked_merge(), receipts, False)

    def test_a_no_item_record_refuses_a_live_branch_switch(self):
        """sd:1937 review. `--no-item` compares the live checkout too, not only its stored record."""
        delivery = SimpleNamespace(branch="topic", checkout_branch="topic", repository=ship.slug(REMOTE_URL))
        _git(self.root, "checkout", "-q", "-b", "elsewhere")
        with self.assertRaisesRegex(ship.Refusal, "the checkout left topic"):
            ship.identity_unchanged(self.root, self.connection, SimpleNamespace(no_item=True), receipts, delivery)
        _git(self.root, "checkout", "-q", "topic")
        ship.identity_unchanged(self.root, self.connection, SimpleNamespace(no_item=True), receipts, delivery)

    def test_a_remote_change_alone_refuses(self):
        """sd:1937 PR review. The branch is unchanged, so only the remote comparison can refuse."""
        delivery = SimpleNamespace(branch="topic", checkout_branch="topic", repository=ship.slug(REMOTE_URL))
        _git(self.root, "checkout", "-q", "topic")
        _git(self.root, "remote", "set-url", "origin", "https://github.com/example/elsewhere.git")
        with self.assertRaisesRegex(ship.Refusal, "the checkout left topic"):
            ship.identity_unchanged(self.root, self.connection, SimpleNamespace(no_item=True), receipts, delivery)

    def test_an_older_library_locks_without_options_and_refuses_a_wait(self):
        """An sd_db without the holder record still serializes; it cannot wait, so --wait refuses."""
        older = SimpleNamespace(repository_lock=receipts.repository_lock)
        self.assertEqual(ship.lock_options(self.args("prepare"), older), {})
        with self.assertRaisesRegex(ship.Refusal, "--wait needs an sd_db whose ship lock can wait") as caught:
            ship.lock_options(self.args("prepare", "--wait", "5"), older)
        self.assertNotIn("local-sd-db", str(caught.exception))

    def test_ending_assignment_keeps_work_and_manual_merge_controls_guarded(self):
        from sd_db.progress import cancel_work, work_controls
        from sd_db.workflow import WorkflowError
        create_assignment(self.connection, item=self.item, role="author", status="ending")
        self.assertFalse(work_controls(self.connection, self.item)["cancel"])
        with self.assertRaisesRegex(WorkflowError, "ending assignment"):
            cancel_work(self.connection, self.item, reason="stop", who="user")
        with self.assertRaisesRegex(WorkflowError, "manual merge authority"):
            receipts.manual_merge_guard(self.connection, str(self.operator))

    def test_changed_acceptance_scope_refuses_before_spending_a_fix_review(self):
        from sd_db import set_item_fields
        acceptance = self.directory / "acceptance.json"
        acceptance.write_text(json.dumps({"item": self.item, "complete": True, "criteria": [
            {"criterion": "actual scope", "passed": True, "evidence": "fixture-check-pass"}]}))
        self.prepare("--deliver", "--acceptance-file", str(acceptance))
        before = self.operation().state
        set_item_fields(self.connection, self.item, body="new scope")
        with self.assertRaisesRegex(ship.Refusal, "acceptance scope changed"):
            self.prepare()
        self.assertEqual(self.operation().state, before)

    def test_author_assignment_cannot_borrow_manual_merge_authority(self):
        from sd_db.workflow import WorkflowError
        self.prepare()
        create_assignment(self.connection, item=self.item, role="author", status="running")
        with self.assertRaisesRegex(WorkflowError, "manual merge authority"):
            self.merge()
        self.assertFalse(any(call.method == "PUT" for call in self.remote.calls))

    def test_standing_review_revocation_invalidates_receipt_but_unrelated_settings_do_not(self):
        config = self.home / ".config/sd-ai-command-pack/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"config":{"sd":{"external_reviews":"configured"}}}')
        self.prepare()
        operation = self.operation()
        head = _git(self.root, "rev-parse", "HEAD")
        config.write_text('{"config":{"sd":{"external_reviews":"configured","assistant_merge":"controlled"}},"unrelated":1}')
        operation.check_review(head)
        config.write_text('{"config":{"sd":{"external_reviews":"deny"}}}')
        with self.assertRaisesRegex(ship.Refusal, "policy changed"):
            self.merge()
        self.assertFalse(any(call.method == "PUT" for call in self.remote.calls))

    def test_absent_to_dangling_local_config_cannot_reuse_a_completed_review(self):
        config = self.home / ".config/sd-ai-command-pack/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"config":{"sd":{"external_reviews":"configured"}}}')
        local = self.root / "CLAUDE.local.md"
        local.unlink()
        self.prepare()
        operation = self.operation()
        head = _git(self.root, "rev-parse", "HEAD")
        operation.check_review(head)
        local.symlink_to(self.root / "missing-config")
        with self.assertRaisesRegex(ship.sd_lib.ConfigError, "dangling local configuration link"):
            operation.check_review(head)
        self.assertFalse(any(call.method == "PUT" for call in self.remote.calls))

    def test_linked_worktree_binding_changes_when_main_checkout_consent_is_revoked(self):
        linked = self.directory / "linked"
        _git(self.root, "worktree", "add", "--detach", str(linked), "HEAD")
        before = ship.binding(linked)
        local = self.root / "CLAUDE.local.md"
        local.write_text('<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\nreviewers: ""\n<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n')
        self.assertEqual(ship.sd_lib.local_block(linked)["reviewers"], "")
        self.assertNotEqual(ship.binding(linked), before)

    def test_new_review_configuration_invalidates_the_saved_receipt(self):
        """A key in the block binds; a note outside it does not (sd:2854)."""
        self.prepare()
        local = self.root / "CLAUDE.local.md"
        with local.open("a") as stream:
            stream.write("\nconfiguration noted outside the block\n")
        self.operation().check_review(_git(self.root, "rev-parse", "HEAD"))
        local.write_text(local.read_text().replace("mode: full", "mode: full\nguest_allow: docs/decisions"))
        with self.assertRaisesRegex(ship.Refusal, "policy changed after review: CLAUDE.local.md \\(policy\\)$"):
            self.merge()

    def set_written_mode(self, word):
        local = self.root / "CLAUDE.local.md"
        local.write_text(local.read_text().replace("mode: full", f"mode: {word}"))

    def test_written_guest_mode_has_no_merge_authority(self):
        self.set_written_mode("guest")
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        with self.assertRaisesRegex(ship.Refusal, "guest mode has no merge authority"):
            self.merge()
        self.assertFalse(any(call.method == "PUT" for call in self.remote.calls))

    def test_merge_authority_reads_the_one_validated_mode_line(self):
        """The merge-authority check reads `sd_lib.written_mode`, not the raw line (sd:930).

        On the real path a `mode:` word that is not a mode never reaches the
        check: `prepare` refuses it through `resolve_mode`, and a line changed
        after `prepare` trips the binding in `check_review` first. So this
        test stands the binding guard down to put the misspelled word in front
        of the check itself, and asks that the check name the word the way
        every other reader of the line does, rather than compare it to
        `"guest"` and let it through.
        """
        self.prepare()
        self.set_written_mode("gust")
        with patch.object(ship.Ship, "check_review", return_value=None):
            with self.assertRaisesRegex(ship.sd_lib.ConfigError, "mode 'gust' is not one of"):
                self.merge()
        self.assertFalse(any(call.method == "PUT" for call in self.remote.calls))

    def watched_merge(self, ci):
        """`merge --watch` under `repo.ci = ci`, stopped at readiness; the `run` calls it made."""
        self.prepare()
        stop = ship.Refusal("stopped at readiness")
        with (patch.object(ship.sd_lib, "repo_ci", return_value=ci),
              patch.object(ship.sd_local_gate, "local_gate", return_value={"status": "success"}),
              patch.object(ship.GitHub, "ready", side_effect=stop),
              patch.object(ship, "run", wraps=ship.run) as calls):
            with self.assertRaises(ship.Refusal) as raised:
                self.merge("--watch")
        return raised.exception, [call.args[1][:3] for call in calls.call_args_list]

    def test_watch_waits_on_the_local_gate_under_ci_local(self):
        """sd:1875. A `ci = local` pull request has no remote check to watch, and
        `gh pr checks --watch` fails on one that has none; the local gate is the wait."""
        refusal, calls = self.watched_merge("local")
        self.assertEqual(str(refusal), "stopped at readiness")
        self.assertNotIn(["gh", "pr", "checks"], calls)

    def test_watch_still_watches_remote_checks_under_ci_github(self):
        refusal, calls = self.watched_merge("github")
        self.assertIn(["gh", "pr", "checks"], calls)
        self.assertNotEqual(str(refusal), "stopped at readiness")

    def test_delivery_clone_identity_cannot_be_replaced(self):
        from sd_db.progress import deliver_work
        from sd_db.workflow import WorkflowError
        self.prepare()
        result = self.merge()
        _git(self.root, "remote", "set-url", "origin", "https://github.com/other/repo.git")
        with self.assertRaisesRegex(WorkflowError, "origin does not match"):
            deliver_work(self.connection, self.item, result["merge_commit"], who="sd-ship", verification_root=self.root)

    def test_forged_delivery_trailer_cannot_turn_a_slice_into_completion(self):
        """sd:2999: the body's `Delivers:` line is stripped, so only `--deliver` delivers."""
        body = self.directory / "body.txt"
        body.write_text(f"A slice\n\nDelivers: sd:{self.item}\n")
        self.prepare("--body-file", str(body))
        self.merge()
        self.assertNotEqual(ship.sd_lib.delivered(self.root, f"sd:{self.item}"), ship.sd_lib.YES)

    def test_a_blocking_review_names_its_findings_and_the_command_that_prints_them(self):
        """sd:2102. The refusal said `see item ship receipt`, and nothing in the CLI printed the receipt.

        The operator dug each finding out of the `state` table by hand. The
        refusal now names every blocking finding, the JSON carries them as
        `findings`, and `next_action` names the command that prints them whole.
        Both prepares refuse that way: the one that ran the review, and the one
        that reuses its receipt.
        """
        program = self.programs / "review-fixture"
        long = "the value is wrong " * 40
        payload = {"type": "result", "subtype": "success", "structured_output": {"findings": [
            {"path": "src.py", "line": 1, "severity": "high", "family": "correctness", "summary": long}]}}
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        head = _git(self.root, "rev-parse", "HEAD")
        for attempt in ("review", "reuse"):
            with self.subTest(attempt), self.assertRaises(ship.Refusal) as caught:
                self.prepare()
            message = str(caught.exception)
            self.assertIn("high src.py:1 the value is wrong", message)
            self.assertLess(len(message), 600)
            command = f"sd-ship adjudicate --item {self.item} --expected-head {head} --json"
            self.assertIn(command, caught.exception.workflow["next_action"])
            self.assertEqual(caught.exception.workflow["blocker"]["code"], "review_blocking")
            findings = ship.failure("prepare", caught.exception)["findings"]
            self.assertEqual([(row["severity"], row["path"], row["line"]) for row in findings], [("high", "src.py", 1)])
            self.assertTrue(findings[0]["summary"].endswith("..."))
            self.assertLess(len(findings[0]["summary"]), len(long))

    def test_fix_verifications_keep_prior_findings_and_refuse_a_pass_past_the_cap(self):
        program = self.programs / "review-fixture"
        payload = {"type": "result", "subtype": "success", "structured_output": {"findings": [
            {"path": "src.py", "line": 1, "severity": "high", "family": "correctness", "summary": "value is wrong"}]}}
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        with self.assertRaisesRegex(ship.Refusal, "local review blocking"):
            self.prepare()
        first = self.operation().state["passes"][0]
        (self.root / "src.py").write_text("value = 2\n")
        _git(self.root, "add", "src.py")
        _git(self.root, "commit", "-m", "correct value\n\nAuthored-with: human")
        payload["structured_output"]["findings"] = []
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        prepared = self.prepare()
        state = self.operation().state
        self.assertEqual(len(state["passes"]), 2)
        self.assertEqual(state["passes"][1]["report"]["subject"]["base"], first["head"])
        self.assertEqual(state["passes"][1]["report"]["verification_report_digest"], ship.digest(first["report"]))
        self.assertEqual(state["passes"][0]["report"]["findings"][0]["summary"], "value is wrong")
        for count in range(3, CAP + 1):
            previous = self.operation().state["passes"][-1]
            _git(self.root, "commit", "--allow-empty", "-m", f"change {count}\n\nAuthored-with: human")
            prepared = self.prepare()
            state = self.operation().state
            self.assertEqual(len(state["passes"]), count)
            self.assertEqual(state["passes"][-1]["report"]["subject"]["base"], previous["head"])
            self.assertEqual(state["passes"][-1]["report"]["verification_report_digest"],
                             ship.digest(previous["report"]))
        self.assertEqual(state["passes"][0]["report"]["findings"][0]["summary"], "value is wrong")
        _git(self.root, "commit", "--allow-empty", "-m", "past the cap\n\nAuthored-with: human")
        with self.assertRaisesRegex(ship.Refusal, "spent"):
            self.prepare()
        self.assertEqual(self.remote.rev_parse("topic"), prepared["reviewed_head"])

    def test_an_amended_reviewed_head_refuses_by_name_and_not_as_retryable(self):
        """sd:1348: `--is-ancestor` answers by exit status and prints nothing.

        The refusal arrived as the bare runtime string 'git failed', marked
        retryable, although no retry of the same command can pass.
        """
        program = self.programs / "review-fixture"
        payload = {"type": "result", "subtype": "success", "structured_output": {"findings": [
            {"path": "src.py", "line": 1, "severity": "high", "family": "correctness", "summary": "value is wrong"}]}}
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        with self.assertRaisesRegex(ship.Refusal, "local review blocking"):
            self.prepare()
        reviewed = self.operation().state["passes"][0]["head"]
        (self.root / "src.py").write_text("value = 2\n")
        _git(self.root, "add", "src.py")
        _git(self.root, "commit", "--amend", "--no-edit")
        amended = _git(self.root, "rev-parse", "HEAD")
        with self.assertRaises(ship.Refusal) as caught:
            self.prepare()
        message = str(caught.exception)
        self.assertIn(reviewed, message)
        self.assertIn(amended, message)
        self.assertIn("amend or a rebase", message)
        workflow = caught.exception.workflow
        self.assertEqual(workflow["blocker"]["code"], "reviewed_head_orphaned")
        self.assertFalse(workflow["blocker"]["retryable"])
        self.assertIn(f"git reset --soft {reviewed}", workflow["next_action"])
        # The refusal spends nothing: the receipt still holds the one pass.
        self.assertEqual(len(self.operation().state["passes"]), 1)
        # The remedy it names restores the ancestry and the fix verification runs.
        _git(self.root, "reset", "--soft", reviewed)
        _git(self.root, "commit", "-m", "correct value\n\nAuthored-with: human")
        payload["structured_output"]["findings"] = []
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        self.prepare()
        self.assertEqual(self.operation().state["passes"][1]["report"]["subject"]["base"], reviewed)

    def amend_after_a_blocking_review(self):
        """One blocking review, then the reviewed commit amended away: (reviewed, amended, payload, program).

        The amended commit is one the remote never had, as with a privacy
        amend: the fixture's first commit is on the remote already, and
        rewriting it would need the force push sd-ship never makes.
        """
        (self.root / "src.py").write_text("value = 3\n")
        _git(self.root, "add", "src.py")
        _git(self.root, "commit", "-m", "change\n\nAuthored-with: human")
        program = self.programs / "review-fixture"
        payload = {"type": "result", "subtype": "success", "structured_output": {"findings": [
            {"path": "src.py", "line": 1, "severity": "high", "family": "correctness", "summary": "value is wrong"}]}}
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        with self.assertRaisesRegex(ship.Refusal, "local review blocking"):
            self.prepare()
        reviewed = self.operation().state["passes"][0]["head"]
        (self.root / "src.py").write_text("value = 2\n")
        _git(self.root, "add", "src.py")
        _git(self.root, "commit", "--amend", "--no-edit")
        return reviewed, _git(self.root, "rev-parse", "HEAD"), payload, program

    def test_a_rewritten_reviewed_head_restarts_its_review_with_a_reason(self):
        """sd:2600: an amend that must not be undone gets a fresh review on the item path.

        A privacy amend drops a line that may not reach a public remote, so the
        `git reset --soft` remedy -- which publishes the reviewed commit -- is
        not open. `--restart-review REASON` keeps the orphaned passes, with
        the reason, and reviews the whole branch again from nothing, counting
        the new pass against the cap with the ones it set aside.
        """
        reviewed, amended, payload, program = self.amend_after_a_blocking_review()
        with self.assertRaises(ship.Refusal) as caught:
            self.prepare()
        said = caught.exception.workflow["next_action"]
        self.assertIn("--restart-review", said)
        self.assertIn("must not be pushed", said)
        self.assertLess(said.index("--restart-review"), said.index(f"git reset --soft {reviewed}"),
                        "the reset stays, as the second option")
        self.assertIn("only edited a commit message", said)
        payload["structured_output"]["findings"] = []
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        prepared = self.prepare("--restart-review", "privacy amend dropped a flagged path")
        self.assertEqual(prepared["reviewed_head"], amended)
        state = self.operation().state
        self.assertEqual([entry["head"] for entry in state["passes"]], [amended])
        self.assertIsNone(state["passes"][0]["base"], "the restart reviews the whole branch")
        self.assertEqual(state["passes"][0]["report"]["status"], "clean")
        [restart] = state["superseded_reviews"]
        self.assertEqual(restart["reason"], "privacy amend dropped a flagged path")
        self.assertEqual(restart["head"], amended)
        self.assertEqual(restart["orphaned"], [reviewed])
        self.assertEqual([entry["head"] for entry in restart["passes"]], [reviewed])
        self.assertEqual(restart["passes"][0]["report"]["findings"][0]["summary"], "value is wrong",
                         "the orphaned review's findings stay in the receipt")
        self.assertEqual(ship.ItemHistory().spent(state), 2, "the set-aside pass still counts against the cap")

    def drop_from_the_object_store(self, commit: str) -> None:
        """Remove `commit` from the checkout's object store, as gc does to an unreachable amend."""
        _git(self.root, "reflog", "expire", "--expire=now", "--all")
        _git(self.root, "gc", "--quiet", "--prune=now")
        with self.assertRaises(RuntimeError, msg="the commit is still there"):
            _git(self.root, "cat-file", "-e", f"{commit}^{{commit}}")

    def test_a_restart_sets_aside_a_reviewed_head_the_object_store_no_longer_holds(self):
        """sd:3167: the orphaned head gc removed is set aside, not a 'Not a valid commit name' runtime failure."""
        reviewed, amended, payload, program = self.amend_after_a_blocking_review()
        self.drop_from_the_object_store(reviewed)
        payload["structured_output"]["findings"] = []
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        prepared = self.prepare("--restart-review", "the reviewed head was pruned")
        self.assertEqual(prepared["reviewed_head"], amended)
        [restart] = self.operation().state["superseded_reviews"]
        self.assertEqual(restart["orphaned"], [reviewed])

    def test_a_restart_refuses_while_every_reviewed_head_is_reachable(self):
        """sd:2600: a restart is for an orphaned review, not a way to drop live findings."""
        program = self.programs / "review-fixture"
        payload = {"type": "result", "subtype": "success", "structured_output": {"findings": [
            {"path": "src.py", "line": 1, "severity": "high", "family": "correctness", "summary": "value is wrong"}]}}
        program.write_text("#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(payload)) + ")\n")
        with self.assertRaisesRegex(ship.Refusal, "local review blocking"):
            self.prepare()
        _git(self.root, "commit", "--allow-empty", "-m", "fix\n\nAuthored-with: human")
        before = self.operation().state
        with self.assertRaisesRegex(ship.Refusal, "only when a reviewed head is not an ancestor"):
            self.prepare("--restart-review", "no reason to restart")
        self.assertEqual(self.operation().state["passes"], before["passes"])
        self.assertNotIn("superseded_reviews", self.operation().state)

    def test_a_restart_needs_a_reason_and_combines_with_no_other_review_flag(self):
        reviewed, amended, _payload, _program = self.amend_after_a_blocking_review()
        for extra, said in ((["--restart-review", "  "], "nonempty REASON"),
                            (["--restart-review", "amend", "--retry-review"], "does not combine"),
                            (["--restart-review", "amend", "--catch-up"], "does not combine"),
                            (["--restart-review", "amend", "--additional-review-for", amended,
                              "--request-reason", "more"], "does not combine")):
            with self.subTest(extra=extra), self.assertRaisesRegex(ship.Refusal, said):
                self.prepare(*extra)
            state = self.operation().state
            self.assertEqual([entry["head"] for entry in state["passes"]], [reviewed])
            self.assertNotIn("superseded_reviews", state)

    def test_tampered_fix_receipt_cannot_claim_original_blockers_were_verified(self):
        self.prepare()
        _git(self.root, "commit", "--allow-empty", "-m", "fix\n\nAuthored-with: human")
        self.prepare()
        operation = self.operation()
        operation.state["passes"][1]["report"]["verification_report_digest"] = "invented"
        operation.save()
        with self.assertRaisesRegex(ship.Refusal, "fix verification"):
            self.merge()

    def test_an_empty_diff_branch_ships_without_a_provider_call(self):
        # sd:1405. An empty commit can be a whole change. `sd-review`
        # refuses an empty subject, so the ship lane waives review for a
        # branch that changes no file, and calls no reader for it.
        calls = self.directory / "provider-calls"
        (self.programs / "review-fixture").write_text(
            "#!/usr/bin/env python3\nimport json, pathlib\n"
            f"pathlib.Path({str(calls)!r}).open('a').write('call\\n')\n"
            "print(json.dumps({'type':'result','subtype':'success','structured_output':{'findings':[]}}))\n")
        _git(self.root, "checkout", "-q", "-b", "repair", "origin/main")
        _git(self.root, "commit", "--allow-empty", "-m", "repair\n\nAuthored-with: human")
        self.item = create_item(self.connection, kind="work", title="repair", status="in_progress",
                                repo=str(self.operator), branch="repair")
        self.assertEqual(self.prepare()["phase"], "ready_to_send")
        self.assertFalse(calls.exists(), "a reader was called for a branch that changes no file")
        state = self.operation().state
        self.assertEqual(state.get("passes") or [], [])
        head = _git(self.root, "rev-parse", "HEAD")
        self.assertEqual(state["reviewed_head"], head)
        # The gate publish and merge read. The fixture remote cannot squash an
        # empty diff, so the merge itself is not driven here.
        self.assertIsNone(self.operation().check_review(head))
        _git(self.root, "commit", "--allow-empty", "-m", "moved\n\nAuthored-with: human")
        with self.assertRaisesRegex(ship.Refusal, "no completed local review receipt"):
            self.operation().check_review(_git(self.root, "rev-parse", "HEAD"))
        self.assertFalse(calls.exists())

    def tiered_branch(self, files: dict[str, str], *, check: str = "true", policy: str | None = None) -> pathlib.Path:
        """sd:2998. A branch off main changing only `files`, a counting reader, and main's Makefile `check`.

        `policy` is the repository's review declaration, which no diff carries (sd:5109).
        """
        calls = self.directory / "provider-calls"
        (self.programs / "review-fixture").write_text(
            "#!/usr/bin/env python3\nimport json, pathlib\n"
            f"pathlib.Path({str(calls)!r}).open('a').write('call\\n')\n"
            "print(json.dumps({'type':'result','subtype':'success','structured_output':{'findings':[]}}))\n")
        if policy:
            self.declare_review(json.loads(policy))
        self.remote.commit_on("main", "gate\n\nAuthored-with: human", files={"Makefile": f"check:\n\t@{check}\n"})
        _git(self.root, "fetch", "-q", "origin")
        _git(self.root, "checkout", "-q", "-b", "tiered", "origin/main")
        for name, text in files.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text)
            _git(self.root, "add", name)
        _git(self.root, "commit", "-q", "-m", "tiered\n\nAuthored-with: human")
        self.item = create_item(self.connection, kind="work", title="tiered", status="in_progress",
                                repo=str(self.operator), branch="tiered")
        return calls

    def test_a_docs_only_branch_skips_the_model_review_and_still_gates(self):
        calls = self.tiered_branch({"README.md": "words\n", "docs/guide.md": "more words\n"})
        with contextlib.redirect_stderr(io.StringIO()) as said:
            result = self.prepare()
        self.assertEqual(result["phase"], "ready_to_send")
        self.assertEqual(result["review_skipped"], "docs-only")
        self.assertIn("review skipped: docs-only", said.getvalue())
        self.assertFalse(calls.exists(), "a reader was called for a docs-only branch")
        state, head = self.operation().state, _git(self.root, "rev-parse", "HEAD")
        self.assertEqual(state.get("passes") or [], [])
        self.assertEqual((state["review_skipped"]["tier"], state["review_skipped"]["head"]), ("docs-only", head))
        self.assertEqual(state["review_skipped"]["check"]["status"], "pass")
        self.assertEqual(state["reviewed_head"], head)
        self.assertIsNone(self.operation().check_review(head))
        recorded = state["review_skipped"]["recorded_at"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.prepare()["review_skipped"], "docs-only")
        self.assertEqual(self.operation().state["review_skipped"]["recorded_at"], recorded, "the gate ran twice at one head")
        self.double.statuses = [{"context": "check", "state": "success"}]
        self.assertEqual(self.merge()["phase"], "merged")

    def test_a_code_commit_after_a_skipped_review_reviews_the_whole_branch(self):
        calls = self.tiered_branch({"README.md": "words\n"})
        with contextlib.redirect_stderr(io.StringIO()):
            self.prepare()
        (self.root / "src.py").write_text("value = 3\n")
        _git(self.root, "add", "src.py")
        _git(self.root, "commit", "-q", "-m", "code\n\nAuthored-with: human")
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.prepare()
        self.assertNotIn("review_skipped", result)
        self.assertTrue(calls.exists())
        [reviewed] = self.operation().state["passes"]
        self.assertIsNone(reviewed["base"], "the first review after a skip must cover the whole branch")

    def test_a_malformed_review_policy_refuses_the_classification(self):
        # A row holds only JSON (sd:5109); the reader refuses this one's tier.
        calls = self.tiered_branch({"README.md": "words\n"}, policy='{"default_tier": "gold"}')
        with self.assertRaisesRegex(ship.Refusal, "review policy does not parse"):
            self.prepare()
        self.assertFalse(calls.exists())
        self.assertNotIn("review_skipped", self.operation().state)

    def test_a_docs_only_branch_whose_gate_fails_is_refused_without_a_review(self):
        calls = self.tiered_branch({"README.md": "words\n"}, check="exit 1")
        with self.assertRaisesRegex(ship.Refusal, "review skipped: docs-only; the repository gate failed"):
            self.prepare()
        self.assertFalse(calls.exists())
        state = self.operation().state
        self.assertIsNone(state.get("reviewed_head"))
        self.assertEqual(state["review_skipped"]["check"]["status"], "fail")
        with self.assertRaisesRegex(ship.Refusal, "no completed local review receipt"):
            self.operation().check_review(_git(self.root, "rev-parse", "HEAD"))

    def test_a_pack_pin_bump_skips_the_model_review(self):
        calls = self.tiered_branch({".sd-pack-rev": "0" * 40 + "\n"})
        with contextlib.redirect_stderr(io.StringIO()) as said:
            result = self.prepare()
        self.assertEqual(result["review_skipped"], "pin-bump")
        self.assertIn("review skipped: pin-bump", said.getvalue())
        self.assertFalse(calls.exists(), "a reader was called for a pin bump")
        _git(self.root, "commit", "--allow-empty", "-q", "-m", "moved\n\nAuthored-with: human")
        with self.assertRaisesRegex(ship.Refusal, "no completed local review receipt"):
            self.operation().check_review(_git(self.root, "rev-parse", "HEAD"))

    LEAD_KEY = "review:v1:fixture/repo:tiered"

    def lead_review(self, *extra: str) -> dict:
        """sd:5988. `sd-review --scope branch` of the checkout, as the lead runs it before enqueue."""
        done = subprocess.run([sys.executable, str(ROOT / "bin/sd-review"), "--scope", "branch", "--json", *extra],
                              cwd=self.root, capture_output=True, text=True, timeout=300, check=False,
                              env=clean_environment(HOME=os.environ["HOME"], XDG_CONFIG_HOME=os.environ["XDG_CONFIG_HOME"]))
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def lead_record(self) -> dict | None:
        row = self.connection.execute("SELECT body FROM state WHERE id = (SELECT MAX(id) FROM state "
                                      "WHERE kind = 'checkpoint' AND key = ?)", (self.LEAD_KEY,)).fetchone()
        return json.loads(row["body"]) if row else None

    def rewrite_lead_record(self, **changes) -> None:
        from sd_db.writes import record_state
        record_state(self.connection, "checkpoint", key=self.LEAD_KEY, body={**self.lead_record(), **changes})

    def lead_reviewed_branch(self) -> pathlib.Path:
        calls = self.tiered_branch({"src.py": "value = 2\n"})
        self.assertEqual(self.lead_review()["status"], "clean")
        self.assertEqual(self.lead_record()["head"], _git(self.root, "rev-parse", "HEAD"))
        calls.unlink()
        return calls

    def assert_full_review(self, calls: pathlib.Path, why: str) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as said:
            result = self.prepare()
        self.assertNotIn("review_skipped", result)
        self.assertIn(f"lead review not reused: {why}", said.getvalue())
        self.assertTrue(calls.exists(), "no reader reviewed a head the lead's pass does not cover")
        [reviewed] = self.operation().state["passes"]
        self.assertIsNone(reviewed["base"], "a lead pass the lane did not reuse is no base for a delta")

    def test_a_lead_review_of_the_head_stands_for_the_lane_review(self):
        calls = self.lead_reviewed_branch()
        head = _git(self.root, "rev-parse", "HEAD")
        with contextlib.redirect_stderr(io.StringIO()) as said:
            result = self.prepare()
        self.assertEqual(result["review_skipped"], "lead-review")
        self.assertIn(f"lead review reused: the clean pass of {head[:12]} covers it", said.getvalue())
        self.assertIn("review skipped: lead-review", said.getvalue())
        self.assertFalse(calls.exists(), "a reader was called for a head the lead's pass covers")
        state = self.operation().state
        self.assertEqual(state.get("passes") or [], [])
        self.assertEqual(state["review_skipped"]["check"]["status"], "pass")
        self.assertEqual(state["review_skipped"]["lead_review"]["head"], head)
        self.assertIsNone(self.operation().check_review(head))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.prepare()["review_skipped"], "lead-review")
        self.assertEqual(self.operation().state["review_skipped"]["recorded_at"], state["review_skipped"]["recorded_at"],
                         "the gate ran twice at one head")

    def test_a_clean_catch_up_merge_after_the_lead_review_is_carried(self):
        calls = self.lead_reviewed_branch()
        reviewed = _git(self.root, "rev-parse", "HEAD")
        self.remote.commit_on("main", "far work\n\nAuthored-with: human", files={"far/data.txt": "far\n"})
        _git(self.root, "fetch", "-q", "origin")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge origin/main\n\nAuthored-with: human", "origin/main")
        head = _git(self.root, "rev-parse", "HEAD")
        with contextlib.redirect_stderr(io.StringIO()) as said:
            result = self.prepare()
        self.assertEqual(result["review_skipped"], "lead-review")
        self.assertIn(f"lead review reused: the clean pass of {reviewed[:12]} covers it; {head[:12]} only merges "
                      "origin/main since", said.getvalue())
        self.assertFalse(calls.exists(), "a reader was called for a clean catch-up of a reviewed head")

    def test_a_code_commit_after_the_lead_review_gets_a_full_review(self):
        calls = self.lead_reviewed_branch()
        (self.root / "src.py").write_text("value = 3\n")
        _git(self.root, "commit", "-qam", "code\n\nAuthored-with: human")
        self.assert_full_review(calls, "it changed more than a clean merge of origin/main")

    def test_a_catch_up_merge_near_the_branch_gets_a_full_review(self):
        calls = self.lead_reviewed_branch()
        self.remote.commit_on("main", "near work\n\nAuthored-with: human", files={"other.py": "near = 1\n"})
        _git(self.root, "fetch", "-q", "origin")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "Merge origin/main\n\nAuthored-with: human", "origin/main")
        self.assert_full_review(calls, "it changed more than a clean merge of origin/main")

    def test_a_lead_review_under_other_review_tools_gets_a_full_review(self):
        calls = self.lead_reviewed_branch()
        self.rewrite_lead_record(binding={**self.lead_record()["binding"], "tools": "0" * 40})
        self.assert_full_review(calls, "the review tools or policy changed since")

    def test_a_lead_review_that_wanted_copilot_gets_the_lane_review(self):
        calls = self.lead_reviewed_branch()
        self.rewrite_lead_record(copilot_automatic=True)
        self.assert_full_review(calls, "it selects an automatic Copilot review")

    def assert_other_subject(self, why: str, **changes) -> None:
        calls = self.lead_reviewed_branch()
        self.rewrite_lead_record(**changes)
        self.assert_full_review(calls, why)

    def test_a_lead_record_naming_another_branch_is_not_reused(self):
        self.assert_other_subject("it names another subject", branch="elsewhere")

    def test_a_lead_record_naming_another_repository_is_not_reused(self):
        self.assert_other_subject("it names another subject", repository="fixture/other")

    def test_a_lead_record_that_is_not_clean_is_not_reused(self):
        self.assert_other_subject("it names another subject", status="advisory")

    def test_a_lead_review_from_a_local_only_base_is_not_reused(self):
        # A missing origin/HEAD makes sd-review diff from local main, which may
        # hold unpublished commits; prepare diffs from origin/main's fork.
        calls = self.tiered_branch({"src.py": "value = 2\n"})
        local_main = _git(self.root, "rev-parse", "HEAD")
        (self.root / "more.py").write_text("more = 1\n")
        _git(self.root, "add", "more.py")
        _git(self.root, "commit", "-qm", "more\n\nAuthored-with: human")
        self.assertEqual(self.lead_review()["status"], "clean")
        calls.unlink()
        self.rewrite_lead_record(base=local_main)
        self.assert_full_review(calls, f"it reviewed from {local_main[:12]}, not the fork from origin/main")

    def test_a_lead_record_of_a_commit_git_cannot_find_is_not_reused(self):
        self.assert_other_subject("git could not compare", head="f" * 40)

    def test_no_lead_review_is_logged_and_reviewed_in_full(self):
        calls = self.tiered_branch({"src.py": "value = 2\n"})
        with contextlib.redirect_stderr(io.StringIO()) as said:
            self.prepare()
        self.assertIn("lead review not reused: none recorded for tiered; full review", said.getvalue())
        self.assertTrue(calls.exists())

    def test_a_lead_record_that_does_not_read_is_unknown_and_reviewed_in_full(self):
        import sd_ship_review

        class Broken:
            def execute(self, *args):
                raise sqlite3.OperationalError("database is locked")

        self.assertEqual(sd_ship_review.lead_review(Broken(), self.root, "fixture/repo", "tiered", "0" * 40, "main", {}),
                         (None, "the record did not read (database is locked)"))

    def test_only_a_plain_clean_branch_review_is_recorded(self):
        self.tiered_branch({"src.py": "value = 2\n"})
        for extra in (["--provider", "reviewer"], ["--draft"]):
            with self.subTest(extra=extra):
                self.lead_review(*extra)
                self.assertIsNone(self.lead_record())
        review = ship.sd_lib.sibling("sd_review_lead_record", "sd-review")
        args = review.build_parser().parse_args(["--scope", "branch"])
        head = _git(self.root, "rev-parse", "HEAD")
        result = {"status": "advisory", "requested_reviews": 1, "completed_reviews": 1,
                  "subject": {"base": head, "head": head}}
        review.record_lead_review(self.root, args, result, dict(os.environ))
        self.assertIsNone(self.lead_record(), "an advisory pass was recorded")
        moved = {**result, "status": "clean", "subject": {"base": head, "head": "0" * 40}}
        with contextlib.redirect_stderr(io.StringIO()) as said:
            review.record_lead_review(self.root, args, moved, dict(os.environ))
        self.assertIsNone(self.lead_record(), "a pass of a head HEAD moved from was recorded")
        self.assertIn("not recorded for lane prepare: Refusal: HEAD moved", said.getvalue())
        with patch.object(review.sd_lib, "import_sd_db", side_effect=OSError("disk full")), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            review.record_lead_review(self.root, args, {**result, "status": "clean"}, dict(os.environ))
        self.assertIn("sd-review: the clean pass was not recorded for lane prepare: OSError: disk full",
                      said.getvalue())

    def assert_tier_reviews(self, files: dict[str, str], policy: str | None = None) -> None:
        calls = self.tiered_branch(files, policy=policy)
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.prepare()
        self.assertNotIn("review_skipped", result)
        self.assertTrue(calls.exists(), "no reader was called for a code change")
        self.assertEqual(len(self.operation().state["passes"]), 1)

    def test_a_mixed_docs_and_code_branch_is_reviewed(self):
        self.assert_tier_reviews({"README.md": "words\n", "src.py": "value = 2\n"})

    def test_a_skill_file_agents_execute_is_reviewed(self):
        self.assert_tier_reviews({"skills/sd-example/SKILL.md": "steps\n"})

    def test_a_design_record_is_reviewed(self):
        self.assert_tier_reviews({"docs/work/2026-10-09-example/design.md": "---\nitem: sd:1\n---\n\ndesign\n"})

    def test_a_never_skip_spec_is_reviewed(self):
        self.assert_tier_reviews({"docs/spec/example.md": "spec\n"})

    def test_a_pin_beside_prose_is_reviewed(self):
        self.assert_tier_reviews({".sd-pack-rev": "0" * 40 + "\n", "README.md": "words\n"})

    def test_an_unknown_path_is_reviewed(self):
        self.assert_tier_reviews({"notes.txt": "unknown\n"})

    #: A policy that lets Markdown anywhere skip review, so only the agent-instruction rule can keep a path reviewed.
    WIDE_DOCS_SKIP = '{"docs_skip": ["**/*.md"]}'

    def test_a_wide_docs_skip_still_skips_nested_prose(self):
        calls = self.tiered_branch({"guide/usage.md": "words\n"}, policy=self.WIDE_DOCS_SKIP)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.prepare()["review_skipped"], "docs-only")
        self.assertFalse(calls.exists())

    def test_a_root_agents_md_is_reviewed(self):
        self.assert_tier_reviews({"AGENTS.md": "rules\n"})

    def test_a_root_claude_md_is_reviewed(self):
        self.assert_tier_reviews({"CLAUDE.md": "rules\n"})

    def test_a_claude_rule_is_reviewed(self):
        self.assert_tier_reviews({".claude/rules/example.md": "rule\n"}, self.WIDE_DOCS_SKIP)

    def test_a_claude_command_is_reviewed(self):
        self.assert_tier_reviews({".claude/commands/example.md": "command\n"}, self.WIDE_DOCS_SKIP)

    def test_a_nested_agents_md_is_reviewed(self):
        self.assert_tier_reviews({"sub/AGENTS.md": "rules\n"}, self.WIDE_DOCS_SKIP)

    def test_prose_under_an_unknown_dot_directory_is_reviewed(self):
        self.assert_tier_reviews({".newagent/notes.md": "notes\n"}, self.WIDE_DOCS_SKIP)

    def test_every_agent_instruction_shape_steers_agents(self):
        for path in ("CLAUDE.local.md", "docs/GEMINI.md", "docs/claude.md", ".github/copilot-instructions.md",
                     "docs/rules.mdc", "docs/api.instructions.md", ".cursorrules", "docs/.agents/x.md",
                     "skills/sd-x/SKILL.md", "hooks/notes.md"):
            with self.subTest(path=path):
                self.assertTrue(ship.steers_agents(path))
        for path in ("README.md", "docs/guide.md", "guide/usage.md", "docs/instructions.md"):
            with self.subTest(path=path):
                self.assertFalse(ship.steers_agents(path))

    def test_status_contexts_are_bound_by_exact_sha_endpoint_without_an_invented_sha_field(self):
        self.prepare()
        self.remote.protection["required_status_checks"]["checks"] = []
        self.remote.pull(1).checks = []
        self.double.statuses = [{"context": "check", "state": "pending"}, {"context": "check", "state": "success"}]
        with self.assertRaisesRegex(ship.Refusal, "required status"):
            self.merge()
        self.double.statuses = [{"context": "check", "state": "success"}]
        self.assertEqual(self.merge()["phase"], "merged")

    def test_incomplete_original_depth_cannot_be_repaired_by_only_reviewing_a_fix(self):
        self.prepare()
        operation = self.operation()
        operation.state["passes"][0]["report"]["completed_reviews"] = 0
        operation.state["reviewed_head"] = None
        operation.save()
        _git(self.root, "commit", "--allow-empty", "-m", "new fix\n\nAuthored-with: human")
        with self.assertRaisesRegex(ship.Refusal, "preceding review did not complete"):
            self.prepare()

    def test_explicit_retry_preserves_incomplete_pass_and_reviews_the_full_branch(self):
        provider = self.programs / "review-fixture"
        working = provider.read_text()
        provider.write_text("#!/usr/bin/env python3\nprint('not a review')\n")
        with self.assertRaises(ship.Refusal):
            self.prepare()
        first = self.operation().state["passes"][0]
        self.assertEqual(first["report"]["completed_reviews"], 0)
        self.assertEqual(len(self.remote.pull_requests), 0)
        _git(self.root, "commit", "--allow-empty", "-m", "recover parser\n\nAuthored-with: human")
        provider.write_text(working)
        self.prepare("--retry-review")
        state = self.operation().state
        self.assertEqual(state["passes"][0], first)
        self.assertEqual(len(state["passes"]), 2)
        report = state["passes"][1]["report"]
        self.assertIn("src.py", report["subject"]["paths"])
        self.assertEqual(report["subject"]["base"], report["authorship_base"])
        # The aggregate, not the first pass's report on its own. That pass
        # completed no review, so it superseded nothing and the retry resumes
        # everything before it. The two carry the same findings here; what
        # changed is which object is the evidence's identity.
        self.assertEqual(report["resume_report_digest"],
                         ship.digest(ship.review_history([first])))
        self.assertEqual(self.merge()["phase"], "merged")

    def test_a_missing_receipt_keeps_the_reviewer_process_output(self):
        """A review that prints no receipt leaves its exit code and output tails.

        #590's MiniMax pass failed with only "no valid receipt": sd-review's
        stderr and exit status were dropped, so nothing said whether the
        provider or sd-review itself failed.
        """
        operation = self.operation()
        head = _git(self.root, "rev-parse", "HEAD")
        original = ship.review_process
        def crashed(root, argv, **kwargs):
            if "--explain" in argv:
                return original(root, argv, **kwargs)
            return subprocess.CompletedProcess([], 1, "partial", "Traceback: boom\n")
        with patch.object(ship, "review_process", side_effect=crashed):
            with self.assertRaisesRegex(ship.Refusal, "no valid receipt.*exit 1"):
                operation.review(head)
        error = self.operation().state["passes"][0]["execution_error"]
        self.assertEqual(error["kind"], "invalid_receipt")
        self.assertEqual(error["stage"], "execution")
        self.assertEqual(error["exit_code"], 1)
        self.assertIn("Traceback: boom", error["stderr"]["tail"])
        self.assertEqual(error["stdout"]["tail"], "partial")

    def test_a_missing_receipt_writes_the_whole_output_when_capture_is_on(self):
        operation = self.operation()
        head = _git(self.root, "rev-parse", "HEAD")
        original = ship.review_process
        def crashed(root, argv, **kwargs):
            if "--explain" in argv:
                return original(root, argv, **kwargs)
            return subprocess.CompletedProcess([], 1, "partial", "Traceback: boom\n")
        capture = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory())) / "raw"
        with patch.dict(os.environ, {"SD_REVIEW_RAW_DIR": str(capture)}), \
                patch.object(ship, "review_process", side_effect=crashed):
            with self.assertRaisesRegex(ship.Refusal, "no valid receipt"):
                operation.review(head)
        error = self.operation().state["passes"][0]["execution_error"]
        # A truncated receipt may carry raw model text: only the private file keeps it.
        self.assertEqual(error["stdout"], {"withheld": "raw capture is on; stdout may carry model output",
                                           "bytes": len("partial")})
        path = pathlib.Path(error["raw_capture"])
        self.assertEqual(path.parent, capture)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        record = json.loads(path.read_text())
        self.assertEqual((record["head"], record["exit_code"], record["stdout"], record["stderr"]),
                         (head, 1, "partial", "Traceback: boom\n"))

    def test_a_watchdog_kill_keeps_raw_model_text_out_of_the_ship_state(self):
        """The timeout path bypasses the receipt check; it must withhold raw text too."""
        operation = self.operation()
        head = _git(self.root, "rev-parse", "HEAD")
        original = ship.review_process
        report = {"status": "clean", "outcomes": [{"backend": "minimax", "diagnostic": {
            "kind": "invalid_response", "raw_response": {"body": "model text", "stderr": ""}}}]}
        def killed(root, argv, **kwargs):
            if "--explain" in argv:
                return original(root, argv, **kwargs)
            raise ship.ReviewTimeout({"kind": "watchdog_expired", "allowed_seconds": kwargs["timeout"],
                                      "stdout": {"bytes": 10, "tail": "model text", "truncated": False},
                                      "captured_report": report})
        capture = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory())) / "raw"
        with patch.dict(os.environ, {"SD_REVIEW_RAW_DIR": str(capture)}), \
                patch.object(ship, "review_process", side_effect=killed):
            with self.assertRaisesRegex(ship.Refusal, "execution watchdog expired"):
                operation.review(head)
        error = self.operation().state["passes"][0]["execution_error"]
        self.assertNotIn("model text", json.dumps(error))
        self.assertEqual(error["stdout"]["bytes"], 10)
        path = pathlib.Path(error["captured_report"]["outcomes"][0]["diagnostic"]["raw_capture"])
        self.assertEqual(json.loads(path.read_text())["body"], "model text")

    def test_a_watchdog_kill_writes_its_whole_output_when_capture_is_on(self):
        """sd:1588. A truncated or oversized output leaves no captured report,
        so the killed process's own bytes are the only evidence left."""
        operation = self.operation()
        head = _git(self.root, "rev-parse", "HEAD")
        original = ship.review_process
        def killed(root, argv, **kwargs):
            if "--explain" in argv:
                return original(root, argv, **kwargs)
            raise ship.ReviewTimeout({"kind": "watchdog_expired", "allowed_seconds": kwargs["timeout"],
                                      "stdout": {"bytes": 13, "tail": "partial model", "truncated": False},
                                      "raw_output": {"stdout": "partial model", "stderr": "warn"}})
        capture = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory())) / "raw"
        with patch.dict(os.environ, {"SD_REVIEW_RAW_DIR": str(capture)}), \
                patch.object(ship, "review_process", side_effect=killed):
            with self.assertRaisesRegex(ship.Refusal, "execution watchdog expired"):
                operation.review(head)
        error = self.operation().state["passes"][0]["execution_error"]
        self.assertNotIn("partial model", json.dumps(error))
        self.assertNotIn("raw_output", error)
        written = json.loads(pathlib.Path(error["raw_capture"]).read_text())
        self.assertEqual((written["stdout"], written["stderr"], written["head"]), ("partial model", "warn", head))

    def test_a_planning_watchdog_kill_writes_its_output_to_the_file_not_the_receipt(self):
        """#1201 review. The `--explain` stage times out before execution; its
        diagnostic is saved as `review_preflight_error` and must be withheld too."""
        operation = self.operation()
        head = _git(self.root, "rev-parse", "HEAD")
        def killed(root, argv, **kwargs):
            raise ship.ReviewTimeout({"kind": "watchdog_expired", "allowed_seconds": kwargs["timeout"],
                                      "stdout": {"bytes": 12, "tail": "planning out", "truncated": False},
                                      "raw_output": {"stdout": "planning out", "stderr": ""}})
        capture = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory())) / "raw"
        with patch.dict(os.environ, {"SD_REVIEW_RAW_DIR": str(capture)}), \
                patch.object(ship, "review_process", side_effect=killed):
            with self.assertRaisesRegex(ship.Refusal, "planning watchdog expired"):
                operation.review(head)
        error = self.operation().state["review_preflight_error"]
        self.assertNotIn("planning out", json.dumps(error))
        self.assertNotIn("raw_output", error)
        self.assertEqual(json.loads(pathlib.Path(error["raw_capture"]).read_text())["stdout"], "planning out")

    def test_review_process_hands_over_its_whole_output_only_under_capture(self):
        # The child says when its output is written, and the watchdog starts
        # counting only then: a short timeout that cannot expire before the
        # output exists, rather than five seconds of waiting per case (sd:1605).
        ready = self.directory / "written"
        script = ("import pathlib, sys, time\nprint('x' * 10000, flush=True)\nprint('e', file=sys.stderr, flush=True)\n"
                  f"pathlib.Path({str(ready)!r}).touch()\ntime.sleep(30)\n")
        original_communicate = subprocess.Popen.communicate
        def after_written(process, *args, **kwargs):
            deadline = time.monotonic() + CLI_TIMEOUT
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(ready.exists(), "the child never wrote its output")
            return original_communicate(process, *args, **kwargs)
        for capture, expected in (("", None), ("/nonexistent-raw-dir", "x" * 10000 + "\n")):
            ready.unlink(missing_ok=True)
            with self.subTest(capture=bool(capture)), patch.dict(os.environ, {"SD_REVIEW_RAW_DIR": capture}), \
                    patch.object(ship, "REVIEW_CLEANUP_SECONDS", .5), \
                    patch.object(subprocess.Popen, "communicate", after_written):
                with self.assertRaises(ship.ReviewTimeout) as raised:
                    ship.review_process(self.root, [sys.executable, "-c", script], timeout=.2)
                raw = raised.exception.diagnostic.get("raw_output")
                self.assertEqual(raw and raw["stdout"], expected)

    def test_missing_receipt_retry_is_explicit_and_never_rolls_back_spent_pass(self):
        operation = self.operation()
        head = _git(self.root, "rev-parse", "HEAD")
        original = ship.review_process
        def malformed(root, argv, **kwargs):
            return original(root, argv, **kwargs) if "--explain" in argv else subprocess.CompletedProcess([], 1, "not JSON", "")
        with patch.object(ship, "review_process", side_effect=malformed):
            with self.assertRaisesRegex(ship.Refusal, "no valid receipt"):
                operation.review(head)
        first = self.operation().state["passes"][0]
        with self.assertRaisesRegex(ship.Refusal, "retry-review") as raised:
            self.prepare()
        # The lane spends its one automatic retry on this code (sd:3037).
        self.assertEqual(raised.exception.workflow["blocker"]["code"], "review_incomplete")
        self.prepare("--retry-review")
        state = self.operation().state
        self.assertEqual(state["passes"][0], first)
        self.assertEqual(len(state["passes"]), 2)

    def test_zero_exit_with_unusable_json_receipt_can_retry(self):
        operation = self.operation()
        head = _git(self.root, "rev-parse", "HEAD")
        original = ship.review_process
        def malformed(root, argv, **kwargs):
            return original(root, argv, **kwargs) if "--explain" in argv else subprocess.CompletedProcess([], 0, "{}", "")
        with patch.object(ship, "review_process", side_effect=malformed):
            with self.assertRaises(ship.Refusal):
                operation.review(head)
        first = self.operation().state["passes"][0]
        self.prepare("--retry-review")
        self.assertEqual(self.operation().state["passes"][0], first)
        self.assertEqual(len(self.operation().state["passes"]), 2)

    def test_retry_cannot_spend_a_pass_past_the_cap(self):
        provider = self.programs / "review-fixture"
        provider.write_text("#!/usr/bin/env python3\nprint('not a review')\n")
        with self.assertRaises(ship.Refusal):
            self.prepare()
        for _ in range(CAP - 1):
            with self.assertRaises(ship.Refusal):
                self.prepare("--retry-review")
        with self.assertRaisesRegex(ship.Refusal, "spent"):
            self.prepare("--retry-review")
        self.assertEqual(len(self.operation().state["passes"]), CAP)
        self.assertEqual(len(self.remote.pull_requests), 0)

    def test_retry_flag_cannot_replace_a_complete_review(self):
        self.prepare()
        _git(self.root, "commit", "--allow-empty", "-m", "fix\n\nAuthored-with: human")
        with self.assertRaisesRegex(ship.Refusal, "only an incomplete"):
            self.prepare("--retry-review")

    def test_a_refused_review_whose_reviewers_all_answered_is_retried_not_wedged(self):
        """sd:3109. sd-review refuses a review whose inputs changed during its gate.

        Every reviewer answered, so the counts read complete, but sd-review
        verifies no refused prior. After a new commit each prepare dispatched a
        fix verification that failed planning, and --retry-review refused too.
        """
        original = ship.review_process
        def refused(root, argv, **kwargs):
            result = original(root, argv, **kwargs)
            if "--explain" in argv:
                return result
            report = dict(json.loads(result.stdout), status="refused")
            return subprocess.CompletedProcess(result.args, 3, json.dumps(report), result.stderr)
        with patch.object(ship, "review_process", side_effect=refused):
            with self.assertRaisesRegex(ship.Refusal, "local review refused"):
                self.prepare()
        first = self.operation().state["passes"][0]
        self.assertEqual((first["report"]["findings"], first["report"]["completed_reviews"]),
                         ([], first["report"]["requested_reviews"]))
        _git(self.root, "commit", "--allow-empty", "-m", "fix\n\nAuthored-with: human")
        with self.assertRaisesRegex(ship.Refusal, "use --retry-review"):
            self.prepare()
        self.prepare("--retry-review")
        passes = self.operation().state["passes"]
        self.assertEqual(passes[0], first)
        self.assertEqual((len(passes), passes[1]["retry"], passes[1]["report"]["status"]), (2, True, "clean"))

    def spent_reviews(self, blockers=False):
        provider = self.programs / "review-fixture"
        working = provider.read_text()
        for index in range(CAP):
            if blockers:
                payload = {"type": "result", "subtype": "success", "structured_output": {"findings": [
                    {"path": "src.py", "line": 1, "severity": "high", "family": "correctness",
                     "summary": f"unresolved from pass {index + 1}"}]}}
                provider.write_text("#!/usr/bin/env python3\nprint(" + repr(json.dumps(payload)) + ")\n")
                if index:
                    _git(self.root, "commit", "--allow-empty", "-m", "fix attempt\n\nAuthored-with: human")
            else:
                provider.write_text("#!/usr/bin/env python3\nprint('not JSON')\n")
            with self.assertRaises(ship.Refusal):
                self.prepare(*(["--retry-review"] if index and not blockers else []))
        return provider, working, json.loads(json.dumps(self.operation().state["passes"]))

    def additional(self, head=None, reason="Operator requests one bounded fixture review", history_digest=None):
        extra = ["--review-history-digest", history_digest] if history_digest is not None else []
        return self.prepare("--additional-review-for", head or _git(self.root, "rev-parse", "HEAD"),
                            "--request-reason", reason, *extra)

    def test_additional_review_preserves_all_blockers_and_full_branch_coverage(self):
        # The request names the head the last pass reviewed: there is no fix
        # to verify, so it reviews the whole branch again (sd:2147).
        provider, _, prior = self.spent_reviews(blockers=True)
        captured = self.directory / "third-prompt.txt"
        provider.write_text("#!/usr/bin/env python3\nimport pathlib,sys\n"
                           "work=pathlib.Path(sys.argv[sys.argv.index('--add-dir')+1])\n"
                           f"pathlib.Path({str(captured)!r}).write_text((work/'review-subject.md').read_text())\n"
                           "print('{\"type\":\"result\",\"subtype\":\"success\",\"structured_output\":{\"findings\":[]}}')\n")
        self.additional()
        state = self.operation().state
        self.assertEqual(state["passes"][:CAP], prior)
        self.assertEqual(len(state["passes"]), CAP + 1)
        request = state["passes"][CAP]["additional_review_request"]
        self.assertEqual(request["prior_history_digest"], ship.digest(prior))
        self.assertEqual(request["head"], _git(self.root, "rev-parse", "HEAD"))
        report = state["passes"][CAP]["report"]
        self.assertIn("src.py", report["subject"]["paths"])
        self.assertEqual(report["subject"]["base"], report["authorship_base"])
        self.assertEqual(report["resume_report_digest"], ship.digest(ship.review_history(prior)))
        prompt = captured.read_text()
        for index, old in enumerate(prior):
            self.assertIn(f"unresolved from pass {index + 1}", prompt)
            self.assertIn(ship.digest(old["report"]), prompt)
            self.assertIn(old["head"], prompt)
        self.assertIn("value = 1", prompt)
        self.assertEqual(self.merge()["phase"], "merged")

    def test_the_pass_after_the_cap_refuses_and_an_explicit_request_still_admits_it(self):
        """The boundary itself, from both sides.

        The automatic allowance is the table's cap and no more: the pass after
        it is refused before any dispatch. The explicit-request mechanism is
        untouched by the raise and still opens exactly one further pass.
        """
        provider, working, prior = self.spent_reviews(blockers=True)
        self.assertEqual(len(prior), CAP)
        _git(self.root, "commit", "--allow-empty", "-m", "resolve findings\n\nAuthored-with: human")
        provider.write_text(working)
        head = _git(self.root, "rev-parse", "HEAD")
        operation = self.operation("prepare")
        with patch.object(ship, "review_process", side_effect=AssertionError("review dispatched")), \
                patch.object(ship, "binding", side_effect=AssertionError("review binding read before the cap refusal")):
            with self.assertRaisesRegex(ship.Refusal, "spent"):
                operation.review(head)
        self.assertEqual(self.operation().state["passes"], prior)
        self.additional()
        state = self.operation().state
        self.assertEqual(state["passes"][:CAP], prior)
        self.assertEqual(len(state["passes"]), CAP + 1)
        self.assertEqual(state["passes"][CAP]["additional_review_request"]["prior_history_digest"],
                         ship.digest(prior))

    def test_additional_review_verifies_the_fix_when_the_whole_branch_exceeds_the_input_cap(self):
        """sd:2147. A post-cap request reviews what an automatic pass would at that head.

        Each fix delta fits the review input cap and the whole branch does
        not, as on ui-design #19 (2.1 MB over 85 files). The request used to
        review the whole branch and every reviewer refused the input.
        """
        self.prepare("--title", "fixture work")
        # Three commits of about 0.8 MB each: each fix verification fits, the branch does not.
        for index in range(1, CAP):
            if index < CAP - 1:
                (self.root / f"bulk{index}.txt").write_text("".join(f"bulk {index} line {n:07d}\n" for n in range(40_000)))
                _git(self.root, "add", f"bulk{index}.txt")
            _git(self.root, "commit", "--allow-empty", "-m", f"fix {index}\n\nAuthored-with: human")
            self.prepare("--title", "fixture work")
        prior = json.loads(json.dumps(self.operation().state["passes"]))
        self.assertEqual(len(prior), CAP)
        self.assertTrue(all(ship.completed_depth(entry["report"]) for entry in prior))
        whole = _git(self.root, "diff", prior[0]["report"]["subject"]["base"], "HEAD")
        self.assertGreater(len(whole.encode()), 2_000_000)
        _git(self.root, "commit", "--allow-empty", "-m", "fix after the cap\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        self.additional()
        state = self.operation().state
        self.assertEqual(state["passes"][:CAP], prior)
        last = state["passes"][CAP]
        self.assertEqual(last["base"], prior[-1]["head"])
        self.assertEqual(last["report"]["subject"]["base"], prior[-1]["head"])
        self.assertEqual(last["report"]["subject"]["head"], head)
        self.assertEqual(last["report"]["verification_report_digest"], ship.digest(prior[-1]["report"]))
        self.assertTrue(ship.completed_depth(last["report"]))
        self.assertEqual(state["reviewed_head"], head)
        self.operation().check_review(head)
        # A stored base cannot be cleared to pass the verification off as a whole-branch review.
        operation = self.operation()
        operation.state["passes"][CAP]["base"] = None
        operation.save()
        with self.assertRaisesRegex(ship.Refusal, "full-branch coverage does not match"):
            self.operation().check_review(head)

    def test_additional_review_after_a_spent_reservation_verifies_from_the_last_completed_pass(self):
        """sd:2192. A spent request that reviewed nothing is not the base a later request verifies from.

        ui-design #19: pass 5 completed at A, pass 6 was a request at B that
        every reviewer refused and that stayed spent. The renewed request at B
        reviewed the whole branch (2.1 MB) instead of A..B, and was refused.
        """
        self.prepare("--title", "fixture work")
        for index in range(1, CAP):
            if index < CAP - 1:
                (self.root / f"bulk{index}.txt").write_text("".join(f"bulk {index} line {n:07d}\n" for n in range(40_000)))
                _git(self.root, "add", f"bulk{index}.txt")
            _git(self.root, "commit", "--allow-empty", "-m", f"fix {index}\n\nAuthored-with: human")
            self.prepare("--title", "fixture work")
        prior = json.loads(json.dumps(self.operation().state["passes"]))
        self.assertTrue(all(ship.completed_depth(entry["report"]) for entry in prior))
        _git(self.root, "commit", "--allow-empty", "-m", "fix after the cap\n\nAuthored-with: human")
        head = _git(self.root, "rev-parse", "HEAD")
        operation = self.operation()
        request = {"head": head, "reason": "refused before sd:2147 released it", "recorded_at": "2026-09-30T00:00:00Z",
                   "allowed_passes": 1, "prior_history_digest": ship.digest(prior),
                   "operator_context": "untrusted assertion, not proof of user approval"}
        operation.state["passes"].append({
            "head": head, "started_at": "2026-09-30T00:00:00Z", "base": None, "retry": False, "requested_provider": None,
            "additional_review_request": request, "exit_code": 3,
            "report": {"status": "refused", "requested_reviews": 1, "completed_reviews": 0, "reviewed_by": [],
                       "findings": [], "outcomes": [], "scope": "branch", "subject": {"head": head}, "authored_with": []}})
        operation.save()
        passes = json.loads(json.dumps(self.operation().state["passes"]))
        self.assertTrue(importlib.import_module("sd_ship_history").reservation(passes[-1]))
        self.additional(history_digest=ship.digest(passes))
        state = self.operation().state
        self.assertEqual(state["passes"][:CAP + 1], passes)
        last = state["passes"][CAP + 1]
        self.assertEqual(last["base"], prior[-1]["head"])
        self.assertEqual(last["report"]["subject"]["base"], prior[-1]["head"])
        self.assertEqual(last["report"]["subject"]["head"], head)
        self.assertEqual(last["report"]["verification_report_digest"], ship.digest(prior[-1]["report"]))
        self.assertTrue(ship.completed_depth(last["report"]))
        self.operation().check_review(head)
        # A reservation that kept a finding still has to be resumed by a whole-branch pass.
        history = importlib.import_module("sd_ship_history")
        kept = json.loads(json.dumps(passes))
        kept[-1]["report"]["findings"] = [{"path": "src.py", "summary": "kept"}]
        self.assertEqual(history.verified_index(passes), CAP - 1)
        self.assertIsNone(history.verified_index(kept))

    def test_additional_review_under_a_moved_binding_records_the_move(self):
        """sd:2192. A request dispatched under a moved binding carries the marker.

        Dispatch stores the new binding before the pass runs. Without the
        marker, a request that then failed would read as an ordinary
        reservation, and the next request would verify past it against a
        report written under the old policy.
        """
        provider, working, prior = self.spent_reviews()
        provider.write_text(working)
        operation = self.operation()
        operation.state["binding"] = "a review tool file landed on the default branch"
        operation.save()
        self.additional()
        state = self.operation().state
        self.assertEqual(state["passes"][:CAP], prior)
        last = state["passes"][CAP]
        self.assertIsNone(last["base"])
        self.assertEqual(last["review_binding_change"]["superseded_binding"],
                         "a review tool file landed on the default branch")
        self.assertEqual(state["binding"], ship.binding(self.root))
        self.operation().check_review(last["head"])

    def test_additional_pass_that_reviewed_nothing_does_not_consume_the_request(self):
        """sd:2147. Every reviewer failed and none left a finding: the request stays unspent."""
        provider, working, prior = self.spent_reviews()
        head = _git(self.root, "rev-parse", "HEAD")
        # The fixture registry holds two reviewers, so the pass asks both (sd:7519).
        with self.assertRaisesRegex(ship.Refusal, "0/2 completed.*nothing was reviewed, so the operator request was not consumed"):
            self.additional()
        state = self.operation().state
        self.assertEqual(state["passes"], prior)
        self.assertEqual(state["review_preflight_error"]["kind"], "additional_review_unreviewed")
        self.assertEqual(state["review_preflight_error"]["head"], head)
        # The same request, unrenewed, runs once the reviewer answers.
        provider.write_text(working)
        self.additional()
        state = self.operation().state
        self.assertEqual(state["passes"][:CAP], prior)
        self.assertEqual(len(state["passes"]), CAP + 1)
        self.assertEqual(state["passes"][CAP]["additional_review_request"]["prior_history_digest"], ship.digest(prior))
        self.assertTrue(ship.completed_depth(state["passes"][CAP]["report"]))

    def test_additional_failed_reservation_remains_spent_and_cannot_be_reused(self):
        provider, _, prior = self.spent_reviews()
        provider.write_text(FAILED_WITH_BLOCKER)
        with self.assertRaisesRegex(ship.Refusal, "the operator request was consumed"):
            self.additional()
        saved = self.operation().state["passes"]
        self.assertEqual(saved[:CAP], prior)
        self.assertEqual(len(saved), CAP + 1)
        for flags in ([], ["--retry-review"], ["--additional-review-for", _git(self.root, "rev-parse", "HEAD"),
                                              "--request-reason", "A changed reason must not reset the request"]):
            with self.assertRaises(ship.Refusal):
                self.prepare(*flags)
            self.assertEqual(self.operation().state["passes"], saved)
        self.assertFalse(self.remote.pull_requests)

    def test_additional_request_validates_head_reason_and_existing_cap_before_dispatch(self):
        head = _git(self.root, "rev-parse", "HEAD")
        with self.assertRaises(ship.Refusal):
            self.additional(head)
        self.spent_reviews()
        prior = self.operation().state["passes"]
        # Each refusal names the flag that fixes it (sd:2026): a nonempty
        # `--request-reason` alone used to be told its reason was empty.
        for flags, names in ((["--additional-review-for", head], "needs a nonempty --request-reason"),
                             (["--request-reason", "reason"], "valid only with --additional-review-for"),
                             (["--additional-review-for", head, "--request-reason", "  "], "needs a nonempty --request-reason"),
                             (["--additional-review-for", head[:12], "--request-reason", "reason"], "is not the clean current HEAD"),
                             (["--additional-review-for", "0" * 40, "--request-reason", "reason"], "is not the clean current HEAD"),
                             (["--additional-review-for", head, "--request-reason", "reason", "--retry-review"],
                              "--retry-review and --additional-review-for do not combine")):
            operation = self.operation("prepare", *flags)
            with self.subTest(flags=flags), patch.object(ship, "review_process", side_effect=AssertionError("review dispatched")):
                with self.assertRaisesRegex(ship.Refusal, re.escape(names)):
                    operation.review(head)
            self.assertEqual(self.operation().state["passes"], prior)

    def test_additional_request_refuses_dirty_or_commit_stage_before_mutation(self):
        self.spent_reviews()
        head = _git(self.root, "rev-parse", "HEAD")
        (self.root / "src.py").write_text("uncommitted = True\n")
        for extra in ([], ["--path", "src.py", "--message-file", str(self.directory / "missing-message")]):
            with self.assertRaises(ship.Refusal):
                self.prepare("--additional-review-for", head, "--request-reason", "reason", *extra)
            self.assertEqual(_git(self.root, "rev-parse", "HEAD"), head)
            self.assertEqual(_git(self.root, "diff", "--cached", "--name-only"), "")
        self.assertEqual(len(self.operation().state["passes"]), CAP)

    def test_additional_receipt_revalidates_history_coverage_and_request_at_merge(self):
        provider, working, _ = self.spent_reviews()
        provider.write_text(working)
        self.additional()
        operation = self.operation()
        original = json.loads(json.dumps(operation.state))
        for change in ("missing-request", "history", "reason", "head", "coverage", "subject", "evidence"):
            operation.state = json.loads(json.dumps(original))
            last = operation.state["passes"][-1]
            if change == "missing-request":
                last.pop("additional_review_request")
            elif change == "history":
                operation.state["passes"][0]["report"]["findings"].append({"path": "src.py", "summary": "lost"})
            elif change in ("reason", "head"):
                last["additional_review_request"][change] = ""
            elif change == "coverage":
                last["report"]["completed_reviews"] = 0
            elif change == "subject":
                last["report"]["subject"]["base"] = last["head"]
            else:
                last["report"]["resume_report_digest"] = "invented"
            operation.save()
            with self.subTest(change=change), self.assertRaises(ship.Refusal):
                self.merge()
        operation.state = original
        operation.save()
        self.assertEqual(self.merge()["phase"], "merged")

    def test_additional_history_keeps_missing_reports(self):
        history = ship.review_history([
            {"head": "a" * 40, "report": {"findings": []}},
            {"head": "b" * 40}])
        self.assertNotIn("authored_with", history)
        self.assertEqual(history["history"][1]["report_digest"], None)
        self.assertEqual(history["subject"]["head"], "b" * 40)
        self.assertNotIn("completed_reviews", history)

    def test_additional_timeout_keeps_reportless_reservation_spent(self):
        _, _, prior = self.spent_reviews()
        head = _git(self.root, "rev-parse", "HEAD")
        operation = self.operation("prepare", "--additional-review-for", head, "--request-reason", "reason")
        original_run = ship.review_process

        def timed_out(root, argv, **kwargs):
            if "--explain" not in argv:
                raise ship.ReviewTimeout({"kind": "watchdog_expired", "allowed_seconds": kwargs["timeout"]})
            return original_run(root, argv, **kwargs)

        with patch.object(ship, "review_process", side_effect=timed_out), self.assertRaisesRegex(ship.Refusal, "watchdog expired"):
            operation.review(head)
        state = self.operation().state
        self.assertEqual(state["passes"][:CAP], prior)
        self.assertEqual(len(state["passes"]), CAP + 1)
        self.assertNotIn("report", state["passes"][-1])
        self.assertEqual(state["passes"][-1]["execution_error"]["stage"], "execution")
        self.assertEqual(state["passes"][-1]["exit_code"], 124)
        self.assertIsNone(state["reviewed_head"])
        with self.assertRaises(ship.Refusal):
            self.additional()
        self.assertEqual(self.operation().state["passes"], state["passes"])

    def spent_additional(self):
        # A failed answer that kept a blocker reviewed something, so it stays spent.
        provider, working, _ = self.spent_reviews()
        provider.write_text(FAILED_WITH_BLOCKER)
        with self.assertRaises(ship.Refusal):
            self.additional()
        return provider, working, json.loads(json.dumps(self.operation().state["passes"]))

    def test_renewed_reviews_past_the_cap_preserve_each_prefix_and_full_branch(self):
        # The first renewal follows an incomplete pass, so it reviews the whole
        # branch; the second follows that completed pass and verifies its fix (sd:2147).
        provider, working, prior = self.spent_additional()
        provider.write_text(working)
        for count in (CAP + 2, CAP + 3):
            _git(self.root, "commit", "--allow-empty", "-m", f"renewal {count}\n\nAuthored-with: human")
            head = _git(self.root, "rev-parse", "HEAD")
            with self.assertRaises(ship.Refusal):
                self.operation().check_review(head)
            self.additional(history_digest=ship.digest(prior))
            state = self.operation().state
            self.assertEqual(len(state["passes"]), count)
            self.assertEqual(state["passes"][:-1], prior)
            last = state["passes"][-1]
            self.assertEqual(last["additional_review_request"]["prior_history_digest"], ship.digest(prior))
            self.assertEqual(last["additional_review_request"]["allowed_passes"], 1)
            self.assertEqual(last["report"]["subject"]["head"], head)
            if count == CAP + 2:
                self.assertEqual(last["report"]["subject"]["base"], last["report"]["authorship_base"])
                self.assertEqual(last["report"]["resume_report_digest"], ship.digest(ship.review_history(prior)))
                self.assertIn("src.py", last["report"]["subject"]["paths"])
            else:
                self.assertEqual(last["report"]["subject"]["base"], prior[-1]["head"])
                self.assertEqual(last["report"]["verification_report_digest"], ship.digest(prior[-1]["report"]))
            prior = json.loads(json.dumps(state["passes"]))
        with self.assertRaisesRegex(ship.Refusal, "required CI is not passing"):
            self.merge()
        pull = self.remote.pull(1)
        pull.checks = [{**row, "head_sha": head} for row in pull.checks]
        self.assertEqual(self.merge()["phase"], "merged")

    def test_renewal_replayed_or_missing_digest_never_reserves_or_dispatches(self):
        provider, working, prior = self.spent_additional()
        consumed = ship.digest(prior)
        provider.write_text(working)
        self.additional(history_digest=consumed)
        head = _git(self.root, "rev-parse", "HEAD")
        prefix = ["--additional-review-for", head, "--request-reason", "another reason"]
        saved = self.operation().state
        revision = self.operation().revision
        for flags in (prefix, [*prefix, "--review-history-digest", consumed],
                      [*prefix, "--review-history-digest", "0" * 64],
                      [*prefix, "--review-history-digest", "not-hex"],
                      [*prefix, "--review-history-digest", "NaN"],
                      ["--review-history-digest", ship.digest(saved["passes"])],
                      [*prefix, "--review-history-digest", ship.digest(saved["passes"]), "--retry-review"]):
            operation = self.operation("prepare", *flags)
            with self.subTest(flags=flags), patch.object(ship, "review_process", side_effect=AssertionError("review dispatched")):
                with self.assertRaises(ship.Refusal):
                    operation.review(head)
            self.assertEqual(self.operation().state, saved)
            self.assertEqual(self.operation().revision, revision)
        operation = self.operation("prepare", *prefix, "--review-history-digest", consumed)
        with patch.object(operation.api, "api", side_effect=AssertionError("remote read attempted")), patch.object(ship, "run", side_effect=AssertionError("check started")):
            with self.assertRaisesRegex(ship.Refusal, ship.digest(saved["passes"])):
                operation.prepare()
        self.assertEqual(self.operation().state, saved)
        self.assertEqual(self.operation().revision, revision)

    def test_renewal_validates_every_additional_request_and_prior_evidence_before_reserving(self):
        self.spent_additional()
        operation = self.operation()
        original = json.loads(json.dumps(operation.state))
        head = _git(self.root, "rev-parse", "HEAD")
        for change in ("missing", "type", "reason", "head", "allowance", "digest", "earlier-history", "report-envelope"):
            state = json.loads(json.dumps(original))
            entry = state["passes"][CAP]
            request = entry["additional_review_request"]
            if change == "missing":
                entry.pop("additional_review_request")
            elif change == "type":
                entry["additional_review_request"] = []
            elif change in ("reason", "head"):
                request[change] = ""
            elif change == "allowance":
                request["allowed_passes"] = True
            elif change == "digest":
                request["prior_history_digest"] = "wrong"
            elif change == "earlier-history":
                state["passes"][0]["report"]["findings"].append({"summary": "changed evidence"})
            else:
                entry["report"]["findings"] = {}
            operation.state = state
            operation.save()
            revision = operation.revision
            candidate = self.operation("prepare", "--additional-review-for", head, "--request-reason", "renew",
                                       "--review-history-digest", ship.digest(state["passes"]))
            with self.subTest(change=change), patch.object(ship, "review_process", side_effect=AssertionError("review dispatched")):
                with self.assertRaises(ship.Refusal):
                    candidate.review(head)
            self.assertEqual(self.operation().state, state)
            self.assertEqual(self.operation().revision, revision)

    def test_renewal_after_reportless_additional_pass_requires_a_new_complete_report(self):
        provider, working, _ = self.spent_reviews()
        head = _git(self.root, "rev-parse", "HEAD")
        operation = self.operation("prepare", "--additional-review-for", head, "--request-reason", "initial additional")
        original_run = ship.review_process

        def reportless(root, argv, **kwargs):
            if "--explain" not in argv:
                return subprocess.CompletedProcess(argv, 1, "not a receipt", "")
            return original_run(root, argv, **kwargs)

        with patch.object(ship, "review_process", side_effect=reportless), self.assertRaises(ship.Refusal):
            operation.review(head)
        prior = self.operation().state["passes"]
        self.assertNotIn("report", prior[-1])
        with self.assertRaises(ship.Refusal):
            self.operation().check_review(head)
        # A failed report need not retroactively acquire successful coverage fields.
        def failed(root, argv, **kwargs):
            if "--explain" not in argv:
                return subprocess.CompletedProcess(argv, 1, '{"status":"gate_failed"}', "")
            return original_run(root, argv, **kwargs)

        with patch.object(ship, "review_process", side_effect=failed), self.assertRaises(ship.Refusal):
            self.additional(history_digest=ship.digest(prior))
        saved = self.operation().state["passes"]
        self.assertEqual(saved[:-1], prior)
        self.assertEqual(saved[-1]["report"], {"status": "gate_failed"})
        prior = saved
        with self.assertRaises(ship.Refusal):
            self.operation().check_review(head)
        provider.write_text(working)
        self.additional(history_digest=ship.digest(prior))
        state = self.operation().state
        self.assertEqual(state["passes"][:-1], prior)
        self.assertEqual(state["passes"][-1]["report"]["resume_report_digest"], ship.digest(ship.review_history(prior)))
        self.assertTrue(ship.completed_depth(state["passes"][-1]["report"]))
        self.assertEqual(self.merge()["phase"], "merged")

    def test_renewed_receipt_rechecks_all_prefixes_and_only_current_success_at_merge(self):
        provider, working, prior = self.spent_additional()
        provider.write_text(working)
        self.additional(history_digest=ship.digest(prior))
        operation = self.operation()
        original = json.loads(json.dumps(operation.state))
        for change in ("earlier-request", "earlier-history", "base", "resume", "depth", "head", "check", "blocker", "binding"):
            operation.state = json.loads(json.dumps(original))
            report = operation.state["passes"][-1]["report"]
            if change == "earlier-request":
                operation.state["passes"][CAP].pop("additional_review_request")
                operation.state["passes"][-1]["additional_review_request"]["prior_history_digest"] = ship.digest(operation.state["passes"][:-1])
                report["resume_report_digest"] = ship.digest(ship.review_history(operation.state["passes"][:-1]))
            elif change == "earlier-history":
                operation.state["passes"][0]["report"]["findings"].append({"summary": "changed"})
            elif change == "base":
                report["subject"]["base"] = report["subject"]["head"]
            elif change == "resume":
                report["resume_report_digest"] = "wrong"
            elif change == "depth":
                report["completed_reviews"] = 0
            elif change == "head":
                report["subject"]["head"] = "0" * 40
            elif change == "check":
                report["check"]["status"] = "fail"
            elif change == "blocker":
                report["findings"].append({"disposition": "blocking"})
            else:
                operation.state["binding"] = "wrong"
            operation.save()
            with self.subTest(change=change), self.assertRaises(ship.Refusal):
                self.merge()
        operation.state = original
        operation.save()
        self.assertEqual(self.merge()["phase"], "merged")

    def test_renewed_current_report_requires_valid_full_branch_bases(self):
        provider, working, prior = self.spent_additional()
        provider.write_text(working)
        self.additional(history_digest=ship.digest(prior))
        operation = self.operation()
        original = json.loads(json.dumps(operation.state))
        head = _git(self.root, "rev-parse", "HEAD")
        for base in (None, "", "not-a-sha", [], "a" * 39, "g" * 40):
            operation.state = json.loads(json.dumps(original))
            report = operation.state["passes"][-1]["report"]
            if base is None:
                report.pop("authorship_base")
                report["subject"].pop("base")
            else:
                report["authorship_base"] = report["subject"]["base"] = base
            operation.save()
            with self.subTest(base=base), self.assertRaises(ship.Refusal):
                self.operation().check_review(head)
        operation.state = original
        operation.save()
        self.operation().check_review(head)

    def test_renewal_digest_with_commit_flags_refuses_before_index_or_commit_changes(self):
        _, _, prior = self.spent_additional()
        head = _git(self.root, "rev-parse", "HEAD")
        (self.root / "src.py").write_text("uncommitted = True\n")
        message = self.directory / "message.txt"
        message.write_text("Should never commit")
        commit = ["--path", "src.py", "--message-file", str(message)]
        for extra in ([], ["--additional-review-for", head, "--request-reason", "renew"]):
            with self.subTest(extra=extra), patch.object(ship, "commit_paths", side_effect=AssertionError("commit attempted")):
                with self.assertRaises(ship.Refusal):
                    self.prepare("--review-history-digest", ship.digest(prior), *extra, *commit)
            self.assertEqual(_git(self.root, "rev-parse", "HEAD"), head)
            self.assertEqual(_git(self.root, "diff", "--cached", "--name-only"), "")
            self.assertEqual((self.root / "src.py").read_text(), "uncommitted = True\n")
            self.assertEqual(self.operation().state["passes"], prior)

    def test_renewal_digest_is_not_accepted_while_the_cap_is_only_just_spent(self):
        self.spent_reviews()
        prior = self.operation().state["passes"]
        head = _git(self.root, "rev-parse", "HEAD")
        operation = self.operation("prepare", "--additional-review-for", head, "--request-reason", "renew",
                                   "--review-history-digest", ship.digest(prior))
        with patch.object(ship, "review_process", side_effect=AssertionError("review dispatched")), self.assertRaises(ship.Refusal):
            operation.review(head)
        self.assertEqual(self.operation().state["passes"], prior)

    def test_additional_review_rejects_rewritten_ancestry_before_reserving(self):
        _, _, prior = self.spent_reviews()
        unrelated = _git(self.root, "commit-tree", _git(self.root, "rev-parse", "HEAD^{tree}"), "-m", "unrelated")
        _git(self.root, "update-ref", "refs/heads/topic", unrelated)
        with self.assertRaises(ship.Refusal):
            self.additional(unrelated)
        self.assertEqual(self.operation().state["passes"], prior)
        self.assertFalse(self.remote.pull_requests)


    def test_timing_plan_allows_valid_sequential_work_without_another_reservation(self):
        from tests.test_sd_review import sd_review
        trace = []
        stages = []
        def child(root, argv, *, timeout):
            args = sd_review.build_parser().parse_args(argv[2:])
            stages.append((args.explain, timeout))
            elapsed = 0
            def logical_runner(command, env, cwd, allowance):
                nonlocal elapsed
                check = any(str(a).endswith("/sd-check") for a in command)
                elapsed += 899 if check else 1799
                self.assertLess(899 if check else 1799, allowance)
                if elapsed > timeout:
                    raise ship.ReviewTimeout({"kind": "watchdog_expired", "allowed_seconds": timeout})
                trace.append("check" if check else "provider")
                payload = {} if check else {"type": "result", "subtype": "success", "structured_output": {"findings": []}}
                return sd_review.Completed(0, json.dumps(payload), "")
            report = sd_review.review(root, args, logical_runner, self.environment)
            return subprocess.CompletedProcess(argv, sd_review.STATUS_EXIT.get(report["status"], 0), json.dumps(report), "")
        with patch.object(ship, "review_process", side_effect=child):
            self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        state = self.operation().state
        # sd:2611: the execution watchdog also counts the gate-slot phase.
        self.assertEqual(stages, [(True, 3600), (False, 10800 + sd_review.sd_lib.GATE_SLOT_SECONDS)])
        # sd:2605: the reviewers run before the gate; sd:7519: two of them.
        self.assertEqual(trace, ["provider", "provider", "check"])
        self.assertEqual(len(state["passes"]), 1)
        self.assertEqual(state["passes"][0]["report"]["completed_reviews"], 2)

    def test_planning_timeout_saves_diagnostics_without_dispatch_or_reservation(self):
        diagnostic = {"kind": "watchdog_expired", "allowed_seconds": 3600,
                      "stdout": {"bytes": 8, "tail": "partial", "truncated": False},
                      "cleanup": {"term": "sent", "kill": "absent", "leader_reaped": True},
                      "captured_report": {"completed_reviews": 3, "status": "clean"}}
        with patch.object(ship, "review_process", side_effect=ship.ReviewTimeout(diagnostic)) as child:
            with self.assertRaisesRegex(ship.Refusal, "planning watchdog expired"):
                self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        state = self.operation().state
        self.assertEqual(child.call_count, 1)
        self.assertFalse(state.get("passes"))
        self.assertEqual(state["review_preflight_error"], dict(diagnostic, stage="planning"))
        self.assertIsNone(state.get("reviewed_head"))

    def test_invalid_timing_plan_never_starts_execution_or_reserves(self):
        with patch.object(ship, "review_process", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as child:
            with self.assertRaisesRegex(ship.Refusal, "no valid timing plan"):
                self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(child.call_count, 1)
        state = self.operation().state
        self.assertFalse(state.get("passes"))
        self.assertEqual(state["review_preflight_error"]["exit_code"], 0)
        self.assertEqual(state["review_preflight_error"]["stdout"]["tail"], "{}")

    def test_a_refused_plan_is_retryable_and_says_prepare_spent_no_pass(self):
        """sd:3478. Five lane entries failed on this refusal as a policy block; each passed when retried by hand."""
        with patch.object(ship, "review_process", return_value=subprocess.CompletedProcess([], 0, "{}", "")):
            with self.assertRaisesRegex(ship.Refusal, "no valid timing plan") as refused:
                self.prepare()
        answer = ship.failure("prepare", refused.exception)
        self.assertEqual((answer["workflow"]["blocker"]["retryable"], answer["passes_spent"]), (True, 0))

    def test_a_planning_watchdog_is_retryable_and_an_execution_watchdog_spent_its_pass(self):
        """sd:3478. Only the planning stage reserves no pass; the lane retries only a refusal that spent none."""
        original = ship.review_process
        for stage in ("planning", "execution"):
            with self.subTest(stage=stage):
                def killed(root, argv, stage=stage, **kwargs):
                    if stage == "execution" and "--explain" in argv:
                        return original(root, argv, **kwargs)
                    raise ship.ReviewTimeout({"kind": "watchdog_expired", "allowed_seconds": kwargs["timeout"]})
                with patch.object(ship, "review_process", side_effect=killed):
                    with self.assertRaisesRegex(ship.Refusal, f"{stage} watchdog expired") as refused:
                        self.prepare()
                answer = ship.failure("prepare", refused.exception)
                self.assertEqual((answer["workflow"]["blocker"]["retryable"], answer["passes_spent"]),
                                 (stage == "planning", int(stage == "execution")))

    def test_a_refused_plan_keeps_its_argv_and_plan_fields_past_the_tail(self):
        """sd:2646. The tail held 4 KiB of an explained report and cut off the fields the plan was refused on."""
        timing = {"candidates": [], "check_seconds": 3600, "execution_seconds": 21600, "phase_seconds": 1800,
                  "setup_seconds": 3600, "slot_seconds": 14400}
        explained = json.dumps({"timing": timing, "requested_reviews": 1,
                                "selection_refusal": "no provider '207'", "status": "explained", "padding": "x" * 10000})
        with patch.object(ship, "review_process",
                          side_effect=lambda root, argv, **kwargs: subprocess.CompletedProcess(argv, 0, explained, "")):
            with self.assertRaisesRegex(ship.Refusal, "no valid timing plan"):
                self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        diagnostic = self.operation().state["review_preflight_error"]
        self.assertNotIn("selection_refusal", diagnostic["stdout"]["tail"])
        self.assertEqual(diagnostic["plan"], {"timing": timing, "requested_reviews": 1,
                                              "selection_refusal": "no provider '207'"})
        self.assertEqual(diagnostic["argv"][-1], "--explain")
        self.assertIn("--scope", diagnostic["argv"])

    def assert_insufficient_reviewers_refuse(self, count):
        import sd_registry

        from tests.test_sd_review import sd_review
        registry = sd_registry.read_file(self.database.parent / "providers.yaml")
        consent = str(sd_registry.recipient(registry.providers["reviewer"])) if count else ""
        (self.root / "CLAUDE.local.md").write_text(
            "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\nmode: full\n"
            f"reviewers: {consent}\n<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n")
        stages = []
        def child(root, argv, *, timeout):
            args = sd_review.build_parser().parse_args(argv[2:])
            stages.append(args.explain)
            self.assertTrue(args.explain, "undersupplied planning must not start execution")
            def no_execution(*_args):
                raise AssertionError("planning dispatched a check or provider")
            report = sd_review.review(root, args, no_execution, self.environment)
            self.assertEqual(report["requested_reviews"], 1)
            self.assertEqual(len(report["timing"]["candidates"]), count)
            return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")
        operation = self.operation()
        with patch.object(ship, "review_process", side_effect=child):
            with self.assertRaisesRegex(ship.Refusal, "no valid timing plan"):
                operation.review(_git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(stages, [True])
        state = self.operation().state
        self.assertFalse(state.get("passes"))
        self.assertIsNone(state.get("reviewed_head"))
        self.assertEqual(state["review_preflight_error"]["kind"], "invalid_timing_plan")

    def test_no_eligible_reviewers_refuse_before_reservation_or_execution(self):
        self.assert_insufficient_reviewers_refuse(0)

    def test_one_eligible_reviewer_can_complete_the_local_pass(self):
        import sd_registry
        registry = sd_registry.read_file(self.database.parent / "providers.yaml")
        consent = str(sd_registry.recipient(registry.providers["reviewer"]))
        (self.root / "CLAUDE.local.md").write_text(
            "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\nmode: full\n"
            f"reviewers: {consent}\n<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n")
        result = self.prepare()
        self.assertEqual(result["phase"], "ready_to_send")
        report = self.operation().state["passes"][0]["report"]
        self.assertEqual(report["completed_reviews"], 1)

    def test_planning_failure_diagnostics_are_bounded_and_hash_original_streams(self):
        stdout, stderr = "x" * 10000, "y" * 10000 + "prior evidence exceeds limit"
        with patch.object(ship, "review_process", return_value=subprocess.CompletedProcess([], 2, stdout, stderr)):
            with self.assertRaises(ship.Refusal):
                self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        state = self.operation().state
        self.assertFalse(state.get("passes"))
        diagnostic = state["review_preflight_error"]
        self.assertEqual((diagnostic["kind"], diagnostic["stage"], diagnostic["exit_code"]), ("invalid_timing_plan", "planning", 2))
        for name, text in (("stdout", stdout), ("stderr", stderr)):
            self.assertEqual(diagnostic[name]["bytes"], len(text))
            self.assertEqual(diagnostic[name]["sha256"], ship.hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(diagnostic[name]["tail"], text[-4096:])
            self.assertTrue(diagnostic[name]["truncated"])

    def test_oversized_resume_preflight_preserves_history_and_next_success_clears_diagnostic(self):
        from tests.test_sd_review import sd_review
        _, _, prior = self.spent_reviews()
        operation = self.operation()
        prior[0]["report"]["findings"] = [{"path": "src.py", "line": 1, "summary": "x" * (sd_review.MAX_OUTPUT_BYTES - 10000),
                                             "disposition": "blocking", "severity": "high", "family": "correctness"}]
        operation.save(passes=prior, review_preflight_error={"stage": "planning", "kind": "old"})
        head = _git(self.root, "rev-parse", "HEAD")
        operation = self.operation("prepare", "--additional-review-for", head, "--request-reason", "fixture")
        before = json.loads(json.dumps(operation.state))
        stages = []
        reports = []
        def child(root, argv, *, timeout):
            args = sd_review.build_parser().parse_args(argv[2:])
            stages.append(args.explain)
            def no_execution(*_args):
                raise AssertionError("check or provider dispatched during invalid planning")
            try:
                with patch.object(sd_review, "local_conventions", return_value="fixture convention " + "y" * 20000):
                    report = sd_review.review(root, args, no_execution, self.environment)
            except sd_review.UsageError as error:
                return subprocess.CompletedProcess(argv, 2, "", str(error))
            reports.append(report)
            return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")
        with patch.object(ship, "review_process", side_effect=child), self.assertRaisesRegex(ship.Refusal, "input_oversized"):
            operation.review(head)
        self.assertEqual(stages, [True])
        failed = self.operation().state
        self.assertEqual(failed["passes"], before["passes"])
        self.assertEqual(failed["reviewed_head"], before["reviewed_head"])
        self.assertEqual(reports[0]["readiness"]["status"], "blocked")
        self.assertEqual(reports[0]["readiness"]["blockers"][0]["code"], "input_oversized")
        diagnostic = failed["review_preflight_error"]
        self.assertEqual(diagnostic["stdout"]["sha256"], ship.hashlib.sha256(json.dumps(reports[0]).encode()).hexdigest())
        self.assertEqual((diagnostic["exit_code"], diagnostic["stderr"]["tail"]), (0, ""))
        prior[0]["report"]["findings"][0]["summary"] = "bounded blocker"
        operation.save(passes=prior)
        valid = {"status": "explained", "requested_reviews": 1,
                 "readiness": {"status": "ready", "blockers": [], "warnings": [], "runtime_approval": "not_observable"},
                 "timing": {"phase_seconds": 1800, "setup_seconds": 3600,
                 "execution_seconds": 7200, "candidates": [{"name": "fixture", "recipient": "fixture@fixture"}]}}
        answers = [subprocess.CompletedProcess([], 0, json.dumps(valid), ""), subprocess.CompletedProcess([], 2, "", "failed")]
        with patch.object(ship, "review_process", side_effect=answers), self.assertRaisesRegex(ship.Refusal, "reserved pass remains recorded"):
            operation.review(head)
        saved = self.operation().state
        self.assertEqual(saved["passes"][:-1], prior)
        self.assertEqual(len(saved["passes"]), CAP + 1)
        self.assertIsNone(saved["review_preflight_error"])
        self.assertEqual(before["review_preflight_error"], {"stage": "planning", "kind": "old"})

    def captured_timeout_retry(self, extra_reviewer=False):
        from tests.test_sd_review import sd_review
        if extra_reviewer:
            registry = self.database.parent / "providers.yaml"
            provider = self.programs / "review-fixture"
            body = registry.read_text().replace("roles:\n  author:",
                f"  reviewer3: {{ start: '{provider}', vendor: fourthvendor, bill: fixture, roles: [reviewer], reader: claude-json }}\nroles:\n  author:")
            registry.write_text(body.replace("reviewer: [reviewer, reviewer2]", "reviewer: [reviewer, reviewer2, reviewer3]"))
            local = self.root / "CLAUDE.local.md"
            local.write_text(local.read_text().replace("reviewers: ", f"reviewers: reviewer3@{provider}, "))
        original = ship.review_process
        def expires(root, argv, **kwargs):
            if "--explain" in argv:
                return original(root, argv, **kwargs)
            args = sd_review.build_parser().parse_args(argv[2:])
            def canned(command, env, cwd, timeout):
                if any(str(a).endswith("/sd-check") for a in command):
                    return sd_review.Completed(0, "{}", "")
                payload = {"type": "result", "subtype": "success", "structured_output": {"findings": [
                    {"path": "src.py", "line": 1, "severity": "high", "family": "correctness", "summary": "captured timeout blocker"}]}}
                return sd_review.Completed(0, json.dumps(payload), "")
            report = sd_review.review(root, args, canned, self.environment)
            raise ship.ReviewTimeout({"kind": "watchdog_expired", "allowed_seconds": kwargs["timeout"], "captured_report": report})
        with patch.object(ship, "review_process", side_effect=expires), self.assertRaisesRegex(ship.Refusal, "watchdog expired"):
            self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        failed = self.operation().state["passes"][0]
        self.assertNotIn("report", failed)
        raw_capture = json.loads(json.dumps(failed["execution_error"]["captured_report"]))
        self.assertEqual(failed["exit_code"], 124)
        self.assertIsNone(self.operation().state["reviewed_head"])
        self.assertIn("captured timeout blocker", json.dumps(ship.review_history([failed])))
        # The reviewers the timeout used are rate limited for the retry, so it
        # falls to the next one, or refuses when there is none.
        registry = self.database.parent / "providers.yaml"
        registry.write_text(registry.read_text().replace(
            "vendor: secondvendor,", "enabled: false, reason: rate-limited, vendor: secondvendor,").replace(
            "vendor: thirdvendor,", "enabled: false, reason: rate-limited, vendor: thirdvendor,"))
        observed = []
        def resumes(root, argv, **kwargs):
            if "--resume-report" in argv:
                observed.append(json.loads(pathlib.Path(argv[argv.index("--resume-report") + 1]).read_text()))
            return original(root, argv, **kwargs)
        with patch.object(ship, "review_process", side_effect=resumes):
            if extra_reviewer:
                self.prepare("--retry-review")
            else:
                with self.assertRaises(ship.Refusal):
                    self.prepare("--retry-review")
        self.assertEqual(len(observed), 2 if extra_reviewer else 1)
        self.assertTrue(all(value == ship.review_history([failed]) for value in observed))
        self.assertTrue(all("completed_reviews" not in value for value in observed))
        self.assertEqual(failed["execution_error"]["captured_report"], raw_capture)
        state = self.operation().state
        self.assertEqual(state["passes"][0], failed)
        if not extra_reviewer:
            self.assertEqual(state["passes"], [failed])
            self.assertIsNone(state["reviewed_head"])
            self.assertEqual(state["review_preflight_error"]["kind"], "invalid_timing_plan")
            return
        latest = state["passes"][-1]["report"]
        self.assertEqual(latest["completed_reviews"], 1)
        self.assertEqual(latest["reviewed_by"], ["reviewer3"])

    def test_captured_timeout_blocker_survives_unavailable_retry_planning(self):
        self.captured_timeout_retry()

    def test_successful_captured_timeout_retry_reaches_verified_fixture_merge(self):
        self.captured_timeout_retry(extra_reviewer=True)
        self.assertEqual(self.merge()["phase"], "merged")

    def test_deep_timing_plan_refusal_does_not_reserve(self):
        raw = '{"timing":' + '[' * 10000 + '0' + ']' * 10000 + '}'
        with patch.object(ship, "review_process", return_value=subprocess.CompletedProcess([], 0, raw, "")):
            with self.assertRaises(ship.Refusal):
                self.operation().review(_git(self.root, "rev-parse", "HEAD"))
        state = self.operation().state
        self.assertFalse(state.get("passes"))
        self.assertIsNone(state.get("reviewed_head"))


class ReviewWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def test_normal_nonzero_output_remains_available_for_report_validation(self):
        result = ship.review_process(self.root, [sys.executable, "-c",
            "import sys; print('{\"status\":\"blocking\"}'); print('diagnostic',file=sys.stderr); sys.exit(1)"], timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"status": "blocking"})
        self.assertEqual(result.stderr, "diagnostic\n")

    def expired_group(self, *, ignores_term=False, valid_report=False, leader_exits=False):
        ready = self.root / "ready"
        grandchild = self.root / "grandchild"
        def stop_owned_fixture():
            if ready.exists():
                try:
                    os.killpg(int(ready.read_text()), ship.signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(stop_owned_fixture)
        script = self.root / "tree.py"
        script.write_text("import os,signal,time,pathlib,sys\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignores_term else "")
            + "child=os.fork()\nif child==0:\n"
            + " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            + f" pathlib.Path({str(grandchild)!r}).write_text(str(os.getpid()))\n"
            + " while True: time.sleep(1)\n"
            + f"while not pathlib.Path({str(grandchild)!r}).exists(): time.sleep(.005)\n"
            + ("print('{\"completed_reviews\":3,\"status\":\"clean\"}',flush=True)\n" if valid_report else "print('x'*10000,flush=True)\n")
            + "print('e'*10000,file=sys.stderr,flush=True)\n"
            + f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
            + ("sys.exit(0)\n" if leader_exits else "while True: time.sleep(1)\n"))
        original_communicate = subprocess.Popen.communicate
        waiting = False
        def after_ready(process, *args, **kwargs):
            nonlocal waiting
            if not waiting:
                waiting = True
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertTrue(ready.exists(), "fixture did not reach its explicit ready barrier")
            return original_communicate(process, *args, **kwargs)
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True) as unrelated:
            try:
                started = time.monotonic()
                with patch.object(subprocess.Popen, "communicate", after_ready), patch.object(ship, "REVIEW_CLEANUP_SECONDS", .05):
                    with self.assertRaises(ship.ReviewTimeout) as raised:
                        ship.review_process(self.root, [sys.executable, str(script)], timeout=.05)
                self.assertLess(time.monotonic() - started, 6)
                self.assertIsNone(unrelated.poll(), "the unrelated process must remain alive")
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)
        for identity in (ready, grandchild):
            pid = int(identity.read_text())
            observed = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], text=True, capture_output=True, timeout=5)
            self.assertIn(observed.returncode, (0, 1), observed.stderr)
            self.assertTrue(not observed.stdout.strip() or observed.stdout.strip().startswith("Z"), observed.stdout)
        return raised.exception.diagnostic

    def test_timeout_stops_descendant_after_leader_exits_and_bounds_output(self):
        diagnostic = self.expired_group()
        self.assertEqual(diagnostic["kind"], "watchdog_expired")
        self.assertEqual(diagnostic["cleanup"]["kill"], "sent")
        self.assertTrue(diagnostic["cleanup"]["leader_reaped"])
        for stream in ("stdout", "stderr"):
            self.assertTrue(diagnostic[stream]["truncated"])
            self.assertEqual(diagnostic[stream]["bytes"], 10001)
            self.assertLessEqual(len(diagnostic[stream]["tail"]), 4096)

    def test_ignored_term_is_killed_and_captured_json_is_only_evidence(self):
        diagnostic = self.expired_group(ignores_term=True, valid_report=True)
        self.assertEqual(diagnostic["cleanup"], {"term": "sent", "kill": "sent", "drained": True, "leader_reaped": True})
        self.assertEqual(diagnostic["captured_report"], {"completed_reviews": 3, "status": "clean"})

    def test_a_late_close_after_kill_is_awaited_not_cut_at_the_term_grace(self):
        """A loaded machine closes the pipes late after KILL; the drain waits for that, not for the TERM grace (sd:2609)."""
        ready = self.root / "holder"
        def stop_holder():
            if ready.exists():
                try:
                    os.kill(int(ready.read_text()), ship.signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(stop_holder)
        # The holder leaves the owned group with both pipes, as a slow reader would keep them, and closes them after a second.
        script = ("import os,pathlib,signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nif os.fork()==0:\n os.setsid()\n"
                  f" pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n time.sleep(1)\n os._exit(0)\n"
                  "while True: time.sleep(1)\n")
        original_communicate = subprocess.Popen.communicate
        def after_ready(process, *args, **kwargs):
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.005)
            return original_communicate(process, *args, **kwargs)
        started = time.monotonic()
        # The close comes after 1 s and the drain's bound at 10 s; the rest is slack for a loaded gate (sd:6468).
        with patch.object(subprocess.Popen, "communicate", after_ready), patch.object(ship, "REVIEW_CLEANUP_SECONDS", .05), \
                patch.object(ship, "REVIEW_DRAIN_SECONDS", 10):
            with self.assertRaises(ship.ReviewTimeout) as raised:
                ship.review_process(self.root, [sys.executable, "-c", script], timeout=.05)
        self.assertEqual(raised.exception.diagnostic["cleanup"], {"term": "sent", "kill": "sent", "drained": True, "leader_reaped": True})
        self.assertLess(time.monotonic() - started, 8, "the drain ends at the close, not at its bound")

    def test_a_killed_review_ends_the_sessions_below_it(self):
        """pack sd:9392, failure table row 4: the KILL reached the review's group only; a session sd-review
        started, such as its codex run, ran on with ppid 1."""
        below = self.root / "below.pid"
        script = ("import pathlib,signal,subprocess,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "child = subprocess.Popen(['sleep', '60'], start_new_session=True, stdout=subprocess.DEVNULL, "
                  "stderr=subprocess.DEVNULL)\n"
                  f"pathlib.Path({str(below)!r}).write_text(str(child.pid))\nwhile True: time.sleep(1)\n")
        original_communicate = subprocess.Popen.communicate
        def after_ready(process, *args, **kwargs):
            deadline = time.monotonic() + 5
            while not (below.exists() and below.read_text()) and time.monotonic() < deadline:
                time.sleep(.005)
            return original_communicate(process, *args, **kwargs)
        def stop_below():
            try:
                os.killpg(int(below.read_text()), ship.signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.addCleanup(stop_below)
        with patch.object(subprocess.Popen, "communicate", after_ready), patch.object(ship, "REVIEW_CLEANUP_SECONDS", .05):
            with self.assertRaises(ship.ReviewTimeout):
                ship.review_process(self.root, [sys.executable, "-c", script], timeout=.3)
        for _ in range(60):
            try:
                os.kill(int(below.read_text()), 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        else:
            self.fail("the session below the review outlived its SIGKILL")

    def test_exited_leader_cannot_leave_inherited_pipes_and_a_live_grandchild(self):
        diagnostic = self.expired_group(leader_exits=True)
        self.assertEqual(diagnostic["cleanup"]["kill"], "sent")
        self.assertTrue(diagnostic["cleanup"]["leader_reaped"])

    def test_cleanup_failure_is_bounded_and_explicit(self):
        process = unittest.mock.Mock(pid=123, returncode=None)
        process.communicate.side_effect = subprocess.TimeoutExpired([], .01)
        process.poll.return_value = None
        with patch.object(ship.subprocess, "Popen", return_value=process), patch.object(ship.os, "killpg", side_effect=PermissionError("fixture refusal")):
            with self.assertRaises(ship.ReviewTimeout) as raised:
                ship.review_process(self.root, ["fixture"], timeout=.01)
        # The run and the drain after the KILL; the grace reaps nothing, so it drains nothing (pack sd:9600).
        self.assertEqual(process.communicate.call_count, 2)
        self.assertFalse(raised.exception.diagnostic["cleanup"]["leader_reaped"])
        self.assertIn("failed", raised.exception.diagnostic["cleanup"]["kill"])


    def test_a_reaped_reviews_reused_number_gets_no_sigkill(self):
        """pack sd:9392 round 5, R1: the SIGKILL went to the reaped leader's bare number, which another
        group held by then."""
        process = unittest.mock.Mock(pid=4242, returncode=0)
        process.communicate.side_effect = [subprocess.TimeoutExpired([], 1), (b"", b""), (b"", b"")]
        reads = iter([{4242: (1, 4242, "Thu Jan  1 00:00:00 1970")}, {5000: (1, 4242, "Fri Jan  2 00:00:00 1970")}])
        with patch.object(ship.subprocess, "Popen", return_value=process), \
                patch.object(ship.sd_lib, "process_table", side_effect=lambda: next(reads)), \
                patch.object(ship.sd_lib, "wait_empty"), patch.object(ship.os, "killpg") as killpg:
            with self.assertRaises(ship.ReviewTimeout) as raised:
                ship.review_process(self.root, ["fixture"], timeout=1)
        self.assertNotIn(unittest.mock.call(4242, ship.signal.SIGKILL), killpg.call_args_list)
        self.assertEqual(raised.exception.diagnostic["cleanup"]["kill"], "absent")

    def test_a_term_deaf_replacement_in_a_timed_out_review_is_killed(self):
        """pack sd:9600 review, sd-ship:320: the grace's drain reaped the leader; a worker forked a
        TERM-deaf child on the TERM and exited, so the child held the group with no witness and
        the group SIGKILL was skipped as absent. The child closes the pipes, so the drain reaps."""
        mark = self.root / "worker.pid"
        deaf = self.root / "worker.pid.deaf"
        script = ("import os,pathlib,signal,sys,time\nmark=pathlib.Path(sys.argv[1])\nif os.fork()==0:\n"
                  " def replace(*_):\n  if os.fork()==0:\n   signal.signal(signal.SIGTERM, signal.SIG_IGN)\n   os.close(1)\n   os.close(2)\n"
                  "   pathlib.Path(str(mark)+'.deaf').write_text(str(os.getpid()))\n   time.sleep(30)\n  os._exit(0)\n"
                  " signal.signal(signal.SIGTERM, replace)\n mark.write_text(str(os.getpid()))\n"
                  " while True: time.sleep(1)\nwhile True: time.sleep(1)\n")
        def stop_deaf():
            try:
                os.kill(int(deaf.read_text()), ship.signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.addCleanup(stop_deaf)
        original_communicate = subprocess.Popen.communicate
        def after_ready(process, *args, **kwargs):
            deadline = time.monotonic() + 5
            while not (mark.exists() and mark.read_text()) and time.monotonic() < deadline:
                time.sleep(.005)
            return original_communicate(process, *args, **kwargs)
        with patch.object(subprocess.Popen, "communicate", after_ready), patch.object(ship, "REVIEW_CLEANUP_SECONDS", .5):
            with self.assertRaises(ship.ReviewTimeout) as raised:
                ship.review_process(self.root, [sys.executable, "-c", script, str(mark)], timeout=.3)
        self.assertEqual(raised.exception.diagnostic["cleanup"]["kill"], "sent")
        for _ in range(60):
            try:
                os.kill(int(deaf.read_text()), 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        else:
            self.fail("the TERM-deaf replacement outlived the review's SIGKILL")

    def test_a_review_writing_a_large_report_on_term_finishes_in_the_grace(self):
        """pack sd:9600 review, sd-ship:318: the grace stopped reading the pipes, so a TERM handler
        writing 1 MiB blocked at the pipe's 64 KiB, never reached its cleanup and lost its report."""
        ready = self.root / "ready"
        cleaned = self.root / "cleaned"
        script = ("import os,pathlib,signal,sys,time\n"
                  "def finish(*_):\n"
                  " sys.stdout.buffer.write(b'x' * 1048576)\n sys.stdout.buffer.flush()\n"
                  f" pathlib.Path({str(cleaned)!r}).write_text('done')\n os._exit(0)\n"
                  "signal.signal(signal.SIGTERM, finish)\n"
                  f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
                  "while True: time.sleep(1)\n")
        original_communicate = subprocess.Popen.communicate
        def after_ready(process, *args, **kwargs):
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.005)
            return original_communicate(process, *args, **kwargs)
        with patch.object(subprocess.Popen, "communicate", after_ready), patch.object(ship, "REVIEW_CLEANUP_SECONDS", 3):
            with self.assertRaises(ship.ReviewTimeout) as raised:
                ship.review_process(self.root, [sys.executable, "-c", script], timeout=.3)
        self.assertTrue(cleaned.exists(), "the TERM handler never reached its cleanup")
        self.assertEqual(raised.exception.diagnostic["stdout"]["bytes"], 1048576)

    def test_a_detached_pipe_holder_gets_the_rest_of_the_grace(self):
        """pack sd:9600 review, sd-ship:344: the grace ended once the group was empty, so a
        detached worker still holding a pipe was SIGKILLed while it handled a forwarded TERM.
        The bounded `communicate` it replaced gave a pipe holder the remaining grace."""
        ready = self.root / "ready"
        cleaned = self.root / "cleaned"
        script = ("import os,pathlib,signal,sys,time\n"
                  "worker=os.fork()\n"
                  "if worker==0:\n"
                  " os.setsid()\n"
                  " def finish(*_):\n"
                  "  time.sleep(1)\n"
                  "  sys.stdout.buffer.write(b'worker report')\n  sys.stdout.buffer.flush()\n"
                  f"  pathlib.Path({str(cleaned)!r}).write_text('done')\n  os._exit(0)\n"
                  " signal.signal(signal.SIGTERM, finish)\n"
                  f" pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
                  " while True: time.sleep(1)\n"
                  "def forward(*_):\n"
                  " os.kill(worker, signal.SIGTERM)\n os._exit(0)\n"
                  "signal.signal(signal.SIGTERM, forward)\n"
                  "while True: time.sleep(1)\n")
        def stop_worker():
            try:
                os.kill(int(ready.read_text()), ship.signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.addCleanup(stop_worker)
        original_communicate = subprocess.Popen.communicate
        def after_ready(process, *args, **kwargs):
            deadline = time.monotonic() + 5
            while not (ready.exists() and ready.read_text()) and time.monotonic() < deadline:
                time.sleep(.005)
            return original_communicate(process, *args, **kwargs)
        with patch.object(subprocess.Popen, "communicate", after_ready), patch.object(ship, "REVIEW_CLEANUP_SECONDS", 4):
            with self.assertRaises(ship.ReviewTimeout) as raised:
                ship.review_process(self.root, [sys.executable, "-c", script], timeout=.3)
        self.assertTrue(cleaned.exists(), "the detached worker was killed inside the grace")
        self.assertIn("worker report", raised.exception.diagnostic["stdout"]["tail"])

    def test_an_interrupted_review_read_still_kills(self):
        """pack sd:9392 round 5, R2: a KeyboardInterrupt in the read before the TERM left the review group
        TERMed and never KILLed."""
        process = unittest.mock.Mock(pid=4242, returncode=None)
        process.communicate.side_effect = [subprocess.TimeoutExpired([], 1), (b"", b"")]
        with patch.object(ship.subprocess, "Popen", return_value=process), \
                patch.object(ship.sd_lib, "process_table", side_effect=KeyboardInterrupt), \
                patch.object(ship.os, "killpg") as killpg:
            with self.assertRaises(KeyboardInterrupt):
                ship.review_process(self.root, ["fixture"], timeout=1)
        sent = [call for call in killpg.call_args_list if call.args[1] != 0]
        self.assertEqual(sent, [unittest.mock.call(4242, ship.signal.SIGTERM),
                                unittest.mock.call(4242, ship.signal.SIGKILL)])

    def test_initial_timeout_output_survives_both_failed_cleanup_drains(self):
        body = b'{"findings":[{"summary":"preserved blocker"}]}'
        process = unittest.mock.Mock(pid=123)
        process.communicate.side_effect = [subprocess.TimeoutExpired([], 1, output=body, stderr=b"initial stderr"),
                                           OSError("first drain failed"), subprocess.SubprocessError("second drain failed")]
        process.poll.return_value = 0
        with patch.object(ship.subprocess, "Popen", return_value=process), patch.object(ship.os, "killpg"):
            with self.assertRaises(ship.ReviewTimeout) as raised:
                ship.review_process(self.root, ["fixture"], timeout=1)
        diagnostic = raised.exception.diagnostic
        self.assertEqual(diagnostic["stdout"]["bytes"], len(body))
        self.assertEqual(diagnostic["stderr"]["tail"], "initial stderr")
        self.assertEqual(diagnostic["captured_report"], json.loads(body))
        self.assertFalse(diagnostic["cleanup"]["drained"])

    @staticmethod
    def nested(depth: int) -> bytes:
        """A report whose deepest container sits `depth` levels down."""
        return ('{"deep":' + '[' * (depth - 1) + '0' + ']' * (depth - 1) + '}').encode()

    def expired(self, body: bytes) -> dict:
        process = unittest.mock.Mock(pid=123)
        process.communicate.side_effect = [subprocess.TimeoutExpired([], 1), (body, b""), (body, b"")]
        process.poll.return_value = 0
        with patch.object(ship.subprocess, "Popen", return_value=process), patch.object(ship.os, "killpg"):
            with self.assertRaises(ship.ReviewTimeout) as raised:
                ship.review_process(self.root, ["fixture"], timeout=1)
        return raised.exception.diagnostic

    def test_deep_captured_json_stays_a_typed_timeout(self):
        # One level past the limit this file states, not past whatever the
        # running interpreter's C recursion guard happens to allow. At 10000
        # -- what this fixture used until sd:818 -- the refusal came from
        # `json.loads` on 3.13 and did not come at all on 3.14, so the test
        # read the interpreter rather than the rule.
        body = self.nested(ship.REVIEW_CAPTURE_DEPTH + 1)
        diagnostic = self.expired(body)
        self.assertNotIn("captured_report", diagnostic)
        # `cleanup` names why each of its own steps failed; a dropped capture
        # is named the same way, or the receipt cannot tell a report refused
        # for depth from output that was never JSON at all.
        self.assertEqual(diagnostic["captured_report_refused"],
                         f"nesting deeper than {ship.REVIEW_CAPTURE_DEPTH}")
        self.assertEqual(diagnostic["stdout"]["bytes"], len(body))

    def test_output_that_is_not_json_is_not_reported_as_too_deep(self):
        # The other half of the sentence above. Naming depth is only useful if
        # depth is the only thing it names: output the review never meant as a
        # report must leave the receipt silent rather than accuse it of nesting.
        diagnostic = self.expired(b"review aborted: provider unreachable\n")
        self.assertNotIn("captured_report", diagnostic)
        self.assertNotIn("captured_report_refused", diagnostic)

    def test_an_oversize_body_is_refused_by_name_and_the_largest_admitted_one_is_kept(self):
        # The size bound is older than the depth one and sits outside it, and
        # until sd:837 it was the one refusal that stayed silent: a 2 MB report
        # left the receipt looking exactly like a review that emitted nothing.
        # The sentence names the budget and what was measured against it, as
        # the depth one names the depth. `stdout.bytes` already carries the
        # length, but a reader of the refusal should not have to correlate two
        # fields to learn why the report is not there.
        cap = ship.REVIEW_CAPTURE_BYTES
        frame = b'{"pad":"' + b'"}'
        largest = b'{"pad":"' + b"x" * (cap - len(frame)) + b'"}'
        self.assertEqual(len(largest), cap)
        self.assertEqual(self.expired(largest)["captured_report"], json.loads(largest))
        oversize = largest[:-2] + b'x"}'
        self.assertEqual(len(oversize), cap + 1)
        diagnostic = self.expired(oversize)
        self.assertNotIn("captured_report", diagnostic)
        self.assertEqual(diagnostic["captured_report_refused"], f"{cap + 1} bytes, more than {cap}")
        self.assertEqual(diagnostic["stdout"]["bytes"], cap + 1)

    def test_json_that_is_not_an_object_is_refused_by_its_kind(self):
        # A review's stdout that decodes but is not an object was dropped at
        # `isinstance(captured, dict)` since #802, explicitly and silently. The
        # decoder said what it found, so the receipt says it too, in JSON's
        # own names rather than Python's: a reader of the receipt is looking
        # at a review's output, not at an interpreter.
        cases = ((b"[1]", "array"), (b'"report"', "string"), (b"42", "number"),
                 (b"4.2", "number"), (b"true", "boolean"), (b"null", "null"))
        for body, kind in cases:
            with self.subTest(body=body):
                diagnostic = self.expired(body)
                self.assertNotIn("captured_report", diagnostic)
                self.assertEqual(diagnostic["captured_report_refused"], f"a JSON {kind}, not an object")

    def test_captured_json_at_the_depth_limit_is_still_evidence(self):
        body = self.nested(ship.REVIEW_CAPTURE_DEPTH)
        self.assertEqual(self.expired(body)["captured_report"], json.loads(body))

    def test_a_report_with_more_findings_than_the_limit_is_still_evidence(self):
        # The shape the limit must never refuse, end to end. `sd-review` caps
        # each provider's response at MAX_FINDINGS (50) and merges every
        # provider's rows into one list, so a report grows wide long before it
        # grows deep: these 150 findings are three responses' worth, and 3
        # levels deep. A reader that counted containers instead of nesting
        # would drop exactly this, and drop it silently.
        body = json.dumps({"scope": "branch", "subject": {"head": "a" * 40},
                           "findings": [{"path": "bin/sd-ship", "line": n} for n in range(150)],
                           "authored_with": ["claude"]}).encode()
        self.assertFalse(ship.capture_too_deep(body, limit=3))
        self.assertEqual(self.expired(body)["captured_report"], json.loads(body))

    def test_brackets_inside_a_string_are_text_and_not_depth(self):
        # A shallow report that quotes some JSON is still evidence. Counting
        # every bracket would refuse it, and the tail it quotes is often the
        # only thing that says what the review was doing when it expired. The
        # leading quote is escaped in the encoded body, so a scan that does not
        # honour escapes ends the string there and counts the rest as depth.
        body = json.dumps({"deep": '"' + "[" * (ship.REVIEW_CAPTURE_DEPTH * 10)}).encode()
        self.assertIn(rb'\"[[[', body)
        self.assertEqual(self.expired(body)["captured_report"], json.loads(body))

    def test_interrupt_also_cleans_the_owned_group(self):
        # An interrupted `communicate` reaped nothing, so the number is still the leader's (pack sd:9392).
        process = unittest.mock.Mock(pid=123, returncode=None)
        process.communicate.side_effect = [KeyboardInterrupt(), (b"", b""), (b"", b"")]
        process.poll.return_value = 0
        with patch.object(ship.subprocess, "Popen", return_value=process), patch.object(ship.os, "killpg") as stop:
            with self.assertRaises(KeyboardInterrupt):
                ship.review_process(self.root, ["fixture"], timeout=1)
        # Signal 0 is the probe that reads whether the group lives (pack sd:9392), not a signal sent.
        self.assertEqual([row.args[1] for row in stop.call_args_list if row.args[1]],
                         [ship.signal.SIGTERM, ship.signal.SIGKILL])

    def test_untrusted_timing_values_cannot_disable_the_watchdog(self):
        valid = {"setup_seconds": 3600, "phase_seconds": 1800, "execution_seconds": 9000,
                 "candidates": [{"name": "a", "recipient": "a@fixture"}, {"name": "b", "recipient": "b@fixture"}]}
        cases = [dict(valid, phase_seconds=0), dict(valid, execution_seconds=float("inf")),
                 dict(valid, execution_seconds=True), dict(valid, candidates="not a list"),
                 dict(valid, candidates=[{}]), dict(valid, execution_seconds=3600), dict(valid, setup_seconds=0)]
        for plan in cases:
            with self.subTest(plan=plan), self.assertRaises(ship.Refusal):
                ship.timing_plan(subprocess.CompletedProcess([], 0, json.dumps({"status": "explained", "requested_reviews": 2, "timing": plan}), ""))

    def test_timing_plan_requires_positive_integer_review_depth_and_enough_candidates(self):
        timing = {"setup_seconds": 3600, "phase_seconds": 1800, "execution_seconds": 9000,
                  "candidates": [{"name": "a", "recipient": "a@fixture"}, {"name": "b", "recipient": "b@fixture"}]}
        report = {"status": "explained", "timing": timing}
        for requested in (None, True, 0, -1, "2", 2.0, float("inf"), 3):
            with self.subTest(requested=requested), self.assertRaises(ship.Refusal):
                ship.timing_plan(subprocess.CompletedProcess([], 0, json.dumps(dict(report, requested_reviews=requested)), ""))
        with self.assertRaises(ship.Refusal):
            ship.timing_plan(subprocess.CompletedProcess([], 0, json.dumps(report), ""))
        for requested in (1, 2):
            self.assertEqual(ship.timing_plan(subprocess.CompletedProcess(
                [], 0, json.dumps(dict(report, requested_reviews=requested)), "")), timing)


#: A prepare: `review_process` runs `argv[2:]` as `sd-ship prepare` runs `sd-review`.
STOPPED_PREPARE = """
import importlib.machinery, importlib.util, pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]).parent))
loader = importlib.machinery.SourceFileLoader("sd_ship_stopped", sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
ship = importlib.util.module_from_spec(spec)
loader.exec_module(ship)
ship.review_process(pathlib.Path.cwd(), sys.argv[2:], timeout=600)
"""

#: A review: say its pid, then run `sd-check` as `sd-review`'s runner does, as a group leader through `run_group`.
STOPPED_REVIEW = """
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import sd_lib
pathlib.Path("review.pid").write_text(str(os.getpid()))
sd_lib.run_group([sys.executable, sys.argv[2], "--json"], cwd=pathlib.Path.cwd(), env=dict(os.environ), timeout=600)
"""


class StoppedPrepareTests(unittest.TestCase):
    """SIGTERM to a prepare's process group ends its review and frees the review's gate slot (sd:3203).

    The review leads a session of its own, so the watchdog can end everything
    it started; that session no longer receives what the lane runner's
    `JOB_TIMEOUT` or an operator sends to the prepare's group. A prepare that
    died of it left the review and its gate running with ppid 1, holding a
    slot for half an hour. Real processes and a real slot lock, because the
    claim is about which processes a signal reaches and when the kernel
    frees the lock.
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name).resolve()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True, env=clean_environment())
        (self.root / ship.sd_lib.LOCAL_FILE_NAME).write_text(
            f"{ship.sd_lib.LOCAL_BLOCK_START}\ncheck: sh -c 'echo $$ > check.pid; exec sleep 600'\n"
            f"{ship.sd_lib.LOCAL_BLOCK_END}\n", encoding="utf-8")
        # One slot in a folder of this test's own; the load rule is off, as the gate slot tests have it.
        self.slots = dict(SD_GATE_SLOTS="1", SD_GATE_SLOTS_DIR=str(self.root / "slots"), SD_GATE_SLOT_POLL="0.1",
                          SD_GATE_LOAD_MAX="0", SD_GATE_SETTLE_SECONDS="0")

    def holders(self) -> list[dict]:
        status = subprocess.run([sys.executable, str(ROOT / "bin/sd"), "gate", "status", "--json"], cwd=self.root,
                                env=clean_environment(**self.slots), capture_output=True, text=True, timeout=60, check=True)
        return json.loads(status.stdout)["holders"]

    def pid(self, name: str) -> int | None:
        path = self.root / name
        return int(path.read_text()) if path.is_file() and path.read_text().strip() else None

    @staticmethod
    def running(pid: int) -> bool:
        """Alive and not a zombie waiting for its parent to reap it."""
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith("Z")

    def stop_leftovers(self, pids: list[int]) -> None:
        """Each of these leads a group of its own; a failed run must not leave them for the next test."""
        for pid in pids:
            for stop in (os.killpg, os.kill):
                try:
                    stop(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def wait_for(self, predicate, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() > deadline:
                return False
            time.sleep(.1)
        return True

    def test_term_to_the_prepare_group_ends_the_review_and_frees_its_gate_slot(self):
        prepare = subprocess.Popen([sys.executable, "-c", STOPPED_PREPARE, str(ROOT / "bin/sd-ship"), sys.executable,
                                    "-c", STOPPED_REVIEW, str(ROOT / "bin"), str(ROOT / "bin/sd-check")],
                                   cwd=self.root, env=clean_environment(**self.slots), start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: self.stop_leftovers([prepare.pid] + [pid for pid in (
            self.pid("review.pid"), *[row["pid"] for row in self.holders()], self.pid("check.pid")) if pid]))
        self.assertTrue(self.wait_for(lambda: self.pid("check.pid") and self.holders(), 120),
                        "the review's gate never took a slot")
        review, holder = self.pid("review.pid"), self.holders()[0]["pid"]

        os.killpg(prepare.pid, signal.SIGTERM)
        self.assertEqual(prepare.wait(timeout=60), -signal.SIGTERM, "the prepare dies of SIGTERM, as it would have")

        self.assertTrue(self.wait_for(lambda: not self.running(review), 10), f"review pid {review} outlived its prepare")
        self.assertTrue(self.wait_for(lambda: not self.holders(), 10), f"gate holder pid {holder} kept its slot")
        self.assertFalse(self.running(self.pid("check.pid")), "the gate's check outlived its prepare")

    def test_a_second_term_does_not_cut_the_reviews_end_short(self):
        """A review that ignores SIGTERM gets SIGKILL after the grace, even when a second SIGTERM lands in it."""
        review = ("import os, pathlib, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  "pathlib.Path('review.pid').write_text(str(os.getpid())); time.sleep(600)")
        prepare = subprocess.Popen([sys.executable, "-c", STOPPED_PREPARE, str(ROOT / "bin/sd-ship"), sys.executable,
                                    "-c", review], cwd=self.root, env=clean_environment(), start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: self.stop_leftovers([prepare.pid] + [pid for pid in [self.pid("review.pid")] if pid]))
        self.assertTrue(self.wait_for(lambda: self.pid("review.pid"), 60), "the review never started")
        os.killpg(prepare.pid, signal.SIGTERM)
        time.sleep(1)  # inside the review's grace, `REVIEW_CLEANUP_SECONDS`
        os.kill(prepare.pid, signal.SIGTERM)
        self.assertEqual(prepare.wait(timeout=60), -signal.SIGTERM)
        self.assertTrue(self.wait_for(lambda: not self.running(self.pid("review.pid")), 10),
                        "a review that ignores SIGTERM outlived its prepare")


class CaptureDepthTests(unittest.TestCase):
    """`capture_too_deep` is read directly, not only through the watchdog.

    Until sd:818 the depth rule was CPython's C recursion guard, which is
    correct by construction. This scan replaces it with a reader of our own,
    and a reader can be wrong in two directions: drop a report the operator
    needed, or admit one the limit exists to refuse. Strings are where that
    goes wrong -- #941 had just fixed the same class of bug in the launch
    check's reader of bash quoting -- so each escape case is a row here.
    """

    def test_a_real_shaped_report_is_nowhere_near_the_limit(self):
        # The part of a report `timeout_evidence` reads: the report object,
        # `findings`, a finding -- 3 levels, with `subject` beside `findings`
        # and not under it. It is not as deep as a report gets. Real reports go
        # deeper in fields that reader passes over, 7 in the store on
        # 2026-09-14, which is what `REVIEW_CAPTURE_DEPTH` is measured against.
        # The two limits below keep this comment's "3" honest.
        body = json.dumps({"scope": "branch", "subject": {"head": "a" * 40},
                           "findings": [{"path": "bin/sd-ship", "summary": "x"}],
                           "authored_with": ["claude"]}).encode()
        self.assertTrue(ship.capture_too_deep(body, limit=2))
        self.assertFalse(ship.capture_too_deep(body, limit=3))
        self.assertFalse(ship.capture_too_deep(body))

    def test_the_limit_is_the_deepest_container_that_is_kept(self):
        at_limit = b"[" * ship.REVIEW_CAPTURE_DEPTH + b"0" + b"]" * ship.REVIEW_CAPTURE_DEPTH
        self.assertFalse(ship.capture_too_deep(at_limit))
        self.assertTrue(ship.capture_too_deep(b"[" + at_limit + b"]"))

    def test_every_kind_of_closed_container_gives_its_depth_back(self):
        # Depth is nesting, not a running count of opening brackets. Siblings
        # must not accumulate, or a long findings list is refused for its
        # length rather than its shape.
        #
        # Both kinds, and interleaved. Asserting on `[]` alone left a reader
        # that decrements for `]` and not for `}` passing: braces are what a
        # findings list is actually made of, so that reader refused every real
        # report while this row stayed green (review-950, M6).
        many = ship.REVIEW_CAPTURE_DEPTH * 100
        for body in (b"[]" * many, b"{}" * many, b"[]{}" * many, b"[{}]" * many):
            with self.subTest(body=body[:8]):
                self.assertFalse(ship.capture_too_deep(body))

    def test_brackets_inside_a_string_are_text(self):
        deep = b"[" * (ship.REVIEW_CAPTURE_DEPTH + 1)
        self.assertTrue(ship.capture_too_deep(deep))
        self.assertFalse(ship.capture_too_deep(b'{"quoted":"' + deep + b'"}'))

    def test_an_escaped_quote_does_not_end_a_string(self):
        # A `\"` in the body is a quote in the text, not the end of the string,
        # so what follows it is still text. Read as an ending, the brackets
        # after it become structure and a legitimate report is refused.
        body = rb'{"quoted":"\"' + b"[" * (ship.REVIEW_CAPTURE_DEPTH + 1) + b'"}'
        self.assertEqual(json.loads(body)["quoted"][0], '"')
        self.assertFalse(ship.capture_too_deep(body))

    def test_an_escaped_backslash_leaves_the_closing_quote_closing(self):
        # The mirror case, and the one that fails the other way. A `\\` is a
        # backslash in the text and consumes itself, so the quote after it does
        # end the string and the brackets that follow are structure. A reader
        # that lets that backslash escape the quote hides them, and admits a
        # report past the limit.
        deep = ship.REVIEW_CAPTURE_DEPTH + 1
        body = rb'{"quoted":"x\\","deep":' + b"[" * deep + b"0" + b"]" * deep + b"}"
        self.assertEqual(json.loads(body)["quoted"], "x\\")
        self.assertTrue(ship.capture_too_deep(body))

    def test_an_unterminated_string_swallows_what_follows(self):
        # Under-counting is the safe direction here: an unterminated string is
        # not JSON, so `json.loads` refuses the body a moment later and no
        # report is kept either way. Recorded because it is a real input --
        # output cut off mid-write by the kill the watchdog just sent.
        body = b'{"quoted":"' + b"[" * (ship.REVIEW_CAPTURE_DEPTH + 1)
        self.assertFalse(ship.capture_too_deep(body))
        with self.assertRaises(ValueError):
            json.loads(body)

    def test_the_largest_body_the_gate_admits_is_read_in_one_pass(self):
        # `REVIEW_CAPTURE_BYTES` is the size bound the gate applies before this
        # scan, so these are the worst cases that can reach it. The 60s bound
        # is a guard against a hang and nothing more; a reader that became
        # quadratic is caught by the ratio below, not by this loop.
        cap = ship.REVIEW_CAPTURE_BYTES
        deep = b"[" * (ship.REVIEW_CAPTURE_DEPTH + 1)
        front = deep + b"x" * (cap - len(deep))
        for body in (front,                       # too deep at the first bytes
                     b'"' * cap,                  # nothing but quotes
                     b'"' + b"x" * (cap - 1)):    # one unterminated string
            started = time.monotonic()
            ship.capture_too_deep(body)
            self.assertLess(time.monotonic() - started, 60)
        self.assertTrue(ship.capture_too_deep(front))
        # Eight times the bytes should cost about eight times the work; a reader
        # that copies the rest of the body at every byte (`data[index:][0]`)
        # costs 62 to 77 times as much. That ratio is measured in this thread's
        # CPU time, `time.thread_time`, not on the clock: at load average 107
        # the same scan by wall clock gave the linear reader ratios from 1.6 to
        # 154.3, because the 1 MB scan spans many scheduler slices and the
        # 125 KB scan few, while CPU time gave it 6.0 to 12.0 across sixteen
        # runs on 3.13 and 3.14. The 24 sits between those two ranges.
        small, large = b'"' * 125_000, b'"' * 1_000_000
        small_cpu: list[float] = []
        large_cpu: list[float] = []
        for _ in range(3):
            for body, spent in ((small, small_cpu), (large, large_cpu)):
                started = time.thread_time()
                ship.capture_too_deep(body)
                spent.append(time.thread_time() - started)
        self.assertLess(min(large_cpu) / min(small_cpu), 24)


class DeclaredGapCase(unittest.TestCase):
    """`sd-ship merge` under the repo row's `status` declaration's `unprotected` entry (sd:1110, sd:5109).

    The rig is `ShipCase`'s with the protection object removed -- borrowed
    method by method rather than inherited, so `ShipCase`'s own tests do not
    run a second time under this name. Every test
    here starts from a 404, commits a declaration (or a wrong one) on the
    branch the merge will land, and answers check runs, statuses and
    workflow runs for the exact head. `PUT` counts are the assertion that
    matters; a refusal that still merged is the failure the item exists to
    prevent.
    """

    TESTS = "name: Tests\n\non:\n  pull_request:\n  push:\n    branches: [main]\n\njobs:\n  unittest:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n"
    ROUTE = "name: sd-review route\n\non:\n  pull_request:\n\njobs:\n  route:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n"
    DECLARATION = {
        "accepted_gaps": [{
            "id": "unprotected", "state": {"branch_protection": False},
            "because": "fixture: one operator", "since": "2026-09-12",
            "until": "a second account with push or merge rights exists",
        }]
    }

    args, operation, prepare, merge = ShipCase.args, ShipCase.operation, ShipCase.prepare, ShipCase.merge

    def setUp(self):
        ShipCase.setUp(self)
        self.remote.protection = None
        # Every declaration here commits `.github/workflows/*.yml`, a sensitive
        # path that routes deep, and since sd:1328 a deep change asks Copilot
        # unless something says otherwise. These tests are about check gaps,
        # and the double never completes a Copilot review, so the fixture's
        # machine says `never` rather than letting the default turn every
        # merge below into a wait for a review nobody answers.
        set_setting(self.connection, "sd.copilot_review", "never")

    def commit(self, files: dict[str, str], message: str = "declare\n\nAuthored-with: human") -> str:
        """Commit `files` on `topic` at the remote and pull them into the clone.

        The reviewed head is what the merge reads, so every fixture file has
        to be in the commit, not the working tree.
        """
        sha = self.remote.commit_on("topic", message, files=files)
        _git(self.root, "fetch", "-q", "origin", "topic")
        _git(self.root, "reset", "-q", "--hard", "FETCH_HEAD")
        return sha

    def declare(self, entries=None, *, workflows=None) -> str:
        """`entries` as the row's `status` declaration (sd:5109), and the workflows committed on `topic`."""
        set_declaration(self.connection, str(self.operator), "status", entries if entries is not None else self.DECLARATION)
        files = {}
        for name, text in (workflows if workflows is not None else {"tests.yml": self.TESTS, "sd-review-route.yml": self.ROUTE}).items():
            files[f".github/workflows/{name}"] = text
        return self.commit(files)

    def restart(self) -> None:
        """A fresh rig mid-test: the loops below merge, and a merged fixture cannot merge twice."""
        self.doCleanups()
        self.setUp()

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD")

    def adapter(self) -> ship.GitHub:
        return ship.GitHub(self.root, self.remote.slug)

    def run_record(self, path: str, *, event: str = "pull_request", conclusion: str = "success", sha: str | None = None, status: str = "completed") -> dict:
        return {"path": path, "name": path.rsplit("/", 1)[-1], "event": event, "status": status,
                "conclusion": conclusion, "head_sha": sha or self.head()}

    def check(self, name: str, *, conclusion: str = "success", sha: str | None = None, status: str = "completed") -> dict:
        return {"name": name, "status": status, "conclusion": conclusion, "head_sha": sha or self.head(), "app": {"id": 7}}

    def green(self) -> None:
        """Every check, status and expected workflow run passing at the head."""
        self.prepare()
        pull = next(iter(self.remote.pull_requests.values()))
        pull.checks = [self.check("unittest (ubuntu-latest, 3.13)"), self.check("route"), self.check("copilot-pull-request-reviewer")]
        self.double.workflow_runs = [self.run_record(".github/workflows/tests.yml"), self.run_record(".github/workflows/sd-review-route.yml")]

    def puts(self) -> int:
        return len([call for call in self.remote.calls if call.method == "PUT"])

    def refuse(self, pattern: str, code: str | None = None) -> ship.Refusal:
        with self.assertRaisesRegex(ship.Refusal, pattern) as caught:
            self.merge()
        if code is not None:
            self.assertEqual(caught.exception.workflow["blocker"]["code"], code)
        self.assertEqual(self.puts(), 0)
        return caught.exception

    def test_a_declared_gap_with_every_check_green_merges_once_and_the_receipt_says_so(self):
        self.declare()
        self.green()
        result = self.merge()
        self.assertEqual(self.puts(), 1)
        self.assertEqual(result["protection"], {"declared_gap": "unprotected", "until": "a second account with push or merge rights exists"})
        key = receipts.receipt_key(self.remote.slug, "topic", self.item)
        self.assertEqual(receipts.read(self.connection, key)[1]["protection"]["declared_gap"], "unprotected")

    def jenkins_green(self, *, workflows=None) -> None:
        """A policy naming Jenkins, its build of the head green, and no check run or status on GitHub."""
        self.declare(workflows=workflows)
        set_declaration(self.connection, str(self.operator), "policy", {"ci": {
            "provider": "jenkins", "url": "http://127.0.0.1:9", "job": "team/repo/PR-{pr}"}})
        self.prepare()
        next(iter(self.remote.pull_requests.values())).checks = []
        self.double.workflow_runs = []
        build = {"result": "SUCCESS", "building": False, "actions": [{"lastBuiltRevision": {"SHA1": self.head()}}]}
        patcher = patch.object(ship.sd_ship_jenkins, "head_build", return_value=build)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_green_jenkins_build_validates_the_head_under_a_declared_gap(self):
        """sd:6880, review of build 317. every_check refused ci_missing on a head only Jenkins validated."""
        self.jenkins_green(workflows={})
        with patch.object(ship.time, "sleep"):
            self.merge()
        self.assertEqual(self.puts(), 1)

    def test_a_green_jenkins_build_still_needs_the_heads_pull_request_runs(self):
        self.jenkins_green()
        with patch.object(ship.time, "sleep"):
            self.refuse(r"workflow .* has no successful pull_request run", "ci_missing")

    # -- sd:1843: `repo.ci = local` swaps pull_request runs for sd/local-gate --

    def local_ci(self) -> None:
        """The row says `local`. Patched at the one reader, because the
        library this suite pins predates the column."""
        patcher = patch.object(ship.sd_lib, "repo_ci", lambda connection, root: "local")
        patcher.start()
        self.addCleanup(patcher.stop)

    def gate_posts(self) -> list:
        return [call for call in self.remote.calls if call.method == "POST" and "/statuses/" in call.path]

    def local_green(self) -> None:
        """Every check green and no pull_request run at all: nothing ran on Actions."""
        self.prepare()
        pull = next(iter(self.remote.pull_requests.values()))
        pull.checks = [self.check("route"), self.check("copilot-pull-request-reviewer")]
        self.double.workflow_runs = []

    def test_ci_local_merges_on_the_local_gate_without_pull_request_runs(self):
        self.declare()
        self.local_ci()
        self.local_green()
        result = self.merge()
        self.assertEqual(self.puts(), 1)
        [post] = self.gate_posts()
        self.assertTrue(post.path.endswith(f"/statuses/{self.head()}"))
        self.assertEqual((post.body["context"], post.body["state"]), ("sd/local-gate", "success"))
        key = receipts.receipt_key(self.remote.slug, "topic", self.item)
        gate = receipts.read(self.connection, key)[1]["local_gate"]
        self.assertEqual((gate["head"], gate["status"]), (self.head(), "success"))
        self.assertEqual(result["protection"]["declared_gap"], "unprotected")

    # -- sd:2050: GitHub recomputes the merge rules after the gate's status --

    # -- sd:2075: `unstable` is GitHub allowing the merge with an optional check open --

    def ruleset_protection(self) -> None:
        """A protection object requiring `route` only, as #583 required `sd/local-gate` only."""
        self.double.rules = self.gating_rules()
        self.double.rulesets = {42: {"id": 42, "name": "main", "enforcement": "active", "bypass_actors": []}}

    def optional_check_open(self, *, mergeable: str = "MERGEABLE") -> None:
        pull = next(iter(self.remote.pull_requests.values()))
        pull.checks.append(self.check("Analyze (python)", status="in_progress", conclusion=None))
        pull.mergeable, pull.merge_state_status = mergeable, "UNSTABLE"

    def test_an_unstable_answer_after_the_gate_post_merges_without_waiting(self):
        # sd:2075, example-org/benchmark #583: sd/local-gate success, the
        # optional CodeQL run in progress, and five reads refused `unstable`.
        self.ruleset_protection()
        self.local_ci()
        self.local_green()
        self.optional_check_open()
        with patch.object(ship.time, "sleep") as sleep:
            self.merge()
        self.assertEqual(self.puts(), 1)
        self.assertEqual(pull_head_waits(sleep), [])

    def test_an_unstable_answer_merges_without_a_local_gate(self):
        self.ruleset_protection()
        self.green()
        self.optional_check_open()
        self.merge()
        self.assertEqual(self.puts(), 1)

    def test_an_unstable_answer_github_cannot_merge_still_refuses(self):
        self.ruleset_protection()
        self.green()
        self.optional_check_open(mergeable="CONFLICTING")
        self.refuse(r"mergeable_state 'unstable', mergeable False", "merge_rules_unconfirmed")

    def test_an_unstable_answer_under_a_declared_gap_still_needs_every_check(self):
        self.declare()
        self.local_ci()
        self.local_green()
        self.optional_check_open()
        self.refuse(r"CI is not passing on .*: Analyze \(python\)", "ci_not_passing")

    def test_a_blocked_answer_after_the_gate_post_is_read_again_until_clean(self):
        self.declare()
        self.local_ci()
        self.local_green()
        self.double.blocked_after_status = 2
        with patch.object(ship.time, "sleep") as sleep:
            self.merge()
        self.assertEqual(self.puts(), 1)
        self.assertEqual(self.double.blocked_pull_reads, 0)
        self.assertEqual(pull_head_waits(sleep), [ship.MERGE_RULES_INTERVAL_SECONDS, 2 * ship.MERGE_RULES_INTERVAL_SECONDS])

    def test_merge_rules_that_never_clear_refuse_with_githubs_last_answer(self):
        self.declare()
        self.local_ci()
        self.local_green()
        self.double.blocked_after_status = ship.MERGE_RULES_ATTEMPTS
        with patch.object(ship.time, "sleep") as sleep:
            with self.assertRaisesRegex(ship.Refusal, r"has not confirmed all required merge rules.*'blocked'.*5 reads") as caught:
                self.merge()
        workflow = caught.exception.workflow
        self.assertEqual(workflow["blocker"]["code"], "merge_rules_unconfirmed")
        self.assertEqual(workflow["state"], "retryable_failure")
        waits = pull_head_waits(sleep)
        self.assertEqual(waits, [ship.MERGE_RULES_INTERVAL_SECONDS * n for n in range(1, ship.MERGE_RULES_ATTEMPTS)])
        self.assertLess(sum(waits), 60)
        self.assertEqual(self.puts(), 0)

    def test_without_a_posted_gate_a_blocked_answer_is_read_once(self):
        self.declare()
        self.green()
        next(iter(self.remote.pull_requests.values())).merge_state_status = "BLOCKED"
        with patch.object(ship.time, "sleep") as sleep:
            self.refuse(r"has not confirmed all required merge rules", "merge_rules_unconfirmed")
        self.assertEqual(pull_head_waits(sleep), [])

    def test_a_behind_pull_request_is_named_without_a_local_gate(self):
        self.declare()
        self.green()
        next(iter(self.remote.pull_requests.values())).merge_state_status = "BEHIND"
        self.refuse(r"the pull request is BEHIND main", "base_moved")

    def test_a_behind_pull_request_is_named_before_the_gate_runs(self):
        # sd:2023: strict protection answers BEHIND, and the refusal said only
        # that the merge rules were unconfirmed, after a full gate run.
        self.declare()
        self.local_ci()
        self.local_green()
        next(iter(self.remote.pull_requests.values())).merge_state_status = "BEHIND"
        with patch.object(ship.time, "sleep") as sleep:
            with self.assertRaisesRegex(ship.Refusal, "BEHIND") as caught:
                self.merge()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "base_moved")
        self.assertIn("--catch-up", caught.exception.workflow["next_action"])
        self.assertEqual(self.gate_posts(), [])
        self.assertEqual(pull_head_waits(sleep), [])
        self.assertEqual(self.puts(), 0)

    def merge_gate_fails(self, failing: str) -> None:
        """Commit a check that passes in the operator's clone, where `.git` is a
        directory, and fails in a worktree, where it is a file. Prepare's branch
        review also checks in a worktree (sd:2077), so a marker outside both
        lets that first worktree run pass and every later one run `failing`."""
        marker = pathlib.Path(tempfile.mkdtemp()) / "prepared"
        self.addCleanup(shutil.rmtree, marker.parent, True)
        self.commit({"Makefile": f"check:\n\t@test -d .git || if [ -e {marker} ]; then {failing}; else touch {marker}; fi\n"})

    def test_ci_local_with_a_failing_check_posts_failure_and_refuses(self):
        """The merge gate's check fails in its worktree: the real run, and proof
        it ran in the worktree rather than the checkout."""
        self.merge_gate_fails("exit 1")
        self.declare()
        # Prepared before the switch, so prepare's check ran first and passed.
        self.local_green()
        self.local_ci()
        self.refuse(r"sd/local-gate is failure", "ci_not_passing")
        self.assertEqual([call.body["state"] for call in self.gate_posts()], ["failure"])

    def test_ci_local_failure_names_the_failing_check_its_tail_and_the_receipt(self):
        """sd:2066. The merge said only `sd-check fail (check fail)`, the status
        description; finding the failing step meant running the gate again."""
        self.merge_gate_fails("echo sd2066-out; echo sd2066-err >&2; exit 1")
        self.declare()
        self.local_green()
        self.local_ci()
        with self.assertRaises(ship.Refusal) as caught:
            self.merge()
        message = str(caught.exception)
        self.assertIn("sd/local-gate is failure", message)
        self.assertRegex(message, r"check \(exit 2\): whole output: (\S+/sd-check-output/\S+-check\.log)\n"
                                  r"stderr: sd2066-err\n.*\nstdout: sd2066-out")
        # sd:2558. The named file is in the repository's Git directory and holds the whole output.
        kept = pathlib.Path(re.search(r"whole output: (\S+)", message).group(1))
        self.assertEqual(kept.parent.parent, (self.root / ".git").resolve())
        self.assertIn("sd2066-out", kept.read_text(encoding="utf-8"))
        self.assertIn(f"`sd-ship observe --item {self.item} --json` prints it", message)
        self.assertEqual(self.puts(), 0)
        observed = self.operation("observe").observe()
        self.assertEqual(observed["receipt"]["local_gate"]["status"], "failure")

    def test_ci_local_prepare_runs_its_check_in_the_gates_worktree(self):
        """sd:2041. Under `repo.ci = local` prepare's check is the gate's, so the
        same Makefile fails at prepare, after the review cleared (sd:2605), and not first at merge."""
        self.commit({"Makefile": "check:\n\t@test -d .git\n"})
        self.declare()
        self.local_ci()
        with self.assertRaisesRegex(ship.Refusal, "the repository gate failed after the review cleared"):
            self.local_green()

    def test_ci_local_merge_reuses_prepares_passing_gate(self):
        """sd:2041. Prepare's gate leaves a receipt at the head; the merge gate reads it and says so."""
        counter = self.root.parent / "gate-runs"
        self.commit({"Makefile": f"check:\n\t@echo run >> {counter}\n"})
        self.declare()
        self.local_ci()
        self.local_green()
        self.merge()
        [post] = self.gate_posts()
        self.assertEqual(post.body["state"], "success")
        self.assertTrue(post.body["description"].endswith("(reused)"), post.body["description"])
        self.assertEqual(len(counter.read_text().splitlines()), 1)

    def test_ci_local_refuses_a_gate_status_for_another_sha(self):
        """A success recorded against another commit is not evidence for this head."""
        self.declare()
        self.local_ci()
        self.local_green()
        self.double.statuses = [{"context": "sd/local-gate", "state": "success", "sha": "f" * 40}]
        with patch.object(ship.sd_local_gate, "check_in_worktree",
                          lambda root, head, **_: {"head": "e" * 40, "status": "success", "summary": "elsewhere"}):
            self.refuse(r"no status is posted for a commit that was not checked", "local_gate_mismatch")
        self.assertEqual(self.gate_posts(), [])

    def test_ci_local_refuses_a_gate_success_another_account_posted(self):
        """Anyone with write access can post `sd/local-gate`; readiness trusts only this token's own."""
        self.declare()
        self.local_ci()
        self.local_green()
        self.double.statuses = [{"context": "sd/local-gate", "state": "success", "sha": self.head(),
                                 "creator": {"login": "someone-else"}, "description": "posted elsewhere"}]
        with patch.object(ship.sd_local_gate, "local_gate", lambda api, root, head, **_: {"head": head}):
            self.refuse(r"sd/local-gate on .* was posted by someone-else, not fixture", "local_gate_foreign")

    def test_ci_local_runs_its_own_gate_over_another_accounts_success(self):
        self.declare()
        self.local_ci()
        self.local_green()
        self.double.statuses = [{"context": "sd/local-gate", "state": "success", "sha": self.head(),
                                 "creator": {"login": "someone-else"}, "description": "posted elsewhere"}]
        self.merge()
        self.assertEqual(self.puts(), 1)
        self.assertEqual([call.body["state"] for call in self.gate_posts()], ["success"])

    def test_ci_local_with_no_gate_status_refuses_as_missing(self):
        """Nothing posted, nothing ran: no pull_request run is asked for, so the
        missing status is the one thing that stops the merge."""
        self.declare()
        self.local_ci()
        self.local_green()
        with patch.object(ship.sd_local_gate, "local_gate", lambda api, root, head, **_: {"head": head, "status": "skipped"}):
            self.refuse(r"sd/local-gate has no status on", "ci_missing")

    def test_ci_local_under_protection_requires_the_gate_beside_the_required_contexts(self):
        """Path A: protection requires `route` only; a local repository needs its gate too."""
        self.commit({".github/sd-review.json": "{}\n"})
        self.remote.protection = {"enforce_admins": {"enabled": True},
                                  "required_pull_request_reviews": {"required_approving_review_count": 0},
                                  "required_status_checks": {"strict": True, "contexts": ["route"]}}
        self.local_ci()
        self.local_green()
        with patch.object(ship.sd_local_gate, "check_in_worktree",
                          lambda root, head, **_: {"head": head, "status": "failure", "summary": "sd-check fail"}):
            self.refuse(r"sd/local-gate is failure")
        self.restart()
        self.commit({".github/sd-review.json": "{}\n"})
        self.remote.protection = {"enforce_admins": {"enabled": True},
                                  "required_pull_request_reviews": {"required_approving_review_count": 0},
                                  "required_status_checks": {"strict": True, "contexts": ["sd/local-gate"]}}
        self.local_ci()
        self.local_green()
        self.merge()
        self.assertEqual(self.puts(), 1)

    def test_ci_github_is_unchanged_no_gate_runs_and_pull_request_runs_are_required(self):
        self.declare()
        self.green()
        self.double.workflow_runs = [self.run_record(".github/workflows/sd-review-route.yml")]
        with patch.object(ship.sd_local_gate, "local_gate", side_effect=AssertionError("gate ran under ci=github")):
            self.refuse(r"workflow .* has no successful pull_request run", "ci_missing")
        self.assertEqual(self.gate_posts(), [])

    def test_a_ruleset_that_gates_the_merge_is_path_a_without_a_declaration(self):
        """example-org/log-tool's shape on the fixture: no classic object, one
        active ruleset requiring a pull request and the strict `route` check
        bound to app 7. No declaration, and the merge lands once; the receipt's
        protection object says where it came from (sd:1327)."""
        self.commit({".github/workflows/tests.yml": self.TESTS, ".github/workflows/sd-review-route.yml": self.ROUTE})
        self.double.rules = self.gating_rules()
        self.double.rulesets = {42: {"id": 42, "name": "main", "enforcement": "active", "bypass_actors": []}}
        self.green()
        result = self.merge()
        self.assertEqual(self.puts(), 1)
        self.assertEqual(result["protection"]["source"], "ruleset")
        self.assertEqual([entry["id"] for entry in result["protection"]["rulesets"]], [42])
        self.assertEqual(result["protection"]["required_status_checks"]["checks"], [{"context": "route", "app_id": 7}])

    def test_a_ruleset_only_main_requiring_ci_is_path_a(self):
        """The fleet's target shape until sd:5406 (sd:1741): no classic object, one active
        ruleset `main` requiring the strict aggregate `ci` from GitHub Actions
        (app 15368), no bypass actor. A green `ci` merges once with no
        declaration; a red one refuses and dispatches nothing (sd:1810)."""
        tests = self.TESTS + "  ci:\n    needs: [unittest]\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n"
        self.commit({".github/workflows/tests.yml": tests, ".github/workflows/sd-review-route.yml": self.ROUTE})
        rules = self.gating_rules()
        rules[2]["parameters"]["required_status_checks"] = [{"context": "ci", "integration_id": 15368}]
        self.double.rules = rules
        self.double.rulesets = {42: {"id": 42, "name": "main", "enforcement": "active", "bypass_actors": []}}
        self.green()
        pull = next(iter(self.remote.pull_requests.values()))
        ci = {**self.check("ci"), "app": {"id": 15368}}
        pull.checks.append({**ci, "conclusion": "failure"})
        with self.assertRaisesRegex(ship.Refusal, r"required CI is not passing on [0-9a-f]{40}: ci$"):
            self.merge()
        self.assertEqual(self.puts(), 0)
        pull.checks[-1] = ci
        result = self.merge()
        self.assertEqual(self.puts(), 1)
        self.assertEqual(result["protection"]["source"], "ruleset")
        self.assertEqual(result["protection"]["required_status_checks"],
                         {"strict": True, "contexts": ["ci"], "checks": [{"context": "ci", "app_id": 15368}]})

    def test_a_ruleset_that_forbids_squash_refuses_before_any_merge_is_dispatched(self):
        """sd:1379. A ruleset's `pull_request` rule names the merge methods the
        branch accepts, and `synthesize` dropped them, so the gate passed and
        `sd-ship` sent a squash the branch forbids. The refusal names the
        methods allowed and dispatches nothing; a list that includes squash
        still merges once."""
        self.commit({".github/workflows/tests.yml": self.TESTS, ".github/workflows/sd-review-route.yml": self.ROUTE})
        rules = self.gating_rules()
        rules[1]["parameters"]["allowed_merge_methods"] = ["merge", "rebase"]
        self.double.rules = rules
        self.double.rulesets = {42: {"id": 42, "name": "main", "enforcement": "active", "bypass_actors": []}}
        self.green()
        with self.assertRaisesRegex(ship.Refusal, r"allows only merge, rebase .*squash"):
            self.merge()
        self.assertEqual(self.puts(), 0)
        rules[1]["parameters"]["allowed_merge_methods"] = ["squash", "rebase"]
        result = self.merge()
        self.assertEqual(self.puts(), 1)
        self.assertEqual(result["protection"]["allowed_merge_methods"], ["rebase", "squash"])

    @staticmethod
    def gating_rules() -> list:
        return [
            {"type": "deletion", "ruleset_id": 42},
            {"type": "pull_request", "ruleset_id": 42, "parameters": {"required_approving_review_count": 0}},
            {"type": "required_status_checks", "ruleset_id": 42,
             "parameters": {"strict_required_status_checks_policy": True,
                            "required_status_checks": [{"context": "route", "integration_id": 7}]}}]

    def test_gating_rules_on_the_second_page_of_rules_still_gate(self):
        """Thirty rules fill the endpoint's first page; the rules that gate
        the merge sit on the second. A reader that stopped at one page would
        take Path B and refuse for want of a declaration. The merge lands,
        and the receipt's object carries the second page's check."""
        self.commit({".github/workflows/tests.yml": self.TESTS, ".github/workflows/sd-review-route.yml": self.ROUTE})
        filler = [{"type": "tag_name_pattern", "ruleset_id": 42, "parameters": {"pattern": f"v{n}"}} for n in range(30)]
        self.double.rules = filler + self.gating_rules()
        self.double.rulesets = {42: {"id": 42, "name": "main", "enforcement": "active", "bypass_actors": []}}
        self.green()
        result = self.merge()
        self.assertEqual(self.puts(), 1)
        self.assertEqual(result["protection"]["required_status_checks"]["checks"], [{"context": "route", "app_id": 7}])

    KEY_WORDS = "main (#42) [pull_request, required_status_checks]: DeployKey (always)"

    @staticmethod
    def accepted(gap: str, state: dict) -> dict:
        return {"id": gap, "state": state, "because": "autocommit pushes", "since": "2026-09-23",
                "until": "autocommit lands through pull requests"}

    def keyed_lax(self, declared: list | None) -> None:
        """platypeeps/system's shape (sd:1451): a ruleset whose named checks
        are not strict and which a deploy key can push past, with `declared`
        committed as the reviewed head's `accepted_gaps`, every check green."""
        rules = self.gating_rules()
        rules[2]["parameters"]["strict_required_status_checks_policy"] = False
        if declared is None:
            self.commit({".github/workflows/tests.yml": self.TESTS, ".github/workflows/sd-review-route.yml": self.ROUTE})
        else:
            self.declare({"accepted_gaps": declared})
        self.double.rules = rules
        self.double.rulesets = {42: {"id": 42, "name": "main", "enforcement": "active", "bypass_actors": [
            {"actor_id": None, "actor_type": "DeployKey", "bypass_mode": "always"}]}}
        self.green()

    def test_a_deploy_key_bypass_and_lax_checks_merge_only_when_the_reviewed_head_declares_both(self):
        """Undeclared, the merge refuses naming the key and pushes nothing.
        With only the bypass declared, it refuses on the lax checks. With
        both declared at the reviewed head in `sd-status`'s words, it lands
        once and the receipt names the two entries it honoured."""
        bypass = self.accepted("bypass", {"bypass": [self.KEY_WORDS]})
        strict = self.accepted("strict", {"strict": False, "bypass": [self.KEY_WORDS]})
        for declared, refusal in (
            (None, r"can be bypassed by DeployKey \(always\)$"),
            ([bypass], r"^ruleset protection requires strict, named CI checks$"),
            ([bypass, strict], None),
        ):
            with self.subTest(declared=declared):
                self.restart()
                self.keyed_lax(declared)
                if refusal is not None:
                    self.refuse(refusal, "protection_required" if declared is None else None)
                    continue
                result = self.merge()
                self.assertEqual(self.puts(), 1)
                self.assertEqual([gap["id"] for gap in result["protection"]["accepted_gaps"]], ["bypass", "strict"])
                key = receipts.receipt_key(self.remote.slug, "topic", self.item)
                self.assertEqual([gap["id"] for gap in receipts.read(self.connection, key)[1]["protection"]["accepted_gaps"]],
                                 ["bypass", "strict"])

    def test_the_base_advancing_before_the_put_refuses_under_an_accepted_strict_gap(self):
        """With `strict` off nothing server-side keeps the branch fresh, as
        under the declared gap, so the freshness read is read again."""
        self.keyed_lax([self.accepted("bypass", {"bypass": [self.KEY_WORDS]}),
                        self.accepted("strict", {"strict": False, "bypass": [self.KEY_WORDS]})])
        seen = {"count": 0}
        double = self.double
        saved = double._route

        def route(method, path, body):
            if method == "GET" and "/compare/" in path:
                seen["count"] += 1
                if seen["count"] >= 2:
                    return 200, {"behind_by": 3, "ahead_by": 1, "status": "diverged"}
            return saved(method, path, body)
        double._route = route
        self.refuse("default branch advanced after the readiness check", "base_moved")

    def test_a_ruleset_that_does_not_show_its_bypass_actors_refuses_the_merge(self):
        """The same ruleset with `bypass_actors` withheld, which is what GitHub
        answers a token that cannot edit it: unknown, and unknown does not
        merge. Nothing is pushed."""
        self.commit({".github/workflows/tests.yml": self.TESTS, ".github/workflows/sd-review-route.yml": self.ROUTE})
        self.double.rules = self.gating_rules()
        self.double.rulesets = {42: {"id": 42, "name": "main", "enforcement": "active"}}
        self.green()
        self.refuse("did not show its bypass actors", "prerequisite_failed")
        self.assertEqual(self.puts(), 0)

    def test_a_ruleset_that_only_forbids_deletion_is_not_protection(self):
        """example-org/simulator's ruleset 21772988: `deletion` and
        `non_fast_forward`, nothing a pull request must satisfy. Path B, so the
        refusal is the present one and a declaration is honoured."""
        self.double.rules = [{"type": "deletion", "ruleset_id": 42}, {"type": "non_fast_forward", "ruleset_id": 42}]
        self.double.rulesets = {42: {"id": 42, "name": "main", "enforcement": "active"}}
        self.commit({".github/workflows/tests.yml": self.TESTS, ".github/workflows/sd-review-route.yml": self.ROUTE})
        self.green()
        self.refuse("Branch not protected", "protection_required")
        self.restart()
        self.double.rules = [{"type": "deletion", "ruleset_id": 42}, {"type": "non_fast_forward", "ruleset_id": 42}]
        self.double.rulesets = {42: {"id": 42, "name": "main", "enforcement": "active"}}
        self.declare()
        self.green()
        self.merge()
        self.assertEqual(self.puts(), 1)

    def test_without_the_declaration_the_refusal_is_the_present_one(self):
        self.commit({".github/workflows/tests.yml": self.TESTS, ".github/workflows/sd-review-route.yml": self.ROUTE})
        self.green()
        self.refuse("Branch not protected", "protection_required")

    def test_a_failing_check_run_at_the_head_refuses(self):
        self.declare()
        self.green()
        pull = next(iter(self.remote.pull_requests.values()))
        pull.checks[0] = self.check("unittest (ubuntu-latest, 3.13)", conclusion="failure")
        self.refuse("CI is not passing", "ci_not_passing")

    def test_a_failing_run_at_another_sha_with_a_passing_rerun_at_the_head_still_refuses(self):
        """`filter=latest` at the head is what counts; the double reports both."""
        self.declare()
        self.green()
        pull = next(iter(self.remote.pull_requests.values()))
        pull.checks.append(self.check("unittest (ubuntu-latest, 3.13)", conclusion="failure", sha="0" * 40))
        self.refuse("CI is not passing", "ci_not_passing")

    def test_two_suites_that_disagree_refuse_under_the_gap_and_name_the_fresh_commit(self):
        """sd:1779 on the declared-gap path: the same refusal and code, with the
        fresh-commit hint only when one check fails in one suite and passes
        in another."""
        for label, runs, hinted in (
            ("two suites disagree", ((98314641878, "failure"), (98314947143, "success")), True),
            ("two suites disagree, listed the other way", ((98314947143, "success"), (98314641878, "failure")), True),
            ("one failing run", ((98314641878, "failure"),), False),
        ):
            with self.subTest(label=label):
                self.restart()
                self.declare()
                self.green()
                pull = next(iter(self.remote.pull_requests.values()))
                pull.checks = [check for check in pull.checks if check["name"] != "route"]
                pull.checks += [{**self.check("route", conclusion=conclusion), "check_suite": {"id": suite}}
                                for suite, conclusion in runs]
                with self.assertRaisesRegex(ship.Refusal, "CI is not passing on .*: route") as caught:
                    self.merge()
                workflow = caught.exception.workflow
                self.assertEqual(workflow["blocker"]["code"], "ci_not_passing")
                self.assertEqual("push a fresh commit" in workflow["next_action"], hinted, workflow["next_action"])
                self.assertEqual(self.puts(), 0)

    def test_status_history_is_read_newest_first_per_context(self):
        for history, puts in (("success,pending", 1), ("success,failure", 1), ("failure,success", 0), ("pending,success", 0)):
            with self.subTest(history=history):
                self.restart()
                self.declare()
                self.green()
                self.double.statuses = [{"context": "legacy", "state": state, "sha": self.head()} for state in history.split(",")]
                if puts:
                    self.merge()
                    self.assertEqual(self.puts(), 1)
                else:
                    self.refuse("status is not passing", "ci_not_passing")

    def test_nothing_validated_the_head_refuses(self):
        self.declare()
        self.green()
        next(iter(self.remote.pull_requests.values())).checks = []
        self.double.workflow_runs = []
        self.refuse("nothing validated", "ci_missing")

    def test_a_dirty_head_with_no_checks_names_the_conflict_not_the_checks(self):
        # sd:1403: an empty rollup on a DIRTY head is no slow queue; GitHub
        # builds no merge ref and never dispatches the run the advice asks for.
        self.declare()
        self.green()
        pull = next(iter(self.remote.pull_requests.values()))
        pull.checks = []
        self.double.workflow_runs = []
        pull.mergeable, pull.merge_state_status = "CONFLICTING", "DIRTY"
        with self.assertRaisesRegex(ship.Refusal, "conflicts with its base") as caught:
            self.merge()
        workflow = caught.exception.workflow
        self.assertEqual(workflow["blocker"]["code"], "merge_conflict")
        self.assertNotIn("Run the repository's checks", workflow["next_action"])
        self.assertIn("resolve the conflict", workflow["next_action"])
        self.assertEqual(self.puts(), 0)

    def test_an_unknown_mergeability_is_one_retry(self):
        self.declare()
        self.green()
        pull = next(iter(self.remote.pull_requests.values()))
        pull.checks = []
        pull.mergeable, pull.merge_state_status = "UNKNOWN", "UNKNOWN"
        with self.assertRaisesRegex(ship.Refusal, "not finished computing mergeability") as caught:
            self.merge()
        self.assertEqual(caught.exception.workflow["state"], "retryable_failure")
        self.assertEqual(self.puts(), 0)

    def test_only_the_advisory_workflow_ran_refuses_naming_tests(self):
        for spelling in ("tests.yml", "tests.yaml"):
            with self.subTest(spelling=spelling):
                self.restart()
                self.declare(workflows={spelling: self.TESTS, "sd-review-route.yml": self.ROUTE})
                self.prepare()
                next(iter(self.remote.pull_requests.values())).checks = [self.check("route")]
                self.double.workflow_runs = [self.run_record(".github/workflows/sd-review-route.yml")]
                self.refuse(r"workflow Tests \(\.github/workflows/" + re.escape(spelling) + r"\) has no successful pull_request run", "ci_missing")

    def test_the_expected_set_is_read_at_the_head_not_the_working_tree(self):
        """`merge` refuses a dirty tree before it reads anything, so the
        reader is asked directly: the tree has no say either way."""
        head = self.declare()
        expected = [(".github/workflows/sd-review-route.yml", "sd-review route"), (".github/workflows/tests.yml", "Tests")]
        # Present at head, deleted from the working tree: still expected.
        (self.root / ".github/workflows/tests.yml").unlink()
        self.assertEqual(self.adapter().expected_workflows(head), expected)
        # Absent at head, present only in the working tree: not expected.
        _git(self.root, "rm", "-q", ".github/workflows/tests.yml")
        _git(self.root, "commit", "-q", "-m", "drop tests\n\nAuthored-with: human")
        head = self.head()
        (self.root / ".github/workflows/tests.yml").write_text(self.TESTS)
        self.assertEqual(self.adapter().expected_workflows(head), expected[:1])

    def test_a_non_ascii_workflow_name_is_still_expected(self):
        """`ls-tree --name-only` quotes a non-ASCII path by default, so
        `tëst.yml` came back as `".github/workflows/t\\303\\253st.yml"`, failed
        the `.yml` suffix test on its closing quote, and the merge stopped
        waiting for its run (sd:1440)."""
        head = self.declare(workflows={"tëst.yml": self.TESTS, "sd-review-route.yml": self.ROUTE})
        self.assertEqual(self.adapter().expected_workflows(head),
                         [(".github/workflows/sd-review-route.yml", "sd-review route"),
                          (".github/workflows/tëst.yml", "Tests")])

    def test_a_push_run_is_not_evidence_of_the_pull_request_validation(self):
        self.declare()
        self.green()
        self.double.workflow_runs = [self.run_record(".github/workflows/tests.yml", event="push"), self.run_record(".github/workflows/sd-review-route.yml")]
        self.refuse("workflow Tests", "ci_missing")
        self.double.workflow_runs.append(self.run_record(".github/workflows/tests.yml", event="pull_request"))
        self.merge()
        self.assertEqual(self.puts(), 1)

    def test_a_trigger_with_an_inline_comment_is_still_that_trigger(self):
        """Codex on the branch: `- pull_request # Validate PRs` used to carry the
        comment as the event name, and the Tests workflow silently stopped
        being expected. The only run at the head is the advisory one."""
        commented = self.TESTS.replace("on:\n  pull_request:\n  push:\n    branches: [main]\n",
                                       "on:\n  - pull_request # Validate PRs\n  - push\n")
        self.assertNotEqual(commented, self.TESTS)
        self.declare(workflows={"tests.yml": commented, "sd-review-route.yml": self.ROUTE})
        self.prepare()
        next(iter(self.remote.pull_requests.values())).checks = [self.check("route")]
        self.double.workflow_runs = [self.run_record(".github/workflows/sd-review-route.yml")]
        self.refuse(r"workflow Tests \(\.github/workflows/tests\.yml\) has no successful pull_request run", "ci_missing")

    def test_a_trigger_the_reader_cannot_resolve_refuses_rather_than_drops_the_workflow(self):
        odd = self.TESTS.replace("on:\n  pull_request:\n  push:\n    branches: [main]\n", "on: [pull_request, ${{ vars.EVENT }}]\n")
        self.declare(workflows={"tests.yml": odd, "sd-review-route.yml": self.ROUTE})
        self.green()
        self.refuse(r"tests\.yml at [0-9a-f]{12}: could not read its triggers \('\$\{\{ vars\.EVENT \}\}'\)", "ci_missing")

    def test_a_filtered_workflow_that_did_not_run_is_a_refusal_naming_it(self):
        """Requirement 2: a `paths`, `branches` or `types` filter under the
        trigger does not make the workflow optional. GitHub's scheduling is
        not reconstructed here; the workflow ran for the event or the merge
        names it."""
        for filtered in ("on:\n  pull_request:\n    paths: ['docs/**']\n",
                         "on:\n  pull_request:\n    branches: [main]\n",
                         "on:\n  pull_request:\n    types: [labeled]\n"):
            with self.subTest(filtered=filtered.splitlines()[2].strip()):
                self.restart()
                tests = self.TESTS.replace("on:\n  pull_request:\n  push:\n    branches: [main]\n", filtered)
                self.assertNotEqual(tests, self.TESTS)
                self.declare(workflows={"tests.yml": tests, "sd-review-route.yml": self.ROUTE})
                self.prepare()
                next(iter(self.remote.pull_requests.values())).checks = [self.check("route")]
                self.double.workflow_runs = [self.run_record(".github/workflows/sd-review-route.yml")]
                self.refuse(r"workflow Tests \(\.github/workflows/tests\.yml\) has no successful pull_request run", "ci_missing")
                self.double.workflow_runs.append(self.run_record(".github/workflows/tests.yml"))
                self.merge()
                self.assertEqual(self.puts(), 1)

    def test_a_quoted_event_key_is_the_same_key(self):
        """Post-cap review: `"pull_request":` beside an unquoted `push:` was
        dropped while its sibling parsed, so the workflow silently stopped
        being expected."""
        for spelling in ('on:\n  push:\n    branches: [main]\n  "pull_request":\n',
                         "on:\n  push:\n    branches: [main]\n  'pull_request':\n"):
            with self.subTest(spelling=spelling.splitlines()[-1].strip()):
                self.restart()
                tests = self.TESTS.replace("on:\n  pull_request:\n  push:\n    branches: [main]\n", spelling)
                self.assertNotEqual(tests, self.TESTS)
                self.declare(workflows={"tests.yml": tests, "sd-review-route.yml": self.ROUTE})
                self.prepare()
                next(iter(self.remote.pull_requests.values())).checks = [self.check("route")]
                self.double.workflow_runs = [self.run_record(".github/workflows/sd-review-route.yml")]
                self.refuse(r"workflow Tests \(\.github/workflows/tests\.yml\) has no successful pull_request run", "ci_missing")

    def test_a_space_before_the_colon_is_still_that_key(self):
        """Verification pass: `pull_request :` read as no key while `push:`
        beside it parsed, so the block looked complete."""
        self.declare(workflows={"tests.yml": self.TESTS.replace(
            "on:\n  pull_request:\n  push:\n    branches: [main]\n",
            "on:\n  push:\n    branches: [main]\n  pull_request :\n"), "sd-review-route.yml": self.ROUTE})
        self.prepare()
        next(iter(self.remote.pull_requests.values())).checks = [self.check("route")]
        self.double.workflow_runs = [self.run_record(".github/workflows/sd-review-route.yml")]
        self.refuse(r"workflow Tests \(\.github/workflows/tests\.yml\) has no successful pull_request run", "ci_missing")

    def test_a_line_in_the_trigger_block_this_cannot_read_refuses(self):
        """A sibling that parses must not make an unreadable line invisible."""
        self.declare(workflows={"tests.yml": self.TESTS.replace(
            "on:\n  pull_request:\n  push:\n    branches: [main]\n",
            "on:\n  push:\n  ?  [a, b]\n"), "sd-review-route.yml": self.ROUTE})
        self.green()
        self.refuse(r"tests\.yml at [0-9a-f]{12}: could not read its triggers \('\?  \[a, b\]'\)", "ci_missing")

    def test_the_base_advancing_before_the_put_refuses(self):
        """Under the declared gap nothing server-side keeps the branch fresh,
        so the freshness `ready` read is read again before the dispatch."""
        self.declare()
        self.green()
        seen = {"count": 0}
        double = self.double
        saved = double._route

        def route(method, path, body):
            if method == "GET" and "/compare/" in path:
                seen["count"] += 1
                if seen["count"] >= 2:
                    return 200, {"behind_by": 3, "ahead_by": 1, "status": "diverged"}
            return saved(method, path, body)
        double._route = route
        refusal = self.refuse("default branch advanced after the readiness check", "base_moved")
        # A reviewed branch takes the newer base by a merge, never a rebase
        # (sd:2034). The policy text is pinned in test_sd_ship_skill.py, so
        # a policy edit does not run this module's shards (sd:5836).
        said = f"{refusal}\n{refusal.workflow['next_action']}"
        self.assertIn("git merge origin/main", said)
        self.assertNotIn("rebase", said)

    def test_the_single_event_form_is_read(self):
        """Codex on the verification pass: `on: pull_request` returned no
        trigger and refused every merge under the declaration."""
        for spelling in ("on: pull_request\n", "on: [pull_request, push]\n"):
            with self.subTest(spelling=spelling.strip()):
                self.restart()
                tests = self.TESTS.replace("on:\n  pull_request:\n  push:\n    branches: [main]\n", spelling)
                self.declare(workflows={"tests.yml": tests, "sd-review-route.yml": self.ROUTE})
                self.prepare()
                next(iter(self.remote.pull_requests.values())).checks = [self.check("route")]
                self.double.workflow_runs = [self.run_record(".github/workflows/sd-review-route.yml")]
                self.refuse(r"workflow Tests", "ci_missing")
                self.double.workflow_runs.append(self.run_record(".github/workflows/tests.yml"))
                self.merge()
                self.assertEqual(self.puts(), 1)

    def test_an_unrelated_manual_workflow_blocks_nothing(self):
        manual = "name: Manual\n\non: workflow_dispatch\n\njobs:\n  job:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n"
        self.declare(workflows={"tests.yml": self.TESTS, "sd-review-route.yml": self.ROUTE, "manual.yml": manual})
        self.green()
        self.merge()
        self.assertEqual(self.puts(), 1)

    def test_a_push_only_workflow_is_not_expected(self):
        nightly = "name: Nightly\n\non:\n  push:\n    branches: [main]\n\njobs:\n  job:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n"
        self.declare(workflows={"tests.yml": self.TESTS, "sd-review-route.yml": self.ROUTE, "nightly.yml": nightly})
        self.green()
        self.merge()
        self.assertEqual(self.puts(), 1)

    def test_another_gaps_acceptance_does_not_authorize_this_one(self):
        entries = {"accepted_gaps": [dict(self.DECLARATION["accepted_gaps"][0], id="reviews")]}
        self.declare(entries)
        self.green()
        self.refuse("carries no `unprotected` entry", "protection_required")

    def test_a_declaration_beside_a_protection_object_is_a_mismatch(self):
        self.declare()
        self.green()
        self.remote.protection = {"enforce_admins": {"enabled": True}, "required_pull_request_reviews": {"required_approving_review_count": 0},
                                  "required_status_checks": {"strict": True, "contexts": ["check"], "checks": [{"context": "check", "app_id": 7}]}}
        self.refuse("does not match the observed state")

    def test_an_invalid_declaration_names_its_fault(self):
        # Not JSON no longer reaches the reader: the row holds an object or nothing (sd:5109).
        for label, entries, fault in (
            ("unknown key", {"accepted_gaps": [], "extra": 1}, "unknown key"),
            ("wrong state shape", {"accepted_gaps": [dict(self.DECLARATION["accepted_gaps"][0], state=[])]}, "state must be a non-empty object"),
        ):
            with self.subTest(label=label):
                self.restart()
                self.declare(entries)
                self.green()
                self.refuse(fault, "protection_required")

    def test_the_declaration_is_read_from_the_row_not_the_tree(self):
        """sd:5109: a `.github/sd-status.json` at the head accepts nothing; the row accepts at merge time."""
        files = {".github/sd-status.json": json.dumps(self.DECLARATION), ".github/workflows/tests.yml": self.TESTS,
                 ".github/workflows/sd-review-route.yml": self.ROUTE}
        head = self.commit(files)
        api = self.adapter()
        self.assertIsNone(api.declared_gap(head))
        self.assertEqual(api.declaration_faults, [f"{ship.sd_lib.ACKNOWLEDGEMENT_SOURCE} carries no `unprotected` entry "
                                                  "pinning branch_protection: false"])
        set_declaration(self.connection, str(self.operator), "status", self.DECLARATION)
        self.assertEqual(self.adapter().declared_gap(head), {"declared_gap": "unprotected", "until": "a second account with push or merge rights exists"})

    def test_an_unread_declaration_accepts_nothing(self):
        self.declare()
        unread = ship.sd_lib.DatabaseUnreadable("the workflow database did not open")
        api = self.adapter()
        with patch.object(ship.sd_lib, "repo_declaration", side_effect=unread):
            self.assertIsNone(api.declared_gap(self.head()))
        self.assertTrue(any("did not open" in fault for fault in api.declaration_faults), api.declaration_faults)

    def test_a_403_from_the_protection_endpoint_is_not_absence(self):
        self.declare()
        self.green()
        self.remote.protection = RemoteRefusal(403, "Resource not accessible by integration")
        self.refuse(r"Resource not accessible by integration \(HTTP 403\)")

    def test_protection_appearing_between_the_two_reads_refuses_as_changed(self):
        self.declare()
        self.green()
        reads = {"count": 0}
        double = self.double
        saved = double._route

        def route(method, path, body):
            if method == "GET" and path.endswith("/protection"):
                reads["count"] += 1
                if reads["count"] >= 2:
                    return 200, {"enforce_admins": {"enabled": True}, "required_pull_request_reviews": {"required_approving_review_count": 0},
                                 "required_status_checks": {"strict": True, "contexts": ["check"], "checks": [{"context": "check", "app_id": 7}]}}
            return saved(method, path, body)
        double._route = route
        self.refuse("ownership or branch protection changed before merge")

    def test_a_protection_re_read_that_cannot_finish_says_so_and_still_refuses(self):
        """A timed-out `gh` on the last read is not a changed gate (sd:2623)."""
        self.declare()
        self.green()
        gate, calls = ship.GitHub.gate, []

        def flaky(api, base, head):
            calls.append(head)
            if len(calls) >= 2:
                raise ship.Refusal("gh could not finish: timed out after 60 seconds", code="command_unavailable",
                                   boundary="runtime", state="retryable_failure")
            return gate(api, base, head)
        with patch.object(ship.GitHub, "gate", flaky):
            refusal = self.refuse("^could not re-read branch protection before merge: gh could not finish: timed out",
                                  "command_unavailable")
        self.assertEqual(len(calls), 2)
        self.assertTrue(refusal.workflow["blocker"]["retryable"])



class RawCaptureTests(unittest.TestCase):
    """A provider's raw response reaches a private file, never the ship state."""

    review = importlib.import_module("sd_ship_review")

    def report(self):
        return {"outcomes": [{"backend": "minimax", "diagnostic": {
            "kind": "invalid_response", "raw_response": {"body": "not json", "stderr": "warn"}}}]}

    def test_the_raw_response_moves_to_an_owner_only_file(self):
        capture = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory())) / "raw"
        report = self.report()
        with patch.dict(os.environ, {"SD_REVIEW_RAW_DIR": str(capture)}):
            self.review.stash_raw_responses(report, "a" * 40)
        diagnostic = report["outcomes"][0]["diagnostic"]
        self.assertNotIn("raw_response", diagnostic)
        path = pathlib.Path(diagnostic["raw_capture"])
        self.assertTrue(path.name.startswith("aaaaaaaaaaaa-minimax-"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(capture.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(path.read_text()), {"head": "a" * 40, "body": "not json", "stderr": "warn"})

    def test_without_the_directory_the_raw_response_is_dropped(self):
        report = self.report()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SD_REVIEW_RAW_DIR", None)
            self.review.stash_raw_responses(report, "a" * 40)
        self.assertEqual(report["outcomes"][0]["diagnostic"], {"kind": "invalid_response"})

    def test_an_unwritable_directory_is_named_not_raised(self):
        locked = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        report = self.report()
        with patch.dict(os.environ, {"SD_REVIEW_RAW_DIR": str(locked / "raw")}):
            self.review.stash_raw_responses(report, "a" * 40)
        diagnostic = report["outcomes"][0]["diagnostic"]
        self.assertNotIn("raw_response", diagnostic)
        self.assertTrue(diagnostic["raw_capture"].startswith("not written: "))


class NeededByRepositories(unittest.TestCase):
    """sd:3036: the `Needed-by:` warning follows the writing repository's checkout folder name."""

    def warnings(self, folder: str) -> list[str]:
        stub = SimpleNamespace(
            args=SimpleNamespace(no_item=False), repo={"path": f"/example.test/repos/{folder}"},
            authorship_start=lambda: "start", root=pathlib.Path("."), state={})
        with patch.object(ship.sd_lib, "commit_messages", return_value=[("abc1234", "subject\n\nbody")]):
            return ship.Ship.commit_warnings(stub, "head")

    def test_the_writing_repository_warns_about_a_missing_trailer(self) -> None:
        self.assertEqual(self.warnings("sd-writing"), ["commit abc1234 has no Needed-by trailer; delivery continues"])

    def test_the_renamed_pack_checkout_warns_too(self) -> None:
        for folder in ("sd-workflow", "sd-ai-command-pack"):
            with self.subTest(folder=folder):
                self.assertEqual(self.warnings(folder), ["commit abc1234 has no Needed-by trailer; delivery continues"])

    def test_an_unrelated_repository_does_not(self) -> None:
        self.assertEqual(self.warnings("elsewhere"), [])


if __name__ == "__main__":
    unittest.main()
