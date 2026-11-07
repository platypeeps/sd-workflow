"""HTTP operations proof using an in-memory observer; no launchctl calls."""

import http.client
import subprocess

from sd_db import operations, record_state, workflow
from sd_db.workflow import WorkflowError

from .test_workflow_actions import BrowserSession


class FakeBackend:
    def __init__(self):
        self.states = {"failed-fixture": "failed", "running-fixture": "running", "idle-fixture": "idle",
                       "interrupted-fixture": "interrupted", "crashed-fixture": "failed"}
        # A last run that ended in a signal has no exit code; launchd records the
        # signal instead (sd:1344). SIGSEGV is the kernel's verdict, so that job
        # is failed; SIGKILL is a stop nobody here sent, so that one is
        # interrupted. Both carry the reason the parser writes.
        self.signals = {"interrupted-fixture": 9, "crashed-fixture": 11}
        # The launchd lifetime a job's run counter belongs to; a reload moves it.
        self.lifetimes = {}
        self.calls = []

    def names(self):
        return list(self.states)

    def inspect(self, name):
        if name not in self.states:
            raise WorkflowError("Unknown fixture job")
        state, signal = self.states[name], self.signals.get(name)
        return {"name": name, "label": "fixture." + name, "service": "fixture/" + name,
                "schedule": [{"Hour": 9, "Minute": 30}], "state": state,
                "pid": 123 if state == "running" else None,
                "last_exit": None if signal is not None else 1 if state == "failed" else 0,
                "last_signal": signal, "runs": 1, "lifetime": self.lifetimes.get(name, 47763), "boot": 1789568182,
                "reason": {9: "last run ended in SIGKILL (9)", 11: "last run ended in SIGSEGV (11)",
                           15: "last run ended in SIGTERM (15)"}.get(signal, ""),
                "_observation": name + state}

    def perform(self, action, service):
        self.calls.append((action, service))
        return subprocess.CompletedProcess([], 0, "", "")


class OperationsActions(BrowserSession):
    def setUp(self):
        self.backend = FakeBackend()
        super().setUp()

    def test_the_item_page_offers_cancel_only_where_the_library_allows_it(self):
        # The jobs and assignments area was retired in sd:3402; an assignment's cancel form stays on its item's page.
        task = workflow.capture_task(self.connection, title="Delegated fixture", who="operator")["item"]["id"]
        queued = self.assignment(task, status="queued")
        running = self.assignment(task, status="running")
        before = self.snapshot()
        status, _, page = self.request(f"/item/{task}")
        self.assertEqual(status, 200)
        self.assertIn(f'action="/api/assignments/{queued}/cancel"', page)
        self.assertNotIn(f'action="/api/assignments/{running}/cancel"', page)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.calls, [])

    def test_a_signal_ended_job_keeps_its_signal_and_only_our_cancel_reads_idle(self):
        """A job whose last run ended in a signal has no exit code; its state names
        the signal. A crashed job and a SIGKILL'd one need attention with retry
        open and stop refused for the usual reason, not for the signal. Only a
        run this dashboard's own accepted cancel ended reads Idle (sd:1344).
        The classic Jobs card that drew this was retired in sd:3402; the state
        is what Management reads."""
        def job(name):
            return operations.job_state(self.connection, name, backend=self.backend)

        for name, state, signal in (("crashed-fixture", "failed", 11), ("interrupted-fixture", "interrupted", 9)):
            with self.subTest(job=name):
                observed = job(name)
                self.assertEqual((observed["state"], observed["last_signal"], observed["last_exit"]), (state, signal, None))
                self.assertTrue(observed["capabilities"]["retry"]["allowed"])
                self.assertFalse(observed["capabilities"]["cancel"]["allowed"])
                self.assertIn("only a currently running job can cancel", observed["capabilities"]["cancel"]["reason"])
        self.assertEqual((job("failed-fixture")["last_exit"], job("failed-fixture")["last_signal"]), (1, None))
        # Cancel the running job, then observe launchd's SIGTERM line on that
        # same run: the stop was meant, so the job is Idle. An idle job may run
        # now (sd:4982), so its retry is allowed.
        state = job("running-fixture")
        self.assertEqual(self.post("/api/jobs/running-fixture/cancel", {"revision": state["revision"]})[0], 200)
        self.backend.states["running-fixture"], self.backend.signals["running-fixture"] = "interrupted", 15
        cancelled = job("running-fixture")
        self.assertEqual((cancelled["state"], cancelled["last_signal"]), ("idle", 15))
        self.assertTrue(cancelled["capabilities"]["retry"]["allowed"])
        # Reload the label (a new lifetime, runs = 1 again) and observe an
        # external SIGTERM on the new run 1: the old cancel is no evidence for
        # it, so the job is Interrupted with its retry.
        self.backend.lifetimes["running-fixture"] = 47790
        reloaded = job("running-fixture")
        self.assertEqual(reloaded["state"], "interrupted")
        self.assertTrue(reloaded["capabilities"]["retry"]["allowed"])
        self.assertEqual(self.backend.calls, [("cancel", "fixture/running-fixture")])

    def test_operations_keeps_no_area_and_lists_where_each_went(self):
        # sd:3402 retired Jobs, Ports, Progress, Repos, Reports and Commands; pack sd:7517 moved Services and
        # Sessions to Management (test_v2_management), pack sd:7524 Usage to Metrics (test_v2_metrics), pack sd:7527
        # Resources' views to Management, Notes and Tasks, and pack sd:7525 Trackers to GitHub (test_trackers_screen).
        status, _, page = self.request("/operations")
        self.assertEqual(status, 200)
        self.assertIn('<a href="/github#view=tracked" data-view="trackers">', page)
        self.assertNotIn('id="view-', page)
        for area in ("unknown", "jobs", "progress", "ports"):
            self.assertEqual(self.request(f"/operations?area={area}")[0], 404, area)
        self.assertEqual(self.backend.calls, [])

    def test_a_moved_area_address_answers_301_to_its_management_view(self):
        """pack sd:7517: a retired area's bookmark lands on the view that holds it now."""
        host, port = self.listening.server_address[:2]
        # pack sd:7524: Usage moved to Metrics' Spend view, and the month its picker named rides along.
        for area, target in (("services", "/management#view=deploys"), ("sessions", "/management#view=sessions"),
                             ("usage", "/metrics#view=spend"), ("usage&month=2026-08", "/metrics?month=2026-08#view=spend"),
                             ("usage&month=soon", "/metrics#view=spend"),
                             # pack sd:7527: each Resources view moved to the page that holds it now.
                             ("resources&resource=toolbox", "/management#view=machine"),
                             ("resources&resource=research", "/management?research=yes#view=repos"),
                             ("resources&resource=vault", "/notes"), ("resources&resource=queues", "/today"),
                             ("resources", "/management#view=machine"), ("resources&resource=ports", "/management#view=machine"),
                             # pack sd:7525: Trackers moved to GitHub's Tracked view.
                             ("trackers", "/github#view=tracked"), ("trackers&view=issues", "/github#view=tracked")):
            with self.subTest(area=area):
                connection = http.client.HTTPConnection(host, port, timeout=10)
                try:
                    connection.request("GET", f"/operations?area={area}", headers={"Cookie": self.cookie})
                    answer = connection.getresponse()
                    answer.read()
                finally:
                    connection.close()
                self.assertEqual((answer.status, answer.headers["Location"]), (301, target))
        self.assertEqual(self.backend.calls, [])

    def test_retry_acceptance_is_distinct_from_observed_job_state(self):
        state = operations.job_state(self.connection, "failed-fixture", backend=self.backend)
        status, _, result = self.post("/api/jobs/failed-fixture/retry", {"revision": state["revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["request"]["status"], "accepted")
        self.assertEqual(result["job"]["state"], "failed")
        self.assertEqual(self.backend.calls, [("retry", "fixture/failed-fixture")])
        before = self.snapshot()
        self.assertEqual(self.post("/api/jobs/failed-fixture/retry", {"revision": state["revision"]})[0], 409)
        self.assertEqual(self.post("/api/jobs/failed-fixture/retry", {"revision": result["job"]["revision"]})[0], 400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(operations.job_state(self.connection, "failed-fixture", backend=self.backend)["last_request"]["status"], "accepted")

    def test_restore_blocks_retry_but_running_cancel_remains_available(self):
        record_state(self.connection, "restore", key="fixture-restore")
        state = operations.job_state(self.connection, "failed-fixture", backend=self.backend)
        self.assertEqual(self.post("/api/jobs/failed-fixture/retry", {"revision": state["revision"]})[0], 400)
        running = operations.job_state(self.connection, "running-fixture", backend=self.backend)
        self.assertEqual(self.post("/api/jobs/running-fixture/cancel", {"revision": running["revision"]})[0], 200)
        self.assertEqual(self.backend.calls, [("cancel", "fixture/running-fixture")])

    def test_queued_cancel_leaves_item_status_unchanged_and_stale_retry_refuses(self):
        task = workflow.capture_task(self.connection, title="Assignment target", who="operator")["item"]["id"]
        queued = self.assignment(task, status="queued")
        state = operations.assignment_state(self.connection, queued)
        status, _, result = self.post(f"/api/assignments/{queued}/cancel", {"revision": state["revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(workflow.item_state(self.connection, task)["item"]["status"], "planning")
        before = self.snapshot()
        self.assertEqual(self.post(f"/api/assignments/{queued}/cancel", {"revision": state["revision"]})[0], 409)
        self.assertEqual(self.snapshot(), before)

    def test_forged_actions_and_cross_origin_requests_dispatch_nothing(self):
        before = self.snapshot()
        state = operations.job_state(self.connection, "failed-fixture", backend=self.backend)
        for path, payload in (
            ("/api/jobs/failed-fixture/retry", {"revision": state["revision"], "command": "custom"}),
            ("/api/jobs/unknown/retry", {"revision": state["revision"]}),
            ("/api/jobs/failed-fixture/retry", {}),
            ("/api/jobs/failed-fixture/run", {"revision": state["revision"]}),
        ):
            self.assertIn(self.post(path, payload)[0], (400, 404))
        self.assertEqual(self.post("/api/jobs/failed-fixture/retry", {"revision": state["revision"]},
                                  Origin="https://other.invalid")[0], 403)
        self.assertEqual(self.request("/api/jobs/failed-fixture/retry")[0], 404)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.calls, [])
