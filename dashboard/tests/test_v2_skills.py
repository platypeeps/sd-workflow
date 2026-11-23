"""The v2 Skills page (sd:2123).

What this slice promises: `/skills` answers under the shared policy and loads the reader and its script before `shell.js`,
and the old screen is retired (sd:3402). `/api/skills` is the catalog the old screen read,
each skill's use in the last three Monday-start weeks from `skill_use`, the rows that name a path instead of a skill, the
rows by surface and the skills outside the pack that recorded use; it sends no local path and writes nothing. The page
registers the design's Skills commands: Try, Review, Promote and Demote ask first and post the old screen's routes with the
skill's revision. Scan, Run and Adopt post /api/actions/run (sd:5886, sd:5887); Schedule is copy only and posts nothing.

`skills.js` runs under JavaScriptCore (osascript) against the stand-in page and shell `test_v2_tasks` uses, with the real
reader (`read.js`). The browser half -- the look at 1440 and 375 px, the confirm dialog -- is a manual check.
"""

from __future__ import annotations

import html
import inspect
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import metrics_screen, server, skill_intake, skills_screen, v2
from sd_dashboard.v2.pages import Read
from sd_db import skills_catalog
from sd_db.writes import record_skill_use
from support import ScreenCase
from test_v2_metrics import METRICS_JS
from test_v2_nav_split import moved
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_shell_shared import cell_grammar
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused, run_js
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "skills.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

#: Wednesday; its week starts Monday 2026-09-21, so the three weeks start 09-07, 09-14 and 09-21.
NOW = "2026-09-23T12:00:00+00:00"

#: The design's Skills commands (design source products/system/designs/pages/skills.js): id, object type, label, key, risk.
#: Try is `confirm` here, not the design's `undo`: no verb ends a trial early. Review and Demote file an item in Tasks
#: (system sd:5477), which closes there like any other, so they are `safe` and ask first.
COMMANDS = [
    ["skill.try", "skill", "Try", "t", "confirm"],
    ["skill.review", "skill", "Review", "v", "safe"],
    ["skill.promote", "skill", "Promote", "p", "safe"],
    ["skill.demote", "skill", "Demote", "d", "safe"],
    ["skill.reimport", "skill", "Re-import", "i", "safe"],
    ["skill.reimport.user", "user skill", "Re-import", "i", "safe"],
    # system sd:5779: Rollback shows the swap back to the kept copy; its approve asks first, as the intake's does.
    ["skill.rollback", "skill", "Rollback", "b", "safe"],
    ["skill.rollback.user", "user skill", "Rollback", "b", "safe"],
    # system sd:5887: Run asks for the repo, then starts claude in herdr through /api/actions/run.
    ["skill.run", "skill", "Run", "r", "confirm"],
    ["skill.schedule", "skill", "Schedule", "s", "safe"],
    ["skill.apply", "skill", "Apply", "a", "confirm"],
    ["skill.scan", "use surfaces", "Scan", "s", "safe"],
    # The Intake view (system sd:5478): Remove has Undo, which tracks the repository again.
    ["skill.intake.review", "tracked repo", "Review", "e", "safe"],
    ["skill.intake.refresh", "tracked repo", "Refresh skills", None, "confirm"],
    ["skill.intake.untrack", "tracked repo", "Remove", "m", "undo"],
    ["skill.intake.track", "untracked repo", "Track", "t", "safe"],
]

SKILL = "---\nname: {name}\ndescription: The {name} fixture.\n---\n## When to use\nFor a bounded fixture.\n"


class Pack:
    """A pack checkout: `sd-used` and `sd-idle` on the `build` path, `sd-extra` in contrib."""

    def make_pack(self):
        self.root = Path(tempfile.mkdtemp(prefix="pack-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.root)], check=False))
        for name, source in (("sd-used", "skills"), ("sd-idle", "skills"), ("sd-extra", "contrib")):
            (self.root / source / name).mkdir(parents=True)
            (self.root / source / name / "SKILL.md").write_text(SKILL.format(name=name))
        (self.root / "skills/paths.json").write_text(json.dumps({"paths": {"build": {"summary": "plan to ship", "skills": ["sd-used", "sd-idle"]}}}))
        patcher = mock.patch.object(skills_catalog, "location", return_value=(self.root, {}))
        patcher.start()
        self.addCleanup(patcher.stop)
        # The user's skills folder is a temporary HOME's: no test reads the real ~/.claude/skills (sd:3997).
        self.home = Path(tempfile.mkdtemp(prefix="home-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.home)], check=False))
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed_use(self):
        for stamp, skill, surface in (("2026-09-11T10:00:00Z", "sd-used", "claude"), ("2026-09-15T10:00:00Z", "sd-used", "claude"),
                                      ("2026-09-22T10:00:00Z", "sd-used", "claude"), ("2026-09-22T11:00:00Z", "sd-used", "codex"),
                                      ("2026-08-01T10:00:00Z", "sd-idle", "claude"), ("2026-09-22T12:00:00Z", "/Volumes/example/Models", "claude"),
                                      ("2026-09-16T12:00:00Z", "outside-skill", "claude"), ("2026-09-17T12:00:00Z", "outside-skill", "claude")):
            record_skill_use(self.connection, skill, surface=surface, mode="direct", timestamp=stamp)


class TheDocument(Pack, ScreenCase):
    def setUp(self):
        super().setUp()
        self.make_pack()
        self.seed_use()

    def test_use_is_counted_per_monday_week_and_by_what_it_names(self):
        doc = skills_screen.document(self.connection, now=NOW)
        self.assertEqual(doc["weeks"], ["2026-09-07", "2026-09-14", "2026-09-21"])
        by = {s["name"]: s for s in doc["skills"]}
        self.assertEqual(by["sd-used"]["weeks"], [1, 1, 2])
        # A use before the first week counts toward nothing drawn, and the last use still says when it was.
        self.assertEqual((by["sd-idle"]["weeks"], by["sd-idle"]["last"]), ([0, 0, 0], "2026-08-01T10:00:00+00:00"))
        self.assertEqual(doc["totals"], [1, 3, 3])
        self.assertEqual(doc["junk"], 1)
        self.assertEqual(doc["other"], [["outside-skill", 2]])
        self.assertEqual(doc["surfaces"], {"claude": 7, "codex": 1})
        self.assertEqual(doc["use"], {"first": "2026-08-01T10:00:00+00:00", "last": "2026-09-22T12:00:00+00:00", "rows": 8})
        self.assertEqual(by["sd-used"]["modes"], {"direct": 4})
        # sd:3446: the rail's badge, the idle path skill and the use-record fault, from the read every page's rail runs.
        self.assertEqual(doc["attention"], {"state": "caution", "n": 2, "what": "unused path skills and use-record faults"})
        self.assertEqual({s["name"]: s["status"] for s in doc["skills"]}, {"sd-used": "path", "sd-idle": "path", "sd-extra": "contrib"})
        self.assertEqual(doc["paths"], {"build": "plan to ship"})

    def test_no_field_holds_a_local_path_or_a_file_digest(self):
        text = json.dumps(skills_screen.document(self.connection, now=NOW))
        self.assertNotIn(str(self.root), text)
        self.assertNotIn('"files"', text)
        self.assertNotIn('"root"', text)
        self.assertIn('"source": "skills/sd-used/SKILL.md"', text)

    def test_the_revision_is_the_one_the_write_routes_check(self):
        doc = skills_screen.document(self.connection, now=NOW)
        catalog = {s["name"]: s["revision"] for s in skills_catalog.catalog(self.connection, now=NOW)["skills"]}
        self.assertEqual({s["name"]: s["revision"] for s in doc["skills"]}, catalog)

    def test_a_skill_installed_by_intake_names_its_source(self):
        """sd:3992: the Re-import control is on for a skill whose trailing Provenance holds the intake's line, and off for the rest."""
        line = f"Adopted by `sd skill intake` on 2026-10-13 from `{URL}` at revision `{'c' * 40} (sha256:{'d' * 16})`."
        (self.root / "contrib/sd-extra/SKILL.md").write_text(SKILL.format(name="sd-extra") + f"\n## Provenance\n\n{line}\n")
        # sd-skill-adopt's own line, and the intake's line planted in the body above Provenance, name no intake source.
        (self.root / "skills/sd-idle/SKILL.md").write_text(SKILL.format(name="sd-idle") + f"\n{line}\n\n## Provenance\n\n"
                                                           "Adopted by `sd-skill-adopt` on 2026-10-13 from `/tmp/x` at revision `abc`.\n")
        by = {s["name"]: s["imported"] for s in skills_screen.document(self.connection, now=NOW)["skills"]}
        self.assertEqual(by, {"sd-extra": {"url": URL, "commit": "c" * 40}, "sd-idle": None, "sd-used": None})

    def test_a_user_skill_installed_by_intake_is_listed_with_its_source(self):
        """sd:3997: a skill in ~/.claude/skills whose trailing Provenance holds the intake's line is listed for Re-import."""
        self.assertEqual(skills_screen.document(self.connection, now=NOW)["user"], [], "no user skills folder lists nothing")
        line = f"Adopted by `sd skill intake` on 2026-10-13 from `{URL}` at revision `{'c' * 40} (sha256:{'d' * 16})`."
        skills = self.home / ".claude/skills"
        for name, tail in (("pdf-tools", f"\n## Provenance\n\n{line}\n"), ("plain", ""),
                           ("by-adopt", "\n## Provenance\n\nAdopted by `sd-skill-adopt` on 2026-10-13 from `/tmp/x` at revision `abc`.\n"),
                           ("planted", f"\n{line}\n")):
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text(SKILL.format(name=name) + tail)
        # A linked folder is never the intake's: the pack's verb refuses it (sd_skill_intake.source_of).
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "SKILL.md").write_text(SKILL.format(name="linked") + f"\n## Provenance\n\n{line}\n")
        (skills / "linked").symlink_to(elsewhere)
        doc = skills_screen.document(self.connection, now=NOW)
        self.assertEqual(doc["user"], [{"name": "pdf-tools", "url": URL, "commit": "c" * 40}])
        self.assertNotIn(str(self.home), json.dumps(doc))
        # A folder that cannot be listed is not "no skill there": the read fails, and the route answers 503 with why.
        skills.chmod(0)
        self.addCleanup(skills.chmod, 0o755)
        with self.assertRaises(PermissionError):
            skills_screen.document(self.connection, now=NOW)

    def test_the_document_writes_nothing(self):
        before = self.connection.total_changes
        skills_screen.document(self.connection, now=NOW)
        self.assertEqual(self.connection.total_changes, before)


class ThePage(Pack, BrowserSession):
    def setUp(self):
        super().setUp()
        self.make_pack()

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/skills")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Skills · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "commands.js", "skills.js", "skills-usage.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_old_one_is_retired(self):
        self.assertEqual(v2.SECTIONS.get("Skills"), "/skills")
        self.assertNotIn("Skills", v2.CLASSIC)
        # sd:3402 retired the classic screen; `test_classic_retired` holds the 404.
        self.assertNotIn("Skills (classic)", v2.SCREENS)
        self.assertEqual(self.request("/classic/skills")[0], 404)

    def test_the_data_route_needs_a_session_takes_no_query_and_says_why_it_failed(self):
        self.assertEqual(self.request("/api/skills")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/skills?facet=path", headers=cookie)[0], 400)
        status, _, body = self.request("/api/skills", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(sorted(s["name"] for s in json.loads(body)["skills"]), ["sd-extra", "sd-idle", "sd-used"])
        (self.root / "skills/paths.json").write_text("{}")
        status, _, body = self.request("/api/skills", headers=cookie)
        self.assertEqual(status, 503)
        self.assertIn("paths.json", json.loads(body)["error"])

    def test_a_skill_the_page_reads_is_one_the_try_route_accepts(self):
        cookie = {"Cookie": self.cookie}
        skill = next(s for s in json.loads(self.request("/api/skills", headers=cookie)[2])["skills"] if s["name"] == "sd-extra")
        self.assertEqual(self.post("/api/skills/sd-extra/try", {"revision": skill["revision"]})[0], 200)
        again = next(s for s in json.loads(self.request("/api/skills", headers=cookie)[2])["skills"] if s["name"] == "sd-extra")
        self.assertEqual(again["status"], "trial")


URL = "https://github.com/example/skills/tree/main/sd-example"
REVISION = "0123456789abcdef"
#: What `sd skill intake --json` prints (pack bin/sd_skill_intake.py `intake`): a re-import with one warning and a diff.
REPORT = {"url": URL, "commit": "a" * 40, "scope": "pack", "skill": "sd-example", "destination": "/pack/contrib/sd-example",
          "mode": "re-import", "installed_commit": "b" * 40, "files": {"SKILL.md": "100644"},
          "risk": [{"severity": "warn", "rule": "ships-script", "file": "run.sh", "line": 0}],
          "match": [{"skill": "sd-used", "overlap": 0.12}], "value": {"instruction_lines": 9, "novelty": 0.88},
          "verdict": {"action": "install", "reason": "no refusal, enough instructions, no close match"},
          "diff": ["--- installed/SKILL.md", "+++ upstream/SKILL.md", "@@ -1 +1 @@", "-old line", "+new line"],
          "revision": REVISION, "recovered": [], "installed": False}
#: What `sd skill intake <name> --rollback --json` prints (pack bin/sd_skill_intake.py `rollback`, system sd:5779):
#: a swap back to the kept copy, and the pack skills the import informed, one of them unread.
ROLLBACK = {"skill": "sd-example", "scope": "pack", "destination": "/pack/contrib/sd-example",
            "kept": "/pack/.sd-skill-intake/kept/contrib/sd-example", "mode": "rollback", "installed_commit": "a" * 40,
            "kept_commit": "b" * 40, "diff": ["--- installed/SKILL.md", "+++ kept/SKILL.md", "@@ -1 +1 @@", "-new line", "+old line"],
            "informed": [{"skill": "sd-adapted", "path": "skills/sd-adapted/SKILL.md", "names": "github.com/example/skills",
                          "commits": ["abc1234"], "unknown": None},
                         {"skill": "sd-other", "path": "contrib/sd-other/SKILL.md", "names": "github.com/example/skills",
                          "commits": None, "unknown": "git log failed: not a git repository"}],
            "revision": REVISION, "recovered": [], "rolled_back": False}
#: A stand-in `sd`: it records its arguments, one per line, then prints INTAKE_OUT on stdout or stderr and exits INTAKE_EXIT.
STUB_SD = """#!/bin/sh
for arg in "$@"; do printf '%s\\n' "$arg"; done > "$INTAKE_ARGS"
[ -z "$INTAKE_SLEEP" ] || exec sleep "$INTAKE_SLEEP"
if [ "${INTAKE_EXIT:-0}" = 0 ]; then printf '%s' "$INTAKE_OUT"; else printf '%s\\n' "$INTAKE_OUT" >&2; fi
exit "${INTAKE_EXIT:-0}"
"""


#: A source URL that runs commands if a shell reads it unquoted: `$( )`, backticks and `;`, then a `'`.
#: Each command touches a file in `folder`. `tick=False` drops the backtick part, which the route refuses on its own;
#: `quote=False` drops the `'`, whose unbalanced quote would stop sh before the other parts run.
def hostile(folder, tick=True, quote=True):
    touch = "touch${IFS}%s/" % folder
    url = f"https://github.com/o/r/tree/main/x$({touch}p1)" + (f"`{touch}p2`" if tick else "") + f";{touch}p3"
    return url + (";'q" if quote else "")


def shell_argv(case, line, folder):
    """The arguments `sd` or `claude` gets when the operator pastes `line` into sh; nothing else may run."""
    stubs = 'sd() { printf "%s\\0" "$@"; }; claude() { printf "%s\\0" "$@"; }; '
    done = subprocess.run(["sh", "-c", stubs + line], cwd=folder, capture_output=True, text=True, timeout=10, check=False)
    case.assertEqual(sorted(os.listdir(folder)), [], f"pasting {line!r} ran a command")
    case.assertEqual(done.returncode, 0, done.stderr)
    return done.stdout.split("\0")[:-1]


def scratch(case):
    folder = Path(tempfile.mkdtemp(prefix="intake-")).resolve()
    case.addCleanup(lambda: subprocess.run(["rm", "-rf", str(folder)], check=False))
    return folder


class StubSd(Pack, BrowserSession):
    """A pack, a session, and `sd` replaced by STUB_SD."""

    #: Each request runs the stub as a real process, so the client waits as long as the route may.
    http_timeout = skill_intake.INTAKE_SECONDS + 10

    def setUp(self):
        super().setUp()
        self.make_pack()
        folder = Path(tempfile.mkdtemp(prefix="intake-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(folder)], check=False))
        self.args = folder / "args"
        stub = folder / "sd"
        stub.write_text(STUB_SD)
        stub.chmod(0o755)
        patcher = mock.patch.object(skill_intake, "command", lambda name: str(stub) if name == "sd" else name)
        patcher.start()
        self.addCleanup(patcher.stop)


class TheIntakeRoute(StubSd):
    """POST /api/skills/intake runs `sd skill intake` once, as an argv list, and installs only with --approve (sd:3992)."""

    def test_the_client_waits_as_long_as_the_route_may_run(self):
        """sd:6895: each request runs a real `sh` stub, so the 5 s quiet-machine wait timed out under load."""
        self.assertGreater(self.http_timeout, skill_intake.INTAKE_SECONDS)

    def intake(self, payload, *, out=None, code=0):
        environment = {"INTAKE_ARGS": str(self.args), "INTAKE_OUT": json.dumps(REPORT) if out is None else out, "INTAKE_EXIT": str(code)}
        with mock.patch.dict(os.environ, environment):
            status, _, body = self.post("/api/skills/intake", payload)
        argv = self.args.read_text().splitlines() if self.args.exists() else None
        if self.args.exists():
            self.args.unlink()
        return status, body, argv

    def test_reimport_runs_intake_on_the_skill_name_and_answers_its_report(self):
        status, body, argv = self.intake({"source": "sd-extra"})
        self.assertEqual(argv, ["skill", "intake", "--json", "--", "sd-extra"])
        self.assertEqual((status, body["revision"], body["verdict"]["action"]), (200, REVISION, "install"))

    def test_a_github_url_runs_with_the_scope_the_form_chose(self):
        status, _, argv = self.intake({"source": URL, "scope": "user"})
        self.assertEqual((status, argv), (200, ["skill", "intake", "--json", "--scope", "user", "--", URL]))

    def test_reimport_of_a_user_skill_runs_intake_on_its_name_with_the_user_scope(self):
        """sd:3997: a user skill's name need not start with sd-; the verb finds it in ~/.claude/skills."""
        for name in ("pdf-tools", "Notes_2.v1"):
            with self.subTest(name=name):
                status, _, argv = self.intake({"source": name, "scope": "user"})
                self.assertEqual((status, argv), (200, ["skill", "intake", "--json", "--scope", "user", "--", name]))

    def test_approve_passes_the_exact_revision_the_report_printed(self):
        status, _, argv = self.intake({"source": URL, "scope": "pack", "approve": REVISION}, out=json.dumps(dict(REPORT, installed=True)))
        self.assertEqual(status, 200)
        self.assertEqual(argv, ["skill", "intake", "--json", "--scope", "pack", "--approve", REVISION, "--", URL])

    def test_a_source_that_is_not_a_github_url_or_a_skill_name_is_refused_and_runs_nothing(self):
        for source in ("http://github.com/example/skills", "https://github.com.example.test/example/skills",
                       "https://example.test/example/skills", "https://github.com/example/sk ills", "https://github.com/a`b",
                       "https://github.com/", "--approve", "file:///etc/passwd", "git@github.com:example/skills.git",
                       "../sd-extra", "", 7, None, ".hidden", "-x", "pdf/../x", "pdf tools", "pdf$(x)", "x" * 101):
            with self.subTest(source=source):
                status, body, argv = self.intake({"source": source})
                self.assertEqual(status, 400)
                self.assertIn("https://github.com/", body["error"])
                self.assertIsNone(argv, "a refused source still ran sd")

    def test_nothing_installs_without_an_approve_that_names_a_revision(self):
        for payload in ({"source": "sd-extra"}, {"source": URL}, {"source": URL, "scope": "pack"}):
            with self.subTest(payload=payload):
                self.assertNotIn("--approve", self.intake(payload)[2])
        for payload in ({"source": URL, "scope": "pack", "approve": ""}, {"source": URL, "scope": "pack", "approve": "yes"},
                        {"source": URL, "scope": "pack", "approve": REVISION + "0"}, {"source": URL, "approve": REVISION},
                        {"source": "sd-extra", "scope": "pack", "approve": REVISION}, {"source": URL, "scope": "all"},
                        {"source": URL, "install": True}, {"revision": REVISION}):
            with self.subTest(payload=payload):
                status, _, argv = self.intake(payload)
                self.assertEqual((status, argv), (400, None))

    def test_a_refused_or_failed_run_says_what_is_known(self):
        status, body, _ = self.intake({"source": "sd-extra"}, out="sd: no installed skill sd-extra records a GitHub source; give its URL", code=1)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "Not evaluated: no installed skill sd-extra records a GitHub source; give its URL")
        stale = f"sd: approved revision {REVISION} is not this report's fedcba9876543210: the source, the target or the installed copy changed; nothing written"
        status, body, _ = self.intake({"source": URL, "scope": "pack", "approve": REVISION}, out=stale, code=1)
        self.assertEqual((status, body.get("reload")), (409, True))
        refused = "sd: a refusal blocks the install (ships-hook-config); nothing written"
        status, body, _ = self.intake({"source": URL, "scope": "pack", "approve": REVISION}, out=refused, code=1)
        self.assertEqual((status, body["error"]), (400, "Not installed: a refusal blocks the install (ships-hook-config); nothing written"))
        # An approve that failed any other way, or printed no report, may have moved files: not known, not "not installed".
        for out, code in (("sd: [Errno 28] No space left on device", 1), ("not json", 0)):
            with self.subTest(out=out):
                status, body, _ = self.intake({"source": URL, "scope": "pack", "approve": REVISION}, out=out, code=code)
                self.assertEqual((status, body["outcome"]), (502, "unknown"))
                self.assertIn("Evaluate again", body["error"])

    def test_the_run_is_sd_skill_intake_alone_and_the_screen_still_calls_no_process(self):
        """test_skill_states keeps `skills_screen` free of process calls; the one run lives here and names no git or gh."""
        source = inspect.getsource(skill_intake)
        self.assertIsNone(re.search(r'--method|git push|pr create|"gh"|"git"|shell=True', source))
        self.assertEqual(source.count("subprocess.run("), 1)
        self.assertIn('[command("sd"), "skill", "intake", "--json"', source)

    def test_a_url_with_shell_syntax_reaches_sd_as_one_literal_argument(self):
        folder = scratch(self)
        status, _, argv = self.intake({"source": hostile(folder), "scope": "pack"})
        self.assertEqual((status, argv), (400, None))
        url = hostile(folder, tick=False)
        status, _, argv = self.intake({"source": url, "scope": "pack", "approve": REVISION}, out=json.dumps(dict(REPORT, installed=True)))
        self.assertEqual((status, argv), (200, ["skill", "intake", "--json", "--scope", "pack", "--approve", REVISION, "--", url]))
        self.assertEqual(os.listdir(folder), [], "the route ran the URL through a shell")

    def test_a_run_past_its_bound_is_unknown_for_an_approve(self):
        with mock.patch.object(skill_intake, "INTAKE_SECONDS", 0.5), mock.patch.dict(os.environ, {"INTAKE_SLEEP": "5"}):
            status, body, _ = self.intake({"source": URL, "scope": "pack", "approve": REVISION})
        self.assertEqual((status, body["outcome"]), (502, "unknown"))

    def test_rollback_runs_intake_with_rollback_and_approves_only_the_printed_revision(self):
        """system sd:5779: Rollback reports first; its approve names the skill, the scope and the revision."""
        status, body, argv = self.intake({"source": "sd-extra", "rollback": True}, out=json.dumps(ROLLBACK))
        self.assertEqual((status, body["mode"]), (200, "rollback"))
        self.assertEqual(argv, ["skill", "intake", "--json", "--rollback", "--", "sd-extra"])
        status, body, argv = self.intake({"source": "sd-extra", "scope": "pack", "rollback": True, "approve": REVISION},
                                         out=json.dumps(dict(ROLLBACK, rolled_back=True)))
        self.assertEqual((status, body["rolled_back"]), (200, True))
        self.assertEqual(argv, ["skill", "intake", "--json", "--rollback", "--scope", "pack", "--approve", REVISION, "--", "sd-extra"])

    def test_a_rollback_of_a_url_or_without_a_scope_to_approve_is_refused_and_runs_nothing(self):
        for payload in ({"source": URL, "rollback": True}, {"source": "sd-extra", "rollback": "yes"},
                        {"source": "sd-extra", "rollback": True, "approve": REVISION}):
            with self.subTest(payload=payload):
                status, _, argv = self.intake(payload)
                self.assertEqual((status, argv), (400, None))

    def test_a_refused_or_failed_rollback_says_what_is_known(self):
        held = "sd: /pack/contrib/sd-extra is committed in the pack at abc1234; revert it through the lane: git revert abc1234; nothing written"
        status, body, _ = self.intake({"source": "sd-extra", "rollback": True}, out=held, code=1)
        self.assertEqual((status, body["error"]), (400, "No rollback: " + held.removeprefix("sd: ")))
        approve = {"source": "sd-extra", "scope": "pack", "rollback": True, "approve": REVISION}
        stale = f"sd: approved revision {REVISION} is not this report's fedcba9876543210: the installed or the kept copy changed; nothing written"
        status, body, _ = self.intake(approve, out=stale, code=1)
        self.assertEqual((status, body.get("reload")), (409, True))
        status, body, _ = self.intake(approve, out="sd: [Errno 28] No space left on device", code=1)
        self.assertEqual((status, body["outcome"]), (502, "unknown"))
        self.assertIn("Rollback again", body["error"])
        # The report runs the verb's recovery first, which may move files: a failure it does not name as a refusal is unknown.
        status, body, _ = self.intake({"source": "sd-extra", "rollback": True}, out="sd: [Errno 28] No space left on device", code=1)
        self.assertEqual((status, body.get("outcome")), (502, "unknown"))
        none = "sd: no kept copy of sd-extra: only an install by `sd skill intake` keeps one; nothing to roll back"
        status, body, _ = self.intake({"source": "sd-extra", "rollback": True}, out=none, code=1)
        self.assertEqual((status, body["error"]), (400, "No rollback: " + none.removeprefix("sd: ")))


REPO = "https://github.com/example/skills"
#: What `sd skill intake --tracked --json` prints (pack bin/sd_skill_intake.py `tracked`): one tracked repository.
OTHER = "https://github.com/example/other"
TRACKED = {"key": "skills.intake_repos", "seeded": False,
           "untracked": [{"url": OTHER, "skills": [{"name": "sd-other", "scope": "user", "url": f"{OTHER}/tree/main/sd-other", "commit": "d" * 40}]}],
           "repos": [{"url": REPO, "scope": "pack", "refreshed": "2026-10-20T15:00:00Z", "result": "1 new, 1 changed",
                      "skills": [{"name": "sd-example", "scope": "pack", "url": URL, "commit": "a" * 40}]}]}
#: What `sd skill intake --refresh URL --json` prints (`refresh`): one report per skill, each with its revision.
REFRESHED = {"url": REPO, "branch": "main", "commit": "c" * 40, "result": "1 changed, 1 unchanged, 1 failed", "recorded": True,
             "unrecorded": "", "refreshed": "2026-10-20T16:00:00Z", "recovered": [], "counts": {"changed": 1, "unchanged": 1, "failed": 1},
             "skills": [dict(REPORT, path="sd-example", diff_lines=1205), dict(REPORT, path="sd-same", skill="sd-same", mode="unchanged", diff=[]),
                        {"path": "sd-broken", "url": f"{REPO}/tree/main/sd-broken", "error": "sd-broken/SKILL.md is over the size bound"}]}

#: What `sd skill intake --refresh URL --apply --json` prints (pack sd:6686): the survey, then each skill it updated or left.
APPLIED = dict(REFRESHED, result="1 changed, 1 unchanged, 1 failed; 1 updated",
               applied=[{"skill": "sd-example", "scope": "pack", "url": URL, "commit": "c" * 40, "installed_commit": "a" * 40,
                         "revision": REVISION}],
               skipped=[{"skill": "sd-folded", "scope": "pack", "why": "verdict fold: into sd-example: overlap 0.9"}])


class TheTrackingRoute(StubSd):
    """GET /api/skills/intake/repos and POST /api/skills/intake/tracking run `sd skill intake` with one repository URL
    (system sd:5478). Neither installs: a refresh lists reports, and each installs only through /api/skills/intake."""

    def run_sd(self, method, path, payload=None, *, out, code=0):
        environment = {"INTAKE_ARGS": str(self.args), "INTAKE_OUT": out if isinstance(out, str) else json.dumps(out),
                       "INTAKE_EXIT": str(code)}
        with mock.patch.dict(os.environ, environment):
            if method == "GET":
                status, _, raw = self.request(path, headers={"Cookie": self.cookie})
                body = json.loads(raw)
            else:
                status, _, body = self.post(path, payload)
        argv = self.args.read_text().splitlines() if self.args.exists() else None
        if self.args.exists():
            self.args.unlink()
        return status, body, argv

    def track(self, payload, *, out=None, code=0):
        return self.run_sd("POST", "/api/skills/intake/tracking", payload,
                           out=dict(TRACKED, url=REPO) if out is None else out, code=code)

    def test_the_list_is_the_verbs_tracked_view(self):
        status, body, argv = self.run_sd("GET", "/api/skills/intake/repos", out=TRACKED)
        self.assertEqual((status, argv), (200, ["skill", "intake", "--json", "--tracked"]))
        self.assertEqual(body["repos"][0]["url"], REPO)

    def test_a_list_that_did_not_read_is_unknown_not_empty(self):
        for out, code in (("sd: the skills.intake_repos setting row does not read; set it again with --track", 1), ("not json", 0)):
            with self.subTest(out=out):
                status, body, _ = self.run_sd("GET", "/api/skills/intake/repos", out=out, code=code)
                self.assertEqual(status, 503)
                self.assertIn("were not read", body["error"])

    def test_track_untrack_and_refresh_pass_one_repository_url(self):
        cases = (({"action": "track", "url": REPO}, ["--track=" + REPO]),
                 ({"action": "track", "url": REPO, "scope": "user"}, ["--track=" + REPO, "--scope=user"]),
                 ({"action": "untrack", "url": REPO}, ["--untrack=" + REPO]),
                 ({"action": "refresh", "url": REPO}, ["--refresh=" + REPO]))
        for payload, tail in cases:
            with self.subTest(payload=payload):
                out = REFRESHED if payload["action"] == "refresh" else None
                status, body, argv = self.track(payload, out=out)
                self.assertEqual((status, argv), (200, ["skill", "intake", "--json", *tail]))
                self.assertEqual(body["url"], REPO)

    def test_a_refresh_never_passes_an_approval(self):
        status, _, argv = self.track({"action": "refresh", "url": REPO, "approve": REVISION}, out=REFRESHED)
        self.assertEqual((status, argv), (400, None))
        _, _, argv = self.track({"action": "refresh", "url": REPO}, out=REFRESHED)
        self.assertFalse(any(arg.startswith("--approve") for arg in argv))

    def test_an_applying_refresh_passes_apply_and_only_with_a_refresh(self):
        # sd:6686: Refresh is the survey plus --apply; Review is the survey alone.
        status, _, argv = self.track({"action": "refresh", "url": REPO, "apply": True}, out=REFRESHED)
        self.assertEqual((status, argv), (200, ["skill", "intake", "--json", "--refresh=" + REPO, "--apply"]))
        for payload in ({"action": "track", "url": REPO, "apply": True}, {"action": "refresh", "url": REPO, "apply": "yes"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.track(payload)[::2], (400, None))
        with mock.patch.object(skill_intake, "INTAKE_SECONDS", 0.5), mock.patch.dict(os.environ, {"INTAKE_SLEEP": "5"}):
            status, body, _ = self.track({"action": "refresh", "url": REPO, "apply": True})
        self.assertEqual((status, body.get("outcome")), (502, "unknown"))
        self.assertNotIn("Nothing was installed", body["error"])

    def test_anything_but_a_repository_url_is_refused_and_runs_nothing(self):
        for url in (URL, REPO + "/", "https://github.com/example", "http://github.com/example/skills", "https://github.com/-x/y",
                    "https://github.com/example/..", "https://github.com/example/sk ills", "https://github.com/a/b`c",
                    "https://github.com/a/b;touch x", "--untrack", "", None, 7):
            with self.subTest(url=url):
                status, body, argv = self.track({"action": "track", "url": url})
                self.assertEqual((status, argv), (400, None))
                self.assertIn("https://github.com/<owner>/<repo>", body["error"])
        for payload in ({"action": "install", "url": REPO}, {"url": REPO}, {"action": "untrack", "url": REPO, "scope": "pack"},
                        {"action": "track", "url": REPO, "scope": "all"}, {"action": "track", "url": REPO, "extra": 1}):
            with self.subTest(payload=payload):
                self.assertEqual(self.track(payload)[::2], (400, None))

    def test_a_refused_or_failed_run_says_what_is_known(self):
        status, body, _ = self.track({"action": "untrack", "url": REPO}, out=f"sd: {REPO} is not tracked; nothing written", code=1)
        self.assertEqual((status, body["error"]), (400, f"Not changed: {REPO} is not tracked; nothing written"))
        stale = "sd: the tracked repositories changed since they were read; read them again"
        status, body, _ = self.track({"action": "track", "url": REPO}, out=stale, code=1)
        self.assertEqual((status, body.get("reload")), (409, True))
        status, body, _ = self.track({"action": "refresh", "url": REPO}, out="sd: git fetch failed: no such repository", code=1)
        self.assertEqual((status, body["error"]), (400, "Not refreshed: git fetch failed: no such repository"))
        # A list write that failed any other way may have landed: unknown, and the page reads the list again.
        for out, code in (("sd: database is locked", 1), ("not json", 0)):
            with self.subTest(out=out):
                status, body, _ = self.track({"action": "untrack", "url": REPO}, out=out, code=code)
                self.assertEqual((status, body["outcome"]), (502, "unknown"))

    def test_a_refresh_past_its_bound_says_nothing_installed(self):
        with mock.patch.object(skill_intake, "INTAKE_SECONDS", 0.5), mock.patch.dict(os.environ, {"INTAKE_SLEEP": "5"}):
            status, body, _ = self.track({"action": "refresh", "url": REPO})
        self.assertEqual(status, 502)
        self.assertIn("Nothing was installed", body["error"])


SHELL_MORE = r"""
window.shell.row = () => null; window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
window.shell.setContext = () => {}; OUT.picks = []; C.pick = id => OUT.picks.push(id);
var pageQuery = document.querySelector;
document.querySelector = sel => sel === '.ledger thead' ? document.getElementById('thead') : pageQuery(sel);
"""


class TheScript(Pack, ScreenCase):
    """skills.js against the document `skills_screen` builds."""

    def setUp(self):
        super().setUp()
        self.make_pack()
        self.seed_use()
        self.doc = skills_screen.document(self.connection, now=NOW)

    def run_page(self, body, answer=None, search="", before=""):
        answer = answer or "(path, body) => path === '/api/skills' ? [200, DOC] : [200, { item: { id: 42 } }]"
        script = (STAND_IN + f"location.search = {json.dumps(search)};\nvar DOC = {json.dumps(self.doc)};\n" + MARKUP_JS
                  + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + READ_SHELL + f"\nANSWER = {answer};\n" + COMMANDS_JS + PAGE_JS
                  + "\nvar R = {};\n(async () => { try {\n" + before + "\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.attention = window.PAGE_ATTENTION; OUT.html = Object.fromEntries(Object.entries(ELS).map(([k, e]) => [k, e.html]));"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    # The list grammar from shell.list (sd:2682, as Reports in sd:2527-2529). Each click goes to the listener the page put on its holder.
    CLICK = "ELS['%s'].listeners.click[0]({ target: { closest: s => s === %s ? { dataset: %s } : null } });"
    URL = "String(OUT.urls[OUT.urls.length - 1])"

    def many(self, n):
        """The document with n path skills named sd-000 onward, none used."""
        base = next(s for s in self.doc["skills"] if s["name"] == "sd-idle")
        self.doc["skills"] = [dict(base, name=f"sd-{i:03d}") for i in range(n)]

    def test_every_column_with_an_order_sorts_through_the_shared_header(self):
        """sd:2682: the header is shell.list's; aria-sort is on the sorted th alone, and the sort is in the URL."""
        click = self.CLICK % ("rows-head", "'button[data-sort]'", "{ sort: 'n' }")
        out = self.run_page(f"R.head = ELS['rows-head'].html; {click} R.head2 = ELS['rows-head'].html; R.rows = ELS.rows.html; R.url = {self.URL};"
                            f" {click} R.rows2 = ELS.rows.html; R.url2 = {self.URL};")
        sorted_th = r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"'
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["head"]), ["n", "st", "u"])
        self.assertEqual(re.findall(sorted_th, out["R"]["head"]), [("descending", "u")])
        self.assertEqual(re.findall(sorted_th, out["R"]["head2"]), [("ascending", "n")])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["sd-extra", "sd-idle", "sd-used"])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows2"]), ["sd-used", "sd-idle", "sd-extra"])
        self.assertEqual((out["R"]["url"], out["R"]["url2"]), ("sort=n&dir=asc", "sort=n&dir=desc"))

    def test_the_pager_numbers_pages_and_keeps_page_and_size_in_the_url(self):
        self.many(60)
        out = self.run_page("R.pager = ELS.pager.html;" + self.CLICK % ("pager", "'.list-pager [data-page]'", "{ page: '3' }")
                            + f" R.pager2 = ELS.pager.html; R.rows2 = ELS.rows.html; R.url2 = {self.URL};"
                            + self.CLICK % ("pager", "'.list-pager [data-size]'", "{ size: '50' }")
                            + f" R.pager3 = ELS.pager.html; R.url3 = {self.URL};", search="?page=2&sort=n")
        self.assertIn('<span class="range">26–50 of 60</span>', out["R"]["pager"])
        self.assertEqual(re.findall(r'data-page="(\d+)"', out["R"]["pager"]), ["1", "2", "3"])
        self.assertEqual(re.findall(r'data-size="(\d+)"', out["R"]["pager"]), ["25", "50", "100", "200"])
        self.assertIn('<span class="range">51–60 of 60</span>', out["R"]["pager2"])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows2"])[0], "sd-050")
        self.assertEqual(out["R"]["url2"], "page=3&sort=n&dir=asc")
        self.assertIn('<span class="range">1–50 of 60</span>', out["R"]["pager3"])
        self.assertEqual(out["R"]["url3"], "size=50&sort=n&dir=asc")

    def test_active_filters_show_as_chips_that_remove_one_or_clear_all(self):
        out = self.run_page("R.chips = ELS.chips.html;" + self.CLICK % ("chips", "'[data-unfilter]'", "{ unfilter: 'facet' }")
                            + f" R.chips2 = ELS.chips.html; R.url2 = {self.URL};"
                            + " ELS.chips.listeners.click[0]({ target: { closest: s => s === '[data-unfilter-all]' ? {} : null } });"
                            + f" R.chips3 = ELS.chips.html; R.url3 = {self.URL}; R.rows = ELS.rows.html;", search="?facet=path&q=sd")
        self.assertEqual(re.findall(r'data-unfilter="(\w+)"', out["R"]["chips"]), ["facet", "q"])
        self.assertIn("2 filters · 2 of 3", out["R"]["chips"])
        self.assertEqual(re.findall(r'data-unfilter="(\w+)"', out["R"]["chips2"]), ["q"])
        self.assertEqual(out["R"]["url2"], "q=sd")
        self.assertEqual((out["R"]["chips3"], out["R"]["url3"]), ("", ""))
        self.assertEqual(len(re.findall(r'<tr data-id=', out["R"]["rows"])), 3)

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]); R.bulk = REG.filter(c => c.bulk).map(c => c.id);")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        self.assertEqual(out["R"]["bulk"], ["skill.try", "skill.review"])

    def test_try_asks_first_posts_the_skills_revision_and_rereads(self):
        revision = next(s["revision"] for s in self.doc["skills"] if s["name"] == "sd-extra")
        out = self.run_page("shellRun(cmd('skill.try'), C.get('sd-extra')); await flush();")
        self.assertEqual(out["confirms"], ["skill.try"])
        self.assertEqual(out["posts"], [["/api/skills/sd-extra/try", {"revision": revision}, 64]])
        self.assertEqual(out["gets"], ["/api/skills", "/api/skills"], "the page did not reread after the write")
        self.assertEqual(out["toasts"][-1], ["Trial of sd-extra starts at the next install", False])

    def test_review_and_demote_ask_post_their_routes_and_name_the_filed_item(self):
        out = self.run_page("shellRun(cmd('skill.review'), C.get('sd-used')); await flush(); shellRun(cmd('skill.demote'), C.get('sd-idle')); await flush();")
        self.assertEqual(out["confirms"], ["skill.review", "skill.demote"])
        self.assertEqual([p[0] for p in out["posts"]], ["/api/skills/sd-used/review", "/api/skills/sd-idle/demote"])
        self.assertEqual([t[0] for t in out["toasts"]], ["Review of sd-used filed in Tasks as item #42",
                                                         "Removal of sd-idle from paths filed in Tasks as item #42"])

    def test_review_promote_and_demote_say_they_file_an_item_and_never_that_it_cannot_be_withdrawn(self):
        """system sd:5477: the request files an sd item and queues no runner work, so a filed item closes in Tasks."""
        out = self.run_page("const slot = document.getElementById('form-slot'), parts = {};"
                            " slot.querySelector = s => (parts[s] = parts[s] || El(s));"
                            " C.run = (c, o) => { R.form = [c.id, c.risk, !!c.askFirst, c.does, c.consequence({ ...o, path: 'build' })]; };"
                            " cmd('skill.promote').run(C.get('sd-extra'));"
                            " parts['#pro-form'].listeners.submit[0]({ preventDefault() {} });"
                            " R.cmds = ['skill.review', 'skill.demote'].map(id => { const c = cmd(id);"
                            " return [c.id, c.risk, !!c.askFirst, c.does, c.consequence(C.get('sd-used'))]; });")
        for command, risk, asks, does, consequence in [out["R"]["form"], *out["R"]["cmds"]]:
            with self.subTest(command=command):
                self.assertEqual((risk, asks), ("safe", True))
                self.assertIn("Files", does)
                self.assertIn("Tasks", does)
                for text in (does, consequence):
                    self.assertNotRegex(text, r"(?i)withdr|queue")

    def test_a_refused_write_says_why_and_reads_again(self):
        answer = "(path) => path === '/api/skills' ? [200, DOC] : [409, { error: 'The skill changed', reload: true }]"
        out = self.run_page("shellRun(cmd('skill.try'), C.get('sd-extra')); await flush();", answer=answer)
        self.assertEqual(out["toasts"][-1], ["sd-extra not changed: The skill changed", False])
        self.assertEqual(len(out["gets"]), 2)
        self.assertEqual(out["states"][-2]["kind"], "loading", "a refused write reads again with load()")

    def test_off_commands_name_their_reason(self):
        out = self.run_page("""const w = (id, key) => cmd(id).when(C.get(key));
R.off = [w('skill.try', 'sd-used'), w('skill.promote', 'sd-used'), w('skill.demote', 'sd-extra'), w('skill.apply', 'sd-used'), w('skill.try', 'sd-extra')];""")
        self.assertEqual(out["R"]["off"], ["already installed · path", "on build already", "not on a path",
                                           "apply review notes from the review task, where the notes are", True])

    def test_schedule_stays_copy_only_and_scan_runs_on_the_server(self):
        """sd:5886: Scan posts /api/actions/run and shows its report in Details; Schedule posts nothing: sd adds no launchd job."""
        out = self.run_page("""const LABEL = 'Scan'; const was = ANSWER; ANSWER = (p, b) => p === '/api/actions/run' ? [200, { action: b.action, label: LABEL, line: 'the line', exit: 0, output: 'out', cut: false }] : was(p, b);
R.exec = ['skill.schedule', 'skill.scan'].map(id => cmd(id).executes);
shellRun(cmd('skill.schedule'), C.get('sd-used')); shellRun(cmd('skill.scan'), C.get('surfaces')); await flush(); R.details = ELS.details.html;""")
        self.assertEqual(out["R"]["exec"], [False, True])
        self.assertEqual(([p[:2] for p in out["posts"]], out["confirms"]), ([["/api/actions/run", {"action": "skill.scan"}]], []))
        self.assertIn('<section class="ran" data-obj="surfaces"', out["R"]["details"])

    def test_run_asks_for_a_repo_and_posts_the_skill_and_the_repo(self):
        """sd:5887: Run is off for a skill not installed; on a confirm it posts the skill and the picked repo's key."""
        out = self.run_page("""const was = ANSWER; ANSWER = (p, b) => p === '/api/actions/run' ? [200, { action: b.action, label: 'Run skill', line: 'the line', exit: 0, output: 'Started claude', cut: false }] : was(p, b);
R.off = cmd('skill.run').when(C.get('sd-extra')); R.cli = cmd('skill.run').cli(C.get('sd-used'), { repo: '/repos/widget' });
shellRun(cmd('skill.run'), C.get('sd-used')); FIELD_VALUES.repo = '/repos/widget'; shellRun(cmd('skill.run'), C.get('sd-used')); await flush();""")
        self.assertEqual((out["R"]["off"], out["R"]["cli"]), ("not installed · try it first", "cd '/repos/widget' && claude '/sd-used'"))
        self.assertEqual(out["toasts"][0][0], "not run: OK is off until Repo is typed")
        self.assertEqual(([p[:2] for p in out["posts"]], out["confirms"]),
                         ([["/api/actions/run", {"action": "skill.run", "skill": "sd-used", "repo": "/repos/widget"}]], ["skill.run", "skill.run"]))
        self.assertEqual(out["toasts"][-1][0], "Run skill · exit 0 · the output is in Details")

    def test_adopt_asks_first_and_posts_the_parsed_source(self):
        """sd:5887: the panel's Adopt runs sd-skill-adopt through /api/actions/run with owner/repo/path; a bad source posts nothing."""
        url = "https://github.com/o/r/tree/main/skills/x"
        out = self.run_page(self.OPEN_ADOPT % json.dumps(url) + """ const was = ANSWER;
ANSWER = (p, b) => p === '/api/actions/run' ? [200, { action: b.action, label: 'Adopt skill', line: 'l', exit: 0, output: 'o', cut: false }] : was(p, b);
ELS['ad-run'].listeners.click[0](); await flush(); ELS['ad-src'].value = 'not a source'; ELS['ad-run'].listeners.click[0](); await flush();
R.err = ELS['ad-err'].textContent;""")
        self.assertEqual(([p[:2] for p in out["posts"] if p[0] == "/api/actions/run"], out["confirms"]),
                         ([["/api/actions/run", {"action": "skill.adopt", "source": "o/r/skills/x"}]], ["skill.adopt"]))
        self.assertIn("Adopt takes owner/repo", out["R"]["err"])

    def test_the_page_draws_lamps_rows_and_the_rail_badge_from_the_document(self):
        out = self.run_page("R.sub = ELS.sub.textContent; R.tally = ELS.tally.textContent;")
        rows, lamps = out["html"]["rows"], out["html"]["annunciator"]
        # Sorted by use this week, most first; an unused path skill carries the caution glyph.
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', rows), ["sd-used", "sd-extra", "sd-idle"])
        self.assertIn('<td class="g g-caution" title="on a path, no recorded use">', rows)
        self.assertEqual(re.findall(r'data-facet="(\w+)" data-state="(\w+)"', lamps), [("path", "ok"), ("contrib", "ok"), ("unused", "caution")])
        self.assertIn('data-open="hygiene" data-state="caution"', lamps)
        self.assertIn('data-open="surfaces" data-state="ok"', lamps)
        self.assertEqual([tag for tag, _ in cell_grammar(self, lamps)], ["button", "div", "button", "div", "button", "a", "a"])
        self.assertEqual([tag for tag, _ in cell_grammar(self, lamps)], ["button", "div", "button", "div", "button", "a", "a"])
        self.assertEqual(out["R"]["sub"], "The sd pack catalog: 3 skills. 2 on paths, 0 on trial, 1 in contrib.")
        self.assertEqual(out["R"]["tally"], "3 uses this week · 1 catalog skills used")
        self.assertEqual(out["attention"], {"state": "caution", "n": 2, "what": "unused path skills and use-record faults"})
        self.assertIn("outside-skill (2)", out["html"]["foot"])

    def test_the_facets_have_no_all_chip_and_a_second_press_clears_one(self):
        """Pack sd:7084: none pressed is every skill; pressing the pressed chip again clears it."""
        press = ("document.dispatchEvent(new CustomEvent('skills:facet', { detail: 'path' })); R.one = ELS.facets.html;"
                 " document.dispatchEvent(new CustomEvent('skills:facet', { detail: 'all' })); R.none = ELS.facets.html;")
        out = self.run_page(press)
        self.assertNotIn('data-facet="all"', out["R"]["none"])
        self.assertIn('data-facet="path" aria-pressed="true"', out["R"]["one"])
        self.assertNotIn('aria-pressed="true"', out["R"]["none"])
        self.assertIn("const c = e.target.closest?.('[data-facet]'); if (c) return setFacet(filter === c.dataset.facet ? 'all' : c.dataset.facet);",
                      PAGE_JS)

    def test_a_facet_filters_the_ledger_and_the_url_keeps_it(self):
        out = self.run_page("document.dispatchEvent(new CustomEvent('skills:facet', { detail: 'unused' })); R.rows = ELS.rows.html; R.url = String(OUT.urls[OUT.urls.length - 1]);")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["sd-idle"])
        self.assertEqual(str(out["R"]["url"]), "facet=unused")
        out = self.run_page("R.rows = ELS.rows.html;", search="?facet=contrib")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["sd-extra"])

    def test_a_failed_read_clears_the_page_and_says_so(self):
        out = self.run_page("R.rows = ELS.rows.html;", answer="() => [503, { error: 'skills/paths.json has no valid path inventory' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("The skills were not read: skills/paths.json has no valid path inventory", out["states"][-1]["text"])
        self.assertIn("Not read: skills/paths.json", out["R"]["rows"])
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "skills not read"})

    def test_details_and_the_use_record_notes_are_read_not_drawn(self):
        out = self.run_page("open('sd-used'); R.used = ELS.details.html; open('hygiene'); R.hygiene = ELS.details.html; open('surfaces'); R.surfaces = ELS.details.html;")
        self.assertIn("week of 09-21, 2", out["R"]["used"])
        self.assertIn("skills/sd-used/SKILL.md", out["R"]["used"])
        self.assertIn("1 of 8", out["R"]["hygiene"])
        self.assertIn("claude 7 · codex 1", out["R"]["surfaces"])
        self.assertNotIn("3DPrinter", out["R"]["hygiene"])

    # sd:3992: Re-import runs `sd skill intake` on an imported skill, shows its report, and offers Approve only after one.
    INTAKE = ("(path, body) => path === '/api/skills' ? [200, DOC] : path === '/api/skills/intake' ? [200, Object.assign({}, %s,"
              " { installed: !!(body && body.approve) })] : [200, {}]")
    CLICK_APPROVE = "ELS.details.listeners.click.forEach(f => f({ target: { closest: s => s === '[data-approve]' ? { dataset: {} } : null } }));"

    def imported(self, report=None):
        """sd-extra installed by intake; the stand-in server's answer, with the report it gives."""
        next(s for s in self.doc["skills"] if s["name"] == "sd-extra")["imported"] = {"url": URL, "commit": "b" * 40}
        return self.INTAKE % json.dumps(report or REPORT)

    def test_reimport_is_on_only_for_a_skill_intake_installed(self):
        self.imported()
        out = self.run_page("R.off = [cmd('skill.reimport').when(C.get('sd-extra')), cmd('skill.reimport').when(C.get('sd-used'))];"
                            " R.cli = cmd('skill.reimport').cli(C.get('sd-extra'));")
        self.assertEqual(out["R"]["off"], [True, "not installed through Evaluate"])
        self.assertEqual(out["R"]["cli"], "sd skill intake 'sd-extra'")

    def test_reimport_shows_the_report_and_approve_posts_the_exact_revision(self):
        answer = self.imported()
        out = self.run_page("open('sd-extra'); R.before = ELS.details.html; shellRun(cmd('skill.reimport'), C.get('sd-extra')); await flush();"
                            " R.slot = ELS['intake-slot'].html; R.posts = OUT.posts.length; " + self.CLICK_APPROVE + " await flush();"
                            " R.after = ELS['intake-slot'].html;", answer=answer)
        self.assertNotIn("data-approve", out["R"]["before"], "Approve showed before any report")
        self.assertEqual(out["posts"][0], ["/api/skills/intake", {"source": "sd-extra"}, 64])
        slot = out["R"]["slot"]
        for shown in ("ships-script", "run.sh", "sd-used 0.12", "9 instruction lines", "novelty 0.88", "install",
                      "no refusal, enough instructions, no close match", "-old line", "+new line", "re-import"):
            self.assertIn(shown, slot)
        self.assertEqual(re.findall(r"<button[^>]*data-approve[^>]*>([^<]*)", slot), [f"Approve revision {REVISION}"])
        self.assertEqual(out["confirms"], ["skill.intake.approve"])
        self.assertEqual(out["posts"][1], ["/api/skills/intake", {"source": URL, "scope": "pack", "approve": REVISION}, 64])
        self.assertEqual(out["R"]["posts"], 1, "the report posted an approve on its own")
        self.assertNotIn("data-approve", out["R"]["after"], "Approve stayed after the install")
        self.assertEqual(out["toasts"][-1], [f"Installed sd-example at revision {REVISION}", False])

    def test_no_approve_without_a_report_that_can_install(self):
        refused = dict(REPORT, risk=[{"severity": "refuse", "rule": "ships-hook-config", "file": "hooks.json", "line": 0}],
                       verdict={"action": "decline", "reason": "refused: ships-hook-config"})
        for name, report in (("refused", refused), ("unchanged", dict(REPORT, mode="unchanged", diff=[])),
                             ("blocked", dict(REPORT, mode="blocked", diff=[]))):
            with self.subTest(report=name):
                out = self.run_page("shellRun(cmd('skill.reimport'), C.get('sd-extra')); await flush();"
                                    " R.slot = ELS['intake-slot'].html; " + self.CLICK_APPROVE + " await flush();", answer=self.imported(report))
                self.assertIn(report["verdict"]["reason"], out["R"]["slot"])
                self.assertNotIn("data-approve", out["R"]["slot"])
                self.assertEqual(len(out["posts"]), 1)
        self.imported()
        answer = "(path) => path === '/api/skills' ? [200, DOC] : [400, { error: 'Not evaluated: git fetch failed' }]"
        out = self.run_page("shellRun(cmd('skill.reimport'), C.get('sd-extra')); await flush(); R.slot = ELS['intake-slot'].html;", answer=answer)
        self.assertIn("Not evaluated: git fetch failed", out["R"]["slot"])
        self.assertNotIn("data-approve", out["R"]["slot"])

    # system sd:5779: Rollback shows its report first; Approve rollback asks, then posts the revision that report printed.
    ROLLBACK_ANSWER = ("(path, body) => path === '/api/skills' ? [200, DOC] : path === '/api/skills/intake' ? [200, Object.assign({},"
                       " body.rollback ? %s : %s, body.rollback ? { rolled_back: !!body.approve } : {})] : [200, {}]")
    CLICK_ROLLBACK = "ELS.details.listeners.click.forEach(f => f({ target: { closest: s => s === '[data-rollback]' ? { dataset: {} } : null } }));"

    def test_rollback_shows_its_report_then_asks_and_posts_the_exact_revision(self):
        self.imported()
        answer = self.ROLLBACK_ANSWER % (json.dumps(ROLLBACK), json.dumps(REPORT))
        out = self.run_page("open('sd-extra'); R.before = ELS.details.html; shellRun(cmd('skill.rollback'), C.get('sd-extra')); await flush();"
                            " R.slot = ELS['intake-slot'].html; R.posts = OUT.posts.length; " + self.CLICK_ROLLBACK + " await flush();"
                            " R.after = ELS['intake-slot'].html;", answer=answer)
        self.assertNotIn("data-rollback", out["R"]["before"], "Approve rollback showed before any report")
        self.assertEqual(out["posts"][0], ["/api/skills/intake", {"source": "sd-extra", "rollback": True}, 64])
        slot = out["R"]["slot"]
        for shown in ("Rollback report", "-new line", "+old line", "sd-adapted (skills/sd-adapted/SKILL.md) names github.com/example/skills: abc1234",
                      "unknown: git log failed: not a git repository"):
            self.assertIn(shown, slot)
        self.assertEqual(re.findall(r"<button[^>]*data-rollback[^>]*>([^<]*)", slot), [f"Approve rollback {REVISION}"])
        self.assertEqual(out["R"]["posts"], 1, "the report posted an approve on its own")
        self.assertEqual(out["confirms"], ["skill.intake.rollback"])
        self.assertEqual(out["posts"][1], ["/api/skills/intake", {"source": "sd-example", "scope": "pack", "rollback": True,
                                                                  "approve": REVISION}, 64])
        self.assertNotIn("data-rollback", out["R"]["after"], "Approve rollback stayed after the rollback")
        self.assertIn("a second Rollback undoes it", out["R"]["after"])
        self.assertEqual(out["toasts"][-1], ["Rolled back sd-example", False])

    def test_rollback_is_on_only_for_a_skill_intake_installed_and_the_evaluate_report_lists_informed_skills(self):
        answer = self.imported(dict(REPORT, informed=ROLLBACK["informed"]))
        out = self.run_page("R.off = [cmd('skill.rollback').when(C.get('sd-extra')), cmd('skill.rollback').when(C.get('sd-used'))];"
                            " R.cli = cmd('skill.rollback').cli(C.get('sd-extra'));"
                            " shellRun(cmd('skill.reimport'), C.get('sd-extra')); await flush(); R.slot = ELS['intake-slot'].html;", answer=answer)
        self.assertEqual(out["R"]["off"], [True, "not installed through Evaluate"])
        self.assertEqual(out["R"]["cli"], "sd skill intake 'sd-extra' --rollback")
        self.assertIn("sd-adapted (skills/sd-adapted/SKILL.md) names github.com/example/skills: abc1234", out["R"]["slot"])

    # The lines the operator copies quote every value (sd:3992 review): pasted into sh, each runs sd with the URL as one argument.
    OPEN_ADOPT = ("ELS.details.querySelector = s => s[0] === '#' ? document.getElementById(s.slice(1)) : El('x');"
                  " ELS['adopt-btn'].listeners.click[0](); ELS['ad-src'].value = %s; ELS['ad-scope'].value = 'user';"
                  " ELS['ad-src'].listeners.input[0]();")

    def test_the_adopt_panel_lines_quote_the_url(self):
        folder = scratch(self)
        for url in (hostile(folder, quote=False), hostile(folder)):
            self.adopt_lines(url, folder)

    def adopt_lines(self, url, folder):
        out = self.run_page(self.OPEN_ADOPT % json.dumps(url) + " R.intake = ELS['ad-intake'].textContent;"
                            " R.copy = ELS['ad-intake-copy'].dataset.copy; R.adopt = ELS['ad-cli'].textContent;")
        self.assertEqual(out["R"]["copy"], out["R"]["intake"])
        self.assertEqual(shell_argv(self, out["R"]["intake"], folder), ["skill", "intake", url, "--scope", "user"])
        self.assertEqual(shell_argv(self, out["R"]["adopt"], folder), ["-p", "/sd-skill-adopt o/r/" + url.split("/main/", 1)[1]])

    def test_the_report_line_and_the_approve_confirm_quote_the_url(self):
        for quote in (False, True):
            for name in ("approvable", "unchanged"):
                with self.subTest(report=name, quote=quote):
                    folder = scratch(self)
                    self.report_lines(name, hostile(folder, quote=quote), folder)

    def report_lines(self, name, url, folder):
        if name == "approvable":
            report, want = dict(REPORT, url=url), ["skill", "intake", url, "--scope", "pack", "--approve", REVISION]
        else:
            report, want = dict(REPORT, url=url, mode="unchanged", diff=[]), ["skill", "intake", url, "--scope", "pack"]
        body = (self.OPEN_ADOPT % json.dumps(url) + " ELS['ad-scope'].value = 'pack';"
                " ELS['adopt-form'].listeners.submit[0]({ preventDefault() {} }); await flush(); R.slot = ELS['intake-slot'].html;"
                " const run = C.run; C.run = (c, o) => { R.confirm = c.cli(o); return run(c, o); }; " + self.CLICK_APPROVE + " await flush();")
        out = self.run_page(body, answer=self.INTAKE % json.dumps(report))
        self.assertEqual(out["posts"][0], ["/api/skills/intake", {"source": url, "scope": "pack"}, 64])
        copied = [html.unescape(c) for c in re.findall(r'data-copy="([^"]*)"', out["R"]["slot"])]
        self.assertEqual([shell_argv(self, line, folder) for line in copied], [want])
        if name == "approvable":
            self.assertEqual(shell_argv(self, out["R"]["confirm"], folder), want)
        else:
            self.assertNotIn("confirm", out["R"])

    def test_the_reimport_line_quotes_the_name(self):
        folder = scratch(self)
        for name in (hostile(folder, quote=False), hostile(folder)):
            out = self.run_page("R.cli = cmd('skill.reimport').cli({ id: %s });" % json.dumps(name))
            self.assertEqual(shell_argv(self, out["R"]["cli"], folder), ["skill", "intake", name])

    def test_the_try_review_promote_and_demote_lines_quote_the_name(self):
        """sd:3997: each line the operator copies passes the skill name, and Promote's path, through shell.shq."""
        folder = scratch(self)
        for name in (hostile(folder, quote=False), hostile(folder)):
            with self.subTest(quote="'" in name):
                promote = ("const slot = document.getElementById('form-slot'), parts = {};"
                           " slot.querySelector = s => (parts[s] = parts[s] || El(s));"
                           " C.run = (c, o) => { R.promote = c.cli({ id: N, path: N }); };"
                           " cmd('skill.promote').run(C.get('sd-extra'));"
                           " parts['#pro-form'].listeners.submit[0]({ preventDefault() {} });")
                out = self.run_page(f"const N = {json.dumps(name)};"
                                    " R.lines = ['skill.try', 'skill.review', 'skill.promote', 'skill.demote'].map(id => cmd(id).cli({ id: N }));"
                                    + promote)
                self.assertEqual([shell_argv(self, line, folder) for line in out["R"]["lines"]],
                                 [["skill", "try", name], ["skill", "review", name], ["skill", "promote", name, "--path", "PATH"],
                                  ["skill", "demote", name]])
                self.assertEqual(shell_argv(self, out["R"]["promote"], folder), ["skill", "promote", name, "--path", name])

    # sd:3997: a user-scope skill sd skill intake installed from GitHub gets Re-import, with the user scope, in the Adopt panel.
    OPEN_PANEL = ("ELS.details.querySelector = s => s[0] === '#' ? document.getElementById(s.slice(1)) : El('x');"
                  " C.rowActions = id => mk`<span class=\"rowact\" data-for=\"${id}\"></span>`;")

    def test_a_user_skill_from_github_gets_reimport_with_the_user_scope(self):
        self.doc["user"] = [{"name": "pdf-tools", "url": URL, "commit": "b" * 40}]
        report = dict(REPORT, scope="user", skill="pdf-tools", destination="/home/.claude/skills/pdf-tools")
        out = self.run_page(self.OPEN_PANEL + " R.cli = cmd('skill.reimport.user').cli(C.get('user:pdf-tools'));"
                            " document.dispatchEvent(new CustomEvent('shell:open', { detail: 'user:pdf-tools' })); R.panel = ELS.details.html;"
                            " shellRun(cmd('skill.reimport.user'), C.get('user:pdf-tools')); await flush();"
                            " R.slot = ELS['intake-slot'].html; " + self.CLICK_APPROVE + " await flush();",
                            answer=self.INTAKE % json.dumps(report))
        self.assertEqual(out["R"]["cli"], "sd skill intake 'pdf-tools' --scope user")
        panel = out["R"]["panel"]
        self.assertIn('data-for="user:pdf-tools"', panel)
        self.assertIn(f"{URL} at {'b' * 12}", panel)
        self.assertEqual(out["posts"][0], ["/api/skills/intake", {"source": "pdf-tools", "scope": "user"}, 64])
        self.assertIn("pdf-tools · user · re-import", out["R"]["slot"])
        self.assertEqual(out["posts"][1], ["/api/skills/intake", {"source": URL, "scope": "user", "approve": REVISION}, 64])

    def test_no_user_skill_from_github_shows_no_list(self):
        out = self.run_page(self.OPEN_PANEL + " ELS['adopt-btn'].listeners.click[0](); R.panel = ELS.details.html;"
                            " R.objs = [...OBJ.values()].filter(o => o.type === 'user skill').length;")
        self.assertNotIn("rowact", out["R"]["panel"])
        self.assertEqual(out["R"]["objs"], 0)

    def test_the_user_reimport_line_quotes_the_name(self):
        folder = scratch(self)
        for name in (hostile(folder, quote=False), hostile(folder)):
            out = self.run_page(f"R.cli = cmd('skill.reimport.user').cli({{ name: {json.dumps(name)} }});")
            self.assertEqual(shell_argv(self, out["R"]["cli"], folder), ["skill", "intake", name, "--scope", "user"])

    # The Intake view (system sd:5478). Its stand-in server answers the list, and each tracking post with the list or a refresh.
    VIEWS = ("(path, body) => path === '/api/skills' ? [200, DOC] : path === '/api/skills/intake/repos' ? [200, %(tracked)s]"
             " : path === '/api/skills/intake/tracking' ? [200, body.action === 'refresh' ? (body.apply ? %(applied)s : %(refreshed)s) : Object.assign({}, %(tracked)s, { url: body.url },"
             " body.action === 'untrack' ? { removed: { url: body.url, scope: 'user', refreshed: null, result: null } } : {})]"
             " : path === '/api/skills/intake' ? [200, Object.assign({}, %(report)s, { installed: !!body.approve })] : [200, {}]")
    INTAKE_VIEW = "document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'intake', first: true, api: '/api/skills/intake/repos' } })); await flush();"
    TRACKING = "/api/skills/intake/tracking"

    def views(self, tracked=None):
        return self.VIEWS % {"tracked": json.dumps(tracked or TRACKED), "refreshed": json.dumps(REFRESHED), "applied": json.dumps(APPLIED), "report": json.dumps(REPORT)}

    def test_the_intake_view_reads_the_tracked_list_the_first_time_it_opens(self):
        out = self.run_page("R.before = OUT.gets.slice(); " + self.INTAKE_VIEW
                            + " R.rows = ELS['intake-rows'].html; R.untracked = ELS['intake-untracked'].html; R.tally = ELS['intake-tally'].textContent;"
                            " R.details = ELS.details.html;", answer=self.views())
        self.assertEqual(out["R"]["before"], ["/api/skills"])
        self.assertEqual(out["gets"], ["/api/skills", "/api/skills/intake/repos"])
        for shown in (REPO, "sd-example", "1 new, 1 changed", "pack · contrib/"):
            self.assertIn(shown, out["R"]["rows"])
        self.assertIn(OTHER, out["R"]["untracked"])
        # sd:5524: the Intake tab carries the count; the tally says only where an unsaved list comes from.
        self.assertEqual(out["R"]["tally"], "")
        self.assertIn(f"<h2>{REPO}</h2>", out["R"]["details"], "the view did not open on its first repository")

    def test_a_view_the_shell_showed_before_the_page_listened_still_reads_its_list(self):
        # The shell listens for DOMContentLoaded on document and the page on window, so its first shell:view came first and
        # the view stayed unread in the browser check: the page asks shell.view at start.
        out = self.run_page("R.rows = ELS['intake-rows'].html; R.count = OUT.count;", answer=self.views(),
                            before="window.shell.view = { current: () => 'intake', count: (id, n) => { if (id === 'intake') OUT.count = [id, n]; } };")
        self.assertIn("/api/skills/intake/repos", out["gets"])
        self.assertIn(REPO, out["R"]["rows"])
        self.assertEqual(out["R"]["count"], ["intake", 1])

    # sd:5964: a failed Usage read left it started, so neither the view nor Read again read it again.
    def test_a_failed_usage_read_is_read_again_by_the_view_and_by_read_again(self):
        usage = skills_screen.usage(self.connection, now=NOW)
        answer = ("(() => { let n = 0; return path => path === '/api/skills' ? [200, DOC] : path === '/api/skills/usage'"
                  f" ? (n++ < 2 ? [500, {{ error: 'database is locked' }}] : [200, {json.dumps(usage)}]) : [200, {{}}]; }})()")
        show = "document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'usage', first: %s, api: '/api/skills/usage' } })); await flush();"
        again, reads = "await window.PAGE_AGAIN.run(); await flush();", "R.reads.push(OUT.gets.filter(p => p === '/api/skills/usage').length);"
        out = self.run_page(f"R.reads = []; {again} {reads} {show % 'true'} {reads} R.failed = ELS['su-weeks'].html; {show % 'false'} {reads}"
                            f" window.shell.view.current = () => 'usage'; {again} {reads} R.weeks = ELS['su-weeks'].html; {again} {reads}",
                            answer=answer, before=(V2 / "static" / "skills-usage.js").read_text(encoding="utf-8"))
        # Read again leaves a Usage never shown alone; a failed read is read again on the next showing and on Read again.
        self.assertEqual(out["R"]["reads"], [0, 1, 2, 3, 4])
        self.assertIn("Skill use could not be read: database is locked.", out["R"]["failed"])
        self.assertIn("2026-09-21", out["R"]["weeks"])

    def test_a_repository_row_is_unknown_when_its_refresh_did_not_read_every_skill(self):
        cases = (("1 unchanged, 1 failed", "▨", "could not read every skill"), ("failed: git ls-remote exited 128", "▨", "The last refresh failed"),
                 ("1 new, 1 failed", "▲", "skills to review and could not read every skill"), ("1 new", "▲", "skills to review"), ("2 unchanged", "●", "nothing new"))
        for result, glyph, why in cases:
            with self.subTest(result=result):
                listed = dict(TRACKED, repos=[dict(TRACKED["repos"][0], result=result)])
                out = self.run_page(self.INTAKE_VIEW + " R.rows = ELS['intake-rows'].html;", answer=self.views(listed))
                cell = re.search(r'<td class="g g-\w+" title="([^"]*)">(.)', out["R"]["rows"])
                self.assertEqual(cell.group(2), glyph)
                self.assertIn(why, cell.group(1))

    def test_an_empty_list_says_why_and_a_failed_read_is_not_an_empty_one(self):
        empty = dict(TRACKED, seeded=True, repos=[], untracked=[])
        out = self.run_page(self.INTAKE_VIEW + " R.rows = ELS['intake-rows'].html;", answer=self.views(empty))
        self.assertIn("No repository is tracked.", out["R"]["rows"])
        self.assertIn("No skill was installed from a tracked repo yet", json.dumps(out["states"]))
        answer = "(path) => path === '/api/skills' ? [200, DOC] : [503, { error: 'The tracked repositories were not read: database is locked' }]"
        out = self.run_page(self.INTAKE_VIEW + " R.rows = ELS['intake-rows'].html;", answer=answer)
        self.assertIn("Not read: The tracked repositories were not read: database is locked", out["R"]["rows"])
        self.assertNotIn("No repository is tracked", out["R"]["rows"])

    SHOW_REPORT = "ELS.details.listeners.click.forEach(f => f({ target: { closest: s => s === '[data-report]' ? { dataset: { report: '0' } } : null } }));"

    def test_refresh_lists_each_skills_report_and_installs_nothing_until_approve(self):
        out = self.run_page(self.INTAKE_VIEW + f" shellRun(cmd('skill.intake.review'), C.get('repo:{REPO}')); await flush();"
                            " R.refresh = ELS['refresh-slot'].html; R.posts = OUT.posts.length; " + self.SHOW_REPORT
                            + " R.slot = ELS['intake-slot'].html; R.shown = OUT.posts.length; " + self.CLICK_APPROVE + " await flush();",
                            answer=self.views())
        self.assertEqual(out["posts"][0], [self.TRACKING, {"action": "refresh", "url": REPO}, 64])
        self.assertEqual((out["R"]["posts"], out["R"]["shown"]), (1, 1), "a refresh or Show report posted on its own")
        refresh = out["R"]["refresh"]
        for shown in ("1 changed, 1 unchanged, 1 failed", "sd-example · changed", "sd-broken · not read", "over the size bound",
                      "1 skill matches the installed copy", "nothing installed"):
            self.assertIn(shown, refresh)
        self.assertNotIn("sd-same", refresh, "an unchanged skill is a count, not a row")
        self.assertIn('<span class="g-caution" aria-hidden="true">▲</span> sd-example · changed', refresh)
        self.assertEqual(len(re.findall(r"data-report=", refresh)), 1, "a skill that was not read offers a report")
        self.assertEqual(re.findall(r"<button[^>]*data-approve[^>]*>([^<]*)", out["R"]["slot"]), [f"Approve revision {REVISION}"])
        self.assertEqual(out["confirms"], ["skill.intake.approve"])
        self.assertEqual(out["posts"][1], ["/api/skills/intake", {"source": URL, "scope": "pack", "approve": REVISION}, 64])
        self.assertIn("1200 more lines", out["R"]["slot"], "the refresh's own count of diff lines was not used")
        self.assertEqual(out["toasts"][0], [f"Reviewed {REPO}: 1 changed, 1 unchanged, 1 failed. Nothing installed", False])
        self.assertIn("/api/skills/intake/repos", out["gets"][2:], "the list was not read again for the refresh time")

    def test_a_tracked_row_and_details_offer_review_and_refresh(self):
        # sd:6686: Review is the survey (safe); Refresh installs updates (asks first). The row draws every icon command.
        out = self.run_page("C.rowActions = (id, o) => mk`<span data-for=\"${id}\" data-icons=\"${String(!!o?.icons)}\"></span>`;"
                            + self.INTAKE_VIEW + " R.rows = ELS['intake-rows'].html;"
                            " R.reg = REG.filter(c => c.on === 'tracked repo').map(c => [c.id, c.label, c.risk, !!c.icon]);",
                            answer=self.views())
        self.assertIn(f'data-for="repo:{REPO}" data-icons="true"', out["R"]["rows"])
        self.assertEqual(out["R"]["reg"], [["skill.intake.review", "Review", "safe", True], ["skill.intake.refresh", "Refresh skills", "confirm", True],
                                           ["skill.intake.untrack", "Remove", "undo", False]])

    def test_refresh_asks_naming_each_update_then_lists_each_with_its_rollback(self):
        rollback = "ELS.details.listeners.click.forEach(f => f({ target: { closest: s => s === '[data-rollback-skill]' ? { dataset: { rollbackSkill: '0' } } : null } }));"
        out = self.run_page(self.INTAKE_VIEW + f" shellRun(cmd('skill.intake.review'), C.get('repo:{REPO}')); await flush();"
                            f" R.what = cmd('skill.intake.refresh').consequence(C.get('repo:{REPO}'));"
                            f" shellRun(cmd('skill.intake.refresh'), C.get('repo:{REPO}')); await flush();"
                            " R.refresh = ELS['refresh-slot'].html; " + rollback + " await flush(); R.slot = ELS['intake-slot'].html;",
                            answer=self.views())
        self.assertIn(f"sd-example (pack) to revision {REVISION}", out["R"]["what"])
        self.assertIn("installs no new skill", out["R"]["what"])
        self.assertEqual(out["confirms"], ["skill.intake.refresh"])
        self.assertEqual(out["posts"][1], [self.TRACKING, {"action": "refresh", "url": REPO, "apply": True}, 64])
        refresh = out["R"]["refresh"]
        self.assertIn("1 updated", refresh)
        self.assertNotIn("nothing installed", refresh)
        self.assertRegex(refresh, r"sd-example · updated to c{12}.*<button[^>]*data-rollback-skill=\"0\"[^>]*>Rollback</button>")
        self.assertIn("sd-folded · left as is", refresh)
        self.assertIn("verdict fold: into sd-example", refresh)
        self.assertEqual(out["posts"][2], ["/api/skills/intake", {"source": "sd-example", "scope": "pack", "rollback": True}, 64])
        self.assertEqual(out["toasts"][1], [f"Refreshed {REPO}: 1 skill updated", False])

    def test_an_update_whose_outcome_is_not_known_says_so_not_left_as_is(self):
        # Review of sd:6686: an install that failed after its swap is unknown, never "left as is".
        unknown = dict(APPLIED, applied=[], skipped=[], result="1 changed, 1 unchanged, 1 failed; 0 updated, 1 not known",
                       unknown=[{"skill": "sd-example", "scope": "pack", "why": "outcome not known: disk full; Review shows what is installed"}])
        answer = self.views().replace(json.dumps(APPLIED), json.dumps(unknown))
        out = self.run_page(self.INTAKE_VIEW + f" shellRun(cmd('skill.intake.refresh'), C.get('repo:{REPO}')); await flush();"
                            " R.refresh = ELS['refresh-slot'].html;", answer=answer)
        refresh = out["R"]["refresh"]
        self.assertIn('<span class="g-unknown" aria-hidden="true">▨</span> sd-example · not known', refresh)
        self.assertIn("outcome not known: disk full", refresh)
        self.assertNotIn("left as is", refresh)
        self.assertEqual(out["toasts"][-1], [f"Refreshed {REPO}: 0 skills updated, 1 not known", False])

    def test_a_refused_skill_in_a_refresh_carries_the_warning_glyph(self):
        refused = dict(REPORT, path="sd-bad", skill="sd-bad", mode="new", risk=[{"severity": "refuse", "rule": "pipe-to-shell", "file": "SKILL.md", "line": 3}],
                       verdict={"action": "decline", "reason": "refused: pipe-to-shell"})
        answer = self.views().replace(json.dumps(REFRESHED), json.dumps(dict(REFRESHED, skills=[refused])))
        out = self.run_page(self.INTAKE_VIEW + f" shellRun(cmd('skill.intake.review'), C.get('repo:{REPO}')); await flush();"
                            " R.refresh = ELS['refresh-slot'].html;", answer=answer)
        self.assertIn('<span class="g-warning" aria-hidden="true">■</span> sd-bad · new', out["R"]["refresh"])

    def test_a_failed_refresh_says_why_and_reads_the_list_again(self):
        answer = ("(path, body) => path === '/api/skills' ? [200, DOC] : path === '/api/skills/intake/repos' ? [200, %s]"
                  " : [400, { error: 'Not refreshed: git fetch failed' }]" % json.dumps(TRACKED))
        out = self.run_page(self.INTAKE_VIEW + f" shellRun(cmd('skill.intake.review'), C.get('repo:{REPO}')); await flush();"
                            " R.refresh = ELS['refresh-slot'].html;", answer=answer)
        self.assertIn("Not refreshed: git fetch failed", out["R"]["refresh"])
        self.assertEqual(out["toasts"][-1], [f"{REPO} not changed: Not refreshed: git fetch failed", False])
        self.assertEqual(out["gets"].count("/api/skills/intake/repos"), 2)

    def test_remove_stops_tracking_and_undo_tracks_it_again_with_its_scope(self):
        out = self.run_page(self.INTAKE_VIEW + f" shellRun(cmd('skill.intake.untrack'), C.get('repo:{REPO}')); await flush();"
                            " await lastUndo().undo(); await flush();", answer=self.views())
        self.assertEqual(out["posts"], [[self.TRACKING, {"action": "untrack", "url": REPO}, 64],
                                        [self.TRACKING, {"action": "track", "url": REPO, "scope": "user"}, 64]])
        self.assertEqual(out["confirms"], [], "Remove has Undo; it does not ask first")
        self.assertIn([f"Stopped tracking {REPO}; its installed skills stay", True], out["toasts"])
        self.assertEqual(out["toasts"][-1], [f"Remove undone · {REPO}", False])

    def test_a_list_write_with_no_answer_is_not_known_and_reads_again(self):
        answer = ("(path, body) => path === '/api/skills' ? [200, DOC] : path === '/api/skills/intake/repos' ? [200, %s]"
                  " : [502, { error: 'sd skill intake gave no answer', outcome: 'unknown' }]" % json.dumps(TRACKED))
        out = self.run_page(self.INTAKE_VIEW + f" shellRun(cmd('skill.intake.track'), C.get('source:{OTHER}')); await flush();", answer=answer)
        self.assertEqual(out["posts"], [[self.TRACKING, {"action": "track", "url": OTHER}, 64]])
        self.assertEqual(out["toasts"][-1], [f"Tracking of {OTHER} not known: sd skill intake gave no answer", False])
        self.assertEqual(out["gets"].count("/api/skills/intake/repos"), 2)

    OPEN_TRACK = ("ELS.details.querySelector = s => s[0] === '#' ? document.getElementById(s.slice(1)) : El('x');"
                  " ELS['track-btn'].listeners.click[0](); ELS['tr-url'].value = %s; ELS['tr-scope'].value = 'user';"
                  " ELS['tr-url'].listeners.input[0](); ELS['track-form'].listeners.submit[0]({ preventDefault() {} }); await flush();")

    def test_the_track_form_takes_a_repository_url_alone(self):
        for url in (URL, REPO + "/", "https://github.com/example", "https://github.com/example/..", "owner/repo"):
            with self.subTest(url=url):
                out = self.run_page(self.INTAKE_VIEW + self.OPEN_TRACK % json.dumps(url) + " R.err = ELS['tr-err'].textContent;", answer=self.views())
                self.assertEqual(out["posts"], [])
                self.assertIn("https://github.com/<owner>/<repo>", out["R"]["err"])
        out = self.run_page(self.INTAKE_VIEW + self.OPEN_TRACK % json.dumps(OTHER) + " R.cli = ELS['tr-cli'].textContent;", answer=self.views())
        self.assertEqual(out["posts"], [[self.TRACKING, {"action": "track", "url": OTHER, "scope": "user"}, 64]])
        self.assertEqual(out["R"]["cli"], f"sd skill intake --track '{OTHER}' --scope 'user'")
        self.assertEqual(out["toasts"][-1], [f"Tracking {OTHER}", False])

    def test_the_tracking_lines_quote_the_url(self):
        out = self.run_page(self.INTAKE_VIEW + f" R.cli = ['skill.intake.review', 'skill.intake.refresh', 'skill.intake.untrack'].map(id => cmd(id).cli(C.get('repo:{REPO}')))"
                            f".concat([cmd('skill.intake.track').cli(C.get('source:{OTHER}'))]);", answer=self.views())
        self.assertEqual(out["R"]["cli"], [f"sd skill intake --refresh '{REPO}'", f"sd skill intake --refresh '{REPO}' --apply",
                                           f"sd skill intake --untrack '{REPO}'",
                                           f"sd skill intake --track '{OTHER}'"])

    def test_the_script_adds_no_sink_no_inline_style_and_no_reader_of_its_own(self):
        self.assertNotIn("innerHTML", PAGE_JS)
        self.assertNotRegex(PAGE_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", PAGE_JS)
        self.assertNotIn("SKILLS_DATA", PAGE_JS)
        self.assertIn("window.shell.read(", PAGE_JS)
        self.assertNotIn("++generation", PAGE_JS)
        self.assertIn("window.PAGE_LIST", PAGE_JS)


class TheUsage(Pack, ScreenCase):
    """sd:5881: Metrics' Skills view is Skills' third view, Usage, after Catalog and Intake; its read moved with it."""

    def setUp(self):
        super().setUp()
        self.seed_use()

    def test_usage_is_four_monday_weeks_and_this_weeks_top_skills(self):
        usage = skills_screen.usage(self.connection, now=NOW)
        self.assertEqual([(w["start"], w["uses"]) for w in usage["weeks"]],
                         [("2026-09-21", 3), ("2026-09-14", 3), ("2026-09-07", 1), ("2026-08-31", 0)])
        self.assertEqual(usage["top"][0], ["sd-used", 2])
        self.assertEqual((usage["since"], usage["first"], usage["error"]), ("2026-08-31", "2026-09-11", ""))
        _, route = v2.api("/api/skills/usage")
        self.assertEqual(route.read(Read(connection=self.connection, now=NOW, path="/api/skills/usage")), usage)

    def test_the_views_are_catalog_intake_and_usage_and_metrics_has_no_skills_view(self):
        views = lambda js: re.findall(r"id: '([a-z]+)'", re.search(r"window\.PAGE_VIEWS = (\[.*?\]);", js, re.S).group(1))  # noqa: E731
        self.assertEqual(views(PAGE_JS), ["catalog", "intake", "usage"])
        self.assertIn('<section id="view-usage"', (V2 / "skills.html").read_text(encoding="utf-8"))
        self.assertEqual(views(METRICS_JS), ["spend", "providers", "ci", "times", "age"])
        self.assertNotIn("view-skills", (V2 / "metrics.html").read_text(encoding="utf-8"))
        self.assertNotIn("skills", metrics_screen.document(self.connection, now=NOW))

    def test_an_old_metrics_skills_address_opens_usage_with_its_query(self):
        self.assertEqual(moved(METRICS_JS, path="/metrics", search="?row=sd-used", hash="#view=skills"),
                         [["replace", "/skills?row=sd-used#view=usage"]])
        self.assertEqual(moved(METRICS_JS, path="/metrics", hash="#view=ci"), [])

    def test_the_view_draws_the_weeks_and_links_each_top_skill_to_its_catalog_row(self):
        usage = skills_screen.usage(self.connection, now=NOW)
        script = ("""
var els = {}, listeners = {};
document.getElementById = id => (els[id] = els[id] || { id, textContent: '', replaceChildren(f) { sunk.push([id, f.parsed]); } });
document.addEventListener = (t, f) => { listeners[t] = f; };
""" + (V2 / "static" / "skills-usage.js").read_text(encoding="utf-8")
                  + f"window.skillsUsage.draw({json.dumps(usage)}, ''); window.skillsUsage.draw(null, 'database is locked');"
                  + "JSON.stringify(sunk)")
        drawn = run_js(self, script)
        out = dict(drawn[:2])
        self.assertIn("2026-09-21", out["su-weeks"])
        self.assertIn('<a href="/skills?row=sd-used#view=catalog">sd-used</a>', out["su-top"])
        # A read that failed is a row naming it, never an empty list.
        self.assertIn("Skill use could not be read: database is locked.", dict(drawn[2:])["su-weeks"])


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "skills", "Skills", "/skills", ("/api/skills", "/api/skills/intake", "/api/skills/intake/repos",
                                                               "/api/skills/intake/tracking", "/api/skills/usage")


if __name__ == "__main__":
    unittest.main()
