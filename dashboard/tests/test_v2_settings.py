"""The v2 Settings page (sd:3438): the theme control, moved from the rail, and the sections width (sd:3445).

What this slice promises: `/settings` holds the one theme control (the rail draws none now), and its width control sets,
shows and resets the width through `shell.railWidth`. `settings.js` runs under JavaScriptCore (osascript) against the
stand-in page `test_v2_tasks` uses, with a stand-in `shell.railWidth`.

The Fleet section (sd:3989): `/api/settings` lists the fleet keys from `sd config list sd --json`, and
`POST /api/settings/fleet` runs `sd config set`. Each test puts a stub `sd` first on PATH, which keeps its one value per key
in a temporary folder, so no test runs the real `sd` or reads this machine's config.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import (
    adversarial,
    health_collectors,
    hoa_screen,
    pack_library,
    places,
    realm_sweep,
    reports_screen,
    setting_rows,
    settings_screen,
    v2,
)
from sd_db import writes
from test_v2_registry import GROUPS, Registers
from test_v2_tasks import FETCH_JS, STAND_IN
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
HTML = (V2 / "settings.html").read_text(encoding="utf-8") if (V2 / "settings.html").exists() else ""
SHELL_JS = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
PAGE_JS = (V2 / "static" / "settings.js").read_text(encoding="utf-8") if (V2 / "static" / "settings.js").exists() else ""
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

#: A stand-in `sd`: `config list sd --json` prints the fleet keys and one machine key, each fleet value read from
#: $SD_STUB/<key> (`database`), else unset (`default`); `config set` writes $SD_STUB/<key>, prints what sd prints, then
#: does what $SD_STUB/mode says: `refuse` exits 1 before writing.
STUB = r"""#!/bin/sh
echo "$*" >> "$SD_STUB/calls"
mode=$(cat "$SD_STUB/mode" 2>/dev/null)
case "$1 $2" in
  "config list")
    [ "$mode" = down ] && { echo "sd: the workflow database cannot be read: hub refused" >&2; exit 1; }
    value() { if [ -f "$SD_STUB/$1" ]; then printf '"%s", "source": "database"' "$(cat "$SD_STUB/$1")"; else printf 'null, "source": "default"'; fi; }
    printf '{"copilot_review": {"description": "When sd-ship requests a Copilot review.", "pattern": "deep|never|always", "value": %s},\n' "$(value copilot_review)"
    printf ' "fleet_owners": {"description": "Comma-separated GitHub logins.", "pattern": "[A-Za-z0-9](-?[A-Za-z0-9])*(,[A-Za-z0-9](-?[A-Za-z0-9])*)*", "value": %s},\n' "$(value fleet_owners)"
    printf ' "hub_name": {"description": "The hub node name.", "pattern": "", "value": %s},\n' "$(value hub_name)"
    printf ' "status_companions": {"description": "Repositories sd-status lists beside its own.", "pattern": "[~/].*", "value": %s},\n' "$(value status_companions)"
    for key in notion_private_folder notion_team_folder notion_experiments notion_experiment_tasks; do
      printf ' "%s": {"description": "A Notion id.", "pattern": "", "value": %s},\n' "$key" "$(value $key)"
    done
    printf ' "gate_slots": {"description": "Gates at once.", "pattern": "[0-9]+", "value": null, "source": "default"}}\n'
    exit 0 ;;
  "config set")
    [ "$mode" = refuse ] && { echo "sd: '$4' does not match 'deep|never|always'" >&2; exit 1; }
    key=${3#sd.}
    was=$(cat "$SD_STUB/$key" 2>/dev/null || echo "(unset)")
    printf '%s' "$4" > "$SD_STUB/$key"
    echo "$3: $was -> $4 (workflow database)"
    exit 0 ;;
esac
exit 2
"""


class StubSd:
    """Puts the stand-in `sd` first on PATH for one test, with its state folder; `mode` and `held` set and read it."""

    def __init__(self, case):
        self.dir = Path(case.enterContext(tempfile.TemporaryDirectory()))
        (self.dir / "bin").mkdir()
        (self.dir / "bin" / "sd").write_text(STUB, encoding="utf-8")
        (self.dir / "bin" / "sd").chmod(0o755)
        case.enterContext(mock.patch.dict(os.environ, {"PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", "SD_STUB": str(self.dir)}))

    def mode(self, word):
        (self.dir / "mode").write_text(word, encoding="utf-8")

    def held(self, key):
        path = self.dir / key
        return path.read_text(encoding="utf-8") if path.exists() else None

    def calls(self):
        path = self.dir / "calls"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "settings", "Settings", "/settings", ("/api/settings", "/api/settings/fleet", "/api/settings/row", "/api/settings/list")


class TheFleetRead(unittest.TestCase):
    """sd:3989: the Fleet section reads `sd config list sd --json`; a failed read names its reason and lists nothing."""

    def setUp(self):
        self.sd = StubSd(self)

    def test_the_fleet_keys_are_the_packs(self):
        source = (pack_library().parent / "bin" / "sd_lib.py").read_text(encoding="utf-8")
        keys = re.search(r"^FLEET_KEYS = \(([^)]*)\)", source, re.M).group(1)
        self.assertEqual(settings_screen.FLEET, tuple(re.findall(r'"(\w+)"', keys)))

    def test_each_fleet_key_has_its_value_source_description_and_control(self):
        (self.sd.dir / "copilot_review").write_text("never", encoding="utf-8")
        doc = settings_screen.document(now="2026-10-14T12:00:00+00:00")
        self.assertEqual(doc["reason"], "")
        self.assertEqual([(k["key"], k["value"], k["source"], k["choices"]) for k in doc["fleet"]],
                         [("fleet_owners", None, "default", None), ("copilot_review", "never", "database", ["deep", "never", "always"]),
                          ("hub_name", None, "default", None), ("status_companions", None, "default", None),
                          ("notion_private_folder", None, "default", None), ("notion_team_folder", None, "default", None),
                          ("notion_experiments", None, "default", None), ("notion_experiment_tasks", None, "default", None)])
        self.assertEqual(doc["fleet"][1]["description"], "When sd-ship requests a Copilot review.")
        self.assertEqual(self.sd.calls(), ["config list sd --json"])

    def test_a_failed_list_is_named_and_lists_nothing(self):
        self.sd.mode("down")
        doc = settings_screen.document(now="2026-10-14T12:00:00+00:00")
        self.assertIsNone(doc["fleet"])
        self.assertEqual(doc["reason"], "sd config list sd exited 1: sd: the workflow database cannot be read: hub refused")

    def test_a_list_without_a_fleet_key_or_past_its_bound_is_named(self):
        with mock.patch.object(settings_screen, "FLEET", ("fleet_owners", "external_reviews")):
            self.assertIn("sd.external_reviews", settings_screen.document(now="x")["reason"])
        with mock.patch.object(settings_screen, "SD_SECONDS", 0.001):
            self.assertIn("ran past", settings_screen.document(now="x")["reason"])


class TheFleetSet(BrowserSession):
    """sd:3989: POST /api/settings/fleet {key, value} runs `sd config set sd.<key> <value>` and answers the value it replaced.
    A refusal is a 400 with sd's reason; a run past the bound is an unknown outcome (504), and the next read shows the value."""

    def setUp(self):
        super().setUp()
        self.sd = StubSd(self)

    def test_a_set_runs_sd_config_set_and_answers_the_replaced_value(self):
        (self.sd.dir / "copilot_review").write_text("deep", encoding="utf-8")
        status, _, answer = self.post("/api/settings/fleet", {"key": "copilot_review", "value": "never"})
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer, {"key": "copilot_review", "value": "never", "was": "deep", "database": True})
        self.assertEqual(self.sd.calls(), ["config set sd.copilot_review never"])
        self.assertEqual(self.sd.held("copilot_review"), "never")

    def test_an_unset_key_reads_back_as_none(self):
        status, _, answer = self.post("/api/settings/fleet", {"key": "fleet_owners", "value": " example-org,other "})
        self.assertEqual((status, answer["was"], answer["value"]), (200, None, "example-org,other"))

    def test_a_refusal_is_a_400_with_sds_reason_and_nothing_written(self):
        self.sd.mode("refuse")
        status, _, answer = self.post("/api/settings/fleet", {"key": "copilot_review", "value": "sometimes"})
        self.assertEqual(status, 400)
        self.assertIn("does not match 'deep|never|always'", answer["error"])
        self.assertIsNone(self.sd.held("copilot_review"))

    def test_a_run_past_the_bound_is_unknown_and_the_next_read_shows_the_value(self):
        # sd:6809: the set finishes, then the bound is spent; no real clock races the stub's write under load.
        ran = settings_screen._sd

        def past_the_bound(*args):
            ran(*args)
            raise subprocess.TimeoutExpired(["sd", *args], settings_screen.SD_SECONDS)
        with mock.patch.object(settings_screen, "_sd", past_the_bound):
            status, _, answer = self.post("/api/settings/fleet", {"key": "copilot_review", "value": "always"})
        self.assertEqual(status, 504, answer)
        self.assertTrue(answer["unknown"])
        self.assertIn("unknown", answer["error"])
        status, _, body = self.request("/api/settings", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["fleet"][1]["value"], "always")

    def test_another_key_an_option_value_or_another_shape_is_refused_before_sd_runs(self):
        for payload in ({"key": "gate_slots", "value": "4"}, {"key": "fleet_owners", "value": "-h"},
                        {"key": "fleet_owners", "value": "  "}, {"key": "fleet_owners"}, {"key": "fleet_owners", "value": 3}):
            status, _, answer = self.post("/api/settings/fleet", payload)
            self.assertEqual(status, 400, (payload, answer))
        self.assertEqual(self.sd.calls(), [])


class TheSweepSettings(BrowserSession):
    """sd:5109: the Sweeps section reads and writes the sweep settings as workflow database rows, never a config file.
    A set names the value shown; a row changed since refuses with 409; an empty value unsets; another key is a 400."""

    def setUp(self):
        super().setUp()
        self.sd = StubSd(self)

    def read(self):
        status, _, body = self.request("/api/settings", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_the_read_lists_every_sweep_setting_with_its_row(self):
        writes.set_setting(self.connection, "sweep.work.jira_scope", "project in (ABC)")
        doc = self.read()
        self.assertEqual(doc["sweeps_reason"], "")
        self.assertEqual([k["key"] for k in doc["sweeps"]], list(realm_sweep.SETTINGS))
        held = {k["key"]: (k["value"], k["source"]) for k in doc["sweeps"]}
        self.assertEqual(held["sweep.work.jira_scope"], ("project in (ABC)", "database"))
        self.assertEqual(held["sweep.personal.mail_query"], (None, "unset"))

    def test_a_set_writes_the_row_and_the_runner_reads_it(self):
        status, _, answer = self.post("/api/settings/row", {"key": "sweep.work.mail_query", "value": " in:inbox ", "before": None})
        self.assertEqual((status, answer), (200, {"key": "sweep.work.mail_query", "value": "in:inbox", "was": None}))
        rows = writes.read_settings(self.connection, ["sweep.work.mail_query"])
        self.assertEqual(rows, {"sweep.work.mail_query": "in:inbox"})
        self.assertEqual(realm_sweep.filters("work", {**rows, "sweep.work.followups_repo": "/r"})[0]["mail_query"], "in:inbox")
        status, _, answer = self.post("/api/settings/row", {"key": "sweep.work.mail_query", "value": "", "before": "in:inbox"})
        self.assertEqual((status, answer["value"], answer["was"]), (200, None, "in:inbox"))
        self.assertEqual(writes.read_settings(self.connection, ["sweep.work.mail_query"]), {"sweep.work.mail_query": None})
        self.assertEqual(self.sd.calls(), [], "a sweep setting ran sd config")

    def test_a_row_changed_since_the_read_refuses_with_409_and_keeps_its_value(self):
        writes.set_setting(self.connection, "sweep.personal.drive_exclude", "Example Drive")
        status, _, answer = self.post("/api/settings/row", {"key": "sweep.personal.drive_exclude", "value": "Other", "before": None})
        self.assertEqual(status, 409, answer)
        self.assertIn("changed since it was read", answer["error"])
        self.assertEqual(writes.read_settings(self.connection, ["sweep.personal.drive_exclude"]),
                         {"sweep.personal.drive_exclude": "Example Drive"})

    def test_another_key_or_shape_is_refused_and_writes_nothing(self):
        for payload in ({"key": "sd.copilot_review", "value": "never", "before": None},
                        {"key": "sweep.work.mail_query", "value": "x"},
                        {"key": "sweep.work.mail_query", "value": 3, "before": None},
                        {"key": "sweep.work.mail_query", "value": "x", "before": 3}):
            status, _, answer = self.post("/api/settings/row", payload)
            self.assertEqual(status, 400, (payload, answer))
        self.assertEqual(writes.read_settings(self.connection, list(realm_sweep.SETTINGS)), {})


class TheDashboardSettings(BrowserSession):
    """sd:5109: the Dashboard section lists the dashboard lists and the adversarial review's model and effort as rows.
    A list saves with the digest of the source and text shown, parsed first; the first Save imports the old file."""

    def setUp(self):
        super().setUp()
        self.sd = StubSd(self)
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.realms = self.dir / "realms.conf"
        self.enterContext(mock.patch.object(places, "REALMS_CONFIG", self.realms))
        self.enterContext(mock.patch.object(reports_screen, "FAMILIES_CONFIG", self.dir / "report-families.conf"))
        self.enterContext(mock.patch.object(hoa_screen, "THRESHOLDS", self.dir / "hoa-thresholds.conf"))
        self.enterContext(mock.patch.object(health_collectors, "_storage_config", lambda: self.dir / "disk.conf"))

    def read(self):
        status, _, body = self.request("/api/settings", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["dashboard_reason"], "")
        return {k["key"]: k for k in doc["dashboard"]}

    def save(self, key, text, before):
        return self.post("/api/settings/list", {"key": key, "text": text, "before": before})

    def test_the_read_lists_each_list_with_its_source_and_the_review_keys(self):
        self.realms.write_text("realm|work|Work|/repos/example-work/*\n", encoding="utf-8")
        writes.set_setting(self.connection, "dashboard.hoa_thresholds", "tank|below|12")
        writes.set_setting(self.connection, "adversarial.model", "gpt-fixture")
        held = self.read()
        self.assertEqual(list(held), list(setting_rows.LISTS) + ["dashboard.home_map_extent"] + list(adversarial.SETTINGS) + list(adversarial.DEEP))
        self.assertEqual((held["dashboard.realms"]["value"], held["dashboard.realms"]["source"]),
                         ("realm|work|Work|/repos/example-work/*\n", "file"))
        self.assertEqual((held["dashboard.hoa_thresholds"]["source"], held["dashboard.report_families"]["source"]), ("database", "unset"))
        self.assertTrue(all(held[key]["lines"] for key in setting_rows.LISTS))
        self.assertEqual((held["adversarial.model"]["value"], held["adversarial.effort"]["value"]), ("gpt-fixture", None))

    def test_the_first_save_imports_the_file_and_the_reader_reads_the_row(self):
        self.realms.write_text("realm|work|Work|/repos/example-work/*\n", encoding="utf-8")
        shown = self.read()["dashboard.realms"]
        status, _, answer = self.save("dashboard.realms", shown["value"], shown["digest"])
        self.assertEqual((status, answer["was"]), (200, None), answer)
        self.realms.unlink()
        self.assertEqual([r["id"] for r in places.realms(self.connection)["list"]], ["work"])
        self.assertEqual(self.read()["dashboard.realms"]["source"], "database")

    def test_a_text_with_a_problem_is_refused_and_writes_nothing(self):
        shown = self.read()["dashboard.report_families"]
        status, _, answer = self.save("dashboard.report_families", "family|x\n", shown["digest"])
        self.assertEqual(status, 400, answer)
        self.assertIn("line 1", answer["error"])
        self.assertEqual(writes.read_settings(self.connection, ["dashboard.report_families"]), {})

    def test_a_stale_digest_refuses_with_409(self):
        shown = self.read()["dashboard.hoa_thresholds"]
        writes.set_setting(self.connection, "dashboard.hoa_thresholds", "tank|below|10")
        status, _, answer = self.save("dashboard.hoa_thresholds", "tank|below|12", shown["digest"])
        self.assertEqual(status, 409, answer)
        self.assertEqual(writes.read_settings(self.connection, ["dashboard.hoa_thresholds"]), {"dashboard.hoa_thresholds": "tank|below|10"})

    def test_a_clear_or_a_file_edit_since_the_read_refuses_with_409(self):
        # A cleared row holds None, as no row does: the digest tells the two apart, so an old tab cannot bring a value back.
        self.realms.write_text("realm|work|Work|/repos/a\n", encoding="utf-8")
        shown = self.read()["dashboard.realms"]
        writes.set_setting(self.connection, "dashboard.realms", None)
        self.assertEqual(self.save("dashboard.realms", shown["value"], shown["digest"])[0], 409)
        self.assertEqual(writes.read_settings(self.connection, ["dashboard.realms"]), {"dashboard.realms": None})
        shown = self.read()["dashboard.report_families"]
        (self.dir / "report-families.conf").write_text("family|home|Home|house|weekly-scan\n", encoding="utf-8")
        self.assertEqual(self.save("dashboard.report_families", "family|hoa|HOA|house|a-job", shown["digest"])[0], 409)

    def test_an_empty_save_clears_the_row_and_the_file_is_not_read_again(self):
        self.realms.write_text("realm|work|Work|/repos/a\n", encoding="utf-8")
        shown = self.read()["dashboard.realms"]
        status, _, answer = self.save("dashboard.realms", "  ", shown["digest"])
        self.assertEqual((status, answer["value"]), (200, None), answer)
        self.assertEqual(places.realms(self.connection)["state"], "missing")

    def test_a_failed_read_writes_nothing(self):
        shown = self.read()["dashboard.realms"]

        def refuse(connection, keys):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(writes, "read_settings", refuse):
            status, _, answer = self.save("dashboard.realms", "realm|work|Work|/repos/a", shown["digest"])
        self.assertEqual(status, 503, answer)
        self.assertEqual(writes.read_settings(self.connection, ["dashboard.realms"]), {})

    def test_the_storage_folders_save_as_a_row_and_a_line_that_is_not_one_is_refused(self):
        # sd:5109: Health's storage folders are a list row too, in disk.conf's storage|<path> grammar.
        shown = self.read()["dashboard.disk_storage"]
        status, _, answer = self.save("dashboard.disk_storage", "volume|/nowhere", shown["digest"])
        self.assertEqual(status, 400, answer)
        self.assertIn("line 1", answer["error"])
        status, _, answer = self.save("dashboard.disk_storage", "storage|~/store", shown["digest"])
        self.assertEqual((status, answer["value"]), (200, "storage|~/store"), answer)
        self.assertEqual(writes.read_settings(self.connection, ["dashboard.disk_storage"]), {"dashboard.disk_storage": "storage|~/store"})

    def test_another_key_or_shape_is_refused(self):
        for payload in ({"key": "dashboard.home_tiles", "text": "x", "before": "d"},
                        {"key": "dashboard.realms", "text": "x"},
                        {"key": "dashboard.realms", "text": 3, "before": "d"}):
            self.assertEqual(self.post("/api/settings/list", payload)[0], 400, payload)

    def test_the_review_keys_set_through_the_row_post_and_refuse_a_value_that_is_not_one_word(self):
        status, _, answer = self.post("/api/settings/row", {"key": "adversarial.model", "value": "gpt-fixture", "before": None})
        self.assertEqual((status, answer), (200, {"key": "adversarial.model", "value": "gpt-fixture", "was": None}))
        self.assertEqual(adversarial.settings(self.connection), ("gpt-fixture", adversarial.EFFORT))
        status, _, answer = self.post("/api/settings/row", {"key": "adversarial.effort", "value": "very high", "before": None})
        self.assertEqual(status, 400, answer)
        self.assertEqual(writes.read_settings(self.connection, ["adversarial.effort"]), {})

    def test_the_deep_review_keys_are_global_rows_that_check_their_pattern(self):
        """sd:5784: the Deep review's settings moved from a Management dialog to this section; effort is a select."""
        writes.set_setting(self.connection, "adversarial.deep.agents", "4")
        held = self.read()
        self.assertEqual((held["adversarial.deep.agents"]["value"], held["adversarial.deep.agents"]["source"]), ("4", "database"))
        self.assertEqual(held["adversarial.deep.effort"]["choices"], ["low", "medium", "high", "xhigh", "max"])
        self.assertIn("Unset is opus.", held["adversarial.deep.model"]["description"])
        status, _, answer = self.post("/api/settings/row", {"key": "adversarial.deep.bound_minutes", "value": "120", "before": None})
        self.assertEqual((status, answer["was"]), (200, None))
        self.assertEqual(adversarial.deep_settings(self.connection)["adversarial.deep.bound_minutes"], {"value": "120", "from": "database"})
        status, _, answer = self.post("/api/settings/row", {"key": "adversarial.deep.agents", "value": "40", "before": "4"})
        self.assertEqual(status, 400, answer)
        self.assertIn("from 1 to 16", answer["error"])


class TheThemeMoved(unittest.TestCase):
    def test_settings_holds_the_one_theme_control_and_the_rail_draws_none(self):
        self.assertEqual(re.findall(r'data-theme-choice="(\w+)"', HTML), ["dark", "light", "system"])
        self.assertNotIn("data-theme-choice", SHELL_JS)
        self.assertIn("Settings", GROUPS)


class TheWidthControl(unittest.TestCase):
    """The width is the shell's .seg radio control (foundation patterns.md has no slider), and a change says Saved (§ Forms)."""

    def run_page(self, body, kept="null"):
        script = (STAND_IN + MARKUP_JS + FETCH_JS + "\nvar KEPT = " + kept + ", CALLS = [];\nANSWER = () => [200, { fleet: [], reason: '' }];\n"
                  "window.shell = { post, getJSON, railWidth: (...a) => { CALLS.push(a); if (a.length) KEPT = a[0] === null ? null : Math.min(384, Math.max(176, Math.round(+a[0]))); return KEPT; } };\n"
                  "var RADIOS = ['176', '', '288', '384'].map(v => ({ value: v, checked: false }));\n"
                  "document.getElementById('rail-width').querySelectorAll = () => RADIOS;\n"
                  + PAGE_JS + "\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nvar R = {};\n" + body
                  + "\nfunction run() { R.calls = CALLS; R.checked = RADIOS.filter(r => r.checked).map(r => r.value);"
                  " R.out = ELS['rail-width-out'].textContent; R.theme = (ELS['theme-saved'] || {}).textContent || ''; R.att = window.PAGE_ATTENTION; return JSON.stringify(R); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_the_page_draws_a_seg_radio_control_and_no_slider(self):
        self.assertNotIn('type="range"', HTML)
        self.assertRegex(HTML, r'<fieldset class="seg" id="rail-width"')
        self.assertEqual(re.findall(r'name="rail-width" value="(\d*)"', HTML), ["176", "", "288", "384"])

    def test_the_control_checks_the_kept_width_or_default(self):
        out = self.run_page("", kept="288")
        self.assertEqual((out["checked"], out["out"]), (["288"], "288 px"))
        out = self.run_page("")
        self.assertEqual((out["checked"], out["out"], out["calls"]), ([""], "default, 216 px", [[]]))
        self.assertEqual(out["att"]["state"], "ok")

    def test_a_dragged_width_checks_no_preset(self):
        out = self.run_page("", kept="300")
        self.assertEqual((out["checked"], out["out"]), ([], "300 px"))

    def test_a_change_sets_the_width_and_says_saved(self):
        out = self.run_page("ELS['rail-width'].listeners.change[0]({ target: { value: '384' } }); R.wide = ELS['rail-width-out'].textContent;"
                            " ELS['rail-width'].listeners.change[0]({ target: { value: '' } });")
        self.assertEqual(out["wide"], "384 px · Saved")
        self.assertEqual(out["calls"], [[], ["384"], [None]])
        self.assertEqual((out["checked"], out["out"]), ([""], "default, 216 px · Saved"))

    def test_a_width_set_on_the_rail_edge_shows_here(self):
        out = self.run_page("(DOC_LISTENERS['shell:rail-width'] || [])[0]({ detail: 263 });")
        self.assertEqual((out["checked"], out["out"]), ([], "263 px · Saved"))

    def test_a_theme_choice_says_saved(self):
        out = self.run_page("ELS['theme'].listeners.click[0]({ target: { closest: s => s === '[data-theme-choice]' ? {} : null } });")
        self.assertEqual(out["theme"], "Saved")
        self.assertEqual(self.run_page("ELS['theme'].listeners.click[0]({ target: { closest: () => null } });")["theme"], "")


#: The document /api/settings answers in the script tests: one key unset by default, one held in the database.
FLEET_DOC = {"read": "2026-10-14T12:00:00+00:00", "reason": "", "fleet": [
    {"key": "fleet_owners", "value": None, "source": "default", "description": "Comma-separated GitHub logins.", "choices": None},
    {"key": "copilot_review", "value": "deep", "source": "database", "description": "When sd-ship requests a Copilot review.",
     "choices": ["deep", "never", "always"]}]}


class TheFleetSection(unittest.TestCase):
    """sd:3989: settings.js draws one row per fleet key with its source, saves a change at once and says what came of it."""

    def run_page(self, body, answer):
        script = (STAND_IN + MARKUP_JS + FETCH_JS + f"\nANSWER = {answer};\n"
                  "window.shell = { post, getJSON, railWidth: () => null };\n" + PAGE_JS
                  + "\nconst flush = async () => { for (let i = 0; i < 200; i++) await null; };\nvar R = {};\n"
                  "(async () => { try { (DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f()); await flush();\n" + body
                  + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  "function run() { OUT.R = R; OUT.list = ELS.fleet.html; OUT.note = ELS['fleet-note'].textContent; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def answer(self, post, doc=FLEET_DOC):
        return f"(path, body) => path === '/api/settings' ? [200, {json.dumps(doc)}] : {post}"

    CHANGE = "await ELS.fleet.listeners.change[0]({ target: { dataset: { key: 'copilot_review' }, value: 'never' } }); await flush();"

    def test_each_key_draws_its_value_source_and_control(self):
        out = self.run_page("", self.answer("[500, {}]"))
        self.assertEqual(out["gets"], ["/api/settings"])
        self.assertIn('<h3 id="f-fleet_owners-h">sd.fleet_owners</h3>', out["list"])
        self.assertIn('<input class="fld" id="f-fleet_owners" data-key="fleet_owners" type="text" value="" placeholder="unset"', out["list"])
        self.assertIn('<option value="deep" selected>deep</option><option value="never">never</option>', out["list"])
        self.assertIn("unset · default applies", out["list"])
        self.assertIn("workflow database", out["list"])
        self.assertEqual(out["note"], "One value serves every machine. A change saves at once.")

    def test_a_change_posts_the_key_and_value_reads_again_and_says_saved(self):
        out = self.run_page(self.CHANGE + " R.out = ELS['f-copilot_review-out'].textContent;",
                            self.answer("[200, { key: 'copilot_review', value: 'never', was: 'deep', database: true }]"))
        self.assertEqual(out["posts"], [["/api/settings/fleet", {"key": "copilot_review", "value": "never"}, 64]])
        self.assertEqual(out["gets"], ["/api/settings"] * 2)
        self.assertEqual(out["R"]["out"], "Saved")

    def test_a_replaced_value_that_differs_from_the_shown_one_is_named(self):
        out = self.run_page(self.CHANGE + " R.out = ELS['f-copilot_review-out'].textContent;",
                            self.answer("[200, { key: 'copilot_review', value: 'never', was: 'always', database: true }]"))
        self.assertEqual(out["R"]["out"], "Saved · it held always, not deep as shown")

    def test_a_refusal_keeps_the_typed_value_and_says_why(self):
        out = self.run_page(self.CHANGE + " R.out = ELS['f-copilot_review-out'].textContent;",
                            self.answer("[400, { error: 'sd config set sd.copilot_review refused: no' }]"))
        self.assertEqual(out["R"]["out"], "Not saved: sd config set sd.copilot_review refused: no")
        self.assertEqual(out["gets"], ["/api/settings"], "a refusal read again and dropped what was typed")

    def test_an_unknown_outcome_says_so_and_reads_again(self):
        out = self.run_page(self.CHANGE + " R.out = ELS['f-copilot_review-out'].textContent;",
                            self.answer("[504, { error: 'sd config set ran past its 5 seconds', unknown: true }]"))
        self.assertEqual(out["R"]["out"], "Outcome unknown: sd config set ran past its 5 seconds. The value shown is the one read after.")
        self.assertEqual(out["gets"], ["/api/settings"] * 2)

    def test_a_sweep_change_posts_the_value_shown_and_a_409_reads_again(self):
        """sd:5109: the Sweeps rows draw from the same read; a set names the value shown, and a stale row reads again."""
        doc = {**FLEET_DOC, "sweeps": [{"key": "sweep.work.mail_query", "value": "in:inbox", "row": "in:inbox", "source": "database",
                                        "description": "The Work sweep's Gmail search.", "choices": None}], "sweeps_reason": ""}
        change = ("await ELS.sweeps.listeners.change[0]({ target: { dataset: { key: 'sweep.work.mail_query' }, value: 'is:unread' } });"
                  " await flush(); R.out = ELS['f-sweep.work.mail_query-out'].textContent; R.sweeps = ELS.sweeps.html;")
        out = self.run_page(change, self.answer("[409, { error: 'sweep.work.mail_query changed since it was read; read it again', reload: true }]", doc))
        self.assertIn('<h3 id="f-sweep.work.mail_query-h">sweep.work.mail_query</h3>', out["R"]["sweeps"])
        self.assertEqual(out["posts"], [["/api/settings/row", {"key": "sweep.work.mail_query", "value": "is:unread", "before": "in:inbox"}, 64]])
        self.assertEqual(out["gets"], ["/api/settings"] * 2)
        self.assertEqual(out["R"]["out"], "Not saved: sweep.work.mail_query changed since it was read; read it again. The value shown is the one read now.")

    LIST = {"key": "dashboard.realms", "value": "realm|work|Work|/repos/example-work/*\n", "lines": True, "source": "file",
            "problem": "", "digest": "d" * 64, "description": "Tasks' realm views.", "choices": None}

    def test_a_list_draws_a_text_area_and_save_posts_its_text_with_the_digest_shown(self):
        """sd:5109: a dashboard list saves on Save, not on change, with the digest of the source and text the page read."""
        doc = {**FLEET_DOC, "dashboard": [self.LIST], "dashboard_reason": ""}
        save = ("R.list = ELS.dashboard.html; document.getElementById('f-dashboard.realms').value = 'realm|lab|Lab|/repos/lab';"
                " await ELS.dashboard.listeners.change[0]({ target: { dataset: {} } }); await flush(); R.after_change = OUT.posts.length;"
                " await ELS.dashboard.listeners.click[0]({ target: { closest: () => ({ dataset: { save: 'dashboard.realms' } }) } });"
                " await flush(); R.out = ELS['f-dashboard.realms-out'].textContent;")
        out = self.run_page(save, self.answer("[200, { key: 'dashboard.realms', value: 'realm|lab|Lab|/repos/lab', was: null, digest: 'e' }]", doc))
        self.assertIn('<textarea class="fld" id="f-dashboard.realms" data-list="dashboard.realms"', out["R"]["list"])
        self.assertIn("old file in the config folder · Save moves it to the database", out["R"]["list"])
        self.assertEqual(out["R"]["after_change"], 0, "a list saved on change")
        self.assertEqual(out["posts"], [["/api/settings/list", {"key": "dashboard.realms", "text": "realm|lab|Lab|/repos/lab", "before": "d" * 64}, 64]])
        self.assertEqual(out["R"]["out"], "Saved")

    def test_a_list_save_refused_keeps_the_text_and_names_the_problem(self):
        doc = {**FLEET_DOC, "dashboard": [self.LIST], "dashboard_reason": ""}
        save = ("await ELS.dashboard.listeners.click[0]({ target: { closest: () => ({ dataset: { save: 'dashboard.realms' } }) } });"
                " await flush(); R.out = ELS['f-dashboard.realms-out'].textContent;")
        out = self.run_page(save, self.answer("[400, { error: 'dashboard.realms was not saved: line 1: not realm|<id>|<label>|<glob>: x.' }]", doc))
        self.assertEqual(out["R"]["out"], "Not saved: dashboard.realms was not saved: line 1: not realm|<id>|<label>|<glob>: x.")
        self.assertEqual(out["gets"], ["/api/settings"])

    def test_a_failed_read_names_its_reason_and_draws_no_row(self):
        out = self.run_page("", self.answer("[500, {}]", {"read": "x", "fleet": None, "reason": "sd config list sd exited 1: hub refused"}))
        self.assertEqual(out["note"], "Not read: sd config list sd exited 1: hub refused.")
        self.assertEqual(out["list"], "")


if __name__ == "__main__":
    unittest.main()
