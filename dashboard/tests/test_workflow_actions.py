"""Real HTTP mutations on disposable state, including refused-request readback."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import server
from sd_db import database, reads, set_item_fields, upsert_repo, upsert_shadow, workflow
from sd_db.testing import patience
from support import ScreenCase


class BrowserSession(ScreenCase):
    #: Seconds one request waits for the in-process server on a quiet machine; a class whose server runs real
    #: subprocesses sets more. `patience` scales it by the load at the request (sd:6158). The server may wait out
    #: one lock for `BUSY_TIMEOUT` before it answers, so the client waits that and 10 s more: at 5 s, requests that
    #: take 0.16 s idle timed out in gates whose one-minute load read about one per core (sd:7289).
    http_timeout = database.BUSY_TIMEOUT / 1000 + 10

    def setUp(self):
        super().setUp()
        self.listening = server.build(self.path, port=0, operations_backend=getattr(self, "backend", None),
                                      fleet_backend=getattr(self, "fleet_backend", None),
                                      metrics_backend=getattr(self, "metrics_backend", None),
                                      usage_backend=getattr(self, "usage_backend", None))
        thread = threading.Thread(target=self.listening.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.listening.server_close)
        self.addCleanup(self.listening.shutdown)
        host, port = self.listening.server_address[:2]
        self.base = f"http://{host}:{port}"
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = re.search(r'name="sd-csrf" content="([a-f0-9]+)"', body).group(1)

    def request(self, path, payload=None, *, headers=None, method=None):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=headers or {}, method=method)
        try:
            answer = urllib.request.urlopen(request, timeout=patience(self.http_timeout))
        except urllib.error.HTTPError as error:
            answer = error
        with answer:
            return answer.status, answer.headers, answer.read().decode()

    def post(self, path, payload, **headers):
        defaults = {"Content-Type": "application/json", "Origin": self.base,
                    "Cookie": self.cookie, "X-SD-CSRF": self.csrf,
                    "Sec-Fetch-Site": "same-origin"}
        defaults.update(headers)
        status, response_headers, body = self.request(path, payload, headers=defaults)
        return status, response_headers, json.loads(body)

    def snapshot(self):
        return tuple(self.connection.iterdump())

    def unjournalled(self):
        """The snapshot less the execution journal's holder item and the records on it (sd:5541): what a command that
        failed in the POST leaves of its own."""
        from sd_dashboard import command_journal

        db = self.connection
        db.execute("SAVEPOINT unjournalled")
        try:
            held = db.execute("SELECT id FROM item WHERE source=? AND external_id=?", command_journal.HOLDER).fetchone()
            if held:
                db.execute("DELETE FROM note WHERE item=?", held)
                db.execute("DELETE FROM item WHERE id=?", held)
            return tuple(db.iterdump())
        finally:
            db.execute("ROLLBACK TO unjournalled")
            db.execute("RELEASE unjournalled")

    def journalled(self):
        """The execution journal's records, oldest first, as (command, exit code)."""
        return [tuple(row) for row in self.connection.execute(
            "SELECT json_extract(body, '$.command'), exit_code FROM note WHERE kind='exec' ORDER BY id")]


class BrowserActions(BrowserSession):
    def test_local_health_reports_actual_schema_without_writing(self):
        import os

        from sd_db.schema import SCHEMA_VERSION

        before = self.snapshot()
        status, headers, body = self.request("/health")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["service"], "sd-dashboard")
        self.assertEqual(result["schema"], SCHEMA_VERSION)
        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], os.getpid())
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.snapshot(), before)

    def test_complete_task_round_trip_has_no_external_or_repository_work(self):
        with patch("subprocess.run", side_effect=AssertionError("ordinary task called a process")):
            status, _, state = self.post("/api/items", {"title": "Book the review"})
            self.assertEqual(status, 201)
            item = state["item"]["id"]
            self.assertEqual(state["item"]["kind"], "task")
            self.assertIsNone(state["item"]["repo"])
            status, _, state = self.post(f"/api/items/{item}", {
                "revision": state["revision"], "priority": 1, "due": "2026-09-06",
                "body": "Discuss the **results**"})
            self.assertEqual(status, 200)
            status, _, state = self.post(f"/api/items/{item}/status", {
                "revision": state["revision"], "status": "in_progress"})
            self.assertEqual(status, 200)
            self.assertIn(item, [row["id"] for row in reads.today_items(self.connection, now=self.now)])
            status, _, state = self.post(f"/api/items/{item}/notes", {
                "revision": state["revision"], "body": "Bring results", "kind": "followup"})
            self.assertEqual(status, 200)
            note = state["note"]["id"]
            status, _, state = self.post(f"/api/notes/{note}/resolve", {"revision": state["revision"]})
            self.assertEqual(status, 200)
            self.assertTrue(state["note"]["resolved_at"])
            status, _, state = self.post(f"/api/items/{item}/status", {
                "revision": state["revision"], "status": "done"})
            self.assertEqual(status, 200)
            self.assertEqual(reads.item_by_id(self.connection, item)["status"], "done")
            self.assertNotIn(item, [row["id"] for row in reads.today_items(self.connection, now=self.now)])
            status, _, body = self.request(f"/item/{item}")
            self.assertEqual(status, 200)
            self.assertIn("Discuss the <strong>results</strong>", body)
            self.assertIn("Resolved", body)

    def test_completing_a_recurring_task_here_creates_its_next_occurrence(self):
        # sd:1099. The recurrence hook sits in `workflow.change_status`, and
        # this route is one of its two callers; if the route ever reaches
        # `transition` directly, this is the test that notices.
        state = workflow.capture_task(self.connection, title="Annual report", due="2026-04-30",
                                      recurrence="FREQ=YEARLY", who="operator")
        item = state["item"]["id"]
        status, _, state = self.post(f"/api/items/{item}/status", {
            "revision": state["revision"], "status": "done"})
        self.assertEqual(status, 200)
        spawned = reads.item_by_id(self.connection, state["next_occurrence"])
        self.assertEqual((spawned["due"], spawned["status"], spawned["recurrence"]),
                         ("2027-04-30", "planning", "FREQ=YEARLY"))
        self.assertIsNone(reads.item_by_id(self.connection, item)["recurrence"])

    def test_done_on_a_branch_item_with_no_merge_is_refused_by_name_and_a_reason_closes_it(self):
        # sd:2570. The library refuses the close (sd:1990's rule); the route
        # answers 400 with the sentence, not a 500, and the row stays open.
        state = workflow.capture_task(self.connection, title="Fleet work", who="operator")
        item = state["item"]["id"]
        self.connection.execute("UPDATE item SET branch = 'fleet/fixture-sd1' WHERE id = ?", (item,))
        self.connection.commit()
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, body = self.post(f"/api/items/{item}/status", {"revision": revision, "status": "done"})
        self.assertEqual(status, 400)
        self.assertIn("worked on branch fleet/fixture-sd1, and no merge of it is recorded", body["error"])
        self.assertEqual(reads.item_by_id(self.connection, item)["status"], "planning")
        status, _, state = self.post(f"/api/items/{item}/status", {
            "revision": revision, "status": "done", "reason": "superseded by another merge"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["status"], "done")

    def test_stale_browser_does_not_overwrite_a_newer_edit(self):
        state = workflow.capture_task(self.connection, title="Original", who="operator")
        item = state["item"]["id"]
        workflow.edit_item(self.connection, item, {"title": "From the CLI"},
                           expected_revision=state["revision"], who="operator")
        before = self.snapshot()
        status, _, result = self.post(f"/api/items/{item}", {
            "revision": state["revision"], "title": "Old tab"})
        self.assertEqual(status, 409)
        self.assertTrue(result["reload"])
        self.assertEqual(self.snapshot(), before)

    def test_each_security_failure_refuses_without_a_write(self):
        before = self.snapshot()
        for override in ({"Origin": "https://evil.invalid"}, {"Origin": ""},
                         {"Cookie": ""}, {"Cookie": "sd_session=broken"},
                         {"X-SD-CSRF": "0" * 64}, {"X-SD-CSRF": "é"},
                         {"Sec-Fetch-Site": "cross-site"},
                         {"Host": "attacker.invalid"}):
            with self.subTest(override=override):
                status, headers, _ = self.post("/api/items", {"title": "Forbidden"}, **override)
                self.assertEqual(status, 403)
                self.assertEqual(headers["Content-Security-Policy"], server.CSP)
                self.assertEqual(self.snapshot(), before)

    def test_token_is_bound_to_the_session_not_just_a_random_header(self):
        _, headers, body = self.request("/")
        second_cookie = headers["Set-Cookie"].split(";", 1)[0]
        second_token = re.search(r'name="sd-csrf" content="([a-f0-9]+)"', body).group(1)
        self.assertNotEqual(second_token, self.csrf)
        self.assertEqual(self.post("/api/items", {"title": "Forbidden"}, Cookie=second_cookie)[0], 403)

    def test_expired_session_is_refused(self):
        with patch("sd_dashboard.server.time.time", return_value=0):
            _, headers, body = self.request("/")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        token = re.search(r'name="sd-csrf" content="([a-f0-9]+)"', body).group(1)
        self.assertEqual(self.post("/api/items", {"title": "Forbidden"},
                                  Cookie=cookie, **{"X-SD-CSRF": token})[0], 403)

    def test_invalid_fields_and_missing_revisions_leave_state_unchanged(self):
        state = workflow.capture_task(self.connection, title="Keep", who="operator")
        item = state["item"]["id"]
        before = self.snapshot()
        for path, payload in (
            ("/api/items", {"title": "x", "command": "rm -rf"}),
            ("/api/items", {"title": ""}),
            ("/api/items", {"title": "x", "priority": True}),
            (f"/api/items/{item}", {"title": "missing revision"}),
            (f"/api/items/{item}", {"revision": state["revision"], "status": "done"}),
            (f"/api/items/{item}/status", {"revision": state["revision"], "status": "made_up"}),
        ):
            self.assertEqual(self.post(path, payload)[0], 400, (path, payload))
            self.assertEqual(self.snapshot(), before)

    def test_get_action_and_other_write_verbs_do_not_write(self):
        before = self.snapshot()
        self.assertEqual(self.request("/api/items?title=must-not-create")[0], 404)
        self.assertEqual(self.request("/api/items", method="DELETE")[0], 501)
        self.assertEqual(self.snapshot(), before)

    def test_non_loopback_bind_is_refused(self):
        with self.assertRaises(ValueError):
            server.build(self.path, port=0, host="0.0.0.0")

    def test_control_forms_have_revisions_and_user_text_is_escaped(self):
        state = workflow.capture_task(self.connection, title="<script>bad</script>", body="<img onerror=bad>", who="operator")
        item = state["item"]["id"]
        status, headers, body = self.request(f"/item/{item}")
        self.assertEqual(status, 200)
        self.assertIn(f'name="revision" value="{state["revision"]}"', body)
        self.assertNotIn("<script>bad", body)
        self.assertNotIn("<img onerror", body)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn('<label for="field-priority">Priority</label>', body)
        self.assertIn('<select name="priority" id="field-priority">', body)
        identifiers = re.findall(r'\bid="([^"]+)"', body)
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_relink_and_cancel_keep_identity_and_leave_artifacts_intact(self):
        root = Path(self.tmp.name) / "repo"
        root.mkdir()
        original = root / "original.md"
        moved = root / "archived.md"
        original.write_text("Original content")
        moved.write_text("Archived content")
        upsert_repo(self.connection, str(root))
        item = self.item("Work to stop", repo=str(root), path="original.md",
                         source="fixture", external_id="stable-source-identity")
        state = workflow.item_state(self.connection, item)
        self.assertEqual(self.post(f"/api/items/{item}/relink", {
            "revision": state["revision"], "path": "../outside.md"})[0], 400)
        status, _, state = self.post(f"/api/items/{item}/relink", {
            "revision": state["revision"], "path": "archived.md"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["path"], "archived.md")
        self.assertEqual(state["item"]["external_id"], "stable-source-identity")
        status, _, state = self.post(f"/api/items/{item}/cancel", {
            "revision": state["revision"], "reason": "No longer needed"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["status"], "done")
        self.assertIsNone(state["item"]["shipped_at"])
        self.assertEqual(json.loads(state["item"]["fields"])["completion"]["outcome"], "cancelled")
        self.assertEqual(original.read_text(), "Original content")
        self.assertEqual(moved.read_text(), "Archived content")
        self.assertEqual(self.post(f"/api/items/{item}/deliver", {"revision": state["revision"]})[0], 404)

    def test_cancel_task_closes_a_task_with_its_reason_under_its_revision(self):
        # sd:3012: the Tasks page's Close is `sd task cancel`: cancel_work with task_guard, the row's revision and a reason.
        task, work = self.item("Nobody will do this", kind="task"), self.item("Work item", kind="work")
        old = workflow.item_state(self.connection, task)["revision"]
        self.assertEqual(self.post(f"/api/items/{task}/cancel-task", {"revision": old})[0], 400)
        status, _, state = self.post(f"/api/items/{task}/cancel-task", {"revision": old, "reason": "Superseded"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["status"], "done")
        completion = json.loads(state["item"]["fields"])["completion"]
        self.assertEqual((completion["outcome"], completion["reason"]), ("cancelled", "Superseded"))
        followup = self.item("A followup", kind="followup")
        before = workflow.item_state(self.connection, followup)["revision"]
        workflow.edit_item(self.connection, followup, {"priority": 1}, who="elsewhere")
        status, _, answer = self.post(f"/api/items/{followup}/cancel-task", {"revision": before, "reason": "Stale"})
        self.assertEqual((status, answer.get("reload")), (409, True))
        self.assertEqual(workflow.item_state(self.connection, followup)["item"]["status"], "planning")
        revision = workflow.item_state(self.connection, work)["revision"]
        self.assertEqual(self.post(f"/api/items/{work}/cancel-task", {"revision": revision, "reason": "No"})[0], 400)

    def test_active_assignments_do_not_offer_cancel(self):
        self.repo()
        item = self.item("Row-owned", repo="/repos/system")
        self.assertIn(f'action="/api/items/{item}/cancel"', self.request(f"/item/{item}")[2])
        self.assignment(item, status="running")
        body = self.request(f"/item/{item}")[2]
        self.assertNotIn(f'action="/api/items/{item}/cancel"', body)
        self.assertNotIn(f'action="/api/items/{item}/relink"', body)

    def test_external_state_and_unsafe_links_do_not_control_local_progress(self):
        state = workflow.capture_task(self.connection, title="Local task", who="operator")
        item = state["item"]["id"]
        set_item_fields(self.connection, item, source="github", external_id="javascript:bad()")
        upsert_shadow(self.connection, tracker="github", url="javascript:bad()", state="open")
        body = self.request(f"/item/{item}")[2]
        self.assertIn("External context", body)
        self.assertIn("Last successful sync", body)
        self.assertNotIn('href="javascript:', body)
        state = workflow.item_state(self.connection, item)
        status, _, state = self.post(f"/api/items/{item}/status", {
            "revision": state["revision"], "status": "done"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["status"], "done")
        self.assertEqual(reads.item_shadow(self.connection, item)["state"], "open")

    def test_title_only_edit_preserves_existing_priority(self):
        upsert_repo(self.connection, "/repos/system")
        item = self.item("Original", repo="/repos/system", priority=0)
        state = workflow.item_state(self.connection, item)
        body = self.request(f"/item/{item}")[2]
        self.assertIn('value="0" selected>0 · Existing priority', body)
        self.assertIn("data-changed-fields", body)
        status, _, state = self.post(f"/api/items/{item}", {
            "revision": state["revision"], "title": "Changed title"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["priority"], 0)


class Unparking(BrowserSession):
    """sd:3007: the item page unparks an item the nightly prune parked, under its revision."""

    def parked(self, **columns):
        item = self.item("Old idea", priority=4, **columns)
        set_item_fields(self.connection, item, parked_at="2026-10-09T08:10:00+00:00")
        self.connection.commit()
        return workflow.item_state(self.connection, item)

    def unpark_form(self, item):
        _, _, page = self.request(f"/item/{item}")
        return re.search(rf'<form[^>]*action="/api/items/{item}"[^>]*data-cli="sd task edit {item} --unpark"[^>]*>(.*?)</form>',
                         page, re.S)

    def test_a_parked_item_offers_unpark_with_its_revision(self):
        for kind in ("task", "work-idea"):
            with self.subTest(kind=kind):
                state = self.parked(kind=kind)
                form = self.unpark_form(state["item"]["id"])
                self.assertIsNotNone(form, "no Unpark form on the parked item's page")
                self.assertIn('<input type="hidden" name="parked_at" value="">', form.group(1))
                self.assertIn(f'name="revision" value="{state["revision"]}"', form.group(1))
        self.assertIsNone(self.unpark_form(self.item("Open", kind="task")))

    def test_the_post_unparks_and_a_stale_revision_writes_nothing(self):
        state = self.parked(kind="task")
        item = state["item"]["id"]
        workflow.add_item_note(self.connection, item, body="Touched elsewhere", who="operator")
        before = self.snapshot()
        status, _, result = self.post(f"/api/items/{item}", {"revision": state["revision"], "parked_at": None})
        self.assertEqual((status, result["reload"]), (409, True))
        self.assertEqual(self.snapshot(), before)
        fresh = workflow.item_state(self.connection, item)
        status, _, result = self.post(f"/api/items/{item}", {"revision": fresh["revision"], "parked_at": None})
        self.assertEqual(status, 200)
        self.assertIsNone(workflow.item_state(self.connection, item)["item"]["parked_at"])
        self.assertIn(item, [row["id"] for row in reads.backlog_items(self.connection, now="2026-10-09T12:00:00Z")])

    def test_the_form_script_sends_a_blank_parked_at_as_null(self):
        script = (Path(server.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
        blank = re.search(r'\[([^\]]*)\]\.forEach\(function \(key\) \{\s*if \(Object\.prototype\.hasOwnProperty\.call\(values, key\) && values\[key\] === ""\)', script)
        self.assertIsNotNone(blank)
        self.assertIn('"parked_at"', blank.group(1))


class TheWaitBudget(BrowserSession):
    def test_a_request_waits_longer_on_a_loaded_machine(self):
        """sd:6158: a fixed 5 s timed requests out at gate load 49-99; the budget grows with the load per core."""
        seen = []
        real = urllib.request.urlopen

        def urlopen(request, timeout):
            seen.append(timeout)
            return real(request, timeout=timeout)

        with patch("os.getloadavg", return_value=(80.0, 80.0, 80.0)), patch("os.cpu_count", return_value=16), \
                patch.object(urllib.request, "urlopen", urlopen):
            self.assertEqual(self.request("/health")[0], 200)
        self.assertEqual(seen, [self.http_timeout * 5])

    def test_a_request_outwaits_one_lock_the_server_waits_out(self):
        """sd:7289: a server statement may wait `BUSY_TIMEOUT` for a lock and still answer; on a machine whose
        load reads quiet, the client waits for that answer instead of timing out first."""
        held = database.BUSY_TIMEOUT / 1000 + 1

        def slow(*_, **__):
            def action(connection):
                time.sleep(held)
                return {"ok": True}
            return action

        with patch("os.getloadavg", return_value=(0.0, 0.0, 0.0)), patch.object(server, "action_route", slow):
            status, _, answer = self.post("/api/items", {"title": "held"})
        self.assertEqual((status, answer), (201, {"ok": True}))
        self.assertGreater(self.http_timeout, held)


class SettingAnArea(BrowserSession):
    """pack sd:8021: Task Details' Area select posts `{area}` to the item edit route, as `sd task edit
    --area` and `--clear-area` do; the Tasks rows then carry the item's own area and `area_set`."""

    def row(self, item):
        status, _, body = self.request("/api/tasks", headers={"Cookie": self.cookie, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        return next(r for r in json.loads(body)["rows"] if r["id"] == item)

    def test_the_route_sets_and_clears_the_area_and_the_tasks_row_shows_it(self):
        item = self.item("Clean the gutters", kind="task")
        self.connection.commit()
        self.assertFalse(self.row(item)["area_set"])
        for area in ("home", "hoa", None):
            with self.subTest(area=area):
                state = workflow.item_state(self.connection, item)
                status, _, result = self.post(f"/api/items/{item}", {"revision": state["revision"], "area": area})
                self.assertEqual(status, 200, result)
                row = self.row(item)
                self.assertEqual(row["area_set"], area is not None)
                if area:
                    self.assertEqual(row["area"], area)
        state = workflow.item_state(self.connection, item)
        status, _, result = self.post(f"/api/items/{item}", {"revision": state["revision"], "area": "garage"})
        self.assertEqual(status, 400, result)
