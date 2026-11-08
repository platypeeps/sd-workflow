"""The v2 Home page (sd:2117), with live Home Assistant state (sd:3427).

What this slice promises: `/home` answers under the shared policy and loads
its script before `shell.js`; `/api/home` lists the tiles the config folder's
`home-tiles.conf` names (the entity ids describe a house, so the public
checkout carries only `home-tiles.conf.example`), names every line it could
not read, and gives each tile its live state and last change from one
`GET /api/states` (sd:3427), with the URL and token local-ha-mcp uses. A
failed read (not configured, down, refused, too slow, redirected) or an entity
Home Assistant does not list is a named error on the tile, and the page still
draws. The page registers the design's Home commands with the design's ids,
labels, keys and risks, and keeps each one off: it sends nothing.

The reader tests talk to a stub server on 127.0.0.1 with synthetic entity ids
and a synthetic token; every other test runs with no Home Assistant settings
(`setUpModule`), so no test reaches a real one.

`home.js` runs under JavaScriptCore (osascript) against the stand-in page and
shell `test_v2_tasks` uses. The browser half -- the look at 375 px, the wall
display, focus -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import html
import json
import os
import re
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from sd_dashboard import home_screen, server, v2
from sd_db import writes
from support import NOW, ScreenCase
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
HOME_JS = (V2 / "static" / "home.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
KIOSK_JS = (V2 / "static" / "home-kiosk.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
EXAMPLE = Path(home_screen.__file__).resolve().parents[1] / "home-tiles.conf.example"
#: A token value no page output may ever hold.
TOKEN = "tok-7f3a9c-never-shown"

#: A synthetic Home Assistant token, sent only to the stub.
HA_TOKEN = "ha-tok-5e1d-synthetic"


def setUpModule():
    """No test here reads this machine's Home Assistant settings: an empty config folder and no HA_* variables."""
    global _CONFIG, _ENV
    _CONFIG = tempfile.TemporaryDirectory()
    _ENV = mock.patch.dict("os.environ", {"SYSTEM_TOOLS_CONFIG": _CONFIG.name})
    _ENV.start()
    for name in ("HA_TOKEN", "HA_MCP_URL"):
        os.environ.pop(name, None)


def tearDownModule():
    _ENV.stop()
    _CONFIG.cleanup()


class Stub:
    """A Home Assistant stand-in on 127.0.0.1: records each request and answers as `answer(path)` says, or
    `answer(path, "POST")` for a service call, whose body it records too."""

    def __init__(self, answer):
        calls = self.calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.path, self.headers.get("Authorization")))
                self.reply(*answer(self.path))

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                calls.append(("POST " + self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(length) or b"null")))
                self.reply(*answer(self.path, "POST"))

            def reply(self, status, body, headers):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    if "Content-Length" not in headers:
                        self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def entity(entity_id, state, changed="2026-09-06T11:58:00+00:00", **attributes):
    return {"entity_id": entity_id, "state": state, "last_changed": changed, "last_updated": changed,
            "attributes": {"friendly_name": entity_id, **attributes}}


BACKUP = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")

#: Synthetic states for the example's entities; binary_sensor.smoke_sensors is left out on purpose.
STATES = [
    entity("binary_sensor.problems_active", "off"),
    entity("alarm_control_panel.house", "armed_home", "2026-09-06T10:25:00+00:00"),
    entity("lock.front_door", "locked", "2026-09-06T11:40:00+00:00"),
    entity("binary_sensor.water_leak", "on", "2026-09-06T11:59:00+00:00", device_class="moisture"),
    entity("binary_sensor.wan_degraded", "unavailable"),
    # The page judges a backup's age by the browser's clock, so this one is an hour before the run, not before NOW.
    entity("sensor.backup_last_successful_automatic_backup", BACKUP, BACKUP, device_class="timestamp"),
    entity("input_boolean.guest_mode", "off"),
    entity("binary_sensor.virtual_porch_light", "on", entity_picture="/api/camera_proxy/x?token=pic-secret"),
    entity("sensor.unlisted", "12"),
]


#: Home's commands (sd:4966): one per service the server's allowlist names, on its domain, and the sensor's copy-only read.
#: id, object type, label, key, risk.
COMMANDS = [
    *[[f"{d}.{svc}", d, label, key, "undo"] for d in ("light", "switch", "fan", "input_boolean")
      for svc, label, key in (("turn_on", "Turn on", "o"), ("turn_off", "Turn off", "f"), ("toggle", "Toggle", "t"))],
    ["scene.turn_on", "scene", "Activate", "a", "safe"],
    ["script.turn_on", "script", "Run", "r", "safe"],
    ["button.press", "button", "Press", "p", "safe"],
    ["lock.unlock", "lock", "Unlock", "u", "safe"],
    ["lock.lock", "lock", "Lock", "l", "safe"],
    ["alarm_control_panel.alarm_disarm", "alarm_control_panel", "Disarm", "d", "safe"],
    ["alarm_control_panel.alarm_arm_home", "alarm_control_panel", "Arm home", "h", "safe"],
    ["alarm_control_panel.alarm_arm_away", "alarm_control_panel", "Arm away", "w", "safe"],
    ["alarm_control_panel.alarm_arm_night", "alarm_control_panel", "Arm night", "n", "safe"],
    ["cover.open_cover", "cover", "Open", "o", "safe"],
    ["cover.close_cover", "cover", "Close", "x", "safe"],
    ["sensor.read", "sensor", "Read state", "s", "safe"],
]


class TheDocument(ScreenCase):
    """`home_screen.document` reads the tile list; with no Home Assistant settings it names that and calls nothing."""

    def setUp(self):
        super().setUp()
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def conf(self, text):
        path = self.dir / "home-tiles.conf"
        path.write_text(text, encoding="utf-8")
        return path

    def test_no_config_is_named_and_lists_nothing(self):
        doc = home_screen.document(self.connection, now=NOW, config=self.dir / "home-tiles.conf")
        self.assertEqual({key: doc["config"][key] for key in ("state", "source", "problems")},
                         {"state": "missing", "source": "<config>/project-dashboard/home-tiles.conf", "problems": []})
        self.assertEqual((doc["headline"], doc["groups"], doc["read"]), (None, [], NOW))
        self.assertEqual(doc["reader"], {"available": False, "reason": home_screen.NO_TILES})

    def test_the_example_reads_as_a_headline_and_groups_in_file_order(self):
        doc = home_screen.document(self.connection, now=NOW, config=EXAMPLE)
        self.assertEqual((doc["config"]["state"], doc["config"]["problems"]), ("read", []))
        self.assertEqual(doc["headline"], {"id": "binary_sensor.problems_active", "name": "Problems active", "domain": "binary_sensor", "type": None,
                                           "state": None, "error": "HA not configured"})
        self.assertEqual([g["name"] for g in doc["groups"]], ["Security", "Water & safety", "Network & backup", "Toggles"])
        self.assertEqual([t["id"] for t in doc["groups"][0]["tiles"]], ["alarm_control_panel.house", "lock.front_door"])
        # The optional fifth field types a tile the domain would not: a virtual switch is a toggle.
        self.assertEqual([(t["id"], t["type"]) for t in doc["groups"][3]["tiles"]],
                         [("input_boolean.guest_mode", None), ("binary_sensor.virtual_porch_light", "toggle")])
        self.assertFalse(doc["reader"]["available"])

    def test_a_line_it_cannot_read_is_named_and_the_rest_still_count(self):
        doc = home_screen.document(self.connection, now=NOW, config=self.conf("\n".join([
            "# a comment",
            "tile|Security|lock.front_door|Front door",
            "tile|Security|Not An Id|Bad",
            "tile|Security|lock.back_door",
            "bogus|lock.side_door|Side",
            "tile|Security|lock.front_door|Again",
            "headline|binary_sensor.problems_active|Problems",
            "headline|binary_sensor.other|Second",
            "tile||lock.gate|Gate",
            "tile|Security|lock.shed|Shed|door",
            "tile|Security|switch.fan|Fan|toggle",
        ])))
        self.assertEqual(doc["config"]["state"], "partial")
        self.assertEqual([p.split(":", 1)[0] for p in doc["config"]["problems"]],
                         ["line 3", "line 4", "line 5", "line 6", "line 8", "line 9", "line 10"])
        self.assertIn("type 'door' is not one of alarm, lock, toggle, sensor", doc["config"]["problems"][6])
        self.assertIn("is listed twice", doc["config"]["problems"][3])
        self.assertIn("a second headline", doc["config"]["problems"][4])
        unread = {"state": None, "error": "HA not configured"}
        self.assertEqual(doc["groups"], [{"name": "Security", "tiles": [
            {"id": "lock.front_door", "name": "Front door", "domain": "lock", "type": None, **unread},
            {"id": "switch.fan", "name": "Fan", "domain": "switch", "type": "toggle", **unread}]}])
        self.assertEqual(doc["headline"]["id"], "binary_sensor.problems_active")


class TheLiveRead(ScreenCase):
    """sd:3427: one GET /api/states, with local-ha-mcp's URL and token; every failure is a named error and the tiles still list."""

    def setUp(self):
        super().setUp()
        self.config = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def stub(self, answer=None):
        stub = Stub(answer or (lambda path: (200, STATES, {})))
        self.addCleanup(stub.close)
        return stub

    def env(self, url, token=HA_TOKEN, **more):
        return {"SYSTEM_TOOLS_CONFIG": str(self.config), "HA_TOKEN": token, "HA_MCP_URL": f"{url}/mcp_server/sse", **more}

    def doc(self, environ, timeout=2.0):
        return home_screen.document(self.connection, now=NOW, config=EXAMPLE, environ=environ, timeout=timeout)

    def tile(self, doc, entity_id):
        tiles = [doc["headline"]] + [t for g in doc["groups"] for t in g["tiles"]]
        return next(t for t in tiles if t["id"] == entity_id)

    def test_each_tile_carries_its_state_and_last_change_from_one_read(self):
        stub = self.stub()
        doc = self.doc(self.env(stub.url))
        self.assertEqual(stub.calls, [("/api/states", f"Bearer {HA_TOKEN}")])
        self.assertEqual(doc["reader"]["available"], True)
        self.assertEqual(self.tile(doc, "lock.front_door")["state"],
                         {"value": "locked", "changed": "2026-09-06T11:40:00+00:00", "device_class": None, "unit": None})
        self.assertEqual(self.tile(doc, "binary_sensor.water_leak")["state"]["device_class"], "moisture")
        self.assertIsNone(self.tile(doc, "lock.front_door")["error"])
        self.assertEqual(self.tile(doc, "binary_sensor.problems_active")["state"]["value"], "off")
        # An entity Home Assistant does not list is named on its tile; the others still read.
        self.assertEqual((self.tile(doc, "binary_sensor.smoke_sensors")["state"], self.tile(doc, "binary_sensor.smoke_sensors")["error"]),
                         (None, home_screen.NOT_LISTED))
        text = json.dumps(doc)
        for secret in (HA_TOKEN, "pic-secret", "127.0.0.1", "sensor.unlisted"):
            self.assertNotIn(secret, text)

    def test_a_script_or_automation_tile_carries_its_last_run(self):
        """sd:5727: a script's or automation's last run is Home Assistant's last_triggered; a tile of another domain has none."""
        config = self.config / "home-tiles.conf"
        config.write_text("headline|binary_sensor.problems_active|Problems\ntile|Routines|script.good_night|Good night\ntile|Routines|automation.good_night|Good night rule\n"
                          "tile|Routines|script.never_ran|Never ran\ntile|Security|lock.front_door|Front door\n", encoding="utf-8")
        states = STATES + [entity("script.good_night", "off", "2026-10-22T04:39:30+00:00", last_triggered="2026-10-22T04:39:00+00:00"),
                           entity("automation.good_night", "on", last_triggered="2026-09-19T04:35:00+00:00"),
                           entity("script.never_ran", "off", last_triggered=None)]
        stub = self.stub(lambda path: (200, states, {}))
        doc = home_screen.document(self.connection, now=NOW, config=config, environ=self.env(stub.url))
        self.assertEqual(self.tile(doc, "script.good_night")["state"]["ran"], "2026-10-22T04:39:00+00:00")
        self.assertEqual(self.tile(doc, "automation.good_night")["state"]["ran"], "2026-09-19T04:35:00+00:00")
        self.assertIsNone(self.tile(doc, "script.never_ran")["state"]["ran"])
        self.assertNotIn("ran", self.tile(doc, "lock.front_door")["state"])

    def test_the_settings_are_local_ha_mcps_and_the_environment_wins(self):
        stub = self.stub()
        (self.config / "ha-mcp").mkdir()
        (self.config / "ha-mcp" / ".env").write_text(f"HA_TOKEN=from-file\nHA_MCP_URL={stub.url}/mcp_server/sse\n", encoding="utf-8")
        self.doc({"SYSTEM_TOOLS_CONFIG": str(self.config)})
        self.doc({"SYSTEM_TOOLS_CONFIG": str(self.config), "HA_TOKEN": "from-env"})
        self.assertEqual(stub.calls, [("/api/states", "Bearer from-file"), ("/api/states", "Bearer from-env")])

    def test_an_exported_empty_token_beats_the_file(self):
        """sd:3964: ha-mcp.sh keeps an exported empty value (st_source_env), so the file's token is not sent."""
        stub = self.stub()
        (self.config / "ha-mcp").mkdir()
        (self.config / "ha-mcp" / ".env").write_text(f"HA_TOKEN=from-file\nHA_MCP_URL={stub.url}/mcp_server/sse\n", encoding="utf-8")
        doc = self.doc({"SYSTEM_TOOLS_CONFIG": str(self.config), "HA_TOKEN": ""})
        self.assertEqual(stub.calls, [])
        self.assertIn("HA_TOKEN not set", doc["reader"]["reason"])

    def test_unset_settings_are_named_and_nothing_is_called(self):
        (self.config / "ha-mcp").mkdir()
        (self.config / "ha-mcp" / ".env").write_text("HA_TOKEN=change-me\nHA_MCP_URL=http://change-me:8123/mcp_server/sse\n", encoding="utf-8")
        doc = self.doc({"SYSTEM_TOOLS_CONFIG": str(self.config)})
        self.assertFalse(doc["reader"]["available"])
        self.assertIn("HA_TOKEN", doc["reader"]["reason"])
        self.assertIn("HA_MCP_URL", doc["reader"]["reason"])
        self.assertIn("<config>/ha-mcp/.env", doc["reader"]["reason"])
        self.assertEqual(self.tile(doc, "lock.front_door")["error"], "HA not configured")
        self.assertEqual(len(doc["groups"]), 4, "a reader failure dropped the tiles")

    def test_a_refused_token_is_named_on_every_tile(self):
        stub = self.stub(lambda path: (401, {"message": "Unauthorized"}, {}))
        doc = self.doc(self.env(stub.url))
        self.assertFalse(doc["reader"]["available"])
        self.assertIn("HTTP 401", doc["reader"]["reason"])
        self.assertEqual({t["error"] for g in doc["groups"] for t in g["tiles"]}, {"HA refused the token"})
        self.assertTrue(all(t["state"] is None for g in doc["groups"] for t in g["tiles"]))

    def test_home_assistant_down_is_named(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        doc = self.doc(self.env(f"http://127.0.0.1:{port}"))
        self.assertFalse(doc["reader"]["available"])
        self.assertEqual(self.tile(doc, "lock.front_door")["error"], "HA did not answer")
        self.assertNotIn(str(port), json.dumps(doc))

    def test_a_slow_answer_is_cut_at_the_timeout(self):
        # The answer comes after 6 s; cut at 0.3 s, the rest is slack for a loaded gate (sd:6468).
        stub = self.stub(lambda path: (time.sleep(6), (200, STATES, {}))[1])
        started = time.monotonic()
        doc = self.doc(self.env(stub.url), timeout=0.3)
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(doc["reader"]["available"])
        self.assertIn("0.3 s", doc["reader"]["reason"])
        self.assertEqual(self.tile(doc, "lock.front_door")["error"], "HA timed out")

    def test_a_redirect_is_not_followed_so_the_token_goes_nowhere_else(self):
        stub = self.stub(lambda path: (302, b"", {"Location": "/elsewhere"}) if path == "/api/states" else (200, STATES, {}))
        doc = self.doc(self.env(stub.url))
        self.assertEqual([path for path, _ in stub.calls], ["/api/states"])
        self.assertFalse(doc["reader"]["available"])
        self.assertIn("HTTP 302", doc["reader"]["reason"])

    def test_an_answer_that_is_not_a_state_list_is_named(self):
        for body in (b"<html>", {"entity_id": "lock.front_door"}, [{"entity_id": 3}]):
            with self.subTest(body=body):
                stub = self.stub(lambda path, body=body: (200, body, {}))
                doc = self.doc(self.env(stub.url))
                self.assertFalse(doc["reader"]["available"])
                self.assertEqual(self.tile(doc, "lock.front_door")["error"], "HA answer not read")

    def test_a_cut_answer_or_an_unusable_url_is_named(self):
        stub = self.stub(lambda path: (200, b'[{"entity_id": "lock.front_door"', {"Content-Length": "4096"}))
        doc = self.doc(self.env(stub.url))
        self.assertFalse(doc["reader"]["available"])
        self.assertEqual(self.tile(doc, "lock.front_door")["error"], "HA answer not read")
        doc = self.doc(self.env("http://127.0.0.1:notaport"))
        self.assertEqual((doc["reader"]["available"], self.tile(doc, "lock.front_door")["error"]), (False, "HA not configured"))

    def test_no_tile_list_makes_no_call(self):
        stub = self.stub()
        doc = home_screen.document(self.connection, now=NOW, config=self.config / "home-tiles.conf", environ=self.env(stub.url))
        self.assertEqual((stub.calls, doc["groups"]), ([], []))

    def test_writes_follow_the_read_and_carry_the_allowlist(self):
        stub = self.stub()
        self.assertEqual(self.doc(self.env(stub.url))["writes"], {"available": True, "reason": "", "services": home_screen.SERVICES})
        refused = self.doc(self.env(self.stub(lambda path: (401, {"message": "Unauthorized"}, {})).url))
        self.assertEqual(refused["writes"], {"available": False, "reason": refused["reader"]["reason"], "services": home_screen.SERVICES})


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/home")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Home · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "home-kiosk.js", "icons.js", "sections.js", "read.js", "commands.js", "brief-details.js", "report-details.js", "task-details.js", "home.js", "home-map.js", "home-koi.js", "places.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_a_hidden_headline_draws_no_box(self):
        """sd:6688: `.headline` sets display, which beats the hidden attribute home.js sets when no headline line
        names an entity; only a `[hidden]` rule after it keeps the empty button off the page."""
        css = (V2 / "static" / "home.css").read_text(encoding="utf-8")
        self.assertIn(".headline[hidden] { display: none; }", css)
        self.assertGreater(css.index(".headline[hidden]"), css.index(".headline {"))

    def test_the_rail_opens_the_new_page(self):
        self.assertEqual(v2.SECTIONS.get("Home"), "/home")
        self.assertNotIn("Home", v2.CLASSIC)

    def test_the_data_route_needs_a_session_takes_no_query_and_reads_the_config(self):
        self.assertEqual(self.request("/api/home")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/home?kiosk=1", headers=cookie)[0], 400)
        with mock.patch.object(home_screen, "CONFIG", EXAMPLE):
            status, _, body = self.request("/api/home", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["headline"]["id"], "binary_sensor.problems_active")
        self.assertFalse(doc["reader"]["available"])

    def test_the_document_never_carries_the_token(self):
        cookie = {"Cookie": self.cookie}
        with mock.patch.object(home_screen, "CONFIG", EXAMPLE), mock.patch.dict("os.environ", {"HA_TOKEN": TOKEN, "HA_URL": "https://ha.example.test"}):
            status, _, body = self.request("/api/home", headers=cookie)
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN, body)
        self.assertNotIn("ha.example.test", body)


class TheRow(ScreenCase):
    """sd:3987: the tile list is the setting row `dashboard.home_tiles`. The row wins over the file; with no row the file
    answers; and a failed row read is unknown: it is named, the file is not read in its place, and Home Assistant is not called."""

    ROW = "headline|binary_sensor.problems_active|Problems\ntile|Security|lock.front_door|Front door\n"

    def setUp(self):
        super().setUp()
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.file = self.dir / "home-tiles.conf"

    def doc(self, **more):
        return home_screen.document(self.connection, now=NOW, config=self.file, **more)

    def test_the_row_wins_over_the_file_and_the_editor_opens_on_it(self):
        self.file.write_text("tile|Water|binary_sensor.water_leak|Leak\n", encoding="utf-8")
        writes.set_setting(self.connection, home_screen.KEY, self.ROW)
        doc = self.doc()
        self.assertEqual([t["id"] for g in doc["groups"] for t in g["tiles"]], ["lock.front_door"])
        self.assertEqual(doc["headline"]["id"], "binary_sensor.problems_active")
        self.assertEqual((doc["config"]["state"], doc["config"]["source"]), ("read", home_screen.ROW_SOURCE))
        self.assertEqual((doc["config"]["editor"]["from"], doc["config"]["editor"]["text"]), ("database", self.ROW))

    def test_with_no_row_the_file_answers_and_the_editor_opens_on_it(self):
        self.file.write_text("tile|Water|binary_sensor.water_leak|Leak\n", encoding="utf-8")
        doc = self.doc()
        self.assertEqual(doc["config"]["source"], "<config>/project-dashboard/home-tiles.conf")
        self.assertEqual([t["id"] for g in doc["groups"] for t in g["tiles"]], ["binary_sensor.water_leak"])
        self.assertEqual(doc["config"]["editor"]["from"], "file")
        self.assertEqual(doc["config"]["editor"]["text"], "tile|Water|binary_sensor.water_leak|Leak\n")

    def test_with_neither_the_editor_opens_on_the_example(self):
        doc = self.doc()
        self.assertEqual(doc["config"]["state"], "missing")
        self.assertEqual(doc["config"]["editor"]["from"], "example")
        self.assertEqual(doc["config"]["editor"]["text"], EXAMPLE.read_text(encoding="utf-8"))

    def test_the_same_entries_draw_from_the_row_as_from_the_file(self):
        text = EXAMPLE.read_text(encoding="utf-8")
        self.file.write_text(text, encoding="utf-8")
        before = self.doc()
        writes.set_setting(self.connection, home_screen.KEY, text)
        self.file.unlink()
        after = self.doc()
        self.assertEqual((after["headline"], after["groups"]), (before["headline"], before["groups"]))
        self.assertEqual(sum(len(g["tiles"]) for g in after["groups"]) + 1, 9)

    def test_a_failed_row_read_is_named_and_neither_the_file_nor_home_assistant_is_read(self):
        self.file.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        stub = Stub(lambda path: (200, STATES, {}))
        self.addCleanup(stub.close)
        environ = {"SYSTEM_TOOLS_CONFIG": os.environ["SYSTEM_TOOLS_CONFIG"], "HA_TOKEN": HA_TOKEN, "HA_MCP_URL": f"{stub.url}/mcp_server/sse"}
        with mock.patch.object(writes, "read_settings", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.doc(environ=environ)
        self.assertEqual(stub.calls, [], "a failed row read called Home Assistant")
        self.assertEqual((doc["headline"], doc["groups"], doc["config"]["state"]), (None, [], "error"))
        self.assertEqual(doc["config"]["problems"], ["tile list not read: OperationalError: database is locked"])
        self.assertEqual(doc["reader"], {"available": False, "reason": "tile list not read: OperationalError: database is locked"})
        self.assertIsNone(doc["config"]["editor"], "a page that could not read the row offered an editor")

    def test_no_connection_is_a_failed_read_not_an_empty_one(self):
        self.file.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        doc = home_screen.document(None, now=NOW, config=self.file)
        self.assertEqual(doc["config"]["state"], "error")
        self.assertTrue(doc["config"]["problems"][0].startswith("tile list not read: "))


class TheSave(BrowserSession):
    """sd:3987: POST /api/home/tiles {text, before} parses first, refuses a stale `before` (409) and writes one setting row."""

    def setUp(self):
        super().setUp()
        self.file = Path(self.enterContext(tempfile.TemporaryDirectory())) / "home-tiles.conf"
        self.enterContext(mock.patch.object(home_screen, "CONFIG", self.file))

    def read(self):
        status, _, body = self.request("/api/home", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        return json.loads(body)["config"]["editor"]

    def held(self):
        return writes.read_settings(self.connection, [home_screen.KEY])

    def test_the_first_save_imports_the_file_text_and_the_page_reads_the_row(self):
        self.file.write_text("tile|Water|binary_sensor.water_leak|Leak\n", encoding="utf-8")
        editor = self.read()
        text = editor["text"] + "tile|Security|lock.front_door|Front door\n"
        status, _, answer = self.post("/api/home/tiles", {"text": text, "before": editor["digest"]})
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.held(), {home_screen.KEY: text})
        self.assertEqual(answer["previous"], editor["text"])
        self.assertEqual(self.read(), {"from": "database", "text": text, "digest": answer["digest"]})

    def test_a_bad_entity_id_is_refused_naming_its_line_and_nothing_is_written(self):
        editor = self.read()
        status, _, answer = self.post("/api/home/tiles", {"text": "tile|Security|lock.front_door|Front door\ntile|Security|Not An Id|Bad\n",
                                                           "before": editor["digest"]})
        self.assertEqual(status, 400)
        self.assertIn("line 2: 'Not An Id' is not an entity id", answer["error"])
        self.assertEqual(self.held(), {})

    def test_two_saves_from_one_read_the_second_is_stale_and_writes_nothing(self):
        editor = self.read()
        first = "tile|Security|lock.front_door|Front door\n"
        self.assertEqual(self.post("/api/home/tiles", {"text": first, "before": editor["digest"]})[0], 200)
        status, _, answer = self.post("/api/home/tiles", {"text": "tile|Security|lock.back_door|Back door\n", "before": editor["digest"]})
        self.assertEqual(status, 409, answer)
        self.assertTrue(answer["reload"])
        self.assertEqual(self.held(), {home_screen.KEY: first})

    def test_undo_posts_the_previous_text_with_the_saved_digest(self):
        self.file.write_text("tile|Water|binary_sensor.water_leak|Leak\n", encoding="utf-8")
        editor = self.read()
        _, _, saved = self.post("/api/home/tiles", {"text": "tile|Security|lock.front_door|Front door\n", "before": editor["digest"]})
        status, _, undone = self.post("/api/home/tiles", {"text": saved["previous"], "before": saved["digest"]})
        self.assertEqual(status, 200, undone)
        self.assertEqual(self.held(), {home_screen.KEY: "tile|Water|binary_sensor.water_leak|Leak\n"})

    def test_a_previous_text_save_would_refuse_offers_no_undo(self):
        # Codex review: the file reader skips a bad line, so Undo of a partial file posted a text Save refuses (400).
        self.file.write_text("tile|Water|binary_sensor.water_leak|Leak\ntile|Water|INVALID|Bad\n", encoding="utf-8")
        editor = self.read()
        status, _, saved = self.post("/api/home/tiles", {"text": "tile|Security|lock.front_door|Front door\n", "before": editor["digest"]})
        self.assertEqual((status, saved["previous"]), (200, ""))

    def test_a_list_with_no_tile_an_oversized_one_or_another_shape_is_refused(self):
        editor = self.read()
        for payload in ({"text": "# only a comment\n", "before": editor["digest"]},
                        {"text": "tile|G|lock.a|A\n" + "#" * writes.SETTING_VALUE_LENGTH, "before": editor["digest"]},
                        {"text": "tile|G|lock.a|A\n"}, {"text": 3, "before": editor["digest"]}):
            status, _, answer = self.post("/api/home/tiles", payload)
            self.assertEqual(status, 400, (payload, answer))
        self.assertEqual(self.held(), {})


#: The tile list the service tests use: synthetic entities, one per kind of guard.
SERVICE_TILES = ("headline|binary_sensor.example_problems|Problems\n"
                 "tile|Lights|light.example_lamp|Lamp\n"
                 "tile|Security|lock.example_door|Door\n"
                 "tile|Security|alarm_control_panel.example_house|Alarm\n"
                 "tile|Water|binary_sensor.example_leak|Leak\n")


class TheService(BrowserSession):
    """sd:4966: POST /api/home/service calls one allowed service on one listed tile, journals it and reads the tile again."""

    def setUp(self):
        super().setUp()
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.file = folder / "home-tiles.conf"
        self.file.write_text(SERVICE_TILES, encoding="utf-8")
        self.enterContext(mock.patch.object(home_screen, "CONFIG", self.file))
        self.enterContext(mock.patch.object(home_screen, "JOURNAL_ITEM", self.item("Home page")))
        self.after = {"light.example_lamp": "on", "lock.example_door": "unlocked"}
        self.refuse = None  # (status, body) the next service call answers with

        def answer(path, method="GET"):
            if method == "POST":
                return self.refuse or (200, [], {})
            if path == "/api/states":
                return 200, [entity("light.example_lamp", "off"), entity("lock.example_door", "locked")], {}
            entity_id = path.rsplit("/", 1)[1]
            return 200, entity(entity_id, self.after.get(entity_id, "on"), "2026-09-06T12:01:00+00:00"), {}

        self.ha = Stub(answer)
        self.addCleanup(self.ha.close)
        self.enterContext(mock.patch.dict("os.environ", {"HA_TOKEN": HA_TOKEN, "HA_MCP_URL": f"{self.ha.url}/mcp_server/sse"}))

    def digest(self):
        status, _, body = self.request("/api/home", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.ha.calls.clear()
        return json.loads(body)["config"]["editor"]["digest"]

    def call(self, entity_id, name, **more):
        return self.post("/api/home/service", {"entity_id": entity_id, "service": name, "before": self.digest(), **more})

    def journal(self):
        from sd_db import runner_exec

        return runner_exec.execution_journal(self.connection)

    def test_an_allowed_service_is_sent_journalled_and_the_tile_is_read_again(self):
        status, _, answer = self.call("light.example_lamp", "turn_on")
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.ha.calls, [("POST /api/services/light/turn_on", f"Bearer {HA_TOKEN}", {"entity_id": "light.example_lamp"}),
                                         ("/api/states/light.example_lamp", f"Bearer {HA_TOKEN}")])
        self.assertEqual(answer["state"], {"value": "on", "changed": "2026-09-06T12:01:00+00:00", "device_class": None, "unit": None})
        self.assertEqual((answer["sent"], answer["reread"]), ("light.turn_on", ""))
        journal = self.journal()
        self.assertEqual(journal["skipped"], {})
        [row] = journal["executions"]
        self.assertEqual((row["id"], row["item"], row["exit_code"], row["command"], row["scope"]),
                         (answer["note"], home_screen.JOURNAL_ITEM, 0, ["home-assistant", "light.turn_on", "light.example_lamp"], "home-assistant"))
        self.assertTrue(row["started"] and row["ended"])
        # Activity's "Command journal" lists it, and its output reads through the palette's own reader.
        from sd_dashboard import activity_screen

        listed = activity_screen.commands(self.connection, datetime.now(timezone.utc) + timedelta(days=1), {}, {})
        self.assertEqual([e["what"] for e in listed], ["home-assistant light.turn_on light.example_lamp · exit 0"])
        status, _, out = self.request(f"/api/executions/{answer['note']}", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, out)
        self.assertEqual([json.loads(line)["event"] for line in json.loads(out)["output"].splitlines()], ["dispatch", "response"])
        self.assertNotIn(HA_TOKEN, out + json.dumps(answer))

    def test_an_entity_outside_the_tile_list_is_refused_and_nothing_is_sent(self):
        status, _, answer = self.call("light.example_other", "turn_on")
        self.assertEqual(status, 400, answer)
        self.assertIn("light.example_other is not a tile in the tile list; nothing was sent", answer["error"])
        self.assertEqual((self.ha.calls, self.journal()["executions"]), ([], []))

    def test_a_service_outside_the_allowlist_is_refused_and_nothing_is_sent(self):
        for entity_id, name, why in (("lock.example_door", "open", "it allows lock, unlock on lock"),
                                     ("binary_sensor.example_leak", "turn_on", "it calls no binary_sensor service"),
                                     ("light.example_lamp", "../../config/core/restart", "it allows turn_on, turn_off, toggle on light")):
            status, _, answer = self.call(entity_id, name)
            self.assertEqual(status, 400, (name, answer))
            self.assertIn(why, answer["error"])
        self.assertEqual((self.ha.calls, self.journal()["executions"]), ([], []))

    def test_a_security_service_runs_with_no_typed_confirm(self):
        # sd:5479: the operator ruled every Home action runs at once; a confirm key is now an unexpected key.
        status, _, answer = self.call("lock.example_door", "unlock")
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.ha.calls[0], ("POST /api/services/lock/unlock", f"Bearer {HA_TOKEN}", {"entity_id": "lock.example_door"}))
        self.assertEqual(answer["state"]["value"], "unlocked")
        status, _, answer = self.call("lock.example_door", "unlock", confirm="lock.example_door")
        self.assertEqual(status, 400, answer)

    def test_a_refusal_from_home_assistant_shows_its_reason_and_is_journalled(self):
        self.refuse = (403, {"message": "User does not have permission to call this service"}, {})
        status, _, answer = self.call("light.example_lamp", "turn_off")
        self.assertEqual(status, 502, answer)
        self.assertEqual(answer["error"], "Home Assistant refused light.turn_off (HTTP 403): User does not have permission to call this service. Nothing changed.")
        [row] = self.journal()["executions"]
        self.assertEqual((row["id"], row["exit_code"]), (answer["note"], 1))
        self.assertEqual(len(self.ha.calls), 1, "a refused call reads no state")
        self.refuse = (401, b"401: Unauthorized", {"Content-Type": "text/plain"})
        status, _, answer = self.call("light.example_lamp", "turn_off")
        self.assertEqual((status, answer["error"]), (502, "Home Assistant refused light.turn_off (HTTP 401): 401: Unauthorized. Nothing changed."))
        self.refuse = (500, {"message": "Error calling service"}, {})
        status, _, answer = self.call("light.example_lamp", "turn_off")
        self.assertEqual((status, answer["error"]), (502, "Home Assistant failed while it ran light.turn_off (HTTP 500): Error calling service. It may have partly run; read the tiles again."))

    def test_a_call_with_no_answer_is_unknown_not_refused(self):
        self.refuse = None
        slow = Stub(lambda path, method="GET": (time.sleep(1.5), (200, [], {}))[1])
        self.addCleanup(slow.close)
        self.enterContext(mock.patch.dict("os.environ", {"HA_MCP_URL": f"{slow.url}/mcp_server/sse"}))
        write = home_screen.service({"entity_id": "light.example_lamp", "service": "toggle", "before": self.digest()}, "local", timeout=0.5)
        status, answer = write(self.connection)
        self.assertEqual(status, 504, answer)
        self.assertTrue(answer["unknown"])
        self.assertIn("gave no answer within 0.5 s; light.toggle may have run", answer["error"])
        [row] = self.journal()["executions"]
        self.assertEqual((row["exit_code"], bool(row["ended"])), (None, True))

    def test_a_stale_tile_list_is_refused_and_nothing_is_sent(self):
        before = self.digest()
        self.file.write_text(SERVICE_TILES + "tile|Lights|light.example_more|More\n", encoding="utf-8")
        status, _, answer = self.post("/api/home/service", {"entity_id": "light.example_lamp", "service": "turn_on", "before": before})
        self.assertEqual(status, 409, answer)
        self.assertTrue(answer["reload"])
        self.assertEqual(self.ha.calls, [])

    def test_another_shape_is_refused(self):
        before = self.digest()
        for payload in ({"entity_id": "light.example_lamp", "service": "turn_on"},
                        {"entity_id": "light.example_lamp", "service": "turn_on", "before": before, "data": "{}"},
                        {"entity_id": "Light Lamp", "service": "turn_on", "before": before},
                        {"entity_id": "light.example_lamp", "service": 3, "before": before}):
            status, _, answer = self.post("/api/home/service", payload)
            self.assertEqual(status, 400, (payload, answer))
        self.assertEqual(self.ha.calls, [])

    def test_the_journal_record_passes_the_backup_and_prune_checks_that_read_every_exec_note(self):
        # Older state: the nightly backup and the output prune read every version 1 exec note and refuse one without its
        # output in the executions directory; a Home record must pass both, or a call would break the next backup.
        from sd_db import retention, runner_exec_backup

        _, _, answer = self.call("light.example_lamp", "turn_on")
        [(row, value)] = runner_exec_backup._notes(self.connection)
        self.assertEqual((row["id"], value["note"]), (answer["note"], answer["note"]))
        self.assertTrue(Path(row["output_path"]).is_file())
        self.assertEqual(retention._output_files(self.connection, row)[0], Path(row["output_path"]))

    def test_a_stop_after_the_request_left_leaves_the_output_file_and_the_next_call_still_runs(self):
        # Steps: the output file and its dispatch line come first, the note last. A stop between them leaves a file and no
        # note (no unfinished record that a reconcile could not finish); the next call makes its own file and note.
        executions = self.path.parent / "executions"
        with mock.patch.object(home_screen, "_call", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                home_screen.service({"entity_id": "light.example_lamp", "service": "turn_on", "before": self.digest()}, "local")(self.connection)
        [left] = executions.iterdir()
        self.assertEqual([json.loads(line)["event"] for line in left.read_text().splitlines()], ["dispatch"])
        self.assertEqual(self.journal()["executions"], [])
        status, _, answer = self.call("light.example_lamp", "turn_on")
        self.assertEqual((status, len(self.journal()["executions"]), len(list(executions.iterdir()))), (200, 1, 2), answer)

    def test_a_failed_journal_write_after_the_call_still_reports_the_outcome(self):
        from sd_db import writes as sd_writes

        with mock.patch.object(sd_writes, "add_note", side_effect=sqlite3.OperationalError("database is locked")):
            answer = home_screen.service({"entity_id": "light.example_lamp", "service": "turn_on", "before": self.digest()}, "local")(self.connection)
        self.assertEqual((answer["sent"], answer["note"], answer["state"]["value"]), ("light.turn_on", None, "on"), answer)
        self.assertIn("the journal note was not written (OperationalError: database is locked)", answer["journal"])

    def test_the_document_offers_writes_once_the_read_worked_with_the_allowlist(self):
        status, _, body = self.request("/api/home", headers={"Cookie": self.cookie})
        self.assertEqual(json.loads(body)["writes"], {"available": True, "reason": "", "services": home_screen.SERVICES})


# The stand-in additions home.js and home-kiosk.js need beyond test_v2_tasks: a root element with attributes, a clock, and the shell's row, url and
# closePane.
ROOT = r"""
var ROOT_ATTRS = {};
document.documentElement = { setAttribute(k, v) { ROOT_ATTRS[k] = v; }, removeAttribute(k) { delete ROOT_ATTRS[k]; },
  hasAttribute(k) { return k in ROOT_ATTRS; } };
var INTERVALS = [];
function setInterval(f, ms) { INTERVALS.push([f, ms]); return 0; }
"""
SHELL_MORE = r"""
window.shell.row = () => new URLSearchParams(location.search).get('row'); window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
OUT.panes = []; window.shell.openPane = t => OUT.panes.push(t);
"""
# The reader as the page gets it, with each spec the page hands it recorded.
READ_SPY = r"""
{ const real = window.shell.read; OUT.reads = []; window.shell.read = spec => { OUT.reads.push([spec.source, spec.what]); return real(spec); }; }
"""


class TheScript(ScreenCase):
    """home.js against the document `home_screen` builds from the example config."""

    def setUp(self):
        super().setUp()
        self.doc = home_screen.document(self.connection, now=NOW, config=EXAMPLE)

    def run_page(self, body, answer=None, search=""):
        answer = answer or f"() => [200, {json.dumps(self.doc)}]"
        script = (STAND_IN + ROOT + f"location.search = {json.dumps(search)};\n" + KIOSK_JS + "\n" + MARKUP_JS + "\nconst mk = window.markup.html;\n"
                  + SHELL + SHELL_MORE + READ_SHELL + READ_SPY + f"\nANSWER = {answer};\n" + COMMANDS_JS + HOME_JS
                  + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.root = ROOT_ATTRS; OUT.attention = window.PAGE_ATTENTION;"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]);")
        self.assertEqual(out["R"]["reg"], COMMANDS)

    def live(self, answer=None):
        """The document `home_screen` builds against a stub Home Assistant."""
        stub = Stub(answer or (lambda path: (200, STATES, {})))
        self.addCleanup(stub.close)
        return home_screen.document(self.connection, now=NOW, config=EXAMPLE, environ={
            "SYSTEM_TOOLS_CONFIG": os.environ["SYSTEM_TOOLS_CONFIG"], "HA_TOKEN": HA_TOKEN, "HA_MCP_URL": f"{stub.url}/mcp_server/sse"})

    def test_every_write_is_off_with_the_read_reason_until_home_assistant_is_read(self):
        out = self.run_page("""R.when = REG.filter(c => c.id !== 'sensor.read').map(c => c.when({ id: 'light.example', type: c.on, label: 'x' }));
R.act = ELS.groups.html.match(/<div class="act">([^<]*)<\\/div>/g);
shellRun(cmd('lock.unlock'), C.get('lock.front_door')); await flush();""")
        self.assertEqual(set(out["R"]["when"]), {self.doc["reader"]["reason"]})
        self.assertEqual(set(out["R"]["act"]), {'<div class="act"></div>'}, "no tile offers a command while writes are off")
        self.assertEqual((out["posts"], out["gets"], out["confirms"]), ([], ["/api/home"], []))

    def test_every_does_line_keeps_the_shared_word_limit(self):
        # The factories pass does by position, which test_v2_commands' scan cannot read, so the limit is checked here.
        from test_v2_commands import WORDS
        out = self.run_page("R.d = REG.map(c => [c.id, c.does]);")
        long = [f"{command}: {does}" for command, does in out["R"]["d"] if len(does.split()) > WORDS]
        self.assertEqual(long, [], f"a does line over {WORDS} words")

    def test_each_command_carries_a_does_line_and_the_rest_call_through_home_assistant(self):
        out = self.run_page("R.c = REG.map(c => [c.id, typeof c.does, c.executes, c.cli({ id: 'x.y' })]);")
        for command, does, executes, line in out["R"]["c"]:
            self.assertEqual(does, "string", command)
            if command != "sensor.read":
                self.assertTrue(executes, command)
                self.assertIn(f'"$HA_URL/api/services/{command.replace(".", "/", 1)}"', line)
        unlock, disarm = self.run_page("R.d = [cmd('lock.unlock').does, cmd('alarm_control_panel.alarm_disarm').does];")["R"]["d"]
        self.assertIn("Unlocks the door", unlock)
        self.assertIn("Disarms the alarm", disarm)

    def test_only_the_services_a_domain_allows_reach_its_tile(self):
        out = self.run_page("R.t = ['alarm_control_panel.house', 'lock.front_door', 'input_boolean.guest_mode', 'binary_sensor.water_leak',"
                            " 'binary_sensor.virtual_porch_light'].map(id => [id, C.get(id).type, REG.filter(c => c.on === C.get(id).type).map(c => c.id)]);",
                            answer=f"() => [200, {json.dumps(self.live())}]")
        self.assertEqual(out["R"]["t"], [
            ["alarm_control_panel.house", "alarm_control_panel", [f"alarm_control_panel.{s}" for s in ("alarm_disarm", "alarm_arm_home", "alarm_arm_away", "alarm_arm_night")]],
            ["lock.front_door", "lock", ["lock.unlock", "lock.lock"]],
            ["input_boolean.guest_mode", "input_boolean", ["input_boolean.turn_on", "input_boolean.turn_off", "input_boolean.toggle"]],
            ["binary_sensor.water_leak", "sensor", ["sensor.read"]],
            # A binary sensor typed toggle still reads as a switch, but Home Assistant has no service for it.
            ["binary_sensor.virtual_porch_light", "sensor", ["sensor.read"]],
        ])
        # The allowlist is the server's: a service the document no longer lists is off with that reason.
        doc = self.live()
        del doc["writes"]["services"]["lock"]["unlock"]
        out = self.run_page("R.w = cmd('lock.unlock').when(C.get('lock.front_door'));", answer=f"() => [200, {json.dumps(doc)}]")
        self.assertEqual(out["R"]["w"], "the server does not allow lock.unlock")

    def answer_service(self, reply):
        return f"(path, body) => path === '/api/home' ? [200, {json.dumps(self.live())}] : {reply}"

    def test_a_one_click_service_posts_shows_the_state_read_again_and_undo_posts_the_other(self):
        sent = {"sent": "input_boolean.turn_on", "entity_id": "input_boolean.guest_mode", "note": 7, "reread": "", "journal": "",
                "state": {"value": "on", "changed": "2026-09-06T12:01:00+00:00", "device_class": None, "unit": None}}
        out = self.run_page("""R.row = ELS.groups.html.includes('data-cmd'); shellRun(cmd('input_boolean.turn_on'), C.get('input_boolean.guest_mode')); await flush();
R.tile = ELS.groups.html.match(/data-id="input_boolean.guest_mode"[\\s\\S]*?<\\/li>/)[0];
await lastUndo().undo(); await flush();""", answer=self.answer_service(f"[200, {json.dumps(sent)}]"))
        digest = self.live()["config"]["editor"]["digest"]
        self.assertEqual(out["posts"], [["/api/home/service", {"entity_id": "input_boolean.guest_mode", "service": "turn_on", "before": digest}, 64],
                                        ["/api/home/service", {"entity_id": "input_boolean.guest_mode", "service": "turn_off", "before": digest}, 64]])
        self.assertEqual(out["confirms"], [])
        self.assertIn("</span>on</span>", out["R"]["tile"])
        self.assertIn('datetime="2026-09-06T12:01:00+00:00"', out["R"]["tile"])
        self.assertEqual(out["toasts"][0], ["Guest mode · input_boolean.turn_on sent", True])

    def test_a_security_service_runs_at_once_with_no_dialog(self):
        # sd:5479: Unlock sends on the first run, with no dialog and no typed field.
        out = self.run_page("""shellRun(cmd('lock.unlock'), C.get('lock.front_door')); await flush();""",
                            answer=self.answer_service("[200, { sent: 'lock.unlock', state: null, reread: '', journal: '', note: 3 }]"))
        self.assertEqual(out["confirms"], [])
        self.assertEqual([p[1] for p in out["posts"]], [{"entity_id": "lock.front_door", "service": "unlock", "before": out["posts"][0][1]["before"]}])

    def test_a_refusal_shows_home_assistants_reason_and_an_unknown_outcome_reads_again(self):
        refused = "Home Assistant refused input_boolean.turn_on (HTTP 403): User does not have permission. Nothing changed."
        out = self.run_page("shellRun(cmd('input_boolean.turn_on'), C.get('input_boolean.guest_mode')); await flush();",
                            answer=self.answer_service(f"[502, {{ error: {json.dumps(refused)} }}]"))
        self.assertEqual(out["toasts"], [[f"Guest mode not changed: {refused}", False]])
        self.assertEqual(out["gets"], ["/api/home", "/api/home"])
        out = self.run_page("shellRun(cmd('input_boolean.turn_on'), C.get('input_boolean.guest_mode')); await flush();",
                            answer=self.answer_service("[400, { error: 'input_boolean.open is not a service the dashboard calls' }]"))
        self.assertEqual(out["gets"], ["/api/home"], "a 400 sent nothing, so nothing is read again")
        out = self.run_page("shellRun(cmd('input_boolean.turn_on'), C.get('input_boolean.guest_mode')); await flush();",
                            answer=self.answer_service("[504, { error: 'no answer; input_boolean.turn_on may have run.', unknown: true }]"))
        self.assertEqual(out["gets"], ["/api/home", "/api/home"], "an unknown outcome reads the tiles again")

    def test_read_state_posts_the_tile_and_shows_its_state_in_details(self):
        """sd:5886: the server reads the tile it finds in the tile list; the page sends the entity id, never a URL or a token."""
        out = self.run_page("""const was = ANSWER; ANSWER = (p, b) => p === '/api/actions/run' ? [200, { action: b.action, label: 'Read state', line: 'curl', exit: 0, output: '{"value": "off"}', cut: false }] : was(p, b);
shellRun(cmd('sensor.read'), C.get('binary_sensor.water_leak')); await flush(); R.details = ELS.details.html;""")
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/actions/run", {"action": "sensor.read", "entity_id": "binary_sensor.water_leak"}]])
        self.assertIn("Read state", out["R"]["details"])

    def test_read_state_runs_on_the_server_and_its_line_names_the_token_and_never_holds_one(self):
        # A document that carries a token by mistake: nothing the page shows or copies may hold its value.
        doc = json.loads(json.dumps(self.doc))
        doc["reader"]["token"] = TOKEN
        doc["groups"][2]["tiles"][0]["token"] = TOKEN
        out = self.run_page("""const read = REG.find(c => c.id === 'sensor.read');
R.read = [read.executes, read.cli(C.get('binary_sensor.water_leak'))];
R.lines = []; OBJ.forEach(o => REG.filter(c => c.on === o.type).forEach(c => R.lines.push(c.cli(o))));
open('binary_sensor.wan_degraded'); R.det = ELS.details.html; R.groups = ELS.groups.html;""", answer=f"() => [200, {json.dumps(doc)}]")
        self.assertEqual(out["R"]["read"], [True, 'curl -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/states/binary_sensor.water_leak"'])
        self.assertTrue(out["R"]["lines"])
        for line in out["R"]["lines"]:
            self.assertIn("$HA_TOKEN", line)
            self.assertIn("$HA_URL", line)
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_unconfigured_tiles_are_unknown_with_the_reason_and_the_page_claims_nothing(self):
        out = self.run_page("R.groups = ELS.groups.html; R.head = ELS.headline.html; R.sub = ELS.subhead.html;")
        groups = out["R"]["groups"]
        for name in ("House alarm", "Front door", "Water leak", "Guest mode", "Water &amp; safety"):
            self.assertIn(name, groups)
        self.assertEqual(len(re.findall(r'data-id="[^"]+" data-state="unknown"', groups)), 8)
        self.assertNotRegex(groups, r'data-state="(ok|caution|warning)"')
        self.assertEqual(groups.count("HA not configured"), 8 * 2, "each tile names the error in its words and its label")
        self.assertIn("Problems active", out["R"]["head"])
        self.assertIn("8 tiles", out["R"]["sub"])
        self.assertIn("no HA state read", out["R"]["sub"])
        self.assertEqual(out["states"][0]["kind"], "loading")
        last = out["states"][-1] or {}
        self.assertEqual(last.get("kind"), "partial", "the page did not say no state was read")
        self.assertIn(self.doc["reader"]["reason"], last["text"])
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "HA not read"})

    def test_live_tiles_show_their_state_lamp_and_age(self):
        out = self.run_page("R.groups = ELS.groups.html; R.head = ELS.headline.html; R.sub = ELS.subhead.html;",
                            answer=f"() => [200, {json.dumps(self.live())}]")
        tiles = dict(re.findall(r'<div class="tile" data-id="([^"]+)"(.*?)</div></li>', out["R"]["groups"], re.S))
        lamp = {i: re.search(r'data-state="(\w+)"', t).group(1) for i, t in tiles.items()}
        self.assertEqual(lamp, {
            "alarm_control_panel.house": "ok", "lock.front_door": "ok", "binary_sensor.water_leak": "warning",
            "binary_sensor.smoke_sensors": "unknown", "binary_sensor.wan_degraded": "unknown",
            "sensor.backup_last_successful_automatic_backup": "ok", "input_boolean.guest_mode": "ok",
            "binary_sensor.virtual_porch_light": "ok"})
        for entity_id, words in (("alarm_control_panel.house", "armed home"), ("lock.front_door", "locked"),
                                 ("binary_sensor.water_leak", "wet"), ("binary_sensor.wan_degraded", "unavailable · no reading"),
                                 ("binary_sensor.smoke_sensors", home_screen.NOT_LISTED), ("input_boolean.guest_mode", "off")):
            self.assertIn(f"</span>{words}</span>", tiles[entity_id], entity_id)
        # The age: each read tile carries its last change for the shell's relative-time renderer; an unread one carries none.
        self.assertIn('<time class="rel" datetime="2026-09-06T11:40:00+00:00"', tiles["lock.front_door"])
        self.assertNotIn('<time class="rel" datetime="2', tiles["binary_sensor.smoke_sensors"])
        self.assertIn('<span>nothing reported</span></span><span class="st"><span aria-hidden="true">●</span> none</span>', out["R"]["head"])
        self.assertIn("· HA read", out["R"]["sub"])
        last = out["states"][-1] or {}
        self.assertEqual(last.get("kind"), "partial", "the page did not name the tile Home Assistant does not list")
        self.assertIn("binary_sensor.smoke_sensors", last["text"])
        self.assertIn(home_screen.NOT_LISTED, last["text"])

    def test_a_failed_home_assistant_read_names_its_error_on_every_tile(self):
        doc = self.live(lambda path: (401, {"message": "Unauthorized"}, {}))
        out = self.run_page("R.groups = ELS.groups.html; open('lock.front_door'); R.det = ELS.details.html;",
                            answer=f"() => [200, {json.dumps(doc)}]")
        self.assertEqual(len(re.findall(r'data-id="[^"]+" data-state="unknown"', out["R"]["groups"])), 8)
        self.assertIn("HA refused the token", out["R"]["groups"])
        last = out["states"][-1] or {}
        self.assertEqual(last.get("kind"), "partial")
        self.assertIn("HTTP 401", last["text"])
        self.assertIn(f"Not read: {html.escape(doc['reader']['reason'], quote=False)}.", out["R"]["det"])

    def test_a_missing_config_and_a_failed_read_each_say_so(self):
        missing = home_screen.document(self.connection, now=NOW, config=Path(self.enterContext(tempfile.TemporaryDirectory())) / "home-tiles.conf")
        out = self.run_page("R.groups = ELS.groups.html;", answer=f"() => [200, {json.dumps(missing)}]")
        last = out["states"][-1]
        self.assertEqual((last["kind"], last["title"], last["source"]), ("empty", "No tile list", "<config>/project-dashboard/home-tiles.conf"))
        self.assertIn("Edit tiles starts from an example list", last["text"])
        self.assertNotIn("data-id=", out["R"]["groups"], "a missing tile list drew a grid")
        out = self.run_page("", answer="() => [500, { error: 'boom' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])

    def test_no_tile_shows_ok_without_a_state_even_when_the_reader_answers(self):
        # A tile claims only a state it was given.
        doc = json.loads(json.dumps(self.doc))
        doc["reader"] = {"available": True, "reason": ""}
        for tile in [doc["headline"]] + [t for g in doc["groups"] for t in g["tiles"]]:
            tile.update(state=None, error=None)
        out = self.run_page("R.groups = ELS.groups.html; R.head = ELS.headline.html;", answer=f"() => [200, {json.dumps(doc)}]")
        self.assertNotRegex(out["R"]["groups"] + out["R"]["head"], r'data-state="(ok|caution|warning)"|●')
        self.assertEqual(len(re.findall(r'data-id="[^"]+" data-state="unknown"', out["R"]["groups"])), 8)


    # sd:4959: a binary sensor reads by its Home Assistant device_class, never "detected" for every on.
    def binary(self):
        doc = self.live()
        base = doc["groups"][0]["tiles"][0]
        def tile(entity_id, value, device_class):
            return dict(base, id=entity_id, name=entity_id.split(".")[1].replace("_", " ").capitalize(), type=None, error="",
                                                           state={"value": value, "changed": "2026-09-06T11:00:00+00:00", "device_class": device_class, "unit": None})
        doc["groups"].append({"name": "Binary", "tiles": [
            tile("binary_sensor.house_powerwall_grid_status", "on", "power"), tile("binary_sensor.pump_fault", "on", "problem"),
            tile("binary_sensor.side_door", "on", "door"), tile("binary_sensor.bare_switch", "on", None),
            tile("binary_sensor.uplink", "off", "connectivity"), tile("binary_sensor.pump_fault_clear", "off", "problem"),
            tile("binary_sensor.grid_off", "off", "power")]})
        return doc

    def test_a_binary_sensor_lamp_and_words_come_from_its_device_class(self):
        out = self.run_page("R.groups = ELS.groups.html;", answer=f"() => [200, {json.dumps(self.binary())}]")
        tiles = dict(re.findall(r'<div class="tile" data-id="([^"]+)"(.*?)</div></li>', out["R"]["groups"], re.S))
        got = {i: (re.search(r'data-state="(\w+)"', t).group(1), re.search(r'</span>([^<]*)</span>\s*<time', t).group(1))
               for i, t in tiles.items() if i.startswith("binary_sensor.") and i not in ("binary_sensor.smoke_sensors", "binary_sensor.wan_degraded", "binary_sensor.virtual_porch_light")}
        self.assertEqual(got, {
            "binary_sensor.water_leak": ("warning", "wet"),
            "binary_sensor.house_powerwall_grid_status": ("ok", "connected"),
            "binary_sensor.pump_fault": ("warning", "problem"),
            "binary_sensor.side_door": ("neutral", "open"),
            "binary_sensor.bare_switch": ("neutral", "on"),
            # A lost connection is worth a look; power off is a plain fact (a grid outage the Powerwall carries).
            "binary_sensor.uplink": ("caution", "disconnected"),
            "binary_sensor.grid_off": ("neutral", "disconnected"),
            "binary_sensor.pump_fault_clear": ("ok", "clear"),
        })

    def test_a_script_tile_reads_as_its_last_run_in_local_time_with_its_zone(self):
        """sd:5727: Good night is a script tile; it shows when it last ran, as this machine's local time and zone, and a run
        Home Assistant did not report is unknown, never "off" or "never"."""
        doc = self.live()
        base = doc["groups"][0]["tiles"][0]
        ran = "2026-10-22T04:39:00+00:00"
        def tile(entity_id, value, last):
            return dict(base, id=entity_id, name=entity_id.split(".")[1], type=None, error="", state={
            "value": value, "changed": "2026-10-22T04:39:30+00:00", "device_class": None, "unit": None, "ran": last})
        doc["groups"].append({"name": "Routines", "tiles": [tile("script.good_night", "off", ran), tile("automation.good_night", "on", ran),
                                                             tile("script.never_ran", "off", None)]})
        out = self.run_page("R.groups = ELS.groups.html; open('script.good_night'); R.det = ELS.details.html;",
                            answer=f"() => [200, {json.dumps(doc)}]")
        tiles = dict(re.findall(r'<div class="tile" data-id="([^"]+)"(.*?)</div></li>', out["R"]["groups"], re.S))
        got = {i: (re.search(r'data-state="(\w+)"', t).group(1), re.search(r'</span>([^<]*)</span>\s*<time', t).group(1))
               for i, t in tiles.items() if i.split(".")[0] in ("script", "automation")}
        # The engine words the date ("Oct 21, 22:39" or "Oct 21 at 22:39"); the day, the 24-hour time and the zone are fixed.
        local = datetime.fromisoformat(ran).astimezone()
        when = rf"{local:%b} {local.day}(,| at) {local:%H:%M} {local:%Z}"
        state, words = got.pop("script.good_night")
        self.assertEqual(state, "ok")
        self.assertRegex(words, rf"^last run {when}$")
        self.assertEqual(got, {"automation.good_night": ("ok", "running"), "script.never_ran": ("unknown", "last run not read")})
        self.assertRegex(out["R"]["det"], rf"<dt>Last run</dt><dd>{when}</dd>")

    def test_details_explain_the_state_in_one_line_and_name_its_source(self):
        out = self.run_page("open('binary_sensor.house_powerwall_grid_status'); R.grid = ELS.details.html; open('binary_sensor.bare_switch'); R.bare = ELS.details.html;"
                            " open('lock.front_door'); R.lock = ELS.details.html;", answer=f"() => [200, {json.dumps(self.binary())}]")
        grid = out["R"]["grid"]
        self.assertIn('<p class="means">House powerwall grid status: on means it is connected to power.</p>', grid)
        self.assertIn("<code>binary_sensor.house_powerwall_grid_status</code>", grid)
        self.assertIn('<time class="rel" datetime="2026-09-06T11:00:00+00:00"', grid)
        self.assertRegex(grid, r"<dt>Source</dt><dd>Home Assistant <code>/api/states</code>, device class <code>power</code></dd>")
        self.assertLess(grid.index('class="means"'), grid.index(">Act<"))
        self.assertIn("Bare switch: on or off, with no device class to judge it by.", out["R"]["bare"])
        self.assertIn("<dt>Source</dt><dd>Home Assistant <code>/api/states</code></dd>", out["R"]["bare"])
        self.assertIn("Front door: locked or unlocked; jammed means the lock did not finish moving.", out["R"]["lock"])

    def test_details_name_the_entity_and_why_it_has_no_state(self):
        out = self.run_page("open('lock.front_door'); R.det = ELS.details.html; open('no.such'); R.after = ELS.details.html;")
        det = out["R"]["det"]
        self.assertIn("<code>lock.front_door</code>", det)
        self.assertIn("Security", det)
        self.assertIn(f"Not read: {html.escape(self.doc['reader']['reason'], quote=False)}.", det)
        self.assertEqual(out["R"]["after"], det)

    def test_details_show_the_live_state_and_its_last_change(self):
        out = self.run_page("open('lock.front_door'); R.det = ELS.details.html; open('binary_sensor.smoke_sensors'); R.missing = ELS.details.html;",
                            answer=f"() => [200, {json.dumps(self.live())}]")
        self.assertIn("locked", out["R"]["det"])
        self.assertIn('<time class="rel" datetime="2026-09-06T11:40:00+00:00"', out["R"]["det"])
        self.assertNotIn("Not read", out["R"]["det"])
        self.assertIn(f"Not read: {home_screen.NOT_LISTED}.", out["R"]["missing"])

    def test_the_wall_display_flag_comes_from_the_address(self):
        self.assertIn("data-kiosk", self.run_page("", search="?kiosk=1")["root"])
        self.assertNotIn("data-kiosk", self.run_page("")["root"])

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", HOME_JS)
        self.assertNotIn("setAttribute('style'", HOME_JS)
        self.assertNotRegex(HOME_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", HOME_JS)
        # A page-level j/k handler is drift (the shell owns them through PAGE_LIST).
        self.assertNotRegex(HOME_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", HOME_JS)



class TheReader(TheScript):
    """sd:2486: Home reads /api/home through shell.read (read.js, sd:2418): who reads, the failed read and ?row=. sd:3522:
    it reads again on Read again and each minute while visible, and gives the shell's staleness band its reading time."""

    def test_the_page_reads_through_the_shared_reader(self):
        out = self.run_page("")
        self.assertEqual(out["reads"], [["/api/home", "the tiles"]])
        self.assertEqual(out["gets"], ["/api/home"])
        self.assertNotIn("async function getJSON", HOME_JS)

    def test_a_failed_read_says_so_and_shows_no_tile(self):
        out = self.run_page("R.groups = ELS.groups.html; R.det = ELS.details.html; R.sub = ELS.subhead.html;", answer="() => [500, { error: 'boom' }]")
        self.assertNotIn("· HA read", out["R"]["sub"] or "", "a failed read claimed Home Assistant was read")
        self.assertEqual(out["states"][-1], {"kind": "error", "text": "The tiles were not read: boom. Reload retries it.", "source": "/api/home"})
        self.assertEqual(out["R"]["groups"], "")
        self.assertIn("Select a tile", out["R"]["det"])

    def test_the_tiles_are_read_again_each_minute_and_on_read_again(self):
        # REGRESSION sd:3522: Home read /api/home once, so a wall display kept its first state until a reload.
        # sd:5638: Read again also reads the shown Tasks, Briefs or Reports view again (places.js, stood in here).
        out = self.run_page("""R.observed = [document.body.dataset.observed, document.body.dataset.interval];
R.again = 0; window.places = { again: () => { R.again++; } };
await window.PAGE_AGAIN.run(); await flush();
const every = INTERVALS.filter(i => i[1] === 60 * 1000).map(i => i[0]);
every.forEach(f => f()); await flush();
document.hidden = true; every.forEach(f => f()); await flush();""")
        self.assertEqual(out["R"]["observed"], [self.doc["read"], "1"])
        self.assertEqual(out["gets"], ["/api/home"] * 3, "the first read, Read again and one minute, and none while hidden")
        self.assertEqual(out["R"]["again"], 1, "Read again did not read the shown place view again")

    def test_the_minute_read_leaves_another_views_details_alone(self):
        # sd:5620: a Briefs row's Details fill the pane on Home; the tiles' minute read selects nothing while the Overview
        # is not shown, so it does not put "Select a tile" over them.
        out = self.run_page("""window.shell.view.current = () => 'briefs';
put(ELS.details, mk`<h2>A brief</h2>`);
INTERVALS.filter(i => i[1] === 60 * 1000).forEach(i => i[0]()); await flush(); R.det = ELS.details.html;
window.shell.view.current = () => 'overview';
INTERVALS.filter(i => i[1] === 60 * 1000).forEach(i => i[0]()); await flush(); R.back = ELS.details.html;""")
        self.assertEqual(out["gets"], ["/api/home"] * 3)
        self.assertEqual(out["R"]["det"], "<h2>A brief</h2>")
        self.assertIn("Select a tile", out["R"]["back"])

    def test_the_minute_read_shows_no_loading_state(self):
        # REGRESSION sd:3522: each minute read drew the loading state above the tiles, so the wall display jumped by its height.
        out = self.run_page("""R.first = OUT.states.length;
INTERVALS.filter(i => i[1] === 60 * 1000).forEach(i => i[0]()); await flush();""")
        self.assertEqual(out["gets"], ["/api/home"] * 2)
        self.assertEqual([s for s in out["states"][out["R"]["first"]:] if s and s["kind"] == "loading"], [])

    def test_a_failed_read_leaves_no_reading_time_for_the_staleness_band(self):
        out = self.run_page("R.observed = document.body.dataset.observed;", answer="() => [500, { error: 'boom' }]")
        self.assertIsNone(out["R"].get("observed"))

    def test_the_linked_tile_is_selected_and_opened_and_an_unlisted_one_is_not(self):
        """A guard: ?row= selected its tile before the reader; it still does, and only for a listed tile outside the wall display."""
        out = self.run_page("R.det = ELS.details.html;", search="?row=lock.front_door")
        self.assertIn("<code>lock.front_door</code>", out["R"]["det"])
        self.assertEqual(out["panes"], ["tab-details"])
        out = self.run_page("R.det = ELS.details.html;", search="?row=lock.back_door")
        self.assertIn("Select a tile", out["R"]["det"])
        self.assertEqual(out["panes"], [])
        out = self.run_page("R.det = ELS.details.html;", search="?kiosk=1&row=lock.front_door")
        self.assertNotIn("lock.front_door", out["R"]["det"])


class TheEditor(TheScript):
    """sd:3987: Edit tiles opens on the text the read showed, Save posts it with that text's digest, and each refusal says why."""

    def answer(self, post):
        return f"(path, body) => path === '/api/home' ? [200, {json.dumps(self.doc)}] : {post}"

    # The page's form starts hidden, as home.html has it.
    OPEN = "ELS.editor.hidden = true; document.getElementById('edit-tiles').listeners.click[0]();"
    SAVE = "await ELS.editor.listeners.submit[0]({ preventDefault() {} }); await flush();"

    def test_edit_tiles_opens_on_the_text_the_read_showed(self):
        out = self.run_page(self.OPEN + " R.text = ELS['editor-text'].value; R.hidden = ELS.editor.hidden; R.off = ELS['edit-tiles'].disabled;"
                            " R.from = ELS['editor-from'].textContent;")
        self.assertEqual(out["R"]["text"], EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual((out["R"]["hidden"], out["R"]["off"]), (False, False))
        self.assertIn("Save moves the list to the workflow database", out["R"]["from"])

    def test_save_posts_the_text_and_digest_then_reads_again_and_undo_posts_the_previous_text(self):
        saved = {"saved": home_screen.KEY, "text": "tile|G|lock.a|A\n", "digest": "d" * 64, "previous": "tile|G|lock.b|B\n"}
        out = self.run_page(self.OPEN + " ELS['editor-text'].value = 'tile|G|lock.a|A\\n'; " + self.SAVE
                            + " R.hidden = ELS.editor.hidden; OUT.toasts[OUT.toasts.length - 1].undo(); await flush();",
                            answer=self.answer(f"[200, {json.dumps(saved)}]"))
        digest = self.doc["config"]["editor"]["digest"]
        self.assertEqual(out["posts"], [["/api/home/tiles", {"text": "tile|G|lock.a|A\n", "before": digest}, 64],
                                        ["/api/home/tiles", {"text": "tile|G|lock.b|B\n", "before": "d" * 64}, 64]])
        self.assertTrue(out["R"]["hidden"])
        self.assertEqual(out["toasts"], [["Tile list saved", True], ["Tile list put back", False]])
        self.assertEqual(out["gets"], ["/api/home"] * 3, "the first read, one after the save and one after Undo")

    def test_a_refused_save_names_its_lines_and_keeps_the_editor_open(self):
        out = self.run_page(self.OPEN + self.SAVE + " R.out = ELS['editor-out'].textContent; R.hidden = ELS.editor.hidden;",
                            answer=self.answer("[400, { error: \"The tile list was not saved: line 2: 'X' is not an entity id (domain.object_id).\" }]"))
        self.assertIn("line 2: 'X' is not an entity id", out["R"]["out"])
        self.assertFalse(out["R"]["hidden"])
        self.assertEqual((out["gets"], out["toasts"]), (["/api/home"], []))

    def test_a_stale_save_writes_nothing_and_reads_again(self):
        out = self.run_page(self.OPEN + self.SAVE + " R.out = ELS['editor-out'].textContent;",
                            answer=self.answer("[409, { error: 'changed', reload: true }]"))
        self.assertIn("the list changed since this page read it", out["R"]["out"])
        self.assertEqual(out["gets"], ["/api/home"] * 2)

    def test_a_read_that_could_not_read_the_row_offers_no_editor(self):
        doc = json.loads(json.dumps(self.doc))
        doc["config"].update(state="error", editor=None, problems=["tile list not read: OperationalError: database is locked"])
        out = self.run_page("R.off = ELS['edit-tiles'].disabled;", answer=f"() => [200, {json.dumps(doc)}]")
        self.assertTrue(out["R"]["off"])
        self.assertEqual(out["states"][-1]["text"], "The tile list was not read: OperationalError: database is locked. Home Assistant was not called.")
        self.assertTrue(self.run_page("R.off = ELS['edit-tiles'].disabled;", answer="() => [500, { error: 'boom' }]")["R"]["off"])


class TheWebUI(BrowserSession):
    """sd:6163: a tile opens Home Assistant's web UI. The page links to `home_screen.WEB`; the server answers 302 to the
    REST base local-ha-mcp's settings name, so no document holds the host."""

    ENV = {"HA_TOKEN": HA_TOKEN, "HA_MCP_URL": "https://ha.example.test:8123/mcp_server/sse"}

    def follow(self, path, cookie=True):
        """The answer to one GET, its redirect not followed: status, Location, body."""
        import http.client

        host, port = self.base.removeprefix("http://").rsplit(":", 1)
        connection = http.client.HTTPConnection(host, int(port), timeout=30)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers={"Cookie": self.cookie} if cookie else {})
        answer = connection.getresponse()
        return answer.status, answer.getheader("Location"), answer.read().decode("utf-8")

    def test_the_document_says_whether_the_web_ui_opens_and_never_holds_its_host(self):
        doc = home_screen.document(self.connection, now=NOW, config=EXAMPLE)
        self.assertEqual((doc["web"]["path"], doc["web"]["available"]), ("/home/assistant", False))
        self.assertIn("HA_MCP_URL", doc["web"]["reason"])
        # No tile list, so Home Assistant itself is not called.
        with mock.patch.object(home_screen, "CONFIG", Path(_CONFIG.name) / "none.conf"), mock.patch.dict("os.environ", self.ENV):
            status, _, body = self.request("/api/home", headers={"Cookie": self.cookie})
        self.assertEqual((status, json.loads(body)["web"]), (200, {"path": "/home/assistant", "available": True, "reason": ""}))
        self.assertNotIn("ha.example.test", body)

    def test_the_link_answers_302_to_home_assistant_with_a_session_only(self):
        with mock.patch.dict("os.environ", self.ENV):
            self.assertEqual(self.follow("/home/assistant")[:2], (302, "https://ha.example.test:8123/"))
            status, location, _ = self.follow("/home/assistant", cookie=False)
        self.assertEqual((status, location), (403, None))

    def test_without_settings_the_link_names_why_and_goes_nowhere(self):
        status, location, body = self.follow("/home/assistant")
        self.assertEqual((status, location), (503, None))
        self.assertIn("HA_MCP_URL", body)


class TheWebTile(TheScript):
    """sd:6163: home.js draws the Home Assistant tile first: a link that opens in a new tab, or the reason it cannot."""

    def test_the_tile_links_to_the_web_ui_in_a_new_tab(self):
        doc = {**self.doc, "web": {"path": "/home/assistant", "available": True, "reason": ""}}
        out = self.run_page("R.groups = ELS.groups.html;", answer=f"() => [200, {json.dumps(doc)}]")
        web = out["R"]["groups"].split("</section>")[0]
        self.assertIn("Home Assistant", web)
        self.assertIn('<a class="open" href="/home/assistant" target="_blank" rel="noopener"', web)

    def test_without_settings_the_tile_is_unknown_with_the_reason_and_no_link(self):
        out = self.run_page("R.groups = ELS.groups.html;")
        web = out["R"]["groups"].split("</section>")[0]
        self.assertIn("Home Assistant", web)
        self.assertIn('data-state="unknown"', web)
        self.assertIn(html.escape(self.doc["web"]["reason"], quote=False), web)
        self.assertNotIn("/home/assistant", web)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "home", "Home", "/home", ("/api/home", "/api/home/tiles", "/api/home/service", "/api/home/map",
                                                     "/api/home/map/aerial", "/api/home/asset", "/api/home/asset/delete",
                                                     "/api/home/care", "/api/home/attachments", "/api/home/attachments/picture",
                                                     "/api/home/attach", "/api/home/koi", "/api/home/koi/water", "/api/home/koi/test",
                                                     "/api/home/tasks", "/api/home/briefs", "/api/home/reports")


if __name__ == "__main__":
    unittest.main()
