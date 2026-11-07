"""The v2 Watches page (sd:6264).

What this slice promises: `/api/watches` lists every market row with its status and its last refresh, the newest
`Last full refresh:` date in its profile, and never calls an active watch fresh past `STALE_DAYS`; it carries the
market-watch job's state and why its last run failed. Refresh watches is Run now on that job; Refresh watch runs one watch's
refresh in herdr and Add watch files a candidate row, both through `/api/actions/run`, which reads every id against the
store and refuses before anything runs.

`watches.js` runs under JavaScriptCore (osascript) against the stand-in page and shell `test_v2_tasks` uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import fix_session, run_actions, v2, watches_screen
from sd_db import create_item
from support import NOW, ScreenCase
from test_run_actions import Run
from test_v2_hoa import HoaJobs
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT

V2 = Path(v2.__file__).resolve().parent
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
WATCHES_JS = (V2 / "static" / "watches.js").read_text(encoding="utf-8")


def market(connection, title, stage, body="", **fields):
    """One `sdw.market` row, as the vault import files it: an idea item of source vault whose fields name its kind."""
    return create_item(connection, kind="idea", title=title, status={"candidate": "planning", "active": "ready", "paused": "blocked"}.get(stage, "done"),
                       source="vault", external_id=f"market:Market Watch/{title}.md", path=f"Market Watch/{title}.md", stage=stage,
                       fields={"kind": "market", "dateCreated": "2026-01-06", **fields}, body={"markdown": body})


def plugin(kind, base, status, transitions, human):
    """One `sd plugin list --json` entry declaring `kind` on the `db` driver, so a stage move never reads the real pack."""
    rules = {"fields": ["status", "slug", "dateCreated", "content-type", "area", "description", "tags"],
             "initial-status": "candidate", "transitions": transitions, "human-only": human}
    return {"root": "/plugins/sdw", "readable": True, "prefix": "sdw",
            "store": {"driver": "db", "root": "$OBSIDIAN_VAULT", "bases": {kind: base}},
            "workflow": {kind: {"status": status}}, "rules": {kind: rules}}


MARKET = plugin("market", "Market Watch", {"active": "ready", "candidate": "planning", "declined": "done", "paused": "blocked"},
                {"active": ["paused", "declined"], "candidate": ["active", "paused", "declined"], "paused": ["active", "declined"],
                 "declined": ["candidate"]},
                {"decline": "declined"})


def profile(*dates, source=""):
    """A profile body: an imported lead-in above `## What it is`, then the routine's own, then its Sources."""
    lead = [f"*Profile maintained in place by the daily `market-watch` routine. Last full refresh: {d}.*" for d in dates]
    return "\n# X\n\n" + (lead[0] if lead else "") + "\n\n## What it is\n\n" + "\n".join(lead[1:]) + f"\n\n## Sources\n\n{source}\n\n## Provenance\n"


def seed(connection):
    """NOW is 2026-09-06: a fresh watch, a stale one, one with no profile, a candidate and a paused one."""
    return {
        "fresh": market(connection, "Example Logs", "active", profile("2026-07-01", "2026-09-01", source="- [site](https://logs.example.test/about)."),
                        **{"content-type": "company", "area": "Observability", "slug": "example-logs", "description": "A log vendor."}),
        "stale": market(connection, "Widget Ops", "active", profile("2026-08-01"), **{"content-type": "repository"}),
        "never": market(connection, "New Agent", "active", "", **{"content-type": "company"}),
        "candidate": market(connection, "Maybe Co", "candidate", profile(source="- https://maybe.example.test"), **{"content-type": "company"}),
        "paused": market(connection, "Old Co", "paused", profile("2026-01-02")),
    }


class TheDocument(ScreenCase):
    def setUp(self):
        super().setUp()
        self.jobs = HoaJobs(self.enterContext(tempfile.TemporaryDirectory()), {"market-watch": ("failed", 1)})

    def stamp(self, text):
        (self.jobs.cron_root / "logs" / ".market-watch.stamp").write_text(text)

    def test_a_watch_says_its_status_and_last_refresh_and_a_stale_one_is_never_fresh(self):
        ids = seed(self.connection)
        rows = {r["id"]: r for r in watches_screen.document(self.connection, now=NOW, jobs=self.jobs)["rows"]}
        fresh, stale, never, candidate, paused = (rows[ids[k]] for k in ("fresh", "stale", "never", "candidate", "paused"))
        # The newest date in the body, not the imported lead-in's.
        self.assertEqual((fresh["stage"], fresh["refreshed"], fresh["age"], fresh["fresh"]), ("active", "2026-09-01", 5, "fresh"))
        self.assertEqual((fresh["url"], fresh["host"], fresh["kind"], fresh["area"]),
                         ("https://logs.example.test/about", "logs.example.test", "company", "Observability"))
        self.assertEqual((stale["refreshed"], stale["age"], stale["fresh"]), ("2026-08-01", 36, "stale"))
        self.assertEqual((never["refreshed"], never["age"], never["fresh"]), ("", None, "never"))
        # A watch the routine does not refresh is not stale, whatever its date.
        self.assertEqual((candidate["fresh"], candidate["url"]), ("idle", "https://maybe.example.test"))
        self.assertEqual((paused["stage"], paused["fresh"]), ("paused", "idle"))

    def test_each_watch_carries_the_revision_a_stage_move_sends(self):
        """sd:6694: Activate, Pause and Unpause post /api/items/<n>/stage with the row's revision."""
        from sd_db import workflow

        ids = seed(self.connection)
        rows = {r["id"]: r for r in watches_screen.document(self.connection, now=NOW, jobs=self.jobs)["rows"]}
        for item in ids.values():
            self.assertEqual(rows[item]["revision"], workflow.item_state(self.connection, item)["revision"])

    def test_the_candidates_pool_and_the_declined_rows_are_read(self):
        """sd:8014: `pool` holds the candidates the bulk Decline may move; `declined` each declined row's when and by."""
        ids = seed(self.connection)
        gone = market(self.connection, "Gone Co", "declined")
        doc = watches_screen.document(self.connection, now=NOW, jobs=self.jobs)
        self.assertEqual([(r["item"], r["age"]) for r in doc["pool"]], [(ids["candidate"], 243)])
        self.assertEqual([r["item"] for r in doc["declined"]], [gone])

    def test_the_bound_is_stale_days(self):
        days = watches_screen.STALE_DAYS
        market(self.connection, "Edge", "active", profile((date(2026, 9, 6) - timedelta(days=days)).isoformat()))
        market(self.connection, "Past", "active", profile((date(2026, 9, 6) - timedelta(days=days + 1)).isoformat()))
        rows = {r["title"]: r for r in watches_screen.document(self.connection, now=NOW, jobs=self.jobs)["rows"]}
        self.assertEqual((rows["Edge"]["age"], rows["Edge"]["fresh"]), (days, "fresh"))
        self.assertEqual((rows["Past"]["age"], rows["Past"]["fresh"]), (days + 1, "stale"))

    def test_a_failed_run_says_why(self):
        self.stamp("started=2026-09-06T09:00:00Z\nended=2026-09-06T09:04:00Z\nexit=1\n")
        self.jobs.log("market-watch", "[market-watch] starting\nrouting lock held by another run\n[market-watch] exit 1\n")
        job = watches_screen.document(self.connection, now=NOW, jobs=self.jobs)["job"]
        self.assertEqual((job["name"], job["state"], job["last_exit"]), ("market-watch", "failed", 1))
        self.assertEqual(job["last_run"], {"state": "finished", "started": "2026-09-06T09:00:00Z", "ended": "2026-09-06T09:04:00Z", "exit": 1, "reason": ""})
        self.assertEqual(job["said"], "routing lock held by another run")
        self.assertTrue(job["capabilities"]["retry"]["allowed"])
        self.assertRegex(job["revision"], r"^[0-9a-f]{64}$")

    def test_a_job_that_is_not_installed_says_so_and_the_rows_still_read(self):
        seed(self.connection)
        doc = watches_screen.document(self.connection, now=NOW, jobs=HoaJobs(self.enterContext(tempfile.TemporaryDirectory())))
        self.assertIn("market-watch is not an installed job", doc["job"]["error"])
        self.assertIn("cron-jobs.sh install market-watch", doc["job"]["error"])
        self.assertEqual(len(doc["rows"]), 5)

    def test_the_route_reads_the_watches_and_writes_nothing(self):
        seed(self.connection)
        before = self.connection.execute("SELECT id, kind, status, stage FROM item ORDER BY id").fetchall()
        doc = v2.pages.watches._read(types.SimpleNamespace(connection=self.connection, now=NOW, jobs=self.jobs))
        self.assertEqual((doc["read"], doc["today"], doc["stale_days"]), (NOW, "2026-09-06", watches_screen.STALE_DAYS))
        self.assertEqual(self.connection.execute("SELECT id, kind, status, stage FROM item ORDER BY id").fetchall(), before)


    def test_a_malformed_source_url_is_text_and_the_other_rows_still_read(self):
        """sd:6280: a Sources link urlsplit refuses no longer fails the whole read; its row shows the text, unlinked."""
        ids = seed(self.connection)
        broken = market(self.connection, "Broken Co", "active", profile("2026-09-01", source="- https://[broken"))
        wide = market(self.connection, "Wide Co", "active", profile("2026-09-01", source="- https://example.com\uff1a80"))
        rows = {r["id"]: r for r in watches_screen.document(self.connection, now=NOW, jobs=self.jobs)["rows"]}
        self.assertEqual((rows[broken]["url"], rows[broken]["host"], rows[broken]["url_text"]), ("", "", "https://[broken"))
        self.assertEqual((rows[wide]["url"], rows[wide]["url_text"]), ("", "https://example.com\uff1a80"))
        self.assertEqual((rows[ids["fresh"]]["url"], rows[ids["fresh"]]["url_text"]), ("https://logs.example.test/about", ""))

class TheActions(Run):
    """watch.refresh and watch.add through `/api/actions/run`, with herdr patched and `sd` a stub on PATH."""

    def setUp(self):
        super().setUp()
        self.vault = Path(self.tmp.name).resolve() / "vault"
        (self.vault / run_actions.WATCH_ROUTINE).parent.mkdir(parents=True)
        (self.vault / run_actions.WATCH_ROUTINE).write_text("# market-watch\n")
        env = patch.dict(os.environ, {"OBSIDIAN_VAULT": str(self.vault)})
        env.start()
        self.addCleanup(env.stop)
        self.launched = []
        launch = patch.object(fix_session, "_launch", lambda *a: self.launched.append(a) or "w1:p1")
        launch.start()
        self.addCleanup(launch.stop)
        self.ids = seed(self.connection)
        self.connection.commit()

    def refused(self, cases):
        before = self.snapshot()
        for ids, code, said in cases:
            with self.subTest(ids=ids):
                status, _, body = self.post("/api/actions/run", ids)
                self.assertEqual(status, code)
                self.assertIn(said, body["error"])
        self.assertEqual((self.ran(), self.launched), ([], []))
        self.assertEqual(self.snapshot(), before)

    def test_refresh_starts_claude_in_the_vault_on_one_active_watch(self):
        status, body = self.run_("watch.refresh", item=self.ids["stale"])
        self.assertEqual((status, body["exit"], body["label"]), (200, 0, "Refresh watch"), body)
        [(directory, label, text)] = self.launched
        self.assertEqual((directory, label), (self.vault, f"watch-{self.ids['stale']}"))
        self.assertIn(f"'{run_actions.WATCH_ROUTINE}'", text)
        self.assertIn('market row "Widget Ops" only', text)
        self.assertIn("w1:p1", body["output"])

    def test_a_herdr_refusal_is_the_refreshs_exit_and_says_why(self):
        with patch.object(fix_session, "_launch", lambda *a: (503, {"error": "Fix failed: herdr is not running"})):
            status, body = self.run_("watch.refresh", item=self.ids["fresh"])
        self.assertEqual((status, body["exit"], body["output"]), (200, 1, "herdr is not running"))

    def test_refresh_refuses_a_watch_that_is_not_active_or_not_a_market_row(self):
        other = create_item(self.connection, kind="task", title="Not a watch")
        self.connection.commit()
        self.refused([({"action": "watch.refresh", "item": self.ids["candidate"]}, 400, "refreshes active watches only"),
                      ({"action": "watch.refresh", "item": self.ids["paused"]}, 400, "refreshes active watches only"),
                      ({"action": "watch.refresh", "item": other}, 400, "is not a market row"),
                      ({"action": "watch.refresh", "item": str(self.ids["stale"])}, 400, "by its item number"),
                      ({"action": "watch.refresh", "item": self.ids["stale"], "title": "x"}, 400, "takes item")])

    def test_refresh_refuses_when_the_routine_is_not_in_the_vault(self):
        (self.vault / run_actions.WATCH_ROUTINE).unlink()
        self.refused([({"action": "watch.refresh", "item": self.ids["stale"]}, 400, "routine is not at")])

    def test_add_files_a_candidate_with_the_url_as_its_first_source(self):
        status, body = self.run_("watch.add", url="https://github.com/example/widget-agent", name="")
        self.assertEqual((status, body["exit"]), (200, 0), body)
        [line] = self.ran()
        self.assertTrue(line.startswith("sd store add sdw.market widget-agent --field content-type=repository --field slug=widget-agent"), line)
        self.assertIn("--field tags+=market-watch --section Sources=- https://github.com/example/widget-agent", line)
        status, body = self.run_("watch.add", url="https://www.example.test/product", name="Example (Cloud): Ops")
        self.assertEqual(self.ran()[-1].split(" --field")[0:3],
                         ["sd store add sdw.market Example Cloud Ops", " content-type=company", " slug=example-cloud-ops"])

    def test_remove_declines_the_watch_and_keeps_its_body(self):
        """sd:6703: Remove retires, it does not delete: the stage goes to declined, the profile stays."""
        item = self.ids["candidate"]
        with patch("sd_db.records.plugin_entries", return_value=[MARKET]):
            status, body = self.run_("watch.remove", item=item)
            self.assertEqual((status, body["exit"], body["label"]), (200, 0, "Remove watch"), body)
            stage, state, kept = self.connection.execute("SELECT stage, status, body FROM item WHERE id=?", (item,)).fetchone()
            self.assertEqual((stage, state), ("declined", "done"))
            self.assertIn("maybe.example.test", kept)
            self.assertIn("stage candidate -> declined", body["output"])
            self.refused([({"action": "watch.remove", "item": item}, 400, "is declined already"),
                          ({"action": "watch.remove", "item": str(item)}, 400, "by its item number")])

    def test_restore_brings_a_declined_watch_back_to_candidate(self):
        """sd:6703: Restore undoes Remove: declined goes back to candidate (sd-writing sd:6828), the profile kept."""
        item = self.ids["candidate"]
        with patch("sd_db.records.plugin_entries", return_value=[MARKET]):
            self.refused([({"action": "watch.restore", "item": item}, 400, "is candidate, not declined")])
            self.assertEqual(self.run_("watch.remove", item=item)[1]["exit"], 0)
            status, body = self.run_("watch.restore", item=item)
            self.assertEqual((status, body["exit"], body["label"]), (200, 0, "Restore watch"), body)
            stage, kept = self.connection.execute("SELECT stage, body FROM item WHERE id=?", (item,)).fetchone()
            self.assertEqual(stage, "candidate")
            self.assertIn("maybe.example.test", kept)
            self.assertIn("stage declined -> candidate", body["output"])

    def same_store(self):
        """`sd-db` opens `default_path(HOME)`; here it is the database the dashboard serves, so Delete may run."""
        same = patch("sd_db.database.default_path", return_value=Path(self.path))
        same.start()
        self.addCleanup(same.stop)

    def test_delete_refuses_when_sd_db_would_open_another_database(self):
        """Review of build/sd-206: the dashboard may serve `--database` elsewhere, and `sd-db item remove` opens the
        default store; the same id there is another row, so Delete refuses and runs nothing."""
        self.stub("sd-db", 'echo "sd-db: fingerprint: 0a1b2c"')
        other = Path(self.tmp.name).resolve() / "other" / "workflow.sqlite3"
        with patch("sd_db.database.default_path", return_value=other):
            self.refused([({"action": "watch.delete", "item": self.ids["paused"]}, 400, "another database")])

    def test_delete_previews_then_applies_the_fingerprint_it_printed(self):
        """sd:6703: Delete runs `sd-db item remove`, whose apply needs the fingerprint its preview printed."""
        self.same_store()
        self.stub("sd-db", 'case "$*" in *--apply*) echo "sd-db: removed 1 item";; *) echo "sd-db: fingerprint: 0a1b2c";; esac')
        status, body = self.run_("watch.delete", item=self.ids["paused"])
        self.assertEqual((status, body["exit"], body["label"]), (200, 0, "Delete watch"), body)
        reason = "--reason Deleted on the dashboard: sdw.market Old Co"
        self.assertEqual(self.ran(), [f"sd-db item remove {self.ids['paused']} --who dashboard {reason}",
                                      f"sd-db item remove {self.ids['paused']} --who dashboard {reason} --apply --if-fingerprint 0a1b2c"])
        self.assertIn("sd-db: removed 1 item", body["output"])

    def test_a_refused_delete_preview_applies_nothing(self):
        self.same_store()
        self.stub("sd-db", 'echo "sd-db:   I7 note 9: note 9 is an unresolved question"; echo "sd-db: fingerprint: 0a1b2c"; exit 3')
        status, body = self.run_("watch.delete", item=self.ids["paused"])
        self.assertEqual((status, body["exit"]), (200, 3))
        self.assertEqual(len(self.ran()), 1)
        self.assertIn("unresolved question", body["output"])

    def test_add_refuses_what_is_not_a_web_url_or_a_name(self):
        self.refused([({"action": "watch.add", "url": "javascript:alert(1)", "name": ""}, 400, "http or https URL"),
                      ({"action": "watch.add", "url": "https://example.test/a b", "name": ""}, 400, "no spaces"),
                      ({"action": "watch.add", "url": "  ", "name": ""}, 400, "Name the URL"),
                      ({"action": "watch.add", "url": "https://example.test", "name": "--force"}, 400, "starts with a letter"),
                      ({"action": "watch.add", "url": "https://example.test", "name": "()"}, 400, "Name the watch"),
                      ({"action": "watch.add", "url": "https://example.test"}, 400, "takes name, url")])


class TheScript(ScreenCase):
    """watches.js against the document `watches_screen` builds from the fixture."""

    def setUp(self):
        super().setUp()
        self.ids = seed(self.connection)
        jobs = HoaJobs(self.enterContext(tempfile.TemporaryDirectory()), {"market-watch": ("failed", 1)})
        (jobs.cron_root / "logs" / ".market-watch.stamp").write_text("started=2026-09-06T03:00:00Z\nended=2026-09-06T03:02:00Z\nexit=1\n")
        jobs.log("market-watch", "Gmail tools unavailable: token expired\n")
        self.doc = watches_screen.document(self.connection, now=NOW, jobs=jobs)

    def run_page(self, body, answer=None, search=""):
        answer = answer or ("(p) => p === '/api/watches' ? [200, " + json.dumps(self.doc) + "]"
                            " : p === '/api/actions/run' ? [200, { action: 'x', label: 'Ran', line: 'l', exit: 0, output: 'ok', cut: false }]"
                            " : [200, { request: { status: 'accepted' } }]")
        script = (STAND_IN + f"location.search = {json.dumps(search)}; location.origin = 'http://dash.example.test';\nvar ROW = null;\n"
                  + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + "\nwindow.shell.row = () => ROW; OUT.opened = []; window.open = (u, t, f) => { OUT.opened.push([u, t, f]); };\n"
                  + READ_SHELL + f"\nANSWER = {answer};\n" + COMMANDS_JS + WATCHES_JS
                  + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_a_malformed_source_url_shows_as_text_and_no_link(self):
        """sd:6280: the row and Details draw a URL the server could not parse as text, never as an href."""
        row = dict(self.doc["rows"][0], id=999, title="Broken Co", url="", host="", url_text="https://[broken")
        self.doc["rows"].insert(0, row)
        out = self.run_page("R.rows = ELS.rows.html;")
        self.assertIn('<td class="mod">https://[broken</td>', out["R"]["rows"])
        self.assertNotIn('href="https://[broken', out["R"]["rows"])

    def test_each_watch_shows_its_status_and_last_refresh_and_a_stale_one_says_so(self):
        out = self.run_page("R.rows = ELS.rows.html; R.job = ELS.job.html; R.sub = ELS.sub.html; R.tally = ELS.tally.html; R.details = ELS.details.html;"
                            " R.filters = ELS.filters.html; R.head = ELS['rows-head'].html;")
        r = out["R"]
        self.assertEqual(out["gets"], ["/api/watches"])
        self.assertEqual(r["tally"], "5 watches")
        self.assertEqual(r["sub"], "5 watches · 3 active · 2 stale or never refreshed")
        rows = r["rows"]
        self.assertIn('<td class="num">2026-08-01 · 36 d ago <span class="g-caution">stale</span></td>', rows)
        self.assertIn('<td class="num">2026-09-01 · 5 d ago</td>', rows)
        self.assertIn('<td class="num">— never</td>', rows)
        self.assertIn('<td class="g g-caution" title="no profile yet">', rows)
        self.assertIn('<td class="g g-queued" title="not refreshed">', rows)
        self.assertIn('<td class="kind">candidate</td>', rows)
        self.assertIn('<a class="ext" href="https://logs.example.test/about" target="_blank" rel="noopener noreferrer">logs.example.test</a>', rows)
        # The oldest refresh first, a watch with no date before any: the first row is the one that needs a refresh.
        self.assertLess(rows.index("New Agent"), rows.index("Widget Ops"))
        # Active watches lead: a candidate with no date is not refreshed, so it follows them.
        self.assertLess(rows.index("Example Logs"), rows.index("Maybe Co"))
        self.assertIn("<h2>New Agent</h2>", r["details"])
        self.assertIn('data-f="stage" data-v="active"', r["filters"])
        self.assertIn("Last refresh", r["head"])
        # The job's failed run says why.
        self.assertIn("The last run failed", r["job"])
        self.assertIn("exit 1: Gmail tools unavailable: token expired", r["job"])

    def test_a_stage_in_the_address_filters_the_list(self):
        """sd:8013: Today's Market Watch link opens /watches?stage=candidate on the candidates."""
        r = self.run_page("R.rows = ELS.rows.html; R.filters = ELS.filters.html;", search="?stage=candidate")["R"]
        self.assertIn("Maybe Co", r["rows"])
        self.assertNotIn("New Agent", r["rows"])
        self.assertIn('data-v="candidate" aria-pressed="true"', r["filters"])

    def test_the_candidates_have_a_bulk_decline_by_age_that_asks_and_posts(self):
        """sd:8014: a market row has no score, so the candidates' Decline is by age alone; it asks first."""
        answer = ("(p, b) => p === '/api/watches' ? [200, " + json.dumps(self.doc) + "]"
                  " : p === '/api/triage/decline' ? [200, { declined: b.items.map(i => i.item), refused: [], by: 'x' }] : [404, {}]")
        out = self.run_page("R.filters = ELS.filters.html; shellRun(REG.find(c => c.id === 'queue.decline'), C.get('queue:market')); await flush();",
                            answer=answer)
        self.assertIn('data-cmd="queue.decline" data-obj="queue:market">Decline 1 older than 30 days</button>', out["R"]["filters"])
        self.assertEqual(out["confirms"], ["queue.decline"])
        candidate = self.doc["rows"][[r["id"] for r in self.doc["rows"]].index(self.ids["candidate"])]
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/triage/decline", {"kind": "market", "score": 6, "days": 30, "items": [
            {"item": self.ids["candidate"], "revision": candidate["revision"]}]}]])

    def test_a_declined_watch_says_when_and_by_which_control(self):
        """sd:8014: the Declined filter lists each declined row with when and by what."""
        self.doc["rows"].append(dict(self.doc["rows"][0], id=990, title="Dropped Co", stage="declined", fresh="idle"))
        self.doc["declined"] = [{"item": 990, "title": "Dropped Co", "at": "2026-09-02T10:00:00Z",
                                 "by": "bulk Decline (older than 30 days)"}]
        r = self.run_page("R.rows = ELS.rows.html;", search="?stage=declined")["R"]
        self.assertIn('declined <span class="why">2026-09-02 by bulk Decline (older than 30 days)</span>', r["rows"])

    def test_clear_filters_in_the_empty_list_clears_the_search_too(self):
        """sd:6285: the empty state's Clear filters cleared the status only, so a search that matched nothing stayed."""
        out = self.run_page("""document.dispatchEvent(new CustomEvent('shell:filter', { detail: { q: 'nothing-matches-this' } })); await flush();
R.before = ELS.rows.html; R.hidden = ELS.empty.hidden;
ELS.empty.listeners.click[0]({ target: { closest: s => s === '[data-fclear]' ? {} : null } }); await flush();
R.after = ELS.rows.html; R.shift = OUT.shift;""")
        r = out["R"]
        self.assertNotIn("New Agent", r["before"])
        self.assertFalse(r["hidden"])
        self.assertIn("New Agent", r["after"])
        self.assertEqual(r["shift"], "", "the bar's text was not cleared")

    def test_refresh_all_runs_the_job_with_the_revision_the_page_read(self):
        out = self.run_page("const j = OBJ.get('job:market-watch'); R.label = cmd('jobs.retry').label; C.run(cmd('jobs.retry'), j); await flush();")
        self.assertEqual(out["R"]["label"], "Refresh watches")
        self.assertIn(["/api/jobs/market-watch/retry", {"revision": self.doc["job"]["revision"]}, 64], out["posts"])
        self.assertIn(["Refresh watches started · market-watch refreshes the next three active watches", False], out["toasts"])
        self.assertEqual(out["gets"], ["/api/watches", "/api/watches"], "the job's line reads again")

    def test_refresh_runs_one_active_watch_and_is_off_on_any_other(self):
        out = self.run_page(f"""const stale = OBJ.get('watch:{self.ids["stale"]}'), maybe = OBJ.get('watch:{self.ids["candidate"]}');
C.run(cmd('watch.refresh'), stale); await flush(); C.run(cmd('watch.refresh'), maybe); await flush();""")
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/actions/run", {"action": "watch.refresh", "item": self.ids["stale"]}]])
        self.assertIn("watch.refresh", out["confirms"])
        self.assertIn(["off: candidate: market-watch refreshes active watches only", False], out["toasts"])

    def test_each_stage_move_shows_only_on_the_stage_it_moves_from(self):
        """sd:6694: Activate on a candidate, Pause on an active watch, Unpause on a paused one; a declined one gets none."""
        declined = market(self.connection, "Gone Co", "declined")
        self.doc["rows"].append(dict(self.doc["rows"][0], id=declined, title="Gone Co", stage="declined"))
        ids = dict(self.ids, declined=declined)
        out = self.run_page(f"""const ids = {json.dumps(ids)}, MOVES = ['watch.activate', 'watch.pause', 'watch.unpause'];
R.on = {{}}; for (const k of ['candidate', 'stale', 'paused', 'declined']) {{ const o = OBJ.get('watch:' + ids[k]);
  R.on[k] = MOVES.filter(m => cmd(m).when(o) === true && cmd(m).primary(o)); }}
R.why = cmd('watch.pause').when(OBJ.get('watch:' + ids.candidate));
R.labels = MOVES.map(m => [cmd(m).label, cmd(m).risk, cmd(m).key]);""")
        r = out["R"]
        self.assertEqual(r["on"], {"candidate": ["watch.activate"], "stale": ["watch.pause"], "paused": ["watch.unpause"], "declined": []})
        self.assertEqual(r["why"], "candidate: Pause moves an active watch")
        self.assertEqual(r["labels"], [["Activate", "undo", "a"], ["Pause", "undo", "p"], ["Unpause", "undo", "p"]])

    def test_activate_moves_the_stage_with_the_rows_revision_and_undo_moves_it_back(self):
        candidate = self.ids["candidate"]
        row = next(r for r in self.doc["rows"] if r["id"] == candidate)
        answer = ("(p, b) => p === '/api/watches' ? [200, " + json.dumps(self.doc) + "]"
                  " : p.endsWith('/stage') ? [200, { item: { id: " + str(candidate) + " }, revision: b.stage === 'active' ? 'r2' : 'r3', mirror: { ok: true } }]"
                  " : [404, { error: 'no answer' }]")
        out = self.run_page(f"""const o = OBJ.get('watch:{candidate}'), c = cmd('watch.activate');
const v = await c.run(o); R.text = v.text; await c.undo(o, v); await flush();""", answer)
        self.assertEqual([p[:2] for p in out["posts"]], [[f"/api/items/{candidate}/stage", {"revision": row["revision"], "stage": "active"}],
                                                        [f"/api/items/{candidate}/stage", {"revision": "r2", "stage": "candidate"}]])
        self.assertEqual(out["R"]["text"], "Maybe Co is active · market-watch refreshes it from its next run")
        self.assertEqual(out["gets"], ["/api/watches", "/api/watches", "/api/watches"], "each move reads the watches again")

    def test_a_stage_move_whose_mirror_failed_says_so(self):
        """sd:6694 review: the stage route can land and still leave the vault mirror stale; the toast names it, as Tasks does."""
        candidate = self.ids["candidate"]
        answer = ("(p, b) => p === '/api/watches' ? [200, " + json.dumps(self.doc) + "]"
                  " : p.endsWith('/stage') ? [200, { item: { id: " + str(candidate) + " }, revision: 'r2', mirror: { ok: false, reason: 'disk full' } }]"
                  " : [404, { error: 'no answer' }]")
        out = self.run_page(f"""R.text = (await cmd('watch.activate').run(OBJ.get('watch:{candidate}'))).text;""", answer)
        self.assertEqual(out["R"]["text"], "Maybe Co is active · market-watch refreshes it from its next run · mirror not updated: disk full")

    def test_add_watch_posts_the_url_and_name_typed_and_reads_again(self):
        out = self.run_page("""FIELD_VALUES = { url: 'https://github.com/example/widget-agent', name: '' };
C.run(cmd('watch.add'), OBJ.get('watches')); await flush();
FIELD_VALUES = { url: '', name: 'x' }; C.run(cmd('watch.add'), OBJ.get('watches')); await flush();""")
        self.assertEqual([p[:2] for p in out["posts"]],
                         [["/api/actions/run", {"action": "watch.add", "url": "https://github.com/example/widget-agent", "name": ""}]])
        self.assertEqual(out["gets"], ["/api/watches", "/api/watches"])
        self.assertIn(["not run: OK is off until URL is typed", False], out["toasts"])

    def test_remove_and_delete_ask_first_and_a_declined_watch_offers_no_remove(self):
        """sd:6703: Remove declines a watch and is off on a declined one; Delete asks first and names what is lost."""
        self.doc["rows"].append(dict(self.doc["rows"][0], id=4242, title="Gone Co", stage="declined"))
        out = self.run_page(f"""const maybe = OBJ.get('watch:{self.ids["candidate"]}'), gone = OBJ.get('watch:4242');
R.off = cmd('watch.remove').when(gone); R.consequence = cmd('watch.delete').consequence(maybe);
C.run(cmd('watch.remove'), gone); await flush(); C.run(cmd('watch.remove'), maybe); await flush(); C.run(cmd('watch.delete'), gone); await flush();""")
        self.assertEqual(out["R"]["off"], "declined already")
        self.assertIn("for good: its profile, its Sources and its notes are lost", out["R"]["consequence"])
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/actions/run", {"action": "watch.remove", "item": self.ids["candidate"]}],
                                                         ["/api/actions/run", {"action": "watch.delete", "item": 4242}]])
        self.assertEqual(out["confirms"], ["watch.remove", "watch.delete"])
        self.assertEqual(out["gets"], ["/api/watches", "/api/watches", "/api/watches"], "each write reads the page again")

    def test_a_declined_watch_offers_restore_and_an_open_one_does_not(self):
        """sd:6703: Restore shows on a declined watch only, asks first, and posts watch.restore."""
        self.doc["rows"].append(dict(self.doc["rows"][0], id=4242, title="Gone Co", stage="declined"))
        out = self.run_page(f"""const maybe = OBJ.get('watch:{self.ids["candidate"]}'), gone = OBJ.get('watch:4242');
R.off = cmd('watch.restore').when(maybe); R.on = cmd('watch.restore').when(gone);
C.run(cmd('watch.restore'), maybe); await flush(); C.run(cmd('watch.restore'), gone); await flush();""")
        self.assertEqual((out["R"]["off"], out["R"]["on"]), ("candidate: Restore brings back a declined watch", True))
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/actions/run", {"action": "watch.restore", "item": 4242}]])
        self.assertEqual(out["confirms"], ["watch.restore"])

    def test_a_failed_read_says_so(self):
        out = self.run_page("R.details = ELS.details.html; R.sub = ELS.sub.html;",
                            answer="() => [500, { error: 'no such table: item' }]")
        self.assertIn({"kind": "error", "text": "The watches were not read: no such table: item. Reload retries it.", "source": "/api/watches"}, out["states"])
        self.assertEqual(out["R"]["sub"], "The watches were not read")


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "watches", "Watches", "/watches", ("/api/watches",)


if __name__ == "__main__":
    unittest.main()
