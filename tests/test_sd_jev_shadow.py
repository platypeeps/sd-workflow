"""The Jev shadow stages: review triage (sd:2092) and the duplicate hint (sd:2093).

A shadow stage records Jev's answer and never reads it back. The local Kev
keeps the text on the machine; hosted Jev may read a public repository only. Every case
puts a `jev` and a `gh` stub on the only `PATH` the run is handed, so the suite
stays offline and neither real command is reached.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest.mock

import sd_db
import sd_db.repos

from tests.test_sd_review import FakeRunner, ReviewFixture, namespace, sd_review

sd_jev = sd_review.sd_jev
#: Importable once `tests.test_sd_review` has put `bin/` on `sys.path`.
import sd_jev_shadow  # noqa: E402
import sd_review_slots  # noqa: E402
import sd_work  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Logs each call as one JSON line. `enabled` exits with the case's code;
#: `choice` exits with the case's code and prints the `--shadow` answer, as
#: the real `jev` does in shadow mode.
JEV_STUB = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
stdin = "" if argv[:1] == ["enabled"] else sys.stdin.read()
with open({log!r}, "a") as log:
    log.write(json.dumps({{"argv": argv, "state": stdin, "run": os.environ.get("JEV_RUN")}}) + "\\n")
if argv[:1] == ["enabled"]:
    raise SystemExit({gate})
if {code}:
    raise SystemExit({code})
if {raw}:
    sys.stdout.buffer.write(b"\\xff\\n")
    sys.stderr.buffer.write(b"\\xfe\\n")
    raise SystemExit(0)
print(argv[argv.index("--shadow") + 1])
"""

#: Answers `gh api repos/<owner>/<repo> --jq .private` with the case's word.
#: Asked of the host `GH_HOST` names, unless `--hostname` pins one, it says
#: `false`: an Enterprise twin of a private github.com repository is public.
GH_STUB = """#!/usr/bin/env python3
import json, os, sys
with open({log!r}, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
pinned = "--hostname" in sys.argv
print("false" if os.environ.get("GH_HOST") and not pinned else {private!r})
"""

FINDING = {"path": "src.py", "line": 1, "severity": "high", "summary": "bad, really", "family": "correctness"}


class Stubs:
    """The two stubs in `bin_dir`, and what each was asked."""

    def __init__(self, bin_dir: pathlib.Path, *, gate: int = 0, code: int = 0, private: str = "false",
                 raw: bool = False) -> None:
        self.jev_log, self.gh_log = bin_dir / "jev.log", bin_dir / "gh.log"
        for name, text in (("jev", JEV_STUB.format(log=str(self.jev_log), gate=gate, code=code, raw=raw)),
                           ("gh", GH_STUB.format(log=str(self.gh_log), private=private))):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o700)

    def jev(self) -> list[dict]:
        return [json.loads(line) for line in self.jev_log.read_text().splitlines()] if self.jev_log.exists() else []

    def choices(self) -> list[dict]:
        return [call for call in self.jev() if call["argv"][:1] == ["choice"]]

    def gh(self) -> list[list[str]]:
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()] if self.gh_log.exists() else []


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


class TriageTests(ReviewFixture):
    def repo(self) -> pathlib.Path:
        root = self.make_repo()
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/example/demo.git"],
                       cwd=str(root), check=True)
        (root / "src.py").write_text("x = 1\n", encoding="utf-8")
        return root

    def triage(self, findings, **env: str) -> str:
        noise = io.StringIO()
        sd_jev.jev_triage(findings, self.environment(**env), self.repo(), noise)
        return noise.getvalue()

    def review(self, root: pathlib.Path, findings) -> tuple[str, str]:
        """One review, its tier reading off, as its JSON bytes and its stderr."""
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", ""),
                             "codex": sd_review.Completed(0, json.dumps({"findings": findings}), "")})
        self.codex_home = getattr(self, "codex_home", None) or self.chatgpt_home()
        noise = io.StringIO()
        with contextlib.redirect_stderr(noise):
            result = sd_review.review(root, namespace(), runner, self.environment(JEV_SD_REVIEW="0"),
                                      self.codex_home)
        # A slot wait is load, not Jev: it differs between two runs on a busy machine (sd:6344).
        result["review_slot"].pop("waited_seconds", None)
        # A reader's wall clock is time, not Jev, too (pack sd:8503).
        for outcome in result["outcomes"]:
            for key in ("started", "finished", "wall_seconds"):
                outcome.pop(key, None)
        return json.dumps(result, sort_keys=True), noise.getvalue()

    def test_a_review_with_findings_records_a_triage_and_changes_nothing(self):
        root = self.repo()
        without, _ = self.review(root, [FINDING])
        stubs = Stubs(self.tool_bin)
        with_jev, said = self.review(root, [FINDING])
        self.assertEqual(with_jev, without)
        self.assertEqual(said, "")
        [call] = stubs.choices()
        argv = call["argv"]
        self.assertEqual((flag(argv, "--stage"), flag(argv, "--shadow"), flag(argv, "--caller")),
                         (sd_jev_shadow.TRIAGE_STAGE, sd_jev_shadow.UNTRIAGED, "sd-review"))
        self.assertEqual(set(sd_jev_shadow.TRIAGE_CRITERIA),
                         {part.split("=", 1)[0] for part in flag(argv, "--criteria").split(",")})
        self.assertEqual(json.loads(call["state"]), {
            "severity": "high", "disposition": "blocking", "family": "correctness",
            "path": "src.py", "summary": "bad, really"})
        self.assertEqual(stubs.gh(), [])
        # No opt-in: the local Kev only, for the probe and the question alike.
        self.assertTrue(all(call["argv"][-1] == "--local-only" for call in stubs.jev()), stubs.jev())

    def test_a_slow_slot_wait_is_not_a_difference_jev_made(self):
        """sd:6344. Two reviews that waited for a review slot unequally compare equal."""
        root = self.repo()
        ticks = iter(range(0, 1000, 2))
        with unittest.mock.patch.object(sd_review_slots, "clock", lambda: next(ticks)):
            slow, _ = self.review(root, [FINDING])
        with unittest.mock.patch.object(sd_review_slots, "clock", lambda: 0):
            quick, _ = self.review(root, [FINDING])
        self.assertEqual(slow, quick)

    def test_a_triage_run_is_one_jev_run(self):
        """sd:2954. The probe and every question of one review share one `JEV_RUN`."""
        root = self.repo()
        stubs = Stubs(self.tool_bin)
        with unittest.mock.patch.dict(os.environ):
            os.environ.pop("JEV_RUN", None)
            self.review(root, [FINDING, {**FINDING, "line": 2}])
        runs = [call["run"] for call in stubs.jev()]
        self.assertEqual(len(runs), 3, stubs.jev())
        self.assertEqual(len(set(runs)), 1, runs)
        self.assertRegex(runs[0], r"^sd-review-\d{8}T\d{6}-[0-9a-f]{4}$")

    def test_a_clean_review_asks_nothing(self):
        stubs = Stubs(self.tool_bin)
        self.review(self.repo(), [])
        self.assertEqual((stubs.jev(), stubs.gh()), ([], []))

    def test_the_local_kev_asks_github_nothing(self):
        """sd:3013. The local path sends nothing off the machine, so no visibility probe runs."""
        stubs = Stubs(self.tool_bin, private="true")
        self.assertEqual(self.triage([FINDING]), "")
        self.assertEqual((len(stubs.choices()), stubs.gh()), (1, []))

    def test_a_private_repository_sends_nothing_hosted(self):
        stubs = Stubs(self.tool_bin, private="true")
        self.assertEqual(self.triage([FINDING], SD_JEV_SHADOW_HOSTED="1"), "")
        self.assertEqual(stubs.choices(), [])
        self.assertEqual(len(stubs.gh()), 1)

    def test_gh_host_cannot_point_the_visibility_question_elsewhere(self):
        """Review of 9b7c4daad: `GH_HOST` sends `gh api` to another host, where a
        public repository of the same name would vouch for a private one."""
        stubs = Stubs(self.tool_bin, private="true")
        self.assertEqual(self.triage([FINDING], GH_HOST="ghe.example.test", SD_JEV_SHADOW_HOSTED="1"), "")
        self.assertEqual(stubs.choices(), [])

    def test_the_hosted_opt_in_still_needs_a_public_repository(self):
        """Lane review of b527597e9: hosted Jev needs the machine's opt-in, and the
        opt-in does not make a private repository's text sendable."""
        root, env = self.repo(), self.environment(SD_JEV_SHADOW_HOSTED="1")
        stubs = Stubs(self.tool_bin, private="true")
        sd_jev.jev_triage([FINDING], env, root, io.StringIO())
        self.assertEqual(stubs.choices(), [])
        Stubs(self.tool_bin)
        sd_jev.jev_triage([FINDING], env, root, io.StringIO())
        self.assertEqual([call["argv"][-1] == "--local-only" for call in stubs.choices()], [False])

    def test_a_failed_local_call_is_never_retried_hosted(self):
        stubs = Stubs(self.tool_bin, code=1)
        self.assertIn("`jev` exited 1", self.triage([FINDING, FINDING]))
        self.assertEqual([call["argv"][-1] for call in stubs.choices()], ["--local-only"])

    def test_a_failing_visibility_answer_reads_as_private(self):
        stubs = Stubs(self.tool_bin, private="")
        self.assertEqual(self.triage([FINDING], SD_JEV_SHADOW_HOSTED="1"), "")
        self.assertEqual(stubs.choices(), [])

    def test_no_github_origin_asks_nobody(self):
        stubs = Stubs(self.tool_bin)
        root = self.repo()
        subprocess.run(["git", "remote", "remove", "origin"], cwd=str(root), check=True)
        sd_jev.jev_triage([FINDING], self.environment(), root, io.StringIO())
        self.assertEqual((stubs.jev(), stubs.gh()), ([], []))

    def test_the_stage_switched_off_asks_nobody(self):
        stubs = Stubs(self.tool_bin)
        self.assertEqual(self.triage([FINDING], JEV_SD_REVIEW_TRIAGE="0"), "")
        self.assertEqual((stubs.jev(), stubs.gh()), ([], []))

    def test_a_jev_that_cannot_answer_asks_github_nothing(self):
        stubs = Stubs(self.tool_bin, gate=3)
        self.assertEqual(self.triage([FINDING]), "")
        self.assertEqual(stubs.gh(), [])
        self.assertEqual([call["argv"][:2] for call in stubs.jev()], [["enabled", sd_jev_shadow.TRIAGE_STAGE]])

    def test_a_failing_jev_is_loud_and_stops(self):
        stubs = Stubs(self.tool_bin, code=2)
        said = self.triage([FINDING, FINDING])
        self.assertIn("the Jev shadow reading stopped: `jev` exited 2", said)
        self.assertIn("JEV_SD_REVIEW_TRIAGE=0", said)
        self.assertEqual(len(stubs.choices()), 1)

    def test_undecodable_output_cannot_fail_the_command(self):
        """Review of 46529bae2: bytes that are not UTF-8 raised UnicodeDecodeError
        out of the shadow call, aborting the review before its gate."""
        stubs = Stubs(self.tool_bin, raw=True)
        self.assertEqual(self.triage([FINDING, FINDING]), "")
        self.assertEqual(len(stubs.choices()), 2)

    def test_a_switched_off_stage_probes_nothing_that_could_raise(self):
        """Review of 857d6d039: `github_head` ran before the switch, so an origin
        that does not decode failed a review whose stage was off."""
        stubs = Stubs(self.tool_bin)
        with unittest.mock.patch.object(sd_jev.sd_lib, "github_head", side_effect=UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "invalid start byte")) as head:
            self.assertEqual(self.triage([FINDING], JEV_SD_REVIEW_TRIAGE="0"), "")
        head.assert_not_called()
        self.assertEqual((stubs.jev(), stubs.gh()), ([], []))

    def test_any_failure_in_a_shadow_stage_is_a_note_not_an_exception(self):
        Stubs(self.tool_bin)
        root, said, noise = self.repo(), io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(sd_jev.sd_lib, "github_head", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(noise):
            sd_jev.jev_triage([FINDING], self.environment(), root, said)
            sd_work._dedupe((None, None, {"item": {"id": 2, "title": "new", "repo": str(root)}}),
                            self.environment())
        self.assertIn("sd-review: the Jev shadow reading stopped: RuntimeError: boom", said.getvalue())
        self.assertIn("sd-task-add: the Jev shadow reading stopped: RuntimeError: boom", noise.getvalue())

    def test_one_review_triages_at_most_the_cap(self):
        stubs = Stubs(self.tool_bin)
        self.triage([dict(FINDING, line=n) for n in range(sd_jev_shadow.MAX_TRIAGE + 3)])
        subjects = [flag(call["argv"], "--subject") for call in stubs.choices()]
        self.assertEqual(len(subjects), sd_jev_shadow.MAX_TRIAGE)
        self.assertTrue(subjects[0].startswith("sd-review-triage:example.demo:"), subjects[0])


class DedupeTests(ReviewFixture):
    """`sd task add` through the real CLI, on a scratch database."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        sd_db.initialise(home=self.home)
        self.root = self.home / "demo"
        self.root.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "T"], ["commit", "-q", "--allow-empty", "-m", "first"],
                     ["remote", "add", "origin", "git@github.com:example/demo.git"]):
            subprocess.run(["git", *args], cwd=str(self.root), check=True, capture_output=True)
        # The library keys a checkout under `$HOME` as `~/...` (sd:1439), so
        # this row and the CLI's agree only when both read the scratch home.
        with unittest.mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                sd_db.connect(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, self.root, home=self.home)

    def add(self, title: str, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("JEV_", "SD_JEV_"))}
        environment.update(HOME=str(self.home), PATH=str(self.tool_bin) + os.pathsep + os.defpath, **env)
        return subprocess.run([sys.executable, str(ROOT / "bin" / "sd"), "task", "add", "--priority", "3", title, "--json"],
                              cwd=str(self.root), env=environment, capture_output=True, text=True, check=True)

    def test_a_new_item_records_a_duplicate_pick_and_prints_the_same(self):
        first = json.loads(self.add("Fix the lane, again").stdout)["item"]["id"]
        stubs = Stubs(self.tool_bin)
        added = self.add("Fix the lane")
        state = json.loads(added.stdout)
        self.assertEqual(set(state), {"item", "notes", "revision"})
        self.assertEqual(added.stderr, "")
        [call] = stubs.choices()
        argv = call["argv"]
        self.assertEqual((flag(argv, "--stage"), flag(argv, "--shadow"), flag(argv, "--subject")),
                         ("JEV_SD_TASK_DEDUPE", "none", f"sd-task-dedupe:sd-{state['item']['id']}"))
        self.assertEqual(flag(argv, "--criteria"),
                         f"none=no listed item tracks the same work,sd-{first}=Fix the lane; again")
        self.assertEqual(json.loads(call["state"]), {"new_item_title": "Fix the lane"})
        self.assertEqual(argv[-1], "--local-only")

    def test_an_add_is_one_jev_run(self):
        """sd:2954. The probe and the question of one `sd task add` share one `JEV_RUN`."""
        self.add("Fix the lane, again")
        stubs = Stubs(self.tool_bin)
        self.add("Fix the lane")
        runs = [call["run"] for call in stubs.jev()]
        self.assertEqual(len(runs), 2, stubs.jev())
        self.assertEqual(len(set(runs)), 1, runs)
        self.assertRegex(runs[0], r"^sd-task-add-\d{8}T\d{6}-[0-9a-f]{4}$")

    def test_a_private_repository_sends_no_title(self):
        self.add("Fix the lane")
        stubs = Stubs(self.tool_bin, private="true")
        self.add("Fix the lane too", SD_JEV_SHADOW_HOSTED="1")
        self.assertEqual(stubs.choices(), [])

    def test_the_stage_switched_off_asks_nobody(self):
        self.add("Fix the lane")
        stubs = Stubs(self.tool_bin)
        self.add("Fix the lane too", JEV_SD_TASK_DEDUPE="off")
        self.assertEqual((stubs.jev(), stubs.gh()), ([], []))

    def test_the_first_item_of_a_repository_asks_no_question(self):
        stubs = Stubs(self.tool_bin)
        self.add("Fix the lane")
        self.assertEqual(stubs.choices(), [])

    def test_a_failing_candidate_read_cannot_fail_the_add(self):
        """Review of c57c43bd2: the candidate read ran outside the guarded stage,
        so with the stage off a read failure after filing hid the new item's id."""
        shim = self.tmp / "shim"
        shim.mkdir()
        (shim / "sitecustomize.py").write_text(
            "import sd_db.reads\n"
            "def broken(*args, **kwargs):\n"
            "    raise RuntimeError('backlog unreadable')\n"
            "sd_db.reads.backlog_items = broken\n")
        added = self.add("Fix the lane", JEV_SD_TASK_DEDUPE="0",
                         PYTHONPATH=f"{shim}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
        self.assertEqual(json.loads(added.stdout)["item"]["title"], "Fix the lane")

