"""Health's Status view (pack sd:9260): the services the operator depends on, from system's status job.

system's `agent-meter.py services` (system sd:9259) writes one `heartbeat` row per 'status services' line, key
`service-status:<name>`; the newest row per key is the answer, and `{"status": "removed"}` means the line left the list.
A status read over 15 minutes ago is unknown, never its last answer. Settings holds the list (`dashboard.status_services`)
with the job's grammar, and the view links there for add and remove.
"""

from __future__ import annotations

import json
import subprocess
import unittest

import test_v2_health as health_tests
from sd_dashboard import health_screen, setting_rows, settings_screen, usage_screen
from sd_db.writes import record_state, set_setting
from support import NOW, ScreenCase
from test_v2_health import PROTECTION, TREES, fleet_of, ports_snapshot, protection_of
from test_v2_settings import MARKUP_JS, PAGE_JS
from test_v2_tasks import FETCH_JS, STAND_IN
from test_v2_today import OSASCRIPT

STATUSPAGE = "https://status.example.test"


def status(connection, name, *, timestamp="2026-09-06T11:55:00Z", **body):
    body = {"name": name, "kind": "statuspage", "url": STATUSPAGE, "providers": [], "read_at": "2026-09-06T11:55:00Z",
            "status": "ok", "incidents": [], "components": [], **body}
    record_state(connection, "heartbeat", key=f"service-status:{name}", timestamp=timestamp, body=body)


class TheList(unittest.TestCase):
    def test_the_status_services_list_is_a_settings_list_with_the_job_s_grammar(self):
        self.assertIn("dashboard.status_services", setting_rows.LISTS)
        check = settings_screen._list("dashboard.status_services")[1]
        self.assertEqual(check("claude statuspage https://status.example.test claude\n"
                               "# a comment\nslack slack https://slack-status.example.test/api/v2.0.0/current\n"), [])
        self.assertEqual(check("Claude statuspage https://status.example.test\n"
                               "x ftp https://status.example.test\n"
                               "y http ftp://status.example.test\n"
                               "z http https://status.example.test A,b\n"
                               "only two\n"
                               "y http https://status.example.test\ny http https://status.example.test\n"),
                         ["line 1: name is not a new lower-case name", "line 2: kind is not one of statuspage, slack, "
                          "google-incidents, http", "line 3: url is not http(s)", "line 4: a provider is not a lower-case name",
                          "line 5: want <name> <kind> <url> [provider] [key=value ...]",
                          "line 7: name is not a new lower-case name"])

    def test_a_line_takes_groups_and_stale_options_as_the_job_does(self):
        """pack sd:9746: system sd:9722's options; Settings refused the line and the view dropped it."""
        check = settings_screen._list("dashboard.status_services")[1]
        self.assertEqual(check("edge statuspage https://status.example.test edge groups=north-america,europe stale=3\n"
                               "cdn statuspage https://cdn.example.test stale=1\n"), [])
        found, _ = usage_screen.parse_services("edge statuspage https://status.example.test edge groups=north-america "
                                               "stale=3\ncdn statuspage https://cdn.example.test\n")
        self.assertEqual(found, [("edge", "statuspage", "https://status.example.test", ["edge"],
                                  {"groups": ["north-america"], "stale": 3}),
                                 ("cdn", "statuspage", "https://cdn.example.test", [], {})])
        self.assertEqual(check("a statuspage https://a.example.test color=red\n"
                               "b statuspage https://b.example.test stale=1 stale=2\n"
                               "c statuspage https://c.example.test stale=0\n"
                               "d statuspage https://d.example.test stale=x\n"
                               "e statuspage https://e.example.test groups=North-America\n"
                               "f http https://f.example.test stale=3\n"
                               "g statuspage https://g.example.test stale=3 extra\n"),
                         ["line 1: option color is not groups or stale", "line 2: option stale given twice",
                          "line 3: stale is not a whole number of days above 0",
                          "line 4: stale is not a whole number of days above 0",
                          "line 5: groups is not lower-case group names", "line 6: options need kind statuspage",
                          "line 7: want <name> <kind> <url> [provider] [key=value ...]"])


class TheRows(ScreenCase):
    def area(self):
        doc = health_screen.part(self.connection, "status", now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot,
                                 protection=protection_of(PROTECTION))
        self.assertEqual([area["id"] for area in doc["areas"]], ["stat"])
        return doc["areas"][0]

    def test_each_service_is_a_row_degraded_caution_outage_warning(self):
        status(self.connection, "claude", status="outage", providers=["claude"], incidents=[
            {"title": "Elevated errors", "url": "https://status.example.test/incidents/1", "impact": "major",
             "started_at": "2026-09-06T10:00:00Z"},
            {"title": "Slow logins", "url": "", "impact": "minor", "started_at": None}])
        status(self.connection, "openai", status="degraded", providers=["codex", "openai"],
               components=[{"name": "API", "status": "degraded_performance"}])
        status(self.connection, "github", status="ok")
        area = self.area()
        rows = {row["id"]: row for row in area["rows"]}
        self.assertEqual({key: (row["state"], row["what"]) for key, row in rows.items()},
                         {"stat:claude": ("warning", "claude: outage"), "stat:openai": ("caution", "openai: degraded"),
                          "stat:github": ("ok", "github: ok")})
        self.assertEqual(rows["stat:claude"]["detail"], "Elevated errors · for 2h · +1 more")
        self.assertEqual(rows["stat:claude"]["list"], [{"text": "Elevated errors", "href": "https://status.example.test/incidents/1"},
                                                       "Slow logins"])
        self.assertEqual(rows["stat:openai"]["detail"], "API degraded performance")
        self.assertEqual(rows["stat:claude"]["read_at"], "2026-09-06T11:55:00Z")
        self.assertEqual(area["extra"]["settings"], "/settings#f-dashboard.status_services")

    def test_a_status_over_15_minutes_old_is_unknown_never_its_last_answer(self):
        status(self.connection, "claude", status="ok", read_at="2026-09-06T11:44:00Z")
        row = self.area()["rows"][0]
        self.assertEqual((row["state"], row["what"]), ("unknown", "claude: unknown"))
        self.assertIn("read 16m ago", row["detail"])

    def test_a_removed_line_is_not_shown_and_the_newest_row_answers(self):
        status(self.connection, "gone", timestamp="2026-09-06T11:00:00Z", status="outage")
        record_state(self.connection, "heartbeat", key="service-status:gone", timestamp="2026-09-06T11:50:00Z",
                     body={"status": "removed"})
        status(self.connection, "claude", timestamp="2026-09-06T11:00:00Z", status="outage")
        status(self.connection, "claude", timestamp="2026-09-06T11:56:00Z", status="ok")
        self.assertEqual([(row["id"], row["state"]) for row in self.area()["rows"]], [("stat:claude", "ok")])

    def test_a_failed_read_or_a_row_that_does_not_parse_is_unknown_with_why(self):
        status(self.connection, "slack", status="unknown", error="HTTP 503")
        record_state(self.connection, "heartbeat", key="service-status:bad", body="not json at all")
        self.connection.execute("UPDATE state SET body = '{oops' WHERE key = 'service-status:bad'")
        self.connection.commit()
        rows = {row["id"]: row for row in self.area()["rows"]}
        self.assertEqual((rows["stat:slack"]["state"], rows["stat:slack"]["detail"]), ("unknown", "HTTP 503"))
        self.assertEqual((rows["stat:bad"]["state"], rows["stat:bad"]["detail"]), ("unknown", "the row does not parse"))

    def test_no_row_yet_is_one_unknown_row_not_a_clean_area(self):
        rows = self.area()["rows"]
        self.assertEqual([(row["id"], row["state"]) for row in rows], [("stat:none", "unknown")])

    def test_a_listed_service_with_no_row_is_unknown_no_reading_yet(self):
        # pack sd:9333: a typo or a missing job showed nothing at all.
        set_setting(self.connection, "dashboard.status_services",
                    "github statuspage https://status.example.test\nopnai statuspage https://other.example.test codex\n")
        status(self.connection, "github", status="ok")
        rows = {row["id"]: row for row in self.area()["rows"]}
        self.assertEqual((rows["stat:opnai"]["state"], rows["stat:opnai"]["detail"], rows["stat:opnai"]["url"],
                          rows["stat:opnai"]["facts"]["Providers"]),
                         ("unknown", "no reading yet: the job has written no row for it", "https://other.example.test", "codex"))
        self.assertEqual(rows["stat:github"]["state"], "ok")

    def test_a_listed_line_with_options_is_not_dropped(self):
        # pack sd:9746: a line with groups= or stale= did not parse, so its service never showed.
        set_setting(self.connection, "dashboard.status_services",
                    "edge statuspage https://status.example.test codex groups=north-america stale=3\n")
        rows = {row["id"]: row for row in self.area()["rows"]}
        self.assertEqual((rows["stat:edge"]["state"], rows["stat:edge"]["facts"]["Providers"]), ("unknown", "codex"))

    def test_only_removed_services_mean_none_watched_and_no_row(self):
        # Bundle review round 4: after the last service was removed, the
        # area showed "No service status read yet" for good.
        status(self.connection, "claude", timestamp="2026-09-06T11:00:00Z")
        record_state(self.connection, "heartbeat", key="service-status:claude", timestamp="2026-09-06T11:50:00Z",
                     body={"status": "removed"})
        area = self.area()
        self.assertEqual((area["rows"], area["extra"]["counts"]["unknown"]), ([], 0))


class TheView(health_tests.Collectors, ScreenCase):
    run_doc, run_page = health_tests.TheScript.run_doc, health_tests.TheScript.run_page

    def test_the_status_view_draws_the_rows_links_settings_and_opens_the_incident(self):
        status(self.connection, "claude", status="outage", providers=["claude"], incidents=[
            {"title": "Elevated errors", "url": "https://status.example.test/incidents/1", "started_at": "2026-09-06T10:00:00Z"}])
        out = self.run_page("""R.body = ELS['body-status'].html;
const o = C.get('stat:claude'); R.type = o.type;
const cmd = REG.find(c => c.id === 'service status.open'); R.when = cmd.when(o);
OUT.opened = []; window.open = (u, t) => OUT.opened.push([u, t]); R.said = cmd.run(o);""", view="status")
        self.assertIn('href="/settings#f-dashboard.status_services"', out["R"]["body"])
        self.assertIn("claude: outage", out["R"]["body"])
        self.assertIn('<time datetime="2026-09-06T11:55:00Z">', out["R"]["body"])
        self.assertEqual((out["R"]["type"], out["R"]["when"]), ("service status", True))
        self.assertEqual(out["opened"], [["https://status.example.test/incidents/1", "_blank"]])


    def test_each_copy_line_quotes_what_a_feed_or_a_repo_supplies(self):
        # Bundle review round 2: `open <url>` put a feed's URL into a shell
        # line unquoted; a sweep found the services and protection lines.
        status(self.connection, "odd", status="outage", incidents=[
            {"title": "x", "url": "https://status.example.test/i;$(id)"}])
        out = self.run_page("""const o = C.get('stat:odd');
const cli = id => REG.find(c => c.id === id).cli;
R.open = cli('service status.open')(o);
R.restart = cli('service.restart')({ facts: { Label: 'a;$(id)' } });
R.start = cli('service.start')({ facts: { Label: 'a;$(id)' } });
R.prot = cli('protection.settings')({ slug: 'o/r;$(id)' });""", view="status")
        self.assertEqual(out["R"], {"open": "open 'https://status.example.test/i;$(id)'",
                                    "restart": "sd services restart 'a;$(id)'",
                                    "start": "sd services start 'a;$(id)'",
                                    "prot": "open 'https://github.com/o/r;$(id)/settings/branches'"})

    def test_no_status_read_yet_counts_no_service(self):
        out = self.run_page("R.body = ELS['body-status'].html;", view="status")
        self.assertIn("0 services · 0 outage · 0 degraded · 0 unknown · 0 ok", out["R"]["body"])


class TheSettingsLink(unittest.TestCase):
    """A link to `/settings#f-<key>` focuses that row's field once the rows have drawn (pack sd:9260)."""

    def test_the_hash_names_the_field_settings_focuses(self):
        doc = {"fleet": [], "sweeps": [], "dashboard": [{"key": "dashboard.status_services", "value": None, "source": "unset",
                                                         "lines": True, "description": "", "digest": "d"}], "reason": "",
               "dashboard_reason": ""}
        script = (STAND_IN + MARKUP_JS + FETCH_JS + f"\nANSWER = () => [200, {json.dumps(doc)}];\n"
                  "location.hash = '#f-dashboard.status_services'; OUT.focused = null;\n"
                  "ELS['f-dashboard.status_services'] = Object.assign(El('f-dashboard.status_services'),"
                  " { focus() { OUT.focused = this.id; } });\n"
                  "window.shell = { post, getJSON, railWidth: () => null };\n" + PAGE_JS
                  + "\nconst flush = async () => { for (let i = 0; i < 200; i++) await null; };\n"
                  "(async () => { try { (DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f()); await flush();\n"

                  "} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  "function run() { return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        self.assertEqual(out["focused"], "f-dashboard.status_services", "the field the link names was not focused")


class TheServicesReader(ScreenCase):
    def test_a_status_job_with_no_read_time_is_unknown(self):
        status(self.connection, "claude", read_at=None)
        self.assertEqual([(one["status"], one["why"]) for one in usage_screen.services(self.connection, now=NOW)],
                         [("unknown", "the reading has no read time")])

    def test_a_failed_read_with_no_read_time_shows_its_error(self):
        # pack sd:9380: an HTTP 503 has no read time; the error says why, as the Usage strip says it.
        status(self.connection, "slack", read_at=None, status="unknown", error="HTTP 503")
        self.assertEqual([(one["status"], one["why"]) for one in usage_screen.services(self.connection, now=NOW)],
                         [("unknown", "HTTP 503")])

    def test_ignore_holds_only_for_the_same_components(self):
        # pack sd:9380: a degraded service with no incident fingerprinted its status alone, so an ignored
        # degradation hid a later component failure.
        status(self.connection, "a", status="degraded", components=[{"name": "API", "status": "degraded_performance"}])
        status(self.connection, "b", status="degraded", components=[{"name": "Login", "status": "major_outage"}])
        rows, _ = health_screen._status_rows(usage_screen.services(self.connection, now=NOW), now=NOW)
        self.assertNotEqual(rows[0]["problem"], rows[1]["problem"])

    def test_each_text_field_is_capped_at_200_characters(self):
        # Bundle review round 3: kind, indicator, providers, impact and
        # components passed through uncapped.
        long = "x" * 10_000
        status(self.connection, "s", status="degraded", kind=long, indicator=long, providers=[long, "codex"],
               incidents=[{"title": long, "impact": long}], components=[{"name": long, "status": long}])
        [one] = usage_screen.services(self.connection, now=NOW)
        self.assertEqual((len(one["kind"]), len(one["indicator"]), one["providers"]), (200, 200, ["codex"]))
        self.assertEqual((len(one["incidents"][0]["title"]), len(one["incidents"][0]["impact"])), (200, 200))
        self.assertEqual(one["components"], [{"name": "x" * 200, "status": "x" * 200}])

    def test_ignore_holds_only_for_the_same_incident(self):
        # Bundle review round 2: the fingerprint read titles only, so an
        # ignored outage hid a later one with the same title.
        first = {"title": "API errors", "url": "https://status.example.test/incidents/1",
                 "started_at": "2026-09-06T10:00:00Z"}
        status(self.connection, "a", status="outage", incidents=[first])
        status(self.connection, "b", status="outage", incidents=[{**first, "started_at": "2026-09-06T11:00:00Z"}])
        status(self.connection, "c", status="outage", incidents=[{**first, "url": "https://status.example.test/incidents/2"}])
        rows, _ = health_screen._status_rows(usage_screen.services(self.connection, now=NOW), now=NOW)
        self.assertEqual(len({json.dumps(row["problem"]) for row in rows}), 3)
        self.assertEqual(rows[0]["problem"], ["outage", "API errors", "https://status.example.test/incidents/1",
                                              "2026-09-06T10:00:00Z"])

    def test_a_status_that_is_not_a_known_word_is_unknown_never_an_error(self):
        # Bundle review round 1: a list or object status raised TypeError
        # in the lookup, which hid the Status view and failed /api/now.
        for number, odd in enumerate((["ok"], {"ok": 1}, 7, "fine")):
            status(self.connection, f"s{number}", status=odd)
        found = usage_screen.services(self.connection, now=NOW)
        self.assertEqual([(one["name"], one["status"]) for one in found],
                         [("s0", "unknown"), ("s1", "unknown"), ("s2", "unknown"), ("s3", "unknown")])
        self.assertEqual({one["why"] for one in found}, {"the status job could not read it"})

    def test_a_time_past_the_calendar_s_end_is_unknown_never_an_error(self):
        # Bundle review round 1: OverflowError escaped _when.
        status(self.connection, "s", read_at="9999-12-31T23:59:59-12:00")
        status(self.connection, "t", status="outage", incidents=[{"title": "x", "started_at": "9999-12-31T23:59:59-12:00"}])
        self.assertIsNone(usage_screen._when("9999-12-31T23:59:59-12:00"))
        found = {one["name"]: one for one in usage_screen.services(self.connection, now=NOW)}
        self.assertEqual((found["s"]["status"], found["s"]["why"]), ("unknown", "the reading has no read time"))
        self.assertIsNone(found["t"]["incidents"][0]["started_at"])

    def test_an_unknown_reading_keeps_no_incident_or_component(self):
        # pack sd:9498: a stale row kept its last incidents, so an outage that has since ended still showed open.
        stale = {"title": "API errors", "started_at": "2026-09-06T10:00:00Z"}
        status(self.connection, "stale", status="outage", read_at="2026-09-06T11:00:00Z", incidents=[stale],
               components=[{"name": "API", "status": "major_outage"}])
        status(self.connection, "failed", status="unknown", error="HTTP 503", incidents=[stale])
        found = usage_screen.services(self.connection, now=NOW)
        self.assertEqual([(one["name"], one["status"], one["incidents"], one["components"]) for one in found],
                         [("failed", "unknown", [], []), ("stale", "unknown", [], [])])


if __name__ == "__main__":
    unittest.main()
