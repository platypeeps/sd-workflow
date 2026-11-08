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
ATTACH_JS = (STATIC / "home-attach.js").read_text(encoding="utf-8")
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

    def test_a_water_test_appends_to_the_current_row_and_keeps_the_logged_changes(self):
        home_koi.log_water({}, now=NOW)(self.connection)
        out = home_koi.log_test({"ammonia": 0.25, "ph": 7.4}, now=NOW)(self.connection)
        self.assertEqual(out, {"tests": 1})
        fields = assets.get_asset(self.connection, self.pond["id"])["fields"]
        self.assertEqual(len(fields["water_log"]), 2)
        self.assertEqual(fields["tests"], [{"date": DAY, "ammonia": 0.25, "ph": 7.4}])
        for bad in ({}, {"chlorine": 1}, {"ph": "7"}, {"ph": float("nan")}, {"ph": True}):
            with self.assertRaises(ValueError):
                home_koi.log_test(bad, now=NOW)

    def test_a_test_written_during_the_home_assistant_wait_survives_the_water_log(self):
        """sd:8076: log_water reads the pond again after Home Assistant answers, so a write in the wait is kept."""
        runs = self.runs

        def slow(*a, **k):
            home_koi.log_test({"ph": 7.4}, now=NOW)(self.connection)
            return runs

        with mock.patch.object(home_koi, "_history", slow):
            self.assertEqual(home_koi.log_water({}, now=NOW)(self.connection), {"added": 2})
        fields = assets.get_asset(self.connection, self.pond["id"])["fields"]
        self.assertEqual((len(fields["water_log"]), len(fields["tests"])), (2, 1))

    def test_a_water_test_keeps_the_local_date(self):
        """sd:8076: 03:00 UTC is still the evening before in a zone west of UTC; the test keeps that local day."""
        import os
        import time

        with mock.patch.dict(os.environ, {"TZ": "America/Denver"}):
            time.tzset()
            try:
                home_koi.log_test({"ph": 7.4}, now="2026-09-07T03:00:00Z")(self.connection)
            finally:
                self.addCleanup(time.tzset)
        self.assertEqual(assets.get_asset(self.connection, self.pond["id"])["fields"]["tests"][0]["date"], "2026-09-06")

    def test_the_daily_sync_logs_the_changes_with_nobody_on_the_page(self):
        """sd:8077: `dashboard.sh koi-sync` runs log_water once on the default store; a scheduler runs it daily."""
        import contextlib
        import io

        from sd_db import connect

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sd_db.connect", lambda: connect(self.path)), contextlib.redirect_stdout(out):
            self.assertEqual(home_koi.main([]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"added": 2})
        self.assertEqual(len(assets.get_asset(self.connection, self.pond["id"])["fields"]["water_log"]), 2)
        writes.set_setting(self.connection, home_koi.KEY, None)
        with mock.patch("sd_db.connect", lambda: connect(self.path)), contextlib.redirect_stderr(err):
            self.assertEqual(home_koi.main([]), 1)
        self.assertIn("koi-sync: No water-change scripts", err.getvalue())
        with contextlib.redirect_stderr(err):
            self.assertEqual(home_koi.main(["extra"]), 2)

    def test_a_stale_pond_save_keeps_the_water_log_and_tests_the_server_wrote(self):
        stale = dict(self.pond["fields"])
        home_koi.log_water({}, now=NOW)(self.connection)
        home_koi.log_test({"ph": 7.4}, now=NOW)(self.connection)
        home_map.save({"id": self.pond["id"], "kind": "pond", "name": "Pond", "geometry": self.pond["geometry"],
                       "fields": {**stale, "volume_gal": 2500}, "photo": None})(self.connection)
        fields = assets.get_asset(self.connection, self.pond["id"])["fields"]
        self.assertEqual((fields["volume_gal"], len(fields["water_log"]), len(fields["tests"])), (2500, 2, 1))
        self.assertEqual(fields.get("water_synced", {}).get("to"), "2026-09-06T12:00:00+00:00")

    def synced(self, *days):
        """Sync on each `2026-MM-DD` at noon UTC; the stored fields after the last."""
        for day in days:
            home_koi.log_water({}, now=f"{day}T12:00:00Z")(self.connection)
        return assets.get_asset(self.connection, self.pond["id"])["fields"]

    def test_the_first_sync_proves_only_the_recorder_days(self):
        # Lane review (pack sd:8054, round 3): a first sync stored the
        # history's oldest row as covered and called the 28-day total
        # complete. A read proves only the recorder's KEEP_DAYS.
        fields = self.synced(DAY)
        self.assertEqual(fields["water_synced"], {"since": "2026-08-27T12:00:00+00:00", "to": "2026-09-06T12:00:00+00:00"})
        water = home_koi.document(self.connection, now=NOW)["water"]
        self.assertEqual((water["incomplete_since"], water["sync_due"]), ({"week": None, "month": "2026-08-10"}, False))

    def test_syncs_closer_than_the_recorder_days_leave_no_gap(self):
        fields = self.synced("2026-08-01", "2026-08-10", "2026-08-19", "2026-08-28", DAY)
        self.assertEqual(fields["water_synced"]["since"], "2026-07-22T12:00:00+00:00")
        self.assertNotIn("water_gaps", fields)
        water = home_koi.document(self.connection, now=NOW)["water"]
        self.assertEqual(water["incomplete_since"], {"week": None, "month": None})

    def test_a_sync_after_the_recorder_days_marks_the_gap(self):
        # Nobody opened the view for longer than Home Assistant keeps
        # history, so the changes between the last sync and the oldest
        # day the recorder still keeps are gone.
        fields = self.synced("2026-08-17", DAY)
        self.assertEqual(fields["water_gaps"], [{"from": "2026-08-17T12:00:00+00:00", "to": "2026-08-27T12:00:00+00:00"}])
        water = home_koi.document(self.connection, now=NOW)["water"]
        self.assertEqual(water["incomplete_since"], {"week": None, "month": "2026-08-17"})

    def test_a_script_with_no_rows_still_covers_the_recorder_days(self):
        # Lane review (pack sd:8054, round 3): a script that ran less often
        # than the recorder keeps has no rows; that read covered nothing,
        # so a long absence was never found.
        self.runs = self.runs[:1]
        fields = self.synced("2026-08-17", DAY)
        self.assertEqual(fields["water_gaps"], [{"from": "2026-08-17T12:00:00+00:00", "to": "2026-08-27T12:00:00+00:00"}])

    def test_unread_days_count_before_the_next_sync(self):
        # A read that fails, or a view nobody opens, leaves the days since
        # the last sync unproven once they pass the recorder's keep.
        self.synced("2026-08-17")
        water = home_koi.document(self.connection, now=NOW)["water"]
        self.assertEqual((water["incomplete_since"], water["sync_due"]), ({"week": None, "month": "2026-08-17"}, True))

    def test_no_sync_leaves_both_totals_incomplete(self):
        water = home_koi.document(self.connection, now=NOW)["water"]
        self.assertEqual(water["incomplete_since"], {"week": "2026-08-31", "month": "2026-08-10"})

    def test_a_pond_save_without_fields_keeps_the_water_log_and_tests(self):
        # pack sd:8054 review: `fields` omitted or null fell through to
        # save_asset's empty default and cleared the server's logs.
        home_koi.log_water({}, now=NOW)(self.connection)
        home_koi.log_test({"ph": 7.4}, now=NOW)(self.connection)
        logged = assets.get_asset(self.connection, self.pond["id"])["fields"]
        base = {"id": self.pond["id"], "kind": "pond", "name": "Pond", "geometry": self.pond["geometry"]}
        for case, payload in (("omitted", base), ("null", {**base, "fields": None})):
            with self.subTest(case):
                # Each case starts from the logged row, so one case's loss
                # cannot hide the other's.
                assets.save_asset(self.connection, asset=self.pond["id"], kind="pond", name="Pond",
                                  geometry=self.pond["geometry"], fields=logged)
                home_map.save(dict(payload))(self.connection)
                fields = assets.get_asset(self.connection, self.pond["id"])["fields"]
                self.assertEqual((len(fields.get("water_log", [])), len(fields.get("tests", []))), (2, 1))

    def test_no_pond_says_to_add_one(self):
        assets.delete_asset(self.connection, self.pond["id"])
        doc = home_koi.document(self.connection, now=NOW)
        self.assertIsNone(doc["pond"])
        self.assertIn("add a pond on the Map", doc["pond_reason"])
        self.assertEqual(home_koi.log_water({}, now=NOW)(self.connection)[0], 409)

    def test_the_routes_are_registered(self):
        self.assertIsNotNone(v2.api("/api/home/koi"))
        self.assertIsNotNone(v2.action("/api/home/koi/water"))
        self.assertIsNotNone(v2.action("/api/home/koi/test"))


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

    def test_a_water_test_posts_only_the_reading(self):
        script = (STAND_IN + MARKUP_JS + f"\nconst DOC = {json.dumps(self.DOC)};\n" + r"""
DOC.water.unlogged = 0;
window.shell = { getJSON: p => { OUT.gets.push(p); return Promise.resolve(JSON.parse(JSON.stringify(DOC))); },
  post: (p, b) => { OUT.posts.push([p, b]); return Promise.resolve({ tests: 1 }); }, toast: m => OUT.toasts.push(m), itemHref: n => '/tasks?row=' + n };
function FormData(f) { this.get = k => f.values[k]; this.entries = () => Object.entries(f.values); }
const flush = () => new Promise(r => r()).then(() => null).then(() => null).then(() => null).then(() => null);
""" + KOI_JS + r"""
var R = {};
(async () => { try {
document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'koi' } })); await flush();
const form = { id: 'koi-test-form', values: { ammonia: '0.25', ph: '' } };
(DOC_LISTENERS.submit || []).forEach(f => f({ target: form, preventDefault() {} })); await flush();
} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();
function run() { return JSON.stringify(OUT); }
""")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        self.assertEqual(out["posts"], [["/api/home/koi/test", {"ammonia": 0.25}]])

    LISTED = {"asset": 2, "folder": "https://drive.google.com/drive/folders/GOL", "files": [
        {"id": "PIC1234567", "name": "scales.jpg", "mime": "image/jpeg", "link": "https://drive.google.com/open?id=PIC1234567",
         "kind": "picture"},
        {"id": "DOC1234567", "name": "receipt.pdf", "mime": "application/pdf", "link": "https://drive.google.com/open?id=DOC1234567",
         "kind": "document"}]}

    def attach_js(self, body):
        """sd:8147: the Koi view with home-attach.js, a fish's folder answering LISTED and an upload staging."""
        script = (STAND_IN + MARKUP_JS + f"\nconst DOC = {json.dumps(self.DOC)};\nconst LISTED = {json.dumps(self.LISTED)};\n" + r"""
DOC.water.unlogged = 0;
window.shell = { getJSON: p => { OUT.gets.push(p); return Promise.resolve(JSON.parse(JSON.stringify(p.startsWith('/api/home/attachments?') ? LISTED : DOC))); },
  post: (p, b) => { OUT.posts.push([p, p === '/api/home/attach/file' ? { name: b.name } : b]); return Promise.resolve({ sha256: 'a'.repeat(64) }); },
  toast: m => OUT.toasts.push(m), itemHref: n => '/tasks?row=' + n };
const flush = () => new Promise(r => r()).then(() => null).then(() => null).then(() => null).then(() => null);
const click = sel => (DOC_LISTENERS.click || []).forEach(f => f({ target: { closest: s => s === sel ? { dataset: { fish: '2' } } : null } }));
""" + ATTACH_JS + KOI_JS + r"""
var R = {};
(async () => { try {
document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'koi' } })); await flush();
""" + body + r"""
} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();
function run() { OUT.R = R; return JSON.stringify(OUT); }
""")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    SELECT = "click('[data-fish]'); await flush(); await flush(); R.form = ELS['koi-form'].html; R.attach = (ELS['koi-attach'] || {}).html || '';"

    def test_a_selected_fish_lists_its_files(self):
        out = self.attach_js(self.SELECT)
        self.assertIn("/api/home/attachments?asset=2", out["gets"])
        self.assertIn('<div id="koi-attach"', out["R"]["form"])
        self.assertIn('href="https://drive.google.com/open?id=DOC1234567" target="_blank" rel="noopener">receipt.pdf', out["R"]["attach"])
        self.assertIn('data-attach="upload"', out["R"]["attach"])
        self.assertIn('data-attach="reference"', out["R"]["attach"])

    def test_a_picture_renders_as_an_image_from_picture(self):
        out = self.attach_js(self.SELECT)
        self.assertIn('<img class="thumb" src="/api/home/attachments/picture?id=PIC1234567" alt="scales.jpg"', out["R"]["attach"])

    def test_upload_posts_that_fishs_id(self):
        out = self.attach_js(self.SELECT + r"""
const button = { setAttribute() {}, removeAttribute() {} };
const form = { dataset: { attach: 'upload' }, elements: { file: { files: [{ name: 'scales.jpg' }] } }, querySelector: () => button,
  closest: s => s === '#koi-attach' ? ELS['koi-attach'] : null };
(DOC_LISTENERS.submit || []).forEach(f => f({ target: form, preventDefault() {} })); await flush(); await flush();""")
        self.assertEqual(out["posts"], [["/api/home/attach/file", {"name": "scales.jpg"}],
                                        ["/api/home/attach", {"asset": 2, "sha256": "a" * 64, "name": "scales.jpg"}]])

    def test_a_new_fish_shows_no_section(self):
        out = self.attach_js("click('#koi-add'); await flush(); R.form = ELS['koi-form'].html;")
        self.assertIn("New fish", out["R"]["form"])
        self.assertNotIn("koi-attach", out["R"]["form"])
        self.assertNotIn("Attachments", out["R"]["form"])
        self.assertFalse([g for g in out["gets"] if g.startswith("/api/home/attachments")])

    def draw_water(self, water):
        script = (STAND_IN + MARKUP_JS + f"\nconst DOC = {json.dumps(self.DOC)};\n" + f"Object.assign(DOC.water, {json.dumps(water)});" + r"""
window.shell = { getJSON: p => { OUT.gets.push(p); return Promise.resolve(JSON.parse(JSON.stringify(DOC))); },
  post: (p, b) => { OUT.posts.push([p, b]); return Promise.resolve({ added: 0 }); }, toast: m => OUT.toasts.push(m), itemHref: n => '/tasks?row=' + n };
const flush = () => new Promise(r => r()).then(() => null).then(() => null).then(() => null).then(() => null);
""" + KOI_JS + r"""
var R = {};
(async () => { try {
document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'koi' } })); await flush(); await flush();
R.water = ELS['koi-water'].html;
} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();
function run() { OUT.R = R; return JSON.stringify(OUT); }
""")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_a_gap_marks_the_totals_it_reaches_incomplete(self):
        # Lane review (pack sd:8054): a total with a gap in its window is
        # not shown as the whole record.
        out = self.draw_water({"unlogged": 0, "sync_due": False, "incomplete_since": {"week": None, "month": "2026-08-17"}})
        figures = out["R"]["water"].split("Weekly average")
        self.assertNotIn("incomplete", figures[0])
        self.assertIn("incomplete since 2026-08-17", figures[1])
        self.assertEqual(out["posts"], [])

    def test_a_due_sync_posts_once_with_nothing_unlogged(self):
        # The view syncs when the stored window is stale, so a quiet pond
        # still records what Home Assistant covered.
        out = self.draw_water({"unlogged": 0, "sync_due": True, "incomplete_since": {"week": None, "month": None}})
        self.assertEqual(out["posts"], [["/api/home/koi/water", {}]])


if __name__ == "__main__":
    unittest.main()
