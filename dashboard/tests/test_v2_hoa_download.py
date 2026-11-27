"""Download from Mission on the HOA page (sd:5626).

What this slice promises: `POST /api/hoa/download {"source"}` starts `hoa_download.py` detached and answers 202; a second
POST for that source while it runs is a 409 and starts nothing. The run opens the portal named in section 2 of the
checkout's prompt in one `claude -p --chrome` with Chrome tools only and no form tool; while that session prints
`STATUS: waiting for login`, `/api/hoa`'s `downloads` part says so, and no download runs. Signed in, the checkout's own
command runs; its exit 0 is done, 3 unavailable, any other failed, each with its last line. A session that does not
sign in runs no download. A runner stopped between steps reads failed, or running while its session holds the lock.

No test calls claude, Chrome or a portal: `CLAUDE_BIN` is a stub that prints a stream-json reply, the checkout's
download scripts are stubs, and HOME is a temporary folder, so no token file is sourced.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from sd_dashboard import command_journal, hoa_download
from test_workflow_actions import BrowserSession

#: claude: records its argv, prints a stream-json reply by `mode`; `login` waits for `release` after its WAITING line.
CLAUDE = r'''import json, os, pathlib, sys, time
here = pathlib.Path(os.environ["STUB_DIR"])
(here / "claude-argv.json").write_text(json.dumps(sys.argv[1:]))
(here / "claude-token").write_text(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "none"))
mode = (here / "mode").read_text().strip() if (here / "mode").exists() else "signed-in"
def say(text):
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}), flush=True)
# pack sd:7723: claude 2.1.316 once started `-p --chrome` with no Chrome server and ToolSearch alone.
init = {"type": "system", "subtype": "init"}
if mode.startswith("chromeless"):
    init.update(tools=["ToolSearch"], mcp_servers=[])
elif mode == "chrome-no-result":
    init.update(tools=["ToolSearch"], mcp_servers=[{"name": "claude-in-chrome", "status": "connected"}])
print(json.dumps(init), flush=True)
if (here / "binary").exists():
    sys.stdout.buffer.write(b"\xff a byte that is not UTF-8\n")
    sys.stdout.flush()
if mode == "crash":
    print("boom: no browser extension", flush=True)
    sys.exit(2)
if mode in ("error-result", "no-result"):
    # A sign-in said along the way is not the session's end: only a successful result event is.
    say("RESULT: signed in")
    if mode == "error-result":
        print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""}), flush=True)
    sys.exit(0)
if mode in ("login", "login-never"):
    say("The portal shows a login page.\nSTATUS: waiting for login")
    while mode == "login-never" or not (here / "release").exists():
        time.sleep(0.05)
result = {"signed-in": "RESULT: signed in", "login": "RESULT: signed in", "chromeless-signed-in": "RESULT: signed in",
          "unavailable": "RESULT: unavailable, no browser is connected",
          "chromeless": "I would call navigate now.", "chrome-no-result": "I would call navigate now."}[mode]
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Done.\n" + result}), flush=True)
'''
#: The checkout's downloads: each counts itself in downloads.txt, prints a line, exits `code` (default 0).
MISSION = '''#!/bin/sh
echo "mission $1" >> "$STUB_DIR/downloads.txt"
code="$(cat "$STUB_DIR/code" 2>/dev/null || echo 0)"
[ -e "$STUB_DIR/binary" ] && printf '\377 a byte that is not UTF-8\n'
echo "mission export: pulled six exports"
[ "$code" = 0 ] || echo "mission export: the portal session is logged out"
exit "$code"
'''
MISSION_PROMPT = """# Pull
## Rules
- Stay on `https://wrong.example.test`.
## 2. Open the portal in your own tab
- Navigate to `https://scada.example.test`.
## 3. Exports
- `https://later.example.test`
"""


def wait(test, until, what, tries=400):
    for _ in range(tries):
        if until():
            return
        time.sleep(0.05)
    test.fail(f"timed out waiting for {what}")


class Stubs:
    """A checkout with the Mission procedure, a stub claude, and this test's own HOME and state folder."""

    def stubs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.stub = root / "stub"
        self.stub.mkdir()
        claude = root / "claude"
        claude.write_text(f"#!{sys.executable}\n" + CLAUDE)
        claude.chmod(0o755)
        self.hoa = root / "hoa"
        (self.hoa / "tools" / "mission").mkdir(parents=True)
        (self.hoa / "tools" / "mission" / "mission.sh").write_text(MISSION)
        (self.hoa / "tools" / "mission" / "export-prompt.md").write_text(MISSION_PROMPT)
        (root / "home").mkdir()
        patch = mock.patch.dict(os.environ, {"STUB_DIR": str(self.stub), "CLAUDE_BIN": str(claude), "HOME": str(root / "home"),
                                             "XDG_STATE_HOME": str(root / "state")})
        patch.start()
        self.addCleanup(patch.stop)
        # A session still waiting when a test ends is released, so no stub outlives its folder.
        self.addCleanup(lambda: (self.stub / "release").touch())

    def downloads(self):
        try:
            return (self.stub / "downloads.txt").read_text().splitlines()
        except FileNotFoundError:
            return []

    def record(self, source="mission"):
        return json.loads((hoa_download.state_dir(source) / "last.json").read_text())


class ThePortal(Stubs, unittest.TestCase):
    def setUp(self):
        self.stubs()

    def test_a_checkout_without_the_procedure_is_the_downloads_error_by_file(self):
        (self.hoa / "tools" / "mission" / "mission.sh").unlink()
        self.assertEqual(hoa_download.status("mission", self.hoa)["error"],
                         f"tools/mission/mission.sh is not in the hoa checkout {self.hoa}, so the Mission download has no procedure there")

    def test_the_page_is_the_first_link_in_section_2_of_the_sources_prompt(self):
        self.assertEqual(hoa_download.portal_url(self.hoa, "mission"), "https://scada.example.test")
        (self.hoa / "tools" / "mission" / "export-prompt.md").write_text("## 2. Open\nno address here\n## 3. https://x.example.test\n")
        with self.assertRaisesRegex(ValueError, "tools/mission/export-prompt.md names no https address in its section 2"):
            hoa_download.portal_url(self.hoa, "mission")



class TheRun(Stubs, unittest.TestCase):
    """`run` in process: the login session's end decides whether the download runs."""

    def setUp(self):
        self.stubs()

    def run_now(self, source="mission"):
        return hoa_download.run(source, self.hoa, out=StringIO(), err=StringIO())

    def test_signed_in_runs_the_checkouts_download_and_records_done(self):
        self.assertEqual(self.run_now(), 0)
        self.assertEqual(self.downloads(), ["mission export"])
        r = self.record()
        self.assertEqual((r["state"], r["exit"], r["reason"]), ("done", 0, "mission export: pulled six exports"))
        self.assertEqual(hoa_download.status("mission")["state"], "done")

    def test_the_session_has_chrome_tools_only_and_no_tool_that_types_into_a_form(self):
        self.run_now()
        argv = json.loads((self.stub / "claude-argv.json").read_text())
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertTrue(all(tool.startswith("mcp__claude-in-chrome__") for tool in allowed), allowed)
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        for tool in ("mcp__claude-in-chrome__form_input", "mcp__claude-in-chrome__javascript_tool", "Bash", "Write"):
            self.assertIn(tool, denied)
            self.assertNotIn(tool, allowed)
        self.assertEqual(argv[argv.index("--tools") + 1], "ToolSearch")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--chrome", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("navigate your tab to https://scada.example.test", argv[argv.index("-p") + 1])
        self.assertIn("Never type into the page", argv[argv.index("-p") + 1])
        self.assertEqual(self.downloads(), ["mission export"])

    def test_the_session_gets_no_setup_token(self):
        # pack sd:7750: the setup token's user:inference scope disables Claude in Chrome.
        shell = Path(os.environ["HOME"], ".config", "shell")
        shell.mkdir(parents=True)
        (shell / "env.sh").write_text("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=setup\n")
        self.run_now()
        self.assertEqual((self.stub / "claude-token").read_text(), "none")

    def test_a_session_that_does_not_sign_in_runs_no_download(self):
        (self.stub / "mode").write_text("unavailable")
        self.assertEqual(self.run_now(), 3)
        r = self.record()
        self.assertEqual((r["state"], r["reason"]), ("unavailable", "no browser is connected"))
        self.assertEqual(self.downloads(), [])
        (self.stub / "mode").write_text("crash")
        self.assertEqual(self.run_now(), 1)
        self.assertEqual((self.record()["state"], self.record()["reason"]), ("failed", "claude exited 2: boom: no browser extension"))
        self.assertEqual(self.downloads(), [])

    def test_only_a_successful_result_event_signs_in(self):
        for mode, reason in (("error-result", "claude ended with error_max_turns"),
                             ("no-result", "claude ended without a result")):
            (self.stub / "mode").write_text(mode)
            self.assertEqual(self.run_now(), 1, mode)
            self.assertEqual((self.record()["state"], self.record()["reason"]), ("failed", reason))
        self.assertEqual(self.downloads(), [])

    def test_a_reply_with_no_result_line_from_a_run_without_chrome_tools_says_they_did_not_load(self):
        """pack sd:7723: init named no Chrome tool or server and no RESULT line came back: the tools did not load."""
        (self.stub / "mode").write_text("chromeless")
        self.assertEqual(self.run_now(), 3)
        self.assertEqual((self.record()["state"], self.record()["reason"]), ("unavailable", hoa_download.CHROME_UNLOADED))
        self.assertIn("/chrome", hoa_download.CHROME_UNLOADED)
        # A Chrome server at init, or a RESULT line, leaves the reply's own outcome.
        (self.stub / "mode").write_text("chrome-no-result")
        self.assertEqual(self.run_now(), 1)
        self.assertEqual((self.record()["state"], self.record()["reason"]), ("failed", "no RESULT: line in the reply"))
        self.assertEqual(self.downloads(), [])
        (self.stub / "mode").write_text("chromeless-signed-in")
        self.assertEqual(self.run_now(), 0)
        self.assertEqual(self.downloads(), ["mission export"])

    def test_each_child_carries_an_alarm_that_outlives_a_killed_runner(self):
        started = []
        real = subprocess.Popen

        def popen(argv, **kwargs):
            started.append(argv)
            return real(argv, **kwargs)

        with mock.patch.object(hoa_download.subprocess, "Popen", popen):
            self.assertEqual(self.run_now(), 0)
        self.assertEqual([argv[:len(hoa_download.alarmed(0)) - 1] for argv in started],
                         [hoa_download.alarmed(0)[:-1]] * 2)
        self.assertEqual([int(argv[len(hoa_download.alarmed(0)) - 1]) for argv in started],
                         [hoa_download.LOGIN_SECONDS + 60, hoa_download.DOWNLOAD_SECONDS + 60])
        # The alarm ends the child by itself, with no runner left to stop it, and its lock with it.
        child = real([*hoa_download.alarmed(1), "sleep", "30"])
        self.assertEqual(child.wait(timeout=20), -signal.SIGALRM)

    def test_a_login_past_the_sessions_bound_is_stopped_and_downloads_nothing(self):
        (self.stub / "mode").write_text("login-never")
        with mock.patch.object(hoa_download, "LOGIN_SECONDS", 1):
            self.assertEqual(self.run_now(), 3)
        self.assertRegex(self.record()["reason"], "^no login within")
        self.assertEqual(self.downloads(), [])

    def test_a_byte_that_is_not_utf8_in_either_output_stops_no_run(self):
        # sd:7939 followup: one such byte in the login or the download output ended the run EXIT_ERROR.
        (self.stub / "binary").touch()
        self.assertEqual(self.run_now(), 0)
        self.assertEqual(self.downloads(), ["mission export"])
        self.assertEqual(self.record()["reason"], "mission export: pulled six exports")

    def test_the_downloads_exit_3_is_unavailable_and_any_other_failed_with_its_last_line(self):
        (self.stub / "code").write_text("3")
        self.assertEqual(self.run_now(), 3)
        self.assertEqual((self.record()["state"], self.record()["reason"]), ("unavailable", "mission export: the portal session is logged out"))
        (self.stub / "code").write_text("1")
        self.assertEqual(self.run_now(), 1)
        self.assertEqual((self.record()["state"], self.record()["reason"]),
                         ("failed", "sh tools/mission/mission.sh export exited 1: mission export: the portal session is logged out"))

    def test_a_failure_keeps_the_last_good_downloads_time(self):
        """sd:10616: a failed or interrupted run still names when the last good download finished."""
        self.assertEqual(hoa_download.status("mission")["last_done"], "")
        self.assertEqual(self.run_now(), 0)
        done = self.record()["finished"]
        self.assertEqual((self.record()["last_done"], hoa_download.status("mission")["last_done"]), (done, done))
        (self.stub / "code").write_text("1")
        self.assertEqual(self.run_now(), 1)
        self.assertEqual((self.record()["state"], self.record()["last_done"]), ("failed", done))
        self.assertEqual(hoa_download.status("mission")["last_done"], done)
        # A record from before the field: a done one is its own last good download, a failed one names none.
        path = hoa_download.state_dir("mission") / "last.json"
        path.write_text(json.dumps({"state": "done", "finished": "2026-09-01T00:00:00Z"}))
        self.assertEqual(hoa_download.status("mission")["last_done"], "2026-09-01T00:00:00Z")
        self.assertEqual(self.run_now(), 1)
        self.assertEqual(self.record()["last_done"], "2026-09-01T00:00:00Z")
        for odd in ('{"state": "failed", "last_done": 7}', "[]", "not json"):
            path.write_text(odd)
            self.assertEqual(hoa_download._last_done(hoa_download._read_record(path)), "")

    def test_a_record_left_running_by_a_stopped_runner_reads_failed(self):
        state = hoa_download.state_dir("mission")
        state.mkdir(parents=True)
        dead = subprocess.Popen(["true"])
        dead.wait()
        (state / "lock").write_text("2026-09-06T08:00:00+00:00")
        (state / "last.json").write_text(json.dumps({"state": "running", "phase": "waiting", "pid": dead.pid,
                                                     "started": "2026-09-06T08:00:00+00:00"}))
        out = hoa_download.status("mission")
        self.assertEqual((out["running"], out["state"]), (False, "failed"))
        self.assertIn("stopped without recording an outcome", out["reason"])

    def test_a_record_left_running_with_the_lock_free_reads_failed_whatever_its_pid(self):
        # Every live runner holds the lock, so a free lock ends the run even when its pid now names another process.
        state = hoa_download.state_dir("mission")
        state.mkdir(parents=True)
        (state / "lock").write_text("2026-09-06T08:00:00+00:00")
        (state / "last.json").write_text(json.dumps({"state": "running", "phase": "waiting", "pid": os.getpid(),
                                                     "started": "2026-09-06T08:00:00+00:00"}))
        out = hoa_download.status("mission")
        self.assertEqual((out["running"], out["state"]), (False, "failed"))
        self.assertIn("stopped without recording an outcome", out["reason"])

    def test_a_start_newer_than_the_record_with_the_lock_free_reads_failed_naming_the_log(self):
        self.assertEqual(self.run_now(), 0)
        (hoa_download.state_dir("mission") / "lock").write_text("2099-01-01T00:00:00+00:00")
        out = hoa_download.status("mission")
        self.assertEqual(out["state"], "failed")
        self.assertIn("ended without a record of its own; see", out["reason"])


class TheRoute(Stubs, BrowserSession):
    def setUp(self):
        super().setUp()
        self.stubs()
        self.repo(str(self.hoa))

    def read(self):
        status, _, body = self.request("/api/hoa", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        return json.loads(body)["downloads"]

    def test_the_row_says_waiting_for_login_until_the_operator_logs_in_then_the_download_runs(self):
        (self.stub / "mode").write_text("login")
        status, _, body = self.post("/api/hoa/download", {"source": "mission"})
        self.assertEqual(status, 202, body)
        self.assertEqual((body["repo"], body["command"]), (str(self.hoa), "sh tools/mission/mission.sh export"))
        wait(self, lambda: self.read()["mission"]["phase"] == "waiting", "the login wait")
        doc = self.read()
        self.assertEqual((doc["mission"]["running"], doc["mission"]["state"]), (True, "running"))
        self.assertEqual(self.downloads(), [])
        # One run per source: a second post is refused and starts nothing.
        status, _, body = self.post("/api/hoa/download", {"source": "mission"})
        self.assertEqual(status, 409, body)
        self.assertIn("A Mission download is already running", body["error"])

        (self.stub / "release").touch()
        wait(self, lambda: not self.read()["mission"]["running"], "the download to end")
        self.assertEqual(self.read()["mission"]["state"], "done")
        self.assertEqual(self.downloads(), ["mission export"])

    def test_a_runner_stopped_while_its_session_waits_holds_the_lock_until_the_session_ends(self):
        (self.stub / "mode").write_text("login")
        self.assertEqual(self.post("/api/hoa/download", {"source": "mission"})[0], 202)
        wait(self, lambda: self.read()["mission"]["phase"] == "waiting", "the login wait")
        os.kill(self.record("mission")["pid"], signal.SIGKILL)
        wait(self, lambda: "runner stopped" in self.read()["mission"]["reason"], "the stopped runner")
        self.assertTrue(self.read()["mission"]["running"])
        self.assertIn("the session ends by itself within", self.read()["mission"]["reason"])
        self.assertEqual(self.post("/api/hoa/download", {"source": "mission"})[0], 409)
        (self.stub / "release").touch()
        wait(self, lambda: not self.read()["mission"]["running"], "the session to end")
        doc = self.read()["mission"]
        self.assertEqual(doc["state"], "failed")
        self.assertIn("stopped without recording an outcome", doc["reason"])
        self.assertEqual(self.downloads(), [])

    def test_a_refused_post_starts_nothing(self):
        # Vantaca is retired (sd:7108): a post for it is refused like any unknown source.
        for payload in ({"source": "core"}, {}, {"source": "vantaca"}, {"source": "mission", "x": 1}):
            status, _, body = self.post("/api/hoa/download", payload)
            self.assertEqual(status, 400, body)
            self.assertIn("Download takes one field, source: mission.", body["error"])
        (self.hoa / "tools" / "mission" / "export-prompt.md").unlink()
        status, _, body = self.post("/api/hoa/download", {"source": "mission"})
        self.assertEqual(status, 400, body)
        self.assertIn("tools/mission/export-prompt.md is not in the hoa checkout", body["error"])
        self.assertFalse(hoa_download.state_dir("mission").exists())



class TheJournal(Stubs, unittest.TestCase):
    """The execution journal's record of a download ends with that run's own record, never another's."""

    def setUp(self):
        self.stubs()

    def test_a_download_record_ends_with_its_runs_end(self):
        settle = command_journal.SETTLE["download"]
        self.assertEqual(settle.handle({"started": "2026-09-06T08:00:00+00:00"}, {"target": "mission"}),
                         {"started": "2026-09-06T08:00:00+00:00", "source": "mission"})
        self.assertEqual(hoa_download.run("mission", self.hoa, out=StringIO(), err=StringIO()), 0)
        started = self.record("mission")["started"]
        code, said = settle.look({"started": started, "source": "mission"})
        self.assertEqual(code, 0)
        self.assertIn("mission download done", said)
        code, said = settle.look({"started": "2026-01-01T00:00:00+00:00", "source": "mission"})
        self.assertEqual(code, command_journal.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
