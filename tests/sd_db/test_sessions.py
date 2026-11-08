"""The session registry (pack sd:6024): which session holds which worktree, on which machine.

Each test writes real heartbeat rows into a scratch database and real git
worktrees under a scratch folder; times are handed in, nothing sleeps.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed argv: git and the hook
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import sd_db
from sd_db import retention, sessions
from sd_db.writes import create_item, record_state

from tests.clean_env import clean_environment

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "bin" / "sd-session-beat"
T0 = "2026-10-25T10:00:00+00:00"
T1 = "2026-10-25T10:00:30+00:00"
T2 = "2026-10-25T10:02:00+00:00"
LATE = "2026-10-26T10:00:01+00:00"
HUB, SATELLITE = "hub", "satellite"


def git(*args: str) -> str:
    env = {**clean_environment(), "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.test",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.test",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


class Registry(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        sd_db.initialise(home=self.home)
        self.db = sd_db.connect(sd_db.default_path(self.home), write=True)
        self.addCleanup(self.db.close)
        self.repo = self.root / "repo"
        git("init", "-q", "-b", "main", str(self.repo))
        git("-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "first")

    def worktree(self, name: str, branch: str) -> Path:
        tree = self.root / "worktrees" / name
        git("-C", str(self.repo), "worktree", "add", "-q", "-b", branch, str(tree))
        return tree

    def beat(self, session: str, *trees: Path, machine: str = HUB, now: str = T0) -> bool:
        found = sessions.worktrees([str(tree) for tree in trees])
        return sessions.beat(self.db, session, machine=machine, cwd=str(self.root), found=found, now=now)

    def holders(self, tree: Path, machine: str = HUB, now: str = T1, **kwargs) -> list[str]:
        return [entry["session"] for entry in sessions.holders(self.db, str(tree), machine=machine, now=now, **kwargs)]

    def rows(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM state WHERE kind = 'heartbeat' AND key LIKE 'session:%'").fetchone()[0]


class Worktrees(Registry):
    def test_a_path_in_a_linked_worktree_names_it_its_repository_branch_and_item(self):
        tree = self.worktree("pack-session-registry-6024", "session-registry-6024")
        (tree / "lib").mkdir()
        found = sessions.worktrees([str(tree / "lib" / "new-file.py")])
        self.assertEqual(found, [{"path": str(tree), "repo": str(self.repo), "branch": "session-registry-6024",
                                  "item": 6024}])

    def test_a_build_branch_names_its_lead_assignments_item_not_the_assignment(self):
        """pack sd:8125: build/sd-<n> carries the bundle's lead assignment id, not an item id."""
        other = [create_item(self.db, kind="task", title=f"Unrelated {n}", status="ready") for n in range(3)][-1]
        item = create_item(self.db, kind="task", title="Lead item", status="ready")
        self.db.execute("INSERT INTO assignment (item, role, status, scope) VALUES (?, 'author', 'queued', 'item')",
                        (item,))
        lead = self.db.execute("SELECT MAX(id) FROM assignment").fetchone()[0]
        self.assertNotEqual(lead, item)
        tree = self.worktree("pack-build", f"build/sd-{lead}")
        self.assertIsNone(sessions.worktrees([str(tree)])[0]["item"], "no database, no item named")
        self.assertEqual(sessions.worktrees([str(tree)], self.db)[0]["item"], item)
        gone = self.worktree("pack-build-gone", f"build/sd-{other + lead + 100}")
        self.assertIsNone(sessions.worktrees([str(gone)], self.db)[0]["item"], "no assignment, no item named")

    def test_a_main_checkout_or_a_plain_folder_holds_nothing(self):
        (self.root / "plain").mkdir()
        self.assertEqual(sessions.worktrees([str(self.repo), str(self.root / "plain"), "/"]), [])

    def test_a_bash_command_names_its_paths(self):
        payload = {"cwd": "/somewhere", "tool_input": {"command": "cd /a/b && git -C ~/c status; make check X='/d/e'"}}
        self.assertEqual(sessions.touched(payload), ["/somewhere", "/a/b", "~/c", "/d/e"])
        self.assertEqual(sessions.touched({"tool_input": {"file_path": "/x/y.py"}}), ["/x/y.py"])

    def test_a_quoted_or_relative_bash_path_is_read_whole(self):
        """pack sd:6152: a quoted path with a space was cut at the space, a relative one was dropped.
        A relative path resolves against the payload's cwd, or the folder a `cd` before it named."""
        def paths(command: str, cwd: str | None = "/w/base") -> list[str]:
            return sessions.touched({"cwd": cwd, "tool_input": {"command": command}})[1 if cwd else 0:]

        self.assertEqual(paths('make -C "/tmp/work trees/pack" check'), ["/tmp/work trees/pack"])
        self.assertEqual(paths("make -C ../worktrees/pack check"), ["/w/worktrees/pack"])
        self.assertEqual(paths("cd /a/b && git -C ./c status"), ["/a/b", "/a/b/c"])
        self.assertEqual(paths("PYTHONPATH=.ci/pack/lib:lib:/x/lib python3"), ["/w/base/.ci/pack/lib", "/x/lib"])
        self.assertEqual(paths("git -C ../pack status", cwd=None), [])
        self.assertEqual(paths("curl https://example.test/a/b"), [])
        self.assertEqual(paths("pytest --rootdir=/r/s --co -k a/b"), ["/r/s", "/w/base/a/b"])

    def test_a_quoted_script_is_read_for_its_paths(self):
        """pack sd:6152 review: shlex made a quoted script one word, read whole as a relative filename, so
        `sh -c 'cd <worktree> && ...'` held no worktree. A word that is a script is read as a command again."""
        def paths(command: str) -> list[str]:
            return sessions.touched({"cwd": "/w/base", "tool_input": {"command": command}})[1:]

        self.assertEqual(paths("sh -c 'cd /tmp/worktrees/pack && make test'"), ["/tmp/worktrees/pack"])
        self.assertEqual(paths('bash -lc "cd ../pack && make"'), ["/w/pack"])
        self.assertEqual(paths("zsh -c 'make -C /a/b check'"), ["/a/b"])
        self.assertEqual(paths("sh -c '/usr/bin/make -C /a/b'"), ["/usr/bin/make", "/a/b"])
        self.assertEqual(paths("python3 -c \"import os; os.listdir('/p/q')\""), ["/p/q"])
        self.assertEqual(paths("node -e \"require('fs').readdirSync('/n/m')\""), ["/n/m"])
        self.assertEqual(paths('eval "cd /e/f && ls"'), ["/e/f"])
        self.assertEqual(paths("python3 - <<'EOF'\nprint(open(\"/h/d x\").read())\nEOF"), ["/h/d x"])
        self.assertEqual(paths("python3 - <<'EOF'\nprint(\"it's /h/e\")\nEOF"), ["/h/e"])
        self.assertEqual(paths('git commit -m "fix the /a/b path"'), ["/a/b"])
        self.assertEqual(paths("echo \"/a/b; cd /c\""), ["/a/b", "/c"])
        self.assertEqual(paths('make -C "/tmp/work trees/pack" check'), ["/tmp/work trees/pack"])

    def test_touched_holds_on_extreme_commands(self):
        """Property: any command string returns bounded absolute or home paths and never raises."""
        cases = ["", " ", "'", '"', "\\", "''\"\"", "'unclosed /a", '"a \'b\' "/c d"', "$(cd /e && ls ../f)",
                 "/" * 5000, "x " * 20000 + "/late", "a" * 1_000_000, "'" + "/b" * 500_000,
                 "cd /ü/ñ && ls ./日本 '/😀 x'", "a\x00b /n\x00ul",
                 "echo \"'\\\"/q\\\"'\"", ";;&&||<<>>((", "cd", "cd ''", "-C", "=:=:", "~", "~/", "../" * 3000]
        cases += ["".join(chr(c) for c in range(start, start + 400)) for start in (0, 0x2000, 0x10000)]
        cases += ["sh -c '" + "cd /a && " * 3000 + "'", "bash -c " + "'(" * 8000, "eval " + "\"$(" * 4000,
                  "sh -c \"sh -c 'sh -c \\\"sh -c /deep\\\"'\"", "python3 -c \"print('/ü x')\"",
                  "node -e " + "'" + "x y " * 6000 + "'", "cat <<'EOF'\nit's /h\n" + "a b\n" * 5000 + "EOF",
                  "sh -c ''", "eval", "-c", "sh -c '\x00/n'"]
        for command in cases:
            with self.subTest(command=command[:40]):
                found = sessions.touched({"cwd": "/w/base", "tool_input": {"command": command}})
                self.assertLessEqual(len(found), 1 + sessions.MAX_COMMAND_PATHS)
                for path in found:
                    self.assertIsInstance(path, str)
                    self.assertTrue(path.startswith(("/", "~/")), path)
        self.assertEqual(sessions.touched({"cwd": "/w", "tool_input": {"command": "cd /ü/ñ && ls ./日本 '/😀 x'"}}),
                         ["/w", "/ü/ñ", "/ü/ñ/日本", "/😀 x"])


class Beats(Registry):
    def test_a_beat_is_one_row_per_session(self):
        one, two = self.worktree("one", "feat/one"), self.worktree("two", "feat/two")
        self.assertTrue(self.beat("session-a", one, now=T0))
        self.assertTrue(self.beat("session-a", two, now=T1))
        self.assertEqual(self.rows(), 1)
        self.assertEqual((self.holders(one), self.holders(two)), (["session-a"], ["session-a"]))

    def test_a_beat_inside_a_minute_with_nothing_new_writes_nothing(self):
        one = self.worktree("one", "feat/one")
        self.assertTrue(self.beat("session-a", one, now=T0))
        self.assertFalse(self.beat("session-a", one, now=T1))
        self.assertTrue(self.beat("session-a", one, now=T2))
        self.assertEqual(sessions.live(self.db, now=T2)[0]["beat"], T2)

    def test_a_stale_beat_holds_nothing(self):
        one = self.worktree("one", "feat/one")
        self.beat("session-a", one, now=T0)
        self.assertEqual(self.holders(one, now=LATE), [])
        self.assertEqual(sessions.live(self.db, now=LATE), [])

    def test_two_sessions_hold_one_worktree(self):
        one = self.worktree("one", "feat/one")
        self.beat("session-a", one)
        self.beat("session-b", one)
        self.assertEqual(sorted(self.holders(one)), ["session-a", "session-b"])
        self.assertEqual(sessions.end(self.db, "session-a"), 1)
        self.assertEqual(self.holders(one), ["session-b"])
        self.assertEqual(self.holders(one, exclude=("session-b",)), [])

    def test_another_machines_session_holds_nothing_here(self):
        one = self.worktree("one", "feat/one")
        self.beat("session-s", one, machine=SATELLITE)
        self.assertEqual(self.holders(one, machine=HUB), [])
        self.assertEqual(self.holders(one, machine=SATELLITE), ["session-s"])

    def test_the_prune_deletes_a_stale_session_row_and_keeps_the_rest(self):
        one = self.worktree("one", "feat/one")
        self.beat("session-old", one, now=T0)
        self.beat("session-new", one, now="2026-10-26T09:00:00+00:00")
        record_state(self.db, "heartbeat", key="cron-report:probe", body={}, timestamp=T0)
        removed = retention.compact_heartbeats(self.db, now=datetime.fromisoformat(LATE).astimezone(UTC))
        self.assertEqual(removed, 1)
        keys = {row[0] for row in self.db.execute("SELECT key FROM state WHERE kind = 'heartbeat'")}
        self.assertEqual(keys, {"session:session-new", "cron-report:probe"})


class Hook(Registry):
    def hook(self, payload: dict, home: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(  # nosec B603 - fixed argv, a scratch HOME
            [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
            env=clean_environment(HOME=str(home or self.home)), check=False)

    def test_a_tool_call_in_a_worktree_holds_it_and_the_session_end_frees_it(self):
        tree = self.worktree("one", "feat/sd-77")
        done = self.hook({"hook_event_name": "PreToolUse", "session_id": "session-a", "cwd": str(self.root),
                          "tool_name": "Bash", "tool_input": {"command": f"cd {tree} && make check"}})
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        found = sessions.live(self.db)
        self.assertEqual([(entry["session"], entry["item"]) for entry in found], [("session-a", 77)])
        self.assertEqual([entry["path"] for entry in found[0]["worktrees"]], [str(tree)])
        self.hook({"hook_event_name": "SessionEnd", "session_id": "session-a"})
        self.assertEqual(sessions.live(self.db), [])

    def teammate_call(self, command: str, **fields) -> dict:
        return {"hook_event_name": "PreToolUse", "session_id": "session-a", "agent_id": "agent-1",
                "cwd": str(self.root), "tool_name": "Bash",
                "tool_input": {"command": command, "description": "Note it", "timeout": 600000}, **fields}

    def test_a_teammates_sd_call_gets_its_mark_and_keeps_the_rest_of_its_input(self):
        """pack sd:6046: a teammate shares its lead's session id; the payload's `agent_id` tells it apart."""
        done = self.hook(self.teammate_call("cd /tmp && sd task note 7 'Done' --kind decision"))
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        printed = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(printed, {"hookEventName": "PreToolUse", "updatedInput": {
            "command": "export SD_AGENT_SESSION=session-a:agent-1; cd /tmp && sd task note 7 'Done' --kind decision",
            "description": "Note it", "timeout": 600000}})
        self.assertEqual([entry["session"] for entry in sessions.live(self.db)], ["session-a"])

    def test_the_mark_names_sd_and_sd_commands_by_name_or_path_only(self):
        """pack sd:6046: a call that runs no `sd` is not rewritten, so its permission check is the same."""
        for command in ("sd task show 7", "bin/sd task note 7 x", "~/.local/bin/sd-ship lane status",
                        "make test; sd-review -C /tmp", "(sd today)"):
            with self.subTest(command=command):
                self.assertTrue(self.hook(self.teammate_call(command)).stdout)
        for command in ("ls ~/repos/platypeeps/sd-workflow/bin", "git log --grep sd:6046", "echo sdx",
                        "cat sd_work.py"):
            with self.subTest(command=command):
                self.assertEqual(self.hook(self.teammate_call(command)).stdout, "")

    def test_the_lead_another_tool_or_an_unsafe_id_gets_no_mark(self):
        """pack sd:6046: the lead's payload has no `agent_id`; an id outside the safe set never reaches a shell."""
        lead = self.teammate_call("sd task note 7 x")
        del lead["agent_id"]
        for payload in (lead, self.teammate_call("sd task note 7 x", tool_name="Edit"),
                        self.teammate_call("sd task note 7 x", agent_id="a1; rm -rf ~"),
                        self.teammate_call("sd task note 7 x", session_id="s$(id)"),
                        self.teammate_call("sd task note 7 x", hook_event_name="SessionEnd")):
            with self.subTest(payload=payload):
                done = self.hook(payload)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_an_unreadable_database_still_marks_a_teammates_call(self):
        """pack sd:6046: the mark needs no database; a failed beat leaves it in place."""
        empty = self.root / "empty-home"
        empty.mkdir()
        done = self.hook(self.teammate_call("sd task show 7"), home=empty)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertIn("SD_AGENT_SESSION=session-a:agent-1", done.stdout)

    def test_an_unreadable_database_is_silent(self):
        empty = self.root / "empty-home"
        empty.mkdir()
        done = self.hook({"hook_event_name": "PreToolUse", "session_id": "session-a"}, home=empty)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
