"""Today's Usage strip (pack sd:9257): each provider's windows, reset, pace and read time, from system's usage job.

system's `agent-meter.py usage` (system sd:9256) writes one `heartbeat` row per 'usage providers' line, key
`usage:<provider>`; the newest row per key is the answer. claude's reading carries cswap's accounts and only their
seven-day share; the strip shows the windows a reading has and never invents five hours. A reading over 30 minutes old
is unknown, never its last numbers and never 0. A service whose `providers` name the provider flags its row.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import unittest
from unittest import mock

from sd_dashboard import now_screen, setting_rows, settings_screen, usage_screen
from sd_db.writes import record_state, set_setting
from support import NOW, ScreenCase
from test_service_status import status
from test_v2_tasks import MARKUP_JS, SHELL_JS, V2
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

READ = "2026-09-06T11:50:00Z"


def reading(connection, name, *, timestamp="2026-09-06T11:50:00Z", **body):
    body = {"provider": name, "status": "ok", "read_at": READ, **body}
    record_state(connection, "heartbeat", key=f"usage:{name}", timestamp=timestamp, body=body)


CODEX = {"windows": [{"name": "primary", "used_percent": 62.0, "window_minutes": 300, "resets_at": "2026-09-06T15:00:00Z"},
                     {"name": "secondary", "used_percent": 85.5, "window_minutes": 10080, "resets_at": "2026-09-10T12:00:00Z"}]}
CLAUDE = {"windows": [], "accounts": [
    {"number": 1, "active": True, "pct": 40, "resetsAt": "2026-09-08T12:00:00Z", "expectedPct": 50, "fetchedAt": READ},
    {"number": 2, "active": False, "pct": 10, "resetsAt": "2026-09-09T12:00:00Z", "fetchedAt": "2026-09-06T10:00:00Z"}]}


class TheList(unittest.TestCase):
    def test_the_usage_providers_list_is_a_settings_list_with_the_job_s_grammar(self):
        self.assertIn("dashboard.usage_providers", setting_rows.LISTS)
        check = settings_screen._list("dashboard.usage_providers")[1]
        self.assertEqual(check("claude\ncodex  # session logs\n\nopenrouter\n"), [])
        self.assertEqual(check("Claude\nco dex\n"), ["line 1: not a provider name", "line 2: not a provider name"])


class TheStrip(ScreenCase):
    def strip(self):
        return usage_screen.strip(self.connection, now=NOW)

    def test_codex_shows_each_window_with_use_reset_and_pace(self):
        reading(self.connection, "codex", **CODEX)
        row = self.strip()["rows"][0]
        self.assertEqual((row["id"], row["state"], row["read_at"]), ("usage:codex", "caution", READ))
        # pack sd:9380: pace is at the read time, 11:50, not the render time. 190 of 300 minutes left is 37 % through:
        # 62 used is 25 over (at 12:00 it would read 22); 4 days 10 minutes of 7 days left is 43 % through: 86 is 43 over.
        self.assertEqual(row["windows"], [{"label": "5h", "used": 62.0, "resets_at": "2026-09-06T15:00:00Z", "pace": 25},
                                          {"label": "7d", "used": 85.5, "resets_at": "2026-09-10T12:00:00Z", "pace": 43}])

    def test_claude_is_a_row_per_cswap_account_with_its_seven_days_only(self):
        reading(self.connection, "claude", **CLAUDE)
        rows = self.strip()["rows"]
        self.assertEqual([(row["label"], row["state"], row["active"]) for row in rows],
                         [("claude#1", "ok", True), ("claude#2", "unknown", False)])
        self.assertEqual(rows[0]["windows"], [{"label": "7d", "used": 40.0, "resets_at": "2026-09-08T12:00:00Z", "pace": -10}])
        self.assertEqual((rows[1]["windows"], rows[1]["why"]), ([], "read 2h ago; a reading over 30 min old is unknown"))

    def test_a_claude_account_with_no_expected_share_paces_at_its_fetch_time(self):
        # pack sd:9380: 11:40 is 92.66 % through a week resetting at 00:00, so 90.3 used is 2 under; at 12:00 it is 3.
        account = {"number": 1, "pct": 90.3, "resetsAt": "2026-09-07T00:00:00Z", "fetchedAt": "2026-09-06T11:40:00Z"}
        reading(self.connection, "claude", windows=[], accounts=[account])
        self.assertEqual(self.strip()["rows"][0]["windows"][0]["pace"], -2)

    def test_a_failed_claude_read_keeps_its_status_and_shows_its_error(self):
        # Bundle review round 1: the accounts branch passed status="ok"
        # and dropped the job's error.
        reading(self.connection, "claude", status="unknown", error="cswap timed out", **CLAUDE)
        self.assertEqual([(row["id"], row["state"], row["status"], row["why"]) for row in self.strip()["rows"]],
                         [("usage:claude", "unknown", "unknown", "cswap timed out")])

    def test_claude_accounts_that_render_no_row_are_one_unknown_row(self):
        reading(self.connection, "claude", windows=[], accounts=[{}])
        self.assertEqual([(row["id"], row["state"], row["why"]) for row in self.strip()["rows"]],
                         [("usage:claude", "unknown", "cswap lists no numbered account")])

    def test_a_time_past_the_calendar_s_end_is_unknown_never_an_error(self):
        far = "9999-12-31T23:59:59-12:00"
        reading(self.connection, "codex", read_at=far, windows=[{"used_percent": 5, "window_minutes": 300, "resets_at": far}])
        status(self.connection, "openai", status="outage", providers=["codex"], incidents=[{"title": "x", "started_at": far}])
        [row] = self.strip()["rows"]
        self.assertEqual((row["state"], row["why"]), ("unknown", "the reading has no read time"))

    def test_a_pace_past_the_float_range_is_no_pace_never_an_error(self):
        reading(self.connection, "claude", windows=[], accounts=[
            {"number": 1, "pct": 1.7e308, "expectedPct": -1.7e308, "fetchedAt": READ}])
        [row] = self.strip()["rows"]
        self.assertEqual(row["windows"][0]["pace"], None)

    def test_a_window_too_long_for_a_float_has_no_pace_and_its_name(self):
        # Bundle review round 3: 10**400 minutes raised "int too large to
        # convert to float", which replaced the whole strip.
        reading(self.connection, "codex", windows=[
            {"name": "primary", "used_percent": 5, "window_minutes": 10 ** 400, "resets_at": "2026-09-06T15:00:00Z"}])
        [row] = self.strip()["rows"]
        self.assertEqual(row["windows"], [{"label": "primary", "used": 5.0, "resets_at": "2026-09-06T15:00:00Z",
                                           "pace": None}])

    def test_each_text_field_is_capped_at_200_characters(self):
        long = "x" * 10_000
        reading(self.connection, "codex", windows=[{"name": long, "used_percent": 5}],
                balance={"amount": 1, "unit": long, "period": long})
        [row] = self.strip()["rows"]
        self.assertEqual((len(row["windows"][0]["label"]), len(row["balance"]["unit"]), len(row["balance"]["period"])),
                         (200, 200, 200))

    def test_a_reading_over_30_minutes_old_is_unknown_and_shows_no_number(self):
        reading(self.connection, "codex", read_at="2026-09-06T11:29:00Z", **CODEX)
        row = self.strip()["rows"][0]
        self.assertEqual((row["state"], row["windows"]), ("unknown", []))
        self.assertEqual(row["why"], "read 31m ago; a reading over 30 min old is unknown")

    def test_a_failed_read_is_unknown_with_why_and_no_source_is_listed_apart(self):
        reading(self.connection, "codex", status="unknown", error="no session log in 30 minutes")
        reading(self.connection, "gemini", status="no-source")
        found = self.strip()
        self.assertEqual([(row["id"], row["state"], row["why"]) for row in found["rows"]],
                         [("usage:codex", "unknown", "no session log in 30 minutes")])
        self.assertEqual(found["nosource"], ["gemini"])

    def test_a_removed_provider_is_not_shown(self):
        reading(self.connection, "codex", timestamp="2026-09-06T11:00:00Z", **CODEX)
        record_state(self.connection, "heartbeat", key="usage:codex", timestamp="2026-09-06T11:40:00Z", body={"status": "removed"})
        self.assertEqual(self.strip()["rows"], [])

    def test_a_service_that_names_the_provider_flags_its_row(self):
        reading(self.connection, "codex", **CODEX)
        status(self.connection, "openai", status="outage", providers=["codex", "openai"],
               incidents=[{"title": "API errors", "url": "https://status.example.test/incidents/9"}])
        status(self.connection, "github", status="degraded", providers=["github"])
        flag = self.strip()["rows"][0]["service"]
        self.assertEqual(flag, {"name": "openai", "status": "outage", "state": "warning", "why": "", "title": "API errors",
                                "url": "https://status.example.test/incidents/9"})

    def test_a_listed_provider_with_no_row_is_unknown_no_reading_yet(self):
        # pack sd:9333: a typo or a missing job showed nothing at all.
        set_setting(self.connection, "dashboard.usage_providers", "codex\nopnerouter\n")
        reading(self.connection, "codex", **CODEX)
        self.assertEqual([(row["id"], row["state"], row["why"], row["windows"]) for row in self.strip()["rows"]][1:],
                         [("usage:opnerouter", "unknown", "no reading yet: the job has written no row for it", [])])

    def test_a_store_that_does_not_answer_is_its_reason_never_an_empty_strip(self):
        with mock.patch.object(usage_screen, "_newest", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(self.strip()["error"], "database is locked")


USAGE = re.search(r"^  // usage:start\n(.*?)^  // usage:end$", SHELL_JS, re.S | re.M)


def run_usage(body, *, answer=None, tz=None):
    """Runs shell.js's usage block (pack sd:9569) with a stub read that answers `answer` (a dict, or an Exception's text)."""
    assert USAGE, "shell.js has no usage:start block: the Usage dropdown is not the shell's"
    script = "var window = globalThis;\n" + MARKUP_JS + r"""
const { html, put } = window.markup;
const ICON = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const toLocal = Date.prototype.toLocaleString;
Date.prototype.toLocaleString = function (_, options) { return toLocal.call(this, 'en-US', options); };
var OUT = { gets: [], error: null }, R = {}, ANSWER = """ + json.dumps(answer) + r""";
const getJSON = async path => { OUT.gets.push(path); if (typeof ANSWER === 'string') throw new Error(ANSWER); return ANSWER; };
const flush = async () => { for (let i = 0; i < 50; i++) await null; };
(async () => { try {
""" + USAGE.group(1) + body + r"""
} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();
function run() { OUT.R = R; return JSON.stringify(OUT); }
"""
    env = {**os.environ, **({"TZ": tz} if tz else {})}
    result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True,
                            timeout=60, check=False, env=env)
    if result.returncode:
        raise AssertionError(result.stderr)
    out = json.loads(result.stdout)
    if out["error"]:
        raise AssertionError(out["error"])
    return out


class TheStripInTheUsageDropdown(ScreenCase):
    """pack sd:9569: Today's Usage strip is the shell's Usage dropdown, beside Read again on every page."""

    def strip(self):
        return usage_screen.strip(self.connection, now=NOW)

    def test_today_s_body_holds_no_usage_strip_and_its_read_carries_none(self):
        self.assertNotIn('id="usage"', (V2 / "today.html").read_text(encoding="utf-8"))
        self.assertNotIn("usageStrip", (V2 / "static" / "today.js").read_text(encoding="utf-8"))
        self.assertNotIn("usage", now_screen.document(self.connection, now=NOW, jobs=None, fleet=None))

    def test_the_dropdown_draws_bars_resets_in_the_local_zone_and_links_settings(self):
        reading(self.connection, "codex", **CODEX)
        status(self.connection, "openai", status="outage", providers=["codex"],
               incidents=[{"title": "API errors", "url": "https://status.example.test/incidents/9"}])
        reading(self.connection, "gemini", status="no-source")
        out = run_usage("R.body = usageBody(ANSWER).text; R.state = usageWorst(ANSWER);",
                        answer=self.strip(), tz="America/Denver")
        body = out["R"]["body"]
        self.assertIn("5h window 62% used", body)
        self.assertIn('<rect class="fill caution" width="85.5"', body)
        self.assertIn('<time datetime="2026-09-06T15:00:00Z">Sun, 09:00 AM MDT</time>', body)
        self.assertIn("25 pts over pace", body)
        self.assertIn('href="https://status.example.test/incidents/9"', body)
        self.assertIn("No reader for gemini.", body)
        self.assertIn('href="/settings#f-dashboard.usage_providers"', body)
        self.assertIn('href="/metrics"', body)
        self.assertIn('read <time class="rel" datetime="2026-09-06T11:50:00Z">', body)
        # The outage on codex's service is the worst state the summary shows.
        self.assertEqual(out["R"]["state"], "warning")

    def test_an_unknown_reading_says_why_and_draws_no_bar(self):
        reading(self.connection, "codex", read_at="2026-09-06T11:00:00Z", **CODEX)
        out = run_usage("R.body = usageBody(ANSWER).text; R.state = usageWorst(ANSWER);", answer=self.strip())
        self.assertIn("Unknown: read 1h ago; a reading over 30 min old is unknown", out["R"]["body"])
        self.assertNotIn("<rect", out["R"]["body"])
        self.assertEqual(out["R"]["state"], "unknown")

    def test_a_usage_read_that_failed_says_so_and_is_unknown_never_ok(self):
        failed = {"error": "database is locked", "rows": [], "nosource": []}
        out = run_usage("R.body = usageBody(ANSWER).text; R.state = usageWorst(ANSWER);", answer=failed)
        self.assertIn("Not read: database is locked", out["R"]["body"])
        self.assertEqual(out["R"]["state"], "unknown")

    def test_the_menu_anchors_to_the_page_head_and_stays_inside_the_gutters(self):
        """Lead review of 8f483c6: on Contributions at 375 px the action slot wraps left, and a menu anchored to the
        slot's right end ran past the screen's left edge. The menu anchors to the page head, which spans the gutters."""
        static = V2 / "static"
        rule = re.compile(r"([^{}]+)\{([^{}]*)\}")

        def rules(name):
            return rule.findall(re.sub(r"/\*.*?\*/", "", (static / name).read_text(encoding="utf-8"), flags=re.S))

        placed = re.compile(r"\bposition:\s*(relative|absolute|fixed|sticky)")
        for name in ("shell.css", "contributions.css", "home.css", "notes.css"):
            for selector, body in rules(name):
                if placed.search(body):
                    for slot in ("[data-again]", ".head-acts", ".tools", ".datebar"):
                        self.assertNotIn(slot, selector, f"{name}: {selector.strip()} makes the slot the menu's anchor")
        shell = {selector.strip(): body for selector, body in rules("shell.css")}
        self.assertRegex(shell[".head, .home-head"], r"position:\s*relative")
        menu = shell[".usage-drop > .ustrip"]
        self.assertRegex(menu, r"\bright:\s*0\b")
        self.assertRegex(menu, r"\bwidth:\s*min\(48rem, calc\(100vw - 2 \* var\(--space-md\)\), 100%\)")

    def test_the_kiosk_gives_the_usage_summary_read_again_s_touch_size(self):
        """Lead review of 8f483c6: Home's kiosk enlarged #read-again to 4rem but left the Usage summary beside it small."""
        css = re.sub(r"/\*.*?\*/", "", (V2 / "static" / "home.css").read_text(encoding="utf-8"), flags=re.S)
        [selector] = [found for found in re.findall(r"([^{}]+)\{[^{}]*min-height:\s*4rem", css)
                      if "html[data-kiosk] #read-again" in found]
        self.assertIn("html[data-kiosk] #usage-sum", [part.strip() for part in selector.split(",")])

class TheStripRoute(BrowserSession):
    def test_the_strip_route_answers_the_strip_and_refuses_a_query(self):
        reading(self.connection, "codex", **CODEX)
        status_code, _, body = self.request("/api/usage/strip", headers={"Cookie": self.cookie})
        self.assertEqual(status_code, 200, body)
        self.assertEqual([row["id"] for row in json.loads(body)["rows"]], ["usage:codex"])
        status_code, _, body = self.request("/api/usage/strip?x=1", headers={"Cookie": self.cookie})
        self.assertEqual(status_code, 400, body)
        status_code, _, body = self.request("/api/usage/strip")
        self.assertEqual(status_code, 403, body)

    def test_a_strip_that_raises_answers_its_reason(self):
        """Bundle review round 1 (pack sd:9257): a strip failure failed all of /api/now; now it costs the dropdown only."""
        with mock.patch.object(usage_screen, "strip", side_effect=OverflowError("date value out of range")):
            status_code, _, body = self.request("/api/usage/strip", headers={"Cookie": self.cookie})
        self.assertEqual((status_code, json.loads(body)), (500, {"error": "date value out of range"}))


if __name__ == "__main__":
    unittest.main()
