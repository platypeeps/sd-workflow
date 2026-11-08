"""Home's property map (pack sd:8053): the address from a setting row, the county's aerial cached, the assets and their
care, and the Map view's script. The county is never called: `home_map._get` is patched, and the address is synthetic."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import home_map, settings_screen, v2
from sd_db import assets, writes
from support import NOW, ScreenCase
from test_v2_tasks import STAND_IN
from test_v2_today import OSASCRIPT

STATIC = Path(v2.__file__).resolve().parent / "static"
MARKUP_JS = (STATIC / "markup.js").read_text(encoding="utf-8")
ATTACH_JS = (STATIC / "home-attach.js").read_text(encoding="utf-8")
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
        self.assertEqual(doc["bbox"], [3199800.0, 1649800.0, 3200800.0, 1650200.0])  # sd:8085: 800 ft east
        self.assertEqual(doc["aerial"], "/api/home/map/aerial")
        self.assertEqual(home_map.aerial_route(self.connection), (200, JPEG, "image/jpeg"))
        home_map.document(self.connection, now=NOW)
        self.assertEqual(home_map.aerial_route(self.connection)[1], JPEG)
        self.assertEqual(len(self.calls), 2, self.calls)
        self.assertIn("outSR=2232", self.calls[0])
        self.assertIn("bboxSR=2232", self.calls[1])
        self.assertIn("size=4608%2C1843", self.calls[1])

    def test_a_saved_extent_changes_the_box_and_the_export_and_refetches_once(self):
        """sd:8110: the four edges move the box and the export size; the cache is keyed by them, so a change asks the
        county for one new aerial and the place is not geocoded again."""
        writes.set_setting(self.connection, home_map.KEY, ADDRESS)
        home_map.aerial_route(self.connection)
        settings_screen.request_row({"key": home_map.EXTENT_KEY, "value": "100,300,150,250", "before": None})(self.connection)
        doc = home_map.document(self.connection, now=NOW)
        self.assertEqual(doc["bbox"], [3199900.0, 1649850.0, 3200300.0, 1650250.0])
        self.assertEqual(doc["extent"]["size"], [1843, 1843])
        self.assertEqual(doc["extent"]["row"], "100,300,150,250")
        self.assertEqual(home_map.aerial_route(self.connection)[0], 200)
        home_map.aerial_route(self.connection)
        self.assertEqual(len(self.calls), 3, self.calls)
        self.assertIn("size=1843%2C1843", self.calls[2])
        self.assertIn("bbox=3199900.000%2C1649850.000%2C3200300.000%2C1650250.000", self.calls[2])

    def test_an_out_of_range_edge_is_refused_naming_the_limit(self):
        for value, said in (("40,800,200,200", "west is 40; each edge is 50 to 2000 ft"),
                            ("200,800,200,2500", "north is 2500; each edge is 50 to 2000 ft"),
                            ("200,800,200", "four numbers")):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                settings_screen.request_row({"key": home_map.EXTENT_KEY, "value": value, "before": None})
            self.assertIn(said, str(raised.exception))

    def test_an_area_past_the_service_maximum_is_fetched_coarser_and_says_so(self):
        writes.set_setting(self.connection, home_map.KEY, ADDRESS)
        writes.set_setting(self.connection, home_map.EXTENT_KEY, "2000,2000,2000,2000")
        doc = home_map.document(self.connection, now=NOW)
        width, height = doc["extent"]["size"]
        self.assertLessEqual((width, height), (15000, 4100))
        self.assertEqual(height, 4100)
        self.assertIn("fetched at 0.976 ft per pixel", doc["extent"]["coarse"])
        home_map.aerial_route(self.connection)
        self.assertIn(f"size={width}%2C4100", self.calls[-1])

    def test_an_asset_outside_the_area_is_listed_off_the_map(self):
        writes.set_setting(self.connection, home_map.KEY, ADDRESS)
        assets.save_asset(self.connection, kind="tree", name="Near", geometry={"type": "Point", "coordinates": [3200100, 1650000]})
        assets.save_asset(self.connection, kind="tree", name="Far", geometry={"type": "Point", "coordinates": [3201500, 1650000]})
        doc = home_map.document(self.connection, now=NOW)
        self.assertEqual({a["name"]: a["off_map"] for a in doc["assets"]}, {"Near": False, "Far": True})
        far = next(a for a in doc["assets"] if a["name"] == "Far")
        self.assertEqual(far["geometry"]["coordinates"], [3201500.0, 1650000.0])

    def test_settings_shows_the_extent_row(self):
        writes.set_setting(self.connection, home_map.EXTENT_KEY, "200,800,200,200")
        rows = {row["key"]: row for row in settings_screen.rows(self.connection)["dashboard"]}
        self.assertEqual(rows[home_map.EXTENT_KEY]["value"], "200,800,200,200")

    def test_a_failed_locator_names_its_reason(self):
        writes.set_setting(self.connection, home_map.KEY, ADDRESS)
        with mock.patch.object(home_map, "_get", side_effect=OSError("down")):
            doc = home_map.document(self.connection, now=NOW)
            self.assertEqual(home_map.aerial_route(self.connection)[0], 503)
        self.assertEqual(doc["state"], "partial")
        self.assertIn("address locator did not answer", doc["reason"])

    def test_a_malformed_locator_answer_names_what_is_wrong(self):
        """sd:8076: a reply that is not a list of candidates, a location without numbers, or an ArcGIS error is named,
        not raised and not called no match."""
        writes.set_setting(self.connection, home_map.KEY, ADDRESS)
        for answer, said in (({"candidates": ["x"]}, "not a list of candidates"),
                             ({"candidates": {"address": "x"}}, "not a list of candidates"),
                             ({"candidates": [{"location": {"x": None, "y": 1}}]}, "no numeric x and y"),
                             ({"candidates": [{"location": {"x": "a", "y": 1}}]}, "no numeric x and y"),
                             ({"candidates": [{"location": {"x": True, "y": 1}}]}, "no numeric x and y"),
                             ({"error": {"code": 498, "message": "Invalid token"}}, "answered an error: 498 Invalid token"),
                             (["not", "an", "object"], "not a JSON object")):
            with self.subTest(answer=answer), mock.patch.object(home_map, "_get", return_value=json.dumps(answer).encode()):
                with self.assertRaises(LookupError) as raised:
                    home_map.place(ADDRESS)
                self.assertIn(said, str(raised.exception))
        self.assertFalse((home_map._cache(ADDRESS) / "place.json").exists())

    def test_a_cached_aerial_that_is_not_a_jpeg_is_fetched_again(self):
        writes.set_setting(self.connection, home_map.KEY, ADDRESS)
        home_map.place(ADDRESS)
        cached = home_map.aerial_file(ADDRESS)
        cached.write_bytes(b"<html>half")
        self.assertEqual(home_map.aerial(ADDRESS), JPEG)
        self.assertEqual(cached.read_bytes(), JPEG)

    def test_a_failed_cache_write_leaves_no_half_aerial_and_still_answers(self):
        """sd:8076: the aerial is written to a temp file and renamed, so a cut write never stands as the cache."""
        writes.set_setting(self.connection, home_map.KEY, ADDRESS)
        home_map.place(ADDRESS)

        def cut(path, data):
            with open(path, "wb") as out:
                out.write(data[:3])
            raise OSError("disk full")

        with mock.patch.object(Path, "write_bytes", cut):
            self.assertEqual(home_map.aerial(ADDRESS), JPEG)
        self.assertFalse((home_map.aerial_file(ADDRESS)).exists())

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
        # sd:8112: an asset's Drive folder, through hoa_attach.HOME.
        for path in ("/api/home/attachments", "/api/home/attachments/picture"):
            self.assertIsNotNone(v2.api(path), path)
        self.assertIsNotNone(v2.action("/api/home/attach"), "/api/home/attach")


class TheMapScript(unittest.TestCase):
    """home-map.js: the SVG over the aerial, the overdue ring, a line, and Add placing a point and posting it."""

    DOC = {"state": "read", "reason": "", "bbox": [0, 0, 400, 400], "aerial": "/api/home/map/aerial", "pixel_ft": 0.217,
           "kinds": ["gas-shutoff", "water-line", "tree"], "lines": ["water-line"], "plant_fields": ["species", "planted"],
           "recur": ["FREQ=WEEKLY"],
           "assets": [{"id": 1, "kind": "tree", "name": "Oak", "geometry": {"type": "Point", "coordinates": [100, 300]},
                       "fields": {}, "photo": None, "care": {"items": [{"id": 9, "title": "Water", "due": "2026-01-01"}], "overdue": True}},
                      {"id": 2, "kind": "water-line", "name": "Main", "geometry": {"type": "LineString", "coordinates": [[0, 0], [10, 20]]},
                       "fields": {}, "photo": None, "care": {"items": [], "overdue": False}},
                      {"id": 4, "kind": "gas-shutoff", "name": "Elm", "geometry": {"type": "Point", "coordinates": [900, 300]},
                       "fields": {}, "photo": None, "care": {"items": [], "overdue": False}, "off_map": True}],
           "extent": {"edges": {"west": 200, "east": 800, "south": 200, "north": 200}, "row": None,
                      "key": "dashboard.home_map_extent", "limits": [50, 2000], "size": [4608, 1843], "coarse": "", "problem": ""}}

    def run_js(self, body):
        script = (STAND_IN + MARKUP_JS + f"\nconst DOC = {json.dumps(self.DOC)};\n" + r"""
window.shell = { getJSON: p => { OUT.gets.push(p); return Promise.resolve(JSON.parse(JSON.stringify(DOC))); },
  post: (p, b) => { OUT.posts.push([p, b]); return Promise.resolve({ id: 3 }); }, toast: m => OUT.toasts.push(m), confirm: () => Promise.resolve(true),
  url: q => (OUT.urls = OUT.urls || []).push(String(q)), itemHref: id => '/item/' + id,
  list: { fseg: (key, name, items) => window.markup.html`<div class="fseg">${items.map(i => window.markup.html`<button type="button" data-f="${key}" data-v="${i.v}" aria-pressed="${String(!!i.pressed)}"${i.off ? window.markup.html` aria-disabled="true"` : ''} title="${i.title}">${i.label}</button>`)}</div>` } };
globalThis.DOMPoint = function (x, y) { this.matrixTransform = () => ({ x, y }); };
function FormData(f) { this.get = k => f.values[k]; this.entries = () => Object.entries(f.values); }
const flush = () => new Promise(r => r()).then(() => null).then(() => null).then(() => null);
""" + ATTACH_JS + MAP_JS + "\nvar R = {};\n(async () => { try {\n" + body
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

    def test_a_group_tile_hides_its_assets_and_keeps_its_count(self):
        """sd:8085: one tile per kind group; Plants off hides the tree's marker and row, every count stays, the URL
        keeps the choice, and a group with no assets is dim (aria-disabled), not hidden."""
        out = self.run_js(self.SHOW + r"""R.before = ELS['map-filters'].html;
const tile = v => (DOC_LISTENERS.click || []).forEach(f => f({ target: { closest: s => s === '#map-filters [data-f="group"]' ? { dataset: { v } } : null } }));
tile('plants'); R.svg = ELS['map-svg'].html; R.rows = ELS['map-rows'].html; R.after = ELS['map-filters'].html;""")
        before, after, svg, rows = (out["R"][k] for k in ("before", "after", "svg", "rows"))
        self.assertIn('data-v="plants" aria-pressed="true"', before)
        self.assertIn('data-v="plants" aria-pressed="false"', after)
        self.assertIn('Plants <span class="n">1</span>', after)
        self.assertIn('data-v="energy" aria-pressed="true" aria-disabled="true"', after)
        self.assertNotIn("k-tree", svg)
        self.assertIn("k-water-line", svg)
        self.assertNotIn("Oak", rows)
        self.assertIn("Main", rows)
        self.assertEqual(out["urls"][-1], "mapoff=plants")

    def test_an_asset_off_the_map_is_listed_and_the_area_saves_revision_checked(self):
        out = self.run_js(self.SHOW + r"""R.rows = ELS['map-rows'].html; R.note = ELS['map-note'].html; R.area = ELS['map-area'].html;
const form = Object.assign(El('area-form'), { id: 'area-form', values: { west: '100', east: '300', south: '150', north: '250' } });
(DOC_LISTENERS.submit || []).forEach(f => f({ target: form, preventDefault() {} })); await flush();""")
        self.assertIn('Elm <span class="off">· off the map</span>', out["R"]["rows"])
        self.assertIn("1 asset is outside the area", out["R"]["note"])
        self.assertIn('name="west"', out["R"]["area"])
        self.assertIn('min="50" max="2000"', out["R"]["area"])
        self.assertEqual(out["posts"][0], ["/api/settings/row", {"key": "dashboard.home_map_extent",
                                                                  "value": "100,300,150,250", "before": None}])

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

    def test_a_selected_asset_lists_its_attachments_with_thumbnails_and_upload(self):
        """sd:8112: the Details of a selected asset read /api/home/attachments; a picture is a thumbnail through the
        server, a Reference links its target, and Upload and Reference forms follow."""
        listed = {"asset": 1, "folder": "https://drive.google.com/drive/folders/OAK", "files": [
            {"id": "PIC1234567", "name": "oak.jpg", "mime": "image/jpeg", "link": "https://drive.google.com/open?id=PIC1234567", "kind": "picture"},
            {"id": "LNK1234567", "name": "Plan.url", "mime": "text/plain", "link": "https://drive.google.com/open?id=LNK1234567",
             "kind": "link", "target": "https://drive.google.com/file/d/TARGET/view"}]}
        out = self.run_js(self.SHOW + f"const LISTED = {json.dumps(listed)};\n" + r"""
const was = window.shell.getJSON; window.shell.getJSON = p => p.startsWith('/api/home/attachments?') ? (OUT.gets.push(p), Promise.resolve(LISTED)) : was(p);
(DOC_LISTENERS.click || []).forEach(f => f({ target: { closest: s => s === '[data-asset]' ? { dataset: { asset: '1' } } : null } }));
await flush(); await flush(); R.form = ELS['map-form'].html; R.attach = ELS['map-attach'] ? ELS['map-attach'].html : '';""")
        self.assertIn("/api/home/attachments?asset=1", out["gets"])
        self.assertIn('<div id="map-attach"', out["R"]["form"])
        self.assertIn('<img class="thumb" src="/api/home/attachments/picture?id=PIC1234567"', out["R"]["attach"])
        self.assertIn('href="https://drive.google.com/file/d/TARGET/view" target="_blank" rel="noopener">Plan.url', out["R"]["attach"])
        self.assertIn('data-attach="upload"', out["R"]["attach"])
        self.assertIn('data-attach="reference"', out["R"]["attach"])


if __name__ == "__main__":
    unittest.main()
