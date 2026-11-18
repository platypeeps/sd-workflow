"""Today's Usage strip (pack sd:9257): each provider's windows, reset, pace and read time, from system's usage job.

system's `agent-meter.py usage` (system sd:9256) writes one `heartbeat` row per 'usage providers' line, key
`usage:<provider>`; the newest row per key is the answer. claude's reading carries cswap's accounts and only their
seven-day share; the strip shows the windows a reading has and never invents five hours. A reading over 30 minutes old
is unknown, never its last numbers and never 0. A service whose `providers` name the provider flags its row.
"""

from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from sd_dashboard import setting_rows, settings_screen, usage_screen
from sd_db.writes import record_state
from support import NOW, ScreenCase
from test_service_status import status
from test_v2_today_reader import ReaderCase

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
        # 3 of 5 hours left at 12:00 is 40 % through: 62 used is 22 over; 4 of 7 days left is 43 % through: 86 is 43 over.
        self.assertEqual(row["windows"], [{"label": "5h", "used": 62.0, "resets_at": "2026-09-06T15:00:00Z", "pace": 22},
                                          {"label": "7d", "used": 85.5, "resets_at": "2026-09-10T12:00:00Z", "pace": 43}])

    def test_claude_is_a_row_per_cswap_account_with_its_seven_days_only(self):
        reading(self.connection, "claude", **CLAUDE)
        rows = self.strip()["rows"]
        self.assertEqual([(row["label"], row["state"], row["active"]) for row in rows],
                         [("claude#1", "ok", True), ("claude#2", "unknown", False)])
        self.assertEqual(rows[0]["windows"], [{"label": "7d", "used": 40.0, "resets_at": "2026-09-08T12:00:00Z", "pace": -10}])
        self.assertEqual((rows[1]["windows"], rows[1]["why"]), ([], "read 2h ago; a reading over 30 min old is unknown"))

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

    def test_a_store_that_does_not_answer_is_its_reason_never_an_empty_strip(self):
        with mock.patch.object(usage_screen, "_newest", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(self.strip()["error"], "database is locked")


class TheStripOnToday(ReaderCase):
    def test_today_s_read_carries_the_strip(self):
        self.assertEqual(self.doc["usage"], {"error": "", "rows": [], "nosource": [], "flags_error": "",
                                             "providers": "/settings#f-dashboard.usage_providers",
                                             "services": "/settings#f-dashboard.status_services"})

    def test_the_strip_draws_bars_resets_in_the_local_zone_and_links_settings(self):
        reading(self.connection, "codex", **CODEX)
        status(self.connection, "openai", status="outage", providers=["codex"],
               incidents=[{"title": "API errors", "url": "https://status.example.test/incidents/9"}])
        reading(self.connection, "gemini", status="no-source")
        self.doc["usage"] = usage_screen.strip(self.connection, now=NOW)
        out = self.run_page("R.usage = ELS.usage.html;")
        strip = out["R"]["usage"]
        self.assertIn("5h window 62% used", strip)
        self.assertIn('<rect class="fill caution" width="85.5"', strip)
        self.assertIn('<time datetime="2026-09-06T15:00:00Z">', strip)
        self.assertRegex(strip, r'<time datetime="2026-09-06T15:00:00Z">[^<]*\b[A-Z]{2,4}[^<]*</time>')
        self.assertIn("22 pts over pace", strip)
        self.assertIn('href="https://status.example.test/incidents/9"', strip)
        self.assertIn("No reader for gemini.", strip)
        self.assertIn('href="/settings#f-dashboard.usage_providers"', strip)
        self.assertIn('read <time class="rel" datetime="2026-09-06T11:50:00Z">', strip)

    def test_an_unknown_reading_says_why_and_draws_no_bar(self):
        reading(self.connection, "codex", read_at="2026-09-06T11:00:00Z", **CODEX)
        self.doc["usage"] = usage_screen.strip(self.connection, now=NOW)
        strip = self.run_page("R.usage = ELS.usage.html;")["R"]["usage"]
        self.assertIn("Unknown: read 1h ago; a reading over 30 min old is unknown", strip)
        self.assertNotIn("<rect", strip)

    def test_a_usage_read_that_failed_says_so(self):
        self.doc["usage"] = {"error": "database is locked", "rows": [], "nosource": []}
        strip = self.run_page("R.usage = ELS.usage.html;")["R"]["usage"]
        self.assertIn("Not read: database is locked", strip)


if __name__ == "__main__":
    unittest.main()
