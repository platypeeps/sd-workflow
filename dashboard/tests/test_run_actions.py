"""Run (sd:5886, sd:5887): a button that gave a line to copy posts /api/actions/run, and the server runs a fixed command from its
registry.

What this slice promises: the page names an action and ids, never a command. Each id is read against what the server
holds -- an installed job, a configured storage folder, a mount, a port number, a tile -- and a refusal runs nothing.
A step that ran answers 200 with its exit and its output's tail, whatever the exit; the journal keeps that exit. A step
past its bound is 504 with `outcome: unknown`. Stubs on PATH stand in for launchctl, lsof, df, du, sd and gh, and a
patched `fix_session._launch` for herdr: no test reaches launchd, herdr, GitHub or the live database.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import fix_session, health_collectors, home_screen, run_actions
from sd_db import contributions, repos, upsert_repo
from sd_db.workflow import WorkflowError
from test_workflow_actions import BrowserSession

STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "{log}"
{body}
"""


class Jobs:
    """launchd as the library reads it: one installed job, `nightly-sync`."""

    def inspect(self, name):
        if name != "nightly-sync":
            raise WorkflowError("job is not supported: installed configuration is unavailable or invalid")
        return {"name": name, "label": "fixture." + name, "service": "gui/501/fixture." + name, "schedule": [],
                "state": "idle", "pid": None, "last_exit": 0, "last_signal": None, "reason": "", "_observation": name}

    def names(self):
        return ["nightly-sync"]


class Run(BrowserSession):
    #: Past the default step's own bound: a request that waited less gave up on a stubbed `sd` that the server still
    #: ran, and timed out at gate load 9-11 (sd:6648).
    http_timeout = run_actions.SECONDS + 10

    def setUp(self):
        self.backend = Jobs()
        super().setUp()
        root = Path(self.tmp.name).resolve()
        self.log, self.bin = root / "ran", root / "bin"
        self.bin.mkdir()
        self.stub("launchctl", 'echo "gui/501/fixture.nightly-sync = {"; echo "}"')
        self.stub("lsof", "exit 1")
        self.stub("df", 'echo "Filesystem Size Used Avail Capacity Mounted on"; echo "/dev/disk3s5 460Gi 300Gi 160Gi 66% $2"')
        self.stub("du", 'printf "10\\t$4/small\\n4194304\\t$4/big\\n4194314\\t$4\\n"')
        self.stub("sd", 'echo "scanned 3 sessions"')
        path = patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}"})
        path.start()
        self.addCleanup(path.stop)
        # The checkout's own scripts, stubbed: cron-jobs.sh and machine-setup.sh print what they were asked.
        self.checkout = root / "checkout"
        for script, body in (("local-cron-jobs/cron-jobs.sh", 'echo "log of $2, last $3 lines"'),
                             ("local-machine-setup/machine-setup.sh", 'echo "✓ disk"; exit 2')):
            (self.checkout / script).parent.mkdir(parents=True)
            (self.checkout / script).write_text(STUB.replace("{log}", str(self.log)).replace("{body}", body))
        checkout = patch.object(run_actions, "CHECKOUT", self.checkout)
        checkout.start()
        self.addCleanup(checkout.stop)
        self.store = root / "store"
        (self.store / "captures").mkdir(parents=True)
        storage = patch.object(health_collectors, "storage_list", lambda connection: (f"storage|{self.store}", "file", ""))
        storage.start()
        self.addCleanup(storage.stop)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(STUB.replace("{log}", str(self.log)).replace("{body}", body))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def ran(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run_(self, action, **ids):
        status, _, body = self.post("/api/actions/run", {"action": action, **ids})
        return status, body


class TheReads(Run):
    def test_each_read_runs_its_fixed_command_and_answers_its_exit_and_output(self):
        status, body = self.run_("job.log", job="nightly-sync")
        self.assertEqual((status, body["exit"], body["output"]), (200, 0, "log of nightly-sync, last 200 lines"))
        self.assertEqual(body["line"], "local-cron-jobs/cron-jobs.sh logs nightly-sync")
        status, body = self.run_("job.print", job="nightly-sync")
        self.assertEqual((status, body["line"]), (200, "launchctl print gui/501/fixture.nightly-sync"))
        status, body = self.run_("port.lsof", port=8080)
        # lsof exits 1 when nothing listens: an answer, worded, with its exit kept.
        self.assertEqual((status, body["exit"], body["output"]), (200, 1, "Nothing listens on TCP port 8080."))
        status, body = self.run_("volume.df", path=str(self.store))
        self.assertEqual((status, body["exit"]), (200, 0))
        status, body = self.run_("storage.du", path=str(self.store))
        self.assertEqual(status, 200)
        self.assertEqual([line.split()[-1] for line in body["output"].splitlines()],
                         [str(self.store), f"{self.store}/big", f"{self.store}/small"])
        self.assertIn("4.0 GiB", body["output"])
        status, body = self.run_("skill.scan")
        self.assertEqual((status, body["output"]), (200, "scanned 3 sessions"))
        status, body = self.run_("check.run", row="doc:0123456789ab")
        self.assertEqual((status, body["exit"], body["output"]), (200, 2, "✓ disk"))
        self.assertEqual(self.ran(), ["cron-jobs.sh logs nightly-sync 200", "launchctl print gui/501/fixture.nightly-sync",
                                      "lsof -nP -iTCP:8080 -sTCP:LISTEN", f"df -h {self.store}", f"du -k -d 1 {self.store}",
                                      "sd skill scan", "machine-setup.sh doctor"])

    def test_the_journal_keeps_each_runs_own_exit(self):
        self.run_("job.log", job="nightly-sync")
        self.run_("port.lsof", port=8080)
        self.assertEqual(self.journalled(), [("Run · job.log nightly-sync", 0), ("Run · port.lsof 8080", 1)])

    def test_a_refusal_runs_nothing_and_records_nothing(self):
        before = self.snapshot()
        for ids, said in (({"action": "rm"}, "Choose one of"),
                          ({"action": "job.log", "job": "nightly-sync", "argv": ["sh"]}, "job.log takes job"),
                          ({"action": "job.log", "job": "other"}, "job is not supported"),
                          ({"action": "job.print", "job": "../x"}, "is not a job name"),
                          ({"action": "port.lsof", "port": "80; rm -rf /"}, "A port is a whole number"),
                          ({"action": "port.lsof", "port": 70000}, "A port is a whole number"),
                          ({"action": "port.lsof", "port": True}, "A port is a whole number"),
                          ({"action": "storage.du", "path": "/"}, "is not a storage folder Health reads"),
                          ({"action": "storage.du", "path": f"{self.store}/captures/../../.."}, "is not a storage folder"),
                          ({"action": "volume.df", "path": f"{self.store}/captures"}, "is not a mounted volume"),
                          ({"action": "check.run", "row": "wt:ok"}, "Re-check reads Health again")):
            with self.subTest(ids=ids):
                status, _, body = self.post("/api/actions/run", ids)
                self.assertEqual(status, 400)
                self.assertIn(said, body["error"])
        self.assertEqual(self.ran(), [])
        self.assertEqual(self.snapshot(), before)

    def test_a_folder_inside_a_storage_folder_is_read_and_a_symlink_out_is_not(self):
        status, _ = self.run_("storage.du", path=str(self.store / "captures"))
        self.assertEqual(status, 200)
        (self.store / "out").symlink_to(Path(self.tmp.name) / "bin")
        status, body = self.run_("storage.du", path=str(self.store / "out"))
        self.assertEqual((status, "is not a storage folder" in body["error"]), (400, True))

    def test_dus_diagnostics_stay_beside_its_sizes(self):
        # sd:5958: a folder du could not read was dropped from the answer once any size row came back.
        self.stub("du", 'printf "10\\t$4/small\\n12\\t$4\\n"; echo "du: $4/locked: Permission denied" >&2; exit 1')
        status, body = self.run_("storage.du", path=str(self.store))
        self.assertEqual((status, body["exit"]), (200, 1))
        self.assertEqual(body["output"].splitlines()[-1], f"du: {self.store}/locked: Permission denied")
        self.assertIn(f"{self.store}/small", body["output"])

    def test_an_unread_storage_list_refuses_rather_than_guessing(self):
        with patch.object(health_collectors, "storage_list", lambda connection: (None, "missing", "the row was not read")):
            status, body = self.run_("storage.du", path=str(self.store))
        self.assertEqual(status, 400)
        self.assertIn("the row was not read", body["error"])

    def test_the_credentials_probe_runs_in_the_server_and_describes_each_probe(self):
        probes = {"probes": [{"name": "GitHub PAT", "ok": True}, {"name": "gh CLI", "ok": False}]}
        with patch("sd_db.credentials.check", lambda connection, env: probes), \
             patch("sd_db.credentials.describe", lambda probe: f"{probe['name']}: {'ok' if probe['ok'] else 'failed'}"):
            for ids in ({"action": "credentials.probe"}, {"action": "check.run", "row": "cred:stale"}):
                status, _, body = self.post("/api/actions/run", ids)
                self.assertEqual((status, body["output"], body["line"]), (200, "GitHub PAT: ok\ngh CLI: failed", "sd-db credentials"))

    def test_sign_in_starts_the_login_read_from_disk_in_herdr(self):
        """sd:6722: the probe names the login; `credentials.signin` reads it again, and an unknown one runs nothing."""
        launched = []
        with patch("sd_db.credentials.signin", lambda probe, env: "! sdm-login" if probe == "sdm" else None), \
             patch.object(fix_session, "_launch", lambda directory, label, text: launched.append((label, text)) or "p1"):
            status, body = self.run_("credentials.signin", probe="sdm")
            self.assertEqual((status, body["exit"], body["label"]), (200, 0, "Sign in"))
            status, body = self.run_("credentials.signin", probe="aws:nope")
        self.assertEqual(status, 400)
        self.assertIn("aws:nope is not a credential this machine can sign in", body["error"])
        self.assertEqual(launched, [("signin-sdm", "! sdm-login")])

    def test_google_sign_in_answers_that_accounts_consent_url_and_starts_no_pane(self):
        """sd:6957: workspace-mcp's start_google_auth for the one account; the URL rides the answer, not the output."""
        calls, launched = [], []

        class Mcp:
            session = None

            def __init__(self, url, timeout):
                pass

            def connect(self):
                pass

            def call(self, name, arguments):
                calls.append((name, arguments["user_google_email"]))
                return f"Click [here](https://accounts.google.com/o/oauth2/auth?login_hint={arguments['user_google_email']}) to sign in."

        fake = type("MailIntake", (), {"Mcp": Mcp})
        with patch("sd_db.credentials.signin", lambda probe, env: "Sign workspace-mcp in"), \
             patch("sd_dashboard.notes_screen._mail_intake", lambda: fake), \
             patch.object(fix_session, "_launch", lambda directory, label, text: launched.append(label) or "p1"):
            answers = [self.run_("credentials.signin", probe=f"google:{who}@example.test")[1] for who in "ab"]
        self.assertEqual(calls, [("start_google_auth", "a@example.test"), ("start_google_auth", "b@example.test")])
        self.assertEqual([(a["exit"], a["account"], a["url"]) for a in answers],
                         [(0, f"{who}@example.test", f"https://accounts.google.com/o/oauth2/auth?login_hint={who}@example.test")
                          for who in "ab"])
        self.assertTrue(all("https://" not in a["output"] for a in answers))
        self.assertEqual(launched, [])

    def test_aws_sso_sign_in_answers_the_device_url_and_code_and_leaves_the_login_waiting(self):
        """sd:6957: `aws sso login --no-browser --use-device-code` runs on; the answer carries what it printed."""
        bin_dir = Path(self.tmp.name) / "fakebin"
        bin_dir.mkdir()
        pid_file = Path(self.tmp.name) / "aws.pid"
        aws = bin_dir / "aws"
        aws.write_text(f"#!/bin/sh\necho $$ > {pid_file}\necho \"$@\" > {pid_file}.argv\n"
                       "printf 'Browser will not be automatically opened.\\nPlease visit the following URL:\\n\\n"
                       "https://device.sso.example.test/\\n\\nThen enter the code:\\n\\nABCD-EFGH\\n'\nexec sleep 30\n")
        aws.chmod(aws.stat().st_mode | stat.S_IEXEC)
        try:
            with patch("sd_db.credentials.signin", lambda probe, env: "! aws sso login --profile dev"), \
                 patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}), \
                 patch.object(fix_session, "_launch", lambda *a: self.fail("no herdr pane for an SSO login")):
                status, body = self.run_("credentials.signin", probe="aws:dev")
            self.assertEqual((status, body["exit"], body["url"], body["code"], body["account"]),
                             (200, 0, "https://device.sso.example.test/", "ABCD-EFGH", "AWS profile dev"))
            self.assertEqual(Path(f"{pid_file}.argv").read_text().split(),
                             ["sso", "login", "--profile", "dev", "--no-browser", "--use-device-code"])
            os.kill(int(pid_file.read_text()), 0)  # still waiting for the code
        finally:
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), 9)
                except ProcessLookupError:
                    pass

    def test_a_credential_rows_re_check_runs_its_own_probe_only(self):
        seen = []
        with patch("sd_db.credentials.check", lambda connection, env, **only: seen.append(only) or {"probes": []}):
            for row in ("cred:google:a@example.test", "cred:stale"):
                status, _, _ = self.post("/api/actions/run", {"action": "check.run", "row": row})
                self.assertEqual(status, 200)
        self.assertEqual(seen, [{"only": "google:a@example.test"}, {}])

    def test_read_state_reads_only_a_tile_in_the_tile_list(self):
        tiles = "tile|Water|binary_sensor.water_leak|Leak\n"
        with patch.object(home_screen, "_current", lambda connection, config: ("file", tiles)), \
             patch.object(home_screen, "_settings", lambda environ: ("http://192.0.2.10:8123", "token")), \
             patch.object(home_screen, "_reread", lambda base, token, entity, timeout: ({"value": "off", "changed": "x"}, "")):
            status, body = self.run_("sensor.read", entity_id="binary_sensor.water_leak")
            self.assertEqual((status, body["exit"]), (200, 0))
            self.assertIn('"value": "off"', body["output"])
            self.assertNotIn("token", body["output"])
            status, body = self.run_("sensor.read", entity_id="lock.front_door")
            self.assertEqual(status, 400)
            self.assertIn("is not a tile in the tile list", body["error"])


class TheBounds(Run):
    def test_a_request_outwaits_the_default_steps_bound(self):
        """sd:6648: a 5 s request gave up while the server still held a 30 s step, so the test, not the step, ended."""
        self.assertGreater(self.http_timeout, run_actions.SECONDS)

    def test_a_step_past_its_bound_is_504_and_its_outcome_unknown(self):
        self.stub("slow", "sleep 5")
        status, body = run_actions.run("job.log", run_actions.Step(line="slow", argv=("slow",), seconds=1))
        self.assertEqual((status, body["outcome"]), (504, "unknown"))

    def test_a_long_output_keeps_its_tail(self):
        self.stub("loud", f"head -c {run_actions.OUTPUT_CHARS + 100} /dev/zero | tr '\\0' a; echo; echo the end")
        status, body = run_actions.run("job.log", run_actions.Step(line="loud", argv=("loud",)))
        self.assertEqual((status, body["cut"], len(body["output"])), (200, True, run_actions.OUTPUT_CHARS))
        self.assertTrue(body["output"].endswith("the end"))

    def test_a_step_finds_cargo_in_keg_only_rustup(self):
        """sd:8343: launchd's PATH lacks Homebrew's keg-only rustup, so a Studio Check's `make` found no cargo."""
        keg = Path(self.tmp.name).resolve() / "keg"
        keg.mkdir()
        (keg / "cargo").write_text("#!/bin/sh\necho cargo ran\n")
        (keg / "cargo").chmod(0o755)
        self.stub("make", "cargo")
        with patch.object(run_actions, "TOOL_DIRS", (str(keg),)):
            status, body = run_actions.run("job.log", run_actions.Step(line="make", argv=("make",)))
        self.assertEqual((status, body["exit"], body["output"]), (200, 0, "cargo ran"))

    def test_a_program_that_does_not_start_is_503_and_ran_nothing(self):
        status, body = run_actions.run("job.log", run_actions.Step(line="gone", argv=(str(self.bin / "gone"),)))
        self.assertEqual(status, 503)
        self.assertIn("did not start", body["error"])


class TheWrites(Run):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name).resolve()
        self.home = root / "home"
        (self.home / ".claude" / "skills" / "demo").mkdir(parents=True)
        (self.home / ".claude" / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n")
        self.repo_dir = root / "widget"
        self.repo_dir.mkdir()
        upsert_repo(self.connection, str(self.repo_dir), remote="git@example.invalid:example/widget.git")
        self.connection.commit()
        self.stub("gh", f'cat > "{root}/posted"')
        env = patch.dict(os.environ, {"HOME": str(self.home), "GH_BIN": str(self.bin / "gh")})
        env.start()
        self.addCleanup(env.stop)
        self.launched = []
        launch = patch.object(fix_session, "_launch", lambda *a: self.launched.append(a) or "w1:p1")
        launch.start()
        self.addCleanup(launch.stop)

    def refused(self, cases):
        before = self.snapshot()
        for ids, code, said in cases:
            with self.subTest(ids=ids):
                status, _, body = self.post("/api/actions/run", ids)
                self.assertEqual(status, code)
                self.assertIn(said, body["error"])
        self.assertEqual((self.ran(), self.launched), ([], []))
        self.assertEqual(self.snapshot(), before)

    def test_show_assignment_runs_sd_assignments_get_for_a_known_assignment(self):
        """sd:6682: the page names the assignment by its number; the server runs the line for a row it holds."""
        n = self.assignment(self.item("Port the page"))
        self.connection.commit()
        status, body = self.run_("assignment.get", assignment=n)
        self.assertEqual((status, body["exit"], body["line"]), (200, 0, f"sd assignments get {n}"), body)
        self.assertEqual(self.ran(), [f"sd assignments get {n}"])

    def test_show_assignment_refuses_what_is_not_an_assignment_number(self):
        n = self.assignment(self.item("Port the page"))
        self.connection.commit()
        self.refused([({"action": "assignment.get", "assignment": str(n)}, 400, "by its number"),
                      ({"action": "assignment.get", "assignment": True}, 400, "by its number"),
                      ({"action": "assignment.get", "assignment": 0}, 400, "by its number"),
                      ({"action": "assignment.get", "assignment": n + 99}, 400, "is not an assignment"),
                      ({"action": "assignment.get"}, 400, "takes assignment")])

    def test_capture_idea_runs_sd_store_add_with_the_idea_as_one_argument(self):
        status, body = self.run_("idea.capture", text="Gates; and $(why) they wait\n")
        self.assertEqual((status, body["exit"]), (200, 0))
        self.assertEqual(self.ran(), ["sd store add sdw.blog-idea Gates; and $(why) they wait --field status=inbox"])
        self.assertEqual(self.journalled()[-1], ("Run · idea.capture", 0))

    def test_capture_idea_refuses_what_sd_store_would_misread(self):
        self.refused([({"action": "idea.capture", "text": "--force"}, 400, "not a dash"),
                      ({"action": "idea.capture", "text": 'say "hi"'}, 400, "double quote"),
                      ({"action": "idea.capture", "text": "  "}, 400, "empty"),
                      ({"action": "idea.capture", "text": ["a"]}, 400, "text")])

    def declared(self):
        return repos.declarations(self.connection, str(self.repo_dir)).get("review")

    def test_write_declaration_writes_at_the_shown_reading_and_undo_removes_it(self):
        status, body = self.run_("declaration.write", path=str(self.repo_dir), kind="review",
                                 value={"severity_floor": "high"}, before="")
        self.assertEqual((status, body["exit"]), (200, 0), body)
        self.assertEqual(self.declared(), {"severity_floor": "high"})
        self.assertIn("sd-db repo declare", body["line"])
        status, body = self.run_("declaration.write", path=str(self.repo_dir), kind="review", value=None,
                                 before=json.dumps({"severity_floor": "high"}))
        self.assertEqual((status, body["exit"]), (200, 0), body)
        self.assertIsNone(self.declared())

    def test_write_declaration_refuses_a_stale_reading_a_bad_value_and_an_unknown_repo(self):
        repos.set_declaration(self.connection, str(self.repo_dir), "review", {"severity_floor": "low"})
        self.connection.commit()
        shown = json.dumps({"severity_floor": "low"})
        write = {"action": "declaration.write", "path": str(self.repo_dir), "kind": "review"}
        self.refused([({**write, "value": {"severity_floor": "high"}, "before": ""}, 409, "changed since the page read it"),
                      ({**write, "value": {"severity_floor": "extreme"}, "before": shown}, 400, "severity_floor must be one of"),
                      ({**write, "value": "high", "before": shown}, 400, "JSON object"),
                      ({**write, "kind": "lane", "value": {}, "before": shown}, 400, "review declaration only"),
                      ({**write, "path": "/repos/elsewhere", "value": {}, "before": ""}, 400, "not a registered repository")])
        self.assertEqual(self.declared(), {"severity_floor": "low"})

    def test_run_starts_the_skill_in_herdr_in_a_registered_checkout(self):
        status, body = self.run_("skill.run", skill="demo", repo=str(self.repo_dir))
        self.assertEqual((status, body["exit"]), (200, 0))
        self.assertEqual(self.launched, [(self.repo_dir, "skill-demo", "/demo")])
        self.assertIn("w1:p1", body["output"])

    def test_run_and_adopt_refuse_an_unknown_skill_repo_or_source(self):
        self.refused([({"action": "skill.run", "skill": "nope", "repo": str(self.repo_dir)}, 400, "not an installed skill"),
                      ({"action": "skill.run", "skill": "../demo", "repo": str(self.repo_dir)}, 400, "not an installed skill"),
                      ({"action": "skill.run", "skill": "demo", "repo": str(self.home)}, 400, "not a registered repository"),
                      ({"action": "skill.adopt", "source": "o/r; rm -rf ~"}, 400, "Adopt takes owner/repo"),
                      ({"action": "skill.adopt", "source": "https://example.test/o/r"}, 400, "Adopt takes owner/repo")])

    def test_adopt_starts_sd_skill_adopt_in_the_pack_and_a_herdr_refusal_is_its_exit(self):
        from sd_dashboard import pack_library

        status, body = self.run_("skill.adopt", source="https://github.com/o/r/tree/main/skills/x")
        self.assertEqual((status, body["exit"]), (200, 0))
        self.assertEqual(self.launched, [(pack_library().parent, "skill-adopt", "/sd-skill-adopt o/r/skills/x")])
        with patch.object(fix_session, "_launch", lambda *a: (503, {"error": "Fix failed: herdr is not running"})):
            status, body = self.run_("skill.adopt", source="o/r")
        self.assertEqual((status, body["exit"], body["output"]), (200, 1, "herdr is not running"))

    def observed(self, url):
        contributions.observe_pull(self.connection, url, {"complete": True, "observed_at": "2026-09-09T12:00:00Z",
            "operator": {"id": "1", "login": "author"}, "author": {"id": "1", "login": "author"}, "repo": "example/project",
            "title": "Upstream patch", "state": "open", "head": "a" * 40, "base": "b" * 40, "draft": False,
            "mergeable": "mergeable", "ci": "success", "ci_head": "a" * 40, "ci_ids": ["check:1"], "why": ["author"],
            "blocking_labels": [], "labels": [], "reviews": [], "events": []},
            expected_revision=contributions.snapshot(self.connection, "github:" + url)["revision"])
        self.connection.commit()
        return contributions.snapshot(self.connection, "github:" + url)["revision"]

    def test_post_nudge_posts_the_draft_on_stdin_to_the_rows_own_url(self):
        url = "https://github.com/example/project/pull/21"
        revision = self.observed(url)
        status, body = self.run_("contribution.nudge", key="github:" + url, revision=revision, body="Any news? $(id)\n")
        self.assertEqual((status, body["exit"]), (200, 0), body)
        self.assertEqual(self.ran(), [f"gh pr comment {url} --body-file -"])
        self.assertEqual((Path(self.tmp.name).resolve() / "posted").read_text(), "Any news? $(id)\n")

    def test_post_nudge_refuses_a_stale_row_an_empty_draft_and_a_key_off_github(self):
        url = "https://github.com/example/project/pull/21"
        revision = self.observed(url)
        nudge = {"action": "contribution.nudge", "key": "github:" + url, "revision": revision}
        self.refused([({**nudge, "revision": "0" * 64, "body": "hi"}, 409, "contribution changed"),
                      ({**nudge, "body": "   "}, 400, "empty"),
                      ({**nudge, "body": "x" * (run_actions.NUDGE_MAX + 1)}, 400, "at most"),
                      ({**nudge, "key": "sd:5", "body": "hi"}, 400, "filed on GitHub")])


class ThePull(Run):
    """sd:5888: real repositories in a temporary folder; the clone is one commit behind origin."""

    def git(self, where, *args):
        import subprocess

        return subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, check=True,
                              env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
                                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}).stdout.strip()

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name).resolve()
        self.origin, self.clone, other = root / "origin.git", root / "clone", root / "other"
        self.git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(root, "clone", "-q", str(self.origin), str(other))
        (other / "a").write_text("one\n")
        self.git(other, "add", "a")
        self.git(other, "commit", "-q", "-m", "one")
        self.git(other, "push", "-q", "origin", "main")
        self.git(root, "clone", "-q", str(self.origin), str(self.clone))
        (other / "a").write_text("two\n")
        self.git(other, "commit", "-q", "-am", "two")
        self.git(other, "push", "-q", "origin", "main")
        self.tip = self.git(other, "rev-parse", "HEAD")
        upsert_repo(self.connection, str(self.clone), remote="git@example.invalid:example/clone.git")
        self.connection.commit()
        self.lanes = root / "lanes"
        lanes = patch.dict(os.environ, {"SD_LANE_ROOT": str(self.lanes), "GIT_CONFIG_GLOBAL": os.devnull})
        lanes.start()
        self.addCleanup(lanes.stop)

    def pull(self, path=None):
        return self.run_("repo.pull", path=str(path or self.clone))

    def head(self):
        return self.git(self.clone, "rev-parse", "HEAD")

    def test_a_clean_checkout_on_its_default_branch_fast_forwards(self):
        status, body = self.pull()
        self.assertEqual((status, body["exit"], body["line"]), (200, 0, f"git -C {self.clone} pull --ff-only"), body)
        self.assertEqual(self.head(), self.tip)
        self.assertEqual(self.journalled()[-1][1], 0)

    def refuses(self, said, path=None):
        before = self.head()
        status, body = self.pull(path)
        self.assertEqual(status, 400, body)
        self.assertIn(said, body["error"])
        self.assertEqual(self.head(), before)

    def test_a_dirty_tree_another_branch_or_a_detached_head_refuses(self):
        (self.clone / "new").write_text("x")
        self.refuses("1 uncommitted or untracked file")
        (self.clone / "new").unlink()
        self.git(self.clone, "switch", "-q", "-c", "topic")
        self.refuses("it is on topic, not the default branch main")
        self.git(self.clone, "switch", "-q", "--detach", "main")
        self.refuses("HEAD is detached")

    def test_a_linked_worktree_and_a_pinned_checkout_refuse(self):
        linked = Path(self.tmp.name).resolve() / "linked"
        self.git(self.clone, "worktree", "add", "-q", "-b", "main-copy", str(linked), "main")
        upsert_repo(self.connection, str(linked), remote="git@example.invalid:example/linked.git")
        self.connection.commit()
        status, body = self.pull(linked)
        self.assertEqual((status, "is a linked worktree" in body["error"]), (400, True), body)
        with patch.object(run_actions, "CHECKOUT", self.clone):
            self.refuses("is a pinned checkout")

    def test_a_lane_that_holds_the_runner_lock_refuses_and_a_free_lock_is_released_after(self):
        import fcntl

        lock = self.lanes / "clone" / "lane" / "queue" / "runner.lock"
        lock.parent.mkdir(parents=True)
        with open(lock, "a", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.refuses("the lane runs in")
        self.assertEqual(self.pull()[0], 200)
        with open(lock, "a", encoding="utf-8") as after:
            fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError if Pull kept it

    def test_an_unregistered_path_runs_nothing(self):
        self.refuses("not a registered repository", self.origin)
