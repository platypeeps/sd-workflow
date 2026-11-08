"""The v2 Tasks page (sd:2124).

What this slice promises: `/tasks` answers under the shared policy and loads
its script before `shell.js`; `/api/tasks` is the row set v1 /backlog reads
(`reads.backlog_items`), each row carrying the revision a write sends back and
the statuses the library allows; `/api/tasks/<id>` is the `sd task show
--json` reading split into the Details sections (status history, notes,
assignments, artifact) plus the external context v1's item page shows.

`tasks.js` runs under JavaScriptCore (osascript, as `test_v2_today` runs
markup.js) against a stand-in page, with the documents above as its fetch
answers. The stand-in shell mirrors `shell.js`'s run contract: a `confirm`
command asks first, a `null` return means the page toasts on its own, and a
string return is toasted by the shell. The browser half -- drag, focus, the
look at 375 px -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urlencode, urlsplit

from sd_dashboard import server, tasks_screen, v2
from sd_db import connect, reads, set_item_fields, upsert_repo, workflow
from support import NOW, ScreenCase, transition, upsert_shadow
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
TASKS_JS = (V2 / "static" / "tasks.js").read_text(encoding="utf-8")
# A task's Details, commands and dialogs (sd:5635): tasks.js attaches task-details.js, which the page loads first.
TASK_DETAILS_JS = (V2 / "static" / "task-details.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
SHELL_JS = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
# The shell's run contract for writes, as shell.js has it: the stand-in runs these, not a copy.
SETTLE_JS = re.search(r"^  // bulk:start\n(.*?)^  // bulk:end$", SHELL_JS, re.S | re.M).group(1)
# The confirm dialog with its fields, as shell.js has it (sd:2200).
CONFIRM = re.search(r"^  // confirm:start\n(.*?)^  // confirm:end$", SHELL_JS, re.S | re.M)
# The page's reads and writes, as shell.js has them (sd:2588): every page posts through shell.post and reads through shell.getJSON.
FETCH_JS = re.search(r"^  // fetch:start\n(.*?)^  // fetch:end$", SHELL_JS, re.S | re.M).group(1)
# An item's one address, as shell.js has it (sd:3526): every page links and opens an item through it.
ITEM_JS = re.search(r"^  // item:start\n(.*?)^  // item:end$", SHELL_JS, re.S | re.M).group(1)
# The list grammar, as shell.js has it (sd:2527, sd:2528, sd:2529): sort header, pager and filter chips.
LIST_JS = re.search(r"^  // list:start\n(.*?)^  // list:end$", SHELL_JS, re.S | re.M).group(1)
# sd:6701: the set-aside group every page draws through shell.aside, cut from shell.js as the list block is.
ASIDE_JS = re.search(r"^  // aside:start\n(.*?)^  // aside:end$", SHELL_JS, re.S | re.M).group(1)
# The layout picker, as shell.js has it (sd:5641): Tasks and Writing wire their view menu through shell.viewPick.
VIEWPICK = re.search(r"^  // viewpick:start\n(.*?)^  // viewpick:end$", SHELL_JS, re.S | re.M)
VIEWPICK_JS = VIEWPICK.group(1) if VIEWPICK else "function viewPick() { throw new Error('shell.js has no viewpick block'); }\n"


def seed(case):
    """Three open items and a week-old done one, on the fixture database."""
    repo = case.repo()
    plan = case.item("Plan the review", kind="task", priority=2, due="2026-09-09", repo=repo)
    ask = case.item("Answer the question", kind="followup", priority=3)
    port = case.item("Port the page", kind="work", repo=repo, path="docs/work/port/prd.md")
    old = case.item("Finished long ago", kind="task")
    workflow.change_status(case.connection, old, "done", who="test")
    case.connection.execute("UPDATE note SET timestamp = '2026-08-01T00:00:00Z' WHERE item = ?", (old,))
    case.connection.commit()
    case.note(plan, "Check the budget first")
    case.note(plan, "A plain comment", kind="comment")
    workflow.change_status(case.connection, plan, "ready", who="test")
    case.assignment(port, status="running")
    set_item_fields(case.connection, plan, source="github", external_id="https://example.invalid/i/1")
    upsert_shadow(case.connection, tracker="github", url="https://example.invalid/i/1", state="open", title="an issue")
    return {"plan": plan, "ask": ask, "port": port, "old": old}


class TheDocuments(ScreenCase):
    """`tasks_screen` builds both documents from library reads."""

    def setUp(self):
        super().setUp()
        self.ids = seed(self)

    def test_each_row_pairs_its_fields_with_the_revision_of_the_same_read(self):
        # An edit commits through a second connection after the row read and before the revisions are read. One snapshot
        # holds both, so the row shows the old title with the old revision; the edit's revision would let a write chosen
        # from the old title pass (review, PR #46).
        ask = self.ids["ask"]
        old = workflow.item_state(self.connection, ask)["revision"]
        writer = connect(self.path)
        self.addCleanup(writer.close)
        real = reads.backlog_items

        def rows_then_edit(connection, **arguments):
            rows = real(connection, **arguments)
            set_item_fields(writer, ask, title="Edited elsewhere")
            writer.commit()
            return rows

        with patch.object(reads, "backlog_items", rows_then_edit):
            doc = tasks_screen.document(self.connection, now=NOW)
        row = next(r for r in doc["rows"] if r["id"] == ask)
        self.assertEqual((row["title"], row["revision"]), ("Answer the question", old))
        self.assertFalse(self.connection.in_transaction)
        self.assertNotEqual(next(r for r in tasks_screen.document(self.connection, now=NOW)["rows"] if r["id"] == ask)["revision"], old)

    def test_each_row_says_whether_edit_item_takes_its_fields_and_why_not(self):
        # The row owns every work item's fields (sd:3015), so the seeded work row is editable; a report keeps its own
        # editing workflow. Each row's `edit` is checked against edit_item itself (review, PR #46).
        report = self.item("Look at the failed run", kind="report", fields={"attention": True})
        rows = {row["id"]: row for row in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertEqual(rows[self.ids["port"]]["edit"], {"allowed": True, "reason": None})
        self.assertEqual(rows[report]["edit"], {"allowed": False, "reason": "report items use their own editing workflow"})
        for item, row in rows.items():
            try:
                workflow.edit_item(self.connection, item, {}, who="test")
                said = {"allowed": True, "reason": None}
            except workflow.WorkflowError as refused:
                said = {"allowed": False, "reason": str(refused)}
            self.assertEqual(row["edit"], said, f"item {item} ({row['kind']})")

    def test_the_rows_are_the_backlog_rows_with_revision_and_allowed_statuses(self):
        doc = tasks_screen.document(self.connection, now=NOW)
        self.assertEqual(doc["statuses"], ["planning", "ready", "in_progress", "blocked", "done"])
        rows = {row["id"]: row for row in doc["rows"]}
        self.assertEqual(set(rows), {self.ids["plan"], self.ids["ask"], self.ids["port"]})
        plan = rows[self.ids["plan"]]
        self.assertEqual((plan["status"], plan["priority"], plan["due"], plan["repo"], plan["repo_path"]),
                         ("ready", 2, "2026-09-09", "system", "/repos/system"))
        self.assertEqual(plan["revision"], workflow.item_state(self.connection, self.ids["plan"])["revision"])
        self.assertEqual(plan["allowed"], workflow.allowed_statuses(self.connection, self.ids["plan"]))
        self.assertIsNone(plan["assignment"])
        port = rows[self.ids["port"]]
        self.assertEqual((port["assignment"], port["allowed"]), ("running", []))

    def test_every_row_is_a_task_table_row_with_its_id(self):
        # sd:3289: the page lists the task table and nothing else, so every row carries the id of an item it names.
        doc = tasks_screen.document(self.connection, now=NOW)
        ids = {row["id"] for row in self.connection.execute("SELECT id FROM item")}
        self.assertTrue(doc["rows"])
        for row in doc["rows"]:
            self.assertIsInstance(row["id"], int)
            self.assertIn(row["id"], ids)

    def test_each_row_carries_its_age_bucket_and_the_document_names_the_buckets(self):
        # The age filter is the histogram's (sd:2589): each row's key is reads.age_bucket, and the document lists every
        # bucket with the label Operations draws, so a bar's ?age= names a chip the page shows.
        self.age(self.ids["ask"], 10)
        doc = tasks_screen.document(self.connection, now=NOW)
        rows = {row["id"]: row for row in reads.backlog_items(self.connection, now=NOW)}
        for row in doc["rows"]:
            self.assertEqual(row["age"], reads.age_bucket(rows[row["id"]], now=NOW))
        self.assertEqual(next(r for r in doc["rows"] if r["id"] == self.ids["ask"])["age"], "7")
        self.assertEqual(doc["ages"], [{"key": bucket.key, "label": bucket.label} for bucket in reads.age_histogram([], now=NOW)])
        self.assertEqual([age["key"] for age in doc["ages"]], [str(lower) for lower, _ in reads.age_bounds()])

    def test_each_row_reads_its_history_once(self):
        # sd:2380: allowed_statuses takes the row's item_state, read in the same snapshot, instead of reading it again.
        calls, real = [], workflow.item_state

        def counted(connection, item):
            calls.append(item)
            return real(connection, item)

        with patch.object(workflow, "item_state", counted):
            doc = tasks_screen.document(self.connection, now=NOW)
        self.assertEqual(sorted(calls), sorted(row["id"] for row in doc["rows"]), "a row's history was read more than once")
        for row in doc["rows"]:
            self.assertEqual(row["allowed"], workflow.allowed_statuses(self.connection, row["id"]), row["id"])
        details = tasks_screen.details(self.connection, self.ids["plan"], now=NOW)
        self.assertEqual(details["allowed"], workflow.allowed_statuses(self.connection, self.ids["plan"]))

    def test_the_details_carry_the_work_controls_for_a_work_item_only(self):
        # sd:2200: relink and cancel are on where progress.work_controls says the mutation would take them.
        from sd_db import progress

        port = tasks_screen.details(self.connection, self.ids["port"], now=NOW)
        self.assertEqual(port["work"], progress.work_controls(self.connection, self.ids["port"]))
        self.assertIsNone(tasks_screen.details(self.connection, self.ids["plan"], now=NOW)["work"])

    def test_the_details_split_the_show_reading_into_its_sections(self):
        got = tasks_screen.details(self.connection, self.ids["plan"], now=NOW)
        state = workflow.item_state(self.connection, self.ids["plan"])
        self.assertEqual((got["item"], got["revision"]), (state["item"], state["revision"]))
        self.assertEqual([n["kind"] for n in got["history"]], ["status_change", "status_change"])
        self.assertEqual([(n["kind"], n["body"], n["resolved"]) for n in got["notes"]],
                         [("followup", "Check the budget first", None), ("comment", "A plain comment", None)])
        self.assertEqual(got["assignments"], [])
        external = got["external"]
        self.assertEqual((external["tracker"], external["url"], external["state"]),
                         ("github", "https://example.invalid/i/1", "open"))
        self.assertEqual(set(external["freshness"]), {"state", "last_success_at", "reason"})

    def test_an_assignment_carries_the_revision_and_cancel_capability_sd_assignments_cancel_checks(self):
        from sd_db import operations

        got = tasks_screen.details(self.connection, self.ids["port"], now=NOW)
        (row,) = got["assignments"]
        self.assertEqual((row["role"], row["status"]), ("author", "running"))
        state = operations.assignment_state(self.connection, row["id"])
        self.assertEqual((row["revision"], row["cancel"]), (state["revision"], state["capabilities"]["cancel"]))
        # sd:3041: no runner readiness or runner capabilities ride on the Details.
        self.assertNotIn("run", got)
        self.assertNotIn("runner", row)
        self.assertIsNone(got["external"])
        self.assertEqual(got["item"]["path"], "docs/work/port/prd.md")

    def test_each_row_carries_the_reads_is_urgent_decision(self):
        report = self.item("Look at the failed run", kind="report", fields={"attention": True})
        backlog = {row["id"]: row for row in reads.backlog_items(self.connection, now=NOW)}
        rows = {row["id"]: row for row in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertTrue(rows[report]["urgent"])
        self.assertEqual({n: r["urgent"] for n, r in rows.items()}, {n: reads.is_urgent(r, now=NOW) for n, r in backlog.items()})

    def test_urgency_beyond_the_due_date_comes_from_reads_is_urgent(self):
        report = self.item("Look at the failed run", kind="report", fields={"attention": True})
        rows = {row["id"]: row for row in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertTrue(rows[report]["urgent_otherwise"])
        self.assertFalse(rows[self.ids["plan"]]["urgent_otherwise"], "a due date is the page's own rule, not sent")

    def test_the_items_the_prune_parked_follow_the_listed_rows_with_their_stamp(self):
        # sd:3012: nothing listed a parked item, so the item page's Unpark was reachable only by its number.
        parked = self.item("Parked by the prune", kind="task", priority=4)
        set_item_fields(self.connection, parked, parked_at="2026-09-01T02:00:00Z")
        self.connection.commit()
        rows = tasks_screen.document(self.connection, now=NOW)["rows"]
        self.assertEqual([(row["id"], row["parked"]) for row in rows][-1], (parked, "2026-09-01T02:00:00Z"))
        self.assertEqual({row["id"] for row in rows if row["parked"] is None}, {self.ids["plan"], self.ids["ask"], self.ids["port"]})
        self.assertEqual(rows[-1]["revision"], workflow.item_state(self.connection, parked)["revision"])

    def test_the_details_list_where_the_item_was_published_in_the_designs_order(self):
        """sd:3465: the item row holds each URL and the Details read the row: every document in `fields.published` with
        its channel links, then `fields.writing.published_urls`, then each publication claim's url. Only http, https and
        obsidian addresses become links; any other is shown as text."""
        from sd_db import channels

        plan = self.ids["plan"]
        self.assertEqual(tasks_screen.details(self.connection, plan, now=NOW)["published"], [])
        repo = self.repo()
        channels.record_url(self.connection, plan, repo, "brief.md", "The brief", "notion", "https://example.test/p", "test")
        channels.record_url(self.connection, plan, repo, "brief.md", "The brief", "obsidian",
                            "obsidian://open?vault=v&file=Briefs/x/brief", "test")
        fields = json.loads(self.connection.execute("SELECT fields FROM item WHERE id = ?", (plan,)).fetchone()[0])
        fields["writing"] = {"published_urls": {"gdrive": "https://example.test/d", "site": "javascript:alert(1)"}}
        self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (json.dumps(fields), plan))
        self.connection.commit()
        got = tasks_screen.details(self.connection, plan, now=NOW)["published"]
        link = lambda channel, url, safe=True: {"channel": channel, "url": url, "href": url if safe else None}  # noqa: E731
        self.assertEqual(got, [
            {"title": "The brief", "where": f"{Path(repo).name} · brief.md",
             "links": [link("obsidian", "obsidian://open?vault=v&file=Briefs/x/brief"), link("notion", "https://example.test/p")]},
            {"title": "Writing piece", "where": "writing targets",
             "links": [link("gdrive", "https://example.test/d"), link("site", "javascript:alert(1)", safe=False)]},
        ])

    def test_a_writing_pieces_details_list_each_publication_claims_url(self):
        # sd:3465: a claim belongs to a writing piece; `writing.piece_state` reads its claims, and one with no url is left out.
        from sd_db import writing

        root = Path(self.tmp.name) / "writing"
        (root / "content" / "2026" / "a-piece").mkdir(parents=True)
        (root / "content" / "2026" / "a-piece" / "index.md").write_text("---\ntitle: A piece\nstatus: review\n---\n\n## Draft\n\nWords.\n")
        upsert_repo(self.connection, str(root), pieces_source="file")
        piece = writing.import_piece(self.connection, str(root), "2026/a-piece", who="test")["item"]["id"]
        claim = "INSERT INTO publication_claim (id, item, payload, state, created_at, updated_at) VALUES (?, ?, '{}', ?, ?, ?)"
        self.connection.execute(claim, ("c1aim0000000", piece, json.dumps({"phase": "published", "url": "https://example.test/c"}), NOW, NOW))
        self.connection.execute(claim, ("c2aim0000000", piece, json.dumps({"phase": "claimed"}), "2026-09-07T00:00:00Z", NOW))
        self.connection.commit()
        self.assertEqual(tasks_screen.details(self.connection, piece, now=NOW)["published"], [
            {"title": "Publication claim c1aim000", "where": "published",
             "links": [{"channel": "claim", "url": "https://example.test/c", "href": "https://example.test/c"}]}])
        # A claims read that fails is named, not read as no claim.
        with patch.object(writing, "piece_state", side_effect=workflow.WorkflowError("the store is locked")):
            self.assertEqual(tasks_screen.details(self.connection, piece, now=NOW)["published"],
                             [{"title": "Publication claims", "where": "not read: the store is locked", "links": []}])

    def test_a_missing_item_raises_for_the_route_to_answer_404(self):
        with self.assertRaises(workflow.MissingItem):
            tasks_screen.details(self.connection, 9999, now=NOW)

    def test_the_details_carry_the_list_row_and_why_the_list_leaves_the_item_out(self):
        """sd:5462: a link to an item /api/tasks leaves out opens it in Details, from its row in the list's shape and the
        reason. `left_out` is None exactly for the items the list holds, a parked task among them."""
        from sd_db import writing

        quiet = self.item("A quiet nightly run", kind="report", fields={"attention": False})
        bare = self.item("A report with no attention field", kind="report")
        loud = self.item("Look at the failed run", kind="report", fields={"attention": True})
        parked = self.item("Parked by the prune", kind="task", priority=4)
        set_item_fields(self.connection, parked, parked_at="2026-09-01T02:00:00Z")
        root = Path(self.tmp.name) / "writing"
        (root / "content" / "2026" / "a-piece").mkdir(parents=True)
        (root / "content" / "2026" / "a-piece" / "index.md").write_text("---\ntitle: A piece\nstatus: review\n---\n\nWords.\n")
        upsert_repo(self.connection, str(root), pieces_source="file")
        piece = writing.import_piece(self.connection, str(root), "2026/a-piece", who="test")["item"]["id"]
        set_item_fields(self.connection, piece, parked_at="2026-09-01T02:00:00Z")
        # pack sd:6262: a learning row moved to a Documents view shows only there.
        video = self.item("2016-06-06 - YouTube - Example Channel - A Talk", kind="idea", status="ready", source="vault",
                          stage="in-progress", fields={"kind": "learning", "document-type": "videos"})
        self.connection.commit()
        rows = {row["id"]: row for row in tasks_screen.document(self.connection, now=NOW)["rows"]}
        every = [*self.ids.values(), quiet, bare, loud, parked, piece, video]
        got = {item: tasks_screen.details(self.connection, item, now=NOW) for item in every}
        self.assertEqual({item: got[item]["left_out"] for item in every if got[item]["left_out"]},
                         {self.ids["old"]: "done before the week", quiet: "a report that needs no attention",
                          bare: "a report that needs no attention",
                          piece: "a parked writing piece, which revives through its own workflow",
                          video: "a document: it shows under Documents > Videos"})
        self.assertEqual({item for item in every if got[item]["left_out"] is None}, set(rows))
        for item in rows:
            self.assertEqual(got[item]["row"], rows[item], item)
        old = got[self.ids["old"]]["row"]
        self.assertEqual((old["id"], old["status"], old["revision"]), (self.ids["old"], "done", got[self.ids["old"]]["revision"]))

    def repeating(self, title, due, lead):
        """A weekly task due `due` with a lead of `lead` days, in the store."""
        item = self.item(title, kind="task", due=due)
        set_item_fields(self.connection, item, recurrence="FREQ=WEEKLY", recurrence_anchor="schedule", recurrence_lead=lead)
        self.connection.commit()
        return item

    def test_a_repeating_row_carries_its_lead_and_the_day_it_opens(self):
        """sd:5649: each row carries `recurrence_lead`, and `opens_on` and `scheduled` from `backlog_items` (pack sd:5648).
        NOW is 2026-09-06: due 09-09 less 2 days opens 09-07, so it is scheduled; less 3 days opens today, so it is open."""
        later = self.repeating("Water the plants", "2026-09-09", 2)
        now = self.repeating("Pay the rent", "2026-09-09", 3)
        doc = tasks_screen.document(self.connection, now=NOW)
        rows = {row["id"]: row for row in doc["rows"]}
        self.assertTrue(doc["lead"])
        pick = lambda row: (row["recurrence_lead"], row["opens_on"], row["scheduled"])  # noqa: E731
        self.assertEqual(pick(rows[later]), (2, "2026-09-07", True))
        self.assertEqual(pick(rows[now]), (3, "2026-09-06", False))
        self.assertEqual(pick(rows[self.ids["plan"]]), (None, None, False))
        # Details' row is the list's row, scheduled or not.
        for item in (later, now):
            self.assertEqual(tasks_screen.details(self.connection, item, now=NOW)["row"], rows[item])

    def test_a_scheduled_row_lights_no_badge_until_it_opens(self):
        """sd:5649: a scheduled occurrence due tomorrow is not on the Tasks list, so it is not caution on the rail either."""
        self.repeating("Take out the bins", "2026-09-07", 0)
        self.assertEqual(tasks_screen.document(self.connection, now=NOW)["attention"], {"state": "ok", "n": 0, "what": "nothing due"})
        self.assertEqual(tasks_screen.document(self.connection, now="2026-09-07T12:00:00Z")["attention"]["state"], "caution")

    def test_a_store_before_041_reads_no_lead_and_does_not_break(self):
        """sd:5649: the live store may be at schema 40, without `recurrence_lead`, and a pack before sd:5648 gives rows
        without `opens_on` and `scheduled`. Each read answers: no lead, `lead` off, nothing scheduled the read did not say."""
        item = self.repeating("Water the plants", "2026-09-09", 2)
        self.connection.execute("ALTER TABLE item DROP COLUMN recurrence_lead")
        self.connection.commit()
        doc = tasks_screen.document(self.connection, now=NOW)
        row = next(r for r in doc["rows"] if r["id"] == item)
        self.assertFalse(doc["lead"])
        # Without a lead the occurrence opens on its due date, 09-09: the pack's rule for a store before 041.
        self.assertEqual((row["recurrence_lead"], row["opens_on"], row["scheduled"]), (None, "2026-09-09", True))
        self.assertEqual(tasks_screen.details(self.connection, item, now=NOW)["row"], row)
        real = reads.backlog_items

        def before_5648(connection, **arguments):
            return [{key: row[key] for key in row.keys() if key not in ("opens_on", "scheduled")} for row in real(connection, **arguments)]

        with patch.object(reads, "backlog_items", before_5648):
            doc = tasks_screen.document(self.connection, now=NOW)
        with patch.object(reads, "item_opens_on", None, create=True):
            details = tasks_screen.details(self.connection, item, now=NOW)
        row = next(r for r in doc["rows"] if r["id"] == item)
        self.assertEqual((row["recurrence_lead"], row["opens_on"], row["scheduled"]), (None, None, False))
        self.assertEqual((details["row"]["opens_on"], details["row"]["scheduled"]), (None, False))


class ThePage(BrowserSession):
    def setUp(self):
        super().setUp()
        self.ids = seed(self)

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Tasks · system</title>", body)
        self.assertRegex(body, r'<meta name="sd-csrf" content="[a-f0-9]{64}"></head>')
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "commands.js", "task-details.js", "tasks.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_routes_need_a_session_and_take_no_query(self):
        self.assertEqual(self.request("/api/tasks")[0], 403)
        self.assertEqual(self.request(f"/api/tasks/{self.ids['plan']}")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/tasks?kind=task", headers=cookie)[0], 400)
        self.assertEqual(self.request("/api/tasks/9999", headers=cookie)[0], 404)
        self.assertEqual(self.request("/api/tasks/99999999999999999999", headers=cookie)[0], 404)
        self.assertEqual(self.request("/api/tasks/0", headers=cookie)[0], 404)

    def test_a_row_revision_is_the_one_the_status_write_accepts(self):
        cookie = {"Cookie": self.cookie}
        status, _, body = self.request("/api/tasks", headers=cookie)
        self.assertEqual(status, 200)
        row = next(r for r in json.loads(body)["rows"] if r["id"] == self.ids["ask"])
        status, _, state = self.post(f"/api/items/{row['id']}/status", {"status": "ready", "revision": row["revision"]})
        self.assertEqual((status, state["item"]["status"]), (200, "ready"))
        status, _, state = self.post(f"/api/items/{row['id']}/status", {"status": "planning", "revision": row["revision"]})
        self.assertEqual(status, 409)
        status, _, body = self.request(f"/api/tasks/{self.ids['plan']}", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["external"]["url"], "https://example.invalid/i/1")


# The stand-in page. Elements record the markup put() gives them; fetch answers from ANSWER(path, body); the shell records
# toasts and runs commands the way shell.js does. The test script is an async body: osascript drains the promise jobs
# between the top level and run(), so run() reads what the whole flow produced.
STAND_IN = r"""
var window = globalThis, OUT = { posts: [], gets: [], toasts: [], confirms: [], states: [], attention: [], error: null };
var innerWidth = 1440, location = { search: '', replace: u => { OUT.replaced = u; } };
function URLSearchParams(q) { var m = new Map(); (q || '').replace(/^\?/, '').split('&').filter(Boolean).forEach(p => { var kv = p.split('='); m.set(kv[0], decodeURIComponent(kv[1] || '')); });
  this.get = k => m.has(k) ? m.get(k) : null; this.set = (k, v) => m.set(k, String(v)); this.delete = k => m.delete(k);
  this.toString = () => [...m].map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&'); }
function El(id) { var e = { id: id, html: null, hidden: false, dataset: {}, style: {}, listeners: {}, value: '',
  classList: { add() {}, remove() {}, toggle() {} },
  addEventListener(t, f) { (e.listeners[t] = e.listeners[t] || []).push(f); }, setAttribute() {}, removeAttribute() {},
  toggleAttribute() {}, getAttribute() { return null; }, querySelector() { return null; }, querySelectorAll() { return []; },
  focus() {}, closest() { return null; }, matches() { return false; }, showModal() {}, remove() {},
  replaceChildren(f) { e.html = f ? f.parsed : ''; }, append(f) { e.html = (e.html || '') + (f && f.parsed || ''); }, prepend() {}, before() {} };
  return e; }
var ELS = {}, DOC_LISTENERS = {}, WIN_LISTENERS = {}, MADE = [];
var document = { body: El('body'),
  getElementById(id) { return ELS[id] = ELS[id] || El(id); },
  querySelector(sel) { return /sd-csrf/.test(sel) ? { content: 'c'.repeat(64) } : null; },
  querySelectorAll() { return []; },
  createElement(tag) { if (tag !== 'template') { var made = El(tag); MADE.push(made); return made; }
    var t = {}; Object.defineProperty(t, 'innerHTML', { set: function (v) { t.content = { parsed: v }; } }); return t; },
  addEventListener(t, f) { (DOC_LISTENERS[t] = DOC_LISTENERS[t] || []).push(f); },
  dispatchEvent(e) { (DOC_LISTENERS[e.type] || []).forEach(f => f(e)); } };
function addEventListener(t, f) { (WIN_LISTENERS[t] = WIN_LISTENERS[t] || []).push(f); }
function CustomEvent(type, o) { this.type = type; this.detail = o && o.detail; }
function Event(type) { this.type = type; }
function matchMedia() { return { matches: false }; }
function requestAnimationFrame() { return 0; }
function setTimeout(f) { f(); return 0; }
var ANSWER = null;
function fetch(path, o) {
  var body = o && o.body ? JSON.parse(o.body) : null;
  if (o && o.method === 'POST') OUT.posts.push([path, body, o.headers['X-SD-CSRF'].length]); else OUT.gets.push(path);
  return Promise.resolve(ANSWER(path, body)).then(a => ({ ok: a[0] < 300, status: a[0], json: () => Promise.resolve(a[1]) }));
}
"""
SHELL = SETTLE_JS + FETCH_JS + ITEM_JS + r"""
var REG = [], OBJ = new Map();
var C = { register: (...cs) => { REG.push(...cs); }, put: o => { OBJ.set(o.id, o); }, get: id => OBJ.get(id), select() {}, pick() {},
  rowActions: () => mk``, bar: () => mk`<div class="bar"></div>`,
  offWhy: (c, o) => { const w = c.when ? c.when(o) : true; return w === true ? '' : (w || 'not available'); } };
// A command with fields asks for them (shell.js's confirm): FIELD_VALUES is what the operator typed, and a required field left
// empty keeps OK off, so nothing runs; a field's value is typed in already. One that asks first (shell.js's asksFirst) records its confirm and runs.
var FIELD_VALUES = {};
function shellRun(c, o) {
  var off = c.when ? c.when(o) : true;
  if (off !== true) { OUT.toasts.push({ msg: 'off: ' + off }); return; }
  // ON_CONFIRM(c, o) is what happens while the dialog is open: the shell's confirm resolves later, so the row can be gone.
  if (c.risk === 'confirm' || c.fields || asksFirst(c, o)) { OUT.confirms.push(c.id); if (typeof ON_CONFIRM === 'function') ON_CONFIRM(c, o); }
  var v = c.fields ? Object.fromEntries(c.fields(o).map(f => [f.name, String((f.name in FIELD_VALUES ? FIELD_VALUES[f.name] : f.value) || '').trim()])) : undefined;
  if (c.fields && c.fields(o).some(f => f.required && !v[f.name])) { OUT.toasts.push({ msg: 'not run: OK is off until ' + c.fields(o).filter(f => f.required && !v[f.name]).map(f => f.label).join(', ') + ' is typed' }); return; }
  var msg = runOne(c, o, v);
  if (thenable(msg)) { settleOne(c, o, msg, { toast: shellToast }); return document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id } })); }
  if (msg === null) return document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id, form: true } }));
  OUT.toasts.push({ msg: msg || c.label, undo: c.risk === 'undo' && c.undo ? () => c.undo(o) : null });
  document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: o.id } }));
}
C.run = shellRun;
// shell.js's runBulk: runEach starts BULK_RUNS runs, each next as one settles, settled by settleBulk. A command with
// batch(objs, done) takes the whole group in one call instead (handOver, sd:2590); done() clears the picks, recorded here.
// A command with fields asks once for the group, and every row's run gets what was typed (sd:3012).
function shellBulk(c, objs) {
  if (handOver(c, objs, () => { OUT.cleared = (OUT.cleared || 0) + 1; })) return;
  var fs = c.fields ? c.fields({ id: 'bulk' }) : [], v = fs.length ? Object.fromEntries(fs.map(f => [f.name, String((f.name in FIELD_VALUES ? FIELD_VALUES[f.name] : f.value) || '').trim()])) : undefined;
  if (c.risk === 'confirm' || fs.length || objs.length > 25 || objs.some(o => asksFirst(c, o))) OUT.confirms.push(c.id);
  if (fs.some(f => f.required && !v[f.name])) { OUT.toasts.push({ msg: 'not run: OK is off until ' + fs.filter(f => f.required && !v[f.name]).map(f => f.label).join(', ') + ' is typed' }); return; }
  OUT.bulkCli = objs.map(o => c.cli ? c.cli(o, v || {}) : '').join(' && ');
  var rs = runEach(c, objs, v);
  settleBulk(c, objs, rs, { toast: shellToast, plural: window.markup.plural });
  document.dispatchEvent(new CustomEvent('shell:ran', { detail: { cmd: c.id, obj: 'bulk', form: true } }));
}
C.runBulk = shellBulk;
function shellToast(msg, undo) { OUT.toasts.push({ msg: msg, undo: undo || null }); }
window.shell = { afterBulk, commands: C, post, getJSON, shown: stillShown, itemHref, itemPage, openItem, ICON: () => mk`<svg></svg>`, toast: shellToast, shq: s => `'${String(s ?? '').replace(/'/g, "'\\''")}'`,
  suggest() {}, openPane() {}, url() {}, chording: () => false, reconcile() {}, capture() {},
  // The Shapeshift bar's text (shell.filter, sd:5539): a page sets it with this and hears it as shell:filter.
  filter: q => (OUT.shift = String(q ?? '').trim()),
  state: s => OUT.states.push(s), attention: a => OUT.attention.push(a),
  view: { current: () => null, show: () => true, hide() {}, count: (id, n) => { (OUT.counts = OUT.counts || {})[id] = n; } },
  viewPick: (() => { const { html, put } = window.markup;
""" + VIEWPICK_JS + r"""
    return viewPick; })(),
  list: (() => { const { html, plural } = window.markup, ICON = n => html`<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
""" + LIST_JS + r"""
    return list; })(),
  aside: (() => { const { html } = window.markup;
""" + ASIDE_JS + r"""
    return aside; })() };
const cmd = id => REG.find(c => c.id === id);
const flush = async () => { for (let i = 0; i < 400; i++) await null; };
const open = key => document.dispatchEvent(new CustomEvent('shell:open', { detail: String(key) }));
const lastToast = () => OUT.toasts[OUT.toasts.length - 1];
const lastUndo = () => OUT.toasts.filter(t => t.undo).pop();
"""
# The view switch as shell.js answers a page with PAGE_VIEWS (sd:6263): the shown view is #view=<id> when the page
# declares it, else the first; show(id) shows a declared view and fires shell:view, as a tab click does; none() shows none.
VIEW_SHELL = """window.shell.view = { now: undefined, ids: () => (window.PAGE_VIEWS || []).map(v => v.id),
  current() { if (this.now === undefined) { const h = (/^#view=([a-z0-9-]+)$/.exec(location.hash || '') || [])[1]; this.now = this.ids().includes(h) ? h : this.ids()[0] || null; } return this.now; },
  show(id) { if (!this.ids().includes(id)) return false; if (this.current() !== id) { this.now = id; document.dispatchEvent(new CustomEvent('shell:view', { detail: { id, first: true, api: null } })); } return true; },
  none() { this.now = null; }, hide() {}, count: (id, n) => { (OUT.counts = OUT.counts || {})[id] = n; } };
"""
# What Tasks adds to the shared stand-in: shell.row() as shell.js answers it, its realm tabs, and the real reader (sd:2484).
TASKS_SHELL = """window.shell.row = () => new URLSearchParams(location.search).get('row');
""" + VIEW_SHELL + READ_SHELL
# A landed write's readback moves the stored row with it, as the server's next read lists it: the page rereads after each
# landed write, and a reread that brought back the row as it was before would undo the write on the page.
STORED = r"""
const STORED_FIELDS = ['status', 'priority', 'due', 'recurrence', 'recurrence_anchor', 'recurrence_lead'];
function stored(a) {
  const body = a && a[0] < 300 && a[1], row = body && body.item && body.revision && DOC.rows.find(r => r.id === body.item.id);
  if (row) { STORED_FIELDS.forEach(k => { if (k in body.item) row[k] = body.item[k]; }); row.revision = body.revision; }
  return a;
}
const storing = a => a && typeof a.then === 'function' ? a.then(stored) : stored(a);
// The Details and the rows are one item_state: a Details read carries the stored row's revision, as the server's does.
const detailNow = id => { const row = DOC.rows.find(r => String(r.id) === String(id)), d = DETAILS[id];
  return row && d ? Object.assign({}, d, { revision: row.revision }) : d; };
"""


class TheShellRunContract(unittest.TestCase):
    """shell.js's settleOne and settleBulk, run alone: a run may return a promise, and Undo covers only what landed."""

    def settle(self, body):
        script = "var window = globalThis;\n" + MARKUP_JS + SETTLE_JS + r"""
var OUT = { toasts: [], undone: [], error: null }, toast = (msg, undo) => OUT.toasts.push({ msg, undo: undo || null });
const plural = window.markup.plural, flush = async () => { for (let i = 0; i < 50; i++) await null; };
const obj = n => ({ id: String(n), label: '#' + n });
(async () => { try {
""" + body + """
} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();
function run() { OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }
"""
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    COMMAND = """const c = { id: 'x.move', on: 'item', label: 'Move', risk: 'undo',
  undo: (o, v) => { OUT.undone.push([o.id, v]); return v === 'gone' ? false : null; } };
"""

    def test_a_bulk_group_toasts_after_every_run_and_undoes_only_what_landed(self):
        out = self.settle(self.COMMAND + """const objs = [obj(1), obj(2), obj(3)];
const p = settleBulk(c, objs, [Promise.resolve('a1'), Promise.reject(new Error('stale')), 'sync'], { toast, plural });
OUT.early = OUT.toasts.length; OUT.result = await p;
OUT.toasts[0].undo(); await flush();""")
        self.assertEqual(out["early"], 0, "the group toasted before its runs settled")
        self.assertEqual(out["result"], {"landed": 2, "stale": 0, "failed": 1})
        self.assertEqual(out["undone"], [["1", "a1"], ["3", "sync"]])
        self.assertEqual(out["toasts"], [["Move · 2 items · 1 of 3 not changed: #2 (stale)", True],
                                         ["Move undone · 2 of 3 reversed · not reversed: #2 (its change did not land)", False]])

    def test_a_run_that_throws_fails_its_own_row_and_the_rows_after_it_still_run(self):
        # A picked row gone in a refresh makes a run throw before it returns. It is that row's failure, not the group's
        # end: the next row runs, and the group's toast names the thrown reason (review, PR #46).
        out = self.settle(self.COMMAND + """const ran = [], objs = [obj(1), obj(2), obj(3)];
c.run = o => { if (o.id === '2') throw new Error('the task is no longer listed'); ran.push(o.id); return Promise.resolve(o.id); };
OUT.result = await settleBulk(c, objs, runEach(c, objs), { toast, plural }); OUT.ran = ran;""")
        self.assertEqual(out["ran"], ["1", "3"])
        self.assertEqual(out["result"], {"landed": 2, "stale": 0, "failed": 1})
        self.assertEqual(out["toasts"], [["Move · 2 items · 1 of 3 not changed: #2 (the task is no longer listed)", True]])

    def test_thirty_picks_run_three_at_a_time_and_a_reread_waits_for_the_group(self):
        """pack sd:7852: runEach started every picked row at once, thirty requests for thirty picks, and each row's
        reread ran as it landed. Three run at a time now, each next as one settles; the group still toasts once, its
        Undo runs three at a time too, and a reread asked for during the group runs once, after it."""
        out = self.settle(self.COMMAND + """const held = [], objs = Array.from({ length: 30 }, (_, i) => obj(i + 1));
let now = 0, most = 0; OUT.reread = 0;
const hold = () => new Promise(ok => { now++; most = Math.max(most, now); held.push(() => { now--; ok('v'); }); });
c.run = o => hold().then(v => { afterBulk('page', () => { OUT.reread++; }); return v; });
const p = settleBulk(c, objs, runEach(c, objs), { toast, plural }); OUT.first = held.length;
while (held.length) { held.shift()(); await flush(); }
OUT.result = await p; await flush(); OUT.most = most;
c.undo = () => hold(); most = 0; OUT.toasts[0].undo(); await flush(); OUT.undoFirst = held.length;
while (held.length) { held.shift()(); await flush(); }
OUT.undoMost = most; afterBulk('page', () => { OUT.reread += 10; });""")
        self.assertEqual((out["first"], out["most"]), (3, 3))
        self.assertEqual(out["result"], {"landed": 30, "stale": 0, "failed": 0})
        self.assertEqual(out["reread"], 11, "one reread after the group, and one at once with no group running")
        self.assertEqual((out["undoFirst"], out["undoMost"]), (3, 3))
        self.assertEqual(out["toasts"], [["Move · 30 items", True], ["Move undone · 30 of 30 reversed", False]])

    def test_a_bulk_group_with_nothing_landed_offers_no_undo(self):
        out = self.settle(self.COMMAND + """await settleBulk(c, [obj(1)], [Promise.reject(new Error('refused'))], { toast, plural });""")
        self.assertEqual(out["toasts"], [["Move · 0 items · 1 of 1 not changed: #1 (refused)", False]])

    def test_a_bulk_group_names_each_row_it_did_not_change_and_the_stale_ones_apart(self):
        # sd:3012: the group runs every row, and its toast says which landed, which the server refused as stale (409, the
        # rows are read again) and which failed, each with its reason.
        out = self.settle(self.COMMAND + """const stale = Object.assign(new Error('item 2 changed; reload it'), { stale: true });
OUT.result = await settleBulk(c, [obj(1), obj(2), obj(3), obj(4)],
  [Promise.resolve('a'), Promise.reject(stale), Promise.reject(new Error('a recurring task cannot be cancelled')), Promise.resolve('d')], { toast, plural });""")
        self.assertEqual(out["result"], {"landed": 2, "stale": 1, "failed": 1})
        self.assertEqual(out["toasts"], [["Move · 2 items · 1 of 4 refused stale, read again: #2 · 1 of 4 not changed: #3 (a recurring task cannot be cancelled)", True]])

    def test_an_undo_that_had_nothing_to_reverse_is_named(self):
        out = self.settle(self.COMMAND + """await settleBulk(c, [obj(1), obj(2)], [Promise.resolve('gone'), Promise.resolve('b')], { toast, plural });
OUT.toasts[0].undo(); await flush();""")
        self.assertEqual(out["toasts"][-1], ["Move undone · 1 of 2 reversed · not reversed: #1 (nothing to reverse)", False])

    def test_a_single_run_toasts_when_it_lands_and_not_when_it_fails(self):
        out = self.settle(self.COMMAND + """await settleOne(c, obj(1), Promise.resolve({ text: 'Moved #1' }), { toast });
await settleOne(c, obj(2), Promise.reject(new Error('stale')), { toast });
OUT.toasts[0].undo(); await flush();""")
        self.assertEqual(out["toasts"], [["Moved #1", True], ["#2 not changed: stale", False], ["Move undone · #1", False]])
        self.assertEqual(out["undone"], [["1", {"text": "Moved #1"}]])


    def test_ask_first_is_a_flag_or_a_test_of_the_object(self):
        """sd:3392: commands.md lets askFirst test the object (Retry after exit 127); a function is not itself a yes."""
        out = self.settle("""OUT.got = [asksFirst({ askFirst: true }, obj(1)), asksFirst({}, obj(1)),
  asksFirst({ askFirst: o => o.exit === 127 }, { exit: 127 }), asksFirst({ askFirst: o => o.exit === 127 }, { exit: 7 })];""")
        self.assertEqual(out["got"], [True, False, True, False])

    def test_a_command_button_hover_help_says_what_it_does_and_whether_it_asks_or_undoes(self):
        """sd:3457: each command button carries a tip, which the help popover shows after a hover delay.
        Pack sd:7536: the tip says what the press does in plain words; the command it runs stays in the
        "Same thing from the CLI" fold, and a copy-only line says the operator runs it."""
        out = self.settle("""OUT.got = [
  tipOf({ label: 'Ready', cli: o => `sd task status ${o.id} ready`, risk: 'undo', undo() {} }, obj(7)),
  tipOf({ label: 'Remove', cli: () => 'git worktree remove /tmp/x', risk: 'confirm' }, obj(7)),
  tipOf({ label: 'Retry', cli: () => 'sd jobs run x', askFirst: true }, obj(7)),
  tipOf({ label: 'Ask in chat' }, obj(7)),
  tipOf({ label: 'Pin', cli: () => 'no sd verb: the pin lives in the database', risk: 'undo' }, obj(7)),
  tipOf({ label: 'Retry', does: 'Runs the job again now', cli: o => `sd jobs run job-${o.id}`, risk: 'undo', undo() {} }, obj(7)),
  tipOf({ label: 'Hide', does: o => `Hides ${o.id} here only.` }, obj(7))];""")
        self.assertEqual(out["got"], [
            "Ready. Undo offered.",
            "Remove. Gives you a command to run yourself. Asks first.",
            "Retry. Asks first.",
            "Ask in chat.",
            "Pin.",
            # sd:5287: what the command does leads; pack sd:7536: no command follows it.
            "Runs the job again now. Undo offered.",
            "Hides 7 here only.",
        ])
        # The foundation's help popover shows it after --delay-help, in the theme's tokens; a native title would not.
        # The Details bar and a row's text button share one form (sd:5747: rowActions names its command c too).
        self.assertGreaterEqual(SHELL_JS.count("data-cmd=\"${c.id}\" data-obj=\"${id}\" data-tip=\"${tipOf(c, o)}\""), 2)
        self.assertNotIn("title=\"${tipOf(", SHELL_JS)
        self.assertIn("const b = e.target.closest?.('.help, [data-tip]');", SHELL_JS, "the hover delay skips the command buttons")
        self.assertIn("btn.dataset.help != null ? helpText(btn.dataset.help) : tipLines(btn.dataset.tip)", SHELL_JS,
                      "a tip is plain text: a CLI line's <path> is not a tag")

    def test_run_and_bulk_ask_through_the_one_test(self):
        """A guard: run() and runBulk() both read asksFirst, so neither treats a test as always yes or skips it."""
        self.assertTrue("c.risk !== 'confirm' && !asksFirst(c, o) && !fields.length" in SHELL_JS, "run() skips the test")
        self.assertTrue("askFirst: objs.length > 25 || objs.some(o => asksFirst(c, o))" in SHELL_JS, "runBulk() skips the test")
        self.assertIsNone(re.search(r"(?<!!)!c\.askFirst\b", SHELL_JS), "a test of the object read as a flag")

    def test_a_command_dialog_says_cancel_unless_its_act_is_a_cancel(self):
        """sd:5614: "Don't deep review settings" read wrong; the button is Cancel, or Keep it beside "Cancel job"."""
        self.assertNotIn("keep: `Don't", SHELL_JS)
        self.assertIn("keep: /^cancel\\b/i.test(act) ? 'Keep it' : 'Cancel'", SHELL_JS)

class TheConfirmFields(unittest.TestCase):
    """shell.js's confirmAction with fields (sd:2200), run against a stand-in dialog: OK is off until a required field is typed."""

    def confirm(self, body):
        self.assertIsNotNone(CONFIRM, "shell.js has no confirm:start block")
        script = "var window = globalThis;\n" + MARKUP_JS + r"""
const { html } = window.markup, put = (el, m) => { el.html = String(m); }, CLOSE_X = '';
var OUT = { focused: null, result: null, error: null };
const yes = { disabled: false }, no = {}, line = { textContent: '' }, said = { textContent: '' }, inputs = { reason: { value: '', tagName: 'INPUT' } }, L = {};
const form = { elements: inputs, addEventListener: (t, f) => { (L[t] = L[t] || []).push(f); },
  querySelector: s => s === '[value="yes"]' ? yes : s === '#confirm-cli' ? line : s === '#confirm-said' ? said : null,
  requestSubmit: b => { confirmDlg.returnValue = b === yes ? 'yes' : 'no'; confirmDlg.onclose(); } };
var confirmDlg = { html: '', returnValue: '', onclose: null, querySelector: s => s === 'form' ? form : s === '[value="no"]' ? no : null };
const modal = (d, first) => { OUT.focused = first === inputs.reason ? 'reason' : first === no ? 'keep' : null; return null; };
const type = v => { inputs.reason.value = v; (L.input || []).forEach(f => f({})); };
const key = k => { let stopped = false; (L.keydown || []).forEach(f => f({ key: k, target: inputs.reason, preventDefault: () => { stopped = true; } })); return stopped; };
const close = v => { confirmDlg.returnValue = v; confirmDlg.onclose(); };
""" + CONFIRM.group(1) + r"""
(async () => { try {
""" + body + """
} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();
function run() { return JSON.stringify(OUT); }
"""
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    FIELDS = """const p = confirmAction({ title: 'Cancel item: #7 Port the page?', cli: v => `sd work cancel 7 --reason ${v.reason || "'<why>'"}`,
  ok: 'Cancel item', fields: [{ name: 'reason', label: 'Reason', required: true }] });
"""

    def test_a_field_with_help_opens_and_names_its_help(self):
        # Close, Relink and Cancel give their field a help line. The confirm built that attribute so markup.js refused it
        # inside the tag, and the dialog never opened in a browser (found by the sd:3012 browser check).
        out = self.confirm("""const p = confirmAction({ title: 'Close: 2 items?', ok: 'Close',
  fields: [{ name: 'reason', label: 'Reason', required: true, help: 'Recorded on each closed task.' }] });
OUT.form = confirmDlg.html; close('no'); await p;""")
        self.assertIn('aria-describedby="cf-reason-help"', out["form"])
        self.assertIn('<p id="cf-reason-help">Recorded on each closed task.</p>', out["form"])

    def test_ok_stays_off_until_a_required_field_is_typed(self):
        out = self.confirm(self.FIELDS + """OUT.steps = [[yes.disabled, line.textContent]];
type('   '); OUT.steps.push([yes.disabled, line.textContent]);
type('superseded'); OUT.steps.push([yes.disabled, line.textContent]);
close('yes'); OUT.result = await p; OUT.form = confirmDlg.html;""")
        self.assertEqual(out["steps"], [[True, "sd work cancel 7 --reason '<why>'"], [True, "sd work cancel 7 --reason '<why>'"],
                                        [False, "sd work cancel 7 --reason superseded"]])
        self.assertEqual(out["focused"], "reason")
        self.assertEqual((out["result"]["yes"], out["result"]["values"]), (True, {"reason": "superseded"}))
        self.assertIn('id="cf-reason" name="reason"', out["form"])
        self.assertIn('required aria-required="true"', out["form"])
        self.assertIn('value="no" formnovalidate', out["form"])

    def test_a_body_of_the_fields_follows_what_is_typed(self):
        """sd:8014: a consequence that reads the fields (the bulk Decline's rows at a score and age) says what OK will
        do with the values typed now, not with the defaults the dialog opened on."""
        out = self.confirm("""const p = confirmAction({ title: 'Decline?', body: v => `Moves ${v.reason || 'the default'} rows.`,
  fields: [{ name: 'reason', label: 'Reason', value: '' }] });
OUT.opened = [confirmDlg.html.includes('<p id="confirm-said">Moves the default rows.</p>'), said.textContent];
type('9'); OUT.typed = said.textContent; close('no'); await p;""")
        self.assertEqual(out["opened"], [True, "Moves the default rows."])
        self.assertEqual(out["typed"], "Moves 9 rows.")

    def test_the_shell_gives_a_confirm_the_consequence_of_the_typed_values(self):
        """sd:8014: run() hands confirmAction the command's consequence as a function of the typed values."""
        self.assertTrue("body: c.consequence ? v => c.consequence(o, v) : ''" in SHELL_JS,
                        "run() gives confirmAction the consequence of the defaults only")

    def test_a_close_on_ok_with_the_field_empty_runs_nothing_and_enter_is_ok(self):
        out = self.confirm(self.FIELDS + """OUT.enterEmpty = [key('Enter'), confirmDlg.returnValue];
close('yes'); OUT.result = await p;
const q = confirmAction({ title: 't', fields: [{ name: 'reason', label: 'Reason', required: true }] });
type('done elsewhere'); key('Enter'); OUT.second = await q;""")
        self.assertEqual(out["enterEmpty"], [True, ""], "Enter with the field empty closed the dialog")
        self.assertFalse(out["result"]["yes"], "OK ran with the required field empty")
        self.assertEqual((out["second"]["yes"], out["second"]["values"]), (True, {"reason": "done elsewhere"}))

    def test_a_field_with_options_is_a_select_and_answers_the_choice(self):
        """sd:3450: Snooze asks which time in one dialog: a field with options is a select, its first option the default."""
        out = self.confirm("""inputs.until = { value: 'snooze-hour', tagName: 'SELECT' };
const p = confirmAction({ title: 'Snooze job: nightly?', ok: 'Snooze job', fields: [{ name: 'until', label: 'Snooze until',
  options: [{ value: 'snooze', label: 'The next 08:00' }, { value: 'snooze-hour', label: 'One hour from now' }] }] });
OUT.form = confirmDlg.html; OUT.off = yes.disabled; close('yes'); OUT.result = await p;""")
        self.assertIn('<select class="cap-in" id="cf-until" name="until"><option value="snooze">The next 08:00</option>'
                      '<option value="snooze-hour">One hour from now</option></select>', out["form"])
        self.assertNotIn('<input class="cap-in" id="cf-until"', out["form"])
        self.assertFalse(out["off"])
        self.assertEqual((out["result"]["yes"], out["result"]["values"]), (True, {"until": "snooze-hour"}))

    def test_a_confirm_without_fields_is_unchanged(self):
        out = self.confirm("""const p = confirmAction({ title: 'Delete: x?', cli: 'sd x' }); OUT.off = yes.disabled; close('yes'); OUT.result = await p;""")
        self.assertEqual(out["focused"], "keep")
        self.assertFalse(out["off"])
        self.assertEqual((out["result"]["yes"], out["result"]["values"]), (True, {}))


def state_answer(row, **changes):
    """A write's readback, `workflow.item_state` shaped, for a row of the tasks document."""
    item = {"id": row["id"], "status": row["status"], "priority": row["priority"], "due": row["due"],
            "recurrence": row["recurrence"], **changes}
    return {"item": item, "notes": [], "revision": "b" * 64}


def field_help(html):
    """Each Details field label (a `dt` of a `dl.fields`) as its text and whether it carries a help icon (sd:5903)."""
    return [(re.sub(r"<[^>]+>", "", dt).strip(), 'class="help"' in dt)
            for dl in re.findall(r'<dl class="fields">(.*?)</dl>', html, re.S) for dt in re.findall(r"<dt>(.*?)</dt>", dl, re.S)]


class TheScript(ScreenCase):
    """tasks.js against the documents `tasks_screen` builds for the seeded database."""

    def setUp(self):
        super().setUp()
        self.ids = seed(self)
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {str(i): tasks_screen.details(self.connection, i, now=NOW) for i in self.ids.values()}

    def row(self, name):
        return next(r for r in self.doc["rows"] if r["id"] == self.ids[name])

    def run_page(self, body, answer="null", *, prelude="", env=None, search="", hash=""):
        """Load tasks.js at `search` and `hash`, fire DOMContentLoaded, run `body` (async), and return OUT plus what `body`
        set on R.

        Each address the page writes is in OUT.urls, as shell.url() received it. The page opens on Personal, the first realm
        tab, unless the address names another; without realms.conf every row sits there (pack sd:7216).
        """
        script = (prelude + STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + TASKS_SHELL
                  + f"\nconst DOC = {json.dumps(self.doc)}, DETAILS = {json.dumps(self.details)};\n"
                  + "const WRITE = " + answer + ";\nvar DETAIL = null;\n" + STORED
                  + f"location.search = {json.dumps(search)}; location.hash = {json.dumps(hash)}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q.toString());\n"
                  + """ANSWER = (path, body) => {
  if (path === '/api/tasks') return typeof FAIL_TASKS === 'string' ? [500, { error: FAIL_TASKS }] : [200, JSON.parse(JSON.stringify(DOC))];
  var m = path.match(/^\\/api\\/tasks\\/(\\d+)$/); if (m) return typeof DETAIL === 'function' ? DETAIL(m[1]) : m[1] in DETAILS ? [200, detailNow(m[1])] : [404, { error: 'no item ' + m[1] }];
  return WRITE ? storing(WRITE(path, body)) : [404, { error: 'no answer' }];
};\n""" + COMMANDS_JS + TASK_DETAILS_JS + TASKS_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False,
                                env=None if env is None else {**os.environ, **env})
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_every_command_is_registered_with_its_risk_key_and_run_path(self):
        out = self.run_page("""R.reg = REG.map(c => [c.id, c.on, c.risk, c.key || null,
  typeof c.executes === 'boolean' ? c.executes : null, typeof c.run === 'function', typeof c.undo === 'function']);""")
        statuses = [[f"item.status.{s}", "item", "undo", str(i + 1), None, True, True]
                    for i, s in enumerate(["planning", "ready", "in_progress", "blocked", "done"])]
        self.assertEqual(out["R"]["reg"], statuses + [
            ["item.complete", "item", "confirm", "5", True, True, False],
            ["item.edit", "item", "safe", "e", None, True, False],
            ["item.move", "item", "safe", "m", None, True, False],
            ["item.p1", "item", "undo", None, None, True, True],
            ["item.p2", "item", "undo", None, None, True, True],
            ["item.p3", "item", "undo", None, None, True, True],
            ["item.p4", "item", "undo", None, None, True, True],
            ["item.close", "item", "confirm", None, True, True, False],
            ["item.park", "item", "undo", None, True, True, True],
            ["item.unpark", "item", "safe", None, True, True, False],
            ["item.note", "item", "safe", "n", None, True, False],
            ["item.fix", "item", "safe", "f", True, True, False],
            ["item.adversarial-review", "item", "safe", None, True, True, False],
            ["item.email", "item", "safe", "d", True, True, False],
            ["item.file-github-issue", "item", "safe", None, True, True, False],
            ["item.file-jira-issue", "item", "safe", None, True, True, False],
            ["issue.file", "issue draft", "confirm", None, True, True, False],
            ["issue.check", "issue draft", "safe", None, True, True, False],
            ["issue.discard", "issue draft", "safe", None, True, True, False],
            ["item.delete", "item", "confirm", None, None, False, False],
            ["work.relink", "item", "safe", "l", True, True, False],
            ["work.cancel", "item", "confirm", "w", True, True, False],
            ["note.resolve", "note", "confirm", "v", None, True, False],
            ["asg.cancel", "assignment", "confirm", "x", None, True, False],
            ["asg.get", "assignment", "safe", "o", True, True, False],  # runs sd assignments get (sd:6682)
            ["item.build", "item", "safe", None, True, True, False],  # Build (pack sd:6095)
            ["realm-sweep.run", "sweep", "safe", None, True, True, False],  # Realm Personal and Work (sd:5096, sd:5097)
        ])

    # sd:5064: the List / Board / Matrix switch is one icon button whose menu lists the three views.
    VIEW_MENU = r"""const items = ['list', 'board', 'matrix'].map(v => { const b = El('item-' + v); b.dataset.view = v; b.attrs = {};
  b.setAttribute = (k, x) => { b.attrs[k] = String(x); }; b.getAttribute = k => k in b.attrs ? b.attrs[k] : null; b.focus = () => { document.activeElement = b; OUT.focus.push(v); }; return b; });
OUT.focus = []; const btn = ELS['view-btn'], menu = ELS['view-menu']; btn.attrs = {}; btn.setAttribute = (k, x) => { btn.attrs[k] = String(x); };
btn.focus = () => { document.activeElement = btn; OUT.focus.push('button'); };
menu.querySelectorAll = () => items; menu.hidden = true;
const key = (k) => { const ev = { key: k, preventDefault() { ev.prevented = true; }, stopPropagation() { ev.stopped = true; } }; menu.listeners.keydown[0](ev); return ev; };
"""

    def test_the_view_button_shows_the_current_views_icon_and_names_it(self):
        out = self.run_page(self.VIEW_MENU + """document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'matrix' })); await flush();
R.label = btn.attrs['aria-label']; R.icon = btn.html; R.checked = items.map(b => b.attrs['aria-checked']);""")
        self.assertEqual(out["R"]["label"], "View: Matrix")
        self.assertIn('<use href="#i-grid-2x2"/>', out["R"]["icon"])
        self.assertEqual(out["R"]["checked"], ["false", "false", "true"])
        self.assertIn("view=matrix", out["urls"][-1])

    def test_the_view_menu_opens_on_the_checked_view_and_arrows_escape_and_a_pick_work(self):
        out = self.run_page(self.VIEW_MENU + """document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'board' })); await flush();
btn.listeners.click[0]({ target: btn }); R.open = [menu.hidden, btn.attrs['aria-expanded'], OUT.focus.slice()];
key('ArrowDown'); key('ArrowDown'); key('ArrowUp'); key('End'); key('Home'); R.moved = OUT.focus.slice(1);
const letter = key('j'); R.letter = !!letter.stopped;
const esc = key('Escape'); R.esc = [menu.hidden, btn.attrs['aria-expanded'], OUT.focus.at(-1), !!esc.stopped];
btn.listeners.click[0]({ target: btn }); menu.listeners.click[0]({ target: { closest: s => s === '[data-view]' ? items[0] : null } }); await flush();
R.pick = [menu.hidden, btn.attrs['aria-label'], OUT.focus.at(-1)];""")
        self.assertEqual(out["R"]["open"], [False, "true", ["board"]])
        self.assertEqual(out["R"]["moved"], ["matrix", "list", "matrix", "matrix", "list"])
        self.assertTrue(out["R"]["letter"], "a letter in the menu reached the shell's keys")
        self.assertEqual(out["R"]["esc"], [True, "false", "button", True])
        self.assertEqual(out["R"]["pick"], [True, "View: List", "button"])
        self.assertIn("view=list", out["urls"][-1])

    def test_the_view_menu_markup_is_a_menu_of_radio_items_and_keeps_the_keys(self):
        page = (V2 / "tasks.html").read_text(encoding="utf-8")
        self.assertIn('<button type="button" class="viewpick-btn" id="view-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="view-menu" aria-label="View: Board">', page)
        self.assertIn('<div class="actmenu viewmenu" id="view-menu" role="menu" aria-label="View" hidden>', page)
        self.assertEqual(re.findall(r'role="menuitemradio" data-view="(\w+)" aria-checked="(\w+)"><svg class="i" aria-hidden="true"><use href="#i-([\w-]+)"/></svg><span>(\w+)</span><kbd>(v \w)</kbd>', page),
                         [("list", "false", "list-todo", "List", "v l"), ("board", "true", "kanban", "Board", "v b"), ("matrix", "false", "grid-2x2", "Matrix", "v m")])
        self.assertNotIn('class="seg" role="group" aria-label="View"', page)
        self.assertIn('aria-label="Help: views"', page)

    def test_the_boards_how_to_is_the_help_line_under_it_not_the_view_text(self):
        """sd:5733: the operator moved "Drag a card, or select it and press 1-5 or use Move to." under the board."""
        out = self.run_page("R.board = ELS['view-board'].html; R.sub = ELS.subhead.html;\n"
                            "document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'matrix' })); await flush();\n"
                            "R.matrix = ELS['view-matrix'].html; R.msub = ELS.subhead.html;")
        self.assertIn("Board of the framework statuses.", out["R"]["sub"])
        self.assertIn("urgency is derived from the due date.", out["R"]["msub"])
        for sub in (out["R"]["sub"], out["R"]["msub"]):
            self.assertNotRegex(sub, r"Drag|press 1")
        self.assertRegex(out["R"]["board"], r'</div><p class="list-help"><span>Drag a card, or select it and press 1–5 or use Move to\.</span></p>$')
        self.assertRegex(out["R"]["matrix"], r'<p class="list-help"><span>Drag a card, or select it and press 1–4\.</span></p>$')

    def test_the_page_reads_the_rows_and_draws_the_kind_and_repo_filters(self):
        out = self.run_page("R.filters = ELS.filters.html; R.board = ELS['view-board'].html; R.sub = ELS.subhead.html;")
        self.assertEqual(out["gets"], ["/api/tasks"])
        self.assertEqual(out["states"][0]["kind"], "loading")
        self.assertIsNone(out["states"][-1])
        filters = out["R"]["filters"]
        # sd:3441: Kind is a combobox; sd:5064: so is every other group, and the active-filter chips are the only chips; sd:5158: Repo is a menu.
        self.assertIn('<label class="label" for="f-kind">Kind</label><select id="f-kind" class="fsel" data-pick="kind"><option value="" selected>All</option>'
                      '<option value="followup">followup</option><option value="task">task</option><option value="work">work</option></select>', filters)
        self.assertIn('<details class="fpick" data-pick="repo">', filters)
        self.assertNotIn('data-f="kind"', filters)
        self.assertNotIn('data-f=', filters)
        self.assertIn('<select id="f-p" class="fsel" data-pick="p"><option value="" selected>All</option><option value="1">P1</option>', filters)
        self.assertIn('<option value="unset">unset</option></select>', filters)
        self.assertIn("Port the page", out["R"]["board"])
        self.assertEqual(out["counts"]["personal"], 3, "sd:5524, sd:6263: the Personal tab carries the count")
        self.assertNotIn("3 tasks", out["R"]["sub"])
        # The badge is the read's own (sd:3446): at NOW the seeded due date is 3 days off, so nothing is due yet.
        self.assertEqual(out["attention"][-1], {"state": "ok", "n": 0, "what": "nothing due"})
        self.assertEqual(out["attention"][-1], self.doc["attention"])

    def test_a_combobox_picks_one_value_and_all_clears_it(self):
        out = self.run_page("""const pick = (key, value) => ELS.filters.listeners.change[0]({ target: { dataset: { pick: key }, value } });
pick('p', '1'); R.one = ELS.filters.html; R.url = OUT.urls[OUT.urls.length - 1];
pick('p', ''); R.url2 = OUT.urls[OUT.urls.length - 1];""", search="?kind=task,work")
        self.assertIn('<option value="1" selected>P1</option>', out["R"]["one"])
        # Two kinds from the address show as a count until one is picked.
        self.assertIn('<option value="" disabled selected>2 picked</option>', out["R"]["one"])
        self.assertIn("p=1", out["R"]["url"])
        self.assertNotIn("p=", out["R"]["url2"])

    def test_kind_holds_the_plugin_kinds_and_an_old_base_address_still_filters(self):
        # sd:5644: Kind and Base were two filters over one question; Base's plugin kinds are Kind's last options now.
        self.doc["rows"] = [*self.doc["rows"], {**self.doc["rows"][0], "id": 901, "title": "A blog idea", "base": "blog-idea"}]
        out = self.run_page("R.filters = ELS.filters.html; R.board = ELS['view-board'].html;", search="?base=blog-idea")
        filters = out["R"]["filters"]
        self.assertNotIn('data-pick="base"', filters)
        self.assertIn('<option value="base:blog-idea" selected>blog-idea (plugin kind)</option></select>', filters)
        self.assertIn("A blog idea", out["R"]["board"])
        self.assertNotIn("Port the page", out["R"]["board"])

    def test_a_realm_tab_counts_open_rows_only(self):
        """sd:8018: a done row (a cancelled one is done too) stays on the board but leaves the tab's count."""
        row = self.doc["rows"][0]
        self.doc["rows"] = [*self.doc["rows"], {**row, "id": 903, "key": "903", "title": "Done this week", "status": "done"}]
        out = self.run_page("R.board = ELS['view-board'].html;", search="?done=1")
        self.assertIn("Done this week", out["R"]["board"])
        self.assertEqual(out["counts"]["personal"], 3, "sd:8018: the tab counted a done row")

    def test_ideas_leave_the_rows_and_counts_until_kind_picks_them(self):
        """pack sd:6743: an idea kind shows in the Writing and Vault views only; Kind picks it, as the Vault view."""
        self.assertEqual(self.doc["ideas"], ["idea", "work-idea", "personal-idea"])
        self.doc["rows"] = [*self.doc["rows"], {**self.doc["rows"][0], "id": 902, "title": "A vault idea", "kind": "idea", "base": "blog-idea"}]
        out = self.run_page("R.board = ELS['view-board'].html; R.filters = ELS.filters.html;")
        self.assertNotIn("A vault idea", out["R"]["board"])
        self.assertEqual(out["counts"]["personal"], 3)
        self.assertIn('<option value="idea">idea</option>', out["R"]["filters"])
        for search in ("?kind=idea", "?base=blog-idea"):
            self.assertIn("A vault idea", self.run_page("R.board = ELS['view-board'].html;", search=search)["R"]["board"])

    def test_repo_is_a_multiselect_dropdown_and_two_repos_show_their_union(self):
        """sd:5158: Repo is a menu of checkboxes; two picks show the tasks of both, and the address names both."""
        box = "const box = (v, on) => ELS.filters.listeners.change[0]({ currentTarget: ELS.filters, target: { dataset: { pick: 'repo' }, type: 'checkbox', value: v, checked: on, getAttribute: () => null } });\n"
        out = self.run_page(box + """R.before = ELS.filters.html; box('system', true); R.one = ELS['view-board'].html;
box('no repo', true); R.two = ELS['view-board'].html; R.after = ELS.filters.html; R.url = OUT.urls[OUT.urls.length - 1];""")
        before, after = out["R"]["before"], out["R"]["after"]
        self.assertEqual(re.findall(r'<input type="checkbox" data-pick="repo" value="([^"]+)"', before), ["no repo", "system"])
        self.assertNotIn('<select id="f-repo"', before)
        self.assertIn("Port the page", out["R"]["one"])
        self.assertNotIn("Answer the question", out["R"]["one"])
        self.assertIn("Answer the question", out["R"]["two"])
        self.assertRegex(after, r'<details class="fpick" data-pick="repo" open><summary[^>]*><span id="f-repo-v">2 picked</span>')
        self.assertIn("repo=system%2Cno%20repo", out["R"]["url"])

    def test_a_repo_pick_keeps_the_repo_control_its_size_so_its_menu_stays_under_the_pointer(self):
        """sd:6254: the operator's Repo pick did not work. Picking a repo widened the summary from All to the repo's name;
        the control wrapped to the bar's next line, the open menu went with it, and the next click closed it."""
        box = "const box = (v, on) => ELS.filters.listeners.change[0]({ currentTarget: ELS.filters, target: { dataset: { pick: 'repo' }, type: 'checkbox', value: v, checked: on, getAttribute: () => null } });\n"
        out = self.run_page(box + "R.before = ELS.filters.html; box('system', true); R.after = ELS.filters.html;")
        size = r'<details class="fpick" data-pick="repo"[^>]*><summary[^>]*><span id="f-repo-v">[^<]*</span>(.*?)</summary>'
        before, after = (re.search(size, out["R"][k], re.S).group(1) for k in ("before", "after"))
        self.assertEqual(before, '<span class="fpick-size" aria-hidden="true"><span>All</span><span>2 picked</span>'
                                 '<span>no repo</span><span>system</span></span>')
        self.assertEqual(after, before)
        # The address names both picks, and a reload of it shows both.
        again = self.run_page("R.filters = ELS.filters.html; R.board = ELS['view-board'].html;", search="?repo=system,no%20repo")
        self.assertEqual(re.findall(r'data-pick="repo" value="([^"]+)" checked', again["R"]["filters"]), ["no repo", "system"])
        self.assertIn("Port the page", again["R"]["board"])
        self.assertIn("Answer the question", again["R"]["board"])

    def test_the_board_done_column_shows_only_the_last_seven_days(self):
        """sd:5305: the board's Done column holds items done in the 7 days before the read; the list keeps every one."""
        import copy
        import datetime as dt
        read = dt.datetime.fromisoformat(self.doc["read"].replace("Z", "+00:00"))
        doc = copy.deepcopy(self.doc)
        old, new = doc["rows"][0], doc["rows"][1]
        for row, days, title in ((old, 8, "Done long ago"), (new, 6, "Done this week")):
            row.update(status="done", title=title,
                       status_since=(read - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z"))
        saved, self.doc = self.doc, doc
        try:
            out = self.run_page("R.board = ELS['view-board'].html;", search="?done=1")
        finally:
            self.doc = saved
        board = out["R"]["board"]
        done = re.search(r'aria-label="Done">.*?</div>\s*</div>\s*</div>', board, re.S).group(0)
        self.assertIn("Done this week", done)
        self.assertNotIn("Done long ago", board)
        self.assertIn('title="done in the last 7 days"', done)

    def test_the_board_hides_done_until_show_done_and_the_status_filter_is_gone(self):
        """sd:6233: Tasks has no Status dropdown; the board's Show done checkbox shows its Done column, hidden by default
        (operator decision, note 30400), and the address keeps it as done=1. The list ignores it and keeps its done rows;
        the matrix never shows done ones."""
        import copy
        import datetime as dt
        read = dt.datetime.fromisoformat(self.doc["read"].replace("Z", "+00:00"))
        doc = copy.deepcopy(self.doc)
        doc["rows"][0].update(status="done", title="Done yesterday", status_since=(read - dt.timedelta(days=1)).isoformat().replace("+00:00", "Z"))
        saved, self.doc = self.doc, doc
        try:
            out = self.run_page("""R.filters = ELS.filters.html; R.board = ELS['view-board'].html;
ELS.filters.listeners.change.forEach(f => f({ target: { id: 'f-done', checked: true } })); await flush();
R.board2 = ELS['view-board'].html; R.filters2 = ELS.filters.html; R.url = OUT.urls[OUT.urls.length - 1];
document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'list' })); await flush(); R.listFilters = ELS.filters.html; R.list = ELS['view-list'].html;""")
            linked = self.run_page("R.board = ELS['view-board'].html; R.filters = ELS.filters.html;", search="?done=1")
        finally:
            self.doc = saved
        R = out["R"]
        self.assertNotIn('id="f-status"', R["filters"])
        self.assertIn('<label class="chk"><input type="checkbox" id="f-done"> Show done</label>', R["filters"])
        self.assertNotIn('aria-label="Done"', R["board"])
        self.assertNotIn("Done yesterday", R["board"])
        self.assertIn("data-nodone", R["board"])
        self.assertIn('aria-label="Done"', R["board2"])
        self.assertIn("Done yesterday", R["board2"])
        self.assertNotIn("data-nodone", R["board2"])
        self.assertIn('<input type="checkbox" id="f-done" checked>', R["filters2"])
        self.assertIn("done=1", R["url"])
        self.assertNotIn('id="f-done"', R["listFilters"])
        self.assertIn("Done yesterday", R["list"])
        self.assertIn("Done yesterday", linked["R"]["board"])
        self.assertIn('<input type="checkbox" id="f-done" checked>', linked["R"]["filters"])

    def test_the_kind_filter_narrows_the_rows(self):
        out = self.run_page("""ELS.filters.listeners.change[0]({ target: { dataset: { pick: 'kind' }, value: 'work' } });
R.board = ELS['view-board'].html;""")
        self.assertIn("Port the page", out["R"]["board"])
        self.assertNotIn("Plan the review", out["R"]["board"])

    def test_details_show_history_notes_assignments_artifact_and_external_context(self):
        plan, port = self.ids["plan"], self.ids["port"]
        out = self.run_page(f"""open({plan}); await flush(); R.plan = ELS.details.html;
open({port}); await flush(); R.port = ELS.details.html;""")
        self.assertEqual(out["gets"], ["/api/tasks", f"/api/tasks/{plan}", f"/api/tasks/{port}"])
        plan_html, port_html = out["R"]["plan"], out["R"]["port"]
        for heading in ("Status history", "Notes", "Assignments", "Artifact", "Recurrence", "External context"):
            self.assertIn(f"<h3>{heading}", plan_html)
        self.assertIn("2 notes · 1 open followup", plan_html)
        self.assertIn("Check the budget first", plan_html)
        self.assertIn("https://example.invalid/i/1", plan_html)
        self.assertIn("No assignment has run on this item.", plan_html)
        self.assertNotIn("External context", port_html)
        self.assertIn("<b>running</b>", port_html)
        self.assertIn("docs/work/port/prd.md", port_html)

    def linked_pull(self, *, draft):
        """Pack sd:7868: link a pull request on the seeded `plan` task, as a build in an unmanaged repository does, and
        observe it open, as the operator's draft or not; then rebuild the Details documents."""
        from sd_db import contributions

        plan, url = self.ids["plan"], "https://github.com/example/anvil/pull/41"
        contributions.configure(self.connection, plan, {"pull_url": url}, who="build")
        observed = {"complete": True, "observed_at": NOW, "operator": {"id": "1", "login": "author"},
                    "author": {"id": "1", "login": "author"}, "repo": "example/anvil", "title": "Plan the review",
                    "state": "open", "head": "a" * 40, "base": "b" * 40, "draft": draft, "mergeable": "mergeable",
                    "ci": "success", "ci_head": "a" * 40, "ci_ids": ["check:1"], "why": ["author"], "blocking_labels": [],
                    "labels": [], "reviews": [], "events": []}
        contributions.observe_pull(self.connection, url, observed, expected_revision=contributions.snapshot(
            self.connection, "github:" + url)["revision"])
        self.details[str(plan)] = tasks_screen.details(self.connection, plan, now=NOW)
        return plan, url

    def test_details_link_the_items_pull_request_in_a_new_tab(self):
        """Pack sd:7868: the pull request a build opened is one click away on the item's Details; a ready one is no flag."""
        plan, url = self.linked_pull(draft=False)
        out = self.run_page(f"open({plan}); await flush(); R.html = ELS.details.html;")
        html = out["R"]["html"]
        self.assertIn("<h3>Pull request", html)
        self.assertIn(f'<a class="ext" href="{url}" target="_blank" rel="noopener noreferrer"', html)
        self.assertNotIn("data-needs-you", html)
        port = self.ids["port"]
        out = self.run_page(f"open({port}); await flush(); R.html = ELS.details.html;")
        self.assertNotIn("<h3>Pull request", out["R"]["html"])

    def test_details_flag_the_operators_open_draft_as_needing_them(self):
        """Pack sd:7868: a draft the build opened waits on the operator (mark it ready, or hand it to the owners)."""
        from sd_db import contributions

        plan, url = self.linked_pull(draft=True)
        out = self.run_page(f"open({plan}); await flush(); R.html = ELS.details.html;")
        html = out["R"]["html"]
        self.assertIn(f'href="{url}"', html)
        self.assertIn("awaiting you", html)
        self.assertIn('<p class="why" data-needs-you=""><b>Needs you:</b>', html)
        self.assertIn(contributions.DRAFTED, html)

    def test_details_show_what_each_assignment_and_the_item_cost(self):
        """sd:6031: an item page shows its cost, each assignment's spent cost rows, `~` when one is an estimate."""
        port = self.ids["port"]
        (asg,) = self.details[str(port)]["assignments"]
        asg.update(usd=1.25, estimated=True)
        out = self.run_page(f"open({port}); await flush(); R.port = ELS.details.html;")
        self.assertIn('Assignments <span class="n">~$1.25 spent</span>', out["R"]["port"])
        self.assertIn("<b>running</b> · ~$1.25", out["R"]["port"])

    # sd:3439: the Details fields edit in place. A field's change is the write its command makes, with the same toast and Undo.
    ECHO = """(path, body) => { const row = DOC.rows.find(r => path.startsWith('/api/items/' + r.id)); window.WROTE = (window.WROTE || 0) + 1;
  const item = { id: row.id, status: body.status || row.status, priority: 'priority' in body ? body.priority : row.priority,
    due: 'due' in body ? body.due : row.due, recurrence: row.recurrence, title: body.title || row.title, repo: 'repo' in body ? body.repo : row.repo_path };
  return [200, { item, notes: [], revision: String(window.WROTE % 10).repeat(64) }]; }"""
    CHANGE = "const change = (id, value) => ELS.details.listeners.change.forEach(f => f({ target: { id, value } }));\n"

    def test_the_details_draw_status_priority_due_repo_and_quadrant_as_fields(self):
        plan, port = self.ids["plan"], self.ids["port"]
        self.row("port")["edit"] = {"allowed": False, "reason": "message items use their own editing workflow"}
        out = self.run_page(f"open({plan}); await flush(); R.plan = ELS.details.html; open({port}); await flush(); R.port = ELS.details.html;")
        html = out["R"]["plan"]
        self.assertRegex(html, r'<select id="det-status"[^>]*>.*<option value="ready" selected>Ready</option>')
        self.assertRegex(html, r'<select id="det-p"[^>]*>.*<option value="2" selected>P2</option>')
        self.assertIn('<input id="det-due" class="fld" type="date" value="2026-09-09">', html)
        self.assertRegex(html, r'<select id="det-repo"[^>]*><option value="">No repo</option>.*selected')
        self.assertRegex(html, r'<select id="det-q"[^>]*>')
        # A row edit_item refuses has its fields off, with the reason; a work item's repo is its checkout's.
        self.assertRegex(out["R"]["port"], r'<select id="det-p" class="fld" disabled aria-describedby="det-off">')
        self.assertIn('<p class="why" id="det-off">The fields are off: message items use their own editing workflow.</p>', out["R"]["port"])
        self.assertRegex(out["R"]["port"], r'<select id="det-repo" class="fld" disabled')
        self.assertNotRegex(html, r'<select id="det-[a-z]+" class="fld" disabled')

    def test_every_details_field_label_carries_help(self):
        # sd:5903: each field in the Details lists carries the shared help icon, as each section heading does.
        plan, port = self.ids["plan"], self.ids["port"]
        self.schedule_plan(2)
        self.details[str(plan)]["experiment"] = {"value": "yes", "row": None, "choices": [], "reason": None}
        out = self.run_page(f"open({plan}); await flush(); R.plan = ELS.details.html; open({port}); await flush(); R.port = ELS.details.html;")
        fields = field_help(out["R"]["plan"]) + field_help(out["R"]["port"])
        self.assertEqual({name for name, _ in fields}, {"Id", "Title", "Status", "Kind", "Priority", "Due", "Repeat", "Lead", "Repo", "Area",
                                                        "Urgent", "Quadrant", "Row", "Assignment"})
        self.assertEqual([name for name, helped in fields if not helped], [])

    def test_status_priority_and_edit_leave_the_act_bar_to_the_details_fields(self):
        """sd:5295: Status, Priority and Due are Details fields, so their commands are `bar: false`: off the Act bar, while
        1-5, e and m, the ⋯ menu, the palette and the bulk bar still run them (test_v2_commands checks the bar honours it)."""
        out = self.run_page("R.off = REG.filter(c => c.bar === false).map(c => c.id);")
        self.assertEqual(sorted(out["R"]["off"]), sorted([f"item.status.{s}" for s in ("planning", "ready", "in_progress", "blocked", "done")]
                                                        + ["item.complete", "item.p1", "item.p2", "item.p3", "item.p4", "item.edit", "item.move"]))

    def test_status_and_priority_leave_the_row_menu_and_keep_their_keys(self):
        """sd:6687: Tasks, Home, HOA and the place pages share these commands; the ⋯ menu drops Status → and Edit → P,
        while keys 1-5, bulk and the Details fields still change them."""
        out = self.run_page("R.off = REG.filter(c => c.menu === false).map(c => c.id); R.keys = REG.filter(c => c.menu === false && c.key).map(c => c.key);")
        self.assertEqual(sorted(out["R"]["off"]), sorted([f"item.status.{s}" for s in ("planning", "ready", "in_progress", "blocked", "done")]
                                                        + ["item.p1", "item.p2", "item.p3", "item.p4"]))
        self.assertEqual(out["R"]["keys"], ["1", "2", "3", "4", "5"])

    def test_a_field_change_writes_once_with_its_cli_line_and_undo(self):
        plan = self.ids["plan"]
        row = self.row("plan")
        out = self.run_page(self.CHANGE + f"""open({plan}); await flush();
change('det-p', '1'); await flush(); lastUndo().undo(); await flush();
change('det-repo', ''); await flush();
change('det-due', ''); await flush();
change('det-status', 'blocked'); await flush();
change('det-p', '1'); change('det-p', '1'); await flush();""", self.ECHO)
        bodies = [(p[0], {k: v for k, v in p[1].items() if k != "revision"}) for p in out["posts"]]
        self.assertEqual(bodies, [
            (f"/api/items/{plan}", {"priority": 1}), (f"/api/items/{plan}", {"priority": 2}),
            (f"/api/items/{plan}", {"repo": None}), (f"/api/items/{plan}", {"due": None}),
            (f"/api/items/{plan}/status", {"status": "blocked"}), (f"/api/items/{plan}", {"priority": 1})])
        self.assertEqual(out["posts"][0][1]["revision"], row["revision"])
        msgs = [t[0] for t in out["toasts"]]
        self.assertIn(f"#{plan} priority P2 → P1 · sd task edit {plan} --priority 1", msgs)
        self.assertIn(f"#{plan} repo → none · sd task edit {plan} --no-repo", msgs)
        self.assertIn(f"#{plan} due removed · sd task edit {plan} --clear-due", msgs)

    # sd:6231: the write moves the stored row's kind and repo too, so the reread after it lists the row as the server would.
    KIND_ECHO = """(path, body) => { const row = DOC.rows.find(r => path.startsWith('/api/items/' + r.id)); window.WROTE = (window.WROTE || 0) + 1;
  if ('kind' in body) row.kind = body.kind; if ('repo' in body) { row.repo_path = body.repo; if (!body.repo) row.repo = 'no repo'; }
  const item = { id: row.id, status: row.status, priority: row.priority, due: row.due, recurrence: row.recurrence, title: row.title, kind: row.kind, repo: row.repo_path };
  return [200, { item, notes: [], revision: String(window.WROTE % 10).repeat(64) }]; }"""
    ASKED = "window.shell.confirm = a => { OUT.asked = (OUT.asked || []).concat([[a.title, a.body, a.cli]]); return Promise.resolve(YES.shift()); };\n"

    def test_the_kind_field_changes_the_kind_and_a_repo_less_kind_confirms_clearing_the_repo(self):
        """sd:6231: Task Details' Kind select writes what `sd task edit --kind` writes; a move to an idea kind
        clears the repo in the same edit, after a confirm that says so. A kind with a producer is off with the reason."""
        plan, ask, port = self.ids["plan"], self.ids["ask"], self.ids["port"]
        out = self.run_page(self.CHANGE + self.ASKED + f"""var YES = [false, true];
open({plan}); await flush(); R.plan = ELS.details.html; open({port}); await flush(); R.port = ELS.details.html;
open({ask}); await flush(); change('det-kind', 'task'); await flush(); R.askUndo = !!lastUndo(); await lastUndo().undo(); await flush();
open({plan}); await flush(); change('det-kind', 'work-idea'); await flush(); R.declined = OUT.posts.length;
change('det-kind', 'work-idea'); await flush(); R.kind = DOC.rows.find(r => r.id === {plan}).kind;""", self.KIND_ECHO)
        # sd:7004: `personal` left the hand kinds; the select offers the four `workflow.HAND_KINDS`.
        self.assertRegex(out["R"]["plan"], r'<select id="det-kind" class="fld"><option value="task" selected>task</option>'
                                           r'<option value="followup">followup</option>'
                                           r'<option value="work-idea">work-idea</option><option value="personal-idea">personal-idea</option></select>')
        self.assertEqual(workflow.HAND_KINDS, ("task", "followup", "work-idea", "personal-idea"))
        self.assertIn('<select id="det-kind" class="fld" disabled title="work items are owned by their producer; their kind cannot be changed here">'
                      '<option value="work" selected>work</option>', out["R"]["port"])
        bodies = [(p[0], {k: v for k, v in p[1].items() if k != "revision"}) for p in out["posts"]]
        self.assertEqual(bodies, [(f"/api/items/{ask}", {"kind": "task"}), (f"/api/items/{ask}", {"kind": "followup"}),
                                  (f"/api/items/{plan}", {"kind": "work-idea", "repo": None})])
        self.assertTrue(out["R"]["askUndo"])
        self.assertEqual(out["R"]["declined"], 2, "a declined confirm wrote nothing")
        self.assertEqual(len(out["asked"]), 2)
        title, body, cli = out["asked"][0]
        self.assertEqual(title, f"Kind → work-idea: #{plan}?")
        self.assertIn("clears its repo", body)
        self.assertEqual(cli, f"sd task edit {plan} --kind work-idea --no-repo")
        msgs = [t[0] for t in out["toasts"]]
        self.assertIn(f"#{ask} kind followup → task · sd task edit {ask} --kind task", msgs)
        self.assertIn(f"#{plan} kind task → work-idea, repo cleared · sd task edit {plan} --kind work-idea --no-repo", msgs)
        self.assertEqual(out["R"]["kind"], "work-idea")

    # pack sd:8021: the write sets the row's own area as the reread would (`area_set`, pack sd:8010).
    AREA_ECHO = """(path, body) => { const row = DOC.rows.find(r => path.startsWith('/api/items/' + r.id)); window.WROTE = (window.WROTE || 0) + 1;
  if ('area' in body) { row.area = body.area; row.area_set = !!body.area; }
  const item = { id: row.id, status: row.status, priority: row.priority, due: row.due, recurrence: row.recurrence, title: row.title, kind: row.kind, repo: row.repo_path };
  return [200, { item, notes: [], revision: String(window.WROTE % 10).repeat(64) }]; }"""

    def test_the_area_field_asks_first_then_sets_or_clears_the_items_own_area(self):
        """pack sd:8021: Task Details' Area select (Home / HOA / none) writes what `sd task edit --area` and
        `--clear-area` write, each only after a confirm; a declined confirm writes nothing. Its Undo sets the
        own area back, not the area the repo rule gives."""
        plan = self.ids["plan"]
        self.row("plan").update(area=None, area_set=False)
        out = self.run_page(self.CHANGE + self.ASKED + f"""var YES = [false, true, true, true];
open({plan}); await flush(); R.html = ELS.details.html;
change('det-area', 'home'); await flush(); R.declined = OUT.posts.length;
change('det-area', 'home'); await flush(); R.set = ELS.details.html;
await lastUndo().undo(); await flush();
change('det-area', 'hoa'); await flush(); change('det-area', ''); await flush();""", self.AREA_ECHO)
        self.assertEqual(self.options(out["R"]["html"], "det-area"),
                         [("", " selected", "None"), ("home", "", "Home"), ("hoa", "", "HOA"), ("personal", "", "Personal")])
        self.assertEqual(out["R"]["declined"], 0, "a declined confirm wrote nothing")
        self.assertIn(("home", " selected", "Home"), self.options(out["R"]["set"], "det-area"))
        bodies = [(p[0], {k: v for k, v in p[1].items() if k != "revision"}) for p in out["posts"]]
        self.assertEqual(bodies, [(f"/api/items/{plan}", {"area": "home"}), (f"/api/items/{plan}", {"area": None}),
                                  (f"/api/items/{plan}", {"area": "hoa"}), (f"/api/items/{plan}", {"area": None})])
        self.assertEqual([a[0] for a in out["asked"]], [f"Area → Home: #{plan}?", f"Area → Home: #{plan}?",
                                                        f"Area → HOA: #{plan}?", f"Area → none: #{plan}?"])
        self.assertEqual(out["asked"][0][2], f"sd task edit {plan} --area home")
        self.assertEqual(out["asked"][3][2], f"sd task edit {plan} --clear-area")
        msgs = [t[0] for t in out["toasts"]]
        self.assertIn(f"#{plan} area → Home · sd task edit {plan} --area home", msgs)
        self.assertIn(f"#{plan} area → none · sd task edit {plan} --clear-area", msgs)

    def test_the_area_field_lists_personal_and_each_realm(self):
        """pack sd:8062: Area offers None, Home, HOA, Personal and each dashboard.realms realm (SHELL_REALMS), and
        a realm pick writes `sd task edit --area <id>`."""
        plan = self.ids["plan"]
        self.row("plan").update(area=None, area_set=False)
        out = self.run_page(self.CHANGE + self.ASKED + f"""var YES = [true];
open({plan}); await flush(); R.html = ELS.details.html;
change('det-area', 'lab'); await flush();""", self.AREA_ECHO,
                            prelude="globalThis.SHELL_REALMS = [{ id: 'work', label: 'Work' }, { id: 'lab', label: 'Lab' }];\n")
        self.assertEqual(self.options(out["R"]["html"], "det-area"),
                         [("", " selected", "None"), ("home", "", "Home"), ("hoa", "", "HOA"), ("personal", "", "Personal"),
                          ("work", "", "Work"), ("lab", "", "Lab")])
        self.assertEqual([p[1]["area"] for p in out["posts"]], ["lab"])
        self.assertEqual(out["asked"][0][:3:2], [f"Area → Lab: #{plan}?", f"sd task edit {plan} --area lab"])

    def test_the_kind_changes_the_page_sends_are_ones_edit_item_takes(self):
        """sd:6231: the posts above, through `workflow.edit_item`: a followup becomes a task, and a task with a repo becomes
        a work-idea only with the repo cleared in the same edit, as the confirm says; its Undo, the kind alone, is taken too."""
        plan, ask = self.ids["plan"], self.ids["ask"]
        self.assertEqual(workflow.edit_item(self.connection, ask, {"kind": "task"}, who="test")["item"]["kind"], "task")
        with self.assertRaises(workflow.WorkflowError):
            workflow.edit_item(self.connection, plan, {"kind": "work-idea"}, who="test")
        state = workflow.edit_item(self.connection, plan, {"kind": "work-idea", "repo": None}, who="test")
        self.assertEqual((state["item"]["kind"], state["item"]["repo"]), ("work-idea", None))
        self.assertEqual(workflow.edit_item(self.connection, plan, {"kind": "task"}, who="test")["item"]["kind"], "task")

    def test_the_published_section_links_each_url_under_its_documents_title(self):
        # sd:3465: the Details link where the item's work was published; an address that is not a link stays text, and an
        # item with nothing published shows no section.
        plan, ask = self.ids["plan"], self.ids["ask"]
        self.details[str(plan)]["published"] = [
            {"title": "The brief", "where": "system · brief.md", "links": [
                {"channel": "notion", "url": "https://example.test/p", "href": "https://example.test/p"},
                {"channel": "site", "url": "javascript:alert(1)", "href": None}]}]
        out = self.run_page(f"open({plan}); await flush(); R.plan = ELS.details.html; open({ask}); await flush(); R.ask = ELS.details.html;")
        html = out["R"]["plan"]
        self.assertIn("<h3>Published ", html)
        self.assertIn('<li><span><b>The brief</b> <small>system · brief.md</small>', html)
        self.assertIn('notion <a class="ext" href="https://example.test/p" target="_blank" rel="noopener noreferrer">https://example.test/p</a>', html)
        self.assertIn("site <code>javascript:alert(1)</code>", html)
        self.assertNotIn('href="javascript', html)
        self.assertNotIn("<h3>Published ", out["R"]["ask"])

    def test_the_experiments_field_posts_the_values_sd_task_edit_takes(self):
        # sd:4639: the designation control writes what `sd task edit N --experiment` takes, through the same
        # `projection.designate`; clearing a pinned row is `new`. A designation the page cannot read turns it off.
        plan, ask, port = self.ids["plan"], self.ids["ask"], self.ids["port"]
        self.details[str(plan)]["experiment"] = {"value": None, "row": None, "reason": None,
                                                 "choices": [{"value": "of:7", "label": "task of #7 An experiment"}]}
        self.details[str(ask)]["experiment"] = {"value": "yes", "row": "3c9f52b1-5782-81a7-a466-fb0e2df4d928",
                                                "choices": [], "reason": None}
        self.details[str(port)]["experiment"] = {"value": None, "row": None, "choices": [],
                                                 "reason": "this sd_db has no projection; update the command pack"}
        out = self.run_page(self.CHANGE + f"""open({plan}); await flush(); R.plan = ELS.details.html;
change('det-exp', 'of:7'); await flush(); change('det-exp', ''); await flush();
open({ask}); await flush(); R.ask = ELS.details.html;
change('det-exp-row', ' https://example.test/3cff52b15782806aacb0ca0c8f41524b '); await flush();
change('det-exp-row', ''); await flush(); change('det-exp', 'off'); await flush();
open({port}); await flush(); R.port = ELS.details.html;""", self.ECHO)
        html = out["R"]["plan"]
        self.assertIn('<h3><label for="det-exp">Experiments</label> ', html)
        self.assertIn('<select id="det-exp" class="fld"><option value="" selected>not projected</option>'
                      '<option value="yes">experiment</option><option value="of:7">task of #7 An experiment</option></select>', html)
        self.assertNotIn('id="det-exp-row"', html)
        self.assertIn('<option value="yes" selected>experiment</option><option value="off">off, keep the row</option>', out["R"]["ask"])
        self.assertIn('<input id="det-exp-row" class="fld" value="3c9f52b1-5782-81a7-a466-fb0e2df4d928" placeholder="a page id or URL">',
                      out["R"]["ask"])
        self.assertIn('<select id="det-exp" class="fld" disabled aria-describedby="det-exp-off">', out["R"]["port"])
        self.assertIn('<p class="why" id="det-exp-off">this sd_db has no projection; update the command pack.</p>', out["R"]["port"])
        bodies = [(p[0], {k: v for k, v in p[1].items() if k != "revision"}) for p in out["posts"]]
        self.assertEqual(bodies, [
            (f"/api/items/{plan}/experiment", {"experiment": ["of:7"]}),
            (f"/api/items/{ask}/experiment", {"experiment": ["row:https://example.test/3cff52b15782806aacb0ca0c8f41524b"]}),
            (f"/api/items/{ask}/experiment", {"experiment": ["new"]}),
            (f"/api/items/{ask}/experiment", {"experiment": ["off"]})])
        msgs = [t[0] for t in out["toasts"]]
        self.assertIn(f"#{plan} experiments → of:7 · sd task edit {plan} --experiment 'of:7'", msgs)
        self.assertIn(f"#{ask} experiments → new · sd task edit {ask} --experiment 'new'", msgs)

    def test_the_title_and_description_edit_in_place_with_undo(self):
        # sd:3467: the v2 Details lost the classic item page's Details textarea; the title and body are fields again.
        plan = self.ids["plan"]
        self.details[str(plan)]["item"]["body"] = json.dumps({"text": "Old words"})
        out = self.run_page(self.CHANGE + f"""open({plan}); await flush(); R.html = ELS.details.html;
change('det-title', '   '); await flush();
change('det-title', ' Plan the big review '); await flush(); lastUndo().undo(); await flush();
change('det-body', 'New words'); await flush(); lastUndo().undo(); await flush();""", self.ECHO)
        self.assertIn('<input id="det-title" class="fld" value="Plan the review" required>', out["R"]["html"])
        self.assertIn('<textarea id="det-body" class="fld" rows="4">Old words</textarea>', out["R"]["html"])
        bodies = [{k: v for k, v in p[1].items() if k != "revision"} for p in out["posts"]]
        self.assertEqual(bodies, [{"title": "Plan the big review"}, {"title": "Plan the review"}, {"body": "New words"}, {"body": "Old words"}])
        msgs = [t[0] for t in out["toasts"]]
        self.assertEqual(msgs[0], f"#{plan} not changed: a title is required")
        self.assertIn(f"#{plan} title → Plan the big review · sd task edit {plan} --title 'Plan the big review'", msgs)
        self.assertIn(f"#{plan} description changed · sd task edit {plan} --body 'New words'", msgs)

    def test_the_description_edits_the_stored_bodys_text(self):
        # Review 2026-10-12, lane entry 225 class pass: the item body is stored as JSON, {"text": ...} (workflow._fields), and
        # the field showed that JSON. It edits the text, as the classic item page does (controls.py); a body that does not
        # parse is withheld, so no save here overwrites it; a kind edit_item takes no body for has no field.
        plan, port = self.ids["plan"], self.ids["port"]
        self.details[str(plan)]["item"]["body"] = json.dumps({"text": "Old words"})
        out = self.run_page(f"open({plan}); await flush(); R.text = ELS.details.html;")
        self.assertIn('<textarea id="det-body" class="fld" rows="4">Old words</textarea>', out["R"]["text"], "the field shows the stored JSON")
        self.details[str(plan)]["item"]["body"] = "{not json"
        out = self.run_page(f"open({plan}); await flush(); R.bad = ELS.details.html;")
        self.assertNotIn('id="det-body"', out["R"]["bad"])
        self.assertIn(f"The saved description is not valid JSON, so it is hidden and nothing here can overwrite it. "
                      f"Fix it from the terminal: <code>sd task edit {plan} --body</code>.", out["R"]["bad"])
        self.details[str(port)]["item"]["body"] = json.dumps({"text": "Report words"})
        out = self.run_page(f"open({port}); await flush(); R.port = ELS.details.html;")
        self.assertNotIn('id="det-body"', out["R"]["port"])

    # A field the operator is in when Details redraws: its live node, the text and the caret, as the redraw found them.
    EDITING = """const editing = (id, value, extra) => { const n = Object.assign(El(id), { value, selectionStart: 4, selectionEnd: 6 }, extra || {});
  n.closest = sel => sel === '#details' ? ELS.details : null; n.focus = () => { R.focused = id; };
  n.setSelectionRange = (a, b) => { R.range = [a, b]; }; document.activeElement = n; n.isConnected = true;
  // A redraw detaches the old field, as the browser does, unless the page moves it back in.
  const rc = ELS.details.replaceChildren; ELS.details.replaceChildren = f => { n.isConnected = false; rc(f); };
  // The redraw's fresh field: its value is the saved one; replaceWith records what took its place.
  ELS[id] = Object.assign(El(id), { value: SAVED[id] || '', replaceWith: m => { R.kept = m === n ? id : 'other'; m.isConnected = true; },
    focus: () => { R.focused = 'fresh ' + id; } });
  return n; };
"""

    def test_a_redraw_keeps_the_field_the_operator_is_editing(self):
        # Review 2026-10-12 (high): a Details read or a refresh that lands mid-edit redrew the title from saved data and
        # dropped the typed text. The redraw now moves the live field into the new Details, with its caret.
        plan, port = self.ids["plan"], self.ids["port"]
        cases = {
            "title": f"var SAVED = {{ 'det-title': 'Plan the review' }}; open({plan}); await flush(); editing('det-title', 'Plan the half'); open({plan}); await flush();",
            "body": f"var SAVED = {{ 'det-body': '' }}; open({plan}); await flush(); editing('det-body', 'Typed words'); open({plan}); await flush();",
            "date": f"var SAVED = {{ 'det-due': '' }}; open({plan}); await flush(); editing('det-due', '', {{ type: 'date', validity: {{ badInput: true }} }}); open({plan}); await flush();",
            "saved": f"var SAVED = {{ 'det-title': 'Plan the review' }}; open({plan}); await flush(); editing('det-title', 'Plan the review'); open({plan}); await flush();",
            "other": f"var SAVED = {{ 'det-title': 'Plan the review' }}; open({plan}); await flush(); editing('det-title', 'Plan the half'); open({port}); await flush();",
        }
        got = {k: self.run_page(self.EDITING + body)["R"] for k, body in cases.items()}
        self.assertEqual(got["title"].get("kept"), "det-title", "a redraw dropped the typed title")
        self.assertEqual((got["title"].get("focused"), got["title"].get("range")), ("det-title", [4, 6]))
        self.assertEqual(got["body"].get("kept"), "det-body", "a redraw dropped the typed description")
        self.assertEqual(got["date"].get("kept"), "det-due", "a redraw dropped a half-typed date")
        self.assertIsNone(got["saved"].get("kept"), "a field that holds the saved value takes the fresh one")
        self.assertEqual(got["saved"].get("focused"), "fresh det-title")
        self.assertIsNone(got["other"].get("kept"), "another task's Details took the old task's text")

    # ECHO, with the item's body as the server keeps it: a write's readback carries every item field.
    BODY_ECHO = ECHO.replace("repo: 'repo' in body ? body.repo : row.repo_path };",
                             "repo: 'repo' in body ? body.repo : row.repo_path, body: JSON.stringify({ text: 'body' in body ? (window.BODY = body.body) : (window.BODY ?? 'Old words') }) };")

    def test_a_description_draft_survives_a_write_that_drops_the_details_reading(self):
        # Review 2026-10-12, lane entry 225: a priority write's absorb() dropped DET.items, so the redraw drew no description
        # field and the operator's unsaved description went with it. The title survived: it is drawn from the row.
        plan = self.ids["plan"]
        self.details[str(plan)]["item"]["body"] = json.dumps({"text": "Old words"})
        # The browser's getElementById answers null for a field the redraw did not draw.
        absent = """const gid = document.getElementById.bind(document);
document.getElementById = x => x === 'det-body' && !String(ELS.details.html).includes('id="det-body"') ? null : gid(x);
"""
        cases = {
            "pending": "DETAIL = () => [500, { error: 'not read yet' }];",
            "read": "",
        }
        got = {}
        for name, detail in cases.items():
            got[name] = self.run_page(self.EDITING + self.CHANGE + absent + f"""var SAVED = {{ 'det-body': 'Old words' }};
open({plan}); await flush(); editing('det-body', 'Typed words'); {detail}
change('det-p', '1'); await flush(); R.html = ELS.details.html;""", self.BODY_ECHO)["R"]
        self.assertEqual(got["pending"].get("kept"), "det-body", "a write that dropped the reading dropped the typed description")
        self.assertIn('<textarea id="det-body" class="fld" rows="4">Old words</textarea>', got["pending"]["html"])
        self.assertEqual(got["read"].get("kept"), "det-body")

    def test_each_details_fields_undo_restores_the_value_its_write_replaced(self):
        # Review 2026-10-12, lane entry 225: the description's Undo read its value when the change was queued. With A saved,
        # B and then C queued, C's Undo wrote A back. Every Details field's Undo reads the value as its write is sent.
        plan = self.ids["plan"]
        self.details[str(plan)]["item"]["body"] = json.dumps({"text": "Old words"})
        cases = {
            "title": ("det-title", "Plan B", "Plan C", "title", "Plan B"),
            "description": ("det-body", "Words B", "Words C", "body", "Words B"),
            "priority": ("det-p", "1", "4", "priority", 1),
            "due": ("det-due", "2026-09-20", "2026-09-30", "due", "2026-09-20"),
            "repo": ("det-repo", "", "~/repos/example", "repo", None),
            "status": ("det-status", "in_progress", "blocked", "status", "in_progress"),
        }
        for name, (field, b, c, key, want) in cases.items():
            with self.subTest(field=name):
                out = self.run_page(self.CHANGE + f"""open({plan}); await flush();
change('{field}', {json.dumps(b)}); change('{field}', {json.dumps(c)}); await flush();
R.undo = !!lastUndo(); if (R.undo) {{ await lastUndo().undo(); await flush(); }}""", self.BODY_ECHO)
                self.assertIsNone(out.get("error"))
                bodies = [p[1] for p in out["posts"]]
                self.assertEqual([x.get(key) for x in bodies[:2]], [b if key != "priority" else int(b), c if key != "priority" else int(c)] if key != "repo" else [None, c])
                self.assertTrue(out["R"]["undo"], f"{name}: no Undo offered")
                self.assertEqual(bodies[-1].get(key, "missing"), want, f"{name}: Undo did not restore the value C replaced")

    # Another tool edits the plan item: its row and its Details read bring the new revision. A write on another task lands,
    # and the page rereads the rows, as after any landed write.
    ELSEWHERE = """const NEW = 'c'.repeat(64), prow = DOC.rows.find(r => r.id === PLAN);
const elsewhere = () => { prow.revision = NEW; DETAILS[PLAN] = Object.assign(JSON.parse(JSON.stringify(DETAILS[PLAN])), { revision: NEW });
  DETAILS[PLAN].item.body = JSON.stringify({ text: 'CLI words' }); DETAILS[PLAN].notes.push({ id: 999, kind: 'comment', at: '2026-09-02T00:00:00Z', body: 'Noted elsewhere', resolved: null }); };
const reread = () => ELS.main.listeners.change.forEach(f => f({ target: { dataset: { edit: 'p', key: String(ASK) }, value: '1' } }));
"""

    def test_a_description_edit_refuses_a_text_another_tool_replaced(self):
        # Review 2026-10-12, sd:3520 lane: after a CLI edit, adopt() took the row's new revision and kept the cached
        # description, so an edit wrote over the CLI's text with no conflict, and its Undo wrote the old text back. The cached
        # description carries the revision it was read at; a field drawn from another text, or a write that leaves while the
        # description known is older than the row, is refused, and the Details are read again.
        plan, ask = self.ids["plan"], self.ids["ask"]
        self.details[str(plan)]["item"]["body"] = json.dumps({"text": "Old words"})
        cases = {
            # The field drawn from the old text is left after the reread brought the new one.
            "field": "change('det-body', 'My words', 'Old words');",
            # The Details read again fails, so the description known is the old one when the write leaves.
            "send": "DETAIL = () => [500, { error: 'not read' }]; change('det-body', 'My words');",
        }
        for name, edit in cases.items():
            with self.subTest(case=name):
                before = "DETAIL = () => [500, { error: 'not read' }];" if name == "send" else ""
                out = self.run_page(self.ELSEWHERE.replace("PLAN", str(plan)).replace("ASK", str(ask))
                                    + "const change = (id, value, defaultValue) => ELS.details.listeners.change.forEach(f => f({ target: { id, value, defaultValue } }));\n"
                                    + f"""open({plan}); await flush(); elsewhere(); {before} reread(); await flush(); await flush();
{edit} await flush(); await flush();""", self.BODY_ECHO)
                self.assertIsNone(out.get("error"))
                bodies = [p[1] for p in out["posts"] if "body" in p[1]]
                self.assertEqual(bodies, [], f"{name}: the edit wrote over the text another tool saved")
                self.assertIn(f"#{plan} not changed: the description changed since it was read", "\n".join(t[0] for t in out["toasts"]))

    def test_a_reread_that_brings_a_new_revision_reads_the_details_again(self):
        # The same review, class pass: the Details' notes, history and documents are cached beside the revision they were
        # read at too. A reread whose row carries another revision drops them and reads them again.
        plan, ask = self.ids["plan"], self.ids["ask"]
        out = self.run_page(self.ELSEWHERE.replace("PLAN", str(plan)).replace("ASK", str(ask))
                            + f"""open({plan}); await flush(); elsewhere(); reread(); await flush(); await flush(); R.html = ELS.details.html;""", self.ECHO)
        self.assertIsNone(out.get("error"))
        self.assertIn("Noted elsewhere", out["R"]["html"], "the Details kept the notes read at the old revision")
        self.assertIn("cccccccccccc…", out["R"]["html"])

    def test_a_cards_priority_and_due_are_dropdowns_that_edit_it(self):
        # sd:3440: the tile sets priority and due through a dropdown for each; Pick a date opens the Edit dialog.
        plan, port = self.ids["plan"], self.ids["port"]
        self.row("port")["edit"] = {"allowed": False, "reason": "message items use their own editing workflow"}
        tile = "const tile = (edit, key, value) => ELS.main.listeners.change.forEach(f => f({ target: { dataset: { edit, key: String(key) }, value } }));\n"
        out = self.run_page(tile + f"""R.board = ELS['view-board'].html;
tile('p', {plan}, '1'); await flush();
tile('due', {plan}, ''); await flush();""", self.ECHO)
        # Pick a date opens the Edit dialog, which the stand-in page cannot draw.
        self.assertIn("if (f === 'due' && e.target.value === 'pick') { render(); askEdit(t); return; }", TASKS_JS)
        board = out["R"]["board"]
        self.assertIn(f'<select class="tile-f pri" data-edit="p" data-key="{plan}" data-p="2" aria-label="Priority of #{plan}">', board)
        self.assertRegex(board, rf'<select class="tile-f due[^"]*" data-edit="due" data-key="{plan}" aria-label="Due date of #{plan}"><option value="2026-09-09" selected>')
        self.assertIn('<option value="pick">Pick a date…</option>', board)
        # A row edit_item refuses keeps the plain labels.
        self.assertNotIn(f'data-key="{port}" aria-label="Priority', board)
        bodies = [{k: v for k, v in p[1].items() if k != "revision"} for p in out["posts"]]
        self.assertEqual(bodies, [{"priority": 1}, {"due": None}])

    def test_each_sections_read_is_a_help_icon_that_copies_it_and_act_has_no_explanations(self):
        # sd:3444: the read under each Details heading moved into a popover beside the heading; pressing the icon copies it.
        # Pack sd:7536: the help says what the section shows first; the command it copies comes after.
        plan = self.ids["plan"]
        out = self.run_page(f"open({plan}); await flush(); R.html = ELS.details.html;")
        html = out["R"]["html"]
        self.assertNotIn('<p class="src">', html)
        self.assertIn(f'<h3>Status history <button class="help src" type="button" aria-label="Help: Status history" '
                      f'data-help="Every status change, from the task&#39;s history. Press to copy the command: '
                      f'&lt;code&gt;sd task show {plan} --json&lt;/code&gt;" data-copy="sd task show {plan} --json">', html)
        # A section with no command to copy is plain help, with no copy.
        self.assertIn('<h3>External context <button class="help" type="button" aria-label="Help: External context" '
                      'data-help="What the tracker says about this task, and how fresh the last sync is.">', html)
        self.assertNotIn("/api/tasks/", html)
        self.assertNotIn("Delete asks first", html)

    def test_a_status_move_toasts_after_the_write_and_undo_posts_the_old_status_with_the_new_revision(self):
        ask = self.ids["ask"]
        row = next(r for r in self.doc["rows"] if r["id"] == ask)
        answer = f"""(path, body) => [200, {{ item: {{ id: {ask}, status: body.status, priority: 3, due: null, recurrence: null }},
  notes: [], revision: body.status === 'ready' ? 'b'.repeat(64) : 'c'.repeat(64) }}]"""
        out = self.run_page(f"""shellRun(cmd('item.status.ready'), C.get('{ask}'));
R.before = OUT.toasts.length; await flush(); R.after = OUT.toasts.length;
lastToast().undo(); await flush();""", answer)
        self.assertEqual(out["R"]["before"], 0, "the toast came before the write landed")
        self.assertEqual(out["R"]["after"], 1)
        self.assertEqual(out["posts"], [
            [f"/api/items/{ask}/status", {"status": "ready", "revision": row["revision"]}, 64],
            [f"/api/items/{ask}/status", {"status": "planning", "revision": "b" * 64}, 64],
        ])
        self.assertEqual(out["toasts"][0], [f"#{ask} Planning → Ready · sd task status {ask} ready", True])
        self.assertEqual(out["toasts"][1], [f"Status → Ready undone · #{ask} Answer the question", False])

    def test_a_refused_move_posts_nothing(self):
        port = self.ids["port"]
        out = self.run_page(f"shellRun(cmd('item.status.ready'), C.get('{port}')); await flush();")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"], [["off: An agent is working on this task. Status is locked until it ends.", False]])

    def test_a_stale_revision_toasts_and_reads_the_rows_again(self):
        ask = self.ids["ask"]
        out = self.run_page(f"shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush();",
                            "() => [409, { error: 'The item changed. Reload it.', reload: true }]")
        self.assertEqual(out["gets"], ["/api/tasks", "/api/tasks"])
        self.assertEqual(out["toasts"], [[f"#{ask} Answer the question not changed: The item changed. Reload it.", False]])

    def test_a_conflict_reads_the_open_details_again(self):
        # A 409 means the item changed elsewhere: the rows are read again, and so are the open Details, or the pane keeps
        # showing what the conflict replaced (review, PR #46).
        ask = self.ids["ask"]
        after = json.loads(json.dumps(self.details[str(ask)]))
        after["notes"] = [{"id": 77, "kind": "comment", "at": "2026-09-06T12:00:00Z", "body": "Written elsewhere", "session": "other", "resolved": None}]
        out = self.run_page(f"""var reads = 0; DETAIL = id => ++reads === 1 ? [200, DETAILS[id]] : [200, AFTER];
open({ask}); await flush(); shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush(); R.html = ELS.details.html;""",
                            "() => [409, { error: 'The item changed. Reload it.' }]", prelude=f"var AFTER = {json.dumps(after)};\n")
        self.assertEqual(out["gets"], ["/api/tasks", f"/api/tasks/{ask}", "/api/tasks", f"/api/tasks/{ask}"])
        self.assertIn("Written elsewhere", out["R"]["html"])

    # sd:2484: the rows are read through shell.read (read.js), as Activity and Documents read theirs.
    def test_a_landed_write_reads_the_rows_again_and_a_row_no_longer_listed_runs_nothing(self):
        ask, port = self.ids["ask"], self.ids["port"]
        answer = f"""(path, body) => {{ DOC.rows = DOC.rows.filter(r => r.id !== {port});
  return [200, {{ item: {{ id: {ask}, status: body.status, priority: 3, due: null, recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]; }}"""
        out = self.run_page(f"""shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush();
R.types = [C.get('{ask}').type, C.get('{port}').type]; R.board = ELS['view-board'].html;""", answer)
        self.assertEqual(out["gets"], ["/api/tasks", "/api/tasks"], "the landed write did not read the rows again")
        self.assertEqual(out["R"]["types"], ["item", "not listed"])
        self.assertNotIn("Port the page", out["R"]["board"])

    def test_a_failed_reread_after_a_write_keeps_the_rows_and_says_the_write_landed(self):
        ask = self.ids["ask"]
        answer = f"(path, body) => [200, {{ item: {{ id: {ask}, status: body.status, priority: 3, due: null, recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"
        out = self.run_page(f"""const A = ANSWER; ANSWER = (path, body) => path === '/api/tasks' ? [500, {{ error: 'database is locked' }}] : A(path, body);
shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush();
R.board = ELS['view-board'].html; R.details = ELS.details.html; R.type = C.get('{ask}').type;""", answer)
        self.assertIn("Answer the question", out["R"]["board"], "the failed reread dropped the rows")
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertTrue(out["states"][-1]["text"].startswith("The change landed; the tasks were not read again: database is locked."), out["states"][-1])
        self.assertIn("The tasks were not read again after the change, so nothing is selected.", out["R"]["details"])

    def test_a_read_sent_before_a_newer_one_never_draws_over_it(self):
        # A refused write loads the rows; a task is added while that read is out. Each read's answer is the rows as they
        # were when it was sent, and the newest is answered first: the older answer must not draw the added task away.
        ask = self.ids["ask"]
        new = {**self.row("ask"), "id": 99, "title": "Call the bank", "kind": "task", "status": "planning"}
        answer = f"""(path, body) => path === '/api/items' ? (DOC.rows.push({json.dumps(new)}), [201, {{ item: {{ id: 99, title: 'Call the bank' }} }}])
  : [409, {{ error: 'The item changed. Reload it.' }}]"""
        out = self.run_page(f"""const A = ANSWER, HELD = []; DETAIL = id => DETAILS[id] ? [200, DETAILS[id]] : [404, {{ error: 'no such item' }}];
ANSWER = (path, body) => {{ if (path !== '/api/tasks') return A(path, body); const snap = A(path, body); return new Promise(r => HELD.push(() => r(snap))); }};
shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush();
var box = document.getElementById('shift-in'); box.value = 'Call the bank';
box.listeners.keydown.forEach(f => f({{ key: 'Enter', preventDefault() {{}}, stopPropagation() {{}} }})); await flush();
for (let i = 0; i < 4; i++) {{ HELD.splice(0).reverse().forEach(f => f()); await flush(); }}
R.board = ELS['view-board'].html; R.listed = C.get('99') ? C.get('99').type : null;""", answer)
        self.assertIn("Call the bank", out["R"]["board"], "an older read drew over the newer one")
        self.assertEqual(out["R"]["listed"], "item")

    def test_a_failed_load_draws_no_row_and_lights_unknown(self):
        out = self.run_page("R.board = ELS['view-board'].html; R.details = ELS.details.html;", prelude="var FAIL_TASKS = 'database is locked';\n")
        self.assertEqual(out["states"][-1], {"kind": "error", "text": "The tasks were not read: database is locked. Reload retries it.", "source": "/api/tasks"})
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "tasks not read"})
        self.assertNotIn('data-key="', out["R"]["board"])
        self.assertIn("The tasks were not read, so nothing is selected.", out["R"]["details"])

    def test_the_read_guards_are_the_readers_not_a_copy(self):
        self.assertIn("shell.read({", TASKS_JS)
        for own in (r"\brereading\b", r"\bwrote\b", r"getJSON\('/api/tasks'\)", r"\bstarted = "):
            self.assertIsNone(re.search(own, TASKS_JS), own)

    def test_a_repeating_task_offers_no_remove_due_date(self):
        # workflow._recurring refuses a repeating item with no due date, so neither the Edit dialog nor the move out of
        # Urgent offers Remove due date on one; each says why instead (review, PR #46).
        self.repeat_plan()
        plan = self.ids["plan"]
        why = "Remove due date is off: a repeating task needs a due date. Stop repeating first to remove it."
        out = self.run_page(f"""const D = MADE.find(e => /date-dlg/.test(e.className || ''));
D.querySelector = sel => {{ const e = El(sel); e.querySelector = () => El('in'); return e; }};
open({plan}); await flush(); shellRun(cmd('item.edit'), C.get('{plan}')); await flush(); R.edit = D.html;
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'matrix' }})); await flush();
document.dispatchEvent({{ type: 'keydown', key: '2', target: El('card'), preventDefault() {{}} }}); await flush(); R.out = D.html;""")
        self.assertIn(f"Edit #{plan}", out["R"]["edit"])
        self.assertIn(f"#{plan} → Schedule", out["R"]["out"])
        for dialog in ("edit", "out"):
            self.assertNotIn("Remove due date</button>", out["R"][dialog], dialog)
            self.assertIn(why, out["R"][dialog], dialog)

    def test_resolve_confirms_first_posts_the_item_revision_and_offers_no_undo(self):
        plan = self.ids["plan"]
        note = self.details[str(plan)]["notes"][0]["id"]
        row = next(r for r in self.doc["rows"] if r["id"] == plan)
        answer = f"() => [200, {{ item: {{ id: {plan}, status: 'ready', priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"
        out = self.run_page(f"""open({plan}); await flush(); R.when = cmd('note.resolve').when(C.get('note:{note}'));
R.comment = cmd('note.resolve').when(C.get('note:{self.details[str(plan)]["notes"][1]["id"]}'));
shellRun(cmd('note.resolve'), C.get('note:{note}')); await flush();""", answer)
        self.assertEqual(out["R"]["when"], True)
        self.assertEqual(out["R"]["comment"], "a comment note has nothing to resolve")
        self.assertEqual(out["confirms"], ["note.resolve"])
        self.assertEqual(out["posts"], [[f"/api/notes/{note}/resolve", {"revision": row["revision"]}, 64]])
        self.assertEqual(out["toasts"], [[f"Resolved note {note} · sd note resolve {note}", False]])

    def owned_work(self):
        """A work item in a repository the database owns, with no assignment: sd work relink and cancel take it."""
        owned = self.repo("/repos/owned")
        upsert_repo(self.connection, owned)
        mine = self.item("Owned work", kind="work", repo=owned, path="docs/work/mine/prd.md")
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details[str(mine)] = tasks_screen.details(self.connection, mine, now=NOW)
        return mine

    def test_relink_and_cancel_post_to_the_work_routes_with_the_typed_text(self):
        # sd:2200: both execute through /api/items/<id>/(relink|cancel) with the row's revision, after the confirm takes
        # the text only the operator has. Neither offers Undo.
        mine = self.owned_work()
        row = next(r for r in self.doc["rows"] if r["id"] == mine)
        answer = f"""(path, body) => [200, {{ item: {{ id: {mine}, status: path.endsWith('/cancel') ? 'done' : 'planning', priority: null, due: null, recurrence: null }},
  notes: [], revision: path.endsWith('/cancel') ? 'c'.repeat(64) : 'b'.repeat(64) }}]"""
        out = self.run_page(f"""open({mine}); await flush();
R.fields = [cmd('work.relink').fields(C.get('{mine}')), cmd('work.cancel').fields(C.get('{mine}'))].map(fs => fs.map(f => [f.name, f.required, f.placeholder]));
R.when = [cmd('work.relink').when(C.get('{mine}')), cmd('work.cancel').when(C.get('{mine}'))];
R.cli = [cmd('work.relink').cli(C.get('{mine}')), cmd('work.cancel').cli(C.get('{mine}'), {{ reason: "it's superseded" }})];
FIELD_VALUES = {{ path: 'docs/work/moved/prd.md' }}; shellRun(cmd('work.relink'), C.get('{mine}')); await flush();
FIELD_VALUES = {{ reason: "it's superseded" }}; shellRun(cmd('work.cancel'), C.get('{mine}')); await flush();""", answer)
        self.assertEqual(out["R"]["fields"], [[["path", True, "docs/work/mine/prd.md"]], [["reason", True, "why the work stops"]]])
        self.assertEqual(out["R"]["when"], [True, True])
        self.assertEqual(out["R"]["cli"], [f"sd work relink {mine} <moved path>", f"sd work cancel {mine} --reason 'it'\\''s superseded'"])
        self.assertEqual(out["confirms"], ["work.relink", "work.cancel"])
        self.assertEqual(out["posts"], [[f"/api/items/{mine}/relink", {"path": "docs/work/moved/prd.md", "revision": row["revision"]}, 64],
                                        [f"/api/items/{mine}/cancel", {"reason": "it's superseded", "revision": "b" * 64}, 64]])
        self.assertEqual(out["toasts"], [[f"#{mine} relinked → docs/work/moved/prd.md · sd work relink {mine} 'docs/work/moved/prd.md'", False],
                                         [f"#{mine} cancelled · sd work cancel {mine} --reason 'it'\\''s superseded'", False]])

    def test_cancel_with_no_reason_posts_nothing(self):
        mine = self.owned_work()
        out = self.run_page(f"""open({mine}); await flush(); FIELD_VALUES = {{ reason: '   ' }}; shellRun(cmd('work.cancel'), C.get('{mine}')); await flush();""")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"], [["not run: OK is off until Reason is typed", False]])

    def test_relink_and_cancel_are_off_where_sd_work_refuses_them(self):
        # The seeded work row's repository lets its files own status, and it has a running assignment: progress.work_controls
        # gives the reason the mutation would. A task row is not work at all.
        port, plan = self.ids["port"], self.ids["plan"]
        why = self.details[str(port)]["work"]["reason"]
        out = self.run_page(f"""open({port}); await flush(); open({plan}); await flush();
R.port = [cmd('work.relink').when(C.get('{port}')), cmd('work.cancel').when(C.get('{port}'))];
R.plan = [cmd('work.relink').when(C.get('{plan}')), cmd('work.cancel').when(C.get('{plan}'))];""")
        self.assertTrue(why)
        self.assertEqual(out["R"]["port"], [why, why])
        self.assertEqual(out["R"]["plan"], ["Relink works on a work item; this is a task", "Cancel works on a work item; this is a task"])

    def test_a_due_weekday_is_a_calendar_date_across_the_dst_change(self):
        # Friday 2026-10-30 in Denver; the clocks go back on Sunday 2026-11-01, so "due mon" is 2026-11-02.
        prelude = """var RealDate = Date; Date = class extends RealDate { constructor(...a) { if (a.length) super(...a); else super(2026, 9, 30, 9, 0, 0); } };\n"""
        out = self.run_page("""var box = document.getElementById('shift-in'); box.value = 'Call the bank due mon';
box.listeners.keydown.forEach(f => f({ key: 'Enter', preventDefault() {}, stopPropagation() {} })); await flush();""",
                            "() => [201, { item: { id: 99, title: 'Call the bank' } }]", prelude=prelude, env={"TZ": "America/Denver"})
        self.assertEqual(out["posts"][0][:2], ["/api/items", {"title": "Call the bank", "priority": 3, "due": "2026-11-02"}])

    def test_a_capture_lands_in_the_filtered_repo_at_p3_unless_typed(self):
        # sd:3521: the quick add and the shell's capture take the one repo the filter shows, else the selected task's.
        row = self.row("plan")
        enter = """var box = document.getElementById('shift-in'); box.value = %s;
box.listeners.keydown.forEach(f => f({ key: 'Enter', preventDefault() {}, stopPropagation() {} })); await flush();"""
        out = self.run_page(enter % "'Ship it'" + "\n" + enter % "'Ship that p1'" + """
R.filtered = window.PAGE_REPO(null);""", "(path, body) => [201, { item: { id: 99, title: body.title } }]", search=f"?repo={row['repo']}")
        self.assertEqual([p[1] for p in out["posts"][:2]], [{"title": "Ship it", "priority": 3, "repo": row["repo_path"]},
                                                           {"title": "Ship that", "priority": 1, "repo": row["repo_path"]}])
        self.assertEqual(out["R"]["filtered"], row["repo_path"])
        out = self.run_page(f"R.none = window.PAGE_REPO(null); R.task = window.PAGE_REPO(C.get('{row['id']}'));")
        self.assertEqual((out["R"]["none"], out["R"]["task"]), (None, row["repo_path"]))

    def test_a_refused_quick_add_gives_back_the_text_unless_the_operator_typed_again(self):
        # Review 2026-10-12 class pass: a refusal rebuilt the box from the parse and overwrote what was typed since.
        press = "box.listeners.keydown.forEach(f => f({ key: 'Enter', preventDefault() {}, stopPropagation() {} }));"
        refuse = "() => [400, { error: 'A title is required.' }]"
        out = self.run_page(f"var box = document.getElementById('shift-in'); box.value = 'Call  the bank P2';\n{press} await flush(); R.back = box.value;", refuse)
        self.assertEqual(out["R"]["back"], "Call  the bank P2", "the refused text did not come back as typed")
        out = self.run_page(f"var box = document.getElementById('shift-in'); box.value = 'Call the bank';\n{press} box.value = 'Next one'; await flush(); R.back = box.value;", refuse)
        self.assertEqual(out["R"]["back"], "Next one", "the refusal overwrote text typed after Enter")
        self.assertIn("Not added: A title is required.", [t[0] for t in out["toasts"]])

    def test_details_read_before_a_write_never_replace_the_read_after_it(self):
        plan = self.ids["plan"]
        after = json.loads(json.dumps(self.details[str(plan)]))
        after["notes"][0]["body"] = "Written after the move"
        after["revision"] = "b" * 64  # the read after the write carries the revision the write answered
        answer = f"(path, body) => [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"
        out = self.run_page(f"""var release, reads = 0;
DETAIL = id => ++reads === 1 ? new Promise(r => {{ release = () => r([200, DETAILS[id]]); }}) : [200, AFTER];
open({plan}); await flush();
shellRun(cmd('item.status.in_progress'), C.get('{plan}')); await flush();
release(); await flush(); R.html = ELS.details.html; R.reads = reads;""", answer, prelude=f"var AFTER = {json.dumps(after)};\n")
        self.assertEqual(out["R"]["reads"], 2)
        self.assertIn("Written after the move", out["R"]["html"])
        self.assertNotIn("Check the budget first", out["R"]["html"])

    # sd:3012: backlog triage. Close and the priorities run on every picked row, each with its own revision, and the group
    # says which rows landed, which were stale and which failed.
    def test_close_runs_on_every_picked_row_with_one_reason_and_names_the_stale_one(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"""(path, body) => path === '/api/items/{ask}/cancel-task' ? [409, {{ error: 'item {ask} changed; reload it before applying this change', reload: true }}]
  : [200, {{ item: {{ id: {plan}, status: 'done', priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'c'.repeat(64) }}]"""
        out = self.run_page(f"""FIELD_VALUES = {{ reason: "nobody's doing it" }};
C.runBulk(cmd('item.close'), [C.get('{plan}'), C.get('{ask}')]); await flush();""", answer)
        self.assertEqual(out["confirms"], ["item.close"])
        self.assertEqual(out["posts"], [
            [f"/api/items/{plan}/cancel-task", {"reason": "nobody's doing it", "revision": self.row("plan")["revision"]}, 64],
            [f"/api/items/{ask}/cancel-task", {"reason": "nobody's doing it", "revision": self.row("ask")["revision"]}, 64]])
        self.assertEqual(out["bulkCli"], f"sd task cancel {plan} --reason 'nobody'\\''s doing it' && sd task cancel {ask} --reason 'nobody'\\''s doing it'")
        self.assertEqual(out["toasts"], [[f"Close · 1 item · 1 of 2 refused stale, read again: #{ask} Answer the question", False]])
        self.assertEqual(out["gets"].count("/api/tasks"), 3, "the landed close and the stale refusal each read the rows again")

    def test_close_with_no_reason_posts_nothing(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        out = self.run_page(f"""FIELD_VALUES = {{ reason: '  ' }}; C.runBulk(cmd('item.close'), [C.get('{plan}'), C.get('{ask}')]); await flush();""")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"], [["not run: OK is off until Reason is typed", False]])

    def test_close_is_off_where_sd_task_cancel_refuses(self):
        self.repeat_plan()
        plan, ask, port, old = self.ids["plan"], self.ids["ask"], self.ids["port"], self.ids["old"]
        out = self.run_page(f"""R.off = [{plan}, {ask}, {port}, {old}].map(i => C.get(String(i)) ? cmd('item.close').when(C.get(String(i))) : 'not listed');""")
        self.assertEqual(out["R"]["off"], [
            "a recurring task cannot be cancelled; clear its recurrence first, or complete it", True,
            "Close works on a task or followup; this is a work item", "not listed"])

    def test_a_priority_runs_on_every_picked_row_with_its_own_revision(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = """(path, body) => { const id = +path.split('/').pop(); return [200, { item: { id, status: 'ready', priority: body.priority, due: null, recurrence: null }, notes: [], revision: String(id).repeat(64).slice(0, 64) }]; }"""
        # A pick that mixes priorities keeps P3 on the bar: the row already at P3 is named, and the other row still runs.
        out = self.run_page(f"""R.p3 = [cmd('item.p3').when(C.get('{plan}')), cmd('item.p3').when(C.get('{ask}'))];
C.runBulk(cmd('item.p3'), [C.get('{plan}'), C.get('{ask}')]); await flush();
C.runBulk(cmd('item.p1'), [C.get('{plan}'), C.get('{ask}')]); await flush();""", answer)
        self.assertEqual(out["R"]["p3"], [True, True])
        self.assertEqual(out["posts"], [[f"/api/items/{plan}", {"priority": 3, "revision": self.row("plan")["revision"]}, 64],
                                        [f"/api/items/{plan}", {"priority": 1, "revision": (str(plan) * 64)[:64]}, 64],
                                        [f"/api/items/{ask}", {"priority": 1, "revision": self.row("ask")["revision"]}, 64]])
        self.assertEqual(out["bulkCli"], f"sd task edit {plan} --priority 1 && sd task edit {ask} --priority 1")
        self.assertEqual(out["toasts"], [[f"Edit → P3 · 1 item · 1 of 2 not changed: #{ask} Answer the question (it is already P3)", True],
                                         ["Edit → P1 · 2 items", True]])

    # sd:5462: every page opens an item at /tasks?row=<n>. One the read leaves out, a quiet report or one done before the
    # week, opens in Details here with why the list leaves it out, never on its classic item page and never in a view.
    LEFT_OUT = """const views = () => ['list', 'board', 'matrix'].map(v => { document.dispatchEvent(new CustomEvent('tasks:view', { detail: v })); return ELS['view-' + v].html; });
R.replaced = OUT.replaced || null; R.views = views(); R.det = ELS.details.html; R.sub = ELS.subhead.html;
const kept = []; window.shell.reconcile = a => { kept.push(a.current, !!(a.keep && a.keep(a.current))); };
ELS.filters.listeners.change[0]({ target: { dataset: { pick: 'kind' }, value: 'task' } }); R.kept = kept;
ELS.main.listeners.change.forEach(f => f({ target: { dataset: { edit: 'p', key: String(ASK) }, value: '1' } })); await flush();
R.after = ELS.details.html; R.views2 = views(); R.obj = C.get(String(ITEM)) && C.get(String(ITEM)).type; R.gets = OUT.gets;"""

    def test_a_linked_item_the_view_hides_stays_selected_and_says_so(self):
        # sd:5462: a report done this week is in the read, but the board's view hid it, so the first draw selected the first
        # card and rewrote ?row= (seen in a real browser on 2026-10-20). The linked row stays selected until another is picked.
        shown = self.item("A report that wants a look", kind="report", fields={"attention": True})
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.assertIn(shown, {row["id"] for row in self.doc["rows"]})
        out = self.run_page(self.LEFT_OUT.replace("ASK", str(self.ids["ask"])).replace("ITEM", str(shown)), self.ECHO,
                            search=f"?view=list&row={shown}")
        kept = out["R"]["kept"]
        self.assertEqual(set(zip(kept[::2], kept[1::2], strict=False)), {(str(shown), True)})
        self.assertIn(f"The view or filters hide #{shown}; it stays selected until you pick another.", out["R"]["after"])

    def test_a_linked_item_the_list_leaves_out_opens_in_details_and_in_no_view(self):
        quiet = self.item("A quiet nightly run", kind="report", fields={"attention": False})
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details[str(quiet)] = tasks_screen.details(self.connection, quiet, now=NOW)
        for item, title, why in ((self.ids["old"], "Finished long ago", "done before the week"),
                                 (quiet, "A quiet nightly run", "a report that needs no attention")):
            with self.subTest(item=item):
                self.assertNotIn(item, {row["id"] for row in self.doc["rows"]})
                out = self.run_page(self.LEFT_OUT.replace("ASK", str(self.ids["ask"])).replace("ITEM", str(item)), self.ECHO,
                                    search=f"?view=list&row={item}")
                self.assertIsNone(out["R"]["replaced"])
                self.assertIn(f"<h2>{title}</h2>", out["R"]["det"])
                self.assertIn(f"The list, board and matrix leave #{item} out: {why}.", out["R"]["det"])
                for drawn in out["R"]["views"] + out["R"]["views2"]:
                    self.assertNotIn(item, keys(drawn))
                    self.assertNotIn(title, drawn)
                self.assertTrue(out["R"]["sub"].startswith("read "), out["R"]["sub"])
                # A filter change keeps it selected though no row shows it, and a reread after a write keeps it in Details.
                kept = out["R"]["kept"]
                self.assertEqual(set(zip(kept[::2], kept[1::2], strict=False)), {(str(item), True)})
                self.assertIn(f"The list, board and matrix leave #{item} out: {why}.", out["R"]["after"])
                self.assertEqual(out["R"]["obj"], "item")
                self.assertNotIn("not read again", out["R"]["after"])
                self.assertEqual(out["R"]["gets"].count("/api/tasks"), 2)

    def test_a_linked_id_with_no_item_says_so_and_a_failed_read_opens_nothing(self):
        out = self.run_page("R.replaced = OUT.replaced || null; R.det = ELS.details.html;", search="?view=list&row=4242")
        self.assertIsNone(out["R"]["replaced"])
        self.assertIn("#4242 did not open: no item 4242.", out["R"]["det"])
        # A read that failed lists nothing, which says nothing about the item: the page stays and says the read failed.
        out = self.run_page("R.replaced = OUT.replaced || null; R.det = ELS.details.html; R.gets = OUT.gets;",
                            prelude="var FAIL_TASKS = 'the store is locked';", search="?view=list&row=4242")
        self.assertIsNone(out["R"]["replaced"])
        self.assertEqual(out["R"]["gets"], ["/api/tasks"])
        self.assertIn("The tasks were not read, so nothing is selected.", out["R"]["det"])

    def park(self):
        """A P4 task the nightly prune parked, in the store and in the documents read again."""
        parked = self.item("Parked by the prune", kind="task", priority=4)
        set_item_fields(self.connection, parked, parked_at="2026-09-01T02:00:00Z")
        self.connection.commit()
        self.doc = tasks_screen.document(self.connection, now=NOW)
        return parked

    # sd:3012: triage's Parked view. A parked row stays off every other view, the badge and the count; the toolbar's
    # Open | Parked (sd:6701) shows the parked rows alone, and Unpark runs on each picked one. Pack sd:7263 dropped All.
    PARK_SEG = ("ELS['parked-seg'].listeners.click[0]({ target: { closest: s => s === 'button[data-aside=\"parked\"]'"
                " ? { dataset: { v: '%s' } } : null } });")

    def test_a_parked_row_shows_only_under_parked(self):
        self.park()
        out = self.run_page(f"""R.sub = ELS.subhead.html; R.board = ELS['view-board'].html; R.filters = ELS.filters.html;
R.seg = ELS['parked-seg'].html; {self.PARK_SEG % 'aside'}
R.parked = ELS['view-board'].html; R.url = OUT.urls[OUT.urls.length - 1];""")
        self.assertIn("1 parked · read", out["R"]["sub"])
        self.assertEqual(out["counts"]["personal"], 3)
        self.assertNotIn("Parked by the prune", out["R"]["board"])
        self.assertIn("Plan the review", out["R"]["board"])
        # sd:5643's Scope keeps Open only and Scheduled only (sd:5649); Parked is the toolbar's segment now.
        self.assertIn('<select id="f-active" class="fsel" data-pick="active"><option value="" selected>Open and done</option><option value="1">Open only</option>'
                      '<option value="scheduled">Scheduled only</option></select>', out["R"]["filters"])
        self.assertNotIn('Parked only', out["R"]["filters"])
        self.assertIn('data-v="aside" aria-pressed="false">Parked <span class="n">1</span>', out["R"]["seg"])
        self.assertIn("Parked by the prune", out["R"]["parked"])
        self.assertNotIn("Plan the review", out["R"]["parked"])
        self.assertIn("parked=1", out["R"]["url"])
        self.assertNotIn('data-v="all"', out["R"]["seg"])
        self.assertEqual(out["attention"][-1], self.doc["attention"])

    def test_parked_is_read_from_the_address_old_and_new(self):
        self.park()
        for search in ("?parked=1", "?active=parked"):
            out = self.run_page("R.board = ELS['view-board'].html;", search=search)
            self.assertIn("Parked by the prune", out["R"]["board"], search)
            self.assertNotIn("Plan the review", out["R"]["board"], search)

    def test_unpark_runs_on_every_picked_parked_row_and_is_off_for_a_listed_one(self):
        parked = self.park()
        other = self.item("Parked the same night", kind="followup", priority=4)
        set_item_fields(self.connection, other, parked_at="2026-09-01T02:00:00Z")
        self.connection.commit()
        self.doc = tasks_screen.document(self.connection, now=NOW)
        revisions = {row["id"]: row["revision"] for row in self.doc["rows"]}
        answer = """(path, body) => { const id = +path.split('/').pop(); return [200, { item: { id, status: 'planning', priority: 4, due: null, recurrence: null, parked_at: null }, notes: [], revision: String(id).repeat(64).slice(0, 64) }]; }"""
        out = self.run_page(f"""R.off = cmd('item.unpark').when(C.get('{self.ids["plan"]}'));
C.runBulk(cmd('item.unpark'), [C.get('{parked}'), C.get('{other}')]); await flush();""", answer)
        self.assertEqual(out["R"]["off"], "it is not parked")
        self.assertEqual(out["confirms"], [])
        self.assertEqual(out["posts"], [[f"/api/items/{parked}", {"parked_at": None, "revision": revisions[parked]}, 64],
                                        [f"/api/items/{other}", {"parked_at": None, "revision": revisions[other]}, 64]])
        self.assertEqual(out["bulkCli"], f"sd task edit {parked} --unpark && sd task edit {other} --unpark")
        self.assertEqual(out["toasts"], [["Unpark · 2 items", False]])

    def test_a_card_marks_its_task_done_from_its_done_icon(self):
        # sd:5917: a card's done icon is the Done command, Done -> next occurrence on a repeating task; the shell runs a
        # data-cmd click, and the card's own click handler leaves it alone, so it opens no Details. Off, it is disabled.
        plan, ask, port = self.ids["plan"], self.ids["ask"], self.ids["port"]
        out = self.run_page(f"""R.board = ELS['view-board'].html;
ELS.main.listeners.click.forEach(f => f({{ target: {{ closest: s => s.includes('.tile-done') ? {{}} : s === '.card' ? {{ dataset: {{ key: '{ask}' }} }} : null }} }}));
await flush(); R.gets = OUT.gets.slice();
shellRun(cmd('item.status.done'), C.get('{ask}')); await flush();""", self.ECHO)
        buttons = {key: b for b, key in re.findall(r'(<button class="icon-btn tile-done"[^>]*data-obj="(\d+)"[^>]*>)', out["R"]["board"])}
        self.assertEqual(buttons[str(ask)], f'<button class="icon-btn tile-done" type="button" data-cmd="item.status.done" data-obj="{ask}" '
                                            'aria-label="Mark Answer the question done">')
        self.assertIn('data-cmd="item.status.done"', buttons[str(plan)])
        self.assertRegex(buttons[str(port)], r' disabled title="An agent is working on this task\. Status is locked until it ends\.">$')
        self.assertNotIn(f"/api/tasks/{ask}", out["R"]["gets"], "a click on the done icon opened the Details")
        self.assertEqual([(p[0], p[1]["status"]) for p in out["posts"]], [(f"/api/items/{ask}/status", "done")])
        self.assertTrue(out["toasts"][-1][1], "Done keeps its Undo")
        self.schedule_plan(2)
        board = self.run_page("R.board = ELS['view-board'].html;", search="?active=scheduled")["R"]["board"]
        self.assertIn(f'data-cmd="item.complete" data-obj="{plan}" aria-label="Mark Plan the review done">', board,
                      "a repeating task's Done opens its next occurrence")

    def test_park_parks_a_listed_task_with_undo_and_is_off_for_a_parked_or_done_one(self):
        # sd:5906: Park beside Unpark, by hand. It posts parked_at as now through the edit route, and Undo posts it back null.
        parked = self.park()
        done = self.item("Finished already", kind="task", priority=3)
        workflow.change_status(self.connection, done, "done", who="test")
        self.connection.commit()
        self.doc = tasks_screen.document(self.connection, now=NOW)
        plan = self.ids["plan"]
        revision = {row["id"]: row["revision"] for row in self.doc["rows"]}[plan]
        answer = """(path, body) => { const id = +path.split('/').pop(); return [200, { item: { id, status: 'planning', priority: 2, due: null, recurrence: null, parked_at: body.parked_at }, notes: [], revision: String(id % 10).repeat(64) }]; }"""
        out = self.run_page(f"""const park = cmd('item.park');
R.off = [park.when(C.get('{parked}')), C.get('{done}') ? park.when(C.get('{done}')) : 'not listed'];
R.cli = park.cli(C.get('{plan}')); R.sends = park.sends(C.get('{plan}'));
shellRun(park, C.get('{plan}')); await flush(); OUT.toasts[0].undo(); await flush();""", answer)
        self.assertEqual(out["R"]["off"][0], "it is already parked")
        self.assertIn(out["R"]["off"][1], ("it is done", "not listed"))
        self.assertEqual(out["R"]["cli"], f"sd task edit {plan} --park")
        self.assertEqual(out["R"]["sends"], f"POST /api/items/{plan} {{parked_at: <now>}}")
        (first, body, _), (second, back, _) = out["posts"]
        self.assertEqual((first, second), (f"/api/items/{plan}", f"/api/items/{plan}"))
        self.assertRegex(body["parked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
        self.assertEqual(body["revision"], revision)
        self.assertEqual(back["parked_at"], None)
        self.assertTrue(out["toasts"][0][1], "Park offers no Undo")

    # sd:5649: Repeat and Lead in Details, Scope's Scheduled only, and the Lead (days) column the Columns menu shows.
    def schedule_plan(self, lead):
        """Make `plan` (due 2026-09-09, read at NOW 2026-09-06) weekly with `lead` days, and read the documents again."""
        set_item_fields(self.connection, self.ids["plan"], recurrence="FREQ=WEEKLY", recurrence_anchor="schedule", recurrence_lead=lead)
        self.connection.commit()
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {str(i): tasks_screen.details(self.connection, i, now=NOW) for i in self.ids.values()}

    RECUR_ECHO = """(path, body) => { const row = DOC.rows.find(r => path.startsWith('/api/items/' + r.id)); window.WROTE = (window.WROTE || 0) + 1;
  const ruled = 'recurrence' in body, item = { id: row.id, status: row.status, priority: row.priority, due: row.due, title: row.title, repo: row.repo_path,
    recurrence: ruled ? body.recurrence : row.recurrence, recurrence_anchor: 'recurrence_anchor' in body ? body.recurrence_anchor : ruled ? (body.recurrence ? 'schedule' : null) : row.recurrence_anchor,
    recurrence_lead: 'recurrence_lead' in body ? body.recurrence_lead : ruled && !body.recurrence ? null : row.recurrence_lead };
  return [200, { item, notes: [], revision: String(window.WROTE % 10).repeat(64) }]; }"""

    @staticmethod
    def options(html, field):
        select = re.search(rf'<select id="{field}"[^>]*>(.*?)</select>', html, re.S).group(1)
        return re.findall(r'<option value="([^"]*)"( selected)?>([^<]*)</option>', select)

    def test_a_scheduled_row_shows_only_under_scheduled_only_with_the_day_it_opens(self):
        self.schedule_plan(2)
        out = self.run_page("""R.sub = ELS.subhead.html; R.board = ELS['view-board'].html;
ELS.filters.listeners.change[0]({ target: { dataset: { pick: 'active' }, value: 'scheduled' } });
R.scheduled = ELS['view-board'].html; R.url = OUT.urls[OUT.urls.length - 1]; R.chips = ELS.filters.html;
document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'list' })); await flush(); R.list = ELS['view-list'].html;""")
        self.assertIn("1 scheduled · read", out["R"]["sub"])
        self.assertEqual(out["counts"]["personal"], 2, "a scheduled row is not counted on a realm tab")
        self.assertNotIn("Plan the review", out["R"]["board"])
        self.assertIn("Plan the review", out["R"]["scheduled"])
        self.assertIn('<span class="opens">opens Sep 7</span>', out["R"]["scheduled"])
        self.assertNotIn("Port the page", out["R"]["scheduled"])
        self.assertIn("active=scheduled", out["R"]["url"])
        self.assertIn("Scope: Scheduled only", out["R"]["chips"])
        self.assertIn('<small class="opens">opens Sep 7</small>', out["R"]["list"])

    def test_scheduled_only_is_read_from_the_address(self):
        self.schedule_plan(2)
        out = self.run_page("R.board = ELS['view-board'].html;", search="?active=scheduled")
        self.assertIn("Plan the review", out["R"]["board"])
        self.assertNotIn("Answer the question", out["R"]["board"])

    def test_the_repeat_field_offers_the_rules_and_writes_each_with_undo(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        out = self.run_page(self.CHANGE + f"""open({plan}); await flush(); R.html = ELS.details.html;
change('det-recur', 'FREQ=MONTHLY;INTERVAL=3'); await flush(); lastUndo().undo(); await flush();
open({ask}); await flush(); R.ask = ELS.details.html; change('det-recur', 'FREQ=DAILY'); await flush();""", self.RECUR_ECHO)
        self.assertEqual(self.options(out["R"]["html"], "det-recur"), [
            ("", " selected", "Does not repeat"), ("FREQ=DAILY", "", "Daily"), ("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "", "Weekdays"),
            ("FREQ=WEEKLY", "", "Weekly"), ("FREQ=MONTHLY", "", "Monthly"), ("FREQ=MONTHLY;INTERVAL=3", "", "Quarterly"),
            ("FREQ=YEARLY;BYMONTH=3,6,9,12;BYMONTHDAY=21", "", "Seasonal"), ("FREQ=YEARLY", "", "Yearly")])
        self.assertNotIn('id="det-lead"', out["R"]["html"], "a task that does not repeat has no lead")
        self.assertIn('<span class="why">needs a due date</span>', out["R"]["ask"])
        bodies = [(p[0], {k: v for k, v in p[1].items() if k != "revision"}) for p in out["posts"]]
        self.assertEqual(bodies, [(f"/api/items/{plan}", {"recurrence": "FREQ=MONTHLY;INTERVAL=3"}), (f"/api/items/{plan}", {"recurrence": None})])
        msgs = [t[0] for t in out["toasts"]]
        self.assertIn(f"#{plan} repeats quarterly · sd task edit {plan} --recur 'FREQ=MONTHLY;INTERVAL=3'", msgs)
        self.assertIn(f"#{ask} not changed: --recur needs a due date", msgs)

    def test_the_lead_field_writes_recur_lead_and_stop_repeating_undo_sets_the_lead_again(self):
        plan = self.ids["plan"]
        self.schedule_plan(2)
        out = self.run_page(self.CHANGE + f"""open({plan}); await flush(); R.html = ELS.details.html;
change('det-lead', '7'); await flush(); lastUndo().undo(); await flush();
change('det-recur', ''); await flush(); lastUndo().undo(); await flush();""", self.RECUR_ECHO)
        html = out["R"]["html"]
        self.assertIn(("FREQ=WEEKLY", " selected", "Weekly"), self.options(html, "det-recur"))
        self.assertEqual(self.options(html, "det-lead"), [
            ("0", "", "On the due date"), ("1", "", "1 day early"), ("2", " selected", "2 days early"), ("3", "", "3 days early"),
            ("7", "", "7 days early"), ("14", "", "14 days early"), ("30", "", "30 days early")])
        self.assertIn('<span class="why">opens Sep 7</span>', html)
        self.assertIn("<dt>Lead</dt><dd>2 days</dd><dt>Opens</dt><dd>2026-09-07</dd>", html)
        bodies = [{k: v for k, v in p[1].items() if k != "revision"} for p in out["posts"]]
        self.assertEqual(bodies, [{"recurrence_lead": 7}, {"recurrence_lead": 2}, {"recurrence": None},
                                  {"recurrence": "FREQ=WEEKLY", "recurrence_anchor": "schedule", "recurrence_lead": 2}])
        msgs = [t[0] for t in out["toasts"]]
        self.assertIn(f"#{plan} lead 2 days → 7 days · sd task edit {plan} --recur-lead 7", msgs)
        self.assertIn(f"#{plan} no longer repeats · sd task edit {plan} --clear-recur", msgs)

    def test_a_store_without_a_lead_shows_no_lead_field_or_column_and_writes_none(self):
        """sd:5649: the live store may be at schema 40 (tasks_screen `lead` false): Repeat stays, Lead goes quietly, and
        stopping a series sets back only the rule and anchor on Undo."""
        plan = self.ids["plan"]
        self.schedule_plan(3)
        self.doc["lead"] = False
        out = self.run_page(self.CHANGE + f"""open({plan}); await flush(); R.html = ELS.details.html; R.filters = ELS.filters.html; R.list = ELS['view-list'].html;
change('det-recur', ''); await flush(); lastUndo().undo(); await flush();""", self.RECUR_ECHO, search="?view=list&cols=lead")
        self.assertIn('id="det-recur"', out["R"]["html"])
        self.assertNotIn('id="det-lead"', out["R"]["html"])
        self.assertNotIn('data-pick="cols"', out["R"]["filters"])
        self.assertNotIn("Lead (days)", out["R"]["list"])
        self.assertEqual([{k: v for k, v in p[1].items() if k != "revision"} for p in out["posts"]],
                         [{"recurrence": None}, {"recurrence": "FREQ=WEEKLY", "recurrence_anchor": "schedule"}])

    def test_the_lead_column_is_hidden_until_the_columns_menu_shows_it(self):
        # A lead of 3 opens plan at NOW, so it is listed.
        self.schedule_plan(3)
        box = "const box = (v, on) => ELS.filters.listeners.change[0]({ currentTarget: ELS.filters, target: { dataset: { pick: 'cols' }, type: 'checkbox', value: v, checked: on, getAttribute: () => null } });\n"
        out = self.run_page(box + """R.board = ELS.filters.html; document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'list' })); await flush();
R.before = ELS['view-list'].html; R.filters = ELS.filters.html; box('lead', true); R.after = ELS['view-list'].html; R.url = OUT.urls[OUT.urls.length - 1];
R.chips = ELS.filters.html;""")
        self.assertNotIn('data-pick="cols"', out["R"]["board"], "Columns is the list's alone")
        self.assertNotIn("Lead (days)", out["R"]["before"])
        self.assertIn('<details class="fpick" data-pick="cols"><summary aria-labelledby="f-cols-l f-cols-v"><span id="f-cols-v">Default</span>', out["R"]["filters"])
        self.assertIn('data-sort="lead" title="Sort ascending">Lead (days)', out["R"]["after"])
        self.assertIn('<td class="mono lead">3</td>', out["R"]["after"])
        self.assertIn('<td class="mono lead"></td>', out["R"]["after"], "a row that does not repeat has no lead")
        self.assertIn("cols=lead", out["R"]["url"])
        self.assertNotIn("list-chips", out["R"]["chips"], "a shown column is not a filter")
        again = self.run_page("R.list = ELS['view-list'].html;", search="?view=list&cols=lead")
        self.assertIn('<td class="mono lead">3</td>', again["R"]["list"])

    def test_the_repeat_rules_are_the_ones_sd_db_recurrence_stores(self):
        """Each Repeat rule is spelled as `Rule.text()` stores it, so a stored rule selects its name (sd:5649)."""
        from sd_db import recurrence

        block = re.search(r"const REPEATS = \[(.*?)\];", TASK_DETAILS_JS, re.S).group(1)
        rules = re.findall(r"\['([A-Z0-9=;,]+)', '(\w+)'\]", block)
        self.assertEqual([name for _, name in rules], ["Daily", "Weekdays", "Weekly", "Monthly", "Quarterly", "Seasonal", "Yearly"])
        for rule, name in rules:
            self.assertEqual(recurrence.parse(rule).text(), rule, name)

    def test_fix_asks_first_posts_the_row_revision_and_names_the_session(self):
        """sd:3456. Fix asks first, naming a builder session for a task; the post carries only the row's revision."""
        plan = self.ids["plan"]
        answer = f"""(path, body) => [200, {{ item: {{ id: {plan}, status: 'planning', priority: 2, due: '2026-09-09', recurrence: null }},
  notes: [], revision: 'd'.repeat(64), fix: {{ session: 'builder', pane: 'w9:p1', checkout: '/repos/system' }} }}]"""
        out = self.run_page(f"""R.what = cmd('item.fix').consequence(C.get('{plan}')); R.cli = cmd('item.fix').cli(C.get('{plan}'));
shellRun(cmd('item.fix'), C.get('{plan}')); await flush();""", answer)
        self.assertEqual(out["confirms"], ["item.fix"])
        self.assertIn(f"Opens a Claude session on #{plan} in herdr", out["R"]["what"])
        self.assertTrue(out["R"]["cli"].startswith("no sd verb:"))
        self.assertEqual(out["posts"], [[f"/api/items/{plan}/fix", {"revision": self.row("plan")["revision"]}, 64]])
        self.assertEqual(out["toasts"], [[f"Fix started · #{plan} · builder session in herdr pane w9:p1", False]])

    def test_adversarial_review_starts_in_the_background_and_details_follow_the_run(self):
        """sd:5064: the row posts {kind: task, target}; the page reads the run until it is filed, and Details link the brief."""
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"""(path, body) => path === '/api/adversarial' ? [202, {{ id: 'r1', state: 'queued', item: {plan}, filed: null }}]
  : path === '/api/adversarial/r1' ? [200, {{ id: 'r1', state: 'done', item: {plan}, filed: 'written', brief_item: 77 }}] : [404, {{ error: 'no answer' }}]"""
        out = self.run_page(f"""R.off = cmd('item.adversarial-review').when(C.get('{ask}'));
C.select('{plan}'); window.shell.row?.('{plan}');
shellRun(cmd('item.adversarial-review'), C.get('{plan}')); await flush(); await flush(); R.det = ELS.details.html;""", answer, search=f"?row={plan}")
        self.assertEqual(out["R"]["off"], "it has no repository to review in")
        self.assertEqual(out["posts"], [["/api/adversarial", {"kind": "task", "target": plan}, 64]])
        self.assertEqual(out["gets"].count("/api/adversarial/r1"), 1)
        self.assertEqual([t[0] for t in out["toasts"]],
                         ["Review started · run r1", "Adversarial review filed · run r1 · brief sd:77; Details links it"])
        self.assertIn('<a href="/item/77">brief sd:77</a>', out["R"]["det"])
        self.assertIn(f'<a href="/tasks?row={plan}">#{plan}</a>', out["R"]["det"])

    def test_a_late_poll_for_an_older_run_does_not_draw_over_the_newer_run(self):
        """Lane review on 16cad7b5 (class pass): r1's poll answers after r2 started on the same row; Details keep r2."""
        plan = self.ids["plan"]
        answer = f"""(() => {{ globalThis.HELD = new Promise(r => {{ globalThis.RELEASE = r; }}); let n = 0;
return path => path === '/api/adversarial' ? [202, {{ id: `r${{++n}}`, state: 'queued', item: {plan}, filed: null }}]
  : path === '/api/adversarial/r1' ? HELD.then(() => [200, {{ id: 'r1', state: 'failed', item: null, reason: 'stale', filed: null }}])
  : path === '/api/adversarial/r2' ? [200, {{ id: 'r2', state: 'done', item: {plan}, filed: 'written', brief_item: 88 }}] : [404, {{ error: 'no answer' }}]; }})()"""
        out = self.run_page(f"""C.select('{plan}'); window.shell.row?.('{plan}');
shellRun(cmd('item.adversarial-review'), C.get('{plan}')); await flush();
shellRun(cmd('item.adversarial-review'), C.get('{plan}')); await flush(); await flush();
RELEASE(); await flush(); await flush(); R.det = ELS.details.html;""", answer, search=f"?row={plan}")
        self.assertIn('<a href="/item/88">brief sd:88</a>', out["R"]["det"])
        self.assertNotIn("run r1", out["R"]["det"])
        self.assertFalse(any("run r1:" in t[0] for t in out["toasts"]), out["toasts"])

    def test_a_run_answer_for_a_row_no_longer_shown_leaves_details_alone(self):
        """Class pass on 16cad7b5: the pane listener compares the run's row with the row Details shows when the answer lands."""
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"""(() => {{ globalThis.HELD = new Promise(r => {{ globalThis.RELEASE = r; }});
return path => path === '/api/adversarial' ? [202, {{ id: 'r1', state: 'queued', item: {plan}, filed: null }}]
  : path === '/api/adversarial/r1' ? HELD.then(() => [200, {{ id: 'r1', state: 'done', item: {plan}, filed: 'written', brief_item: 77 }}])
  : [404, {{ error: 'no answer' }}]; }})()"""
        out = self.run_page(f"""shellRun(cmd('item.adversarial-review'), C.get('{plan}')); await flush();
open('{ask}'); await flush(); RELEASE(); await flush(); await flush(); R.det = ELS.details.html;""", answer, search=f"?row={plan}")
        self.assertIn("<h2>Answer the question</h2>", out["R"]["det"])
        self.assertNotIn("adv-run", out["R"]["det"])

    def test_a_refused_adversarial_review_toasts_the_servers_sentence(self):
        plan = self.ids["plan"]
        answer = """(path, body) => [409, { error: 'An adversarial review is already running (run r0); one runs at a time.' }]"""
        out = self.run_page(f"""shellRun(cmd('item.adversarial-review'), C.get('{plan}')); await flush();""", answer)
        self.assertEqual(out["toasts"], [["An adversarial review is already running (run r0); one runs at a time.", False]])

    def test_a_refused_fix_says_why_and_offers_no_undo(self):
        plan = self.ids["plan"]
        answer = """(path, body) => [503, { error: 'No session started: herdr.sh launch: session sd is not running and did not start: herdr --session sd server exited 1' }]"""
        out = self.run_page(f"""R.on = cmd('item.fix').when(C.get('{plan}'));
shellRun(cmd('item.fix'), C.get('{plan}')); await flush();""", answer)
        self.assertTrue(out["R"]["on"])
        self.assertEqual(len(out["posts"]), 1)
        self.assertIn("did not start: herdr --session sd server exited 1", out["toasts"][-1][0])
        self.assertFalse(out["toasts"][-1][1])

    def test_a_failed_launch_reads_the_tasks_again_so_details_show_its_note(self):
        """pack sd:7756: a failed launch writes a note on the item, so the page reads again: Details show the cause after
        the toast fades, and the next Fix carries the new revision. A refusal that wrote nothing (400) reads nothing."""
        plan = self.ids["plan"]
        failed = """(path, body) => [503, { error: 'Fix failed: herdr.sh launch: session sd is not running.'
  + ' A note on sd:1 keeps the cause.' }]"""
        refused = """(path, body) => [400, { error: 'item 1 is done' }]"""
        # pack sd:7783: a post with no answer may have noted the cause too, so it reads again as a 5xx does.
        silent = """(path, body) => { if (body) throw new TypeError('Load failed'); return [503, { error: 'store busy' }]; }"""
        for answer, want in ((failed, 1), (silent, 1), (refused, 0)):
            out = self.run_page(f"""const before = OUT.gets.filter(g => g === '/api/tasks').length;
shellRun(cmd('item.fix'), C.get('{plan}')); await flush(); await flush();
R.reads = OUT.gets.filter(g => g === '/api/tasks').length - before;
R.details = OUT.gets.filter(g => g === '/api/tasks/{plan}').length;""", answer)
            self.assertEqual(len(out["posts"]), 1)
            self.assertEqual(out["R"]["reads"], want, answer)
            self.assertEqual(out["R"]["details"], want, answer)

    def test_a_failed_launch_whose_task_left_the_rows_keeps_its_cause(self):
        """pack sd:7820: a read during the Fix post drops the task, so `again` must not look it up by the row again: a
        TypeError from T(o).id would replace the 5xx toast that names the cause."""
        plan = self.ids["plan"]
        prelude = """var HOST; Object.defineProperty(globalThis, 'taskDetails', { configurable: true,
  set(v) { const a = v.attach; v.attach = h => { HOST = h; return a(h); }; this._td = v; }, get() { return this._td; } });
"""
        answer = f"""(path, body) => path.endsWith('/fix') ? (async () => {{
  DOC.rows = DOC.rows.filter(r => String(r.id) !== '{plan}'); await HOST.reread();
  return [503, {{ error: 'Fix failed: herdr.sh launch: session sd is not running. A note on sd:{plan} keeps the cause.' }}]; }})()
  : [404, {{ error: 'no answer' }}]"""
        out = self.run_page(f"""shellRun(cmd('item.fix'), C.get('{plan}')); await flush(); await flush(); await flush();""",
                            answer, prelude=prelude)
        self.assertEqual(len(out["posts"]), 1)
        self.assertIn("session sd is not running", out["toasts"][-1][0])

    START_PLAN = {"read": NOW, "cap": 6, "running": 2, "load": 4.5, "load_max": 40.0, "room": 2, "refusal": "",
                  "start": [{"id": 11, "title": "First <ready>", "repo": "/r", "revision": "a" * 64},
                            {"id": 12, "title": "Second", "repo": "/r", "revision": "b" * 64}],
                  "skipped": [{"id": 13, "title": "Taken", "repo": "/r", "why": "a live Claude session works on it"}]}
    # The palette's Start next opens the dialog; the count field is a stand-in the test types into.
    START = """const D = MADE.find(e => /start-dlg/.test(e.className || '')); const N = El('start-n'); N.value = COUNT;
D.querySelector = s => s === '#start-n' ? N : null;
window.PAGE_COMMANDS.find(c => c.label === 'Start next ready tasks').run(); await flush();
R.shown = D.html; R.before = OUT.posts.length; D.returnValue = 'start'; D.onclose(); await flush();"""

    def start_next(self, plan, run, count="1"):
        answer = (f"(path, body) => path === '/api/tasks/start-next' ? [200, {json.dumps(plan)}] : "
                  f"path === '/api/tasks/start-next/run' ? ({run}) : [404, {{ error: 'no answer' }}]")
        return self.run_page(self.START.replace("COUNT", json.dumps(count)), answer)

    def test_start_next_shows_the_plan_first_then_posts_the_first_n_with_the_revisions_it_read(self):
        """sd:5107. Nothing posts until Build; Build sends the first N of the plan, each with its revision."""
        run = "[200, { queued: [{ id: 7, item: 11, status: 'queued' }], kickstart: { ok: true, service: 'x', why: '' } }]"
        out = self.start_next(self.START_PLAN, run)
        shown = out["R"]["shown"]
        self.assertIn("2 of 6 builders run · five-minute load 4.5 of 40", shown)
        self.assertIn("<code>#11</code> First &lt;ready&gt;", shown)
        self.assertIn("<code>#12</code> Second", shown)
        self.assertIn("<code>#13</code> Taken: a live Claude session works on it", shown)
        self.assertIn('max="2" value="2"', shown)
        self.assertIn('value="start"', shown)
        self.assertEqual(out["R"]["before"], 0)
        self.assertEqual(out["posts"], [["/api/tasks/start-next/run", {"items": [{"id": 11, "revision": "a" * 64}]}, 64]])
        self.assertEqual(out["gets"][-2:], ["/api/tasks/start-next", "/api/tasks"])

    def test_start_next_queues_a_build_and_says_so_not_a_herdr_session(self):
        """Build step 5 (pack sd:6096): the dialog offers Build, and its toast names the queued builds and the
        dispatcher's kickstart; a kickstart that failed says the next slot claims them."""
        run = "[200, { queued: [{ id: 7, item: 11, status: 'queued' }], kickstart: { ok: true, service: 'x', why: '' } }]"
        out = self.start_next(self.START_PLAN, run)
        self.assertIn('<button class="btn" value="start" type="submit">Build</button>', out["R"]["shown"])
        self.assertIn("Tasks to build (1–2)", out["R"]["shown"])
        self.assertIn("Each queues an unattended build, grouped by repo.", out["R"]["shown"])
        self.assertNotIn("herdr", out["R"]["shown"])
        self.assertEqual(out["toasts"][-1], ["Queued #11 for build · the dispatcher was started", False])
        late = ("[200, { queued: [{ id: 7, item: 11, status: 'queued' }, { id: 8, item: 12, status: 'queued' }],"
                " kickstart: { ok: false, service: 'x', why: 'launchctl kickstart exit 113' } }]")
        out = self.start_next(self.START_PLAN, late, count="2")
        self.assertEqual(out["toasts"][-1][0], "Queued #11, #12 for build · the dispatcher starts at its next slot "
                                               "(launchctl kickstart exit 113)")

    def test_start_next_refused_offers_no_build_and_a_refused_post_says_why(self):
        refused = dict(self.START_PLAN, running=None, room=0, start=[], refusal="the running builders were not counted: ps failed")
        out = self.start_next(refused, "[500, { error: 'not called' }]")
        self.assertIn("▨ of 6 builders run", out["R"]["shown"])
        self.assertIn("▲ Nothing can start: the running builders were not counted: ps failed.", out["R"]["shown"])
        self.assertNotIn('value="start"', out["R"]["shown"])
        self.assertNotIn("view=sessions", out["R"]["shown"])
        self.assertEqual(out["posts"], [])
        full = dict(self.START_PLAN, running=6, room=0, start=[], refusal="6 builders run already; the cap is 6. Stalled: "
                    "research b4211 (3h 0m). End a session in Management > Sessions to free its slots",
                    holders=[{"session": "research", "repo": "research", "agent": "b4211", "state": "stalled", "age": 10800}])
        out = self.start_next(full, "[500, { error: 'not called' }]")
        self.assertIn("Stalled: research b4211 (3h 0m).", out["R"]["shown"])
        self.assertIn('<a class="btn quiet" href="/management?view=sessions">Open Management › Sessions</a>', out["R"]["shown"])
        self.assertEqual(out["posts"], [])
        queued = "[200, { queued: [{ id: 7, item: 11 }, { id: 8, item: 12 }], kickstart: { ok: true } }]"
        out = self.start_next(dict(self.START_PLAN, room=3), queued, count="3")
        self.assertEqual(len(out["posts"][0][1]["items"]), 2)  # the plan listed two; a count past them sends no more
        out = self.start_next(self.START_PLAN, "[409, { error: 'Nothing queued: item 12 changed; refresh the selection.' }]")
        self.assertEqual(out["toasts"][-1][0], "Nothing queued: item 12 changed; refresh the selection.")
        out = self.start_next(self.START_PLAN, "[400, { error: 'Nothing queued: #12 is not ready; read the plan again.' }]")
        self.assertEqual(out["toasts"][-1][0], "Nothing queued: #12 is not ready; read the plan again.")
        out = self.start_next(self.START_PLAN, "Promise.reject(new Error('Load failed'))")
        self.assertIn("got no answer, so what queued is not known", out["toasts"][-1][0])

    # Build (pack sd:6095): the plan builds.plan answers for one ready item, read before anything is written.
    def build_plan(self, item, refused=()):
        return {"read": NOW, "budget_minutes": 60, "refused": list(refused),
                "room": {"machine": 4, "why": "", "cap": 6, "repo_cap": 3},
                "bundles": [] if refused else [{"repo": "/r/alpha", "name": "alpha", "room": 2, "model": "opus",
                                                 "branch": "build/sd-<lead assignment>",
                                                 "items": [{"id": item, "title": "Plan <the> review", "revision": "c" * 64}]}]}

    BUILD = """shellRun(cmd('item.build'), C.get('ITEM')); await flush();
const D = MADE.find(e => /id="build-h"/.test(e.html || '')); R.shown = D ? D.html : null; R.before = OUT.posts.length;
if (D) { D.returnValue = 'build'; D.onclose(); await flush(); }"""

    def test_build_refuses_a_ready_item_with_no_repository(self):
        """Pack sd:7275. The row's repo reads 'no repo' (TD.shape), so the check reads repo_path."""
        ask = self.ids["ask"]
        workflow.change_status(self.connection, ask, "ready", who="test")
        self.doc = tasks_screen.document(self.connection, now=NOW)
        out = self.run_page(f"R.off = cmd('item.build').when(C.get('{ask}'));", "() => [404, {}]")
        self.assertEqual(out["R"]["off"], "a build needs a repository")

    def test_build_shows_the_bundles_first_then_posts_the_items_with_the_revisions_it_read(self):
        """Pack sd:6095. Build on a ready row reads the plan, shows its bundle, and posts only on Build."""
        plan, port = self.ids["plan"], self.ids["port"]
        queued = f"[200, {{ queued: [{{ id: 9, item: {plan}, status: 'queued' }}], kickstart: {{ ok: true, why: '' }} }}]"
        answer = (f"(path, body) => path.startsWith('/api/tasks/build/plan') ? [200, {json.dumps(self.build_plan(plan))}] : "
                  f"path === '/api/tasks/build' ? {queued} : [404, {{ error: 'no answer' }}]")
        out = self.run_page(f"R.on = cmd('item.build').when(C.get('{plan}')); R.off = cmd('item.build').when(C.get('{port}'));\n"
                            + self.BUILD.replace("ITEM", str(plan)), answer)
        self.assertTrue(out["R"]["on"])
        self.assertIn("a build takes a ready task", out["R"]["off"])
        shown = out["R"]["shown"]
        self.assertIn("4 of 6 builders free now", shown)
        self.assertIn("alpha <small>opus · branch build/sd-&lt;lead assignment&gt; · 2 may start now</small>", shown)
        self.assertIn(f"<code>#{plan}</code> Plan &lt;the&gt; review", shown)
        self.assertIn('value="build"', shown)
        self.assertEqual(out["R"]["before"], 0)
        self.assertIn(f"/api/tasks/build/plan?items={plan}", out["gets"])
        self.assertEqual([post[:2] for post in out["posts"]],
                         [["/api/tasks/build", {"items": [plan], "revisions": {str(plan): "c" * 64}}]])
        self.assertEqual(out["toasts"][-1][0], f"Queued #{plan} for build · the dispatcher was started")

    def test_build_names_the_satellite_that_runs_a_bundle(self):
        """Pack sd:7933. A bundle whose lane runs on a satellite names that host, not the hub's room."""
        plan = self.ids["plan"]
        planned = self.build_plan(plan)
        planned["bundles"][0].update(host="work", room=0, beat=42)
        queued = f"[200, {{ queued: [{{ id: 9, item: {plan}, status: 'queued', host: 'work' }}], kickstart: null }}]"
        answer = (f"(path, body) => path.startsWith('/api/tasks/build/plan') ? [200, {json.dumps(planned)}] : "
                  f"path === '/api/tasks/build' ? {queued} : [404, {{ error: 'no answer' }}]")
        out = self.run_page(self.BUILD.replace("ITEM", str(plan)), answer)
        self.assertIn("alpha <small>opus · branch build/sd-&lt;lead assignment&gt; · runs on work, whose dispatcher "
                      "beat 42 s ago</small>", out["R"]["shown"])
        self.assertEqual(out["toasts"][-1][0], f"Queued #{plan} for build · work's dispatcher claims #{plan} "
                                               "at its next tick")

    def test_build_with_a_refused_item_offers_no_build_and_a_409_says_nothing_queued(self):
        plan = self.ids["plan"]
        refused = self.build_plan(plan, refused=[{"id": plan, "why": f"item {plan} is held by build run r1"}])
        answer = (f"(path, body) => path.startsWith('/api/tasks/build/plan') ? [200, {json.dumps(refused)}] : "
                  f"[500, {{ error: 'not called' }}]")
        out = self.run_page(self.BUILD.replace("ITEM", str(plan)), answer)
        self.assertIn("Nothing can be queued while one item is refused", out["R"]["shown"])
        self.assertIn(f"item {plan} is held by build run r1", out["R"]["shown"])
        self.assertNotIn('value="build"', out["R"]["shown"])
        self.assertEqual(out["posts"], [])
        answer = (f"(path, body) => path.startsWith('/api/tasks/build/plan') ? [200, {json.dumps(self.build_plan(plan))}] : "
                  f"[409, {{ error: 'Nothing queued: item {plan} changed; refresh the selection.' }}]")
        out = self.run_page(self.BUILD.replace("ITEM", str(plan)), answer)
        self.assertEqual(out["toasts"][-1][0], f"Nothing queued: item {plan} changed; refresh the selection.")

    def test_draft_email_asks_first_posts_the_row_revision_and_names_the_draft(self):
        """sd:3463. Draft email asks first, naming a Gmail draft that is not sent; the post carries only the row's revision."""
        plan = self.ids["plan"]
        answer = f"""(path, body) => [200, {{ item: {{ id: {plan}, status: 'planning', priority: 2, due: '2026-09-09', recurrence: null }},
  notes: [], revision: 'd'.repeat(64), draft: {{ subject: 'The plan is ready', id: 'r-1', link: 'https://mail.google.com/mail/#drafts' }} }}]"""
        out = self.run_page(f"""R.what = cmd('item.email').consequence(C.get('{plan}')); R.cli = cmd('item.email').cli(C.get('{plan}'));
shellRun(cmd('item.email'), C.get('{plan}')); await flush();""", answer)
        self.assertEqual(out["confirms"], ["item.email"])
        self.assertIn(f"from #{plan}'s title and notes", out["R"]["what"])
        self.assertIn("Nothing is sent.", out["R"]["what"])
        self.assertTrue(out["R"]["cli"].startswith("no sd verb:"))
        self.assertEqual(out["posts"], [[f"/api/items/{plan}/draft-email", {"revision": self.row("plan")["revision"]}, 64]])
        self.assertEqual(out["toasts"], [[f'Draft saved · #{plan} · "The plan is ready" · in Gmail Drafts, not sent', False]])

    def test_a_draft_email_past_the_item_write_bound_still_lands_its_answer(self):
        # sd:8033 review: the 30 s bound is for the item's status and field writes. Draft email runs a model and Gmail
        # (email_draft: 120 s plus 30 s), so its post keeps the browser's wait: a 40 s draft still lands its answer.
        plan = self.ids["plan"]
        answer = f"""(path, body) => [200, {{ item: {{ id: {plan}, status: 'planning', priority: 2, due: '2026-09-09', recurrence: null }},
  notes: [], revision: 'd'.repeat(64), draft: {{ subject: 'The plan is ready', id: 'r-1', link: 'https://mail.google.com/mail/#drafts' }} }}]"""
        prelude = "var AbortSignal = { timeout: ms => ({ timedOut: true }) };\n"
        out = self.run_page(f"""const F = fetch;
fetch = (path, o) => o && o.signal && o.signal.timedOut
  ? Promise.reject(Object.assign(new Error('signal timed out'), {{ name: 'TimeoutError' }})) : F(path, o);
shellRun(cmd('item.email'), C.get('{plan}')); await flush();""", answer, prelude=prelude)
        self.assertEqual(out["toasts"], [[f'Draft saved · #{plan} · "The plan is ready" · in Gmail Drafts, not sent', False]])

    def test_draft_email_is_off_for_a_kind_it_does_not_fit_and_a_failure_reads_again(self):
        plan, ask, port = self.ids["plan"], self.ids["ask"], self.ids["port"]
        answer = """(path, body) => [504, { error: 'Draft email failed: claude -p gave no answer in 120 s. No Gmail draft was saved.' }]"""
        out = self.run_page(f"""R.on = [{plan}, {ask}, {port}].map(i => cmd('item.email').when(C.get(String(i))));
const before = OUT.gets.length; shellRun(cmd('item.email'), C.get('{plan}')); await flush(); R.reads = OUT.gets.slice(before);""", answer)
        self.assertEqual(out["R"]["on"], [True, True, "Draft email writes about a task or followup; this is a work item"])
        self.assertEqual(len(out["posts"]), 1)
        self.assertIn("No Gmail draft was saved.", out["toasts"][-1][0])
        self.assertFalse(out["toasts"][-1][1])
        self.assertIn("/api/tasks", out["R"]["reads"])

    # sd:5103: the targets tasks_screen gives a row whose repo files GitHub issues and sets no Jira project.
    GITHUB_ONLY = {"github": {"ok": True, "target": {"kind": "github", "repo": "example/tool"}, "why": None},
                   "jira": {"ok": False, "target": None, "why": "no Jira project is set for this repository (Management)"}}

    def issue_answer(self, plan, draft, filed=None):
        """A server for one GitHub issue run on `plan`: the start answers queued, the first read drafting and every later
        read `draft`; Confirm answers `filed`, or a 409 when it is None."""
        run = {"id": f"github-{plan}-20261018T120000Z", "item": plan, "kind": "github", "target": {"kind": "github", "repo": "example/tool"}}
        return run, f"""(() => {{ const RUN = {json.dumps(run)}, DRAFT = {json.dumps(draft)}, FILED = {json.dumps(filed)}; let reads = 0;
  return (path, body) => {{
    if (path === '/api/items/{plan}/file-issue') return [202, {{ ...RUN, state: 'queued' }}];
    if (path === '/api/issue-runs/' + RUN.id) return [200, reads++ ? {{ ...RUN, ...DRAFT }} : {{ ...RUN, state: 'drafting' }}];
    if (path === '/api/issue-runs/' + RUN.id + '/confirm') return FILED ? [200, {{ ...RUN, ...FILED }}] : [409, {{ error: 'refused' }}];
    if (path === '/api/issue-runs/' + RUN.id + '/check') return FILED ? [200, {{ ...RUN, ...FILED }}] : [409, {{ error: 'still not known' }}];
    if (path === '/api/issue-runs/' + RUN.id + '/discard') return [200, {{ ...RUN, ...DRAFT, state: 'discarded' }}];
    return [404, {{ error: 'no answer' }}]; }}; }})()"""

    def test_file_issue_is_offered_only_where_the_repo_enables_it(self):
        """sd:5103. Each command is on where the row's repo enables its tracker, and off with the server's reason."""
        plan, ask = self.ids["plan"], self.ids["ask"]
        self.row("plan")["issue_targets"] = self.GITHUB_ONLY
        out = self.run_page(f"""R.on = [{plan}, {ask}].map(i => ['github', 'jira'].map(k => cmd(`item.file-${{k}}-issue`).when(C.get(String(i)))));""")
        why = self.row("ask")["issue_targets"]["github"]["why"]
        self.assertEqual(out["R"]["on"], [[True, "no Jira project is set for this repository (Management)"],
                                          [why, self.row("ask")["issue_targets"]["jira"]["why"]]])
        self.assertEqual(why, "the item names no repository")

    def test_file_issue_drafts_and_previews_and_files_once_only_on_file_issue_with_the_previewed_digest(self):
        """sd:5103. The start posts the target and the row's revision and files nothing; Details shows the exact target,
        title and body; File issue asks first, naming the target, and posts that draft's digest once."""
        plan = self.ids["plan"]
        self.row("plan")["issue_targets"] = self.GITHUB_ONLY
        draft = {"state": "ready", "title": "Crash on empty input", "body": "Steps to reproduce:\n1. Run it.", "removed": {"local path": 1},
                 "digest": "a" * 64}
        run, answer = self.issue_answer(plan, draft, {"state": "linked", "url": "https://github.com/example/tool/issues/7",
                                                      "ref": "github:example/tool#7"})
        out = self.run_page(f"""shellRun(cmd('item.file-github-issue'), C.get('{plan}')); await flush();
R.preview = ELS.details.html; R.before = OUT.posts.map(p => p[0]); const d = C.get('issue:{run["id"]}');
R.state = d.run.state; R.what = cmd('issue.file').consequence(d); R.discard = cmd('issue.discard').when(d);
shellRun(cmd('issue.file'), d); await flush(); R.after = ELS.details.html;""", answer)
        self.assertEqual(out["R"]["before"], [f"/api/items/{plan}/file-issue"])
        self.assertEqual(out["posts"][0][1], {"target": "github", "revision": self.row("plan")["revision"]})
        self.assertEqual(out["R"]["state"], "ready")
        for shown in ("GitHub example/tool", "Crash on empty input", "Steps to reproduce:\n1. Run it.", "took out 1 local path"):
            self.assertIn(shown, out["R"]["preview"])
        self.assertTrue(out["R"]["discard"])
        self.assertIn("public issue in GitHub example/tool", out["R"]["what"])
        self.assertEqual(out["confirms"], ["issue.file"])
        confirms = [p for p in out["posts"] if p[0].endswith("/confirm")]
        self.assertEqual(confirms, [[f"/api/issue-runs/{run['id']}/confirm", {"digest": "a" * 64}, 64]])
        self.assertIn("https://github.com/example/tool/issues/7", out["R"]["after"])
        self.assertEqual(out["toasts"][-1], ["GitHub issue filed · https://github.com/example/tool/issues/7 · #%d links it" % plan, False])

    def test_a_blocked_draft_shows_its_reason_not_its_text_and_offers_only_discard(self):
        """sd:5103. A blocked draft names the line, never the value; File issue is off and Discard posts once."""
        plan = self.ids["plan"]
        self.row("plan")["issue_targets"] = self.GITHUB_ONLY
        draft = {"state": "blocked", "title": "", "body": "", "removed": {}, "reason": "a privacy pattern matches body line 2"}
        run, answer = self.issue_answer(plan, draft)
        out = self.run_page(f"""shellRun(cmd('item.file-github-issue'), C.get('{plan}')); await flush();
const d = C.get('issue:{run["id"]}'); R.preview = ELS.details.html; R.file = cmd('issue.file').when(d);
shellRun(cmd('issue.discard'), d); await flush(); R.after = ELS.details.html;""", answer)
        self.assertIn("a privacy pattern matches body line 2", out["R"]["preview"])
        self.assertNotIn('<pre class="body">', out["R"]["preview"])
        self.assertEqual(out["R"]["file"], "the draft is blocked")
        self.assertFalse([p for p in out["posts"] if p[0].endswith("/confirm")])
        self.assertEqual([p[0] for p in out["posts"]][-1], f"/api/issue-runs/{run['id']}/discard")
        self.assertNotIn("issue <span", out["R"]["after"])

    def test_an_unknown_run_offers_check_again_never_file_issue_and_check_links_what_it_finds(self):
        """sd:5103. A run whose outcome is not known may have filed: File issue is off, Check again posts /check, and
        the issue it finds is linked; no Confirm is ever posted."""
        plan = self.ids["plan"]
        self.row("plan")["issue_targets"] = self.GITHUB_ONLY
        draft = {"state": "unknown", "title": "Crash on empty input", "body": "b", "removed": {},
                 "reason": "gh exited 1: HTTP 502; whether GitHub holds the issue is not known"}
        run, answer = self.issue_answer(plan, draft, {"state": "linked", "url": "https://github.com/example/tool/issues/8",
                                                      "ref": "github:example/tool#8"})
        out = self.run_page(f"""shellRun(cmd('item.file-github-issue'), C.get('{plan}')); await flush();
const d = C.get('issue:{run["id"]}'); R.file = cmd('issue.file').when(d); R.check = cmd('issue.check').when(d);
R.does = cmd('issue.check').does(d); R.preview = ELS.details.html;
shellRun(cmd('issue.check'), d); await flush(); R.after = ELS.details.html;""", answer)
        self.assertEqual(out["R"]["file"], "the draft is unknown")
        self.assertTrue(out["R"]["check"])
        self.assertEqual(out["R"]["does"], "Looks for this issue in GitHub example/tool; files nothing.")
        self.assertIn("whether GitHub holds the issue is not known", out["R"]["preview"])
        self.assertFalse([p for p in out["posts"] if p[0].endswith("/confirm")])
        self.assertEqual([p[0] for p in out["posts"]][-1], f"/api/issue-runs/{run['id']}/check")
        self.assertIn("https://github.com/example/tool/issues/8", out["R"]["after"])

    def test_a_linked_item_shows_the_issue_link_on_its_row_and_card(self):
        """sd:5103. The row and the card link the issue the item's ref names; a row with none shows no link."""
        plan = self.ids["plan"]
        self.row("plan")["issue_link"] = "https://github.com/example/tool/issues/7"
        out = self.run_page("R.list = ELS['view-list'].html;", search="?view=list")
        board = self.run_page("R.board = ELS['view-board'].html;")
        for html in (out["R"]["list"], board["R"]["board"]):
            self.assertEqual(html.count('class="ext issue-link"'), 1)
            self.assertIn(f'href="https://github.com/example/tool/issues/7" target="_blank" rel="noopener noreferrer" aria-label="Filed issue for #{plan}"', html)

    def test_no_command_sets_or_clears_repeating_the_details_select_does(self):
        # sd:5737: "Edit → repeat weekly" and "Edit → stop repeating" doubled the Details Repeat select (sd:5649) on every page
        # that shows tasks (Tasks, HOA, Home and the places pages, RWBP among them, load task-details.js), so no command id starts with item.recur.
        out = self.run_page("R.ids = REG.map(c => c.id);")
        self.assertEqual([i for i in out["R"]["ids"] if i.startswith("item.recur")], [])
        self.assertIn("item.p2", out["R"]["ids"])

    def test_commands_are_off_where_the_library_would_refuse_them(self):
        plan, ask, port = self.ids["plan"], self.ids["ask"], self.ids["port"]
        (asg,) = self.details[str(port)]["assignments"]
        out = self.run_page(f"""open({ask}); await flush(); open({port}); await flush(); open({plan}); await flush();
R.p2 = cmd('item.p2').when(C.get('{plan}'));
R.cancel = cmd('asg.cancel').when(C.get('asg:{asg["id"]}'));""")
        self.assertEqual(out["R"], {
            "p2": True,
            "cancel": asg["cancel"]["reason"]})
        self.assertTrue(out["R"]["cancel"])

    def test_edit_controls_are_off_for_a_row_edit_item_refuses(self):
        # edit_item refuses every field edit on a report: Edit, P2 and a matrix placement are off with the library's
        # reason, and nothing is posted (review, PR #46).
        port = self.item("Look at the failed run", kind="report", fields={"attention": True})
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details[str(port)] = tasks_screen.details(self.connection, port, now=NOW)
        why = "report items use their own editing workflow"
        out = self.run_page(f"""open({port}); await flush();
R.edit = cmd('item.edit').when(C.get('{port}')); R.p2 = cmd('item.p2').when(C.get('{port}'));
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'matrix' }})); await flush();
document.dispatchEvent({{ type: 'keydown', key: '1', target: El('card'), preventDefault() {{}} }}); await flush();""")
        self.assertEqual((out["R"]["edit"], out["R"]["p2"]), (why, why))
        self.assertEqual(out["posts"], [])
        self.assertIn([f"#{port} not moved: {why}", False], out["toasts"])

    def test_a_status_with_no_column_gets_the_other_lane(self):
        ask = self.ids["ask"]
        for row in self.doc["rows"]:
            if row["id"] == ask:
                row["status"] = "ready_to_send"
        out = self.run_page("R.board = ELS['view-board'].html;")
        other = out["R"]["board"].split('aria-label="Other statuses"', 1)
        self.assertEqual(len(other), 2, "no Other lane")
        self.assertIn("Answer the question", other[1])
        self.assertIn("data-other", out["R"]["board"])

    def test_the_matrix_places_by_reads_is_urgent_and_each_quadrant_owns_its_cards(self):
        ask = self.ids["ask"]
        for row in self.doc["rows"]:
            if row["id"] == ask:
                row["urgent"] = True  # the server's reads.is_urgent decision, with no due date on the row
        out = self.run_page("""document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'matrix' })); await flush();
R.matrix = ELS['view-matrix'].html;""")
        quads = dict(re.findall(r'data-q="(\w+)"(.*?)(?=data-q="|$)', out["R"]["matrix"], re.S))
        self.assertIn("Answer the question", quads["delegate"])
        # Every card sits in a quadrant's drop list, a labelled group; a card is an article, not an option.
        self.assertEqual(out["R"]["matrix"].count('<div class="drop" role="group"'), 4)
        self.assertNotIn('<div class="drop">', out["R"]["matrix"])

    # 2026-09-06T12:00Z, 06:00 in Denver: Sep 14 is 8 calendar days away there, and 7 whole days by reads.is_urgent.
    SEP6 = "var RealDate = Date; Date = class extends RealDate { constructor(...a) { if (a.length) super(...a); else super(2026, 8, 6, 6, 0, 0); } };\n"

    def test_a_due_edit_is_urgent_by_the_servers_rule_from_the_read_stamp(self):
        # A due edit lands before the rows are read again, so the page places it with reads.is_urgent's due rule: from the
        # document's read stamp in whole days, not the viewer's calendar days (review, PR #46).
        plan, read = self.ids["plan"], "2026-09-06T12:00:00Z"
        self.doc["read"] = read
        row = self.row("plan")
        row.update(due="2026-12-01", urgent=False)
        self.assertTrue(reads.is_urgent({"due": "2026-09-14", "status": "ready", "kind": "task"}, now=read))
        answer = f"(path, body) => [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-14', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"
        # The landed write's reread is held: what the page shows meanwhile is its own placement, not the server's.
        out = self.run_page(f"""const A = ANSWER; ANSWER = (path, body) => path === '/api/tasks' ? new Promise(() => {{}}) : A(path, body);
shellRun(cmd('item.status.in_progress'), C.get('{plan}')); await flush();
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'matrix' }})); await flush(); R.matrix = ELS['view-matrix'].html;""",
                            answer, prelude=self.SEP6, env={"TZ": "America/Denver"})
        quads = dict(re.findall(r'data-q="(\w+)"(.*?)(?=data-q="|$)', out["R"]["matrix"], re.S))
        self.assertIn("Plan the review", quads["do"], "a task due in 7 whole days is not urgent on the page")

    def test_the_week_filter_is_the_servers_due_rule(self):
        # The ≤ 7 days chip is the window the matrix calls urgent: for each due date, the rows it keeps are the ones
        # reads.is_urgent's due rule calls urgent at the document's read stamp (review, PR #46).
        read = "2026-09-06T12:00:00Z"
        self.doc["read"] = read
        base = self.row("plan")
        dues = ["2026-09-06", "2026-09-07", "2026-09-13", "2026-09-14", "2026-09-15", "2026-10-01"]
        self.doc["rows"] = [{**base, "id": 900 + n, "title": f"due {due}", "due": due, "urgent": False} for n, due in enumerate(dues)]
        out = self.run_page("""document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'list' })); await flush();
ELS.filters.listeners.change[0]({ target: { dataset: { pick: 'due' }, value: 'week' } }); await flush();
R.list = ELS['view-list'].html;""", prelude=self.SEP6, env={"TZ": "America/Denver"})
        kept = {due for due in dues if f"due {due}" in out["R"]["list"]}
        self.assertEqual(kept, {due for due in dues if reads.is_urgent({"due": due, "status": "planning", "kind": "task"}, now=read)})
        self.assertIn("2026-09-14", kept)

    def test_the_date_dialog_bounds_are_the_servers_urgent_window(self):
        # At 2026-09-06T12:00Z the server calls Sep 14 urgent, so out of Urgent starts on Sep 15 and into Urgent ends on
        # Sep 14; 7 and 8 local days put the boundary a day early (review, PR #46).
        plan, ask = self.ids["plan"], self.ids["ask"]
        self.doc["read"] = "2026-09-06T12:00:00Z"
        self.row("plan").update(due="2026-09-10", urgent=True)
        self.row("ask").update(due=None, urgent=False, urgent_otherwise=False)
        out = self.run_page(f"""const D = MADE.find(e => /date-dlg/.test(e.className || '')); const due = El('due-in'); D.querySelector = () => due;
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'matrix' }})); await flush();
open({plan}); await flush(); document.dispatchEvent({{ type: 'keydown', key: '2', target: El('card'), preventDefault() {{}} }}); await flush();
R.out = D.html;
open({ask}); await flush(); document.dispatchEvent({{ type: 'keydown', key: '1', target: El('card'), preventDefault() {{}} }}); await flush();
R.into = D.html;""", prelude=self.SEP6, env={"TZ": "America/Denver"})
        self.assertIn('min="2026-09-15"', out["R"]["out"])
        self.assertIn("Not urgent means due after Sep 14.", out["R"]["out"])
        self.assertIn('min="2026-09-06" max="2026-09-14"', out["R"]["into"])

    def test_a_card_holding_controls_is_an_article_and_the_selection_is_aria_current(self):
        # An option's descendants are presentational to assistive technology, so a card with a checkbox and buttons
        # is no option, and no list holding cards is a listbox (review, PR #46).
        plan = self.ids["plan"]
        out = self.run_page(f"""open({plan}); await flush();
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'board' }})); await flush(); R.board = ELS['view-board'].html;
document.dispatchEvent(new CustomEvent('tasks:view', {{ detail: 'matrix' }})); await flush(); R.matrix = ELS['view-matrix'].html;""")
        for view in ("board", "matrix"):
            html = out["R"][view]
            self.assertNotIn('role="option"', html, view)
            self.assertNotIn('role="listbox"', html, view)
            self.assertNotIn("aria-selected", html, view)
            self.assertIn(f'<article class="card" tabindex="0" data-key="{plan}" aria-current="true"', html, view)

    def landed_plan(self, refused):
        """A write answer: `refused` gets a 409; a write to `plan` lands, and the stored row moves with it, as a re-read sees."""
        plan = self.ids["plan"]
        return f"""(path, body) => {refused}
  ? [409, {{ error: 'The item changed. Reload it.' }}]
  : (Object.assign(DOC.rows.find(r => r.id === {plan}), {{ status: body.status, revision: 'b'.repeat(64) }}),
     [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}])"""

    def test_a_bulk_undo_reverses_only_the_writes_that_landed(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = self.landed_plan(f"path === '/api/items/{ask}/status' && body.status === 'in_progress'")
        # No Undo is offered before every write answered; then it reverses the landed one and skips the refused one.
        out = self.run_page(f"""shellBulk(cmd('item.status.in_progress'), [C.get('{plan}'), C.get('{ask}')]);
R.early = OUT.toasts.length; await flush(); lastUndo().undo(); await flush();""", answer)
        self.assertEqual(out["R"]["early"], 0, "the bulk toast came before the writes answered")
        self.assertEqual([[p[0], p[1]["status"], p[1]["revision"]] for p in out["posts"]][2], [f"/api/items/{plan}/status", "ready", "b" * 64])
        self.assertEqual([[p[0], p[1]["status"]] for p in out["posts"]], [
            [f"/api/items/{plan}/status", "in_progress"], [f"/api/items/{ask}/status", "in_progress"], [f"/api/items/{plan}/status", "ready"]])
        self.assertIn(f"Status → In progress undone · 1 of 2 reversed · not reversed: #{ask} Answer the question (its change did not land)", [t[0] for t in out["toasts"]])

    def test_a_bulk_failure_toast_offers_undo_for_the_landed_writes_only(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = self.landed_plan(f"path === '/api/items/{ask}/status'")
        out = self.run_page(f"""shellBulk(cmd('item.status.blocked'), [C.get('{plan}'), C.get('{ask}')]); await flush();
R.gets = OUT.gets.length; lastUndo().undo(); await flush();""", answer)
        # The first read, the landed write's reread, and the refused write's load (sd:2484).
        self.assertEqual(out["R"]["gets"], 3, "the refused write did not read the rows again")
        self.assertIn([f"Status → Blocked · 1 item · 1 of 2 refused stale, read again: #{ask} Answer the question", True], out["toasts"])
        self.assertEqual(out["toasts"][-1], [f"Status → Blocked undone · 1 of 2 reversed · not reversed: #{ask} Answer the question (its change did not land)", False])
        self.assertEqual([p[0] for p in out["posts"]].count(f"/api/items/{ask}/status"), 1, "Undo wrote the refused item")
        self.assertEqual(out["posts"][-1][:2], [f"/api/items/{plan}/status", {"status": "ready", "revision": "b" * 64}])

    def test_a_bulk_undo_refuses_a_row_another_writer_changed_after_its_write(self):
        # Review round 3: the group moves plan; another writer then blocks plan; ask's 409 re-reads the rows, which bring
        # that writer's revision. Undo must not move plan back over it: it names the row and posts nothing for it.
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"""(path, body) => path === '/api/items/{ask}/status'
  ? (Object.assign(DOC.rows.find(r => r.id === {plan}), {{ status: 'blocked', revision: 'f'.repeat(64) }}), [409, {{ error: 'The item changed. Reload it.' }}])
  : [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"""
        out = self.run_page(f"""shellBulk(cmd('item.status.in_progress'), [C.get('{plan}'), C.get('{ask}')]); await flush();
R.status = DOC.rows.find(r => r.id === {plan}).status; lastUndo().undo(); await flush();""", answer)
        self.assertEqual(out["R"]["status"], "blocked")
        self.assertEqual([[p[0], p[1]["status"]] for p in out["posts"]], [
            [f"/api/items/{plan}/status", "in_progress"], [f"/api/items/{ask}/status", "in_progress"]], "Undo wrote over the other writer's change")
        self.assertEqual(out["toasts"][-1], [
            f"Status → In progress undone · 0 of 2 reversed · not reversed: #{ask} Answer the question (its change did not land), "
            f"#{plan} Plan the review (it changed after this write, and Undo would overwrite that change)", False])

    def test_a_single_undo_refuses_a_row_another_writer_changed_after_its_write(self):
        # A single move lands at revision b. Another writer blocks the task; a refused write elsewhere re-reads the rows,
        # which bring that writer's revision f. Undo posts nothing.
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"""(path, body) => path === '/api/items/{ask}/status' ? [409, {{ error: 'The item changed. Reload it.' }}]
  : [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-09', recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"""
        out = self.run_page(f"""shellRun(cmd('item.status.in_progress'), C.get('{plan}')); await flush(); const undo = lastUndo().undo;
Object.assign(DOC.rows.find(r => r.id === {plan}), {{ status: 'blocked', revision: 'f'.repeat(64) }});
shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush(); undo(); await flush();""", answer)
        self.assertEqual([p[0] for p in out["posts"]], [f"/api/items/{plan}/status", f"/api/items/{ask}/status"], "Undo wrote over the other writer's change")
        self.assertEqual(out["toasts"][-1], [f"Status → In progress not undone · #{plan} Plan the review: it changed after this write, and Undo would overwrite that change", False])

    def test_an_edit_undo_refuses_a_row_another_writer_changed_after_its_write(self):
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"""(path, body) => path === '/api/items/{plan}/status' ? [409, {{ error: 'The item changed. Reload it.' }}]
  : [200, {{ item: {{ id: {ask}, status: 'planning', priority: body.priority, due: null, recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"""
        out = self.run_page(f"""shellRun(cmd('item.p2'), C.get('{ask}')); await flush(); const undo = lastUndo().undo;
Object.assign(DOC.rows.find(r => r.id === {ask}), {{ priority: 1, revision: 'f'.repeat(64) }});
shellRun(cmd('item.status.in_progress'), C.get('{plan}')); await flush(); undo(); await flush();""", answer)
        self.assertEqual([p[0] for p in out["posts"]], [f"/api/items/{ask}", f"/api/items/{plan}/status"], "Undo wrote over the other writer's change")
        self.assertEqual(out["toasts"][-1], [f"Edit → P2 not undone · #{ask} Answer the question: it changed after this write, and Undo would overwrite that change", False])

    def test_a_queued_done_asks_again_whether_the_task_repeats(self):
        # Review round 3: repeat weekly is sent (the Details Repeat select, sd:5737); Done is pressed before it answers, while the task does not repeat yet. As
        # Done's write leaves, the task repeats, so the plain move would complete a series without its confirm: it refuses.
        plan = self.ids["plan"]
        answer = f"""(path, body) => new Promise(r => HOLD.push(() => r([200, {{ item: {{ id: {plan}, status: 'ready', priority: 2, due: '2026-09-09',
  recurrence: body.recurrence === undefined ? null : body.recurrence }}, notes: [], revision: 'b'.repeat(64) }}])))"""
        out = self.run_page(self.CHANGE + f"""open({plan}); await flush();
change('det-recur', 'FREQ=WEEKLY'); shellRun(cmd('item.status.done'), C.get('{plan}')); await flush();
HOLD.splice(0).forEach(f => f()); await flush(); HOLD.splice(0).forEach(f => f()); await flush();""", answer, prelude="var HOLD = [];\n")
        self.assertEqual([p[0] for p in out["posts"]], [f"/api/items/{plan}"], "Done was sent after the task began to repeat")
        self.assertIn([f"#{plan} Plan the review not changed: it repeats: 5 completes it and opens the next occurrence", False], out["toasts"])

    def test_each_undo_reverses_its_own_operation(self):
        ask = self.ids["ask"]
        answer = f"""(path, body) => new Promise(r => HOLD.push(() => r([200, {{ item: {{ id: {ask}, status: body.status, priority: 3,
  due: null, recurrence: null }}, notes: [], revision: body.status.slice(0, 1).repeat(64).replace(/[^a-f0-9]/g, 'd') }}])))"""
        # Ready, then In progress, both sent before either answered: the second Undo goes back to Ready, not Planning.
        out = self.run_page(f"""shellRun(cmd('item.status.ready'), C.get('{ask}')); shellRun(cmd('item.status.in_progress'), C.get('{ask}'));
await flush(); HOLD.splice(0).forEach(f => f()); await flush(); HOLD.splice(0).forEach(f => f()); await flush();
R.toasts = OUT.toasts.map(t => t.msg); OUT.toasts[1].undo(); await flush(); HOLD.splice(0).forEach(f => f()); await flush();""",
                            answer, prelude="var HOLD = [];\n")
        self.assertEqual(out["R"]["toasts"][:2], [f"#{ask} Planning → Ready · sd task status {ask} ready",
                                                  f"#{ask} Ready → In progress · sd task status {ask} in_progress"])
        self.assertEqual([p[1]["status"] for p in out["posts"]], ["ready", "in_progress", "ready"])
        self.assertEqual(out["toasts"][-1], [f"Status → In progress undone · #{ask} Answer the question", False])

    def test_an_edit_undo_restores_the_fields_its_own_write_changed(self):
        ask = self.ids["ask"]
        answer = f"""(path, body) => new Promise(r => HOLD.push(() => r([200, {{ item: {{ id: {ask}, status: 'planning', priority: body.priority,
  due: null, recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}])))"""
        # Two P2 edits sent before either answered: the second finds P2 as it leaves and sends nothing (sd:3012), so the
        # first one's Undo sets back the P3 it changed.
        out = self.run_page(f"""shellRun(cmd('item.p2'), C.get('{ask}')); shellRun(cmd('item.p2'), C.get('{ask}'));
await flush(); HOLD.splice(0).forEach(f => f()); await flush(); HOLD.splice(0).forEach(f => f()); await flush();
OUT.toasts[0].undo(); await flush(); HOLD.splice(0).forEach(f => f()); await flush();""", answer, prelude="var HOLD = [];\n")
        self.assertEqual([p[1]["priority"] for p in out["posts"]], [2, 3])
        self.assertIn([f"#{ask} Answer the question not changed: it is already P2", False], out["toasts"])
        self.assertEqual(out["toasts"][-1], [f"Edit → P2 undone · #{ask} Answer the question", False])

    def test_completing_a_repeating_task_is_confirmed_shows_the_next_occurrence_and_has_no_undo(self):
        plan = self.ids["plan"]
        row = self.row("plan")
        row["recurrence"], row["next_due"] = "FREQ=WEEKLY", "2026-09-16"
        successor = {**row, "id": 99, "status": "planning", "due": "2026-09-16", "revision": "e" * 64}
        answer = f"""(path, body) => {{ DOC.rows.push({json.dumps(successor)});
  return [200, {{ item: {{ id: {plan}, status: 'done', priority: 2, due: '2026-09-09', recurrence: null }}, notes: [],
    revision: 'b'.repeat(64), next_occurrence: 99, next_occurrence_reason: null }}]; }}"""
        out = self.run_page(f"""R.done = cmd('item.status.done').when(C.get('{plan}'));
R.complete = cmd('item.complete').when(C.get('{plan}')); R.key = cmd('item.complete').key;
shellRun(cmd('item.complete'), C.get('{plan}')); await flush(); R.listed = !!C.get('99');""", answer)
        self.assertEqual(out["R"]["done"], "it repeats: 5 completes it and opens the next occurrence")
        self.assertEqual(out["R"]["complete"], True)
        self.assertEqual(out["R"]["key"], "5")
        self.assertEqual(out["confirms"], ["item.complete"])
        self.assertEqual(out["posts"], [[f"/api/items/{plan}/status", {"status": "done", "revision": row["revision"]}, 64]])
        self.assertEqual(out["gets"], ["/api/tasks", "/api/tasks"], "the rows were not read again for the next occurrence")
        self.assertTrue(out["R"]["listed"], "the next occurrence is not listed")
        self.assertEqual(out["toasts"], [[f"#{plan} Ready → Done · next occurrence #99 due Sep 16 · sd task status {plan} done", False]])

    def test_a_completion_the_server_never_answers_says_so_and_frees_the_row(self):
        # sd:8033: Done → next occurrence was confirmed, and nothing landed or showed. A write the server never answers
        # stayed pending: no toast, and every later write on the row queued behind it. The write is bounded now: the
        # page says the outcome is not known, reads the rows again, and the next write on the row is sent.
        self.repeat_plan()
        plan = self.ids["plan"]
        answer = "(path, body) => new Promise(() => {})"
        prelude = """var BOUNDS = [];
var AbortSignal = { timeout: ms => { BOUNDS.push(ms); return { timedOut: true }; } };
"""
        out = self.run_page(f"""const F = fetch;
fetch = (path, o) => o && o.signal && o.signal.timedOut ? (OUT.posts.push([path, JSON.parse(o.body), 64]),
  Promise.reject(Object.assign(new Error('signal timed out'), {{ name: 'TimeoutError' }}))) : F(path, o);
shellRun(cmd('item.complete'), C.get('{plan}')); await flush();
R.bounds = BOUNDS.slice(); R.gets = OUT.gets.length;
shellRun(cmd('item.p1'), C.get('{plan}')); await flush();""", answer, prelude=prelude)
        self.assertEqual([p[0] for p in out["posts"]], [f"/api/items/{plan}/status", f"/api/items/{plan}"],
                         "the next write on the row waited behind the unanswered one")
        self.assertEqual(out["R"]["bounds"], [30000])
        self.assertIn([f"#{plan} Plan the review: the dashboard did not answer within 30 s, so whether it changed is "
                       "not known; the rows were read again", False], out["toasts"])
        self.assertEqual(out["R"]["gets"], 2, "the rows were not read again after the unknown outcome")

    def test_a_queued_complete_asks_again_whether_the_task_still_repeats(self):
        # The mirror case: stop repeating is sent (the Details Repeat select, sd:5737), and 5 is confirmed before it answers. As the completion leaves, the task
        # no longer repeats, so it would end without a next occurrence the confirm promised: it refuses.
        self.repeat_plan()
        plan = self.ids["plan"]
        answer = f"""(path, body) => new Promise(r => HOLD.push(() => r([200, {{ item: {{ id: {plan}, status: 'ready', priority: 2, due: '2026-09-09',
  recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}])))"""
        out = self.run_page(self.CHANGE + f"""open({plan}); await flush();
change('det-recur', ''); shellRun(cmd('item.complete'), C.get('{plan}')); await flush();
HOLD.splice(0).forEach(f => f()); await flush(); HOLD.splice(0).forEach(f => f()); await flush();""", answer, prelude="var HOLD = [];\n")
        self.assertEqual([p[0] for p in out["posts"]], [f"/api/items/{plan}"], "the completion was sent after the series stopped")
        self.assertIn([f"#{plan} Plan the review not changed: the task does not repeat; Status → Done completes it", False], out["toasts"])

    def test_a_row_that_leaves_while_its_done_confirm_is_open_says_why(self):
        # sd:8035: the shell's confirm resolves later, and run(o) read T(o) with no row: the throw rejected the
        # confirm's promise, and nothing showed. The run is refused now, and the toast names why.
        self.repeat_plan()
        plan = self.ids["plan"]
        out = self.run_page(f"""shellRun(cmd('item.complete'), C.get('{plan}')); await flush();""",
                            prelude="var ON_CONFIRM = (c, o) => { o.id = 'gone'; };\n")
        self.assertEqual(out["confirms"], ["item.complete"])
        self.assertEqual(out["posts"], [])
        self.assertIn([f"#{plan} Plan the review not changed: the task is no longer listed", False], out["toasts"])

    def repeat_plan(self):
        """Make `plan` weekly in the store, and read the documents again."""
        set_item_fields(self.connection, self.ids["plan"], recurrence="FREQ=WEEKLY")
        self.connection.commit()
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {str(i): tasks_screen.details(self.connection, i, now=NOW) for i in self.ids.values()}

    def test_a_completion_reads_the_rows_again_after_a_read_already_in_flight(self):
        # A 409 on ask starts a re-read; plan's completion lands while that read is in flight, and the server then lists
        # the next occurrence. The completion's re-read must not join the older read, which was sent before it.
        self.repeat_plan()
        plan, ask = self.ids["plan"], self.ids["ask"]
        successor = {**self.row("plan"), "id": 99, "status": "planning", "due": "2026-09-16", "recurrence": None, "revision": "e" * 64}
        answer = f"""(path, body) => path === '/api/items/{ask}/status' ? [409, {{ error: 'The item changed. Reload it.' }}]
  : (DOC.rows.push({json.dumps(successor)}), [200, {{ item: {{ id: {plan}, status: 'done', priority: 2, due: '2026-09-09', recurrence: null }},
     notes: [], revision: 'b'.repeat(64), next_occurrence: 99, next_occurrence_reason: null }}])"""
        out = self.run_page(f"""const A = ANSWER, HELD = [];
ANSWER = (path, body) => {{ if (path !== '/api/tasks') return A(path, body); const snap = JSON.parse(JSON.stringify(A(path, body)));
  return new Promise(r => HELD.push(() => r(snap))); }};
shellRun(cmd('item.status.ready'), C.get('{ask}')); await flush();
shellRun(cmd('item.complete'), C.get('{plan}')); await flush();
for (let i = 0; i < 4; i++) {{ HELD.splice(0).forEach(f => f()); await flush(); }}
R.listed = !!C.get('99');""", answer)
        self.assertEqual(out["gets"].count("/api/tasks"), 3, "the completion joined the read sent before it")
        self.assertTrue(out["R"]["listed"], "the next occurrence is not listed")
        self.assertIn(f"#{plan} Ready → Done · next occurrence #99 due Sep 16 · sd task status {plan} done", [t[0] for t in out["toasts"]])

    def test_exactly_one_key_5_command_is_on_for_a_repeating_and_a_plain_row(self):
        self.repeat_plan()
        plan, ask = self.ids["plan"], self.ids["ask"]
        out = self.run_page(f"""const five = k => REG.filter(c => c.key === '5' && c.when(C.get(k)) === true).map(c => c.id);
R.plan = five('{plan}'); R.ask = five('{ask}');""")
        self.assertEqual(out["R"], {"plan": ["item.complete"], "ask": ["item.status.done"]})

    def test_the_confirm_names_the_next_occurrence_before_the_write(self):
        self.repeat_plan()
        plan = self.ids["plan"]
        self.assertEqual(self.row("plan")["next_due"], "2026-09-16")
        self.assertIsNone(self.row("ask")["next_due"])
        out = self.run_page(f"R.text = cmd('item.complete').consequence(C.get('{plan}'));")
        self.assertTrue(out["R"]["text"].startswith(f"Completes #{plan} and opens the next occurrence, due Sep 16 (FREQ=WEEKLY)."), out["R"]["text"])
        self.assertEqual(out["posts"], [])
        # No next date (workflow.next_occurrence_due returned None): the completion ends the series, and both the confirm
        # and Done's off reason say so instead of promising an occurrence (review, PR #46).
        self.row("plan")["next_due"] = None
        out = self.run_page(f"R.text = cmd('item.complete').consequence(C.get('{plan}')); R.done = cmd('item.status.done').when(C.get('{plan}'));")
        self.assertEqual(out["R"]["text"], f"Completes #{plan} and ends the series: its rule (FREQ=WEEKLY) gives no next date, so no next occurrence opens. There is no Undo.")
        self.assertEqual(out["R"]["done"], "it repeats: 5 completes it and ends the series, since its rule gives no next date")

    def test_5_on_a_mixed_selection_moves_the_plain_rows_and_skips_the_repeating_ones(self):
        self.repeat_plan()
        plan, ask = self.ids["plan"], self.ids["ask"]
        answer = f"(path, body) => [200, {{ item: {{ id: {ask}, status: 'done', priority: 3, due: null, recurrence: null }}, notes: [], revision: 'b'.repeat(64) }}]"
        out = self.run_page(f"""document.dispatchEvent(new CustomEvent('shell:picked', {{ detail: ['{plan}', '{ask}'] }}));
document.dispatchEvent({{ type: 'keydown', key: '5', target: El('card'), preventDefault() {{}} }}); await flush();
R.bulk = cmd('item.complete').bulk;""", answer)
        self.assertEqual(out["posts"], [[f"/api/items/{ask}/status", {"status": "done", "revision": self.row("ask")["revision"]}, 64]])
        self.assertIn([f"Skipped #{plan}: it repeats: 5 completes it and opens the next occurrence", False], out["toasts"])
        self.assertIn(["Status → Done · 1 item", True], out["toasts"])
        self.assertIsNone(out["R"].get("bulk"))

    def test_a_refused_cancel_reads_the_assignment_again(self):
        # A 409 means the assignment revision the Details hold is old; a retry would send it again unless they are read
        # again (review, PR #46).
        port = self.ids["port"]
        (asg,) = self.details[str(port)]["assignments"]
        answer = f"(path) => path === '/api/assignments/{asg['id']}/cancel' ? [409, {{ error: 'The assignment changed. Reload it.' }}] : [404, {{}}]"
        out = self.run_page(f"""open({port}); await flush();
try {{ await cmd('asg.cancel').run(C.get('asg:{asg['id']}')); }} catch (e) {{ R.err = e.message; }} await flush();
R.reads = OUT.gets.filter(g => g === '/api/tasks/{port}').length;""", answer)
        self.assertEqual(out["R"]["err"], "The assignment changed. Reload it.")
        self.assertEqual([p[:2] for p in out["posts"]], [[f"/api/assignments/{asg['id']}/cancel", {"revision": asg["revision"]}]])
        self.assertGreaterEqual(out["R"]["reads"], 2, "the Details were not read again after the refusal")

    def test_a_task_added_under_a_filter_that_hides_it_is_not_selected(self):
        # Selection stays on a visible task: a new task the filters hide is added and named, not selected (review, PR #46).
        new = {**self.row("ask"), "id": 99, "title": "Call the bank", "kind": "task", "repo": None, "repo_path": None}
        answer = f"(path) => path === '/api/items' ? (DOC.rows.push({json.dumps(new)}), [201, {{ item: {{ id: 99, title: 'Call the bank' }} }}]) : [404, {{}}]"
        out = self.run_page("""ELS.filters.listeners.change[0]({ target: { dataset: { pick: 'kind' }, value: 'work' } }); await flush();
var box = document.getElementById('shift-in'); box.value = 'Call the bank';
box.listeners.keydown.forEach(f => f({ key: 'Enter', preventDefault() {}, stopPropagation() {} })); await flush();""", answer)
        self.assertIn(["Added #99 to Planning · the filters hide it; Clear all shows it", False], out["toasts"])
        self.assertNotIn("/api/tasks/99", out["gets"], "the hidden task was selected and its Details read")

    def test_the_confirm_names_the_rule_of_a_series_sd_cannot_walk(self):
        set_item_fields(self.connection, self.ids["plan"], recurrence="FREQ=WEEKLY;COUNT=3")
        self.connection.commit()
        self.doc = tasks_screen.document(self.connection, now=NOW)
        plan = self.ids["plan"]
        out = self.run_page(f"""const o = C.get('{plan}'); R.text = cmd('item.complete').consequence(o); R.danger = cmd('item.complete').danger(o);
R.done = cmd('item.status.done').when(o);""")
        # The library walks no COUNT rule, so the completion ends that series; the confirm names the rule and says so, on the
        # danger button, since the series ends for good (sd:2250, case 3).
        self.assertIsNone(self.row("plan")["next_due"])
        self.assertEqual(out["R"]["text"], f"Completes #{plan} and ends the series: sd cannot walk FREQ=WEEKLY;COUNT=3, so no next occurrence opens. There is no Undo.")
        self.assertTrue(out["R"]["danger"])
        self.assertEqual(out["R"]["done"], "it repeats: 5 completes it and ends the series, since sd cannot walk its rule")

    def test_a_byday_series_opens_its_next_occurrence(self):
        # sd:3713: the library walks BYDAY on FREQ=WEEKLY (pack sd:3453), so the page does too and names the next date.
        rule = "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"
        set_item_fields(self.connection, self.ids["plan"], recurrence=rule)
        self.connection.commit()
        self.doc = tasks_screen.document(self.connection, now=NOW)
        plan = self.ids["plan"]
        out = self.run_page(f"""const o = C.get('{plan}'); R.text = cmd('item.complete').consequence(o); R.danger = cmd('item.complete').danger(o);
R.done = cmd('item.status.done').when(o);""")
        nxt = self.row("plan")["next_due"]
        self.assertIsNotNone(nxt)
        self.assertEqual(datetime.date.fromisoformat(nxt).weekday(), 4)
        self.assertIn(f"Completes #{plan} and opens the next occurrence, due ", out["R"]["text"])
        self.assertIn(f"({rule})", out["R"]["text"])
        self.assertFalse(out["R"]["danger"])
        self.assertEqual(out["R"]["done"], "it repeats: 5 completes it and opens the next occurrence")

    def test_a_rule_whose_date_the_page_has_not_read_says_sd_sets_it(self):
        # sd:2250, cases 1 and 2. The row's next_due is the date the confirm names, on a plain confirm button. A readback
        # that moves the due date leaves the next date unknown until the rows are read again (the reread is held here): the
        # confirm names the valid rule and says sd sets its date.
        plan = self.ids["plan"]
        self.row("plan").update(recurrence="FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=9", next_due="2026-11-09")
        answer = f"(path, body) => [200, {{ item: {{ id: {plan}, status: body.status, priority: 2, due: '2026-09-10', recurrence: 'FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=9' }}, notes: [], revision: 'b'.repeat(64) }}]"
        out = self.run_page(f"""const o = C.get('{plan}'); R.dated = [cmd('item.complete').consequence(o), cmd('item.complete').danger(o)];
const A = ANSWER; ANSWER = (path, body) => path === '/api/tasks' ? new Promise(() => {{}}) : A(path, body);
shellRun(cmd('item.status.in_progress'), o); await flush();
R.undated = [cmd('item.complete').consequence(o), cmd('item.complete').danger(o)];""", answer)
        rule = "FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=9"
        self.assertEqual(out["R"]["dated"], [f"Completes #{plan} and opens the next occurrence, due Nov 9 ({rule}). A move back would leave two open tasks, so there is no Undo.", False])
        self.assertEqual(out["R"]["undated"], [f"Completes #{plan} and opens the next occurrence ({rule}); sd sets its date. A move back would leave two open tasks, so there is no Undo.", False])

    def test_the_rule_parts_the_page_walks_are_the_librarys(self):
        from sd_db import recurrence

        parts = re.search(r"const PARTS = new Set\(\[([^\]]*)\]\)", TASK_DETAILS_JS)
        self.assertIsNotNone(parts)
        self.assertEqual(tuple(re.findall(r"'([A-Z]+)'", parts.group(1))), recurrence.PARTS)
        # The shell's confirm colour asks the command where it declares danger(o).
        self.assertIn("danger: c.danger ? !!c.danger(o) : c.risk === 'confirm'", SHELL_JS)

    def test_work_and_done_rows_have_neither_key_5_command(self):
        port, ask = self.ids["port"], self.ids["ask"]
        for row in self.doc["rows"]:
            if row["id"] == ask:
                row["status"] = "done"
        out = self.run_page(f"""const five = k => REG.filter(c => c.key === '5' && c.when(C.get(k)) === true).map(c => c.id);
R.work = five('{port}'); R.done = five('{ask}');""")
        self.assertEqual(out["R"], {"work": [], "done": []})

    def test_a_task_that_does_not_repeat_has_no_complete_occurrence(self):
        ask = self.ids["ask"]
        out = self.run_page(f"R.when = cmd('item.complete').when(C.get('{ask}')); R.done = cmd('item.status.done').when(C.get('{ask}'));")
        self.assertEqual(out["R"], {"when": "the task does not repeat; Status → Done completes it", "done": True})

    def test_the_narrow_board_is_a_carousel_with_or_without_the_other_lane(self):
        # `.board[data-other]` outranks `.board`, so the narrow-screen rule names both, or a board with an Other lane keeps
        # its six columns on a phone (review, PR #46). The [data-nodone] forms outrank both: they need naming too, or the
        # container rule's three columns beat the carousel and the lanes were 2 px wide (pack sd:6017).
        css = (V2 / "static" / "tasks.css").read_text(encoding="utf-8")
        narrow = re.search(r"^@media \(max-width: 719px\) \{\n(.*?)^\}", css, re.S | re.M).group(1)
        rule = re.search(r"^  ([^{]*)\{[^}]*grid-template-columns: none;", narrow, re.M)
        self.assertIsNotNone(rule, "the narrow block sets no carousel")
        self.assertEqual({sel.strip() for sel in rule.group(1).split(",")}, {".board", ".board[data-other]", ".board[data-nodone]", ".board[data-nodone][data-other]"})

    def test_the_list_scrolls_inside_its_wrap_and_holds_its_hidden_labels(self):
        # The Actions header's `.sr` label is absolutely placed. With no positioned ancestor its box sat at the table's
        # right edge, outside the wrap's scroller, so the page scrolled sideways at 320 and 375 px (sd:3138).
        css = (V2 / "static" / "tasks.css").read_text(encoding="utf-8")
        wrap = re.search(r"^\.list-wrap \{([^}]*)\}", css, re.M)
        self.assertIsNotNone(wrap, "tasks.css sets no .list-wrap rule")
        self.assertIn("overflow-x: auto;", wrap.group(1))
        self.assertIn("position: relative;", wrap.group(1), "the wrap does not contain the table's absolutely placed labels")

    def test_the_list_fits_its_column_beside_the_docked_pane(self):
        # sd:4037: with a base's Stage and Score the list scrolled sideways 305 px in its 729 px column at 1440 and 1024.
        # The design source's folds (design.md, sd:2249): the wrap is a container whose narrow width drops the row's
        # primary button and narrows every gutter, Due drops the word its head says, and a long repo name ends in an ellipsis.
        css = (V2 / "static" / "tasks.css").read_text(encoding="utf-8")
        wrap = re.search(r"^\.list-wrap \{([^}]*)\}", css, re.M)
        self.assertIn("container-type: inline-size;", wrap.group(1))
        narrow = re.search(r"^@container \(max-width: 64rem\) \{\n(.*?)^\}", css, re.S | re.M)
        self.assertIsNotNone(narrow, "tasks.css has no narrow-list container block")
        self.assertIn(".list .rowact .btn.sm { display: none; }", narrow.group(1))
        self.assertRegex(css, r"\.list td\.rp span \{[^}]*text-overflow: ellipsis;")
        self.doc["read"] = "2026-09-06T12:00:00Z"
        self.doc["rows"] = [{**self.row("plan"), "id": 900, "title": "Far off", "due": "2026-10-01", "repo": "example-research"}]
        out = self.run_page("""document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'list' })); await flush();
R.list = ELS['view-list'].html;""", prelude=self.SEP6, env={"TZ": "America/Denver"})
        self.assertIn('<td class="mono rp"><span title="example-research">example-research</span></td>', out["R"]["list"])
        self.assertRegex(out["R"]["list"], r'<td class="mono due ?[^"]*">Oct 1</td>')

    def test_the_list_folds_id_repo_and_score_beside_the_docked_pane_at_1280(self):
        # sd:4037: the docked pane leaves the list 529 px at 1240 and 569 px at 1280, where it still scrolled 150 px.
        # Below 45.5rem Id and Repo fold into a line under the title and Score into one under Stage; no text is cut.
        css = (V2 / "static" / "tasks.css").read_text(encoding="utf-8")
        narrow = re.search(r"^@container \(max-width: 45\.5rem\) \{\n(.*?)^\}", css, re.S | re.M)
        self.assertIsNotNone(narrow, "tasks.css has no block that folds columns beside the docked pane")
        self.assertIn(".list :is(th, td):is(.id, .rp, .sc) { display: none; }", narrow.group(1))
        self.assertIn(".list .fold { display: block;", narrow.group(1))
        self.assertIn(".list .fold { display: none; }", css)
        self.doc["rows"] = [{**self.row("plan"), "id": 900, "title": "An idea", "repo": "example-research",
                             "base": "blog-idea", "stage": "inbox", "score": "7"}]
        out = self.run_page("""document.dispatchEvent(new CustomEvent('tasks:view', { detail: 'list' })); await flush();
R.list = ELS['view-list'].html;""")
        self.assertIn('<td class="mono id">#900</td>', out["R"]["list"])
        self.assertIn('An idea</button><span class="fold mono">#900 · example-research</span></td>', out["R"]["list"])
        self.assertIn('<td>inbox<span class="fold mono">score 7</span></td><td class="mono sc">7</td>', out["R"]["list"])
        self.assertEqual(re.findall(r'<th scope="col" class="label (id|rp|sc)"', out["R"]["list"]), ["id", "rp", "sc"])

    def test_a_plain_toast_clears_a_live_undo_and_the_bulk_bar(self):
        # The bar lifts both toasts one layer, and a plain message over a live Undo is one more: with all three up, the plain
        # one sits two layers above the bar's, or "Copied" covers the Undo (review, PR #46).
        css = (V2 / "static" / "shell.css").read_text(encoding="utf-8")
        layer = r"\{ bottom: calc\(var\(--space-lg\) \+ (\S+) \+ env\(safe-area-inset-bottom\)\); \}"
        offsets = {sel.strip(): size for sels, size in re.findall(r"^([^{\n]*)" + layer, css, re.M) for sel in sels.split(",")}
        self.assertEqual(offsets.get(".toast:has(~ .bulkbar:not([hidden]))"), "3.5rem")
        self.assertEqual(offsets.get(".toast:not([hidden]) ~ .toast.plain:has(~ .bulkbar:not([hidden]))"), "7rem",
                         "a plain toast over a live Undo with the bulk bar up shares the Undo's offset")

    def test_the_script_has_no_branch_for_a_row_without_an_sd_id(self):
        # sd:3289: /api/tasks rows are item rows, each with an id (TheDocuments), so the mockup's id-less job rows are gone
        # and with them the "no sd id" reasons and the `ops` kind, which item.kind does not allow.
        for text in (TASKS_JS, TASK_DETAILS_JS):
            self.assertNotIn("no sd id", text)
            self.assertNotRegex(text, r"kind [!=]== 'ops'")
            self.assertNotIn("'no id'", text)

    def test_the_script_adds_no_sink_and_no_inline_style(self):
        # task-details.js holds the Details tasks.js drew (sd:5635), so the rule holds for both.
        for text in (TASKS_JS, TASK_DETAILS_JS):
            self.assertNotIn("innerHTML", text)
            self.assertNotIn("createElement('style')", text)
            self.assertNotIn("setAttribute('style'", text)
            self.assertNotRegex(text, r"style=\\?\"")
            self.assertEqual(re.findall(r"window\.markup\b", re.sub(r"const \{ [\w, ]+ \} = window\.markup;", "", text)), [])
            # A page-level j/k or Escape handler is drift (the shell owns them through PAGE_LIST).
            self.assertNotRegex(text, r"e\.key === '[jk]'")


def keys(fragment: str) -> set[int]:
    """The ids of the rows or cards a view drew."""
    return {int(found) for found in re.findall(r'data-key="(\d+)"', fragment or "")}


class TheFilterAddress(ScreenCase):
    """The query v1 /backlog took -- status, age, active, q and page -- read and written by Tasks (sd:2589)."""

    run_page = TheScript.run_page

    def setUp(self):
        super().setUp()
        self.ids = seed(self)
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {}
        self.base = next(r for r in self.doc["rows"] if r["id"] == self.ids["plan"])
        self.doc["rows"] = [self.made(901, "Send the reply", "ready_to_send", "3", kind="message"),
                            self.made(902, "Budget review", "ready", "7"),
                            self.made(903, "Closed last week", "done", "0"),
                            self.made(904, "Plan the trip", "planning", "14", repo="other")]

    def made(self, number, title, status, age, **more):
        return {**self.base, "id": number, "title": title, "status": status, "age": age, **more}

    def listed(self, search, body=""):
        out = self.run_page(body + "\nR.list = ELS['view-list'].html; R.board = ELS['view-board'].html; R.filters = ELS.filters.html;",
                            search=search)
        return keys(out["R"]["list"]) | keys(out["R"]["board"]), out

    def test_only_the_sorted_header_carries_aria_sort_and_a_click_moves_it(self):
        """sd:2527: each column with an order sorts through its button; aria-sort is on the sorted th alone."""
        click = "ELS['view-list'].listeners.click[0]({ target: { closest: s => s === 'button[data-sort]' ? { dataset: { sort: 'due' } } : null } });"
        sorted_ = r'aria-sort="(\w+)"[^>]*><button class="sorter"[^>]*data-sort="(\w+)"'
        out = self.run_page(f"R.a = ELS['view-list'].html; {click} R.b = ELS['view-list'].html; {click} R.c = ELS['view-list'].html;"
                            " R.url = OUT.urls[OUT.urls.length - 1];", search="?view=list")
        for key, want in (("a", [("ascending", "p")]), ("b", [("ascending", "due")]), ("c", [("descending", "due")])):
            self.assertEqual(re.findall(sorted_, out["R"][key]), want, key)
            self.assertEqual(out["R"][key].count("aria-sort"), 1, key)
        # sd:2682: every column with an order sorts, through shell.list's header.
        # sd:3977: Stage and Score show only once a row has a base; this store has none.
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["a"]), ["id", "title", "repo", "status", "p", "due"])
        self.assertIn("sort=due&dir=desc", out["R"]["url"])

    def test_status_age_and_active_narrow_as_v1_did(self):
        for search, expected in (("?view=list&status=ready_to_send", {901}), ("?view=list&age=7", {902}),
                                 ("?view=list&active=1", {901, 902, 904}), ("?view=list&active=1&age=0", set()),
                                 ("?view=list&status=ready,done", {902, 903}),
                                 # A bucket v1 would not take is dropped, as v1 dropped it.
                                 ("?view=list&age=5", {901, 902, 903, 904})):
            with self.subTest(search=search):
                self.assertEqual(self.listed(search)[0], expected)
        _, out = self.listed("?view=list&status=ready_to_send&age=3&active=1")
        # sd:6233: Status has no dropdown; an address's status (Today's Landed 24h and Blocked) narrows, with its chip.
        self.assertNotIn('id="f-status"', out["R"]["filters"])
        self.assertIn('data-unfilter="status:ready_to_send"', out["R"]["filters"])
        for key, value in (("age", "3"), ("active", "1")):
            self.assertRegex(out["R"]["filters"], rf'<select id="f-{key}"[^>]*>(?:(?!</select>).)*<option value="{value}" selected>', key)
        self.assertIn('<option value="14">14-29d</option>', out["R"]["filters"])

    def test_the_text_filter_reads_q_and_the_box_writes_it(self):
        self.assertEqual(self.listed("?view=list&q=BUDGET")[0], {902})
        self.assertEqual(self.listed("?view=list&q=%23904")[0], {904})
        # The v1 capture check opens /tasks?q=<title> in the default view (static/dashboard.js).
        self.assertEqual(self.listed("?q=" + quote("Send the reply"))[0], {901})
        shown, out = self.listed("?view=list", "const box = document.getElementById('find-in'); box.value = ' Trip ';\n"
                                 "box.listeners.input[0]({ target: box }); await flush();")
        self.assertEqual(shown, {904})
        self.assertEqual(out["urls"][-1], "view=list&q=trip")

    def test_the_address_carries_every_filter(self):
        _, out = self.listed("?view=list&status=ready_to_send&age=3&active=1&q=reply")
        self.assertEqual(out["urls"][-1], "view=list&status=ready_to_send&age=3&active=1&q=reply")

    def test_the_list_pages_fifty_and_the_address_keeps_the_page(self):
        self.doc["rows"] = [self.made(1000 + n, f"row {n}", "planning", "0") for n in range(60)]
        for search, count, words in (("?view=list", 50, "1–50 of 60"), ("?view=list&page=2", 10, "51–60 of 60"),
                                     ("?view=list&page=2&size=25", 25, "26–50 of 60"), ("?view=list&page=9", 10, "51–60 of 60"),
                                     ("?view=list&page=1.5", 50, "1–50 of 60")):
            with self.subTest(search=search):
                shown, out = self.listed(search)
                self.assertEqual(len(shown), count)
                self.assertIn(words, out["R"]["list"])
        click = "ELS['view-list'].listeners.click[0]({ target: { id: '', closest: s => s === '.list-pager [data-page]' ? { dataset: { page: '2' } } : null } }); await flush();"
        shown, out = self.listed("?view=list", click)
        self.assertEqual(shown, {1000 + n for n in range(50, 60)})
        self.assertEqual(out["urls"][-1], "view=list&page=2")
        self.assertIn('data-page="2" aria-label="Page 2" aria-current="page"', out["R"]["list"])
        self.assertEqual(re.findall(r'data-size="(\d+)"', out["R"]["list"]), ["25", "50", "100", "200"])
        # A filter change starts the list at its first page again.
        chip = "ELS.filters.listeners.change[0]({ target: { dataset: { pick: 'age' }, value: '0' } }); await flush();"
        _, out = self.listed("?view=list&page=2", chip)
        self.assertEqual(out["urls"][-1], "view=list&age=0")
        # A short list draws its range and no page numbers; the board shows every filtered card, whatever the page.
        short = self.listed("?view=list&q=row%2059")[1]["R"]["list"]
        self.assertIn('<span class="range">1–1 of 1</span>', short)
        self.assertNotIn("data-page=", short)
        self.assertEqual(len(self.listed("?view=board&page=2")[0]), 60)

    def test_active_filters_show_as_chips_that_remove_one_or_clear_all(self):
        """sd:2682: one chip per active filter value above the list, with the count and one Clear all (shell.list)."""
        unfilter = "ELS.filters.listeners.click[0]({ target: { id: '', closest: s => s === '[data-unfilter]' ? { dataset: { unfilter: 'status:ready_to_send' } } : null } }); await flush();"
        clear = "ELS.filters.listeners.click[0]({ target: { id: '', closest: s => s === '[data-unfilter-all]' ? {} : null } }); await flush();"
        _, out = self.listed("?view=list&status=ready_to_send&active=1&q=reply",
                             f"R.chips = ELS.filters.html; {unfilter} R.chips2 = ELS.filters.html; R.url2 = OUT.urls[OUT.urls.length - 1];"
                             f" {clear} R.chips3 = ELS.filters.html; R.url3 = OUT.urls[OUT.urls.length - 1]; R.box = ELS['find-in'].value;")
        def chips(key):
            return re.findall(r'data-unfilter="([\w:]+)"', out["R"][key])
        self.assertEqual(chips("chips"), ["status:ready_to_send", "active:1", "q"])
        self.assertIn("3 filters · 1 of 4", out["R"]["chips"])
        self.assertEqual(chips("chips2"), ["active:1", "q"])
        self.assertEqual(out["R"]["url2"], "view=list&active=1&q=reply")
        self.assertEqual((chips("chips3"), out["R"]["url3"], out["R"]["box"]), ([], "view=list", ""), "Clear all keeps the view")

    def test_a_linked_row_opens_the_page_that_holds_it(self):
        self.doc["rows"] = [self.made(1000 + n, f"row {n}", "planning", "0") for n in range(60)]
        shown, _ = self.listed("?view=list&row=1055")
        self.assertIn(1055, shown)


class TheOperationsBars(ScreenCase):
    """Operations' age histogram opened Tasks (sd:2589); sd:3402 retired that area, and Tasks still takes its query.

    Every query a bar carried, built as the bar built it, goes into tasks.js and lists exactly that bar's rows.
    """

    run_page = TheScript.run_page
    DAYS = (0, 2, 5, 10, 20, 40, 90)

    def setUp(self):
        super().setUp()
        for days in self.DAYS:
            self.age(self.item(f"aged {days} days", status="planning"), days)
        for days in (5, 20):
            row = self.item(f"unsent for {days} days", status="planning")
            transition(self.connection, row, "ready_to_send", who="sd-ship")
            self.age(row, days)
        # Done this week, so /api/tasks lists it: a bar counts open items only, and its link says active=1.
        self.age(self.item("recently completed", status="done"), 5)
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {}

    def bars(self):
        """(href, bucket key, series) for each bar the retired histogram drew: a series of a bucket holding open rows."""
        found = set()
        for row in reads.backlog_items(self.connection, now=NOW):
            if row["status"] != "done":
                key = reads.age_bucket(row, now=NOW)
                for series in ("other", "ready_to_send") if row["status"] == "ready_to_send" else ("other",):
                    filters = {"view": "list", "active": "1", "age": key} | ({"status": series} if series == "ready_to_send" else {})
                    found.add(("/tasks?" + urlencode(sorted(filters.items())), key, series))
        return sorted(found)

    def test_every_bar_lists_exactly_its_own_rows_on_tasks(self):
        bars = self.bars()
        self.assertGreaterEqual(len(bars), 5, "the histogram drew almost no bars")
        rows = reads.backlog_items(self.connection, now=NOW)
        for href, key, series in bars:
            with self.subTest(bucket=key, series=series):
                parts = urlsplit(href)
                self.assertEqual(parts.path, "/tasks")
                expected = {row["id"] for row in rows if row["status"] != "done" and reads.age_bucket(row, now=NOW) == key
                            and (series != "ready_to_send" or row["status"] == "ready_to_send")}
                self.assertTrue(expected, "a bar was drawn for a bucket holding no rows")
                out = self.run_page("R.list = ELS['view-list'].html;", search="?" + parts.query)
                self.assertEqual(keys(out["R"]["list"]), expected)


class TheLinksIntoTasks(unittest.TestCase):
    """Operations, the v1 capture check (sd:2589) and the Skills page's Run with agent (sd:2590) open Tasks, not /backlog.

    sd:2356 retires /backlog itself.
    """

    def test_no_link_opens_a_backlog_query(self):
        root = V2.parent
        found = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".js", ".html"):
                hits = re.findall(r"""["']/backlog\?""", path.read_text(encoding="utf-8"))
                if hits:
                    found[path.relative_to(root).as_posix()] = len(hits)
        self.assertEqual(found, {})


class TheQueuesMoved(unittest.TestCase):
    """sd:8013: the Decision queues fold left Tasks for one line on Today's Needs you (now_screen.triage_rows)."""

    def test_tasks_has_no_queues_fold_or_route(self):
        page = (V2 / "tasks.html").read_text(encoding="utf-8")
        self.assertNotIn("queues-box", page)
        self.assertNotIn("/api/tasks/queues", TASKS_JS)
        entry = next(entry for entry in v2.registry.PAGES if entry.section == "Tasks")
        self.assertNotIn("/api/tasks/queues", [api.path for api in entry.api])


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "tasks", "Tasks", "/tasks", ("/api/tasks", "/api/tasks/start-next", "/api/tasks/start-next/run",
                                                           "/api/tasks/build/plan", "/api/tasks/build", "/api/tasks/1", "/api/tasks/sweep", "/api/tasks/sweep/status",
                                                           "/api/why")


class TheDocumentLinks(ScreenCase):
    """The details name the item's design and related documents, each linked where GitHub has it."""

    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp.name) / "checkout"
        for rel, text in {
            "docs/work/2026-10-01-own/prd.md": "the item's own file",
            "docs/work/2026-10-01-own/design.md": "the folder's design",
            "docs/work/2026-10-02-other/design.md": "Work: sd:{id}\n",
            "docs/work/2026-10-03-unrelated/design.md": "Work: sd:{id}0\n",
            "docs/notes/plan.md": "named in a note",
            "docs/dashboard/report.html": "<p>local report</p>",
        }.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text, encoding="utf-8")
        upsert_repo(self.connection, str(self.root), remote="git@github.com:owner/name.git")
        self.id = self.item("Build it", kind="work", repo=str(self.root), path="docs/work/2026-10-01-own/prd.md",
                            body="See docs/notes/plan.md and docs/missing.md.")
        for rel in ("docs/work/2026-10-02-other/design.md", "docs/work/2026-10-03-unrelated/design.md"):
            path = self.root / rel
            path.write_text(path.read_text(encoding="utf-8").format(id=self.id), encoding="utf-8")
        self.outside = Path(self.tmp.name) / "review" / "SYNTHESIS.md"
        self.outside.parent.mkdir()
        self.outside.write_text("x", encoding="utf-8")
        self.note(self.id, f"Report at {self.outside} and docs/dashboard/report.html", kind="comment")

    def test_the_details_list_the_design_and_related_documents_with_links(self):
        got = tasks_screen.details(self.connection, self.id, now=NOW)["documents"]
        by_path = {doc["path"]: doc for doc in got}
        self.assertEqual(sorted(by_path), sorted([
            "docs/work/2026-10-01-own/prd.md", "docs/work/2026-10-01-own/design.md",
            "docs/work/2026-10-02-other/design.md", "docs/notes/plan.md",
            "docs/dashboard/report.html", str(self.outside),
        ]))
        self.assertEqual(by_path["docs/work/2026-10-01-own/design.md"]["url"],
                         "https://github.com/owner/name/blob/HEAD/docs/work/2026-10-01-own/design.md")
        # docs/dashboard is gitignored and a file outside the checkout has no GitHub address: path only.
        self.assertEqual(by_path["docs/dashboard/report.html"]["url"], "")
        self.assertEqual(by_path[str(self.outside)]["url"], "")
        self.assertTrue(all(doc["why"] for doc in got))

    def test_an_item_without_a_repo_lists_only_absolute_paths_its_notes_name(self):
        loose = self.item("Loose", kind="task", body=f"see {self.outside}")
        got = tasks_screen.details(self.connection, loose, now=NOW)["documents"]
        self.assertEqual([doc["path"] for doc in got], [str(self.outside)])

    def test_the_panel_renders_the_documents_section(self):
        self.assertIn("<h3>Documents ${src(", TASK_DETAILS_JS)
        self.assertIn("d.documents", TASK_DETAILS_JS)


if __name__ == "__main__":
    unittest.main()


class TheBadge(BrowserSession):
    """sd:3446: the rail's Tasks badge is the read's own rule, and every page's rail reads it on each request."""

    def rows(self, *dues, status="ready", parked=None):
        return [{"status": status, "parked": parked, "due": due} for due in dues]

    def test_overdue_is_warning_and_due_within_two_days_is_caution(self):
        rule = lambda rows: tasks_screen._attention(rows, now=NOW)  # noqa: E731
        # NOW is 2026-09-06 at noon UTC, the same calendar day in every zone this runs in.
        self.assertEqual(rule(self.rows("2026-09-05", "2026-09-06", "2026-09-08", "2026-09-09", None)),
                         {"state": "warning", "n": 1, "what": "overdue tasks"})
        self.assertEqual(rule(self.rows("2026-09-06", "2026-09-08", "2026-09-09")),
                         {"state": "caution", "n": 2, "what": "tasks due within 2 days"})
        self.assertEqual(rule(self.rows("2026-09-01", status="done") + self.rows("2026-09-01", parked="2026-09-02T02:00:00Z")),
                         {"state": "ok", "n": 0, "what": "nothing due"})

    def test_every_page_shows_the_tasks_badge_read_now(self):
        self.item("Pay the bill", kind="task", due="2026-01-02")
        sections = lambda: self.request("/ui/sections.js", headers={"Cookie": self.cookie})[2]  # noqa: E731
        badge = lambda: json.loads(re.search(r"^window\.SHELL_COUNTS = (.*);$", sections(), re.M).group(1))["pages"]["Tasks"]  # noqa: E731
        self.assertEqual(badge(), {"state": "warning", "n": 1, "what": "overdue tasks"})
        self.item("Renew the domain", kind="task", due="2026-01-03")
        self.assertEqual(badge()["n"], 2)
