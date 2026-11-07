"""The v2 Briefs page (sd:2112, sd:3459).

What this slice promises: `/briefs` answers under the shared policy and loads
its script before `shell.js`; `/api/briefs` needs a session, takes no query,
and lists the routine briefs the workflow database holds, read through the
pack's `routine_briefs.rows`, capped in rows, with every row it could not read
counted and the watchdog said to be unread. A failed read is an error with its
cause, never an empty page. Each row carries its item id; Read opens that
item's page for every row, and Open in Obsidian only where the `brief-copies`
heartbeat vouches for the vault copy. The page claims no failure and no
follow-up, and sends nothing until Capture.

`briefs.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` uses, with the real reader (`read.js`, sd:2418). The browser half -- the look at 375 px, the
cadence lanes, focus -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from sd_dashboard import briefs_screen, review_screen, server, v2
from sd_db import create_item, review, routine_briefs, workflow
from support import NOW, ScreenCase
from test_run_actions import Run
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_shell_shared import cell_grammar
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_v2_watches import plugin
from test_workflow_actions import BrowserSession

TOPIC = plugin("topic", "Topics", {"active": "ready", "candidate": "planning", "parked": "blocked", "retired": "done"},
               {"active": ["parked", "retired"], "candidate": ["active", "parked", "retired"], "parked": ["active", "retired"],
                "retired": ["candidate"]},
               {"retire": "retired"})

V2 = Path(v2.__file__).resolve().parent
BRIEFS_JS = (V2 / "static" / "briefs.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
BRIEF_DETAILS_JS = (V2 / "static" / "brief-details.js").read_text(encoding="utf-8")
REPO = "/repos/system"
FOLDER = "Briefs/system"

#: The brief commands this port declares: id, object type, label, key, risk.
COMMANDS = [
    ["brief.read", "brief", "Read", "r", "safe"],
    ["brief.task", "brief", "Make task", "k", "safe"],
    ["brief.open", "brief", "Open in Obsidian", "o", "safe"],
    ["brief.adversarial-review", "brief", "Adversarial review", None, "safe"],
    # sd:5102: commands.js declares both for Briefs and Documents.
    ["brief.publish", "brief destination", "Publish to", None, "safe"],
    # sd:5489: a ticked destination unticks: ask first, and Undo ticks it again.
    ["brief.unpublish", "brief destination", "Remove copy", None, "undo"],
    # A designated destination with no copy: queue it again.
    ["brief.queue-copy", "brief", "Queue copy", None, "safe"],
    ["brief.publish-now", "brief", "Publish now", None, "safe"],
    # sd:6703: the Topics view's Add, and each topic row's Remove (retire) and Delete.
    ["topic.add", "topic list", "Add topic", "a", "safe"],
    ["topic.remove", "topic", "Remove", "m", "confirm"],
    ["topic.delete", "topic", "Delete", "x", "confirm"],
    ["topic.restore", "topic", "Restore", "e", "confirm"],
]

#: The briefs a small store holds: stem, when first filed, the body's first line.
SEEDS = [
    ("2026-09-06 - Intel Brief", "2026-09-06T09:00:10+00:00", "Three items worth reading today."),
    ("2026-09-06 - Fun Events", "2026-09-06T11:02:00+00:00", "First lines of the brief."),
    ("2026-09-04 - Intel Brief", "2026-09-04T09:01:00+00:00", "First lines of the brief."),
    # Filed after its day: the day stands and the time is not given.
    ("2026-09-03 - Weekly Digest", "2026-09-05T08:00:00+00:00", "First lines of the brief."),
    # Outside 7 days, inside 30.
    ("2026-08-20 - Intel Brief", "2026-08-20T09:00:00+00:00", "First lines of the brief."),
]
#: Every row's last change, then the render that wrote the copies; one brief changed after it.
UPDATED, RENDERED, LATER = "2026-09-06T11:30:00+00:00", "2026-09-06T11:45:00+00:00", "2026-09-06T11:50:00+00:00"
NO_COPY = "2026-09-04 - Intel Brief"


def beat(state="ok", stamp=RENDERED, **more):
    """A `brief-copies` heartbeat as `copies_state` returns it."""
    return (stamp, {"at": stamp, "ok": state != "failed", "state": state, "folder": {REPO: FOLDER}, "previous_folder": {},
                    "written": 4, "refused": [], "reason": None, **more})


def link(stem):
    return "obsidian://open?vault=Vault&file=" + urllib.parse.quote(f"{FOLDER}/{stem}")


class BriefCase(ScreenCase):
    """A store holding `SEEDS`, each last changed at `UPDATED` but `NO_COPY`, changed at `LATER`."""

    def setUp(self):
        super().setUp()
        self.repo(REPO)
        self.ids = {stem: routine_briefs.add(self.connection, stem, f"---\nseries: x\n---\n\n{lead}\n", repo=REPO,
                                             who="test", at=at)["item"] for stem, at, lead in SEEDS}
        # Seeding, as `ScreenCase.age` backdates history: the clock that wrote the rows is the real one.
        self.connection.execute("UPDATE item SET updated_at = CASE title WHEN ? THEN ? ELSE ? END WHERE source = 'brief'",
                                (NO_COPY, LATER, UPDATED))
        self.connection.commit()

    def doc(self, copies=beat, vault="Vault", **more):
        found = copies() if callable(copies) else copies
        return briefs_screen.document(self.connection, now=NOW, copies_reader=lambda _: found, vault=lambda: vault, **more)


class TheDocument(BriefCase):
    def test_rows_come_from_the_database_with_their_item_and_keep_their_day(self):
        doc = self.doc()
        self.assertEqual(doc["reader"], {"state": "read", "reason": "", "source": "workflow database", "total": 5, "shown": 5,
                                         "skipped": 0, "capped": False,
                                         "copies": {"state": "ok", "reason": "", "at": RENDERED}})
        self.assertEqual(doc["watchdog"], {"available": False, "reason": briefs_screen.WATCHDOG_REASON})
        self.assertEqual([b["id"] for b in doc["briefs"]],
                         ["2026-09-06 - Fun Events", "2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief",
                          "2026-09-03 - Weekly Digest", "2026-08-20 - Intel Brief"])
        self.assertEqual({b["id"]: b["item"] for b in doc["briefs"]}, self.ids)
        first = doc["briefs"][1]
        self.assertEqual((first["src"], first["day"], first["at"], first["words"]),
                         ("Intel Brief", "2026-09-06", "2026-09-06T09:00:10Z", 5))
        self.assertEqual(first["lead"], "Three items worth reading today.")
        digest = doc["briefs"][3]
        self.assertEqual((digest["day"], digest["at"], digest["src"]), ("2026-09-03", None, "Weekly Digest"))

    def test_a_series_is_its_stem_up_to_the_first_middle_dot(self):
        """sd:5064: each adversarial review is its own stem, so the target and time follow a ` · `; the page groups
        every review under one source, as it groups one job's briefs."""
        for stem in ("2026-09-06 - Adversarial review · task 12 · 101500Z",
                     "2026-09-06 - Adversarial review · repo system · 111500Z"):
            routine_briefs.add(self.connection, stem, "---\nreview: adversarial\n---\n\nNo recommendations.\n",
                               repo=REPO, who="test")
        found = [b for b in self.doc()["briefs"] if "Adversarial" in b["id"]]
        self.assertEqual([b["src"] for b in found], ["Adversarial review", "Adversarial review"])

    def test_open_needs_an_ok_render_newer_than_the_row_and_read_needs_nothing(self):
        doc = self.doc()
        by = {b["id"]: b for b in doc["briefs"]}
        self.assertEqual(by["2026-09-06 - Intel Brief"]["open"], link("2026-09-06 - Intel Brief"))
        self.assertEqual(by["2026-09-06 - Intel Brief"]["rel"], f"{FOLDER}/2026-09-06 - Intel Brief.md")
        self.assertEqual(by["2026-09-06 - Intel Brief"]["copy"], "")
        # Changed after the render: its copy may be older, so no link; the row and its item stay.
        late = by[NO_COPY]
        self.assertEqual((late["open"], late["rel"], late["item"]), (None, None, self.ids[NO_COPY]))
        self.assertIn("not newer than this brief", late["copy"])

    def test_each_heartbeat_withholds_open_unless_it_vouches_for_the_copy(self):
        stem = "2026-09-06 - Intel Brief"
        cases = {
            "none": (None, "unknown", "no brief render"),
            "failed": (beat("failed", reason="the vault is not a directory"), "failed", "the vault is not a directory"),
            "refused": (beat("failed", refused=[stem]), "failed", "1 copies refused"),
            "off": (beat("off", folder={REPO: None}), "off", "obsidian channel"),
            "older": (beat(stamp="2026-09-06T11:00:00+00:00"), "ok", ""),
            "other repo": (beat(folder={"/repos/other": FOLDER}), "ok", ""),
            "odd state": (beat("rendering"), "unknown", "rendering"),
        }
        for name, (found, state, why) in cases.items():
            with self.subTest(name):
                doc = self.doc(found)
                self.assertEqual(doc["reader"]["copies"]["state"], state)
                self.assertIn(why, doc["reader"]["copies"]["reason"])
                row = next(b for b in doc["briefs"] if b["id"] == stem)
                self.assertEqual((row["open"], row["rel"]), (None, None))
                self.assertTrue(row["copy"])
                self.assertEqual(len(doc["briefs"]), 5, "a heartbeat never hides a row")
        # An ok render that lists the stem as refused, and no vault name: no link either way.
        row = next(b for b in self.doc(beat(refused=[stem]))["briefs"] if b["id"] == stem)
        self.assertEqual(row["open"], None)
        self.assertIn("refused", row["copy"])
        row = next(b for b in self.doc(vault=None)["briefs"] if b["id"] == stem)
        self.assertEqual(row["open"], None)

    def test_a_heartbeat_that_cannot_be_read_is_unknown_and_the_rows_stay(self):
        def refuse(_):
            raise sqlite3.OperationalError("database is locked")
        doc = briefs_screen.document(self.connection, now=NOW, copies_reader=refuse, vault=lambda: "Vault")
        self.assertEqual(doc["reader"]["copies"]["state"], "unknown")
        self.assertIn("database is locked", doc["reader"]["copies"]["reason"])
        self.assertEqual((doc["reader"]["state"], len(doc["briefs"])), ("read", 5))
        self.assertTrue(all(b["open"] is None for b in doc["briefs"]))

    def test_a_reader_that_fails_is_an_error_with_its_cause_never_no_rows(self):
        # Failure table, "page: read rows": a raising reader is state error with the cause, never zero briefs.
        def refuse(_):
            raise sqlite3.OperationalError("database is locked")
        doc = self.doc(reader=refuse)
        self.assertEqual((doc["reader"]["state"], doc["briefs"]), ("error", []))
        self.assertIn("database is locked", doc["reader"]["reason"])
        self.assertEqual(doc["reader"]["source"], "workflow database")

    def test_a_library_without_routine_briefs_is_an_error_naming_it(self):
        with mock.patch.dict(sys.modules, {"sd_db.routine_briefs": None}):
            doc = briefs_screen.document(self.connection, now=NOW)
        self.assertEqual((doc["reader"]["state"], doc["briefs"]), ("error", []))
        self.assertIn("no routine_briefs", doc["reader"]["reason"])
        self.assertEqual(doc["reader"]["copies"]["state"], "unknown")

    def test_a_row_that_is_not_a_brief_is_counted_and_skipped(self):
        good = routine_briefs.rows(self.connection)
        one = good[0]
        bad = [None, {"title": "x"}, {**one, "brief": {**one["brief"], "at": "yesterday"}},
               {**one, "brief": {**one["brief"], "at": "2026-09-06T09:00:00"}},  # no zone: not an instant
               {**one, "brief": {**one["brief"], "words": True}}, {**one, "brief": {**one["brief"], "day": "2026-02-30"}},
               {**one, "id": "7"}, dict(one)]
        doc = self.doc(reader=lambda _: good + bad)
        self.assertEqual((doc["reader"]["state"], doc["reader"]["skipped"], len(doc["briefs"])), ("partial", 8, 5))

    def test_the_page_takes_at_most_its_row_cap_and_cuts_each_lead(self):
        one = routine_briefs.rows(self.connection)[0]
        many = [{**one, "id": 1000 + i, "title": f"2026-09-01 - K{i:03d}",
                 "brief": {**one["brief"], "series": f"K{i:03d}", "day": "2026-09-01", "lead": "y" * 500}}
                for i in range(briefs_screen.ROWS + 1)]
        doc = self.doc(reader=lambda _: many)
        self.assertEqual(len(doc["briefs"]), briefs_screen.ROWS)
        self.assertEqual((doc["reader"]["capped"], doc["reader"]["total"], doc["reader"]["shown"]), (True, None, briefs_screen.ROWS))
        self.assertEqual(len(doc["briefs"][0]["lead"]), briefs_screen.LEAD)
        self.assertFalse(self.doc()["reader"]["capped"])

    def test_a_real_render_vouches_for_its_copies_and_no_local_path_is_sent(self):
        vault = Path(self.tmp.name) / "Vault"
        vault.mkdir()
        self.connection.execute("UPDATE item SET updated_at = '2026-01-01T00:00:00+00:00' WHERE source = 'brief'")
        self.connection.commit()
        self.assertEqual(routine_briefs.render(self.connection, vault)["state"], "ok")
        with mock.patch.dict(os.environ, {"OBSIDIAN_VAULT": str(vault)}):
            doc = briefs_screen.document(self.connection, now=NOW)
        self.assertEqual(doc["reader"]["copies"]["state"], "ok")
        for row in doc["briefs"]:
            self.assertTrue((vault / row["rel"]).is_file(), row["rel"])
            self.assertEqual(row["open"], link(row["id"]))
        self.assertNotIn(self.tmp.name, json.dumps(doc))
        self.assertNotIn(str(Path(self.tmp.name).resolve()), json.dumps(doc))


#: Topic rows (system sd:5778): title, stage, slug, body. Memory is newest first; a topic may lack either section.
TOPICS = [
    ("Graphs", "active", "graphs", "## Covers\n\nGraphs.\n\n## Memory\n\n- 2026-09-06 a running story · 2026-09-06 - Intel Brief\n"
     "- 2026-09-04 an older fact · 2026-09-04 - Intel Brief\n\n## Sources\n\n- arxiv · last signal 2026-09-06\n- a blog · dead since 2026-08-01\n"),
    ("Climate", "parked", "climate", "## Covers\n\nClimate.\n"),
    ("Agents", "candidate", "agents", "## Memory\n\n## Sources\n\n- a feed · last signal 2026-09-01\n"),
]


def seed_topics(connection):
    """The `TOPICS` rows as `sdw.topic` base rows, by title."""
    return {title: create_item(connection, kind="idea", title=title, status="ready", source="vault",
                               external_id=f"topic:Topics/{title}.md", path=f"Topics/{title}.md", stage=stage,
                               fields={"kind": "topic", "slug": slug}, body={"markdown": body})
            for title, stage, slug, body in TOPICS}


def record_topics(connection, found):
    """Each named brief's `fields.brief.signal_topics`, as pack sd:5798 will copy them."""
    for stem, value in found.items():
        connection.execute("UPDATE item SET fields = json_set(fields, '$.brief.signal_topics', json(?)) WHERE title = ?",
                           (json.dumps(value), stem))
    connection.commit()


class TheTopics(BriefCase):
    """system sd:5778: the Topics view's rows, read from the `sdw.topic` rows, and each brief's signal topics."""

    def setUp(self):
        super().setUp()
        self.topics = seed_topics(self.connection)

    def test_topics_come_from_the_topic_rows_active_first_with_their_newest_memory_and_source_count(self):
        create_item(self.connection, kind="idea", title="Not a topic", status="planning", source="vault",
                    fields={"kind": "blog-idea"}, body={"markdown": "## Memory\n\n- x\n"})
        self.assertEqual(self.doc()["topics"], {"state": "read", "reason": "", "rows": [
            {"item": self.topics["Graphs"], "title": "Graphs", "slug": "graphs", "stage": "active",
             "memory": "2026-09-06 a running story · 2026-09-06 - Intel Brief", "memory_lines": 2, "sources": 2},
            {"item": self.topics["Agents"], "title": "Agents", "slug": "agents", "stage": "candidate",
             "memory": None, "memory_lines": 0, "sources": 1},
            {"item": self.topics["Climate"], "title": "Climate", "slug": "climate", "stage": "parked",
             "memory": None, "memory_lines": None, "sources": None}]})

    def test_a_section_ends_at_any_heading_and_a_closing_hash_run_still_names_it(self):
        """Review: `## Memory ##` is the Memory heading, and a later `# ` heading is not Memory's content."""
        create_item(self.connection, kind="idea", title="Edges", status="ready", source="vault", stage="active",
                    fields={"kind": "topic", "slug": "edges"},
                    body={"markdown": "## Memory ##\n\n- kept\n\n# Appendix\n\n- not memory\n"})
        row = next(r for r in self.doc()["topics"]["rows"] if r["title"] == "Edges")
        self.assertEqual((row["memory"], row["memory_lines"], row["sources"]), ("kept", 1, None))

    def test_a_topic_read_that_fails_is_an_error_with_its_cause_and_the_briefs_stay(self):
        def refuse(_):
            raise sqlite3.OperationalError("database is locked")
        doc = self.doc(topic_reader=refuse)
        self.assertEqual((doc["topics"]["state"], doc["topics"]["rows"]), ("error", []))
        self.assertIn("database is locked", doc["topics"]["reason"])
        self.assertEqual(len(doc["briefs"]), 5)

        def broken(_):
            raise sqlite3.OperationalError("disk I/O error")
        self.assertEqual(self.doc(reader=broken)["topics"]["state"], "read")

    def test_a_brief_carries_its_signal_topics_only_when_its_record_holds_a_list_of_slugs(self):
        """Pack sd:5798 copies `signal-topics` into `fields.brief.signal_topics`; until then no brief records them."""
        self.assertEqual({b["topics"] for b in self.doc()["briefs"]}, {None})
        record_topics(self.connection, {"2026-09-06 - Intel Brief": ["graphs", "agents"], "2026-09-04 - Intel Brief": "graphs",
                                        "2026-09-06 - Fun Events": [], "2026-09-03 - Weekly Digest": ["graphs", 7]})
        got = {b["id"]: b["topics"] for b in self.doc()["briefs"]}
        self.assertEqual([got[stem] for stem in ("2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief",
                                                 "2026-09-06 - Fun Events", "2026-09-03 - Weekly Digest")],
                         [["graphs", "agents"], None, [], None])


class TheTopicActions(Run):
    """sd:6703: Add topic files a candidate, Remove retires a topic, Delete removes its row, through /api/actions/run."""

    def setUp(self):
        super().setUp()
        self.topics = seed_topics(self.connection)
        self.connection.commit()
        vault = mock.patch.dict(os.environ, {"OBSIDIAN_VAULT": str(Path(self.tmp.name).resolve() / "vault")})
        vault.start()
        self.addCleanup(vault.stop)

    def test_add_files_a_candidate_topic_with_its_slug(self):
        status, body = self.run_("topic.add", title="Edge AI: chips")
        self.assertEqual((status, body["exit"], body["label"]), (200, 0, "Add topic"), body)
        [line] = self.ran()
        self.assertTrue(line.startswith("sd store add sdw.topic Edge AI chips --field slug=edge-ai-chips --field dateCreated="), line)
        status, body = self.run_("topic.add", title="  ")
        self.assertEqual(status, 400)
        self.assertIn("Name the topic", body["error"])

    def test_remove_retires_the_topic_and_keeps_its_memory(self):
        item = self.topics["Graphs"]
        with mock.patch("sd_db.records.plugin_entries", return_value=[TOPIC]):
            status, body = self.run_("topic.remove", item=item)
            self.assertEqual((status, body["exit"], body["label"]), (200, 0, "Remove topic"), body)
            stage, kept = self.connection.execute("SELECT stage, body FROM item WHERE id=?", (item,)).fetchone()
            self.assertEqual(stage, "retired")
            self.assertIn("a running story", kept)
            status, body = self.run_("topic.remove", item=item)
            self.assertEqual(status, 400)
            self.assertIn("is retired already", body["error"])

    def test_restore_brings_a_retired_topic_back_to_candidate(self):
        """sd:6703: Restore undoes Remove: retired goes back to candidate (sd-writing sd:6828), Memory kept."""
        item = self.topics["Graphs"]
        with mock.patch("sd_db.records.plugin_entries", return_value=[TOPIC]):
            status, body = self.run_("topic.restore", item=item)
            self.assertEqual(status, 400)
            self.assertIn("not retired; nothing ran", body["error"])
            self.assertEqual(self.run_("topic.remove", item=item)[1]["exit"], 0)
            status, body = self.run_("topic.restore", item=item)
            self.assertEqual((status, body["exit"], body["label"]), (200, 0, "Restore topic"), body)
            stage, kept = self.connection.execute("SELECT stage, body FROM item WHERE id=?", (item,)).fetchone()
            self.assertEqual(stage, "candidate")
            self.assertIn("a running story", kept)

    def test_delete_removes_the_topic_row_through_item_remove(self):
        same = mock.patch("sd_db.database.default_path", return_value=Path(self.path))
        same.start()
        self.addCleanup(same.stop)
        self.stub("sd-db", 'case "$*" in *--apply*) echo "sd-db: removed 1 item";; *) echo "sd-db: fingerprint: ff00";; esac')
        status, body = self.run_("topic.delete", item=self.topics["Climate"])
        self.assertEqual((status, body["exit"], body["label"]), (200, 0, "Delete topic"), body)
        self.assertTrue(self.ran()[-1].endswith("sdw.topic Climate --apply --if-fingerprint ff00"), self.ran())
        status, body = self.run_("topic.delete", item=999999)
        self.assertEqual(status, 400)
        self.assertIn("is not a topic row", body["error"])

    def test_delete_refuses_when_sd_db_would_open_another_database(self):
        """Review of build/sd-206: Delete is bound to the database the dashboard read the row from."""
        self.stub("sd-db", 'echo "sd-db: fingerprint: ff00"')
        other = Path(self.tmp.name).resolve() / "other" / "workflow.sqlite3"
        with mock.patch("sd_db.database.default_path", return_value=other):
            status, body = self.run_("topic.delete", item=self.topics["Climate"])
        self.assertEqual(status, 400)
        self.assertIn("another database", body["error"])
        self.assertEqual(self.ran(), [])


class TheItemPage(BriefCase):
    """Read's target: `/item/<id>` renders the brief's Markdown, and offers no report control and no Fix."""

    def test_the_item_page_renders_the_markdown_and_no_report_source(self):
        page = self.render(f"/item/{self.ids[NO_COPY]}")
        self.assertIn("<p>First lines of the brief.</p>", page)
        # Its frontmatter is not text: stripped as a piece's draft's is.
        self.assertNotIn("series: x", page)
        self.assertNotIn("Report source", page)
        self.assertNotIn("acknowledge", page.lower())

    def test_an_obsidian_link_in_a_brief_is_a_link_and_javascript_stays_text(self):
        # REGRESSION (sd:5000). The page showed a brief's vault links as
        # source text; Today's and Briefs' Read both open this page.
        url = link("2026-09-06 - Intel Brief")
        item = routine_briefs.add(self.connection, "2026-09-07 - Intel Brief",
                                  f"---\nseries: x\n---\n\nSee [yesterday]({url}) and [this](javascript:alert(1)).\n",
                                  repo=REPO, who="test", at="2026-09-07T09:00:00+00:00")["item"]
        self.connection.commit()
        page = self.render(f"/item/{item}")
        hrefs = re.findall(r'<a href="([^"]*)"', page)
        self.assertIn(url.replace("&", "&amp;"), hrefs)
        self.assertFalse([h for h in hrefs if h.lower().startswith("javascript")], hrefs)
        self.assertIn("[this](javascript:alert(1))", page)

    def test_report_controls_draw_nothing_for_a_brief(self):
        from sd_dashboard import reports_screen
        from sd_db import workflow

        state = workflow.item_state(self.connection, self.ids[NO_COPY])
        self.assertEqual(reports_screen.report_controls(self.connection, state["item"], state["revision"]), "")

    def test_fix_starts_no_session_for_a_brief_even_when_open(self):
        # A brief stays done; were one moved, Fix still runs no herdr launch and writes nothing.
        from sd_dashboard import fix_session
        from sd_db import workflow

        item = self.ids[NO_COPY]
        self.connection.execute("UPDATE item SET status = 'planning' WHERE id = ?", (item,))
        self.connection.commit()
        revision = workflow.item_state(self.connection, item)["revision"]
        with mock.patch.object(fix_session, "checkout", lambda row: Path(self.tmp.name)), \
                mock.patch.object(fix_session.subprocess, "run") as launched, \
                self.assertRaisesRegex(workflow.WorkflowError, "routine brief"):
            fix_session.start(self.connection, item, expected_revision=revision, who="dashboard")
        launched.assert_not_called()


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/briefs")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Briefs · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "commands.js", "brief-details.js", "briefs.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)
        self.assertNotIn("resource=briefs", body)

    def test_the_rail_opens_the_new_page_and_the_resources_view_is_gone(self):
        from sd_dashboard import reports_screen

        self.assertEqual(v2.SECTIONS.get("Briefs"), "/briefs")
        self.assertNotIn("Briefs", v2.CLASSIC)
        self.assertNotIn("briefs", dict(reports_screen.VIEWS))
        self.assertNotIn("briefs", reports_screen.VIEW_SECONDS)

    def test_the_data_route_needs_a_session_takes_no_query_and_reads_the_store(self):
        self.repo(REPO)
        routine_briefs.add(self.connection, SEEDS[0][0], "---\nseries: x\n---\n\nBody.\n", repo=REPO, who="test", at=SEEDS[0][1])
        self.assertEqual(self.request("/api/briefs")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/briefs?range=30d", headers=cookie)[0], 400)
        status, _, body = self.request("/api/briefs", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual([b["id"] for b in doc["briefs"]], [SEEDS[0][0]])
        self.assertEqual(doc["reader"]["copies"]["state"], "unknown")
        self.assertFalse(doc["watchdog"]["available"])

    def test_the_recommendations_route_needs_a_session_and_reads_the_brief(self):
        self.repo(REPO)
        brief, _ = adversarial(self)
        self.assertEqual(self.request(f"/api/briefs/{brief}/recommendations")[0], 403)
        status, _, body = self.request(f"/api/briefs/{brief}/recommendations", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(json.loads(body)["questions"]), 3)


# The stand-in additions briefs.js needs beyond test_v2_tasks: the shell's row, url, capture, chat, and window.open.
SHELL_MORE = r"""
URLSearchParams = function (q) { var m = []; (q || '').replace(/^\?/, '').split('&').filter(Boolean).forEach(p => { var kv = p.split('='); m.push([kv[0], decodeURIComponent(kv[1] || '')]); });
  this.get = k => { var e = m.find(x => x[0] === k); return e ? e[1] : null; }; this.getAll = k => m.filter(x => x[0] === k).map(x => x[1]); };
OUT.captures = []; OUT.opened = []; OUT.chat = []; OUT.urls = []; OUT.panes = [];
window.shell.row = () => ROW; window.shell.url = q => OUT.urls.push(q); window.shell.capture = (o, t) => OUT.captures.push([o && o.id, t]);
window.shell.openChat = () => {}; window.shell.send = t => OUT.chat.push(t); window.shell.openPane = p => OUT.panes.push(p);
window.open = (u, t, f) => OUT.opened.push([u, t, f]);
OUT.shown = []; window.shell.view = { show: id => { OUT.shown.push(id); return true; }, current: () => 'list', count: (id, n) => { (OUT.counts = OUT.counts || {})[id] = n; } };
"""


class TheScript(BriefCase):
    """briefs.js against the document `briefs_screen` builds from the seeded store, read at NOW."""

    def setUp(self):
        super().setUp()
        self.page_doc = self.doc()

    def run_page(self, body, answer=None, search="", row=None):
        answer = answer or f"(p) => p === '/api/items' ? [201, {{ item: {{ id: 77, title: 'Read it' }} }}] : [200, {json.dumps(self.page_doc)}]"
        script = (STAND_IN + f"location.search = {json.dumps(search)}; var ROW = {json.dumps(row)};\n" + MARKUP_JS
                  + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + READ_SHELL + f"\nANSWER = {answer};\n" + COMMANDS_JS + BRIEF_DETAILS_JS + BRIEFS_JS
                  + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.attention = window.PAGE_ATTENTION;"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("""R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]);
R.primary = REG.filter(c => c.primary && c.primary({})).map(c => c.id); R.gated = REG.filter(c => c.when).map(c => c.id);""")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        # Read is every row's primary action and is never off, so a row without a vault copy offers it too (the shell draws
        # the primary in each row: the browser check on the pull request).
        self.assertEqual((out["R"]["primary"], out["R"]["gated"]), (["brief.read"], ["brief.open", "brief.adversarial-review", "brief.publish", "brief.unpublish", "brief.queue-copy", "brief.publish-now",
                                                                         "topic.remove", "topic.restore"]))

    def test_adversarial_review_on_a_brief_posts_its_item(self):
        """sd:5064: a brief row asks for {kind: brief, target: <its item>}; the run starts in the background."""
        stem = "2026-09-06 - Intel Brief"
        answer = f"""(path, body) => path === '/api/adversarial' ? [202, {{ id: 'r1', state: 'queued', item: null, filed: null }}]
  : path === '/api/adversarial/r1' ? [200, {{ id: 'r1', state: 'failed', item: null, reason: 'codex exited 1', filed: null }}]
  : [200, {json.dumps(self.page_doc)}]"""
        out = self.run_page(f"shellRun(cmd('brief.adversarial-review'), C.get({json.dumps(stem)})); await flush();", answer)
        self.assertEqual(out["posts"], [["/api/adversarial", {"kind": "brief", "target": self.ids[stem]}, 64]])
        self.assertEqual([t[0] for t in out["toasts"]],
                         ["Review started · run r1", "Adversarial review failed · run r1: codex exited 1"])

    def recommend(self):
        """An adversarial brief with three recommendations, the first approved and filed; the page selects it."""
        brief, batch = adversarial(self)
        notes = [n["id"] for n in workflow.item_state(self.connection, batch)["notes"] if n["kind"] == "question"]
        revision = workflow.item_state(self.connection, batch)["revision"]
        review_screen.rule_request({"item": batch, "note": notes[0], "option": "Approve", "revision": revision})(self.connection)
        self.page_doc = self.doc()
        recs = review_screen.recommendations(self.connection, brief)
        stem = next(b["id"] for b in self.page_doc["briefs"] if b["item"] == brief)
        return brief, batch, notes, recs, stem

    def pick(self, body, refuse=None):
        """Run `body` on the selected adversarial brief, with NOTES its question notes; a rule on NOTES[refuse] answers 409."""
        brief, batch, notes, recs, stem = self.recommend()
        refused = None if refuse is None else notes[refuse]
        answer = f"""(path, body) => path === '/api/briefs/{brief}/recommendations' ? [200, {json.dumps(recs)}]
  : path === '/api/review/rule' ? (body.note === {json.dumps(refused)} ? [409, {{ error: 'note ' + body.note + ' is settled: ruled by note #9' }}]
    : [200, {{ item: body.item, note: body.note, revision: String(body.note % 10).repeat(64), ruling: 900 + body.note }}])
  : [200, {json.dumps(self.page_doc)}]"""
        prelude = f"""const NOTES = {json.dumps(notes)};
const box = (n, on) => ({{ target: {{ closest: s => s === 'input[data-q]' ? {{ checked: on, dataset: {{ q: String(n) }} }} : null }} }});
const act = a => ({{ target: {{ closest: s => s === '[data-recs]' ? {{ dataset: {{ recs: a }} }} : null }} }});
const fire = (t, e) => (ELS.details.listeners[t] || []).forEach(f => f(e));
await flush();\n"""
        return self.run_page(prelude + body, answer, row=stem), batch, notes, recs

    def test_an_adversarial_brief_lists_its_recommendations_to_pick(self):
        out, batch, notes, recs = self.pick("R.recs = ELS.recs.html; R.det = ELS.details.html;")
        recs_html, filed = out["R"]["recs"], recs["questions"][0]["filed"]
        self.assertIn("<h3>Create tasks</h3>", out["R"]["det"])
        # Open questions each have a box; the approved one shows its label and links its filed task.
        self.assertEqual(re.findall(r'<input type="checkbox" data-q="(\d+)"', recs_html), [str(n) for n in notes[1:]])
        self.assertIn("Fix hypothesis 2 of task 12", recs_html)
        self.assertIn("system", recs_html)
        self.assertRegex(recs_html, rf'Approve</span> <span>Fix hypothesis 1 of task 12</span>.*<a href="/tasks\?row={filed}">sd:{filed}</a>')
        for label in ("Select all", "Create selected", "Reject the rest"):
            self.assertIn(f">{label}</button>", recs_html)
        self.assertEqual(out["posts"], [])

    def test_create_selected_rules_only_the_chosen_question_approve_under_its_batch_revision(self):
        out, batch, notes, recs = self.pick("""fire('change', box(NOTES[1], true)); fire('click', act('create')); await flush();
await flush(); R.recs = ELS.recs.html;""")
        self.assertEqual(out["posts"], [["/api/review/rule", {"item": batch, "note": notes[1], "option": "Approve",
                                                              "revision": recs["batches"][0]["revision"]}, 64]])
        # The page reads the recommendations again, so a filed task shows as its link; the unchosen one stays open.
        self.assertEqual(out["gets"].count(f"/api/briefs/{recs['brief']}/recommendations"), 2)
        self.assertIn("1 of 1 ruled Approve", " ".join(t[0] for t in out["toasts"]))

    def test_a_box_unticked_again_is_not_created_and_nothing_chosen_sends_nothing(self):
        out, _, _, _ = self.pick("""fire('change', box(NOTES[1], true)); fire('change', box(NOTES[1], false));
fire('click', act('create')); await flush();""")
        self.assertEqual(out["posts"], [])
        self.assertIn("Tick a recommendation first", " ".join(t[0] for t in out["toasts"]))

    def test_select_all_then_create_carries_each_answers_revision(self):
        out, batch, notes, recs = self.pick("fire('click', act('all')); fire('click', act('create')); await flush(); await flush();")
        first = recs["batches"][0]["revision"]
        self.assertEqual([(p[1]["note"], p[1]["option"], p[1]["revision"]) for p in out["posts"]],
                         [(notes[1], "Approve", first), (notes[2], "Approve", str(notes[1] % 10) * 64)])

    def test_reject_the_rest_rules_every_open_question_reject_and_a_failure_stays_on_its_row(self):
        out, batch, notes, recs = self.pick("fire('click', act('reject')); await flush(); await flush(); R.recs = ELS.recs.html;",
                                            refuse=1)
        self.assertEqual([(p[1]["note"], p[1]["option"]) for p in out["posts"]], [(notes[1], "Reject"), (notes[2], "Reject")])
        self.assertIn(f"note {notes[1]} is settled: ruled by note #9", out["R"]["recs"])
        self.assertIn("1 of 2 ruled Reject; 1 failed", " ".join(t[0] for t in out["toasts"]))

    def two(self):
        """Two adversarial briefs, A (task 12) and B (task 13), each with three open recommendations; the page shows A."""
        a, a_batch = adversarial(self)
        b, b_batch = adversarial(self, task=13)
        self.page_doc = self.doc()
        stem = {item: next(r["id"] for r in self.page_doc["briefs"] if r["item"] == item) for item in (a, b)}
        notes = {batch: [n["id"] for n in workflow.item_state(self.connection, batch)["notes"] if n["kind"] == "question"]
                 for batch in (a_batch, b_batch)}
        recs = {item: review_screen.recommendations(self.connection, item) for item in (a, b)}
        return (a, b), stem, notes[a_batch], notes[b_batch], recs

    def held(self, recs, a, hold_a, hold_rule):
        """An answer where A's recommendations (or each rule post) wait for RELEASE(), so they arrive after B's. It is
        made before the page loads, so the first read of A waits too."""
        return f"""(() => {{ globalThis.HELD = new Promise(r => {{ globalThis.RELEASE = r; }});
const FIX = {json.dumps({str(k): v for k, v in recs.items()})};
return (path, body) => path === '/api/briefs/{a}/recommendations' && {json.dumps(hold_a)} ? HELD.then(() => [200, FIX[{a}]])
  : path.startsWith('/api/briefs/') && path.endsWith('/recommendations') ? [200, FIX[+path.split('/')[3]]]
  : path === '/api/review/rule' ? ({json.dumps(hold_rule)} ? HELD : Promise.resolve()).then(() => [200, {{ item: body.item, note: body.note, revision: 'e'.repeat(64), ruling: 900 }}])
  : [200, {json.dumps(self.page_doc)}]; }})()"""

    def run_two(self, body, hold_a=True, hold_rule=False):
        (a, b), stem, a_notes, b_notes, recs = self.two()
        prelude = f"""const act = x => ({{ target: {{ closest: s => s === '[data-recs]' ? {{ dataset: {{ recs: x }} }} : null }} }});
const box = (n, on) => ({{ target: {{ closest: s => s === 'input[data-q]' ? {{ checked: on, dataset: {{ q: String(n) }} }} : null }} }});
const fire = (t, e) => (ELS.details.listeners[t] || []).forEach(f => f(e));
const A = {json.dumps(stem[a])}, B = {json.dumps(stem[b])}, A_NOTES = {json.dumps(a_notes)}, B_NOTES = {json.dumps(b_notes)};
await flush();\n"""
        out = self.run_page(prelude + body, self.held(recs, a, hold_a, hold_rule), row=stem[a])
        return out, (a, b), a_notes, b_notes

    def test_a_late_answer_for_a_brief_no_longer_shown_changes_nothing(self):
        """Lane review on 16cad7b5: A's recommendations arrived after B was selected, so B's buttons ruled A's questions."""
        out, (a, b), a_notes, b_notes = self.run_two("""window.PAGE_LIST.select(B); await flush(); RELEASE(); await flush(); await flush();
R.recs = ELS.recs.html; fire('click', act('reject')); await flush(); await flush();""")
        self.assertIn("Fix hypothesis 1 of task 13", out["R"]["recs"])
        self.assertNotIn("of task 12", out["R"]["recs"])
        self.assertEqual(sorted(p[1]["note"] for p in out["posts"]), sorted(b_notes))

    def test_the_read_after_create_selected_changes_nothing_once_another_brief_is_shown(self):
        out, (a, b), a_notes, b_notes = self.run_two("""fire('change', box(A_NOTES[0], true)); fire('click', act('create')); await flush();
window.PAGE_LIST.select(B); await flush(); RELEASE(); await flush(); await flush(); R.recs = ELS.recs.html;
fire('click', act('reject')); await flush(); await flush();""", hold_a=False, hold_rule=True)
        self.assertEqual(out["posts"][0][1]["note"], a_notes[0])
        self.assertIn("Fix hypothesis 1 of task 13", out["R"]["recs"])
        self.assertNotIn("of task 12", out["R"]["recs"])
        # A's refresh is not asked once B is shown, and B's buttons rule B's questions only.
        self.assertEqual(out["gets"].count(f"/api/briefs/{a}/recommendations"), 1)
        self.assertEqual(sorted(p[1]["note"] for p in out["posts"][1:]), sorted(b_notes))

    def test_a_brief_from_another_series_has_no_create_tasks(self):
        out = self.run_page("await flush(); R.det = ELS.details.html;", row="2026-09-06 - Intel Brief")
        self.assertNotIn("Create tasks", out["R"]["det"])
        self.assertFalse(any("recommendations" in g for g in out["gets"]))

    def test_the_rows_in_range_are_drawn_each_with_read_and_nothing_claims_a_failure(self):
        out = self.run_page("R.rows = ELS.rows.html; R.ann = ELS.annunciator.html; R.lanes = ELS.lanes.html; R.sub = ELS.sub.textContent;")
        rows = out["R"]["rows"]
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', rows),
                         ["2026-09-06 - Fun Events", "2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief", "2026-09-03 - Weekly Digest"])
        self.assertIn("Three items worth reading today.", rows)
        self.assertIn('<time datetime="2026-09-06T09:00:10Z"', rows)
        self.assertIn("09-03 –", rows)
        self.assertNotRegex(rows, r'class="unread"|class="dot"|▲|■')
        ann = out["R"]["ann"]
        self.assertNotRegex(ann + out["R"]["lanes"], r'data-state="(caution|warning)"|class="fail"')
        self.assertRegex(ann, r'title="' + re.escape(briefs_screen.WATCHDOG_REASON) + '" data-state="unknown"')
        self.assertIn("<b>4</b> in 7 days", ann)
        self.assertEqual(cell_grammar(self, ann), [("div", "ok"), ("div", "unknown"), ("div", "ok")])
        self.assertIn("Vault copies", ann)
        self.assertEqual(len(re.findall(r'class="lane-btn"', out["R"]["lanes"])), 3)
        self.assertEqual(out["R"]["sub"], "All read")
        self.assertEqual(out["states"][0]["kind"], "loading")
        self.assertIsNone(out["states"][-1])
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "watchdog not read"})

    def test_read_opens_the_item_page_for_a_row_with_and_without_a_vault_copy(self):
        out = self.run_page(f"""shellRun(cmd('brief.read'), C.get({json.dumps(NO_COPY)})); R.one = location.href;
shellRun(cmd('brief.read'), C.get('2026-09-06 - Intel Brief')); R.two = location.href;
R.cli = cmd('brief.read').cli(C.get({json.dumps(NO_COPY)})); R.exec = cmd('brief.read').executes;
R.det = ELS.details.html;""", row=NO_COPY)
        self.assertEqual((out["R"]["one"], out["R"]["two"]),
                         (f"/item/{self.ids[NO_COPY]}", f"/item/{self.ids['2026-09-06 - Intel Brief']}"))
        self.assertEqual(out["R"]["cli"], f"sd reports brief show '{NO_COPY}'")
        self.assertFalse(out["R"]["exec"])
        self.assertEqual(out["posts"], [])
        # Details says why there is no copy.
        self.assertIn("<dt>Vault copy</dt><dd>the last render is not newer than this brief", out["R"]["det"])

    def test_the_vault_copies_lamp_says_what_the_heartbeat_says(self):
        # sd:5298: a failed or unknown lamp is a link to its evidence, the Health row that lists the refused copies.
        for found, state, text in ((beat("failed", reason="the vault is not a directory"), "caution", "failed"),
                                   (beat("off", folder={REPO: None}), "ok", "off"), (None, "unknown", "unknown")):
            with self.subTest(text):
                doc = self.doc(found)
                ann = self.run_page("R.ann = ELS.annunciator.html;", answer=f"() => [200, {json.dumps(doc)}]")["R"]["ann"]
                lamps = cell_grammar(self, ann)
                self.assertEqual(lamps[2], ("div" if state == "ok" else "a", state))
                self.assertIn(f"<b>{text}</b>", ann)
                link = re.search(r'<a class="cell[^"]*" href="([^"]+)"[^>]*>(?:(?!</a>).)*Vault copies', ann, re.S)
                self.assertEqual(link and link.group(1), None if state == "ok" else "/health?row=mir%3Abriefs#view=jobs")

    def test_the_range_and_the_address_choose_the_rows(self):
        out = self.run_page("R.rows = ELS.rows.html;", search="?range=30d&src=Intel%20Brief")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]),
                         ["2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief", "2026-08-20 - Intel Brief"])
        out = self.run_page("R.rows = ELS.rows.html;", search="?range=24h")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["2026-09-06 - Fun Events", "2026-09-06 - Intel Brief"])

    def test_a_source_with_a_comma_survives_the_address(self):
        # Review 39d6c524a35c: a source is the free end of a stem, so the address repeats `src` rather than joining.
        routine_briefs.add(self.connection, "2026-09-05 - Daily, Special", "---\nseries: x\n---\n\nBody.\n", repo=REPO,
                           who="test", at="2026-09-05T07:00:00+00:00")
        out = self.run_page("R.rows = ELS.rows.html;", answer=f"() => [200, {json.dumps(self.doc())}]",
                            search="?src=Daily%2C%20Special&src=Fun%20Events")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]),
                         ["2026-09-06 - Fun Events", "2026-09-05 - Daily, Special"])
        self.assertEqual(out["urls"][-1], [["src", "Daily, Special"], ["src", "Fun Events"]])

    def test_source_is_one_multiselect_dropdown_and_day_one_select(self):
        """sd:5158: Source is a menu of checkboxes, two picks show their union; Day is a select of the busy days."""
        box = "const box = (v, on) => ELS.filters.listeners.change[0]({ currentTarget: ELS.filters, target: { dataset: { pick: 'src' }, type: 'checkbox', value: v, checked: on, getAttribute: () => null } });\n"
        out = self.run_page(box + """R.before = ELS.filters.html;
box('Intel Brief', true); box('Fun Events', true); await flush();
R.two = ELS.rows.html; R.after = ELS.filters.html; R.url = OUT.urls[OUT.urls.length - 1];
ELS.filters.listeners.change[0]({ currentTarget: ELS.filters, target: { dataset: { pick: 'day' }, value: '2026-09-04' } }); await flush();
R.day = ELS.rows.html;""")
        before, after = out["R"]["before"], out["R"]["after"]
        self.assertNotIn('data-f="src"', before)
        self.assertEqual(re.findall(r'<input type="checkbox" data-pick="src" value="([^"]+)"', before),
                         ["Fun Events", "Intel Brief", "Weekly Digest"])
        self.assertRegex(before, r'<details class="fpick" data-pick="src"><summary[^>]*><span id="f-src-v">All</span>')
        self.assertEqual(re.findall(r'<option value="([^"]*)"', re.search(r'<select id="f-day".*?</select>', before).group(0)),
                         ["", "2026-09-06", "2026-09-04", "2026-09-03"])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["two"]),
                         ["2026-09-06 - Fun Events", "2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief"])
        # The menu in use stays open across the redraw, names the count and keeps both boxes ticked.
        self.assertRegex(after, r'<details class="fpick" data-pick="src" open><summary[^>]*><span id="f-src-v">2 picked</span>')
        self.assertEqual(len(re.findall(r'data-pick="src" value="[^"]+" checked', after)), 2)
        self.assertEqual(out["R"]["url"], [["src", "Intel Brief"], ["src", "Fun Events"]])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["day"]), ["2026-09-04 - Intel Brief"])

    def test_the_filters_are_activitys_bar_and_the_count_sits_once_on_the_title_line(self):
        """sd:5463: the picked sources and the query are tokens, Clear all ends the bar, and the count is the title line's
        alone: the bar said "3 of 3" beside the title's "3 shown"."""
        click = "ELS.filters.listeners.click[0]({ target: { closest: s => s === %s ? { dataset: %s } : null } }); await flush();"
        out = self.run_page("R.bar = ELS.filters.html; R.tally = ELS.tally.html;"
                            + click % ("'[data-f]'", "{ f: 'src', v: 'Fun Events' }") + " R.bar2 = ELS.filters.html; R.tally2 = ELS.tally.html;"
                            + click % ("'[data-fclear]'", "{}") + " R.bar3 = ELS.filters.html; R.tally3 = ELS.tally.html;",
                            search="?src=Intel%20Brief&src=Fun%20Events")
        R = out["R"]
        self.assertTrue(R["bar"].startswith('<div class="fbar" role="group" aria-label="Filters">'), R["bar"][:80])
        self.assertEqual(re.findall(r'class="tok" data-f="src" data-v="([^"]+)"', R["bar"]), ["Intel Brief", "Fun Events"])
        self.assertTrue(R["bar"].endswith('<div class="fsum"><button class="linkbtn" type="button" data-fclear>Clear all</button></div></div>'))
        self.assertNotRegex(R["bar"], r"\d+ of \d+", "the bar holds no count")
        self.assertNotRegex(R["tally"], r"briefs", "sd:5524: the Briefs tab carries the count")
        self.assertEqual(re.findall(r'class="tok" data-f="src" data-v="([^"]+)"', R["bar2"]), ["Intel Brief"])
        self.assertTrue(R["bar3"].endswith('<div class="fsum"></div></div>'))
        self.assertNotRegex(R["tally3"], r"briefs")

    def test_a_search_from_the_address_matches_as_typing_it_does(self):
        # Review 27bec38c4a96: the address keeps the case the reader typed; the match ignores case either way.
        out = self.run_page("R.rows = ELS.rows.html;", search="?q=Intel")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["2026-09-06 - Intel Brief", "2026-09-04 - Intel Brief"])

    def refresh(self, body, later):
        """Load, then press Read again with each answer of `later` held; `body` releases them and reads the page."""
        return self.run_page(f"""const A = ANSWER, LATER = {json.dumps(later)}, HELD = [];
C.select = id => (OUT.selects = OUT.selects || []).push(id);
ANSWER = (path, body) => {{ if (path !== '/api/briefs') return A(path, body); const doc = LATER.shift();
  return new Promise(r => HELD.push(() => r(Array.isArray(doc) ? doc : [200, doc]))); }};
const click = () => window.PAGE_AGAIN.run();
{body}""")

    def subset(self, keep):
        """The page's document with only the briefs `keep` names."""
        rows = [b for b in self.page_doc["briefs"] if b["id"] in keep]
        return {**self.page_doc, "briefs": rows, "reader": {**self.page_doc["reader"], "total": len(rows), "shown": len(rows)}}

    def refused(self):
        def refuse(_):
            raise sqlite3.OperationalError("database is locked")
        return self.doc(reader=refuse)

    def test_an_older_refresh_that_answers_last_does_not_win(self):
        # Review ede2f75d05ab: two Read again presses overlap; the newer read stands whatever order they answer in.
        older = self.subset({"2026-09-06 - Intel Brief"})
        out = self.refresh("""click(); click(); await flush();
HELD[1](); await flush(); HELD[0](); await flush();
R.rows = ELS.rows.html;""", [older, self.page_doc])
        self.assertEqual(len(re.findall(r'<tr data-id=', out["R"]["rows"])), 4)
        self.assertIsNone(out["states"][-1])

    def test_a_refresh_with_no_rows_clears_the_selection_and_the_details(self):
        # Review d1f41457d2b0: Details must not offer Read or Make task on a brief the last read no longer has.
        for doc in (self.subset(set()), self.refused(), [500, {"error": "boom"}]):
            out = self.refresh("""R.before = window.PAGE_LIST.current();
click(); await flush(); HELD[0](); await flush();
R.det = ELS.details.html; R.now = window.PAGE_LIST.current(); R.rows = ELS.rows.html;""", [doc])
            self.assertEqual(out["R"]["rows"], "")
            self.assertEqual(out["R"]["before"], "2026-09-06 - Fun Events")
            self.assertIsNone(out["R"]["now"])
            self.assertEqual(out["selects"][-1], None)
            self.assertNotIn('class="bar"', out["R"]["det"])
            self.assertIn("Select a brief to see it.", out["R"]["det"])

    def test_a_brief_the_refresh_drops_runs_no_command(self):
        # sd:2418: the shell's object map only grows, so a brief the refresh no longer lists must not still offer Read.
        fewer = self.subset({b["id"] for b in self.page_doc["briefs"]} - {"2026-09-06 - Intel Brief"})
        out = self.refresh("""click(); await flush(); HELD[0](); await flush();
const o = C.get('2026-09-06 - Intel Brief'); R.dropped = [o.type, o.label, REG.filter(c => c.on === o.type).length];
R.kept = C.get('2026-09-06 - Fun Events').type;""", [fewer])
        self.assertEqual(out["R"]["dropped"], ["not listed", "2026-09-06 - Intel Brief (no longer listed)", 0],
                         "a brief the refresh dropped kept a command object")
        self.assertEqual(out["R"]["kept"], "brief")

    def test_make_task_opens_the_capture_form_and_only_capture_files_it(self):
        out = self.run_page("""shellRun(cmd('brief.task'), C.get('2026-09-04 - Intel Brief')); await flush();
R.cli = cmd('brief.task').cli(C.get('2026-09-04 - Intel Brief'));
R.hook = typeof window.SHELL_CAPTURE;""")
        self.assertEqual(out["captures"], [["2026-09-04 - Intel Brief", "2026-09-04 - Intel Brief"]])
        self.assertEqual(out["R"]["cli"], "sd task add --priority 3 '2026-09-04 - Intel Brief'")
        # sd:3520: the shell's default capture files it (test_v2_shell_shared); Briefs keeps no hook of its own.
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["R"]["hook"], "undefined")
        self.assertEqual(out["confirms"], [])

    def test_open_in_obsidian_is_the_copy_link_and_off_with_the_reason_without_one(self):
        out = self.run_page(f"""shellRun(cmd('brief.open'), C.get('2026-09-06 - Intel Brief'));
shellRun(cmd('brief.open'), C.get({json.dumps(NO_COPY)}));
R.cli = cmd('brief.open').cli(C.get('2026-09-06 - Intel Brief')); R.exec = cmd('brief.open').executes;""")
        self.assertEqual(out["opened"], [[link("2026-09-06 - Intel Brief"), "_blank", "noopener"]])
        self.assertEqual(out["toasts"][0][0], "Obsidian opens the copy")
        self.assertRegex(out["toasts"][1][0], r"^off: the last render is not newer than this brief")
        self.assertEqual(out["R"]["cli"], f"open '{link('2026-09-06 - Intel Brief')}'")
        self.assertFalse(out["R"]["exec"])
        self.assertEqual(out["posts"], [])

    def test_details_show_the_brief_and_the_address_row_opens_it(self):
        out = self.run_page("R.det = ELS.details.html;", row="2026-09-03 - Weekly Digest")
        det = out["R"]["det"]
        self.assertIn("<h2>2026-09-03 - Weekly Digest</h2>", det)
        self.assertIn("time not known: the note changed after its day", det)
        self.assertIn(f"<code>{FOLDER}/2026-09-03 - Weekly Digest.md</code>", det)
        self.assertIn(f"<dd>#{self.ids['2026-09-03 - Weekly Digest']}</dd>", det)
        self.assertIn(briefs_screen.WATCHDOG_REASON, det)
        self.assertEqual(out["panes"], ["tab-details"])

    def test_a_skipped_row_is_named_apart_and_is_not_a_cap(self):
        # Review 66a49b9f8bec: only a cut is "capped"; a rejected row is skipped.
        doc = self.doc(reader=lambda c: routine_briefs.rows(c) + [{"title": "x"}])
        self.assertEqual((doc["reader"]["capped"], doc["reader"]["skipped"]), (False, 1))
        out = self.run_page("R.sub = ELS.sub.textContent; R.ann = ELS.annunciator.html;", answer=f"() => [200, {json.dumps(doc)}]")
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("1 row skipped: not a brief.", out["states"][-1]["text"])
        self.assertNotIn("Showing the newest", out["states"][-1]["text"])
        self.assertNotIn("at least", out["R"]["ann"])
        self.assertEqual(out["R"]["sub"], "All read, 1 skipped")

    def test_a_failed_read_a_reader_refusal_and_a_capped_read_each_say_so(self):
        out = self.run_page("R.rows = ELS.rows.html;", answer="() => [500, { error: 'boom' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])
        out = self.run_page("R.rows = ELS.rows.html;", answer=f"() => [200, {json.dumps(self.refused())}]")
        self.assertEqual((out["states"][-1]["kind"], out["R"]["rows"]), ("error", ""))
        self.assertIn("database is locked", out["states"][-1]["text"])
        capped = {**self.page_doc, "reader": {**self.page_doc["reader"], "capped": True, "total": None}}
        out = self.run_page("R.sub = ELS.sub.textContent;", answer=f"() => [200, {json.dumps(capped)}]")
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("Showing the newest 5; older briefs are in the database.", out["states"][-1]["text"])
        self.assertEqual(out["R"]["sub"], "newest 5 briefs read")

    def test_a_lamp_with_no_read_behind_it_is_unknown_and_a_cut_count_says_at_least(self):
        # Lead check: missing evidence never shows as healthy, and a count from a capped list says it is a floor.
        for answer in (f"() => [200, {json.dumps(self.refused())}]", "() => [500, { error: 'boom' }]"):
            ann = self.run_page("R.ann = ELS.annunciator.html;", answer=answer)["R"]["ann"]
            # The Briefs lamp; the copies lamp has its own read, and a failed answer leaves it unknown too.
            self.assertEqual(cell_grammar(self, ann)[0], ("div", "unknown"))
            self.assertIn("<b>not read</b>", ann)
        # sd:5298: the unknown copies lamp still links to Health's row, which reads the heartbeat on its own.
        self.assertEqual(cell_grammar(self, ann)[2], ("a", "unknown"))
        cut = self.subset({b["id"] for b in self.page_doc["briefs"][:4]})
        cut["reader"] = {**cut["reader"], "capped": True, "total": None}
        out = self.run_page("""R.ann = ELS.annunciator.html;
ELS.lanes.listeners.click[0]({ target: { closest: s => s === '.lane-btn' ? { dataset: { src: 'Intel Brief' } } : null } });
R.det = ELS.details.html;""", answer=f"() => [200, {json.dumps(cut)}]")
        self.assertIn("at least <b>4</b> in 7 days", out["R"]["ann"])
        self.assertIn("Counts before 09-03 are not read.", out["states"][-1]["text"])
        self.assertIn("<dd>2 of the newest 4</dd>", out["R"]["det"])
        full = self.run_page("R.ann = ELS.annunciator.html;")["R"]["ann"]
        self.assertNotIn("at least", full)

    def test_the_list_and_the_cadence_are_the_shells_views_with_the_range_in_each_filter_row(self):
        """sd:5080: Briefs, Topics and Cadence are PAGE_VIEWS, in that order; the Range chips lead each filter row (pack sd:7084)."""
        page = (V2 / "briefs.html").read_text(encoding="utf-8")
        self.assertLess(page.index('id="view-list"'), page.index('id="view-topics"'))
        self.assertLess(page.index('id="view-topics"'), page.index('id="view-cadence"'))
        out = self.run_page("""R.views = window.PAGE_VIEWS.map(v => [v.id, v.label, v.key || null]);
R.list = ELS.range.html; R.cad = ELS['range-cad'].html;
ELS['range-cad'].listeners.click[0]({ target: { closest: () => ({ dataset: { r: '30d' } }) } }); await flush();
R.after = [ELS.range.html, ELS['range-cad'].html]; R.urls = OUT.urls.slice(-1);""")
        self.assertEqual(out["R"]["views"], [["list", "Briefs", "b"], ["topics", "Topics", None], ["cadence", "Cadence", None]])
        self.assertIn('data-r="7d" aria-pressed="true"', out["R"]["list"])
        self.assertEqual(out["R"]["list"], out["R"]["cad"])
        self.assertTrue(all('data-r="30d" aria-pressed="true"' in h for h in out["R"]["after"]), out["R"]["after"])

    def test_a_tick_or_a_lane_in_the_cadence_shows_the_list(self):
        out = self.run_page("""ELS.lanes.listeners.click[0]({ target: { closest: s => s === 'rect.tick' ? { dataset: { id: '2026-09-04 - Intel Brief' } } : null } });
await flush(); R.tick = OUT.shown.slice();
ELS.lanes.listeners.click[0]({ target: { closest: s => s === '.lane-btn' ? { dataset: { src: 'Fun Events' } } : null } });
await flush(); R.lane = OUT.shown.slice(); R.rows = ELS.rows.html;""")
        self.assertEqual(out["R"]["tick"], ["list"])
        self.assertEqual(out["R"]["lane"], ["list", "list"])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["2026-09-06 - Fun Events"])

    def topics_page(self, found=None, **more):
        """The page's document with `TOPICS` seeded and `found` recorded as signal topics (pack sd:5798)."""
        seeded = seed_topics(self.connection)
        if found:
            record_topics(self.connection, found)
        return seeded, self.doc(**more)

    def test_the_topics_tab_counts_open_topics_only(self):
        """sd:8018: a retired topic stays listed but leaves the tab's count."""
        _, doc = self.topics_page()
        doc["topics"]["rows"][-1]["stage"] = "retired"
        out = self.run_page("R.rows = ELS['topic-rows'].html; R.counts = OUT.counts;",
                            answer=f"() => [200, {json.dumps(doc)}]")
        self.assertEqual(len(re.findall(r'<tr data-topic=', out["R"]["rows"])), 3)
        self.assertEqual(out["R"]["counts"]["topics"], 2, "sd:8018: the tab counted a retired topic")

    def test_the_topics_view_shows_each_topic_its_last_signal_newest_memory_and_sources(self):
        """system sd:5778: one row per topic row; a topic opens its Details on Tasks, where it is edited."""
        seeded, doc = self.topics_page({"2026-09-06 - Intel Brief": ["graphs"], "2026-09-04 - Intel Brief": ["graphs", "agents"],
                                        "2026-08-20 - Intel Brief": ["agents"], "2026-09-06 - Fun Events": []})
        out = self.run_page("R.rows = ELS['topic-rows'].html; R.note = ELS['topic-note'].html; R.counts = OUT.counts || {};",
                            answer=f"() => [200, {json.dumps(doc)}]")
        rows = out["R"]["rows"]
        self.assertEqual(re.findall(r'<tr data-topic="([^"]+)"', rows), ["Graphs", "Agents", "Climate"])
        graphs, agents, climate = re.split(r'(?=<tr data-topic=)', rows)[1:]
        self.assertIn(f'href="/tasks?row={seeded["Graphs"]}"', graphs)
        self.assertIn("<td>graphs</td><td>active</td>", graphs)
        self.assertIn(">09-06</", graphs)
        self.assertIn("2 days", graphs)
        self.assertIn("2026-09-06 a running story · 2026-09-06 - Intel Brief", graphs)
        self.assertIn("2 lines", graphs)
        self.assertIn("2 sources", graphs)
        # 08-20 is outside 7 days: the last signal stands, the days in range count one.
        self.assertIn(">09-04</", agents)
        self.assertIn("1 day", agents)
        self.assertIn("empty", agents)
        self.assertIn("no Memory section", climate)
        self.assertIn("no Sources section", climate)
        # One brief in range records no signal topics: a topic none of the others names is not recorded, never quiet.
        self.assertIn("not recorded", climate)
        self.assertNotIn("none in", climate)
        self.assertIn("1 brief in range records no signal topics", out["R"]["note"])
        # Once every brief in range records its topics, a topic none names is none in the range.
        record_topics(self.connection, {"2026-09-03 - Weekly Digest": []})
        out = self.run_page("R.rows = ELS['topic-rows'].html; R.note = ELS['topic-note'].html;",
                            answer=f"() => [200, {json.dumps(self.doc())}]")
        self.assertIn("none in 7 days", re.split(r'(?=<tr data-topic=)', out["R"]["rows"])[3])
        self.assertEqual(out["R"]["note"], "")

    def test_a_topic_row_offers_remove_and_delete_behind_a_confirm_and_a_retired_one_no_remove(self):
        """sd:6703: Remove retires an active topic and is off on a retired one; Delete asks first and names the loss."""
        seeded, doc = self.topics_page()
        next(t for t in doc["topics"]["rows"] if t["title"] == "Climate")["stage"] = "retired"
        graphs, climate = seeded["Graphs"], seeded["Climate"]
        out = self.run_page(f"""const g = OBJ.get('topic:{graphs}'), c = OBJ.get('topic:{climate}');
R.retired = cmd('topic.remove').when(c); R.consequence = cmd('topic.delete').consequence(g); R.head = ELS['topic-rows'].html;
C.run(cmd('topic.remove'), c); await flush(); C.run(cmd('topic.remove'), g); await flush(); C.run(cmd('topic.delete'), c); await flush();
FIELD_VALUES = {{ title: 'Edge AI' }}; C.run(cmd('topic.add'), OBJ.get('topics')); await flush();""",
                            answer=f"(p) => p === '/api/actions/run' ? [200, {{ action: 'x', label: 'Ran', line: 'l', exit: 0, output: 'ok', cut: false }}] : [200, {json.dumps(doc)}]")
        self.assertEqual(out["R"]["retired"], "retired already")
        self.assertIn("for good: its body, its Memory and its Sources are lost", out["R"]["consequence"])
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/actions/run", {"action": "topic.remove", "item": graphs}],
                                                         ["/api/actions/run", {"action": "topic.delete", "item": climate}],
                                                         ["/api/actions/run", {"action": "topic.add", "title": "Edge AI"}]])
        self.assertEqual(out["confirms"], ["topic.remove", "topic.delete", "topic.add"])
        self.assertIn(["off: retired already", False], out["toasts"])
        self.assertIn('<td class="act">', out["R"]["head"])

    def test_a_retired_topic_offers_restore_and_an_active_one_does_not(self):
        """sd:6703: Restore shows on a retired topic only, asks first, and posts topic.restore."""
        seeded, doc = self.topics_page()
        next(t for t in doc["topics"]["rows"] if t["title"] == "Climate")["stage"] = "retired"
        graphs, climate = seeded["Graphs"], seeded["Climate"]
        out = self.run_page(f"""const g = OBJ.get('topic:{graphs}'), c = OBJ.get('topic:{climate}');
R.off = cmd('topic.restore').when(g); R.on = cmd('topic.restore').when(c);
C.run(cmd('topic.restore'), g); await flush(); C.run(cmd('topic.restore'), c); await flush();""",
                            answer=f"(p) => p === '/api/actions/run' ? [200, {{ action: 'x', label: 'Ran', line: 'l', exit: 0, output: 'ok', cut: false }}] : [200, {json.dumps(doc)}]")
        self.assertEqual(out["R"]["on"], True)
        self.assertRegex(out["R"]["off"], r": Restore brings back a retired topic$")
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/actions/run", {"action": "topic.restore", "item": climate}]])
        self.assertEqual(out["confirms"], ["topic.restore"])

    def test_with_no_brief_recording_its_topics_signal_is_not_recorded_never_quiet(self):
        _seeded, doc = self.topics_page()
        out = self.run_page("R.rows = ELS['topic-rows'].html; R.note = ELS['topic-note'].html;",
                            answer=f"() => [200, {json.dumps(doc)}]")
        self.assertEqual(len(re.findall(r'<tr data-topic=', out["R"]["rows"])), 3)
        self.assertNotRegex(out["R"]["rows"], r"none in|\d day")
        self.assertEqual(out["R"]["rows"].count("not recorded"), 6)
        self.assertIn("No brief records its signal topics yet", out["R"]["note"])

    def test_a_range_whose_briefs_record_no_topics_says_not_recorded_though_an_older_brief_does(self):
        """Review: one recorded brief outside the range is no evidence that the range was quiet."""
        _seeded, doc = self.topics_page({"2026-08-20 - Intel Brief": ["agents"]})
        out = self.run_page("R.rows = ELS['topic-rows'].html;", answer=f"() => [200, {json.dumps(doc)}]")
        self.assertNotIn("none in", out["R"]["rows"])
        agents = re.split(r'(?=<tr data-topic=)', out["R"]["rows"])[2]
        self.assertIn(">08-20</", agents)
        self.assertIn("not recorded", agents)

    def test_a_failed_brief_read_says_signal_was_not_read_not_that_no_brief_records_it(self):
        """Review: a failed brief read is no evidence about the briefs' records."""
        def refuse(_):
            raise sqlite3.OperationalError("database is locked")
        _seeded, doc = self.topics_page(reader=refuse)
        out = self.run_page("R.rows = ELS['topic-rows'].html; R.note = ELS['topic-note'].html;",
                            answer=f"() => [200, {json.dumps(doc)}]")
        self.assertNotIn("records its signal topics", out["R"]["note"])
        self.assertIn("Signal was not read: the briefs were not read", out["R"]["note"])
        self.assertEqual(out["R"]["rows"].count("not read"), 6)
        self.assertNotIn("not recorded", out["R"]["rows"])

    def test_a_topic_read_that_failed_says_so_and_names_no_topic(self):
        def refuse(_):
            raise sqlite3.OperationalError("database is locked")
        _seeded, doc = self.topics_page(topic_reader=refuse)
        out = self.run_page("R.rows = ELS['topic-rows'].html; R.note = ELS['topic-note'].html;",
                            answer=f"() => [200, {json.dumps(doc)}]")
        self.assertEqual(out["R"]["rows"], "")
        self.assertIn("The topics were not read: database is locked", out["R"]["note"])

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        for text in (BRIEFS_JS, BRIEF_DETAILS_JS):
            self.assertNotIn("innerHTML", text)
            self.assertNotIn("setAttribute('style'", text)
            self.assertNotRegex(text, r"style=\\?\"")
            self.assertNotIn("history.replaceState", text)
            self.assertNotRegex(text, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", BRIEFS_JS)


def adversarial(case, questions=3, task=12):
    """An adversarial review's batch of Approve-or-Reject recommendations and the brief that names it (sd:5064)."""
    batch = review.file(case.connection, {
        "schema": 1, "queue": "adversarial", "batch": f"task-{task}-101500", "title": f"Adversarial review of task {task}",
        "repo": REPO, "source": "local-adversarial-gate", "mode": "open",
        "questions": [{"subject": f"task {task}: hypothesis {n}", "text": f"Fix hypothesis {n} of task {task}\n\nWhy it matters.",
                       "options": ["Approve", "Reject"]} for n in range(1, questions + 1)]}, who="test")["item"]
    stem = f"2026-09-06 - Adversarial review · task {task} · 101500Z"
    brief = routine_briefs.add(case.connection, stem, f"---\nreview: adversarial\nrun: r1\nbatches: sd:{batch}\n---\n\n"
                               f"# Adversarial review of task {task}\n", repo=REPO, who="test")["item"]
    return brief, batch


class TheRecommendations(BriefCase):
    """`/api/briefs/<item>/recommendations`: an adversarial brief's batch questions, to pick which become tasks."""

    def test_each_question_of_each_named_batch_comes_with_its_title_repo_and_batch_revision(self):
        brief, batch = adversarial(self)
        notes = [n["id"] for n in workflow.item_state(self.connection, batch)["notes"] if n["kind"] == "question"]
        revision = workflow.item_state(self.connection, batch)["revision"]
        out = review_screen.rule_request({"item": batch, "note": notes[0], "option": "Approve",
                                          "revision": revision})(self.connection)
        review_screen.rule_request({"item": batch, "note": notes[1], "option": "Reject",
                                    "revision": out["revision"]})(self.connection)
        doc = review_screen.recommendations(self.connection, brief)
        self.assertEqual(doc["batches"], [{"item": batch, "repo": "system", "error": None, "open": 1,
                                           "revision": workflow.item_state(self.connection, batch)["revision"]}])
        filed = review.read(self.connection, batch)["questions"][0]["filed"]
        self.assertIsInstance(filed, int)
        self.assertEqual([(q["batch"], q["note"], q["title"], q["repo"], q["label"], q["filed"], q["open"], q["options"])
                          for q in doc["questions"]],
                         [(batch, notes[0], "Fix hypothesis 1 of task 12", "system", "Approve", filed, False, ["Approve", "Reject"]),
                          (batch, notes[1], "Fix hypothesis 2 of task 12", "system", "Reject", None, False, ["Approve", "Reject"]),
                          (batch, notes[2], "Fix hypothesis 3 of task 12", "system", None, None, True, ["Approve", "Reject"])])
        # The repository's name, never its local path.
        self.assertNotIn(REPO, json.dumps(doc))

    def test_a_brief_that_is_no_adversarial_review_or_no_item_is_refused(self):
        self.assertEqual(review_screen.recommendations(self.connection, self.ids[NO_COPY])[0], 404)
        self.assertEqual(review_screen.recommendations(self.connection, 999999)[0], 404)

    def test_a_named_batch_that_does_not_read_is_listed_with_its_reason(self):
        brief, batch = adversarial(self)
        other = self.item("Not a batch")
        self.connection.execute("UPDATE item SET body = json_object('text', ?) WHERE id = ?",
                                (f"---\nreview: adversarial\nbatches: sd:{batch}, sd:{other}\n---\n", brief))
        self.connection.commit()
        doc = review_screen.recommendations(self.connection, brief)
        self.assertEqual([b["item"] for b in doc["batches"]], [batch, other])
        self.assertIn("is not a review batch", doc["batches"][1]["error"])
        self.assertEqual(len(doc["questions"]), 3)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "briefs", "Briefs", "/briefs", ("/api/briefs", "/api/briefs/7/recommendations", "/api/briefs/publish", "/api/briefs/unpublish",
                                                                "/api/briefs/publish-now", "/api/briefs/publish-now/drain-x-20261018T000000Z")


if __name__ == "__main__":
    unittest.main()
