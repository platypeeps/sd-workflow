"""Home's Koi view (pack sd:8054): the fish and pond as home assets, the pond's care, and water changes read from Home
Assistant's history and logged on the pond. Home Assistant is never called: `home_koi._history` is patched, and the
entity ids are synthetic."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import home_koi, home_map, home_screen, v2
from sd_db import assets, writes
from support import NOW, ScreenCase
from test_v2_tasks import STAND_IN
from test_v2_today import OSASCRIPT

STATIC = Path(v2.__file__).resolve().parent / "static"
MARKUP_JS = (STATIC / "markup.js").read_text(encoding="utf-8")
KOI_JS = (STATIC / "home-koi.js").read_text(encoding="utf-8")
SCRIPTS = "script.pond_change_small|25\nscript.pond_change_big|50\n"
DAY = NOW[:10]


def history(*runs):
    """Home Assistant's minimal history: one list per entity, the first row naming it."""
    out = {}
    for entity, at in runs:
        series = out.setdefault(entity, [{"entity_id": entity, "state": "off", "last_changed": "2026-01-01T00:00:00+00:00"}])
        series += [{"state": "on", "last_changed": at}, {"state": "off", "last_changed": at.replace(":00+", ":30+")}]
    return list(out.values())


class TheKoi(ScreenCase):
    def setUp(self):
        super().setUp()
        writes.set_setting(self.connection, home_koi.KEY, SCRIPTS)
        self.pond = assets.save_asset(self.connection, kind="pond", name="Pond", fields={"volume_gal": 2000},
                                      geometry={"type": "Point", "coordinates": [1, 2]})
        self.runs = history(("script.pond_change_small", f"{DAY}T09:00:00+00:00"),
                            ("script.pond_change_big", "2020-01-01T09:00:00+00:00"))
        patch = mock.patch.object(home_koi, "_history", lambda *a, **k: self.runs)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_parse_takes_entity_and_percent(self):
        self.assertEqual(home_koi.parse(SCRIPTS), ([("script.pond_change_small", 25.0), ("script.pond_change_big", 50.0)], []))
        self.assertEqual(home_koi.parse("script.x|0\nnot an entity|5")[1],
                         ["line 1: expected <entity_id>|<percent 1-100>", "line 2: expected <entity_id>|<percent 1-100>"])

    def test_each_run_is_one_change_of_its_percent(self):
        found = home_koi.changes(self.runs, [("script.pond_change_small", 25.0), ("script.pond_change_big", 50.0)])
        self.assertEqual([(c["entity"], c["percent"]) for c in found],
                         [("script.pond_change_big", 50.0), ("script.pond_change_small", 25.0)])

    def test_the_document_shows_days_since_and_the_weeks_percent_then_logs_once(self):
        doc = home_koi.document(self.connection, now=NOW)
        water = doc["water"]
        self.assertEqual((water["days_since"], water["week_percent"], water["week_gallons"], water["unlogged"]),
                         (0, 25.0, 500.0, 2))
        self.assertEqual(water["average_week_percent"], 6.25)
        self.assertEqual(home_koi.log_water({}, now=NOW)(self.connection), {"added": 2})
        self.assertEqual(home_koi.log_water({}, now=NOW)(self.connection), {"added": 0})
        self.runs = []  # Home Assistant forgot them: the log still holds both
        doc = home_koi.document(self.connection, now=NOW)
        self.assertEqual((len(doc["water"]["log"]), doc["water"]["unlogged"], doc["water"]["week_percent"]), (2, 0, 25.0))

    def test_a_failed_read_names_its_reason_and_the_log_still_shows(self):
        def down(*a, **k):
            raise home_screen._Failed("HA did not answer", "Home Assistant did not answer: OSError")

        with mock.patch.object(home_koi, "_history", down):
            doc = home_koi.document(self.connection, now=NOW)
            self.assertEqual(home_koi.log_water({}, now=NOW)(self.connection)[0], 503)
        self.assertEqual(doc["state"], "partial")
        self.assertIn("Water changes not read from Home Assistant", doc["reason"])

    def test_fish_are_koi_assets_off_the_map_and_pond_care_recurs(self):
        fish = home_map.save({"kind": "koi", "name": "Goldie", "fields": {"variety": "Ogon",
                                                                         "log": [{"date": DAY, "size_in": 14, "weight_lb": 2}]}})
        fish = fish(self.connection)
        doc = home_koi.document(self.connection, now=NOW)
        self.assertEqual([f["name"] for f in doc["fish"]], ["Goldie"])
        self.assertNotIn(fish["id"], [a["id"] for a in home_map.document(self.connection, now=NOW)["assets"]])
        self.assertIn("pond", home_map.document(self.connection, now=NOW)["kinds"])
        self.assertNotIn("koi", home_map.document(self.connection, now=NOW)["kinds"])
        title, rule = home_koi.CARE[0]
        home_map.care({"asset": self.pond["id"], "title": title, "due": "2026-01-01", "recurrence": rule}, "operator")(self.connection)
        self.assertTrue(home_koi.document(self.connection, now=NOW)["pond"]["care"]["overdue"])

    def test_no_pond_says_to_add_one(self):
        assets.delete_asset(self.connection, self.pond["id"])
        doc = home_koi.document(self.connection, now=NOW)
        self.assertIsNone(doc["pond"])
        self.assertIn("add a pond on the Map", doc["pond_reason"])
        self.assertEqual(home_koi.log_water({}, now=NOW)(self.connection)[0], 409)

    def test_the_routes_are_registered(self):
        self.assertIsNotNone(v2.api("/api/home/koi"))
        self.assertIsNotNone(v2.action("/api/home/koi/water"))


class TheKoiScript(unittest.TestCase):
    DOC = {"state": "read", "reason": "", "pond_reason": "", "tests": ["ammonia", "ph"],
           "care": [{"title": "Feed the koi", "recurrence": "FREQ=DAILY"}],
           "pond": {"id": 1, "kind": "pond", "name": "Pond", "geometry": None, "photo": None, "fields": {"tests": []},
                    "care": {"items": [], "overdue": False}},
           "fish": [{"id": 2, "kind": "koi", "name": "Goldie", "photo": None, "care": {"items": [], "overdue": False},
                     "fields": {"variety": "Ogon", "log": [{"date": "2026-01-01", "size_in": 14, "weight_lb": 2}]}}],
           "water": {"log": [{"at": "2026-01-02T09:00:00+00:00", "percent": 25, "entity": "script.pond_change_small"}],
                     "unlogged": 1, "days_since": 3, "week_percent": 25, "average_week_percent": 6.25, "week_gallons": 500}}

    def test_the_view_draws_the_fish_and_water_and_logs_unlogged_changes_once(self):
        script = (STAND_IN + MARKUP_JS + f"\nconst DOC = {json.dumps(self.DOC)};\n" + r"""
window.shell = { getJSON: p => { OUT.gets.push(p); return Promise.resolve(JSON.parse(JSON.stringify(DOC))); },
  post: (p, b) => { OUT.posts.push([p, b]); return Promise.resolve({ added: 1 }); }, toast: m => OUT.toasts.push(m), itemHref: n => '/tasks?row=' + n };
const flush = () => new Promise(r => r()).then(() => null).then(() => null).then(() => null).then(() => null);
""" + KOI_JS + r"""
var R = {};
(async () => { try {
document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'koi' } })); await flush(); await flush();
R.fish = ELS['koi-fish'].html; R.water = ELS['koi-water'].html; R.care = ELS['koi-care'].html;
} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();
function run() { OUT.R = R; return JSON.stringify(OUT); }
""")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        r = out["R"]
        self.assertIn("<td>Goldie</td><td>Ogon</td>", r["fish"])
        self.assertIn("14 in", r["fish"])
        self.assertIn("<dd>3 d</dd>", r["water"])
        self.assertIn("25 % · 500 gal", r["water"])
        self.assertIn("Feed the koi", r["care"])
        self.assertEqual(out["posts"], [["/api/home/koi/water", {}]])
        self.assertEqual(out["gets"], ["/api/home/koi", "/api/home/koi"])


if __name__ == "__main__":
    unittest.main()
