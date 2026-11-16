"""Tasks > Start next (sd:5107): a queued build of the next ready items, within the builder cap and the load cap.

`start_next.plan` is the preview and `start_next.run` the press (Build step 5, pack sd:6096). The Claude sessions,
the gate and the dispatcher's kickstart are seams: the sessions document is a dict, the gate's answer a dict, and
`builds.kickstart` a recorder. Herdr's `subprocess.run` is replaced too, so a test sees any launch Start next makes.
The queue is the real `build.queue`. Nothing starts claude, runs `sd gate`, reaches launchctl or reaches herdr.
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import builds, fix_session, start_next
from sd_db import build, upsert_repo, workflow
from support import NOW, ScreenCase, before_5648


def sessions(running=0, named=(), why=""):
    rows = [{"tasks": [{"item": item, "via": "Fix builder prompt"} for item in named]}]
    return lambda connection, *, now: {"sessions": rows if running is not None else None,
                                       "builders": {"running": running, "why": why}}


def gate(load5=4.0, load_max=40.0):
    return lambda: {"load": [load5 + 1, load5, load5 - 1], "load_max": load_max}


class Crash(Exception):
    """The server process stopping between two launches."""


class StartNext(ScreenCase):
    def setUp(self):
        super().setUp()
        self.work = Path(self.tmp.name) / "checkout"
        self.work.mkdir()
        self.repo_path = self.repo(str(self.work))
        self.launched, self.kicks = [], []
        herdr = patch.object(fix_session.subprocess, "run", self.herdr)
        herdr.start()
        self.addCleanup(herdr.stop)
        kick = patch.object(builds, "kickstart", lambda: self.kicks.append(1) or {"ok": True, "service": "x", "why": ""})
        kick.start()
        self.addCleanup(kick.stop)
        cores = patch.object(build.os, "cpu_count", return_value=16)  # a builder load cap of 24 (sd:9094)
        cores.start()
        self.addCleanup(cores.stop)

    def repo(self, path="/repos/system"):
        """A managed repository with a remote, hosted here: one a Build press takes."""
        upsert_repo(self.connection, path, remote="git@example.invalid:x.git", managed=1)
        return path

    def builds_of(self, *items):
        return [tuple(row) for row in self.connection.execute(
            f"SELECT item, status FROM assignment WHERE scope = 'build' AND item IN ({','.join('?' * len(items))}) "
            "ORDER BY item", items)]

    def herdr(self, argv, **options):
        self.launched.append(argv[3])
        return subprocess.CompletedProcess(argv, 0, stdout=f"launched w1:p{len(self.launched)}: claude in {argv[2]}\n", stderr="")

    def ready(self, title, **columns):
        columns.setdefault("repo", self.repo_path)
        return self.item(title, kind="task", status="ready", **columns)

    def plan(self, **seams):
        seams.setdefault("sessions", sessions())
        seams.setdefault("gate", gate())
        return start_next.plan(self.connection, now=NOW, **seams)

    def run_plan(self, items, **seams):
        seams.setdefault("sessions", sessions())
        seams.setdefault("gate", gate())
        return start_next.run(self.connection, now=NOW, items=items, who="dashboard", **seams)

    def test_a_scheduled_occurrence_is_not_started_until_it_opens(self):
        """sd:5670: a ready occurrence that is scheduled (pack sd:5648) is off Tasks, so Start next leaves it out too."""
        later = self.schedule(self.ready("Water the plants"), "2026-09-09")
        ids = lambda out: [row["id"] for row in out["eligible"] + out["skipped"]]  # noqa: E731
        self.assertEqual(ids(self.plan()), [])
        with before_5648():
            self.assertEqual(ids(self.plan()), [later])

    def test_the_plan_names_the_next_ready_items_with_their_revisions_and_why_others_are_left_out(self):
        first, second = self.ready("First"), self.ready("Second")
        self.item("Planning only", kind="task", status="planning", repo=self.repo_path)
        nowhere = self.ready("No checkout", repo=self.repo("/repos/not-on-this-mac"))
        launched = self.ready("Fixed already")
        workflow.add_item_note(self.connection, launched, kind="comment", who="dashboard",
                               body=fix_session.NOTE.format(session="builder", item=launched, pane="p9", where="~/x"))
        named = self.ready("A session has it")
        upsert_repo(self.connection, "/repos/unmanaged", remote="git@example.invalid:y.git")
        unmanaged = self.ready("Unmanaged", repo="/repos/unmanaged")
        pressed = self.ready("Queued already")
        build.queue(self.connection, [pressed], who="test")
        out = self.plan(sessions=sessions(running=2, named=[named]))
        self.assertEqual((out["cap"], out["running"], out["room"], out["refusal"]), (6, 2, 2, ""))
        self.assertEqual([(row["id"], row["revision"]) for row in out["start"]],
                         [(first, workflow.item_state(self.connection, first)["revision"]),
                          (second, workflow.item_state(self.connection, second)["revision"])])
        why = {row["id"]: row["why"] for row in out["skipped"]}
        self.assertIn("not a directory on this machine", why[nowhere])
        self.assertEqual(why[launched], "Fix started a session for it already")
        self.assertEqual(why[named], "a live Claude session works on it")
        self.assertEqual(why[unmanaged], f"item {unmanaged}'s repository /repos/unmanaged is not managed and its remote "
                         "is not on GitHub, where its build opens a draft pull request")
        self.assertRegex(why[pressed], rf"^item {pressed} has queued assignment \d+$")

    def test_the_room_is_the_cap_less_the_running_builders(self):
        items = [self.ready(f"Task {n}") for n in range(8)]
        out = self.plan(sessions=sessions(running=3))
        self.assertEqual((out["room"], [row["id"] for row in out["start"]]), (3, items[:3]))

    def test_no_room_when_the_cap_is_full_the_load_is_high_or_either_was_not_read(self):
        self.ready("Waiting")
        cases = [
            (dict(sessions=sessions(running=6)), "6 builders run already; the cap is 6"),
            (dict(gate=gate(load5=41.5)), "the five-minute load is 41.5, above the cap of 24"),
            (dict(sessions=sessions(running=None, why="claude agents --json gave no answer in 10 s")),
             "the running builders were not counted: claude agents --json gave no answer in 10 s"),
            (dict(gate=lambda: {"load": []}), "the load was not read: sd gate status gave no load or load cap"),
            (dict(gate=gate(load5=float("nan"))), "the load was not read: sd gate status gave no load or load cap"),
        ]
        for seams, refusal in cases:
            out = self.plan(**seams)
            self.assertEqual((out["room"], out["start"], out["refusal"]), (0, [], refusal))
            with self.assertRaisesRegex(workflow.WorkflowError, "Nothing queued"):
                self.run_plan([{"id": out["eligible"][0]["id"], "revision": out["eligible"][0]["revision"]}], **seams)
        self.assertEqual((self.launched, self.kicks), ([], []))
        self.assertEqual(self.builds_of(out["eligible"][0]["id"]), [])

    def test_the_gate_load_rule_off_leaves_the_builder_cap(self):
        """sd:9094: the gate's `load_max` of 0 is its own rule off; builders still stop at 1.5x the cores."""
        self.ready("Waiting")
        out = self.plan(gate=gate(load5=41.5, load_max=0.0))
        self.assertEqual((out["room"], out["load_max"], out["refusal"]),
                         (0, 24.0, "the five-minute load is 41.5, above the cap of 24"))

    def test_a_run_queues_a_build_of_each_item_and_starts_no_herdr_session(self):
        """Build step 5's fail-first (pack sd:6096): Start next writes a queued build assignment per item, kickstarts
        the dispatcher, and launches nothing in herdr; Fix alone does (operator, note #29486)."""
        first, second = self.ready("First"), self.ready("Second")
        out = self.run_plan([{"id": row["id"], "revision": row["revision"]} for row in self.plan()["start"]])
        self.assertEqual(self.builds_of(first, second), [(first, "queued"), (second, "queued")])
        self.assertEqual(self.launched, [])
        self.assertEqual([(row["item"], row["status"]) for row in out["queued"]], [(first, "queued"), (second, "queued")])
        self.assertEqual((out["kickstart"]["ok"], self.kicks), (True, [1]))
        self.assertEqual(self.plan()["refusal"], "no ready item is left to start")

    def test_a_moved_item_refuses_the_whole_run_and_queues_nothing(self):
        """The press is `build.queue`'s one transaction: an item changed since the plan is a 409 for all of them."""
        first, second, third = self.ready("First"), self.ready("Second"), self.ready("Third")
        rows = [{"id": row["id"], "revision": row["revision"]} for row in self.plan()["start"]]
        workflow.add_item_note(self.connection, second, kind="comment", who="human", body="changed since the plan")
        status, body = self.run_plan(rows)
        self.assertEqual((status, body["error"]), (409, f"Nothing queued: item {second} changed; refresh the selection."))
        self.assertEqual((self.builds_of(first, second, third), self.kicks, self.launched), ([], [], []))

    def test_nothing_is_queued_when_an_item_left_the_plan_or_more_were_asked_than_fit(self):
        first, second = self.ready("First"), self.ready("Second")
        rows = [{"id": row["id"], "revision": row["revision"]} for row in self.plan()["start"]]
        with self.assertRaisesRegex(workflow.WorkflowError, "1 builders fit now, not 2"):
            self.run_plan(rows, sessions=sessions(running=5))
        workflow.change_status(self.connection, second, "in_progress", who="human",
                               expected_revision=workflow.item_state(self.connection, second)["revision"])
        with self.assertRaisesRegex(workflow.WorkflowError, f"#{second} is not ready"):
            self.run_plan(rows)
        self.assertEqual((self.builds_of(first, second), self.kicks, self.launched), ([], [], []))

    def test_a_run_stopped_before_its_kickstart_leaves_its_items_queued_and_out_of_the_next_plan(self):
        """Steps: the queue commits before the kickstart, so a stop there leaves the rows for the job's next slot,
        and a second run neither queues them twice nor offers them again."""
        first, second = self.ready("First"), self.ready("Second")
        rows = [{"id": row["id"], "revision": row["revision"]} for row in self.plan()["start"]]

        def crash():
            raise Crash("the server stopped")

        with self.assertRaises(Crash):
            self.run_plan(rows, start=crash)
        self.assertEqual(self.builds_of(first, second), [(first, "queued"), (second, "queued")])
        again = self.plan()
        self.assertEqual((again["start"], again["refusal"]), ([], "no ready item is left to start"))
        self.assertEqual({row["id"] for row in again["skipped"]}, {first, second})
        with self.assertRaisesRegex(workflow.WorkflowError, "Nothing queued: no ready item is left to start"):
            self.run_plan(rows)
        self.ready("Third")  # a plan with room still refuses the two queued already, naming why
        with self.assertRaisesRegex(workflow.WorkflowError, rf"Nothing queued: #{first} item {first} has queued assignment"):
            self.run_plan(rows)
        self.assertEqual(self.builds_of(first, second), [(first, "queued"), (second, "queued")])

    def test_a_stalled_or_unread_builder_holds_its_slot_and_its_item(self):
        """Review class (sd:5106 round 1): the real sessions document, with one builder stalled past `STALL` and five
        whose logs hold no record: the five unread hold their slots, the stalled one does not (pack sd:6400), and the
        stalled builder's item is still not offered again."""
        import json
        import os

        from sd_dashboard import claude_sessions
        held, other = self.ready("Held by a stalled builder"), self.ready("Next")
        home = Path(self.tmp.name) / "claude"
        folder = home / "projects" / "-w" / "lead-session-1" / "subagents"
        folder.mkdir(parents=True)
        (home / "projects" / "-w" / "lead-session-1.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "go"}}) + "\n")
        mid_turn = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}) + "\n"
        for n in range(6):
            log = folder / f"agent-a{n}.jsonl"
            log.write_text(mid_turn if n == 0 else "")
            then = 1788696000 - 3 * 3600 - 60 * n  # NOW less 3 h and n minutes: stalled, or unread
            os.utime(log, (then, then))
            (folder / f"agent-a{n}.meta.json").write_text(json.dumps(
                {"customAgentType": "sd-slice-builder", "name": f"builder-{chr(97 + n)}", "description": f"Build sd:{held}" if n == 0 else ""}))
        agents = [{"pid": 1, "cwd": str(self.work), "sessionId": "lead-session-1", "name": "lead", "status": "idle"}]

        def document(connection, *, now):
            return claude_sessions.document(connection, now=now, agents=lambda: agents, processes=list, root=home,
                                            work=lambda path: None, fleet=lambda area: {"worktrees": []})

        out = self.plan(sessions=document)
        # pack sd:6400 (operator): the stalled builder frees its slot; the five unread ones still hold theirs.
        self.assertEqual((out["running"], out["room"]), (5, 1))
        self.assertEqual({row["id"]: row["why"] for row in out["skipped"]}[held], "a live Claude session works on it")
        self.assertEqual([row["id"] for row in out["eligible"]], [other])

    def test_the_post_takes_only_distinct_id_and_revision_pairs_up_to_the_cap(self):
        good = {"id": 1, "revision": "r"}
        self.assertTrue(callable(start_next.request({"items": [good]})))
        for body in ({}, {"items": []}, {"items": [good, good]}, {"items": [{"id": "1", "revision": "r"}]},
                     {"items": [dict(good, id=n) for n in range(1, 8)]}, {"items": [good], "n": 1},
                     {"items": [{"id": 1}]}, {"items": [{"id": True, "revision": "r"}]}):
            with self.assertRaises(ValueError, msg=body):
                start_next.request(body)


if __name__ == "__main__":
    unittest.main()
