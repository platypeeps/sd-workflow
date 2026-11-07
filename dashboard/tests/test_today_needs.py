"""Needs you (sd:5921): Today's one ranked list of what asks the operator to act, and the badge that counts it.

The list is `now_screen.document`'s `needs`: Now's rows, the open problem reports, the mail waiting, each lane's blocked
entries, and the work items and briefs as group rows. The rail badge counts its broken rows, else its look rows, and
nothing the list does not show. A lane read that failed is a dark row, never an empty lane.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from unittest import mock

from sd_dashboard import now_screen, queue_screen
from support import NOW
from test_now_screen import JobsBackend, fleet_document
from test_today_reports import ReportsCase

# support.ScreenCase stands in for the lane read; this is the one the server calls.
SERVER_LANES = now_screen._lanes


def lane(repo="system", rows=(), revision="r1"):
    return {"repo": repo, "path": f"/repos/{repo}", "revision": revision, "runner_merge": "auto", "rows": list(rows)}


def blocked(item, *, head="a" * 40, reason="sd-check: a suite failed", step="prepare"):
    return {"item": item, "title": f"item {item}", "state": "blocked", "status": "failed", "step": step, "reason": reason,
            "who": "lane", "finished": "2026-09-06T09:00:00Z", "head": head[:12], "expected_head": head}


def queue(*lanes, gates=None):
    return lambda connection, now: {"lanes": list(lanes), "gates": gates or {"running": 1, "cap": 3, "waiting": 0},
                                    "load": {"load5": 4.0}, "problems": []}


def work(item, *, state="planning", due=None, repo="system", priority=None, recurring=False):
    return {"id": f"item:{item}", "kind": "item", "item": item, "what": f"#{item} task {item}", "detail": state,
            "state": state, "due": due, "item_kind": "work", "revision": "r", "repo": repo, "priority": priority,
            "recurring": recurring, "since": "2026-09-01 10:00:00"}


class TheBadge(ReportsCase):
    def today(self, lanes=None):
        # No seam reads the lanes as the server does: this fixture registers no managed repository.
        return now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name), fleet=fleet_document,
                                   **({"lanes": lanes} if lanes else {}))

    def test_the_badge_counts_only_rows_needs_you_lists(self):
        # On origin/main the badge counted an open report that Now never listed (sd:5921).
        item = self.report("nas-prune", "r1")
        doc = self.today()
        listed = [row["id"] for row in doc.get("needs", doc["rows"])]
        self.assertIn(f"report:{item}", listed)
        look = sum(row["band"] == "look" for row in doc["needs"])
        self.assertEqual((doc["attention"]["state"], doc["attention"]["n"]), ("caution", look))

    def test_a_ready_grill_is_one_queued_row_that_says_how_to_start_it(self):
        """pack sd:6022: the KISS audit files the weekly grill's questions; Today says it is ready, once."""
        from sd_db import create_item
        from sd_db.writes import add_note

        grilled = create_item(self.connection, kind="task", title="Weekly grill", source="task-ref",
                              external_id="grill:2026-09-07")
        for number in range(3):
            add_note(self.connection, grilled, "question", f"Keep mechanism {number}?\nOption: Keep (Recommended)\nOption: Drop")
        rows = [row for row in self.today()["needs"] if row["id"] == f"grill:{grilled}"]
        self.assertEqual([(row["band"], row["item"], row["what"], row["detail"]) for row in rows],
                         [("queued", grilled, "Grill ready: 3 questions", "say 'grill' in a fresh session")])

    def base_row(self, kind, stage, title):
        from sd_db import create_item

        return create_item(self.connection, kind="idea", title=title, status="ready", source="vault",
                           external_id=f"{kind}:{title}", path=f"{kind}/{title}.md", stage=stage, fields={"kind": kind})

    def test_the_decision_queues_are_one_queued_row_that_links_each_home(self):
        """sd:8013: the Tasks page's Queues fold is one Needs you line, each queue linking to where it is decided."""
        for title in ("Idea one", "Idea two"):
            self.base_row("blog-idea", "inbox", title)
        self.base_row("blog-idea", "accepted", "Taken idea")
        self.base_row("tip", "inbox", "A tip")
        self.base_row("market", "candidate", "Example Co")
        self.base_row("market", "active", "Watched Co")
        rows = [row for row in self.today()["needs"] if row["id"] == "triage"]
        self.assertEqual([(row["band"], row["what"], row["detail"]) for row in rows],
                         [("queued", "4 waiting on a decision", "Blog Ideas 2 · Tips 1 · Market Watch 1")])
        self.assertEqual(rows[0]["links"], [
            {"label": "Blog Ideas", "n": 2, "href": "/writing#ideas"},
            {"label": "Tips", "n": 1, "href": "/writing#tips"},
            {"label": "Market Watch", "n": 1, "href": "/watches?stage=candidate"}])

    def test_no_decision_waiting_raises_no_row_and_a_failed_read_is_dark(self):
        self.assertNotIn("triage", [row["id"] for row in self.today()["needs"]])
        with mock.patch("sd_db.records.list_rows", side_effect=sqlite3.OperationalError("database is locked")):
            dark = [row for row in self.today()["needs"] if row["id"] == "dark:queues"]
        self.assertEqual([(row["band"], row["detail"]) for row in dark], [("broken", "database is locked")])

    def test_a_grill_read_that_failed_is_a_dark_row(self):
        with mock.patch("sd_db.today.grill", side_effect=sqlite3.OperationalError("database is locked")):
            dark = [row for row in self.today()["needs"] if row["id"] == "dark:grill"]
        self.assertEqual([(row["band"], row["detail"]) for row in dark], [("broken", "database is locked")])

    def test_a_snoozed_grill_row_carries_a_seen_and_leaves_the_list(self):
        """pack sd:6235: the Grill ready row joined Needs you after the snooze, so it had no seen and never hid."""
        from sd_db import create_item
        from sd_db.writes import add_note

        grilled = create_item(self.connection, kind="task", title="Weekly grill", source="task-ref",
                              external_id="grill:2026-09-07")
        add_note(self.connection, grilled, "question", "Keep mechanism?\nOption: Keep (Recommended)\nOption: Drop")
        row = next(row for row in self.today()["needs"] if row["id"] == f"grill:{grilled}")
        self.assertTrue(row.get("seen"), "the Grill ready row carries no seen, so Snooze posts none")
        self.post_snooze(f"grill:{grilled}", row["seen"])
        doc = self.today()
        self.assertNotIn(f"grill:{grilled}", [row["id"] for row in doc["needs"]])
        self.assertIn(f"grill:{grilled}", [row["id"] for row in doc["snoozed"]])
        add_note(self.connection, grilled, "question", "Keep another?\nOption: Keep (Recommended)\nOption: Drop")
        self.assertIn(f"grill:{grilled}", [row["id"] for row in self.today()["needs"]])

    def test_a_blocked_lane_entry_is_a_broken_row_and_outranks_a_report(self):
        self.report("nas-prune", "r1")
        doc = self.today(queue(lane(rows=[blocked(5780)])))
        first = doc["needs"][0]
        self.assertEqual((first["kind"], first["band"], first["lamp"], first["item"]), ("lane", "broken", "lane", 5780))
        self.assertEqual(first["expected_head"], "a" * 40)
        self.assertEqual(doc["attention"], {"state": "warning", "n": 1, "what": "broken rows"})
        self.assertEqual((doc["lane"]["blocked"], doc["lane"]["gates"]["cap"]), (1, 3))

    def test_a_lane_read_that_failed_is_a_dark_row_never_an_empty_lane(self):
        def broken(connection, now):
            raise OSError("sd-ship gave no answer")

        doc = self.today(broken)
        self.assertEqual(doc["sources"]["lane"], "sd-ship gave no answer")
        dark = [row for row in doc["needs"] if row["id"] == "dark:lane"]
        self.assertEqual([(row["band"], row["lamp"]) for row in dark], [("broken", "lane")])

    def test_a_snoozed_report_leaves_the_list_and_the_badge(self):
        item = self.report("nas-prune", "r1")
        row = next(row for row in self.today()["needs"] if row["id"] == f"report:{item}")
        self.post_snooze(f"report:{item}", row["seen"])
        doc = self.today()
        self.assertNotIn(f"report:{item}", [row["id"] for row in doc["needs"]])
        self.assertIn(f"report:{item}", [row["id"] for row in doc["snoozed"]])
        self.assertEqual(doc["attention"]["state"], "ok")

    def test_with_no_seam_today_reads_queues_document(self):
        with mock.patch.object(queue_screen, "_gates", return_value={"running": 2, "cap": 3, "waiting": 1}), \
                mock.patch.object(queue_screen, "retry_help", return_value={"retry": True, "requeue": True, "requeue_off": ""}):
            doc = self.today(SERVER_LANES)
        self.assertEqual((doc["sources"]["lane"], doc["lane"]["gates"]), ("", {"running": 2, "cap": 3, "waiting": 1}))

    def post_snooze(self, row, seen):
        from sd_db import writes

        writes.snooze(self.connection, f"today:{row}", "2026-09-07T08:00:00Z", seen=seen, now=NOW)


class TheGroups(ReportsCase):
    def test_142_work_items_collapse_to_at_most_five_rows(self):
        late = [work(n, due="2026-08-01", repo="hoa" if n % 3 else "home", recurring=True) for n in range(1, 115)]
        ready = [work(n, state="ready_to_send") for n in range(200, 209)]
        idle = [work(n, state="in_progress") for n in range(300, 306)]
        live = [work(400, state="in_progress")]
        due = [work(n, due=NOW[:10]) for n in range(500, 512)]
        rows = now_screen.work_needs(late + ready + idle + live + due, {400}, NOW[:10])
        self.assertLessEqual(len(rows), 5)
        groups = {row["id"]: row for row in rows if row.get("group")}
        self.assertEqual({key: len(row["members"]) for key, row in groups.items()},
                         {"group:late": 114, "group:ready": 9, "group:idle": 6, "group:due": 12})
        self.assertEqual(groups["group:late"]["more"], 114 - now_screen.GROUP_SHOWN)
        self.assertIn("hoa 76 · home 38", groups["group:late"]["detail"])
        self.assertNotIn(400, [member["item"] for row in rows for member in row.get("members", [row])])

    def test_an_urgent_late_item_breaks_out_of_its_group(self):
        rows = now_screen.work_needs([work(1, due="2026-08-01", recurring=True), work(2, due="2026-09-01", recurring=True),
                                      work(3, due="2026-09-01", priority=1), work(4, due="2026-07-01"),
                                      work(5, due="2026-07-01", recurring=True)], set(), NOW[:10])
        alone = sorted(row["item"] for row in rows if not row.get("group"))
        self.assertEqual(alone, [3, 4])
        [group] = [row for row in rows if row.get("group")]
        self.assertEqual(sorted(member["item"] for member in group["members"]), [1, 2, 5])

    def test_a_lone_member_is_its_own_row_not_a_group_of_one(self):
        [row] = now_screen.work_needs([work(1, state="ready_to_send")], set(), NOW[:10])
        self.assertEqual((row["id"], row.get("group")), ("item:1", None))

    def test_a_group_counts_once_in_the_badge(self):
        listed = now_screen.needs([], work=[work(n, due="2026-08-01", recurring=True) for n in range(1, 40)], live=set(),
                                  today=NOW[:10], lanes=[], briefs=[])
        self.assertEqual([row["id"] for row in listed], ["group:late"])
        self.assertEqual(now_screen._attention(listed), {"state": "caution", "n": 1, "what": "rows to look at"})

    def test_the_bands_order_the_list_and_each_row_names_its_lamp(self):
        listed = now_screen.needs(
            [{**now_screen.dark_row("jobs", "launchd gave no answer"), "band": "broken"}],
            work=[work(1, state="ready_to_send"), work(2, state="in_progress")], live=set(), today=NOW[:10],
            lanes=[lane(rows=[blocked(9)])],
            briefs=[{"id": "b1", "subj": "Morning", "src": "hoa", "words": 90, "day": NOW[:10], "seen": "x"},
                    {"id": "b2", "subj": "Evening", "src": "home", "words": 40, "day": NOW[:10], "seen": "y"}])
        self.assertEqual([(row["id"], row["band"], row["lamp"]) for row in listed],
                         [("dark:jobs", "broken", "jobs"), ("lane:system:9:aaaaaaaaaaaa", "broken", "lane"),
                          ("item:1", "look", ""), ("group:briefs", "queued", ""), ("item:2", "queued", "")])

    def test_a_late_item_with_a_live_session_is_in_motion_only(self):
        """sd:5954: a past-due item in progress with a live session was in Needs you, in the past-due group's bulk
        snooze, and in In motion, now the Queue view's live rows (pack sd:7234). A live session puts it there only, late
        or not, urgent or not."""
        rows = now_screen.work_needs([work(1, state="in_progress", due="2026-08-01", recurring=True),
                                      work(2, state="in_progress", due="2026-08-01", recurring=True),
                                      work(3, state="in_progress", due="2026-07-01", priority=1),
                                      work(4, state="in_progress", due="2026-08-01", recurring=True)], {1, 2, 3}, NOW[:10])
        self.assertEqual([(row["id"], row.get("group")) for row in rows], [("item:4", None)])

    def test_needs_and_in_motion_share_no_item(self):
        self.item("live one", status="in_progress")
        doc = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name), fleet=fleet_document,
                                  lanes=queue())
        moving = {row["item"] for row in doc["active"]["rows"]}
        listed = {member.get("item") for row in doc["needs"] for member in row.get("members", [row])}
        self.assertFalse(moving & listed)


class TheTrack(ReportsCase):
    """The day track (sd:5921): the last 7 days as marks (pack sd:7681: the Timeline's 24h / 7d tiles), one per landed
    item, failed run, new brief and session, and the key's four counts, Landed over the last 24 hours. A mark outside
    the 7 days is left out; a read that failed is its reason, never an empty day."""

    DELIVERED = [{"id": 7, "title": "Seven", "at": "2026-09-06 10:00:00"}, {"id": 8, "title": "Eight", "at": "2026-09-05T11:00:00Z"}]
    PLANNED = [{"due": "2026-09-08"}, {"due": "2026-09-20"}]

    def track(self, jobs, **seams):
        from sd_db import today

        with mock.patch.object(today, "delivered", return_value=seams.get("delivered", self.DELIVERED)), \
                mock.patch.object(today, "planned", return_value=self.PLANNED), \
                mock.patch.object(today, "blocked", side_effect=seams.get("blocked", lambda connection: [{}, {}, {}])):
            return now_screen.document(self.connection, now=NOW, jobs=jobs, fleet=fleet_document,
                                       lanes=queue(lane(rows=[{**blocked(1), "state": "merging"}])))["track"]

    def test_each_lane_marks_its_source_in_the_last_7_days_and_the_key_counts_24_hours(self):
        from datetime import datetime, timezone

        jobs = JobsBackend(self.tmp.name, jobs=[("nightly-sync", "failed", 7, None), ("old-one", "failed", 1, None),
                                                ("older", "failed", 2, None)])
        jobs.log("nightly-sync", datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc).timestamp())
        jobs.log("old-one", datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc).timestamp())
        jobs.log("older", datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc).timestamp())
        self.brief("2026-09-06 - Daily Intel Brief", at="2026-09-06T06:00:00Z")
        found = self.track(jobs)
        self.assertEqual((found["since"], found["day"], found["until"]), ("2026-08-30T12:00:00Z", "2026-09-05T12:00:00Z", NOW))
        self.assertEqual([(m["at"], m["item"]) for m in found["landed"]], [("2026-09-05T11:00:00Z", 8), ("2026-09-06T10:00:00Z", 7)])
        self.assertEqual([(m["at"], m["id"]) for m in found["jobs"]], [("2026-09-04T09:00:00Z", "job:old-one:1"),
                                                                     ("2026-09-06T09:00:00Z", "job:nightly-sync:7")])
        self.assertEqual([m["at"] for m in found["briefs"]], ["2026-09-06T06:00:00Z"])
        self.assertEqual(found["live"], [])
        self.assertEqual(found["key"], {"landed": 1, "landing": 1, "due_week": 1, "blocked": 3})

    def test_a_session_started_before_the_window_is_live_from_the_windows_start(self):
        """sd:6710: Live shows every session running now; one started before the 7 days spans the whole window."""
        from sd_db import today

        active = {"rows": [{"item": 5585, "session": "Worktree old", "started": "2026-08-20T08:00:00Z"},
                           {"item": 9, "session": "Worktree new", "started": "2026-09-06T11:30:00Z"}]}
        with mock.patch.object(today, "delivered", return_value=[]), \
                mock.patch.object(today, "planned", return_value=[]), \
                mock.patch.object(today, "blocked", return_value=[]):
            found = now_screen.track(self.connection, NOW, rows=[], briefs=[], active=active,
                                     lane={"merging": 0, "next": 0})
        self.assertEqual([(m["at"], m["item"]) for m in found["live"]],
                         [("2026-08-30T12:00:00Z", 5585), ("2026-09-06T11:30:00Z", 9)])

    def test_a_read_that_failed_is_its_reason(self):
        import sqlite3

        def locked(connection):
            raise sqlite3.OperationalError("database is locked")

        found = self.track(JobsBackend(self.tmp.name), blocked=locked)
        self.assertEqual(found, {"error": "database is locked"})


class TheUnread(ReportsCase):
    """sd:5939: a source read only in part is a dark Needs you row, never a quiet 0. Queue's document names an unread
    lane in `problems` and does not raise; the reports, the mail, a checkout, the gate and a live-session source each
    say an unread part the same way. Each such part is one broken row under its lamp, so the lamp reads unknown and the
    badge counts it."""

    def today(self, *, lanes=None, fleet=fleet_document):
        return now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name), fleet=fleet,
                                   lanes=lanes or queue())

    def dark(self, doc):
        return {row["id"]: (row["band"], row["lamp"], row["what"], row["detail"]) for row in doc["needs"] if row["kind"] == "dark"}

    def test_an_unread_lane_is_a_broken_row_and_the_badge_counts_it(self):
        def partial(connection, now):
            return {**queue(lane(rows=[]))(connection, now), "problems": [{"repo": "/repos/pack", "error": "sd-ship gave no answer"}]}

        doc = self.today(lanes=partial)
        self.assertEqual(self.dark(doc), {"dark:lane": ("broken", "lane", "lane not read for pack", "pack: sd-ship gave no answer")})
        self.assertEqual(doc["attention"], {"state": "warning", "n": 1, "what": "broken rows"})

    def test_a_gate_read_that_failed_is_a_broken_row(self):
        doc = self.today(lanes=queue(gates={"error": "sd gate status gave no answer"}))
        self.assertEqual(self.dark(doc), {"dark:gate": ("broken", "gate", "gate could not be read", "sd gate status gave no answer")})

    def test_reports_or_mail_that_could_not_be_read_are_broken_rows(self):
        from sd_dashboard import today_mail, today_reports

        reports = {"problems": [], "problems_more": 0, "problems_capped": False, "problems_error": "database is locked",
                   "briefs": [], "briefs_more": 0, "briefs_error": "", "briefs_skipped": 0}
        mail = {"mail": [], "mail_more": 0, "mail_unread": [{"repo": "hoa", "reason": "alias board is not in aliases.conf"}],
                "mail_error": ""}
        with mock.patch.object(today_reports, "document", return_value=reports), \
                mock.patch.object(today_mail, "document", return_value=mail):
            doc = self.today()
        self.assertEqual(self.dark(doc), {
            "dark:reports": ("broken", "reports", "reports could not be read", "database is locked"),
            "dark:mail": ("broken", "mail", "mail not read for hoa", "hoa: alias board is not in aliases.conf")})

    def test_a_checkout_the_fleet_could_not_read_is_a_broken_repos_row(self):
        from test_now_screen import repo

        doc = self.today(fleet=lambda area: fleet_document(area, repos=[repo("fine"), repo("gone", error="git did not answer")]))
        self.assertEqual(self.dark(doc), {"dark:repos": ("broken", "repos", "repos not read for gone", "gone: git did not answer")})

    def test_a_live_session_source_that_failed_is_a_broken_sessions_row(self):
        from sd_dashboard import active_sessions

        found = {"rows": [], "sources": {"fleet": "", "lane": "", "launch": "", "herdr": "herdr did not answer"}, "error": ""}
        with mock.patch.object(active_sessions, "document", return_value=found):
            doc = self.today()
        self.assertEqual(self.dark(doc), {"dark:live": ("broken", "sessions", "live not read for herdr", "herdr: herdr did not answer")})

    def test_every_dark_row_carries_a_seen_and_a_snooze_holds_it(self):
        """sd:5954: the dark rows a part read raised skipped the snooze, so they carried no `seen` and /api/snooze refused
        their Snooze. Each is split as Now's rows are: it carries a `seen`, a snooze holds it, and a snoozed one still
        names its lamp, so the lamp reads unknown (sd:5939)."""
        from sd_dashboard import active_sessions, today_mail, today_reports
        from sd_db import writes
        from test_now_screen import repo

        def lane_down(connection, now):
            raise OSError("sd-ship gave no answer")

        reports = {"problems": [], "problems_more": 0, "problems_capped": False, "problems_error": "database is locked",
                   "briefs": [], "briefs_more": 0, "briefs_error": "", "briefs_skipped": 0}
        mail = {"mail": [], "mail_more": 0, "mail_unread": [{"repo": "hoa", "reason": "no alias"}], "mail_error": ""}
        live = {"rows": [], "sources": {"herdr": "herdr did not answer"}, "error": ""}
        def fleet(area):
            return fleet_document(area, repos=[repo("gone", error="git did not answer")])
        cases = (("dark:lane", {"lanes": lane_down}), ("dark:gate", {"lanes": queue(gates={"error": "no answer"})}),
                 ("dark:repos", {"fleet": fleet}))
        with mock.patch.object(today_reports, "document", return_value=reports), \
                mock.patch.object(today_mail, "document", return_value=mail), \
                mock.patch.object(active_sessions, "document", return_value=live):
            for row_id, seams in (*cases, ("dark:reports", {}), ("dark:mail", {}), ("dark:live", {})):
                with self.subTest(row=row_id):
                    [row] = [row for row in self.today(**seams)["needs"] if row["id"] == row_id]
                    self.assertTrue(row.get("seen"), row)
                    writes.snooze(self.connection, f"today:{row_id}", "2026-09-07T08:00:00Z", seen=row["seen"], now=NOW)
                    doc = self.today(**seams)
                    self.assertNotIn(row_id, [row["id"] for row in doc["needs"]])
                    [held] = [held for held in doc["snoozed"] if held["id"] == row_id]
                    self.assertEqual((held["lamp"], held["until"]), (row["lamp"], "2026-09-07T08:00:00+00:00"))

    def test_a_malformed_fleet_document_is_the_whole_repos_row_never_a_crash(self):
        # Review of sd:5939: unread_rows read the kept fleet rows before it checked that _repos accepted them.
        for repos in ("not a list", [{"error": "git did not answer"}], [{"name": "x", "error": {"not": "text"}}]):
            with self.subTest(repos=repos):
                doc = self.today(fleet=lambda area, *, repos=repos: {"repos": repos} if area == "repos" else fleet_document(area))
                self.assertEqual(self.dark(doc), {"dark:repos": ("broken", "repos", "repos could not be read",
                                                                 "fleet collector returned an incomplete repos document")})

    def test_a_malformed_queue_document_reads_only_what_it_can(self):
        def odd(connection, now):
            return {**queue()(connection, now), "gates": "no gates", "problems": [{"repo": "/repos/pack"}, "junk"]}

        doc = self.today(lanes=odd)
        self.assertEqual(self.dark(doc), {"dark:lane": ("broken", "lane", "lane not read for pack", "pack: no reason given")})


class TheFlakyHeads(ReportsCase):
    """pack sd:7663: a head the gate failed and then passed at that head is a look row on Today, not a Metrics table.

    Two or more fold into one group row that opens to its heads, as the work groups do; one is its own row. A lane read
    that failed is a partial lane row, never a week with no flaky head.
    """

    def flaky(self, head, item, *, repository="example/alpha", day="2026-09-04"):
        from test_v2_metrics import GATE, lane_run

        lane_run(self.connection, f"f{item}", "failed", f"{day}T10:00:00Z", expected=head, reason=GATE, item=item,
                 repository=repository)
        lane_run(self.connection, f"p{item}", "merged", f"{day}T14:00:00Z", head=head, item=item,
                 repository=repository)

    def test_two_flaky_heads_are_one_look_group_on_the_lane_lamp(self):
        self.flaky("1" * 40, 9)
        self.flaky("2" * 40, 10, repository="example/beta", day="2026-09-05")
        doc = self.today()
        [group] = [row for row in doc["needs"] if row["id"] == "group:flaky"]
        self.assertEqual((group["band"], group["lamp"], group["what"], group["detail"]),
                         ("look", "lane", "2 flaky heads", "example/alpha · example/beta"))
        self.assertEqual([(m["id"], m["kind"], m["item"], m["head"], m["band"]) for m in group["members"]],
                         [("flaky:example/beta:" + "2" * 40, "flaky", 10, "2" * 40, "look"),
                          ("flaky:example/alpha:" + "1" * 40, "flaky", 9, "1" * 40, "look")])
        self.assertEqual(group["members"][1]["failed"], ["2026-09-04T10:00:00Z"])
        self.assertEqual(doc["attention"], {"state": "caution", "n": 1, "what": "rows to look at"})

    def test_one_flaky_head_is_its_own_row_and_a_snooze_hides_it(self):
        self.flaky("1" * 40, 9)
        [row] = [row for row in self.today()["needs"] if row["id"].startswith(("flaky:", "group:flaky"))]
        self.assertEqual((row["id"], row["what"]), ("flaky:example/alpha:" + "1" * 40,
                                                    "Flaky: example/alpha sd:9 at 111111111111"))
        from sd_db import writes

        writes.snooze(self.connection, f"today:{row['id']}", "2026-09-07T08:00:00Z", seen=row["seen"], now=NOW)
        doc = self.today()
        self.assertNotIn(row["id"], [r["id"] for r in doc["needs"]])
        self.assertIn(row["id"], [r["id"] for r in doc["snoozed"]])

    def test_a_flaky_head_older_than_the_week_is_not_listed(self):
        # NOW is 2026-09-06T12:00Z: the week began 08-30 at noon.
        self.flaky("1" * 40, 9, day="2026-08-28")
        doc = self.today()
        self.assertEqual([row["id"] for row in doc["needs"] if "flaky" in row["id"]], [])
        self.flaky("3" * 40, 11)
        self.assertEqual([row["id"] for row in self.today()["needs"] if "flaky" in row["id"]],
                         ["flaky:example/alpha:" + "3" * 40])

    def test_a_lane_entry_read_that_failed_is_a_partial_lane_row(self):
        from sd_db import reads

        with mock.patch.object(reads, "lane_entries", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.today()
        [row] = [row for row in doc["needs"] if row["id"] == "dark:flaky"]
        self.assertEqual((row["band"], row["lamp"], row["partial"], row["what"]),
                         ("broken", "lane", True, "lane not read for flaky heads"))
        self.assertIn("database is locked", row["detail"])

    def test_a_lane_entry_that_cannot_be_read_is_a_partial_lane_row_not_a_quiet_week(self):
        """pack sd:7706: `_entries` counts a body that is not a lane entry; the flaky read dropped that count, so a bad
        entry vanished and the Lane lamp read 0. It is the partial `dark:flaky` row, which names the count."""
        self.flaky("1" * 40, 9)
        for n in (1, 2):
            self.connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                    (f"lane:v1:example/alpha:bad{n}", NOW, json.dumps({"status": "merged"})))
        self.connection.commit()
        doc = self.today()
        [row] = [row for row in doc["needs"] if row["id"] == "dark:flaky"]
        self.assertEqual((row["band"], row["lamp"], row["partial"], row["what"]),
                         ("broken", "lane", True, "lane not read for flaky heads"))
        self.assertIn("2 lane entries could not be read", row["detail"])
        # The heads it could read still show.
        self.assertIn("flaky:example/alpha:" + "1" * 40, [r["id"] for r in doc["needs"]])

    def test_an_unreadable_lane_entry_older_than_the_window_is_not_counted(self):
        """pack sd:7734: the count covered every body in history, so one old bad entry kept `dark:flaky` up for good."""
        from sd_dashboard import metrics_screen

        old = (datetime.fromisoformat(NOW.replace("Z", "+00:00"))
               - timedelta(days=metrics_screen.CI_DAYS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                ("lane:v1:example/alpha:old", old, json.dumps({"status": "merged"})))
        self.connection.commit()
        self.assertEqual([row["id"] for row in self.today()["needs"] if row["id"] == "dark:flaky"], [])
