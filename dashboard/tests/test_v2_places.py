"""The HOA and Home sub-views (Tasks, Briefs, Reports) and Tasks' realm sub tabs (sd:6263).

What this slice promises: one rule places a row in an area (`places.area_of`): an item of the hoa checkout is HOA, an
item with no repo is Home, and a report is in neither, since an area's Reports view holds it. The place pages' Tasks
views (HOA's from `hoa_screen`'s items) and the Tasks rows' `area` all come from that rule, so they hold the same rows.
Briefs are the area's when their repo is the area's checkout or their series names a job of the area's report family;
Reports are the reports of the family's jobs. The family list is `report-families.conf`; a missing family is unknown
with the file it was looked for in, never an empty list.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_v2_tasks
from sd_dashboard import (
    hoa_screen,
    places,
    reports_screen,
    snooze,
    tasks_screen,
    today_reports,
    v2,
)
from sd_db import (
    reporting,
    repos,
    routine_briefs,
    upsert_repo,
    upsert_shadow,
    workflow,
    writes,
)
from sd_db import votes as sd_votes
from support import NOW, ScreenCase, before_5648
from test_now_screen import JobsBackend
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

STATIC = Path(v2.__file__).resolve().parent / "static"
PLACES_JS = (STATIC / "places.js").read_text(encoding="utf-8")
MARKUP_JS = (STATIC / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (STATIC / "commands.js").read_text(encoding="utf-8")
BRIEF_DETAILS_JS = (STATIC / "brief-details.js").read_text(encoding="utf-8")
REPORT_DETAILS_JS = (STATIC / "report-details.js").read_text(encoding="utf-8")
TASK_DETAILS_JS = (STATIC / "task-details.js").read_text(encoding="utf-8")

FAMILIES = ("family|hoa|HOA|house|hoa-mission-alarms,hoa-morning-digest\n"
            "family|home|Home|house|ha-backups-prune,fun-events-nightly\n")


def brief(case, stem, repo):
    return routine_briefs.add(case.connection, stem, "---\nseries: x\n---\n\nA lead.\n", repo=repo, who="test",
                              at="2026-09-05T12:00:00Z")["item"]


def report(case, job, run, attention=False):
    return reporting.ingest(case.connection, job=job, run_id=run, started="2026-09-05T08:00:00Z",
                            ended="2026-09-05T08:01:00Z", exit_code=1 if attention else 0, text="ran\n",
                            source_path="sd-db scan", attention=attention,
                            attention_basis="job exited 1" if attention else "")["item"]["id"]


class Fixture(ScreenCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.config = self.dir / "report-families.conf"
        self.config.write_text(FAMILIES, encoding="utf-8")
        self.hoa = self.repo("/repos/hoa")
        self.other = self.repo("/repos/system")
        self.hoa_item = self.item("Fix the valve log", kind="task", repo=self.hoa)
        self.home_item = self.item("Clean the gutters", kind="task")
        self.other_item = self.item("Tune the gate", kind="task", repo=self.other)
        self.item("A settled HOA item", kind="task", repo=self.hoa, status="done")
        # A report no repo owns is not a Home issue: the family's Reports view holds it.
        self.alarm = report(self, "hoa-mission-alarms", "r1", attention=True)
        self.fun = report(self, "fun-events-nightly", "r2")
        self.stray = report(self, "weekly-scan", "r3")
        self.hoa_brief = brief(self, "2026-09-05 - Board Notes", self.hoa)
        self.digest = brief(self, "2026-09-05 - HOA Morning Digest", self.other)
        self.fun_brief = brief(self, "2026-09-05 - Fun Events Nightly", self.other)
        self.system_brief = brief(self, "2026-09-05 - Daily Intel Brief", self.other)
        # The operations backend a Reports view reads its jobs through (sd:5636); weekly-scan is in no place's family.
        self.jobs = JobsBackend(self.dir, jobs=[("hoa-mission-alarms", "failed", 1, None), ("fun-events-nightly", "idle", 0, None),
                                                ("weekly-scan", "idle", 0, None)])

    def ids(self, doc):
        return sorted(row["item"] if "item" in row else row["id"] for row in doc["rows"])


class Tasks(Fixture):
    def test_hoa_tasks_are_the_open_items_of_the_hoa_checkout(self):
        doc = places.tasks(self.connection, "hoa", now=NOW)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", [self.hoa_item]))

    def test_home_tasks_are_the_open_items_with_no_repo_and_no_report(self):
        doc = places.tasks(self.connection, "home", now=NOW)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", [self.home_item]))

    def test_without_a_registered_hoa_checkout_hoa_tasks_are_unknown_with_the_reason(self):
        bare = ScreenCase()
        bare.setUp()
        self.addCleanup(bare.doCleanups)
        bare.repo("/repos/system")
        doc = places.tasks(bare.connection, "hoa", now=NOW)
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("no repo named hoa", doc["reason"])

    def test_tasks_hoa_and_home_views_hold_the_place_pages_tasks_rows(self):
        rows = tasks_screen.document(self.connection, now=NOW)["rows"]
        for area in places.AREAS:
            tasks = sorted(r["id"] for r in rows if r["area"] == area and r["status"] != "done" and not r["parked"])
            self.assertEqual(tasks, self.ids(places.tasks(self.connection, area, now=NOW)), area)
        # The HOA page draws its Tasks view from /api/hoa's items: the same rows, in urgency order.
        hoa = sorted(r["id"] for r in hoa_screen.document(self.connection, now=NOW)["items"]["rows"])
        self.assertEqual(hoa, self.ids(places.tasks(self.connection, "hoa", now=NOW)))
        self.assertEqual({r["id"]: r["area"] for r in rows if r["id"] == self.other_item}, {self.other_item: None})

    def tag(self, item, area):
        workflow.edit_item(self.connection, item, {"area": area}, who="test")

    def test_an_items_own_area_puts_it_on_that_place_alone_whatever_its_repo(self):
        """pack sd:8010: a vault task tagged home is Home's, not its realm's; one tagged hoa is HOA's, on the HOA page
        too; an hoa checkout task tagged home leaves HOA; untagged rows keep the repo rule."""
        vault = self.repo("/repos/vault")
        upkeep = self.item("Flush the water heater", kind="task", repo=vault)
        gate = self.item("Paint the gate", kind="task", repo=vault)
        stays = self.item("Plan the trip", kind="task", repo=vault)
        self.tag(upkeep, "home")
        self.tag(gate, "hoa")
        self.tag(self.hoa_item, "home")
        self.assertEqual(self.ids(places.tasks(self.connection, "home", now=NOW)),
                         sorted([self.home_item, upkeep, self.hoa_item]))
        self.assertEqual(self.ids(places.tasks(self.connection, "hoa", now=NOW)), [gate])
        hoa = sorted(r["id"] for r in hoa_screen.document(self.connection, now=NOW)["items"]["rows"])
        self.assertEqual(hoa, [gate])
        rows = {r["id"]: r for r in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertEqual([(rows[i]["area"], rows[i]["area_set"]) for i in (upkeep, gate, self.hoa_item, stays, self.home_item)],
                         [("home", True), ("hoa", True), ("home", True), (None, False), ("home", False)])
        realms = self.dir / "realms.conf"
        realms.write_text("realm|acme|Acme|/repos/*\n", encoding="utf-8")
        with mock.patch.object(places, "REALMS_CONFIG", realms):
            self.assertEqual(self.ids(places.tasks(self.connection, "acme", now=NOW)), sorted([stays, self.other_item]))

    def test_an_item_tagged_personal_or_a_realm_sits_in_that_place_alone(self):
        """pack sd:8062: an item's own area may be personal or a dashboard.realms id: a Work repo's task tagged
        personal leaves Work; a task with no repo tagged acme leaves Home for Acme."""
        writes.set_setting(self.connection, "dashboard.realms", "realm|acme|Acme|/repos/sys*\n")
        errand = self.item("Buy stamps", kind="task")
        self.tag(self.other_item, "personal")
        self.tag(errand, "acme")
        self.assertEqual(self.ids(places.tasks(self.connection, "acme", now=NOW)), [errand])
        self.assertEqual(self.ids(places.tasks(self.connection, "home", now=NOW)), [self.home_item])
        rows = {r["id"]: r for r in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertEqual([(rows[i]["area"], rows[i]["area_set"]) for i in (self.other_item, errand, self.home_item)],
                         [("personal", True), ("acme", True), ("home", False)])

    def test_a_places_tasks_rows_are_the_tasks_pages_rows(self):
        # sd:5635: a Tasks row's Details on Home, HOA and a realm place are the Tasks page's own, so the view lists the
        # /api/tasks rows (tasks_screen._rows), with the revision, allowed moves, edit capability and issue targets.
        # Its `lead` is /api/tasks' too (pack sd:5648), so Details show the Lead field where Tasks does.
        page = tasks_screen.document(self.connection, now=NOW)
        listed = {r["id"]: r for r in page["rows"]}
        for area in places.AREAS:
            doc = places.tasks(self.connection, area, now=NOW)
            self.assertTrue(doc["rows"], area)
            self.assertEqual(doc["rows"], [listed[r["id"]] for r in doc["rows"]], area)
            self.assertEqual(doc["lead"], page["lead"], area)

    def test_a_scheduled_occurrence_is_no_place_task_until_it_opens(self):
        """sd:5670: as Tasks does, Home and HOA Tasks hide a scheduled occurrence (pack sd:5648) until the day it opens.
        A pack before sd:5648 marks nothing scheduled, so the row shows as before."""
        self.schedule(self.home_item, "2026-09-09")
        self.assertEqual(self.ids(places.tasks(self.connection, "home", now=NOW)), [])
        self.assertEqual(self.ids(places.tasks(self.connection, "home", now="2026-09-09T12:00:00Z")), [self.home_item])
        with before_5648():
            self.assertEqual(self.ids(places.tasks(self.connection, "home", now=NOW)), [self.home_item])


    def test_a_places_done_rows_are_the_weeks_done_items_kept_apart_from_its_open_rows(self):
        """sd:5752: a place's Tasks All view lists the week's done items, as Tasks does (`backlog_items`' bound); `rows`
        stays the open ones, which every count reads. HOA's are /api/hoa's `items.done`, each `ok`."""
        swept = self.item("Swept the porch", kind="task", status="done")
        old = self.item("Painted the fence", kind="task", status="done")
        self.age(old, 30)
        home = places.tasks(self.connection, "home", now=NOW)
        self.assertEqual((self.ids(home), [r["id"] for r in home["done"]]), ([self.home_item], [swept]))
        hoa = places.tasks(self.connection, "hoa", now=NOW)
        settled = [r["id"] for r in hoa["done"]]
        self.assertEqual(len(settled), 1)
        items = hoa_screen.document(self.connection, now=NOW)["items"]
        self.assertEqual(([r["id"] for r in items["rows"]], [(r["id"], r["state"]) for r in items["done"]]),
                         ([self.hoa_item], [(settled[0], "ok")]))
        listed = {r["id"]: r for r in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertEqual(home["done"], [listed[swept]], "a done row is the Tasks page's row too")


class Realms(Fixture):
    """sd:5064: `places.realm_of` puts a Tasks row in Personal or a realm of the dashboard.realms list."""

    RULES = [{"id": "work", "label": "Work", "globs": ["/repos/example-work/*", "/repos/system"]}]

    def conf(self, text):
        path = self.dir / "realms.conf"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_repo_a_glob_matches_is_that_realm_and_any_other_repo_personal(self):
        self.assertEqual(places.realm_of("/repos/example-work/api", "task", self.RULES), "work")
        self.assertEqual(places.realm_of("/repos/system", "task", self.RULES), "work")
        self.assertEqual(places.realm_of("/repos/hoa", "task", self.RULES), "personal")
        self.assertEqual(places.realm_of("/repos/system-two", "task", self.RULES), "personal")

    def test_the_hoa_checkout_is_hoa_before_any_glob_and_without_a_realm_list(self):
        """pack sd:7687: the hoa checkout (`hoa`, `area_of`'s) is HOA, not Personal, so its rows leave Personal for
        the HOA tab; before a glob that claims it, without a realm list, and with ~ in either path."""
        claims = [{"id": "work", "label": "Work", "globs": ["/repos/*"]}]
        for rules in (self.RULES, claims, [], None):
            with self.subTest(rules=rules):
                self.assertEqual(places.realm_of("/repos/hoa", "task", rules, "/repos/hoa"), "hoa")
        self.assertEqual(places.realm_of("~/repos/hoa", "task", None, str(Path.home() / "repos/hoa")), "hoa")
        self.assertEqual(places.realm_of(str(Path.home() / "repos/hoa"), "lane", None, "~/repos/hoa"), "hoa")
        self.assertEqual(places.realm_of("/repos/system", "task", claims, "/repos/hoa"), "work")
        self.assertEqual(places.realm_of(None, "task", claims, "/repos/hoa"), "personal")
        self.assertIsNone(places.realm_of("/repos/hoa", "report", claims, "/repos/hoa"))

    def test_a_repo_stored_with_a_tilde_matches_a_tilde_glob(self):
        # The database keeps a repo as ~/repos/...; realms() expands a glob's ~, so the repo's ~ must expand too.
        rules = places.realms(self.connection, self.conf("realm|work|Work|~/repos/example-work/*\n"))["list"]
        self.assertEqual(places.realm_of("~/repos/example-work/api", "task", rules), "work")
        self.assertEqual(places.realm_of(str(Path.home() / "repos/example-work/api"), "task", rules), "work")

    def test_no_repo_is_personal_and_a_report_is_in_no_realm(self):
        self.assertEqual(places.realm_of(None, "task", self.RULES), "personal")
        self.assertEqual(places.realm_of(None, "task", None), "personal")
        self.assertIsNone(places.realm_of(None, "report", self.RULES))
        self.assertIsNone(places.realm_of("/repos/system", "report", self.RULES))

    def test_without_realms_conf_a_repo_row_is_unknown_not_personal(self):
        self.assertIsNone(places.realm_of("/repos/hoa", "task", None))
        doc = places.realms(self.connection, self.dir / "realms.conf")
        self.assertEqual((doc["state"], doc["list"], doc["source"]),
                         ("missing", [], "<config>/project-dashboard/realms.conf"))

    def test_the_file_reads_in_order_and_one_id_on_two_lines_gets_both_globs(self):
        doc = places.realms(self.connection, self.conf("# comment\nrealm|work|Work|/repos/example-work/*\nrealm|brand|Brand|~/brand\n"
                                      "realm|work|Work|/repos/system\n"))
        self.assertEqual(doc["state"], "read")
        self.assertEqual([(r["id"], r["label"]) for r in doc["list"]], [("work", "Work"), ("brand", "Brand")])
        self.assertEqual(doc["list"][0]["globs"], ["/repos/example-work/*", "/repos/system"])
        self.assertEqual(doc["list"][1]["globs"], [str(Path.home() / "brand")])

    def test_a_bad_line_or_a_reserved_id_is_skipped_and_named(self):
        doc = places.realms(self.connection, self.conf("realm|work|Work\nrealm|all|Everything|/repos/*\nrealm|Work|W|/x\n"
                                      "realm|lab|Lab|/repos/lab\n"))
        self.assertEqual(doc["state"], "partial")
        self.assertEqual([r["id"] for r in doc["list"]], ["lab"])
        self.assertEqual([p.split(":")[0] for p in doc["problems"]], ["line 1", "line 2", "line 3"])

    def test_an_unreadable_file_is_an_error_not_missing(self):
        path = self.conf("")
        path.write_bytes(b"\xff\xfe")
        self.assertEqual(places.realms(self.connection, path)["state"], "error")

    def test_each_tasks_row_carries_its_realm_and_the_document_the_files_state(self):
        with mock.patch.object(places, "REALMS_CONFIG", self.conf("realm|work|Work|/repos/sys*\n")), \
                mock.patch.object(reports_screen, "FAMILIES_CONFIG", self.dir / "absent.conf"):
            doc = tasks_screen.document(self.connection, now=NOW)
        realm = {r["id"]: r["realm"] for r in doc["rows"]}
        self.assertEqual((realm[self.hoa_item], realm[self.home_item], realm[self.other_item]),
                         ("hoa", "personal", "work"))
        # pack sd:7774: a report no rule places is Personal (`places.report_realm`), as a repo no line claims.
        self.assertEqual({r["realm"] for r in doc["rows"] if r["kind"] == "report"}, {"personal"})
        self.assertEqual(doc["realms"], {"state": "read", "source": "<config>/project-dashboard/realms.conf",
                                         "problems": []})
        with mock.patch.object(places, "REALMS_CONFIG", self.dir / "absent.conf"):
            doc = tasks_screen.document(self.connection, now=NOW)
        realm = {r["id"]: r["realm"] for r in doc["rows"]}
        self.assertEqual((realm[self.hoa_item], realm[self.home_item], realm[self.other_item]), ("hoa", "personal", None))
        self.assertEqual(doc["realms"]["state"], "missing")

    def pull_report(self, url, run):
        return reporting.ingest(self.connection, job="pr-attention", run_id=run, started="2026-09-05T08:00:00Z",
                                ended="2026-09-05T08:00:00Z", exit_code=0, text=f"example\n{url}\n",
                                source_path="sd shadow sync", attention=True,
                                attention_basis=f"review_requested {url}")["item"]["id"]

    def report_realms(self, families=FAMILIES):
        (self.dir / "families.conf").write_text(families, encoding="utf-8")
        with mock.patch.object(places, "REALMS_CONFIG", self.conf("realm|work|Work|/repos/example-work/*\n")), \
                mock.patch.object(reports_screen, "FAMILIES_CONFIG", self.dir / "families.conf"):
            return {r["id"]: r["realm"] for r in tasks_screen.document(self.connection, now=NOW)["rows"]}

    def test_a_pr_attention_report_takes_the_realm_of_its_pull_repos_checkout(self):
        """pack sd:7774: a pr-attention report sat on Personal, as every report had no realm. Its realm is that of the
        registered checkout whose remote is the pull's repo, by realm_of's globs; a pull in no checkout stays Personal."""
        upsert_repo(self.connection, "/repos/example-work/bench", remote="git@github.com:Example/Bench.git")
        work = self.pull_report("https://github.com/example/bench/pull/617", "r9")
        elsewhere = self.pull_report("https://github.com/example/unknown/pull/3", "r10")
        realm = self.report_realms()
        self.assertEqual(realm[work], "work")
        self.assertEqual(realm[elsewhere], "personal")

    def test_another_report_takes_its_familys_key_when_that_key_is_a_realm(self):
        """pack sd:7774: a report whose job is in a family keyed by a realm id is that realm's; the hoa and home families
        are no realm, so their reports, and a job in no family, stay Personal."""
        scan, other = report(self, "weekly-scan", "r4", attention=True), report(self, "disk-check", "r5", attention=True)
        realm = self.report_realms(FAMILIES + "family|work|Work|bot|weekly-scan\n")
        self.assertEqual((realm[scan], realm[self.alarm], realm[other]), ("work", "personal", "personal"))
        with mock.patch.object(places, "REALMS_CONFIG", self.dir / "absent.conf"):
            unread = {r["id"]: r["realm"] for r in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertIsNone(unread[scan], "without the realm list a report's realm is unknown, as a repo row's")

    def test_a_report_realm_is_unknown_when_its_source_did_not_read_and_hoa_without_a_realm_list(self):
        """pack sd:7792: a family list that did not read made a report Personal, not unknown; and a missing realm list
        returned None before the pull's checkout was looked up, so a pr-attention report on the hoa repo lost HOA."""
        rules, hoa = [{"id": "work", "label": "Work", "globs": ["/repos/example-work/*"]}], "/repos/hoa"
        basis = "review_requested https://github.com/example/hoa/pull/4"
        self.assertIsNone(places.report_realm("weekly-scan", "", rules, hoa, checkouts={}, families=None))
        self.assertEqual(places.report_realm("weekly-scan", "", rules, hoa, checkouts={}, families=[]), "personal")
        self.assertEqual(places.report_realm("pr-attention", basis, None, hoa, checkouts={"example/hoa": hoa},
                                             families=[]), "hoa")
        self.assertIsNone(places.report_realm("pr-attention", basis, None, hoa,
                                              checkouts={"example/hoa": "/repos/example-work/hoa"}, families=None))
        # A pull in no registered checkout has no repo: Personal, as realm_of's row with no repo.
        self.assertEqual(places.report_realm("pr-attention", basis, None, hoa, checkouts={}, families=None), "personal")

    def test_a_tasks_report_reads_its_current_basis_and_fields_that_do_not_read_are_unknown(self):
        """pack sd:7792: `_job_basis` let RecursionError and TypeError escape, so one report's fields failed the whole
        Tasks read, and it read the first run's basis, not `repeats.basis`, which a later run's pull replaced."""
        first, later = ("review_requested https://github.com/example/a/pull/1",
                        "review_requested https://github.com/example/b/pull/2")
        report_fields = {"report": {"job": "pr-attention", "attention_basis": first}}
        cases = [({**report_fields, "repeats": {"count": 2, "basis": later}}, ("pr-attention", later)),
                 ({**report_fields, "repeats": {"count": 2}}, ("pr-attention", first)),
                 ({**report_fields, "repeats": None}, ("pr-attention", first))]
        for fields, want in cases:
            self.assertEqual(tasks_screen._job_basis({"fields": json.dumps(fields)}), want, fields)
        for raw in ("[" * 100000 + "]" * 100000, 7, "[1]", '{"report": 3}', "{not json"):
            self.assertEqual(tasks_screen._job_basis({"fields": raw}), (None, None), str(raw)[:20])

    def test_the_pull_url_helper_reads_a_pr_attention_basis_only(self):
        url = "https://github.com/example/bench/pull/617"
        self.assertEqual(places.pull_url("pr-attention", f"changes_requested {url}"), url)
        self.assertIsNone(places.pull_url("weekly-scan", f"changes_requested {url}"))
        for basis in ("", None, "review_requested https://example.test/x/pull/1", "checks_failing github.com/a/b/pull/2"):
            self.assertIsNone(places.pull_url("pr-attention", basis), basis)


class Briefs(Fixture):
    def test_hoa_briefs_are_the_checkouts_and_those_whose_series_names_an_hoa_job(self):
        doc = places.briefs(self.connection, "hoa", now=NOW, config=self.config)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", sorted([self.hoa_brief, self.digest])))

    def test_home_briefs_are_those_whose_series_names_a_home_job(self):
        doc = places.briefs(self.connection, "home", now=NOW, config=self.config)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", [self.fun_brief]))

    def test_without_a_home_family_home_briefs_are_unknown_naming_the_file(self):
        self.config.write_text(FAMILIES.splitlines()[0] + "\n", encoding="utf-8")
        doc = places.briefs(self.connection, "home", now=NOW, config=self.config)
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("<config>/project-dashboard/report-families.conf", doc["reason"])
        self.assertIn("family|home|", doc["reason"])


    def test_a_brief_seen_on_today_reads_seen_here_until_its_text_grows(self):
        # sd:5752: Seen is Today's snooze, under Today's row id and fingerprint, so a mark made in either place holds in both.
        stem = "2026-09-05 - Board Notes"
        def mine():
            return next(r for r in places.briefs(self.connection, "hoa", now=NOW, config=self.config)["rows"] if r["id"] == stem)
        today = next(r for r in today_reports.document(self.connection, now=NOW, snoozes={})["briefs"] if r["stem"] == stem)
        row = mine()
        self.assertEqual((row["mark"], row["seen"], row["marked"]), (today["id"], today["seen"], False))
        writes.snooze(self.connection, snooze.key("today", today["id"]), today["until"], seen=today["seen"], now=NOW)
        self.assertTrue(mine()["marked"])
        routine_briefs.add(self.connection, stem, "---\nseries: x\n---\n\nA lead.\n\nA second.\n", repo=self.hoa,
                           who="test", mode="extend")
        self.assertFalse(mine()["marked"])

    def test_a_seen_read_that_fails_marks_nothing_and_says_so(self):
        with mock.patch.object(writes, "snoozed", side_effect=sqlite3.OperationalError("no such table: state")):
            doc = places.briefs(self.connection, "hoa", now=NOW, config=self.config)
        self.assertEqual((doc["state"], [r["marked"] for r in doc["rows"]]), ("read", [False, False]))
        self.assertIn("Seen marks were not read: no such table: state. Every brief shows as open.", doc["reason"])


class Reports(Fixture):
    def test_hoa_reports_are_the_hoa_familys(self):
        doc = places.reports(self.connection, "hoa", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual((doc["state"], self.ids(doc), doc["jobs"]),
                         ("read", [self.alarm], ["hoa-mission-alarms", "hoa-morning-digest"]))
        self.assertTrue(doc["rows"][0]["attention"])

    def test_home_reports_are_the_home_familys(self):
        doc = places.reports(self.connection, "home", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", [self.fun]))

    def test_a_missing_family_list_is_unknown_naming_the_setting_never_an_empty_list(self):
        doc = places.reports(self.connection, "home", now=NOW, config=self.dir / "absent.conf", jobs=self.jobs)
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("dashboard.report_families on Settings", doc["reason"])

    def test_a_file_without_the_family_is_unknown_naming_the_line_to_add(self):
        self.config.write_text(FAMILIES.splitlines()[0] + "\n", encoding="utf-8")
        doc = places.reports(self.connection, "home", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual(doc["state"], "unknown")
        self.assertIn("names no family home", doc["reason"])

    def test_a_reports_details_are_the_reports_pages_document_for_the_familys_jobs_alone(self):
        # sd:5636: a Reports row's Details on HOA and Home are the Reports page's own, so the view carries that page's
        # document (reports_screen.document) for the family's jobs: their reports with bodies, installed jobs and cadence.
        doc = places.reports(self.connection, "hoa", now=NOW, config=self.config, jobs=self.jobs)
        details = doc["details"]
        self.assertEqual([r["id"] for r in details["reports"]], [self.alarm])
        self.assertEqual(details["reports"][0]["body"], "ran\n")
        self.assertEqual([j["name"] for j in details["jobs"]], ["hoa-mission-alarms"])
        self.assertEqual(list(details["cadence"]), ["hoa-mission-alarms"])
        self.assertEqual((details["sources"], details["attention"]["n"]), ({"reports": "", "jobs": "", "cadence": ""}, 1))
        self.assertNotIn("html", details, "a place reads no HTML folder count")

    def test_a_jobs_read_that_fails_leaves_the_reports_and_names_what_was_not_read(self):
        # sd:5636: without the job list there is no Run now and no cadence; the view says so, never "no job".
        jobs = JobsBackend(self.dir, refuse="launchctl refused")
        doc = places.reports(self.connection, "hoa", now=NOW, config=self.config, jobs=jobs)
        self.assertEqual((doc["state"], self.ids(doc), doc["details"]["jobs"], doc["details"]["cadence"]), ("read", [self.alarm], None, None))
        self.assertEqual(doc["reason"], "Not read: jobs (launchctl refused), cadence (the job list was not read, so no job log was).")


class TheRowWins(Fixture):
    """sd:5109: the realm and family lists are setting rows. A row wins over the old file, a cleared row hides it, and a
    row that does not read is unknown: no page reads the file in its place."""

    def setUp(self):
        super().setUp()
        realms = self.dir / "realms.conf"
        realms.write_text("realm|work|Work|/repos/sys*\n", encoding="utf-8")
        self.enterContext(mock.patch.object(places, "REALMS_CONFIG", realms))

    def refuse(self):
        def refuse(connection, keys):
            raise sqlite3.OperationalError("database is locked")

        return mock.patch.object(writes, "read_settings", refuse)

    def realm(self, doc):
        return {r["id"]: r["realm"] for r in doc["rows"]}[self.other_item]

    def test_no_row_reads_the_old_file(self):
        self.assertEqual(self.realm(tasks_screen.document(self.connection, now=NOW)), "work")

    def test_a_realms_row_wins_over_the_file(self):
        writes.set_setting(self.connection, "dashboard.realms", "realm|lab|Lab|/repos/system\n")
        doc = tasks_screen.document(self.connection, now=NOW)
        self.assertEqual(self.realm(doc), "lab")
        self.assertEqual(doc["realms"]["source"], "workflow database setting dashboard.realms")

    def test_a_cleared_realms_row_hides_the_file(self):
        writes.set_setting(self.connection, "dashboard.realms", "realm|lab|Lab|/repos/system\n")
        writes.set_setting(self.connection, "dashboard.realms", None)
        doc = tasks_screen.document(self.connection, now=NOW)
        self.assertEqual((doc["realms"]["state"], self.realm(doc)), ("missing", None))

    def test_a_realms_row_that_does_not_read_is_unknown_and_reads_no_file(self):
        with self.refuse():
            doc = tasks_screen.document(self.connection, now=NOW)
        self.assertEqual((doc["realms"]["state"], self.realm(doc)), ("error", None))
        self.assertIn("database is locked", doc["realms"]["problems"][0])

    def test_a_families_row_wins_over_the_file(self):
        writes.set_setting(self.connection, "dashboard.report_families", "family|home|Home|house|weekly-scan\n")
        doc = places.reports(self.connection, "home", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual((doc["state"], doc["jobs"], self.ids(doc)), ("read", ["weekly-scan"], [self.stray]))

    def test_a_families_row_that_does_not_read_leaves_reports_and_briefs_unknown(self):
        with self.refuse():
            reports = places.reports(self.connection, "hoa", now=NOW, config=self.config, jobs=self.jobs)
            briefs = places.briefs(self.connection, "hoa", now=NOW, config=self.config)
        # The hoa checkout's own briefs could be listed, but which series are HOA's is unknown, not "none".
        self.assertEqual((reports["state"], reports["rows"], briefs["state"], briefs["rows"]), ("error", [], "error", []))
        self.assertIn("database is locked", briefs["reason"])


class RealmFixture(Fixture):
    """A realm acme with a report family of its id, an open item, a report and two briefs."""

    def setUp(self):
        super().setUp()
        self.realms = self.dir / "realms.conf"
        self.realms.write_text("realm|acme|Acme|/repos/acme-*\nrealm|lab|Lab|/repos/lab\n", encoding="utf-8")
        self.enterContext(mock.patch.object(places, "REALMS_CONFIG", self.realms))
        self.config.write_text(FAMILIES + "family|acme|Acme|map|acme-nightly\n", encoding="utf-8")
        self.site = self.repo("/repos/acme-site")
        self.acme_item = self.item("Ship the press kit", kind="task", repo=self.site)
        self.item("A settled Acme item", kind="task", repo=self.site, status="done")
        self.acme_report = report(self, "acme-nightly", "r4")
        self.site_brief = brief(self, "2026-09-05 - Site Notes", self.site)
        self.nightly_brief = brief(self, "2026-09-05 - Acme Nightly", self.other)


class RealmPlaces(RealmFixture):
    """sd:5663: a realm whose id is also a report family key is a place page, with HOA's and Home's three views."""

    def refuse(self):
        def refuse(connection, keys):
            raise sqlite3.OperationalError("database is locked")

        return mock.patch.object(writes, "read_settings", refuse)

    def test_a_realm_whose_glob_matches_the_hoa_checkout_leaves_its_items_to_hoa(self):
        """pack sd:7687: a realm place's Tasks are its Tasks tab's rows, and the hoa checkout's rows are HOA's there."""
        self.realms.write_text("realm|acme|Acme|/repos/*\n", encoding="utf-8")
        doc = places.tasks(self.connection, "acme", now=NOW)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", sorted([self.acme_item, self.other_item])))
        self.assertEqual(self.ids(places.tasks(self.connection, "hoa", now=NOW)), [self.hoa_item])

    def test_tasks_are_the_realms_open_items_the_rows_of_tasks_realm_view(self):
        doc = places.tasks(self.connection, "acme", now=NOW)
        self.assertEqual((doc["state"], doc["area"], self.ids(doc)), ("read", "acme", [self.acme_item]))
        rows = tasks_screen.document(self.connection, now=NOW)["rows"]
        self.assertEqual(sorted(r["id"] for r in rows if r["realm"] == "acme" and r["status"] != "done" and not r["parked"]),
                         self.ids(doc))

    def test_briefs_are_the_realms_repos_and_those_whose_series_names_a_family_job(self):
        doc = places.briefs(self.connection, "acme", now=NOW, config=self.config)
        self.assertEqual((doc["state"], self.ids(doc)), ("read", sorted([self.site_brief, self.nightly_brief])))

    def test_reports_are_the_familys(self):
        doc = places.reports(self.connection, "acme", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual((doc["state"], self.ids(doc), doc["jobs"]), ("read", [self.acme_report], ["acme-nightly"]))

    def test_a_realm_the_list_does_not_name_is_unknown_with_the_line_to_add(self):
        for read in (places.tasks, places.briefs):
            doc = read(self.connection, "beta", now=NOW)
            self.assertEqual((doc["state"], doc["rows"]), ("unknown", []), read.__name__)
            self.assertIn("names no realm beta; add realm|beta|<label>|<glob> to dashboard.realms on Settings", doc["reason"])

    def test_a_missing_family_line_is_unknown_naming_the_line_with_the_realms_label(self):
        self.config.write_text(FAMILIES, encoding="utf-8")
        doc = places.reports(self.connection, "acme", now=NOW, config=self.config, jobs=self.jobs)
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("names no family acme; add family|acme|Acme|<icon>|<job>,<job>,... to dashboard.report_families",
                      doc["reason"])
        # The realm's own repos still give its briefs, and the reason says what else would be read.
        briefs = places.briefs(self.connection, "acme", now=NOW, config=self.config)
        self.assertEqual((briefs["state"], self.ids(briefs)), ("read", [self.site_brief]))
        self.assertIn("names no family acme", briefs["reason"])

    def test_a_realms_row_that_does_not_read_leaves_tasks_and_briefs_error_not_empty(self):
        with self.refuse():
            tasks = places.tasks(self.connection, "acme", now=NOW)
        self.assertEqual((tasks["state"], tasks["rows"]), ("error", []))
        self.assertIn("database is locked", tasks["reason"])
        with mock.patch.object(places, "realms", return_value={"state": "error", "source": "x", "problems": ["not read: locked"],
                                                               "list": []}):
            briefs = places.briefs(self.connection, "acme", now=NOW, config=self.config)
        self.assertEqual((briefs["state"], briefs["rows"]), ("error", []))
        self.assertIn("not read: locked", briefs["reason"])

    def test_a_realm_is_a_place_only_when_a_family_has_its_id(self):
        self.assertEqual([(r["id"], r["place"]) for r in places.realm_places(self.connection, self.config)],
                         [("acme", "map"), ("lab", None)])
        with self.refuse():
            self.assertEqual(places.realm_places(self.connection, self.config), [])
        broken = self.dir / "broken.conf"
        broken.write_bytes(b"\xff\xfe")
        self.assertEqual([r["place"] for r in places.realm_places(self.connection, broken)], [None, None])

    def test_hoa_and_home_keep_their_rows(self):
        self.assertEqual(self.ids(places.tasks(self.connection, "hoa", now=NOW)), [self.hoa_item])
        self.assertEqual(self.ids(places.tasks(self.connection, "home", now=NOW)), [self.home_item])
        self.assertEqual(self.ids(places.briefs(self.connection, "home", now=NOW, config=self.config)), [self.fun_brief])


class RealmIssues(RealmFixture):
    """sd:6164: a realm place's Tasks also lists the operator's open GitHub and Jira issues that /api/github/issues reads
    (issue sources and the shadow sync), only those of the realm's repos, each with its source; issue Hide (sd:6175)
    marks a row hidden, which the page leaves out until Hidden or All."""

    MINE = {"/repos/acme-site": [
        {"tracker": "jira", "url": "https://example.test/browse/ACME-8", "number": None, "title": "Fix the kit",
         "state": "open", "reason": "filed", "last_seen": "2026-09-06T08:00:00+00:00"},
        {"tracker": "github", "url": "https://github.com/example/acme-site/issues/12", "number": 12, "title": "Crash on start",
         "state": "open", "reason": "assigned", "last_seen": "2026-09-05T08:00:00+00:00"}],
        "/repos/system": [
        {"tracker": "github", "url": "https://github.com/example/system/issues/3", "number": 3, "title": "Not the realm's",
         "state": "open", "reason": "assigned", "last_seen": "2026-09-04T08:00:00+00:00"}]}

    def setUp(self):
        super().setUp()
        upsert_repo(self.connection, self.site, remote="git@github.com:example/acme-site.git")
        for number, state in ((30, "open"), (31, "closed")):
            upsert_shadow(self.connection, tracker="github", url=f"https://github.com/example/acme-site/issues/{number}",
                          repo="example/acme-site", number=number, kind="issue", title=f"Site {number}", state=state,
                          reason="filed", filed=True, author="me", closed_at="2026-09-05T10:00:00Z" if state == "closed" else None)
        upsert_shadow(self.connection, tracker="github", url="https://github.com/upstream/tool/issues/5", repo="upstream/tool",
                      number=5, kind="issue", title="Upstream", state="open", reason="filed", filed=True, author="me")
        writes.set_view(self.connection, "issue:jira:acme-8", hidden=True)
        self.connection.commit()

    def doc(self, area="acme"):
        with mock.patch.object(repos, "mine_issues", create=True, return_value=self.MINE):
            return places.tasks(self.connection, area, now=NOW)

    def test_the_realms_open_issues_from_both_trackers_each_with_its_source_and_hidden(self):
        doc = self.doc()
        self.assertEqual([(r["tracker"], r["ref"], r["hidden"]) for r in doc["issues"]],
                         [("Jira", "ACME-8", True), ("GitHub", "#12", False), ("GitHub", "#30", False)])
        self.assertEqual((doc["issues_reason"], doc["issues_store"]), ("", {"available": True, "reason": ""}))
        self.assertEqual(self.ids(doc), [self.acme_item], "the item rows stay as they were")

    def test_a_failed_issue_read_is_named_and_hoa_and_home_list_none(self):
        with mock.patch.object(repos, "mine_issues", create=True, side_effect=sqlite3.OperationalError("database is locked")):
            doc = places.tasks(self.connection, "acme", now=NOW)
        self.assertEqual([r["ref"] for r in doc["issues"]], ["#30"])
        self.assertIn("database is locked", doc["issues_reason"])
        for area in ("hoa", "home"):
            self.assertNotIn("issues", self.doc(area))


class TheRoutes(BrowserSession):
    def test_sections_js_names_the_realms_only_to_a_signed_in_page(self):
        """sd:5064: the realm labels are the operator's own, so a request without a session gets an empty list."""
        with tempfile.TemporaryDirectory() as folder:
            conf = Path(folder) / "realms.conf"
            conf.write_text("realm|work|Work|/repos/example-work/*\n", encoding="utf-8")
            with mock.patch.object(places, "REALMS_CONFIG", conf):
                signed = self.request("/ui/sections.js", headers={"Cookie": self.cookie})[2]
                bare = self.request("/ui/sections.js")[2]
        def realms(body):
            return json.loads(re.search(r"^window\.SHELL_REALMS = (.*);$", body, re.M).group(1))
        self.assertEqual(realms(signed), [{"id": "work", "label": "Work", "place": None}])
        self.assertEqual(realms(bare), [])
        self.assertNotIn("example-work", signed, "a glob is a path; the page needs only the id and label")

    def test_a_realm_with_a_family_of_its_id_carries_its_icon_as_place(self):
        """sd:5663: the rail lists a realm place only when its realm and family lines both read."""
        with tempfile.TemporaryDirectory() as folder:
            conf, families = Path(folder) / "realms.conf", Path(folder) / "report-families.conf"
            conf.write_text("realm|acme|Acme|/repos/acme-*\nrealm|lab|Lab|/repos/lab\n", encoding="utf-8")
            families.write_text("family|acme|Acme|map|acme-nightly\n", encoding="utf-8")
            with mock.patch.object(places, "REALMS_CONFIG", conf), mock.patch.object(reports_screen, "FAMILIES_CONFIG", families):
                body = self.request("/ui/sections.js", headers={"Cookie": self.cookie})[2]
        realms = json.loads(re.search(r"^window\.SHELL_REALMS = (.*);$", body, re.M).group(1))
        self.assertEqual(realms, [{"id": "acme", "label": "Acme", "place": "map"}, {"id": "lab", "label": "Lab", "place": None}])

    def test_a_realm_places_views_are_routes_that_need_a_session(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(places, "REALMS_CONFIG", Path(folder) / "realms.conf"), \
                mock.patch.object(reports_screen, "FAMILIES_CONFIG", Path(folder) / "report-families.conf"):
            for view in ("tasks", "briefs", "reports"):
                path = f"/api/place/acme/{view}"
                self.assertEqual(self.request(path)[0], 403, path)
                status, _, body = self.request(path, headers={"Cookie": self.cookie})
                doc = json.loads(body)
                # No realm list and no family list: unknown, with the line to add, never an empty list.
                self.assertEqual((status, doc["area"], doc["state"], doc["rows"]), (200, "acme", "unknown", []), path)
                self.assertIn("add ", doc["reason"], path)
                # A Tasks place view or a malformed id is not a realm.
                for other in ("hoa", "personal", "all"):
                    self.assertEqual(self.request(f"/api/place/{other}/{view}", headers={"Cookie": self.cookie})[0], 404)
                self.assertEqual(self.request(f"/api/place/Acme/{view}", headers={"Cookie": self.cookie})[0], 404)
        status, _, page = self.request("/places?realm=acme", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn('src="/ui/place.js', page)

    def test_each_view_is_its_own_route_that_needs_a_session(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(reports_screen, "FAMILIES_CONFIG", Path(folder) / "report-families.conf"):
            # HOA's Tasks view is /api/hoa's items, so HOA has no tasks read of its own.
            self.assertEqual(self.request("/api/hoa/tasks", headers={"Cookie": self.cookie})[0], 404)
            for area in places.AREAS:
                for view in ("tasks", "briefs", "reports") if area == "home" else ("briefs", "reports", "votes"):
                    path = f"/api/{area}/{view}"
                    self.assertEqual(self.request(path)[0], 403, path)
                    status, _, body = self.request(path, headers={"Cookie": self.cookie})
                    self.assertEqual(status, 200, path)
                    doc = json.loads(body)
                    self.assertEqual(doc["area"], area, path)
                    # The fixture registers no hoa checkout and the folder holds no family file: unknown, with why.
                    if (area, view) != ("home", "tasks"):
                        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []), path)
                        self.assertTrue(doc["reason"], path)


class ThePlaceScript(Fixture):
    """places.js on the HOA and Home pages: the switch, and each view reading its own document only when shown."""

    #: The views each page's markup declares: [id, label, api]. HOA's Tasks is drawn by hoa.js, so it reads nothing here.
    VIEWS = {"hoa": [["overview", "Overview", None], ["tasks", "Tasks", None], ["briefs", "Briefs", "/api/hoa/briefs"], ["reports", "Reports", "/api/hoa/reports"],
                     ["votes", "Votes", "/api/hoa/votes"]],
             "home": [["overview", "Overview", None], ["tasks", "Tasks", "/api/home/tasks"], ["briefs", "Briefs", "/api/home/briefs"], ["reports", "Reports", "/api/home/reports"]],
             # A realm place (place.html, sd:6162): every view reads its own document, the Overview too.
             "acme": [[v, v.title(), f"/api/place/acme/{v}"] for v in ("overview", "tasks", "briefs", "reports")]}

    def run_page(self, body, docs, place="hoa", setup="", before=""):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL
                  + f"\nconst DOCS = {json.dumps(docs)}, PLACE = {json.dumps(place)}, VIEWS = {json.dumps(self.VIEWS[place])};\n"
                  + """ANSWER = path => path in DOCS ? DOCS[path] : [404, { error: 'no answer' }];
location.hash = '';
document.body.dataset.place = PLACE;
document.documentElement = { hasAttribute: () => false };
const SECTIONS = VIEWS.map(([v, label, api]) => Object.assign(El('view-' + v), { dataset: Object.assign({ placeView: v, label }, api ? { api } : {}) }));
document.querySelectorAll = sel => sel === '[data-place-view]' ? SECTIONS : [];
const shown = () => SECTIONS.filter(x => !x.hidden).map(x => x.dataset.placeView);
// The shell's view switch (shell.js view:start): the first view shows at load, and each change fires 'shell:view'.
let VIEW = VIEWS[0][0];
window.shell.view = { current: () => VIEW, show: () => true, count: () => {} };
const go = async hash => { VIEW = hash.replace('#view=', ''); document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: VIEW } })); await flush(); };
""" + before + "\n" + COMMANDS_JS + BRIEF_DETAILS_JS + REPORT_DETAILS_JS + TASK_DETAILS_JS + PLACES_JS + "\nvar R = {};\n(async () => { try {\n" + setup + "\n(DOC_LISTENERS['shell:ready'] || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def docs(self, area="hoa"):
        more = {"tasks": {}, "briefs": {"config": self.config}, "reports": {"config": self.config, "jobs": self.jobs}, "votes": {}}
        return {f"/api/{area}/{view}": [200, getattr(places, view)(self.connection, area, now=NOW, **more[view])]
                for view in ("tasks", "briefs", "reports", *(("votes",) if area == "hoa" else ()))}


    # pack sd:8050: HOA's Votes view: Open | All, open votes first, read-only.
    def test_the_votes_view_shows_open_votes_and_all_adds_the_closed_ones_with_each_column(self):
        sd_votes.add(self.connection, self.hoa, "CI-1", {"date": "2026-10-02", "topic": "Crime policy",
                     "motion": "Buy the <crime> policy.", "type": "email vote", "moved": "Ada", "seconded": "Ben",
                     "votes": "Ada, Ben Yes; Cy No", "outcome": "adopted", "source": "mail 1"})
        sd_votes.add(self.connection, self.hoa, "PG-1", {"date": "2026-09-29", "topic": "Inventory hours",
                     "motion": "Approve the hours.", "votes": "not recorded", "outcome": "pending: not sent"})
        out = self.run_page(self.ACTS + """await go('#view=votes'); R.open = ELS['votes-rows'].html; R.tally = ELS['votes-tally'].html;
seg('votes', 'all'); await flush(); R.all = ELS['votes-rows'].html;""", self.docs())
        r = out["R"]
        self.assertEqual(out["gets"], ["/api/hoa/votes"])
        self.assertIn("PG-1", r["open"])
        self.assertNotIn("CI-1", r["open"])
        self.assertIn('Open <span class="n">1</span>', r["open"])
        self.assertIn('All <span class="n">2</span>', r["open"])
        self.assertLess(r["all"].index("PG-1"), r["all"].index("CI-1"))
        for part in ("Crime policy", "Buy the &lt;crime&gt; policy.", "email vote", "Ada / Ben", "Yes: Ada, Ben", "No: Cy",
                     "adopted", "mail 1", "not recorded", "2026-10-02"):
            self.assertIn(part, r["all"], part)
        self.assertIn('<table class="ledger place-rows" data-toolbar="votes-filters">', r["all"])
        self.assertIn("1 open vote", r["tally"])

    def test_add_and_edit_vote_post_the_fields_and_the_revision_read_and_read_the_view_again(self):
        # pack sd:8058: Add vote posts every field typed with no revision; Edit posts only the fields changed, with the
        # revision the view read, as `sd votes edit --if-revision`. Each reads the view again; a stale edit does too.
        sd_votes.add(self.connection, self.hoa, "PG-1", {"motion": "Approve the hours.", "outcome": "pending: not sent"})
        docs = self.docs()
        docs["/api/hoa/vote"] = [200, {"vote": {}}]
        out = self.run_page(self.ACTS + """await go('#view=votes'); R.rows = ELS['votes-rows'].html;
FIELD_VALUES = { key: 'CI-9', motion: 'Buy the <crime> policy.', outcome: 'adopted', state: '' };
shellRun(cmd('vote.add'), C.get('votes')); await flush(); R.addCli = cmd('vote.add').cli(C.get('votes'), FIELD_VALUES);
FIELD_VALUES = { outcome: 'adopted', state: 'closed' };
shellRun(cmd('vote.edit'), C.get('vote:PG-1')); await flush(); R.editCli = cmd('vote.edit').cli(C.get('vote:PG-1'), FIELD_VALUES);
ANSWER = path => path === '/api/hoa/vote' ? [409, { error: 'vote PG-1 is at revision 2, not 1; read it again' }] : DOCS[path];
shellRun(cmd('vote.edit'), C.get('vote:PG-1')); await flush();""", docs)
        self.assertRegex(out["R"]["rows"], r'(?s)<tr>.*PG-1.*<i data-act="vote.edit"></i></td></tr>')
        self.assertEqual(out["posts"][0][:2], ["/api/hoa/vote", {"key": "CI-9", "revision": None,
                                                                  "fields": {"motion": "Buy the <crime> policy.", "outcome": "adopted"}}])
        self.assertEqual(out["posts"][1][:2], ["/api/hoa/vote", {"key": "PG-1", "revision": 1, "fields": {"outcome": "adopted", "state": "closed"}}])
        self.assertEqual(out["gets"].count("/api/hoa/votes"), 4, "each write did not read the view again")
        self.assertEqual([t["msg"] for t in out["toasts"]][:2], ["Vote added · CI-9", "Vote edited · PG-1"])
        self.assertEqual(out["toasts"][2]["msg"], "PG-1 not changed: vote PG-1 is at revision 2, not 1; read it again")
        self.assertIn("sd votes add 'CI-9' --motion 'Buy the <crime> policy.' --outcome 'adopted' --belongs-to '/repos/hoa'", out["R"]["addCli"])
        self.assertIn("sd votes edit 'PG-1' --if-revision 1 --outcome 'adopted' --state 'closed' --belongs-to '/repos/hoa'", out["R"]["editCli"])

    def test_no_vote_and_no_open_vote_say_which(self):
        out = self.run_page("await go('#view=votes'); R.none = ELS['votes-rows'].html;", self.docs())
        self.assertIn("No vote is recorded for the hoa checkout.", out["R"]["none"])
        sd_votes.add(self.connection, self.hoa, "CI-1", {"outcome": "adopted"})
        out = self.run_page("await go('#view=votes'); R.closed = ELS['votes-rows'].html;", self.docs())
        self.assertIn("No open vote. All lists the 1 closed vote.", out["R"]["closed"])

    def test_a_click_on_a_row_opens_what_its_link_opens(self):
        # sd:5481: a row opens its page with its Details; a click on the link itself or with a modifier is the browser's.
        out = self.run_page("""R.went = []; location.assign = h => R.went.push(h); globalThis.getSelection = () => ({ toString: () => '' });
const tr = { querySelector: () => ({ href: '/tasks?row=5' }) };
const click = (onLink, more = {}) => (DOC_LISTENERS['click'] || []).forEach(f => f(Object.assign({ button: 0, target: { closest: sel => sel === '.place-rows tbody tr' ? tr : onLink ? {} : null } }, more)));
click(false); click(true); click(false, { metaKey: true });""", self.docs())
        self.assertEqual(out["R"]["went"], ["/tasks?row=5"])

    #: A Briefs row as the click handler sees it: its id, and the link the row used to follow.
    BRIEF_CLICK = """const clickRow = id => { const tr = { dataset: { id }, querySelector: () => ({ href: '/briefs?row=' + encodeURIComponent(id) }) };
  (DOC_LISTENERS['click'] || []).forEach(f => f({ button: 0, target: { closest: sel => sel === '.place-rows tbody tr' ? tr : null } })); };
R.went = []; location.assign = h => R.went.push(h); globalThis.getSelection = () => ({ toString: () => '' });
R.panes = []; window.shell.openPane = p => R.panes.push(p);
"""

    def test_a_briefs_row_shows_its_details_in_the_pane_and_the_page_stays(self):
        # sd:5620: a Briefs row on HOA or Home fills Details with the Briefs page's own Details and commands, rather than
        # opening Briefs; a link in Details still opens it there.
        for place, stem in (("hoa", "2026-09-05 - Board Notes"), ("home", "2026-09-05 - Fun Events Nightly")):
            out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=briefs'); R.rows = ELS['briefs-rows'].html;
clickRow({json.dumps(stem)}); await flush();
R.det = document.getElementById('details').html || ''; R.reg = REG.map(c => c.id); R.obj = (C.get({json.dumps(stem)}) || {{}}).type;""", self.docs(place), place=place)
            r = out["R"]
            self.assertEqual(r["went"], [], place)
            self.assertEqual(r["panes"], ["tab-details"], place)
            self.assertIn(f'<tr data-id="{stem}" aria-selected="false">', r["rows"], place)
            self.assertIn("Brief · ", r["det"], place)
            self.assertIn(f"<h2>{stem}</h2>", r["det"], place)
            self.assertIn(f'href="/briefs?row={stem.replace(" ", "%20")}"', r["det"], place)
            self.assertEqual(r["obj"], "brief", place)
            for command in ("brief.read", "brief.task", "brief.open", "brief.adversarial-review", "brief.publish"):
                self.assertIn(command, r["reg"], place)

    def test_j_k_and_enter_walk_the_briefs_rows_through_the_shells_list(self):
        # sd:5620: the shell's grammar (shell.js walk): j / k select a row, Details follows without opening the pane, and
        # Enter (shell:open) opens it.
        board, digest = "2026-09-05 - Board Notes", "2026-09-05 - HOA Morning Digest"
        out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=briefs');
window.PAGE_LIST.select({json.dumps(digest)}); await flush();
R.cur = window.PAGE_LIST.current(); R.det = ELS.details.html || ''; R.quiet = R.panes.slice();
open({json.dumps(board)}); await flush(); R.cur2 = window.PAGE_LIST.current(); R.det2 = ELS.details.html || '';""", self.docs())
        r = out["R"]
        self.assertEqual((r["cur"], r["quiet"]), (digest, []))
        self.assertIn(f"<h2>{digest}</h2>", r["det"])
        self.assertEqual((r["cur2"], r["panes"]), (board, ["tab-details"]))
        self.assertIn(f"<h2>{board}</h2>", r["det2"])

    def test_a_reports_row_shows_the_reports_pages_details_in_the_pane_and_the_page_stays(self):
        # sd:5636: a Reports row on HOA or Home fills Details with the Reports page's own Details and commands, rather than
        # opening Reports; a link in Details still opens it there.
        for place, n, job in (("hoa", self.alarm, "hoa-mission-alarms"), ("home", self.fun, "fun-events-nightly")):
            out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=reports'); R.rows = ELS['reports-rows'].html;
clickRow('r{n}'); await flush();
R.det = ELS.details.html || ''; R.reg = REG.map(c => c.id); R.obj = (C.get('r{n}') || {{}}).type; R.job = (C.get('job:{job}') || {{}}).type;""", self.docs(place), place=place)
            r = out["R"]
            self.assertEqual(r["went"], [], place)
            self.assertEqual(r["panes"], ["tab-details"], place)
            self.assertIn(f'<tr data-id="r{n}" aria-selected="false">', r["rows"], place)
            self.assertIn(f"Database report #{n} · ", r["det"], place)
            for part in ("<h3>Report source</h3>", "<h3>Last 7 days</h3>", "Since the previous report", f"<h3>Job · {job}</h3>",
                         f'href="/reports?row=r{n}"'):
                self.assertIn(part, r["det"], place)
            self.assertEqual((r["obj"], r["job"]), ("report", "report-job"), place)
            for command in ("report.ack", "report.resolve", "report.show", "report.log", "report.task", "report.fix", "report-job.retry"):
                self.assertIn(command, r["reg"], place)

    def test_a_reports_run_now_is_its_own_beside_hoas_source_jobs(self):
        # sd:5636: HOA's source rows are 'job' objects with HOA's own Run now (hoa.js, jobs.retry); a report's job is another
        # type, so neither page's Run now runs on the other's object, and a report's posts its job's retry route.
        setup = "C.register({ id: 'jobs.retry', on: 'job', run: () => { throw new Error('HOA source handler'); } });"
        docs = self.docs()
        docs["/api/jobs/hoa-mission-alarms/retry"] = [200, {"answered": True}]
        out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=reports'); clickRow('r{self.alarm}'); await flush();
R.ids = REG.map(c => c.id); const job = C.get('job:hoa-mission-alarms');
R.on = REG.filter(c => c.on === job.type).map(c => c.id);
shellRun(cmd('report-job.retry'), job); await flush();""", docs, setup=setup)
        r = out["R"]
        self.assertEqual(len(r["ids"]), len(set(r["ids"])), "each command id is registered once")
        self.assertEqual(r["on"], ["report-job.retry"])
        self.assertEqual([p[0] for p in out["posts"]], ["/api/jobs/hoa-mission-alarms/retry"])

    def test_a_reread_that_fails_keeps_the_report_selected_and_says_it_is_not_known(self):
        # sd:5636: a failed read is unknown, not "gone": Details say why, no command runs (its pick is dropped, as read.js
        # drops one), and the next read selects it again.
        out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=reports'); clickRow('r{self.fun}'); await flush();
R.unpicked = []; C.pick = id => R.unpicked.push(id); document.dispatchEvent(new CustomEvent('shell:picked', {{ detail: ['r{self.fun}'] }}));
const ok = DOCS['/api/home/reports']; DOCS['/api/home/reports'] = [503, {{ error: 'store busy' }}];
await window.places.reread('reports'); await flush(); R.det = ELS.details.html || ''; R.cur = window.PAGE_LIST.current(); R.type = C.get('r{self.fun}').type;
DOCS['/api/home/reports'] = ok; await window.places.reread('reports'); await flush(); R.det2 = ELS.details.html || ''; R.type2 = C.get('r{self.fun}').type;""", self.docs("home"), place="home")
        r = out["R"]
        self.assertIn("The reports were not read again, so the selected row is not known: /api/home/reports did not answer", r["det"])
        self.assertEqual((r["cur"], r["type"], r["type2"], r["unpicked"]), (f"r{self.fun}", "not listed", "report", [f"r{self.fun}"]))
        self.assertIn(f"Database report #{self.fun} · ", r["det2"])

    def test_acknowledge_in_a_reports_details_posts_the_reports_route_and_reads_the_view_again(self):
        # sd:5636: the command is the Reports page's (report-details.js), with the report's revision; the view reads again,
        # and Details show the report as the new read lists it. (HOA's alarm report waits on its followup: Acknowledge is off.)
        docs = self.docs("home")
        revision = docs["/api/home/reports"][1]["details"]["reports"][0]["revision"]
        docs[f"/api/reports/{self.fun}/acknowledge"] = [200, {"revision": "after-ack"}]
        out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=reports'); clickRow('r{self.fun}'); await flush();
shellRun(cmd('report.ack'), C.get('r{self.fun}')); await flush(); R.det = ELS.details.html || '';""", docs, place="home")
        self.assertEqual(out["posts"], [[f"/api/reports/{self.fun}/acknowledge", {"revision": revision}, 64]])
        self.assertEqual(out["gets"], ["/api/home/reports", "/api/home/reports"])
        self.assertEqual(out["toasts"][-1]["msg"], f"Acknowledged · #{self.fun}")
        self.assertIn(f"Database report #{self.fun} · ", out["R"]["det"])

    def test_a_failed_fix_launch_in_a_reports_details_reads_the_view_again(self):
        # pack sd:7783: a failed launch (5xx) notes its cause on the report, so the revision the view holds moved; the
        # view reads again, or the next Fix is refused stale. A refusal that wrote nothing (400) reads nothing.
        for status, reads in ((503, 2), (400, 1)):
            docs = self.docs("home")
            docs[f"/api/items/{self.fun}/fix"] = [status, {"error": "Fix failed: herdr.sh launch: session sd is not running."}]
            out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=reports'); clickRow('r{self.fun}'); await flush();
shellRun(cmd('report.fix'), C.get('r{self.fun}')); await flush(); await flush();""", docs, place="home")
            self.assertEqual([p[0] for p in out["posts"]], [f"/api/items/{self.fun}/fix"], status)
            self.assertEqual(out["gets"], ["/api/home/reports"] * reads, status)

    def task_docs(self):
        docs = self.docs("home")
        docs[f"/api/tasks/{self.home_item}"] = [200, tasks_screen.details(self.connection, self.home_item, now=NOW)]
        return docs

    def test_a_tasks_row_shows_the_tasks_pages_details_in_the_pane_and_the_page_stays(self):
        # sd:5635: a Tasks row on Home fills Details with the Tasks page's own Details and commands, rather than opening
        # Tasks; a link in Details still opens it there. HOA's Tasks rows are hoa.js's, which attaches it itself (sd:5671).
        n = self.home_item
        out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=tasks'); R.rows = ELS['tasks-rows'].html;
clickRow('{n}'); await flush();
R.det = ELS.details.html || ''; R.reg = REG.map(c => c.id); R.obj = (C.get('{n}') || {{}}).type; R.cur = window.PAGE_LIST.current();""",
                            self.task_docs(), place="home")
        r = out["R"]
        self.assertEqual((r["went"], r["panes"], r["cur"], r["obj"]), ([], ["tab-details"], str(n), "item"))
        self.assertIn(f'<tr data-id="{n}" aria-selected="false">', r["rows"])
        self.assertIn(f"/api/tasks/{n}", out["gets"])
        for part in ("<h2>Clean the gutters</h2>", '<dl class="fields">', f"<dd>#{n}</dd>", '<select id="det-status" class="fld">',
                     "<h3>Status history", f'href="/tasks?row={n}">Open on Tasks</a>', "from /api/home/tasks."):
            self.assertIn(part, r["det"])
        for command in ("item.status.done", "item.edit", "item.note"):
            self.assertIn(command, r["reg"])
        hoa = self.run_page("await go('#view=tasks'); R.reg = REG.map(c => c.id);", self.docs())
        self.assertEqual([c for c in hoa["R"]["reg"] if c.startswith("item.")], [])

    def test_a_status_move_in_a_tasks_details_posts_the_item_route_and_reads_the_view_again(self):
        # sd:5635: the move is the Tasks page's (task-details.js), with the row's revision; the view reads again.
        n, docs = self.home_item, self.task_docs()
        row = docs["/api/home/tasks"][1]["rows"][0]
        to = next(s for s in row["allowed"] if s != row["status"])
        docs[f"/api/items/{n}/status"] = [200, {"revision": "after-move", "item": {"status": to, "priority": row["priority"], "due": row["due"],
                                                                                    "recurrence": row["recurrence"], "title": row["title"]}}]
        out = self.run_page(self.BRIEF_CLICK + f"""await go('#view=tasks'); clickRow('{n}'); await flush();
shellRun(cmd('item.status.{to}'), C.get('{n}')); await flush(); R.det = ELS.details.html || '';""", docs, place="home")
        self.assertEqual([p[:2] for p in out["posts"]], [[f"/api/items/{n}/status", {"status": to, "revision": row["revision"]}]])
        self.assertEqual(out["gets"].count("/api/home/tasks"), 2)
        self.assertIn("<h2>Clean the gutters</h2>", out["R"]["det"])

    #: sd:5752: each row's act cell names its primary command, as shell.js's rowActions picks it (the first live one whose
    #: primary holds); a click on a filter bar segment; shell.url's writes.
    ACTS = """C.rowActions = id => { const o = C.get(id), p = o && REG.find(c => c.on === o.type && (!c.when || c.when(o) === true) && c.primary?.(o));
  return mk`<i data-act="${p ? p.id : ''}"></i>`; };
const seg = (f, v) => (DOC_LISTENERS['click'] || []).forEach(h => h({ button: 0, target: { closest: sel => sel === '.place-view[data-api] [data-f]' ? { dataset: { f, v } } : null } }));
R.urls = []; window.shell.url = q => R.urls.push(String(q));
"""

    def acked_docs(self):
        """Home's documents with one done task and the fun report acknowledged."""
        self.swept = self.item("Swept the porch", kind="task", status="done")
        reporting.acknowledge(self.connection, self.fun, expected_revision=workflow.item_state(self.connection, self.fun)["revision"], who="test")
        self.open_report = report(self, "fun-events-nightly", "r4")
        return self.docs("home")

    def test_tasks_and_reports_show_their_open_rows_and_all_shows_every_row_kept_in_the_url(self):
        # sd:5752: Open is the default; All adds the week's done tasks or the acknowledged reports, and the URL keeps it.
        docs = self.acked_docs()
        out = self.run_page(self.ACTS + """await go('#view=tasks'); R.open = ELS['tasks-rows'].html;
seg('tasks', 'all'); await flush(); R.all = ELS['tasks-rows'].html; R.gets = OUT.gets.slice();
seg('tasks', 'open'); await flush(); R.back = ELS['tasks-rows'].html;
await go('#view=reports'); R.reports = ELS['reports-rows'].html; seg('reports', 'all'); await flush(); R.reportsAll = ELS['reports-rows'].html;""",
                            docs, place="home")
        r = out["R"]
        def row(n):
            return f'<tr data-id="{n}" '
        self.assertEqual([row(self.home_item) in r["open"], row(self.swept) in r["open"]], [True, False])
        self.assertIn('data-f="tasks" data-v="open" aria-pressed="true"', r["open"])
        self.assertIn('Open <span class="n">1</span>', r["open"])
        self.assertIn('All <span class="n">2</span>', r["open"])
        self.assertEqual([row(self.home_item) in r["all"], row(self.swept) in r["all"]], [True, True])
        self.assertEqual(r["gets"], ["/api/home/tasks"], "a filter draws the read it has; it reads nothing")
        self.assertNotIn(row(self.swept), r["back"])
        self.assertEqual([row(f"r{self.open_report}") in r["reports"], row(f"r{self.fun}") in r["reports"]], [True, False])
        self.assertEqual([row(f"r{self.open_report}") in r["reportsAll"], row(f"r{self.fun}") in r["reportsAll"]], [True, True])
        self.assertEqual(r["urls"], ["tasks=all", "", "reports=all"])
        # A page opened with ?tasks=all shows All at once.
        out = self.run_page("await go('#view=tasks'); R.tasks = ELS['tasks-rows'].html;", docs, place="home",
                            before="location.search = '?tasks=all';")
        self.assertIn(row(self.swept), out["R"]["tasks"])
        self.assertIn('data-f="tasks" data-v="all" aria-pressed="true"', out["R"]["tasks"])

    def test_each_views_table_puts_its_column_filter_in_the_open_all_row(self):
        # pack sd:7210: one filter row per view. The table names the Open | All row as its toolbar, so the shell's column
        # filter joins that row (shell.js tapply), as HOA's Tasks; a .fbar there took the row's full width and pushed it under.
        out = self.run_page(self.ACTS + """await go('#view=tasks'); R.tasks = ELS['tasks-rows'].html;
await go('#view=briefs'); R.briefs = ELS['briefs-rows'].html; await go('#view=reports'); R.reports = ELS['reports-rows'].html;""",
                            self.acked_docs(), place="home")
        for view in ("tasks", "briefs", "reports"):
            html = out["R"][view]
            self.assertIn(f'<div class="filters toolbar-2" id="{view}-filters" role="group" aria-label="Filters">'
                          f'<div class="fseg" role="group" aria-label="Shown"><button type="button" data-f="{view}" data-v="open"', html, view)
            self.assertIn(f'<table class="ledger place-rows" data-toolbar="{view}-filters">', html, view)
            self.assertNotIn('class="fbar"', html, view)

    def test_a_tasks_due_cell_and_a_reports_when_cell_are_date_cells(self):
        # sd:6185: shell.css's date-cell rule keeps them on one line; the Due cell carries its class too.
        out = self.run_page("await go('#view=tasks'); R.tasks = ELS['tasks-rows'].html; "
                            "await go('#view=reports'); R.reports = ELS['reports-rows'].html;", self.acked_docs(), place="home")
        self.assertIn('<td class="hide-sm due">', out["R"]["tasks"])
        self.assertIn('<td class="when">', out["R"]["reports"])

    def test_each_tasks_and_reports_rows_primary_act_is_its_acknowledge(self):
        # sd:5752: a task's Done (task-details.js' own move), whatever its status, and a report's Acknowledge (report.ack).
        docs = self.acked_docs()
        out = self.run_page(self.ACTS + """await go('#view=tasks'); R.tasks = ELS['tasks-rows'].html;
await go('#view=reports'); R.reports = ELS['reports-rows'].html;""", docs, place="home")
        r = out["R"]
        self.assertIn('<td class="what"><button type="button">Clean the gutters</button></td>', r["tasks"])
        self.assertRegex(r["tasks"], rf'(?s)<tr data-id="{self.home_item}" .*?<i data-act="item.status.done"></i></td></tr>')
        self.assertRegex(r["reports"], rf'(?s)<tr data-id="r{self.open_report}" .*?<i data-act="report.ack"></i></td></tr>')
        self.assertIn('<th scope="col" class=""><span class="sr">Actions</span></th>', r["tasks"])

    def test_done_moves_a_task_out_of_open_and_undo_brings_it_back(self):
        # sd:5752: Done is the Tasks page's move, with its Undo; the view reads again after each, and Open follows the read.
        n, docs = self.home_item, self.docs("home")
        row = docs["/api/home/tasks"][1]["rows"][0]
        state = workflow.item_state(self.connection, n)
        moved = workflow.change_status(self.connection, n, "done", expected_revision=state["revision"], who="test")
        done_doc = places.tasks(self.connection, "home", now=NOW)
        docs[f"/api/items/{n}/status"] = [200, {"revision": moved["revision"], "item": {"status": "done", "priority": row["priority"],
                                                                                         "due": row["due"], "recurrence": row["recurrence"], "title": row["title"]}}]
        out = self.run_page(self.ACTS + f"""const open = DOCS['/api/home/tasks'];
await go('#view=tasks'); R.before = ELS['tasks-rows'].html;
DOCS['/api/home/tasks'] = {json.dumps([200, done_doc])};
shellRun(cmd('item.status.done'), C.get('{n}')); await flush(); R.after = ELS['tasks-rows'].html;
DOCS['/api/home/tasks'] = open; lastUndo().undo(); await flush(); R.undone = ELS['tasks-rows'].html;""", docs, place="home")
        r, mark = out["R"], f'<tr data-id="{n}" '
        self.assertEqual([mark in r["before"], mark in r["after"], mark in r["undone"]], [True, False, True])
        self.assertEqual([p[1]["status"] for p in out["posts"]], ["done", row["status"]])
        self.assertIn('All <span class="n">1</span>', r["after"])

    def seen_docs(self):
        """HOA's documents with Board Notes marked seen on Today, the way Today's Seen writes it."""
        row = next(r for r in places.briefs(self.connection, "hoa", now=NOW, config=self.config)["rows"] if r["item"] == self.hoa_brief)
        writes.snooze(self.connection, snooze.key("today", row["mark"]), writes.SNOOZE_FOREVER, seen=row["seen"], now=NOW)
        return self.docs("hoa")

    def test_briefs_show_the_unseen_and_all_adds_the_seen_kept_in_the_url(self):
        # sd:5752: Open is the briefs Today's Seen has not marked; All adds the seen ones, and ?briefs=all keeps it.
        docs, seen, unseen = self.seen_docs(), "2026-09-05 - Board Notes", "2026-09-05 - HOA Morning Digest"
        out = self.run_page(self.ACTS + """await go('#view=briefs'); R.open = ELS['briefs-rows'].html;
seg('briefs', 'all'); await flush(); R.all = ELS['briefs-rows'].html;""", docs)
        r, row = out["R"], lambda stem: f'<tr data-id="{stem}" '
        self.assertEqual([row(unseen) in r["open"], row(seen) in r["open"]], [True, False])
        self.assertIn('data-f="briefs" data-v="open" aria-pressed="true"', r["open"])
        self.assertEqual([row(unseen) in r["all"], row(seen) in r["all"]], [True, True])
        self.assertEqual(r["urls"], ["briefs=all"])
        out = self.run_page("await go('#view=briefs'); R.briefs = ELS['briefs-rows'].html;", docs, before="location.search = '?briefs=all';")
        self.assertIn(row(seen), out["R"]["briefs"])

    def test_a_briefs_seen_is_its_rows_act_and_posts_todays_mark_until_it_changes_and_undo_clears_it(self):
        # sd:5752: the Today snooze, under Today's row id and fingerprint, until 'forever'; the view reads again after each.
        docs, stem = self.docs("hoa"), "2026-09-05 - HOA Morning Digest"
        row = next(r for r in docs["/api/hoa/briefs"][1]["rows"] if r["id"] == stem)
        docs["/api/snooze"] = [200, {"key": f"today:{row['mark']}"}]
        out = self.run_page(self.ACTS + f"""await go('#view=briefs'); R.rows = ELS['briefs-rows'].html; const reads = OUT.gets.length;
shellRun(cmd('brief.seen'), C.get({json.dumps(stem)})); await flush(); R.reads = OUT.gets.length - reads;
lastUndo().undo(); await flush(); R.undone = OUT.gets.length - reads;""", docs)
        r = out["R"]
        self.assertRegex(r["rows"], rf'(?s)<tr data-id="{stem}" .*?<i data-act="brief.seen"></i></td></tr>')
        self.assertEqual([p[:2] for p in out["posts"]],
                         [["/api/snooze", {"page": "today", "row": f"brief:{stem}", "until": "forever", "seen": row["seen"]}],
                          ["/api/snooze", {"page": "today", "row": f"brief:{stem}", "until": None, "seen": row["seen"]}]])
        self.assertEqual((r["reads"], r["undone"]), (1, 2))

    def test_a_bulk_seen_reads_the_view_again_once_after_the_group(self):
        # pack sd:7852: each row's Seen read the view again as it landed, so a bulk Seen of N briefs started N reads.
        docs, stem = self.docs("hoa"), "2026-09-05 - HOA Morning Digest"
        row = next(r for r in docs["/api/hoa/briefs"][1]["rows"] if r["id"] == stem)
        docs["/api/snooze"] = [200, {"key": f"today:{row['mark']}"}]
        out = self.run_page(self.ACTS + f"""await go('#view=briefs'); const o = C.get({json.dumps(stem)}), reads = OUT.gets.length;
C.runBulk(cmd('brief.seen'), [o, o, o, o, o, o]); await flush(); R.reads = OUT.gets.length - reads;""", docs)
        self.assertEqual(len(out["posts"]), 6)
        self.assertEqual(out["R"]["reads"], 1)

    def test_the_overview_reads_no_view_and_each_view_reads_its_own_document_when_shown(self):
        out = self.run_page("""R.first = shown(); R.gets0 = OUT.gets.slice();
await go('#view=tasks'); R.second = shown(); R.gets1 = OUT.gets.slice();
await go('#view=reports'); R.reports = ELS['reports-rows'].html; R.third = shown();""", self.docs())
        r = out["R"]
        self.assertEqual([r["gets0"], r["first"], r["second"], r["gets1"], r["third"]], [[], ["overview"], ["tasks"], [], ["reports"]])
        self.assertEqual(out["gets"], ["/api/hoa/reports"])
        self.assertIn(f'<tr data-id="r{self.alarm}" aria-selected="false">', r["reports"])

    def test_the_page_declares_its_views_for_the_shell_before_shell_js(self):
        # places.js loads before shell.js, which reads window.PAGE_VIEWS when it loads (shell.js view:start).
        for place in ("hoa", "home"):
            out = self.run_page("R.views = window.PAGE_VIEWS;", self.docs(place), place=place)
            self.assertEqual(out["R"]["views"], [{"id": v, "label": label, **({"api": api} if api else {})} for v, label, api in self.VIEWS[place]], place)

    def test_with_the_shells_view_switch_the_page_draws_none_and_reads_the_view_the_shell_shows(self):
        # The shell's 'shell:view' fired at DOMContentLoaded, before shell:ready: the view it shows then is read at shell:ready.
        setup = "window.shell.view = { current: () => 'briefs', show: () => true, count: () => {} };"
        out = self.run_page("""R.sw = (ELS['viewswitch'] || {}).html || ''; R.first = shown(); R.gets0 = OUT.gets.slice();
document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'reports', first: true, api: '/api/hoa/reports' } })); await flush();
R.second = shown(); R.reports = ELS['reports-rows'].html;""", self.docs(), setup=setup)
        r = out["R"]
        self.assertFalse(r["sw"])
        self.assertEqual([r["first"], r["gets0"], r["second"], out["gets"]],
                         [["briefs"], ["/api/hoa/briefs"], ["reports"], ["/api/hoa/briefs", "/api/hoa/reports"]])
        self.assertIn(f'<tr data-id="r{self.alarm}" aria-selected="false">', r["reports"])

    def test_a_view_reads_again_each_time_it_shows(self):
        out = self.run_page("""await go('#view=briefs'); await go('#view=overview'); await go('#view=briefs');""", self.docs())
        self.assertEqual(out["gets"], ["/api/hoa/briefs", "/api/hoa/briefs"])

    def test_home_tasks_read_their_own_document_when_shown(self):
        out = self.run_page("""await go('#view=tasks'); R.tasks = ELS['tasks-rows'].html; R.tally = ELS['tasks-tally'].html;""",
                            self.docs("home"), place="home")
        self.assertEqual(out["gets"], ["/api/home/tasks"])
        self.assertIn(f'<tr data-id="{self.home_item}" aria-selected="false"><td class="ref num">sd:{self.home_item}</td>', out["R"]["tasks"])
        self.assertNotIn("Fix the valve log", out["R"]["tasks"])
        self.assertEqual("", out["R"]["tally"], "sd:5524: the Tasks tab carries the count")

    def test_a_view_that_did_not_read_is_unknown_with_its_reason_never_an_empty_table(self):
        docs = self.docs()
        docs["/api/hoa/briefs"] = [200, places.briefs(self.connection, "home", now=NOW, config=self.dir / "absent.conf")]
        docs["/api/hoa/reports"] = [500, {"error": "the server failed"}]
        out = self.run_page("""await go('#view=briefs'); R.briefs = ELS['briefs-rows'].html;
await go('#view=reports'); R.reports = ELS['reports-rows'].html;""", docs)
        self.assertIn("▨ Not read.</b> not configured: no report families are set", out["R"]["briefs"].replace("&lt;", "<").replace("&gt;", ">"))
        self.assertNotIn("<table", out["R"]["briefs"])
        self.assertIn("▨ Not read.</b> /api/hoa/reports did not answer: the server failed", out["R"]["reports"])

    def test_read_again_reads_the_shown_view_again_and_a_view_without_a_read_reads_nothing(self):
        # sd:5638: the shell's Read again on HOA, Home and a realm place reads the shown place view's document again.
        out = self.run_page("""await go('#view=briefs'); let before = OUT.gets.length;
await window.places.again(); await flush(); R.briefs = OUT.gets.slice(before);
await go('#view=overview'); before = OUT.gets.length;
await window.places.again(); await flush(); R.overview = OUT.gets.slice(before);""", self.docs())
        self.assertEqual((out["R"]["briefs"], out["R"]["overview"]), (["/api/hoa/briefs"], []))


class TheOverviewScript(Fixture):
    """sd:6162, sd:6164: places.js draws a realm place's Overview tiles from /api/place/<id>/overview, and a button posts the
    tile's names, says what came of it and reads the view again."""

    TILES = {"state": "read", "area": "acme", "read": NOW, "reason": "", "problems": [], "rows": [
        {"id": "app", "label": "Staging site", "url": "https://staging.example.test", "here": True, "why": "",
         "probe": {"answers": False, "status": "nothing listens there"},
         "actions": [{"label": "Deploy", "command": "npm run staging:deploy", "help": "Builds the site and puts it on staging.", "last": {"state": "done", "exit": 0, "started": NOW, "ended": NOW, "output": "deployed\n", "cut": False}},
                     {"label": "Tear down", "command": "npm run staging:down", "last": {"state": "done", "exit": 3, "started": NOW, "ended": NOW, "output": "", "cut": False}},
                     {"label": "Refresh", "command": "npm run staging:up", "last": {"state": "running", "started": NOW, "output": "", "cut": False}},
                     {"label": "Smoke", "command": "npm run staging:smoke", "last": {"state": "unknown", "reason": "it ended with no exit record", "started": NOW, "output": "", "cut": False}}]},
        {"id": "far", "label": "Simulator", "url": "", "here": False, "why": "/x is not on this machine, so its actions run elsewhere",
         "probe": None, "actions": [{"label": "Start", "command": "./run.sh", "last": {"state": "none"}}]}]}

    #: A click on the button `label` of tile `tile`, as the document listener sees it.
    CLICK = """const clickAct = (tile, label, off) => { const li = { dataset: { tile, label: tile === 'app' ? 'Staging site' : 'Simulator' } };
  const b = { dataset: { tileAct: label, tip: off || '' }, closest: () => li, getAttribute: () => off ? 'true' : null, setAttribute: () => {} };
  (DOC_LISTENERS['click'] || []).forEach(f => f({ button: 0, target: { closest: sel => sel === '[data-tile-act]' ? b : null } })); };
"""

    def run_overview(self, body, answer=(202, {"started": NOW})):
        docs = {"/api/place/acme/overview": [200, self.TILES], "/api/place/tiles": list(answer)}
        return ThePlaceScript.run_page(self, self.CLICK + body, docs, place="acme")

    run_page = ThePlaceScript.run_page
    VIEWS = ThePlaceScript.VIEWS

    def test_each_tile_shows_its_link_whether_it_answers_and_each_commands_last_run(self):
        out = self.run_overview("R.rows = ELS['overview-rows'].html;")
        rows = out["R"]["rows"]
        self.assertEqual(out["gets"], ["/api/place/acme/overview"])
        self.assertIn('<a class="btn quiet sm" href="https://staging.example.test" target="_blank" rel="noopener">', rows)
        self.assertIn("not answering: nothing listens there", rows)
        for words in ("Done, exit 0", "Failed, exit 3", "Running since", "Not known: it ended with no exit record."):
            self.assertIn(words, rows)
        self.assertIn("<summary>Output</summary><pre>deployed\n</pre>", rows)
        # A tile whose folder is not here says why, and its button is off with the reason.
        self.assertIn("<p class=\"why\">/x is not on this machine, so its actions run elsewhere.</p>", rows)
        self.assertIn('data-tile-act="Start" aria-disabled="true"', rows)
        # pack sd:7675: an action that runs turns its button's icon; one that ended does not.
        self.assertRegex(rows, r'data-tile-act="Refresh" aria-busy="true"[^>]*><svg[^>]*>.*?</svg>Refresh</button>')
        self.assertNotRegex(rows, r'data-tile-act="Deploy" aria-busy')

    def test_a_buttons_hover_says_what_it_does_then_the_command(self):
        # Pack sd:7536: the act line's help leads; with none, the hover names the action plainly. The command is the
        # second line.
        rows = self.run_overview("R.rows = ELS['overview-rows'].html;")["R"]["rows"]
        self.assertIn('data-tile-act="Deploy" data-tip="Builds the site and puts it on staging.\nnpm run staging:deploy"', rows)
        self.assertIn('data-tile-act="Smoke" data-tip="Runs the Smoke action in this tile&#39;s folder.\nnpm run staging:smoke"', rows)
        hosted = self.run_page("R.rows = ELS['overview-rows'].html;", {"/api/place/acme/overview": [200, self.hosted({"state": "none"})]},
                               place="acme")["R"]["rows"]
        self.assertIn('data-tip="Asks far-host to run the Start action in this tile&#39;s folder there.\n./run.sh studio"', hosted)

    def test_a_button_posts_the_tiles_names_toasts_the_start_and_reads_again(self):
        out = self.run_overview("clickAct('app', 'Deploy'); await flush();")
        self.assertEqual(out["posts"], [["/api/place/tiles", {"realm": "acme", "tile": "app", "action": "Deploy"}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], "Staging site · Deploy started. Read again shows how it ended.")
        self.assertEqual(out["gets"], ["/api/place/acme/overview"] * 2)

    @staticmethod
    def hosted(*lasts):
        """sd:6387: an Overview with one tile on another machine's host, one Start per last run given."""
        return {"state": "read", "area": "acme", "read": NOW, "reason": "", "problems": [], "rows": [
            {"id": "sim", "label": "Simulator", "url": "", "here": False, "host": "far-host", "why": "", "probe": None,
             "actions": [{"label": f"Start{i or ''}", "command": "./run.sh studio", "last": {**last, "host": "far-host"}}
                         for i, last in enumerate(lasts)]}]}

    def test_a_hosted_tiles_button_posts_its_names_and_the_tile_reads_pending_on_its_host(self):
        docs = {"/api/place/acme/overview": [200, self.hosted({"state": "none"})],
                "/api/place/tiles": [202, {"requested": "ab12", "at": NOW, "host": "far-host", "command": "./run.sh studio"}]}
        pending = self.hosted({"state": "pending", "requested": NOW})
        out = self.run_page(self.CLICK + f"""R.before = ELS['overview-rows'].html;
DOCS['/api/place/acme/overview'] = [200, {json.dumps(pending)}];
clickAct('sim', 'Start'); await flush(); R.after = ELS['overview-rows'].html;""", docs, place="acme")
        before, after = out["R"]["before"], out["R"]["after"]
        self.assertIn("Runs on far-host; a press takes up to five minutes to start.", before)
        self.assertNotIn("aria-disabled", before, "a hosted tile's button is off")
        self.assertIn("Not run on far-host from here yet.", before)
        self.assertEqual(out["posts"], [["/api/place/tiles", {"realm": "acme", "tile": "sim", "action": "Start"}, 64]])
        self.assertEqual(out["toasts"][-1]["msg"], "Simulator · Start asked of far-host; it starts within about five minutes.")
        self.assertIn("Asked far-host <time", after)
        self.assertIn("; it starts within five minutes.", after)

    def test_a_hosted_actions_answer_reads_with_its_host(self):
        doc = self.hosted({"state": "starting", "started": NOW}, {"state": "running", "started": NOW, "output": "", "cut": False},
                          {"state": "done", "exit": 0, "started": NOW, "ended": NOW, "output": "up\n", "cut": False},
                          {"state": "refused", "reason": "example-simulator.sh studio is already running"},
                          {"state": "unknown", "reason": "its result reads state 'odd'"})
        out = self.run_page("R.rows = ELS['overview-rows'].html;", {"/api/place/acme/overview": [200, doc]}, place="acme")
        for words in ("Starting on far-host since", "Running on far-host since", "Done on far-host, exit 0",
                      "Not run on far-host: example-simulator.sh studio is already running.",
                      "Not known on far-host: its result reads state &#39;odd&#39;."):
            self.assertIn(words, out["R"]["rows"])

    def test_a_checkout_tile_shows_update_and_one_that_starts_and_stops_refresh(self):
        # Pack sd:7665: every service on a realm place gets Update (pull the latest code, restart nothing), and one
        # with Start and Stop also Refresh (stop, update, start). The Overview is the server's, from a real file.
        from sd_dashboard import place_tiles

        svc, site = self.dir / "svc", self.dir / "site"
        for folder in (svc, site):
            (folder / ".git").mkdir(parents=True)
        conf = self.dir / "place-tiles.conf"
        conf.write_text(f"tile|acme|svc|Service|{svc}|-\nact|svc|Start|./svc.sh start\nact|svc|Stop|./svc.sh stop\n"
                        f"tile|acme|site|Site|{site}|-\nact|site|Deploy|./deploy.sh\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {"XDG_STATE_HOME": str(self.dir / "state")}):
            doc = place_tiles.document("acme", now=NOW, config=conf)
        out = self.run_page(self.CLICK + "R.rows = ELS['overview-rows'].html; clickAct('svc', 'Refresh'); await flush();",
                            {"/api/place/acme/overview": [200, doc], "/api/place/tiles": [202, {"started": NOW}]}, place="acme")
        tiles = dict(part.split('"', 1) for part in out["R"]["rows"].split('data-tile="')[1:])
        self.assertIn('data-tile-act="Update" data-tip="Pulls the latest code into this folder; restarts nothing, '
                      'and refuses local changes.\ngit pull --ff-only"', tiles["svc"])
        self.assertIn('data-tile-act="Refresh" data-tip="Stops Service, runs its Update, then starts it again.', tiles["svc"])
        self.assertIn('data-tile-act="Update"', tiles["site"])
        self.assertNotIn('data-tile-act="Refresh"', tiles["site"], "a tile that starts nothing got Refresh")
        self.assertEqual(out["posts"], [["/api/place/tiles", {"realm": "acme", "tile": "svc", "action": "Refresh"}, 64]])

    ISSUES = {"state": "read", "area": "acme", "read": NOW, "reason": "", "rows": [], "done": [], "lead": False, "issues_reason": "",
              "issues_store": {"available": True, "reason": ""}, "issues": [
                  {"tracker": "Jira", "ref": "ACME-8", "key": "jira:ACME-8", "title": "Fix the kit", "repo": "acme-site", "reason": "filed",
                   "url": "https://example.test/browse/ACME-8", "hidden": True},
                  {"tracker": "GitHub", "ref": "#12", "key": "github:example/acme-site#12", "title": "Crash <b>on</b> start", "repo": "acme-site",
                   "reason": "assigned", "url": "https://github.com/example/acme-site/issues/12", "hidden": False}]}

    def test_tasks_lists_the_realms_issues_with_their_source_and_hidden_ones_under_hidden(self):
        docs = {"/api/place/acme/overview": [200, self.TILES], "/api/place/acme/tasks": [200, self.ISSUES]}
        out = self.run_page("""await go('#view=tasks'); R.rows = ELS['tasks-rows'].html;
const b = { dataset: { v: 'aside' } };
(DOC_LISTENERS['click'] || []).forEach(f => f({ button: 0, target: { closest: sel => sel === 'button[data-aside="issues-hidden"]' ? b : null } }));
await flush(); R.all = ELS['tasks-rows'].html;""", docs, place="acme")
        rows, aside = out["R"]["rows"], out["R"]["all"]
        self.assertIn("<h3>Issues</h3>", rows)
        self.assertIn('<td class="src">GitHub</td>', rows)
        self.assertIn('<a href="https://github.com/example/acme-site/issues/12" target="_blank" rel="noopener">#12</a>', rows)
        self.assertIn("Crash &lt;b&gt;on&lt;/b&gt; start", rows)
        self.assertNotIn("ACME-8", rows, "a hidden issue showed by default")
        self.assertIn('data-aside="issues-hidden" data-v="aside" aria-pressed="false">Hidden <span class="n">1</span>', rows)
        # pack sd:7210: the Issues table's column filter joins its Open | Hidden row, not a row of its own.
        self.assertIn('<h3>Issues</h3><div class="filters toolbar-2" id="place-issues-filters">', rows)
        self.assertIn('<table class="ledger place-issues" data-toolbar="place-issues-filters">', rows)
        self.assertIn('<td class="src">Jira</td>', aside)
        self.assertIn("ACME-8", aside)
        self.assertNotIn("#12", aside)
        self.assertIn('data-v="aside" aria-pressed="true"', aside)
        self.assertNotIn('data-aside="issues-hidden" data-v="all"', rows)

    def test_a_refused_or_unanswered_start_says_why_and_an_off_button_posts_nothing(self):
        out = self.run_overview("clickAct('app', 'Deploy'); await flush();", answer=(409, {"error": "Staging site Deploy is already running"}))
        self.assertEqual(out["toasts"][-1]["msg"], "Staging site · Deploy not run: Staging site Deploy is already running")
        out = self.run_overview("clickAct('far', 'Start', '/x is not on this machine'); await flush();")
        self.assertEqual((out["posts"], out["toasts"][-1]["msg"]), ([], "Simulator · Start not run: /x is not on this machine."))


class TheTasksRealm(Fixture):
    """sd:6263: Tasks' realm sub tabs (Personal, <realms>, HOA and Home; pack sd:7216 dropped All), in place of sd:5753's
    Realm filter: tasks.js against the rows `tasks_screen` builds."""

    #: The realms /ui/sections.js hands the page, and the realms.conf line that puts /repos/system in Work.
    REALMS = "globalThis.SHELL_REALMS = [{ id: 'work', label: 'Work' }];\n"
    #: A realm tab shown, as the shell's switch shows it.
    PICK = "const realm = async v => { window.shell.view.show(v); await flush(); };\n"
    TITLES = ("Fix the valve log", "Clean the gutters", "Tune the gate")

    def realm_doc(self):
        conf = self.dir / "realms.conf"
        conf.write_text("realm|work|Work|/repos/sys*\n", encoding="utf-8")
        with mock.patch.object(places, "REALMS_CONFIG", conf):
            return tasks_screen.document(self.connection, now=NOW)

    def run_tasks(self, body, **kw):
        kw.setdefault("prelude", self.REALMS)
        return test_v2_tasks.TheScript.run_page(self, self.PICK + body, **kw)

    def test_each_realm_is_a_sub_tab_that_narrows_the_board_and_counts_its_tasks(self):
        self.doc, self.details = self.realm_doc(), {}
        out = self.run_tasks("""R.first = ELS['view-board'].html; R.filters = ELS.filters.html; R.urls = [OUT.urls[OUT.urls.length - 1]];
R.views = window.PAGE_VIEWS.map(v => [v.id, v.label, v.key]);
for (const v of ['work', 'hoa', 'home', 'personal']) { await realm(v); R[v] = ELS['view-board'].html; R.urls.push(OUT.urls[OUT.urls.length - 1]); }""")
        r = out["R"]
        # The tabs: Personal, each dashboard.realms realm, HOA and Home, each with its v key off l, b and m. pack sd:7216:
        # no All; every task sits in Personal or a realm.
        self.assertEqual(r["views"], [["personal", "Personal", "p"], ["work", "Work", "w"], ["hoa", "HOA", "h"],
                                      ["home", "Home", "o"]])
        # Realm is no filter now: the bar holds Kind, Repo and the rest, in every tab.
        self.assertNotIn('data-pick="realm"', r["filters"])
        self.assertIn('data-pick="kind"', r["filters"])
        # sd:5064: Personal, the first tab, is every row no realm claims: the one with no repo. pack sd:7687: the hoa
        # checkout's row is realm hoa, so it sits in HOA alone, not in Personal too.
        self.assertEqual([t in r["first"] for t in self.TITLES], [False, True, False])
        self.assertEqual([t in r["work"] for t in self.TITLES], [False, False, True])
        self.assertEqual([t in r["hoa"] for t in self.TITLES], [True, False, False])
        self.assertEqual([t in r["home"] for t in self.TITLES], [False, True, False])
        self.assertEqual([t in r["personal"] for t in self.TITLES], [False, True, False])
        # Each tab carries its tasks (sd:5524); the shell keeps the tab in #view=, so the query names no realm.
        # The report row is in no realm (places.realm_of), so it sits in Personal too.
        # pack sd:7687: the hoa checkout's open and done rows left Personal for HOA (Personal held 4).
        # sd:8018: HOA counts its open row only; the done one stays on the board.
        self.assertEqual(out["counts"], {"personal": 2, "work": 1, "hoa": 1, "home": 1})
        self.assertEqual(r["urls"], ["view=board"] * 5)

    def test_a_vault_task_with_its_own_home_area_sits_in_home_alone(self):
        """pack sd:8010: a vault task tagged home (`sd task edit N --area home`) shows under Home and in no Personal or
        realm tab; an untagged vault task stays in Personal, and the row with no repo stays in Personal and Home."""
        vault = self.repo("/repos/vault")
        upkeep = self.item("Flush the water heater", kind="task", repo=vault)
        self.item("Plan the trip", kind="task", repo=vault)
        work = self.item("Tag the work task", kind="task", repo=self.other)
        workflow.edit_item(self.connection, upkeep, {"area": "home"}, who="test")
        workflow.edit_item(self.connection, work, {"area": "home"}, who="test")
        self.doc, self.details = self.realm_doc(), {}
        out = self.run_tasks("for (const v of ['personal', 'work', 'hoa', 'home']) { await realm(v); R[v] = ELS['view-board'].html; }")
        r, titles = out["R"], ("Flush the water heater", "Plan the trip", "Clean the gutters", "Tag the work task")
        self.assertEqual([t in r["personal"] for t in titles], [False, True, True, False])
        self.assertEqual([t in r["work"] for t in titles], [False, False, False, False])
        self.assertEqual([t in r["hoa"] for t in titles], [False, False, False, False])
        self.assertEqual([t in r["home"] for t in titles], [True, False, True, True])
        self.assertEqual(out["counts"], {"personal": 3, "work": 1, "hoa": 1, "home": 3})

    def test_a_task_tagged_personal_or_a_realm_sits_in_that_tab_alone(self):
        """pack sd:8062: the Work repo's task tagged personal sits in Personal, not Work; a task with no repo tagged
        work sits in Work, not Personal or Home; a tag naming a realm with no tab falls back to the repo rule."""
        writes.set_setting(self.connection, "dashboard.realms", "realm|work|Work|/repos/sys*\n")
        errand = self.item("Buy stamps", kind="task")
        workflow.edit_item(self.connection, self.other_item, {"area": "personal"}, who="test")
        workflow.edit_item(self.connection, errand, {"area": "work"}, who="test")
        self.doc, self.details = self.realm_doc(), {}
        stale = self.item("Water the ferns", kind="task", repo=self.other)
        self.doc["rows"] += [{**next(r for r in self.doc["rows"] if r["id"] == self.other_item), "id": stale,
                              "title": "Water the ferns", "area": "gone", "area_set": True, "realm": "work"}]
        out = self.run_tasks("for (const v of ['personal', 'work', 'home']) { await realm(v); R[v] = ELS['view-board'].html; }")
        r, titles = out["R"], ("Tune the gate", "Buy stamps", "Clean the gutters", "Water the ferns")
        self.assertEqual([t in r["personal"] for t in titles], [True, False, True, False])
        self.assertEqual([t in r["work"] for t in titles], [False, True, False, True])
        self.assertEqual([t in r["home"] for t in titles], [False, False, True, False])

    def test_an_address_or_an_older_link_lands_on_its_realm(self):
        """Metrics and Today link /tasks?...&realm=all, and a bookmark may hold #view=hoa or #view=home. pack sd:7216: the
        All tab is gone: ?realm=all still lists every realm, and #view=all opens Personal, the address saying #view=personal."""
        self.doc, self.details = self.realm_doc(), {}
        prelude = self.REALMS + "var history = { state: null, replaceState: (s, t, u) => { OUT.replaced = u; location.hash = u; } };\n"
        for search, hash_, shown in (("?realm=hoa", "", [True, False, False]), ("", "#view=all", [False, True, False]),
                                     ("?realm=all", "", [True, True, True]), ("?realm=all", "#view=work", [True, True, True]),
                                     ("", "#view=hoa", [True, False, False]), ("", "#view=home", [False, True, False]),
                                     ("", "#view=work", [False, False, True]), ("?realm=home", "#view=all", [False, True, False]),
                                     ("", "#view=nope", [False, True, False]), ("?realm=nope", "", [False, True, False])):
            with self.subTest(search=search, hash=hash_):
                out = self.run_tasks("R.board = ELS['view-board'].html;", search=search, hash=hash_, prelude=prelude)
                self.assertEqual([t in out["R"]["board"] for t in self.TITLES], shown)
                self.assertEqual(out.get("replaced"), "#view=personal" if hash_ == "#view=all" else None)

    def test_a_today_drilldown_with_realm_all_lists_every_realm_until_a_tab_is_picked(self):
        """pack sd:7216: Today's and Metrics' drilldowns count every realm and link ?realm=all. With no All tab the list
        still holds every realm's rows, a work-realm item too; no tab is current and the head says All realms. A filter
        keeps ?realm=all in the address; a tab picked, Personal too, leaves it."""
        self.doc, self.details = self.realm_doc(), {}
        out = self.run_tasks("""R.cur = window.shell.view.current(); R.list = ELS['view-list'].html; R.sub = ELS.subhead.html;
const box = document.getElementById('find-in'); box.value = 'the'; box.listeners.input[0]({ target: box }); await flush();
R.kept = OUT.urls[OUT.urls.length - 1]; box.value = ''; box.listeners.input[0]({ target: box }); await flush();
await realm('personal'); R.cur2 = window.shell.view.current(); R.list2 = ELS['view-list'].html; R.sub2 = ELS.subhead.html;
R.left = OUT.urls[OUT.urls.length - 1];""", search="?view=list&realm=all")
        r = out["R"]
        self.assertEqual([t in r["list"] for t in self.TITLES], [True, True, True])
        self.assertIsNone(r["cur"])
        self.assertIn("All realms", r["sub"])
        self.assertIn("realm=all", r["kept"])
        self.assertEqual([t in r["list2"] for t in self.TITLES], [False, True, False])
        self.assertEqual(r["cur2"], "personal")
        self.assertNotIn("All realms", r["sub2"])
        self.assertNotIn("realm=all", r["left"])

    def test_a_realm_with_a_taken_id_is_dropped(self):
        """A realm named for a tab Tasks owns, or for a layout panel (list, board, matrix, btn, menu), takes no tab."""
        self.doc, self.details = self.realm_doc(), {}
        prelude = ("globalThis.SHELL_REALMS = [{ id: 'all', label: 'Sneaky' }, { id: 'hoa', label: 'Hoa2' }, { id: 'list', label: 'List' },"
                   " { id: 'Bad id', label: 'Bad' }, { id: 'work', label: 'Work' }, { id: 'menu', label: 'Menu' }];\n")
        views = self.run_tasks("R.views = window.PAGE_VIEWS.map(v => v.id);", prelude=prelude)["R"]["views"]
        self.assertEqual(views, ["personal", "work", "hoa", "home"])

    def test_without_realms_conf_personal_holds_every_row_but_hoas_and_says_why(self):
        # pack sd:7216: a row in no realm tab (no realm list read, or a realm with no tab) sits in Personal, not only All.
        # pack sd:7687: the hoa checkout's row is HOA's without a realm list too.
        with mock.patch.object(places, "REALMS_CONFIG", self.dir / "realms.conf"):
            self.doc, self.details = tasks_screen.document(self.connection, now=NOW), {}
        r = self.run_tasks("R.board = ELS['view-board'].html; R.sub = ELS.subhead.html; await realm('work'); R.work = ELS['view-board'].html;")["R"]
        self.assertEqual([t in r["board"] for t in self.TITLES], [False, True, True])
        self.assertEqual([t in r["work"] for t in self.TITLES], [False, False, False])
        self.assertIn("repos in no realm: no realm list; set dashboard.realms on Settings", r["sub"])

    def test_personal_and_work_carry_their_sweep_and_run_sweep_posts_the_realm(self):
        """sd:5096, sd:5097: the subhead names the realm's last sweep on Personal and Work only; Run sweep posts the
        realm, and is off while that realm's run goes. A running sweep reads again after SWEEP_POLL, not at once."""
        self.doc, self.details = self.realm_doc(), {}
        never = {"running": False, "error": "", "command": "python3 -m sd_dashboard.realm_sweep run personal",
                 "last": {"state": "never"}}
        going = {"running": True, "error": "", "command": "python3 -m sd_dashboard.realm_sweep run work",
                 "last": {"state": "running", "started": "2026-09-06T11:30:00+00:00"}}
        self.doc["sweeps"] = {"personal": never, "work": going}
        prelude = self.REALMS + "var TIMERS = []; setTimeout = (f, ms) => { TIMERS.push(ms); return TIMERS.length; };\n"
        answer = "(path, body) => path === '/api/tasks/sweep' ? [202, { started: '2026-09-06T12:01:00+00:00', repo: '/x', command: 'c' }] : null"
        out = self.run_tasks("""R.personal = ELS.subhead.html;
await realm('work'); R.work = ELS.subhead.html;
await realm('hoa'); R.hoa = ELS.subhead.html;
R.objects = [...OBJ.keys()].filter(k => k.startsWith('sweep:')); R.timers = TIMERS.slice();
C.run(cmd('realm-sweep.run'), C.get('sweep:work')); await flush();
C.run(cmd('realm-sweep.run'), C.get('sweep:personal')); await flush();""", answer=answer, prelude=prelude)
        r = out["R"]
        self.assertIn('data-sweep="personal">Personal sweep not run yet', r["personal"])
        self.assertIn('data-sweep="work">Work sweep running since 11:30 UTC', r["work"])
        self.assertNotIn("data-sweep", r["hoa"])
        self.assertEqual(sorted(r["objects"]), ["sweep:personal", "sweep:work"])
        self.assertEqual(r["timers"], [15000])
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/tasks/sweep", {"realm": "personal"}]])
        self.assertIn(["off: a sweep is running now", False], out["toasts"])
        self.assertIn(["Personal sweep started 12:01 UTC · Tasks reads again until it ends", False], out["toasts"])

    def test_a_done_sweep_names_the_sources_it_did_not_read(self):
        """sd:5096 review: a done run with a gap says so, rather than a plain done that hides an unread source."""
        self.doc, self.details = self.realm_doc(), {}
        done = {"running": False, "error": "", "command": "c",
                "last": {"state": "done", "finished": "2026-09-06T11:40:00+00:00", "gaps": ["jira", "github"]}}
        self.doc["sweeps"] = {"personal": done, "work": {**done, "last": {**done["last"], "gaps": []}}}
        r = self.run_tasks("R.personal = ELS.subhead.html; await realm('work'); R.work = ELS.subhead.html;")["R"]
        self.assertIn("Personal sweep done 2026-09-06 11:40 UTC · not read: jira, github", r["personal"])
        self.assertIn("Work sweep done 2026-09-06 11:40 UTC ", r["work"])
        self.assertNotIn("not read", r["work"])


PLACE_JS = (STATIC / "place.js").read_text(encoding="utf-8")
SHELL_PLACES = re.search(r"^  // places:start\n(.*?)^  // places:end$", (STATIC / "shell.js").read_text(encoding="utf-8"), re.S | re.M)


class TheRealmPlaceScripts(unittest.TestCase):
    """sd:5663: place.js names the page and its views after ?realm=, and shell.js puts each realm place in the rail."""

    def run_script(self, script):
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out["R"]

    def place(self, search, realms):
        return self.run_script(STAND_IN + MARKUP_JS + f"""
location.search = {json.dumps(search)}; window.SHELL_REALMS = {json.dumps(realms)};
document.documentElement = {{ hasAttribute: () => false }};
const BLOCKS = ['tasks', 'briefs', 'reports'].map(v => Object.assign(El('view-' + v), {{ dataset: {{ placeView: v, label: v }} }}));
document.querySelectorAll = sel => sel === '[data-place-view]' ? BLOCKS : [];
var R = {{}};
try {{
{PLACE_JS}
{PLACES_JS}
R.page = document.body.dataset.page || null; R.place = document.body.dataset.place || null; R.title = document.title || null;
R.h1 = ELS.title ? ELS.title.textContent : null; R.sub = ELS.subhead ? ELS.subhead.textContent : null;
R.again = !!window.PAGE_AGAIN; R.views = window.PAGE_VIEWS || null; R.state = window.PAGE_STATE ? [window.PAGE_STATE.title, String(window.PAGE_STATE.text.text || window.PAGE_STATE.text)] : null;
}} catch (e) {{ OUT.error = String(e) + ' ' + e.stack; }}
function run() {{ OUT.R = R; return JSON.stringify(OUT); }}
""")

    def test_a_realm_place_names_the_page_and_points_each_view_at_its_route(self):
        r = self.place("?realm=acme", [{"id": "acme", "label": "Acme", "place": "map"}])
        self.assertEqual((r["page"], r["place"], r["title"], r["h1"]), ("Acme", "acme", "Acme · system", "Acme"))
        self.assertEqual(r["views"], [{"id": v, "label": v, "api": f"/api/place/acme/{v}"} for v in ("tasks", "briefs", "reports")])
        self.assertIsNone(r["state"])
        self.assertTrue(r["again"], "a realm place declares no Read again (sd:5638)")

    def test_a_realm_without_a_family_or_not_listed_keeps_its_views_and_says_why(self):
        r = self.place("?realm=acme", [{"id": "acme", "label": "Acme", "place": None}])
        self.assertIn("dashboard.report_families has no family acme", r["sub"])
        self.assertEqual(len(r["views"]), 3)
        r = self.place("?realm=acme", [])
        self.assertEqual((r["page"], r["h1"]), ("acme", "acme"))
        self.assertIn("dashboard.realms names no realm acme", r["sub"])
        self.assertEqual(r["views"][0]["api"], "/api/place/acme/tasks")
        # A label the rail could not find by [data-section="..."] is left to the id.
        self.assertEqual(self.place("?realm=acme", [{"id": "acme", "label": 'A"cme', "place": "map"}])["page"], "acme")

    def test_without_a_realm_the_page_lists_the_realm_places_and_reads_no_view(self):
        r = self.place("", [{"id": "acme", "label": "Acme", "place": "map"}, {"id": "lab", "label": "Lab", "place": None}])
        self.assertEqual((r["place"], r["views"], r["state"][0], r["again"]), (None, None, "Pick a place", False))
        self.assertIn('href="/places?realm=acme">Acme</a>', r["state"][1])
        self.assertNotIn("Lab", r["state"][1])
        r = self.place("?realm=../x", [])
        self.assertEqual((r["place"], r["state"][0]), (None, "No realm place"))

    def test_the_rail_lists_each_realm_place_under_places_with_no_g_key(self):
        self.assertIsNotNone(SHELL_PLACES, "shell.js has no places:start block")
        realms = [{"id": "acme", "label": "Acme", "place": "map"}, {"id": "lab", "label": "Lab", "place": None},
                  {"id": "tasks", "label": "Tasks", "place": "map"}, {"id": "q", "label": 'Q"', "place": "map"},
                  {"id": "bad", "label": "Bad", "place": "x y"}]
        r = self.run_script(f"""var window = globalThis, OUT = {{ error: null }}, R = {{}};
window.SHELL_REALMS = {json.dumps(realms)};
const PAGES = {{ Tasks: '/tasks', HOA: '/hoa', Home: '/home', Places: '/places' }};
const GROUPS = [['Work', [['Tasks', 'list-todo', 'k']]], ['Places', [['HOA', 'droplets', 'h'], ['Home', 'house', 'o']]]];
try {{
{SHELL_PLACES.group(1)}
R.places = GROUPS[1][1]; R.pages = PAGES;
}} catch (e) {{ OUT.error = String(e) + ' ' + e.stack; }}
function run() {{ OUT.R = R; return JSON.stringify(OUT); }}
""")
        self.assertEqual(r["places"], [["HOA", "droplets", "h"], ["Home", "house", "o"], ["Acme", "map", ""]])
        self.assertEqual(r["pages"]["Acme"], "/places?realm=acme")
        self.assertEqual(r["pages"]["Tasks"], "/tasks")

    def test_place_js_sets_the_views_before_places_js_reads_them(self):
        text = (STATIC.parent / "place.html").read_text(encoding="utf-8")
        self.assertLess(text.index('src="/ui/sections.js"'), text.index('src="/ui/place.js"'))
        self.assertLess(text.index('src="/ui/place.js"'), text.index('src="/ui/places.js"'))

    def test_a_rail_entry_or_palette_row_without_a_key_draws_no_kbd(self):
        shell = (STATIC / "shell.js").read_text(encoding="utf-8")
        self.assertIn("${key ? html`<kbd>g ${key}</kbd>` : ''}", shell)
        self.assertIn("${k ? html`<kbd>g ${k}</kbd>` : ''}", shell)
        self.assertIn(".filter(([, , k]) => k).map(([k2, , k]) => [k, k2])", shell)


class ThePages(unittest.TestCase):
    def test_page_views_are_declared_before_shell_js_and_each_page_has_the_switch_host(self):
        for page, script in (("hoa", "places.js"), ("home", "places.js"), ("place", "place.js"), ("place", "places.js")):
            text = (STATIC.parent / f"{page}.html").read_text(encoding="utf-8")
            self.assertLess(text.index(f'src="/ui/{script}"'), text.index('src="/ui/shell.js"'), page)
            self.assertEqual(text.count('id="viewswitch"'), 1, page)

    def test_tasks_hosts_its_realm_tabs_before_shell_js(self):
        """sd:6263: Tasks' realms are sub tabs that filter one board, so the switch goes in its #viewswitch host, and
        tasks.js declares PAGE_VIEWS before shell.js reads it."""
        text = (STATIC.parent / "tasks.html").read_text(encoding="utf-8")
        self.assertEqual(text.count('id="viewswitch"'), 1)
        self.assertLess(text.index('src="/ui/tasks.js"'), text.index('src="/ui/shell.js"'))
        self.assertIn("window.PAGE_VIEWS = [", (STATIC / "tasks.js").read_text(encoding="utf-8"))

    def test_the_overviews_read_state_shows_with_the_overview_only(self):
        # The page's #state names the Overview's read (/api/hoa, /api/home); on Briefs it would describe another read.
        for page in ("hoa", "home"):
            text = (STATIC.parent / f"{page}.html").read_text(encoding="utf-8")
            overview, state, other = (text.index(mark) for mark in ('data-place-view="overview"', 'id="state"', 'data-place-view="tasks"'))
            self.assertLess(overview, state, page)
            self.assertLess(state, other, page)


class Votes(Fixture):
    """pack sd:8050: HOA's Votes view is the hoa checkout's `vote` rows, open first, then newest first. Read-only."""

    def add(self, key, **fields):
        return sd_votes.add(self.connection, self.hoa, key, fields)

    def test_hoa_votes_are_the_checkouts_open_first_then_newest(self):
        self.add("OLD", date="2025-03-01", outcome="adopted")
        self.add("NEW", date="2026-03-01", outcome="failed")
        self.add("OPEN", date="2024-01-01", outcome="pending: not voted")
        sd_votes.add(self.connection, self.other, "ELSE", {"outcome": "pending"})
        doc = places.votes(self.connection, "hoa", now=NOW)
        self.assertEqual((doc["state"], [r["key"] for r in doc["rows"]]), ("read", ["OPEN", "NEW", "OLD"]))

    def test_without_a_registered_hoa_checkout_votes_are_unknown_with_the_reason(self):
        bare = ScreenCase()
        bare.setUp()
        self.addCleanup(bare.doCleanups)
        doc = places.votes(bare.connection, "hoa", now=NOW)
        self.assertEqual((doc["state"], doc["rows"]), ("unknown", []))
        self.assertIn("no repo named hoa", doc["reason"])
        self.assertEqual(places.votes(self.connection, "home", now=NOW)["state"], "unknown")

    def test_a_store_or_library_without_the_vote_table_is_an_error_not_an_empty_list(self):
        """Older state: a dashboard on an sd_db before 048, or a store not yet migrated."""
        with mock.patch.object(sd_votes, "rows", side_effect=sqlite3.OperationalError("no such table: vote")):
            doc = places.votes(self.connection, "hoa", now=NOW)
        self.assertEqual((doc["state"], doc["rows"]), ("error", []))
        self.assertIn("no such table: vote", doc["reason"])
        with mock.patch.dict("sys.modules", {"sd_db.votes": None}):
            doc = places.votes(self.connection, "hoa", now=NOW)
        self.assertEqual(doc["state"], "error")
        self.assertIn("migration 048", doc["reason"])


    # pack sd:8058: Add and Edit post /api/hoa/vote; an edit names the revision it read, as `sd votes edit --if-revision`.
    def test_a_vote_write_adds_then_edits_at_the_revision_read(self):
        added = places.write_vote({"key": "CI-2", "revision": None, "fields": {"motion": "Buy it.", "outcome": "pending: not sent"}})(self.connection)
        self.assertEqual((added["vote"]["revision"], added["vote"]["state"], added["vote"]["repo"]), (1, "open", self.hoa))
        edited = places.write_vote({"key": "CI-2", "revision": 1, "fields": {"outcome": "adopted"}})(self.connection)
        self.assertEqual((edited["vote"]["revision"], edited["vote"]["state"], edited["vote"]["motion"]), (2, "closed", "Buy it."))
        closed = places.write_vote({"key": "CI-2", "revision": 2, "fields": {"state": "open"}})(self.connection)
        self.assertEqual((closed["vote"]["revision"], closed["vote"]["state"]), (3, "open"))

    def test_a_stale_vote_edit_is_refused_as_stale_and_writes_nothing(self):
        self.add("CI-3", motion="Buy it.")
        sd_votes.edit(self.connection, self.hoa, "CI-3", {"notes": "moved"})
        with self.assertRaises(workflow.StaleItem):
            places.write_vote({"key": "CI-3", "revision": 1, "fields": {"outcome": "adopted"}})(self.connection)
        self.assertEqual(sd_votes.get(self.connection, self.hoa, "CI-3")["revision"], 2)
        with self.assertRaises(sd_votes.VoteRefused):
            places.write_vote({"key": "CI-3", "revision": None, "fields": {"motion": "again"}})(self.connection)

    def test_a_malformed_vote_write_is_refused_before_it_runs(self):
        for payload in ({"key": "A", "fields": {}}, {"key": 1, "revision": None, "fields": {}},
                        {"key": "A", "revision": True, "fields": {}}, {"key": "A", "revision": None, "fields": {"motion": 3}},
                        {"key": "A", "revision": None, "fields": []}, {"key": "A", "revision": None, "fields": {}, "x": 1}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                places.write_vote(payload)


class TheRule(unittest.TestCase):
    def test_area_of(self):
        self.assertEqual(places.area_of("/repos/hoa", "task", "/repos/hoa"), "hoa")
        self.assertEqual(places.area_of(None, "task", "/repos/hoa"), "home")
        self.assertEqual(places.area_of("", "followup", None), "home")
        self.assertIsNone(places.area_of(None, "report", "/repos/hoa"))
        self.assertIsNone(places.area_of("/repos/system", "task", "/repos/hoa"))
        self.assertIsNone(places.area_of("/repos/hoa", "task", None))

    def test_area_of_expands_a_tilde_on_either_side_as_realm_of_does(self):
        # sd:7707: a row stored as ~/repos/hoa against an absolute registered path (or the reverse) is still HOA's.
        home = str(Path.home() / "repos/hoa")
        self.assertEqual(places.area_of("~/repos/hoa", "task", home), "hoa")
        self.assertEqual(places.area_of(home, "task", "~/repos/hoa"), "hoa")
        self.assertIsNone(places.area_of("~/repos/hoa-two", "task", home))

    def test_an_items_own_area_wins_over_the_repo_rule_and_a_report_stays_in_none(self):
        """pack sd:8010: `own` is the item's `fields.area`; it places the row whatever its repo, a report excepted."""
        self.assertEqual(places.area_of("/repos/vault", "task", "/repos/hoa", "home"), "home")
        self.assertEqual(places.area_of("/repos/hoa", "task", "/repos/hoa", "home"), "home")
        self.assertEqual(places.area_of("/repos/vault", "followup", None, "hoa"), "hoa")
        self.assertEqual(places.area_of(None, "task", None, "hoa"), "hoa")
        self.assertIsNone(places.area_of("/repos/vault", "report", "/repos/hoa", "home"))
        self.assertEqual(places.area_of(None, "task", None, None), "home")

    def test_a_series_names_a_job_lower_cased_with_hyphens(self):
        self.assertEqual(places.series_job("HOA Morning Digest"), "hoa-morning-digest")
        self.assertEqual(places.series_job("  Fun  Events Nightly "), "fun-events-nightly")


if __name__ == "__main__":
    unittest.main()
