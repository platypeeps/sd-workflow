"""Home's property map (pack sd:8053): the address from a setting row, the county's aerial cached, the assets and their
care, and the Map view's script. The county is never called: `home_map._get` is patched, and the address is synthetic."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import home_map, v2
from sd_db import assets, writes
from support import NOW, ScreenCase
from test_v2_tasks import STAND_IN
from test_v2_today import OSASCRIPT

STATIC = Path(v2.__file__).resolve().parent / "static"
MARKUP_JS = (STATIC / "markup.js").read_text(encoding="utf-8")
MAP_JS = (STATIC / "home-map.js").read_text(encoding="utf-8")
ADDRESS = "1 Example Way, Example Town, CO"
JPEG = b"\xff\xd8\xff\xe0 synthetic"


def county(calls):
    """The locator and the aerial, as the county answers them; each call is recorded."""

    def get(url, *, timeout):
        calls.append(url)
        if "findAddressCandidates" in url:
            return json.dumps({"candidates": [{"address": "1 EXAMPLE WAY", "location": {"x": 3200000.0, "y": 1650000.0}}]}).encode()
        return JPEG

    return get


class TheMap(ScreenCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        patch = mock.patch.object(home_map, "_get", county(self.calls))
        patch.start()
        self.addCleanup(patch.stop)

    def test_no_address_says_where_to_set_it_and_still_lists_the_assets(self):
        assets.save_asset(self.connection, kind="tree", name="Oak", geometry={"type": "Point", "coordinates": [1, 2]})
        doc = home_map.document(self.connection, now=NOW)
        self.assertEqual((doc["state"], doc["bbox"], doc["aerial"]), ("partial", None, None))
        self.assertIn("set dashboard.home_map on Settings", doc["reason"])
        self.assertEqual([a["name"] for a in doc["assets"]], ["Oak"])
        self.assertEqual(self.calls, [])

    def test_the_address_is_placed_once_and_the_aerial_fetched_once(self):
        writes.set_setting(self.connection, home_map.KEY, ADDRESS)
        doc = home_map.document(self.connection, now=NOW)
        self.assertEqual(doc["bbox"], [3199800.0, 1649800.0, 3200200.0, 1650200.0])
        self.assertEqual(doc["aerial"], "/api/home/map/aerial")
        self.assertEqual(home_map.aerial_route(self.connection), (200, JPEG, "image/jpeg"))
        home_map.document(self.connection, now=NOW)
        self.assertEqual(home_map.aerial_route(self.connection)[1], JPEG)
        self.assertEqual(len(self.calls), 2, self.calls)
        self.assertIn("outSR=2232", self.calls[0])
        self.assertIn("bboxSR=2232", self.calls[1])
        self.assertIn("size=1843%2C1843", self.calls[1])

    def test_a_failed_locator_names_its_reason(self):
        writes.set_setting(self.connection, home_map.KEY, ADDRESS)
        with mock.patch.object(home_map, "_get", side_effect=OSError("down")):
            doc = home_map.document(self.connection, now=NOW)
            self.assertEqual(home_map.aerial_route(self.connection)[0], 503)
        self.assertEqual(doc["state"], "partial")
        self.assertIn("address locator did not answer", doc["reason"])

    def test_the_setting_takes_one_line(self):
        self.assertEqual(home_map.parse(f"# home\n{ADDRESS}\n"), (ADDRESS, []))
        self.assertEqual(home_map.parse("a\nb")[0], None)

    def test_add_move_care_and_overdue(self):
        oak = home_map.save({"kind": "tree", "name": "Oak", "geometry": {"type": "Point", "coordinates": [5, 6]},
                             "fields": {"species": "Bur oak"}})(self.connection)
        moved = home_map.save({"id": oak["id"], "kind": "tree", "name": "Oak",
                               "geometry": {"type": "Point", "coordinates": [7, 8]}})(self.connection)
        self.assertEqual(moved["geometry"]["coordinates"], [7.0, 8.0])
        self.assertEqual(home_map.save({"kind": "moat", "name": "x"})(self.connection)[0], 400)
        filed = home_map.care({"asset": oak["id"], "title": "Water the oak", "due": "2026-01-01",
                               "recurrence": "FREQ=WEEKLY"}, "operator")(self.connection)
        row = self.connection.execute("SELECT fields, recurrence FROM item WHERE id = ?", (filed["item"],)).fetchone()
        self.assertEqual((json.loads(row[0]), row[1]), ({"area": "home", "asset": oak["id"]}, "FREQ=WEEKLY"))
        doc = home_map.document(self.connection, now=NOW)
        self.assertTrue(doc["assets"][0]["care"]["overdue"])
        self.assertEqual(home_map.care({"asset": 99, "title": "x", "due": "2026-01-01", "recurrence": "FREQ=WEEKLY"},
                                       "operator")(self.connection)[0], 404)
        with self.assertRaises(ValueError):
            home_map.care({"asset": oak["id"], "title": "x", "due": "2026-01-01", "recurrence": "FREQ=HOURLY"}, "operator")
        self.assertEqual(home_map.delete({"id": oak["id"]})(self.connection)["name"], "Oak")
        self.assertEqual(home_map.delete({"id": oak["id"]})(self.connection)[0], 404)

    def test_the_routes_are_registered(self):
        for path in ("/api/home/map", "/api/home/map/aerial"):
            self.assertIsNotNone(v2.api(path), path)
        for path in ("/api/home/asset", "/api/home/asset/delete", "/api/home/care"):
            self.assertIsNotNone(v2.action(path), path)


class TheMapScript(unittest.TestCase):
    """home-map.js: the SVG over the aerial, the overdue ring, a line, and Add placing a point and posting it."""

    DOC = {"state": "read", "reason": "", "bbox": [0, 0, 400, 400], "aerial": "/api/home/map/aerial", "pixel_ft": 0.217,
           "kinds": ["gas-shutoff", "water-line", "tree"], "lines": ["water-line"], "plant_fields": ["species", "planted"],
           "recur": ["FREQ=WEEKLY"],
           "assets": [{"id": 1, "kind": "tree", "name": "Oak", "geometry": {"type": "Point", "coordinates": [100, 300]},
                       "fields": {}, "photo": None, "care": {"items": [{"id": 9, "title": "Water", "due": "2026-01-01"}], "overdue": True}},
                      {"id": 2, "kind": "water-line", "name": "Main", "geometry": {"type": "LineString", "coordinates": [[0, 0], [10, 20]]},
                       "fields": {}, "photo": None, "care": {"items": [], "overdue": False}}]}

    def run_js(self, body):
        script = (STAND_IN + MARKUP_JS + f"\nconst DOC = {json.dumps(self.DOC)};\n" + r"""
window.shell = { getJSON: p => { OUT.gets.push(p); return Promise.resolve(JSON.parse(JSON.stringify(DOC))); },
  post: (p, b) => { OUT.posts.push([p, b]); return Promise.resolve({ id: 3 }); }, toast: m => OUT.toasts.push(m), confirm: () => Promise.resolve(true) };
globalThis.DOMPoint = function (x, y) { this.matrixTransform = () => ({ x, y }); };
function FormData(f) { this.get = k => f.values[k]; this.entries = () => Object.entries(f.values); }
const flush = () => new Promise(r => r()).then(() => null).then(() => null).then(() => null);
""" + MAP_JS + "\nvar R = {};\n(async () => { try {\n" + body
                  + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\nfunction run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    SHOW = "document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'map' } })); await flush();\n"

    def test_the_map_draws_the_aerial_the_overdue_ring_and_the_line(self):
        out = self.run_js(self.SHOW + "R.svg = ELS['map-svg'].html; R.rows = ELS['map-rows'].html;")
        svg = out["R"]["svg"]
        self.assertEqual(out["gets"], ["/api/home/map"])
        self.assertIn('viewBox="0 -400 400 400"', svg)
        self.assertIn('<image href="/api/home/map/aerial"', svg)
        self.assertIn('class="asset k-tree overdue"', svg)
        self.assertIn('transform="translate(100.00 -300.00)"', svg)
        self.assertIn('<circle class="ring"', svg)
        self.assertIn('points="0.00,0.00 10.00,-20.00"', svg)
        self.assertIn("■ overdue", out["R"]["rows"])

    def test_add_places_a_point_where_the_map_is_clicked_and_posts_it(self):
        out = self.run_js(self.SHOW + r"""
const click = (sel, more = {}) => (DOC_LISTENERS.click || []).forEach(f => f(Object.assign({ target: { closest: s => s === sel ? {} : null } }, more)));
click('#map-add'); R.tools = ELS['map-tools'].html;
ELS['map-canvas'] = Object.assign(El('map-canvas'), { getScreenCTM: () => ({ inverse: () => ({}) }) });
(DOC_LISTENERS.change || []).forEach(f => f({ target: { id: 'map-kind', value: 'tree' } }));
ELS['map-canvas'] = Object.assign(El('map-canvas'), { getScreenCTM: () => ({ inverse: () => ({}) }) });
click('#map-canvas', { clientX: 50, clientY: -60 });
R.form = ELS['map-form'].html;
const form = Object.assign(ELS['asset-form'], { id: 'asset-form', values: { name: 'Maple', 'f-species': 'Red maple', photo: '' } });
(DOC_LISTENERS.submit || []).forEach(f => f({ target: form, preventDefault() {} })); await flush();""")
        self.assertIn('aria-pressed="true"', out["R"]["tools"])
        self.assertIn("New tree", out["R"]["form"])
        self.assertIn('name="f-species"', out["R"]["form"])
        self.assertEqual(out["posts"][0], ["/api/home/asset", {"kind": "tree", "name": "Maple",
                                                                "geometry": {"type": "Point", "coordinates": [50, 60]},
                                                                "fields": {"species": "Red maple"}, "photo": None}])


if __name__ == "__main__":
    unittest.main()
