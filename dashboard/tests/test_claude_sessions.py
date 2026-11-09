"""Management > Sessions (sd:5106): each Claude session's task, last action, wait and safe to exit.

`claude_sessions.document` joins `claude agents --json`, the session transcripts, one `ps` and git. Every source is a
seam here: a Claude home in a temporary folder, and lists in place of claude, `ps`, git and the fleet. Nothing runs
the real claude, `ps` or git.
"""

import json
import os
import unittest
from pathlib import Path

from sd_dashboard import claude_sessions, fix_session
from support import ScreenCase, transition

NOW = "2026-09-06T12:00:00Z"
CLOCK = 1788696000.0  # NOW as a timestamp
LEAD = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def record(kind, *, stop=None, blocks=("text",), text="done"):
    if kind == "user":
        return {"type": "user", "message": {"role": "user", "content": text}}
    return {"type": "assistant", "message": {"role": "assistant", "stop_reason": stop,
                                             "content": [{"type": block, "text": text} if block == "text" else {"type": block}
                                                         for block in blocks]}}


FINISHED = [record("user", text="go"), record("assistant", stop="end_turn")]
MID_TURN = [record("user", text="go"), record("assistant", blocks=("tool_use",))]


def agent(session=LEAD, *, pid=100, status="idle", cwd="/work/system", name="system", waiting=None):
    row = {"pid": pid, "cwd": cwd, "kind": "interactive", "startedAt": int((CLOCK - 3600) * 1000), "sessionId": session,
           "name": name, "status": status}
    if waiting:
        row["waitingFor"] = waiting
    return row


def counted(builders):
    """The count and its reason; `holders` names the slots, checked on its own."""
    return {key: builders[key] for key in ("running", "why")}


def clean(path):
    return {"branch": "HEAD", "dirty": 0, "unpushed": 0}


class Sessions(ScreenCase):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / "claude"
        (self.home / "projects" / "-work-system").mkdir(parents=True)

    def transcript(self, session=LEAD, records=FINISHED, *, age=60):
        path = self.home / "projects" / "-work-system" / f"{session}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in records))
        os.utime(path, (CLOCK - age, CLOCK - age))
        return path

    def subagent(self, session, name, records, *, age=30, kind="sd-slice-builder", description=""):
        folder = self.home / "projects" / "-work-system" / session / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / f"agent-a{name}-0.jsonl"
        log.write_text("".join(json.dumps(row) + "\n" for row in records))
        os.utime(log, (CLOCK - age, CLOCK - age))
        (folder / f"agent-a{name}-0.meta.json").write_text(json.dumps(
            {"agentType": name, "customAgentType": kind, "name": name, "description": description}))
        return log

    def read(self, agents, *, processes=list, work=clean, fleet=None, connection=True):
        trees = fleet or (lambda area: {"worktrees": []})
        return claude_sessions.document(self.connection if connection else None, now=NOW, fleet=trees,
                                        agents=lambda: agents, processes=processes, work=work, root=self.home)

    def test_an_idle_session_with_nothing_under_it_is_safe_to_exit_and_carries_its_last_action(self):
        self.transcript(age=600)
        doc = self.read([agent()])
        row = doc["sessions"][0]
        self.assertEqual((row["status"], row["waiting"], row["safe"]), ("idle", False, {"state": "yes", "why": []}))
        self.assertEqual((row["last_action"], row["age"], row["age_text"]), ("2026-09-06T11:50:00Z", 600, "10m"))
        self.assertEqual(doc["sources"], {"agents": "", "processes": "", "worktrees": ""})
        self.assertEqual(counted(doc["builders"]), {"running": 0, "why": ""})

    def test_a_session_waiting_on_the_operator_ranks_first_and_says_what_holds_it(self):
        self.transcript(age=10)
        self.transcript(OTHER, age=5)
        doc = self.read([agent(), agent(OTHER, pid=200, status="waiting", name="hoa", waiting="permission: Bash")])
        first = doc["sessions"][0]
        self.assertEqual((first["id"], first["waiting"], first["waiting_for"]), (OTHER, True, "permission: Bash"))
        self.assertEqual(first["safe"], {"state": "no", "why": ["it waits on you: permission: Bash"]})

    def test_a_running_builder_names_the_task_and_blocks_exit(self):
        self.transcript()
        item = self.item("Sessions screen")
        self.subagent(LEAD, f"b{item}-sessions", MID_TURN, age=120, description="Build sessions")
        self.subagent(LEAD, "old-reader", MID_TURN, age=claude_sessions.STALL + 60, kind="general-purpose")
        def trees(area):
            return {"worktrees": [{"path": "/wt/s", "branch": f"review/sd{item}-s", "live": True}]}
        row = self.read([agent()], fleet=trees)["sessions"][0]
        self.assertEqual(row["tasks"], [{"item": item, "via": f"builder b{item}-sessions", "title": "Sessions screen",
                                         "status": "planning"}])
        self.assertEqual(row["safe"]["why"][0], f"builder b{item}-sessions is running")
        self.assertEqual([(a["name"], a["state"]) for a in row["agents"]],
                         [(f"b{item}-sessions", "running"), ("old-reader", "stalled")])
        # The age is the newest write of the session or any of its agents.
        self.assertEqual(row["age"], 60)

    def test_stalled_or_unread_counts_as_active_until_a_final_answer(self):
        """Review class (sd:5106 round 1): an unknown or stale reading counts as idle, done or safe. Each reading here
        lacks completion evidence, so the session is not safe to exit. An unread builder holds a Start next slot; a
        stalled one does not (operator, pack sd:6400): the load cap guards the machine, and a day's finished builders
        left 51 stalled slots against a cap of 6, so Build claimed nothing."""
        item = self.item("Builder under test")
        def trees(area):
            return {"worktrees": [{"path": "/wt/b", "branch": f"sd{item}-b", "live": True}]}

        def meta_only(session, name):
            folder = self.home / "projects" / "-work-system" / session / "subagents"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"agent-a{name}-0.meta.json").write_text(json.dumps(
                {"agentType": name, "customAgentType": "sd-slice-builder", "name": name, "description": f"Build sd:{item}"}))

        cases = {
            "stalled": (lambda s: self.subagent(s, "b", MID_TURN, age=claude_sessions.STALL + 60, description=f"Build sd:{item}"),
                        "no", "builder b stalled: no write for 31m and no final answer"),
            "no record read": (lambda s: self.subagent(s, "b", [], description=f"Build sd:{item}"),
                               "unknown", "builder b was not read, so it is not known to be done"),
            "old, no record read": (lambda s: self.subagent(s, "b", [], age=claude_sessions.STALL + 60, description=f"Build sd:{item}"),
                                    "unknown", "builder b was not read, so it is not known to be done"),
            "no log yet": (lambda s: meta_only(s, "b"), "unknown", "builder b was not read, so it is not known to be done"),
        }
        for case, (make, state, why) in cases.items():
            with self.subTest(case):
                session = f"{len(case):02d}{case.replace(' ', '').replace(',', '')}"[:20].ljust(12, "0")
                self.transcript(session)
                make(session)
                doc = self.read([agent(session)], fleet=trees)
                row = doc["sessions"][0]
                self.assertEqual(row["safe"], {"state": state, "why": [why]})
                self.assertEqual(counted(doc["builders"]), {"running": 0 if case == "stalled" else 1, "why": ""})
                self.assertEqual([t["item"] for t in row["tasks"]], [item])
        # A final answer is the evidence that frees it.
        self.transcript(OTHER)
        self.subagent(OTHER, "b", FINISHED, age=claude_sessions.STALL + 60, description=f"Build sd:{item}")
        doc = self.read([agent(OTHER)], fleet=trees)
        self.assertEqual((doc["sessions"][0]["safe"]["state"], doc["builders"]["running"]), ("yes", 0))

    def test_a_report_or_message_then_silence_is_not_a_final_answer(self):
        """Review round 2 (sd:5106): a SendMessage may be a progress note or a message to a peer, and no tool's name
        proves completion. Only a finished answer frees an agent; a report answered last stays running, then stalled."""
        def handed(tool, ident="t1"):
            return [record("user", text="go"),
                    {"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use",
                                                      "content": [{"type": "tool_use", "id": ident, "name": tool}]}},
                    {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": ident}]}}]
        cases = [("SendMessage", 121, "running"), ("SendMessage", claude_sessions.STALL + 60, "stalled"),
                 ("SubagentHandback", claude_sessions.STALL + 60, "stalled"), ("Bash", claude_sessions.STALL + 60, "stalled")]
        for tool, age, state in cases:
            with self.subTest(tool=tool, age=age):
                log = self.subagent(LEAD, "b", handed(tool), age=age)
                self.assertEqual(claude_sessions._subagents(self.transcript(), CLOCK)[0]["state"], state)
                log.unlink()

    def test_a_fix_sessions_own_item_has_its_worktree_checked(self):
        """Review round 2 (sd:5106): Fix tells its builder to work in a worktree, so the session's own item counts."""
        item = self.item("Started by Fix")
        self.transcript(records=[record("user", text=fix_session.prompt("task", item)), record("assistant", stop="end_turn")])
        def trees(area):
            return {"worktrees": [{"path": "/wt/fix", "branch": f"sd{item}-fix", "live": True}]}
        held = {"/work/system": {"branch": "main", "dirty": 0, "unpushed": 0},
                "/wt/fix": {"branch": f"sd{item}-fix", "dirty": 0, "unpushed": 2}}
        row = self.read([agent()], fleet=trees, work=held.__getitem__)["sessions"][0]
        self.assertEqual(row["safe"], {"state": "no", "why": ["/wt/fix holds 2 unpushed commits"]})
        row = self.read([agent()], work=held.__getitem__)["sessions"][0]
        self.assertEqual(row["safe"], {"state": "unknown", "why": [
            f"no live worktree names #{item}, and the item is not done, so its builder's work was not read"]})

    def test_a_finished_builder_whose_worktree_is_not_found_is_unknown_until_its_item_is_done(self):
        self.transcript()
        item = self.item("Landed elsewhere", kind="task")
        self.subagent(LEAD, "b", FINISHED, description=f"Build sd:{item}")
        self.subagent(LEAD, "nameless", FINISHED)
        row = self.read([agent()])["sessions"][0]
        self.assertEqual(row["safe"], {"state": "unknown", "why": [
            "builder nameless names no item, so its worktree was not read",
            f"no live worktree names #{item}, and the item is not done, so its builder's work was not read"]})
        (self.home / "projects" / "-work-system" / LEAD / "subagents" / "agent-anameless-0.meta.json").unlink()
        transition(self.connection, item, "done", who="human")
        self.assertEqual(self.read([agent()])["sessions"][0]["safe"], {"state": "yes", "why": []})

    def test_a_missing_status_pid_or_cwd_is_unknown_and_a_fix_session_without_status_holds_a_slot(self):
        item = self.item("Started by Fix")
        self.transcript(records=[record("user", text=fix_session.prompt("task", item)), record("assistant", stop="end_turn")])
        bare = agent()
        for key in ("status", "pid", "cwd"):
            bare.pop(key)
        doc = self.read([bare])
        row = doc["sessions"][0]
        self.assertEqual((row["status"], row["waiting"], row["safe"]["state"]), ("unknown", None, "unknown"))
        self.assertEqual(row["safe"]["why"], [
            "claude agents gave no status, so whether it works or waits on you is not known",
            "claude agents gave no pid, so its lane watches were not read",
            "claude agents gave no cwd, so its checkout was not read",
            f"no live worktree names #{item}, and the item is not done, so its builder's work was not read"])
        self.assertEqual(counted(doc["builders"]), {"running": 1, "why": ""})
        self.assertEqual(doc["builders"]["holders"], [{"session": "system", "repo": "system", "agent": "", "state": "unknown", "age": 60}])

    def test_a_first_prompt_past_the_head_is_still_read_and_one_never_found_is_unread(self):
        item = self.item("Long head")
        filler = [{"type": "summary", "summary": "x" * 1000} for _ in range(100)]
        self.transcript(records=filler + [record("user", text=fix_session.prompt("task", item)), record("assistant", blocks=("tool_use",))])
        self.assertEqual(counted(self.read([agent(status="busy")])["builders"]), {"running": 1, "why": ""})
        self.transcript(records=[{"type": "summary", "summary": "x" * (claude_sessions.TAIL_MAX + 10)}])
        doc = self.read([agent(status="busy")])
        self.assertIsNone(doc["builders"]["running"])
        self.assertIn("its transcript could not be read: no first prompt in its first 4 MiB", doc["sessions"][0]["safe"]["why"])

    def test_the_turn_reads_the_last_record_even_past_the_tail_and_a_question_is_still_a_turn(self):
        long = record("assistant", stop="end_turn", text="x" * (claude_sessions.TAIL_BYTES * 2))
        log = self.transcript(records=[record("user"), long])
        self.assertEqual(claude_sessions._turn(log), "idle")
        log = self.transcript(records=[record("user"), record("assistant", stop="end_turn", blocks=("text", "tool_use"))])
        self.assertEqual(claude_sessions._turn(log), "running")
        log = self.transcript(records=[record("assistant", stop="end_turn"), record("user", text="a message")])
        self.assertEqual(claude_sessions._turn(log), "running")

    def test_a_lane_watch_under_the_session_blocks_exit_and_one_under_another_does_not(self):
        self.transcript()
        ps = [(100, 1, "claude"), (101, 100, "/bin/zsh -c sd-ship lane watch --item 7"),
              (102, 101, "/usr/bin/python3 /pack/bin/sd-ship lane watch --item 7"),
              (300, 1, "sd-ship lane watch --item 8"), (103, 100, "sd-ship lane enqueue 9")]
        row = self.read([agent()], processes=lambda: ps)["sessions"][0]
        self.assertEqual([watch["pid"] for watch in row["watches"]], [101, 102])
        self.assertEqual(row["safe"]["state"], "no")

    def test_unpushed_work_in_the_cwd_or_a_builders_worktree_blocks_exit_and_names_the_checkout(self):
        self.transcript()
        item = self.item("Fleet controls")
        self.subagent(LEAD, f"b{item}", FINISHED, description=f"Build sd:{item}")
        def trees(area):
            return {"worktrees": [
            {"path": "/wt/mine", "branch": f"review/sd{item}-x", "live": True},
            {"path": "/wt/theirs", "branch": "review/sd424242-x", "live": True}]}
        held = {"/work/system": {"branch": "main", "dirty": 2, "unpushed": 0},
                "/wt/mine": {"branch": f"review/sd{item}-x", "dirty": 0, "unpushed": 1},
                "/wt/theirs": {"branch": "x", "dirty": 9, "unpushed": 9}}
        row = self.read([agent()], fleet=trees, work=held.__getitem__)["sessions"][0]
        self.assertEqual(row["safe"], {"state": "no", "why": ["/work/system holds 2 uncommitted files",
                                                              "/wt/mine holds 1 unpushed commit"]})
        self.assertEqual([c["path"] for c in row["checkouts"]], ["/work/system", "/wt/mine"])

    def test_a_fix_builder_session_counts_as_a_builder_while_it_works(self):
        item = self.item("Started by Fix")
        self.transcript(records=[record("user", text=fix_session.prompt("task", item)), record("assistant", blocks=("tool_use",))])
        doc = self.read([agent(status="busy")])
        row = doc["sessions"][0]
        self.assertTrue(row["builder"])
        self.assertEqual(row["tasks"][0]["via"], "Fix builder prompt")
        self.assertEqual(counted(doc["builders"]), {"running": 1, "why": ""})
        self.transcript(records=[record("user", text=fix_session.prompt("task", item)), record("assistant", stop="end_turn")])
        self.assertEqual(counted(self.read([agent(status="idle")])["builders"]), {"running": 0, "why": ""})

    def test_the_branch_of_the_cwd_names_its_task(self):
        self.transcript()
        item = self.item("On a branch")
        row = self.read([agent()], work=lambda path: {"branch": f"sd{item}-fix", "dirty": 0, "unpushed": 0})["sessions"][0]
        self.assertEqual([(t["item"], t["via"]) for t in row["tasks"]], [(item, f"branch sd{item}-fix")])

    def test_another_sessions_agents_are_not_this_sessions(self):
        """Evidence: a session reads only the transcript and subagents under its own id."""
        self.transcript()
        self.transcript(OTHER)
        self.subagent(OTHER, "b77-other", MID_TURN)
        row = self.read([agent()])["sessions"][0]
        self.assertEqual((row["agents"], row["safe"]["state"]), ([], "yes"))

    def test_a_failed_read_is_unknown_never_safe_and_never_a_count(self):
        self.transcript()

        def broken(*args):
            raise claude_sessions.Unread("ps gave no answer in 10 s")

        row = self.read([agent()], processes=broken)["sessions"][0]
        self.assertEqual(row["safe"], {"state": "unknown", "why": ["the process table was not read: ps gave no answer in 10 s"]})
        row = self.read([agent()], work=broken)["sessions"][0]
        self.assertEqual(row["safe"]["state"], "unknown")
        # No transcript: its builders are not known, so neither is the machine's builder count.
        doc = self.read([agent(OTHER)])
        self.assertEqual(doc["sessions"][0]["safe"]["state"], "unknown")
        self.assertIsNone(doc["builders"]["running"])
        # claude itself not answering: no session list, which is not an empty one.
        doc = claude_sessions.document(None, now=NOW, agents=broken, fleet=lambda area: {"worktrees": []}, root=self.home)
        self.assertIsNone(doc["sessions"])
        self.assertEqual(counted(doc["builders"]), {"running": None, "why": "ps gave no answer in 10 s"})
        doc = claude_sessions.document(None, now=NOW, agents=lambda: {"not": "a list"}, root=self.home)
        self.assertEqual(doc["sources"]["agents"], "claude agents --json answered no session list")

    def test_a_session_id_that_is_no_id_reads_no_file(self):
        self.transcript()
        doc = self.read([agent("*")])
        self.assertEqual(doc["sessions"][0]["safe"]["state"], "unknown")

    def test_an_observer_session_with_no_transcript_leaves_the_builder_count_known(self):
        # pack sd:8266: claude-mem's observers keep no transcript and build nothing; two stopped the dispatcher.
        cwd = "/home/op/.claude-mem/observer-sessions/49303"
        doc = self.read([agent(OTHER, status="busy", cwd=cwd, name="49303-f1")])
        self.assertEqual(counted(doc["builders"]), {"running": 0, "why": ""})


class Ending(unittest.TestCase):
    """sd:5126: End session signals only the pid that claude agents and ps still tie to the posted session."""

    PS = [(100, 1, "claude -n system"), (200, 1, "claude -n hoa"), (300, 1, "/usr/bin/python3 server.py")]

    def end(self, payload, *, agents=None, processes=None):
        sent = []
        write = claude_sessions.end(payload, agents=agents or (lambda: [agent(LEAD, pid=100), agent(OTHER, pid=200, name="hoa")]),
                                    processes=processes or (lambda: self.PS), kill=lambda pid, sig: sent.append((pid, sig)))
        return write(None), sent

    def test_the_posted_session_and_pid_are_signalled_with_sigterm(self):
        got, sent = self.end({"id": OTHER, "pid": 200})
        self.assertEqual(sent, [(200, claude_sessions.signal.SIGTERM)])
        self.assertEqual(got, {"id": OTHER, "pid": 200, "name": "hoa", "ended": "SIGTERM sent"})

    def test_evidence_from_another_session_or_process_refuses_and_signals_nothing(self):
        cases = {
            "now runs as pid 100": ({"id": LEAD, "pid": 200}, None, None),  # the other session's pid
            "no longer listed": ({"id": "33333333-3333-4333-8333-333333333333", "pid": 200}, None, None),
            "runs /usr/bin/python3, not claude": ({"id": LEAD, "pid": 300}, lambda: [agent(LEAD, pid=300)], None),
            "not in the process table": ({"id": LEAD, "pid": 100}, None, lambda: []),
        }
        for said, (payload, agents, processes) in cases.items():
            with self.subTest(said):
                sent = []
                write = claude_sessions.end(payload, agents=agents or (lambda: [agent(LEAD, pid=100), agent(OTHER, pid=200)]),
                                            processes=processes or (lambda: self.PS), kill=lambda pid, sig, *, sent=sent: sent.append(pid))
                with self.assertRaisesRegex(ValueError, said):
                    write(None)
                self.assertEqual(sent, [])

    def test_a_read_that_failed_refuses_and_is_never_a_match(self):
        def broken():
            raise claude_sessions.Unread("claude agents --json gave no answer in 10 s")
        for agents, processes in ((broken, None), (None, broken)):
            sent = []
            write = claude_sessions.end({"id": LEAD, "pid": 100}, agents=agents or (lambda: [agent(LEAD, pid=100)]),
                                        processes=processes or (lambda: self.PS), kill=lambda pid, sig, *, sent=sent: sent.append(pid))
            with self.assertRaisesRegex(ValueError, "no answer"):
                write(None)
            self.assertEqual(sent, [])

    def test_a_malformed_post_is_refused_before_any_read(self):
        for payload in ({"id": LEAD}, {"id": "*", "pid": 100}, {"id": LEAD, "pid": "100"}, {"id": LEAD, "pid": 1},
                        {"id": LEAD, "pid": True}, {"id": LEAD, "pid": 100, "signal": "KILL"}):
            with self.subTest(payload), self.assertRaisesRegex(ValueError, "Name the session"):
                claude_sessions.end(payload, agents=lambda: self.fail("read"), processes=lambda: self.fail("read"))

    def test_a_process_gone_before_the_signal_answers_already_gone(self):
        def gone(pid, sig):
            raise ProcessLookupError(pid)
        write = claude_sessions.end({"id": LEAD, "pid": 100}, agents=lambda: [agent(LEAD, pid=100)],
                                    processes=lambda: self.PS, kill=gone)
        self.assertEqual(write(None)["ended"], "already gone")

    def test_a_node_launched_claude_counts_as_claude(self):
        self.assertTrue(claude_sessions._runs_claude("node /opt/lib/node_modules/@anthropic-ai/claude-code/cli.js -c"))
        self.assertTrue(claude_sessions._runs_claude("/Users/x/.local/bin/claude --resume"))
        self.assertFalse(claude_sessions._runs_claude("bash -c claude"))


if __name__ == "__main__":
    unittest.main()
