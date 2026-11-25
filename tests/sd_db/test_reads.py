"""`sd_db.reads`."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sd_db import connect, create_item, reads, reporting, upsert_repo, workflow
from sd_db.errors import SdDbError
from sd_db.migrate import initialise
from sd_db.schema import migrations
from sd_db.testing.wire import hub_only
from sd_db.writes import (
    add_note,
    record_state,
    resolve_state,
    set_item_fields,
    transition,
    upsert_shadow,
)
from sd_db.writing import (
    cutover_pieces,
    cutover_preview,
    import_piece,
    list_pieces,
    park_piece,
)

#: A fixed instant, so the week window does not depend on when the suite runs.
NOW = "2026-09-06T12:00:00Z"
WHEN = "2026-09-04T09:00:00+00:00"


class TheItemShadow(unittest.TestCase):
    """`shadow` is keyed by `(tracker, url)`, so a url names a row only once
    you say whose. The read either takes the tracker or refuses to guess."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "sd.db"
        initialise(path)
        self.connection = connect(path)
        self.addCleanup(self.connection.close)
        self.url = "https://github.com/o/r/issues/1"
        self.item = create_item(
            self.connection, kind="task", title="A thing",
            source="github", external_id=self.url)
        upsert_shadow(self.connection, tracker="github", url=self.url, title="from github")

    def test_one_tracker_needs_no_predicate(self):
        self.assertEqual(reads.item_shadow(self.connection, self.item)["title"], "from github")

    def test_the_named_tracker_gets_its_own_row(self):
        upsert_shadow(self.connection, tracker="jira", url=self.url, title="from jira")
        self.assertEqual(
            reads.item_shadow(self.connection, self.item, tracker="github")["title"],
            "from github")
        self.assertEqual(
            reads.item_shadow(self.connection, self.item, tracker="jira")["title"],
            "from jira")

    def test_a_tracker_with_no_such_row_gets_nothing_rather_than_the_other_one(self):
        self.assertIsNone(reads.item_shadow(self.connection, self.item, tracker="jira"))

    def test_an_unnamed_tracker_on_a_contested_url_is_refused(self):
        upsert_shadow(self.connection, tracker="jira", url=self.url, title="from jira")
        with self.assertRaises(SdDbError) as raised:
            reads.item_shadow(self.connection, self.item)
        self.assertIn("github, jira", str(raised.exception))


class OpenFollowups(unittest.TestCase):
    def test_a_done_items_followups_are_not_owed(self):
        # sd:3505: Today's list read followups on done items, which nobody owes.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            connection = connect(path)
            self.addCleanup(connection.close)
            live = create_item(connection, kind="task", title="Live", status="planning")
            done = create_item(connection, kind="task", title="Done", status="done")
            owed = add_note(connection, live, "followup", "Still owed")
            add_note(connection, done, "followup", "Left behind")
            self.assertEqual([row["id"] for row in reads.open_followups(connection)], [owed])

    def test_a_followup_a_delivery_kept_open_is_still_owed(self):
        # sd:9869: a delivery closes its item but leaves a followup that waits on an outside reply;
        # Today must still list it. One the delivery settled, or a hand close's, is gone.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            connection = connect(path)
            self.addCleanup(connection.close)
            item = create_item(connection, kind="task", title="Delivered", status="in_progress")
            waiting = add_note(connection, item, "followup", "Waits on an outside reply")
            settled = add_note(connection, item, "followup", "Rename the flag")
            workflow.change_status(connection, item, "done", who="sd-ship", reason="delivered", followups={settled})
            closed = create_item(connection, kind="task", title="Closed by hand", status="in_progress")
            add_note(connection, closed, "followup", "Not owed after a hand close")
            workflow.change_status(connection, closed, "done", who="operator", reason="test",
                                   resolve_followups=True)
            self.assertEqual([row["id"] for row in reads.open_followups(connection)], [waiting])

    def test_a_close_reason_that_names_a_followup_does_not_keep_it_owed(self):
        # sd:9869 class pass: Today read the kept followups from the close note's text, so a
        # hand close whose reason says "kept open notes N" listed a later note N as owed.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            connection = connect(path)
            self.addCleanup(connection.close)
            item = create_item(connection, kind="task", title="Closed by hand", status="in_progress")
            named = ", ".join(str(note) for note in range(1, 50))
            workflow.change_status(connection, item, "done", who="operator", reason=f"kept open notes {named}")
            self.assertLess(add_note(connection, item, "followup", "Left behind"), 50)
            self.assertEqual(reads.open_followups(connection), [])


class ParkedWritingReads(unittest.TestCase):
    def test_due_date_does_not_put_parked_writing_back_in_today(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            connection = connect(path)
            self.addCleanup(connection.close)
            item = create_item(connection, kind="idea", title="Parked due idea", status="planning", due="2026-09-01")
            set_item_fields(connection, item, parked_at=WHEN)
            self.assertEqual(reads.today_items(connection, now=NOW), [])
            self.assertEqual(reads.backlog_items(connection, now=NOW), [])

    @hub_only
    def test_park_and_revive_change_today_and_backlog_but_preserve_explicit_writing_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "writing"
            index = repo / "content/2026/a-piece/index.md"
            index.parent.mkdir(parents=True)
            index.write_text("---\ntitle: A piece\nstatus: review\npublished: null\n---\n\n## Draft\nDraft prose.\n")
            path = root / "sd.db"
            initialise(path)
            connection = connect(path)
            self.addCleanup(connection.close)
            upsert_repo(connection, str(repo))
            piece = import_piece(connection, str(repo), "2026/a-piece", who="operator")["item"]["id"]
            followup = add_note(connection, piece, "followup", "Resume the parked piece only when revived")
            preview = cutover_preview(connection, str(repo))
            cutover_pieces(connection, str(repo), expected_fingerprint=preview["fingerprint"], who="operator")
            task = create_item(connection, kind="task", title="Due ordinary task", status="planning", due="2026-09-01")
            for query in (reads.today_items, reads.backlog_items):
                self.assertEqual({row["id"] for row in query(connection, now=NOW)}, {piece, task})
            self.assertEqual([row["id"] for row in reads.open_followups(connection)], [followup])
            park_piece(connection, piece, require_row=True, who="operator")
            for query in (reads.today_items, reads.backlog_items):
                self.assertEqual([row["id"] for row in query(connection, now=NOW)], [task])
            self.assertEqual(reads.open_followups(connection), [])
            self.assertEqual(list_pieces(connection, str(repo)), [])
            self.assertEqual([row["id"] for row in list_pieces(connection, str(repo), include_parked=True)], [piece])
            park_piece(connection, piece, parked=False, require_row=True, who="operator")
            for query in (reads.today_items, reads.backlog_items):
                self.assertEqual({row["id"] for row in query(connection, now=NOW)}, {piece, task})
            self.assertEqual([row["id"] for row in reads.open_followups(connection)], [followup])

    def test_schema_two_readonly_queries_remain_available_without_parked_column(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "schema-two.db"
            raw = sqlite3.connect(path)
            for version, source in migrations():
                if version <= 2:
                    raw.executescript(source.read_text())
            raw.execute("PRAGMA user_version = 2")
            raw.execute("INSERT INTO item (kind,title,status,created_at,updated_at) VALUES ('task','Old active task','in_progress',?,?)", (WHEN, WHEN))
            raw.execute("INSERT INTO note (item,timestamp,kind,body) VALUES (1,?,'followup','Old followup')", (WHEN,))
            raw.commit()
            raw.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)
            self.assertNotIn("parked_at", {row[1] for row in connection.execute("PRAGMA table_info(item)")})
            before = connection.total_changes
            for query in (reads.today_items, reads.backlog_items):
                self.assertEqual([row["title"] for row in query(connection, now=NOW)], ["Old active task"])
            self.assertEqual([row["body"] for row in reads.open_followups(connection)], ["Old followup"])
            captured = reads.capture_items(connection)
            self.assertEqual([row["title"] for row in captured], ["Old active task"])
            self.assertIsNone(captured[0]["parked_at"])
            self.assertEqual(connection.total_changes, before)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)


class TheUnscopedBacklog(unittest.TestCase):
    """One pane of glass over the items that belong to no repository. An empty
    `repo` means "every repository", so before `NO_REPO` the personal rows
    could only be read mixed into everything."""

    def test_no_repository_is_selectable_and_is_not_the_same_as_no_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            writer = connect(path)
            repo = str(Path(temporary) / "alpha")
            upsert_repo(writer, repo)
            scoped = create_item(writer, kind="work", title="Scoped work",
                                 status="planning", repo=repo)
            chore = create_item(writer, kind="work-idea", title="Fence the repo",
                                status="planning")
            spark = create_item(writer, kind="personal-idea",
                                title="A thought that is not an article",
                                status="planning")
            errand = create_item(writer, kind="followup", title="Chase the quote",
                                 status="planning")
            writer.commit()
            writer.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)

            everything = [row["id"] for row in reads.backlog_items(connection)]
            self.assertEqual(sorted(everything), sorted([scoped, chore, spark, errand]))

            unscoped = [row["id"] for row in
                        reads.backlog_items(connection, repo=reads.NO_REPO)]
            self.assertEqual(sorted(unscoped), sorted([chore, spark, errand]))

            self.assertEqual([row["id"] for row in
                              reads.backlog_items(connection, repo=repo)], [scoped])

            # Facets still compose, which is what makes the pane of glass work:
            # one kind within the unscoped rows.
            self.assertEqual([row["id"] for row in reads.backlog_items(
                connection, repo=reads.NO_REPO, kind="work-idea")], [chore])

    def test_a_followup_with_a_repository_is_read_with_it_and_an_old_one_stays_unscoped(self):
        """sd:809. A followup took no repository before, so no checkout's brief
        and no scoped backlog listed one. The two ways it now takes one, a task
        reclassified with its repository and a followup moved onto a checkout,
        put it in both readers; a followup filed before then stays unscoped."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            writer = connect(path)
            repo = str(Path(temporary) / "alpha")
            upsert_repo(writer, repo)
            older = create_item(writer, kind="followup", title="Filed before sd:809",
                                status="planning")
            reclassified = workflow.capture_task(
                writer, title="A review finding", repo=repo, who="alex")["item"]["id"]
            workflow.edit_item(writer, reclassified, {"kind": "followup"}, who="alex")
            moved = create_item(writer, kind="followup", title="Filed off the checkout",
                                status="planning")
            workflow.edit_item(writer, moved, {"repo": repo}, who="alex")
            writer.commit()
            writer.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)

            self.assertEqual(sorted(row["id"] for row in reads.brief_items(connection, repo)),
                             sorted([reclassified, moved]))
            self.assertEqual(sorted(row["id"] for row in reads.backlog_items(
                connection, repo=repo, kind="followup")), sorted([reclassified, moved]))
            self.assertEqual([row["id"] for row in reads.backlog_items(
                connection, repo=reads.NO_REPO, kind="followup")], [older])

    def test_parked_reads_the_open_items_the_prune_parked_and_nothing_else(self):
        """sd:3012. The default read hides a parked item, so nothing listed one to unpark.
        `parked=True` reads the open parked items: not a parked piece, which revives
        through its own workflow, and not a done one, which has nothing to come back to."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            writer = connect(path)
            shown = create_item(writer, kind="task", title="Still listed", status="planning")
            parked = create_item(writer, kind="task", title="Parked by the prune", status="planning")
            piece = create_item(writer, kind="idea", title="A parked piece", status="planning")
            finished = create_item(writer, kind="task", title="Parked, then done", status="planning")
            set_item_fields(writer, parked, parked_at="2026-09-01T02:00:00Z")
            set_item_fields(writer, piece, piece="a-parked-piece", parked_at="2026-09-01T02:00:00Z")
            set_item_fields(writer, finished, parked_at="2026-09-01T02:00:00Z")
            transition(writer, finished, "done", who="test")
            writer.commit()
            writer.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)

            self.assertEqual([row["id"] for row in reads.backlog_items(connection, now=NOW)], [shown])
            self.assertEqual([row["id"] for row in reads.backlog_items(connection, now=NOW, parked=True)], [parked])

    def test_a_repository_registered_under_the_token_does_not_shadow_the_selector(self):
        """The collision, tested through the API that would cause it.

        `upsert_repo` validates nothing and `repo.path` carries no CHECK, so a
        repository really can be registered under the word the URL uses. The
        selector is an object, so identity keeps the two apart: the repository
        named `none` is one filter and "no repository at all" is another."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            writer = connect(path)
            upsert_repo(writer, reads.NO_REPO_TOKEN)
            named = create_item(writer, kind="work", title="In a repo called none",
                                status="planning", repo=reads.NO_REPO_TOKEN)
            unscoped = create_item(writer, kind="work-idea", title="Fence the repo",
                                   status="planning")
            writer.commit()
            writer.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)
            self.assertEqual([row["id"] for row in reads.backlog_items(
                connection, repo=reads.NO_REPO)], [unscoped])
            self.assertEqual([row["id"] for row in reads.backlog_items(
                connection, repo=reads.NO_REPO_TOKEN)], [named])
            self.assertIsNot(reads.NO_REPO, reads.NO_REPO_TOKEN)
            self.assertNotEqual(reads.NO_REPO, reads.NO_REPO_TOKEN)


class QuietRunReportsAreNotBacklog(unittest.TestCase):
    """sd:708 defect 2. A clean cron report is a log line that was given an
    item because there was nowhere else to put it; sd:739 built the somewhere
    else. Until it leaves the shared row set it is a row in the list, the board
    and the matrix for fourteen days -- seven `planning` before `retention`
    settles it, seven more inside `include_done_days`.

    Measured on the live store the day this was written: 310 of 739 rows.
    """

    def store(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "sd.db"
        initialise(path)
        writer = connect(path)
        self.addCleanup(writer.close)
        return writer

    def report(self, writer, *, attention, job="fixture-job"):
        """Through `ingest`, the real producer, so `fields` is the real shape.

        Building the row with `create_item` and a hand-written `fields` dict
        would test the test's idea of the JSON rather than the one the cron
        path writes.
        """
        state = reporting.ingest(
            writer, job=job, run_id=f"{job}-{attention}", started="2026-09-13T00:00:00+00:00",
            ended="2026-09-13T00:01:00+00:00", exit_code=1 if attention else 0,
            text="fixture run\n", source_path="/tmp/log", attention=attention)
        return state["item"]["id"]

    def test_a_clean_report_is_not_in_the_set_and_an_attention_report_is(self):
        writer = self.store()
        quiet = self.report(writer, attention=False, job="quiet-job")
        loud = self.report(writer, attention=True, job="loud-job")
        chore = create_item(writer, kind="task", title="A real task", status="planning")
        writer.commit()

        ids = [row["id"] for row in reads.backlog_items(writer)]
        self.assertNotIn(quiet, ids)
        self.assertIn(loud, ids)
        self.assertIn(chore, ids)

    def test_the_exclusion_is_by_attention_and_not_by_kind(self):
        """The whole point, and the reason a blanket `kind = 'report'` clause
        is wrong: `is_urgent` promotes an attention report into the matrix's
        urgent quadrant by name, so excluding the kind would leave that
        function dead in the same file it lives in."""
        writer = self.store()
        loud = self.report(writer, attention=True, job="loud-job")
        writer.commit()

        rows = {row["id"]: row for row in reads.backlog_items(writer)}
        self.assertIn(loud, rows)
        self.assertTrue(reads.is_urgent(rows[loud]))

    def test_asking_for_the_report_kind_returns_the_ones_that_need_a_person(self):
        writer = self.store()
        self.report(writer, attention=False, job="quiet-job")
        loud = self.report(writer, attention=True, job="loud-job")
        writer.commit()

        self.assertEqual([row["id"] for row in reads.backlog_items(writer, kind="report")],
                         [loud])

    def test_a_settled_clean_report_does_not_return_inside_the_done_window(self):
        """`done` is the other door into the set. A report that `retention`
        settles is `done` with a fresh `status_since`, which is exactly what
        `include_done_days` lets back in -- so the clause has to hold on both
        sides or the row leaves for seven days and comes back."""
        writer = self.store()
        quiet = self.report(writer, attention=False, job="quiet-job")
        transition(writer, quiet, "done", who="retention")
        writer.commit()

        self.assertNotIn(quiet, [row["id"] for row in reads.backlog_items(writer)])

    def test_a_numeric_attention_is_quiet_and_agrees_with_is_urgent(self):
        """The two readers must not disagree about the same row.

        `json_extract` returns 1 for the JSON boolean `true` and for the JSON
        number `1` alike, while `is_urgent` accepts only the boolean. Before
        this was tightened the row below was in the backlog and not in the
        urgent quadrant -- the shared-row contract broken by the clause written
        to protect it. `ingest` cannot produce this row: it refuses a non-bool
        `attention` (`type(attention) is not bool`, in `ingest` of `local-sd-db/sd_db/reporting.py`),
        so it is a hand-edited or legacy one, and quiet is the safe direction
        for those.
        """
        writer = self.store()
        quiet = self.report(writer, attention=False, job="quiet-job")
        writer.execute("UPDATE item SET fields=? WHERE id=?",
                       (json.dumps({"attention": 1, "report": {}}), quiet))
        writer.commit()

        rows = {row["id"]: row for row in reads.backlog_items(writer)}
        self.assertNotIn(quiet, rows)
        # The agreement is the assertion, not the exclusion on its own: both
        # readers have to reach the same verdict about this row.
        row = writer.execute("SELECT * FROM item WHERE id=?", (quiet,)).fetchone()
        self.assertFalse(reads.is_urgent(row))

    def test_a_report_whose_fields_cannot_speak_is_not_in_the_backlog_on_its_own(self):
        """sd:873. NULL, '' and `{not json` are one case at every site: a
        report whose `fields` cannot say it is clean is for a person to look
        at. Retention refuses to settle it and names it in the prune report,
        an attention report that IS in this backlog; the bulk clean declines
        it; the page withholds the one-click acknowledge. The backlog lists a
        report on its own word, and this row has none, so it is not listed
        on its own -- it reaches the person through the prune report, not
        by being called clean. The same three inputs are pinned at the other
        three sites, so the predicate cannot drift at one and stay green.
        """
        writer = self.store()
        for index, value in enumerate((None, "", "{not json")):
            with self.subTest(fields=value):
                quiet = self.report(writer, attention=False, job=f"quiet-job-{index}")
                writer.execute("UPDATE item SET fields=? WHERE id=?", (value, quiet))
                writer.commit()
                self.assertNotIn(quiet, [row["id"] for row in reads.backlog_items(writer)])
                row = writer.execute("SELECT * FROM item WHERE id=?", (quiet,)).fetchone()
                self.assertFalse(reads.is_urgent(row))

    def test_a_null_fields_report_is_quiet(self):
        """`item.fields` is nullable, and `json_valid(NULL)` is NULL rather
        than 0, so this path is distinct from malformed text and needs its own
        case -- otherwise a future rewrite of the clause could let NULL rows
        back in with every other test still green. Not in the backlog is not
        the same as clean: retention never settles this row and the prune
        report names it (sd:873, the three-input test above)."""
        writer = self.store()
        quiet = self.report(writer, attention=False, job="quiet-job")
        writer.execute("UPDATE item SET fields=NULL WHERE id=?", (quiet,))
        writer.commit()

        self.assertNotIn(quiet, [row["id"] for row in reads.backlog_items(writer)])

    def test_a_report_whose_fields_will_not_parse_is_treated_as_quiet(self):
        """`json_extract` and `json_type` do not return NULL for malformed
        JSON: they raise `sqlite3.OperationalError: malformed JSON`, and one
        such row would take down the whole backlog query. `json_valid` is
        0 for it, so the guard in `_NOT_A_QUIET_REPORT` short-circuits
        before `json_type` runs, and the row falls out. This is the test
        that proves the guard. Falling out is the safe direction: a report
        that cannot say it needs attention is not evidence that it does."""
        writer = self.store()
        quiet = self.report(writer, attention=False, job="quiet-job")
        writer.execute("UPDATE item SET fields='{not json' WHERE id=?", (quiet,))
        writer.commit()

        self.assertNotIn(quiet, [row["id"] for row in reads.backlog_items(writer)])


class CaptureItems(unittest.TestCase):
    def test_all_parents_have_explicit_fields_stable_order_and_one_readonly_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            writer = connect(path)
            repositories = (str(Path(temporary) / "alpha"), str(Path(temporary) / "beta"))
            for repo in repositories:
                upsert_repo(writer, repo)
            fixtures = (
                ("task", "planning", repositories[0], "2026-09-01", None),
                ("work", "in_progress", repositories[1], "2026-09-01", None),
                ("followup", "ready", None, "2026-09-02", None),
                ("task", "done", repositories[0], "2026-09-05", None),
                ("idea", "planning", repositories[1], "2026-09-06", WHEN),
                ("idea", "done", repositories[0], "2026-09-04", WHEN),
                ("report", "planning", None, "2026-08-31", None),
                ("work-idea", "planning", None, "2026-08-31", None),
                ("personal-idea", "blocked", None, "2026-08-31", None),
            )
            ids = []
            for kind, status, repo, updated, parked in fixtures:
                item = create_item(writer, kind=kind, title="Same title", status=status,
                                   repo=repo, body={"text": "Private body omitted from selector"})
                ids.append(item)
                writer.execute("UPDATE item SET updated_at=?, parked_at=? WHERE id=?", (updated, parked, item))
            writer.commit()
            writer.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)
            before = tuple(connection.iterdump())
            statements = []
            connection.set_trace_callback(statements.append)
            rows = reads.capture_items(connection)
            connection.set_trace_callback(None)
            self.assertEqual(len(statements), 1)
            self.assertTrue(statements[0].lstrip().startswith("SELECT"))
            self.assertEqual([row["id"] for row in rows], [ids[i] for i in (2, 1, 0, 8, 7, 6, 4, 3, 5)])
            self.assertEqual({row["repo"] for row in rows}, {*repositories, None})
            self.assertTrue(all(row.keys() == ["id", "title", "kind", "status", "repo", "parked_at"] for row in rows))
            self.assertEqual(connection.total_changes, 0)
            self.assertEqual(tuple(connection.iterdump()), before)


class TheActivityReads(unittest.TestCase):
    """The two reads behind the dashboard's Activity (sd:2111): delivery notes and recent assignments, by day."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "sd.db"
        initialise(path)
        self.connection = connect(path)
        self.addCleanup(self.connection.close)
        self.item = create_item(self.connection, kind="work", title="A slice")

    def test_delivery_notes_are_the_code_delivery_comments_from_the_day_on(self):
        from sd_db.ship import note_merge

        for number, sha in ((1, "a" * 40), (2, "b" * 40)):
            note_merge(self.connection, self.item, {"pull_request": {"url": f"https://github.com/o/r/pull/{number}"},
                                                    "merge_commit": sha})
        add_note(self.connection, self.item, "comment", "Code delivery, said in passing")
        add_note(self.connection, self.item, "decision", "Code delivery https://github.com/o/r/pull/3 at " + "c" * 40)
        ids = [row["id"] for row in self.connection.execute("SELECT id FROM note WHERE body LIKE 'Code delivery http%' ORDER BY id")]
        self.connection.execute("UPDATE note SET timestamp = '2026-09-01T10:00:00Z' WHERE id = ?", (ids[0],))
        self.connection.commit()
        rows = reads.delivery_notes(self.connection, since="2026-09-05T12:00:00Z")
        self.assertEqual([row["id"] for row in rows], [ids[1]])
        self.assertEqual(rows[0]["title"], "A slice")

    def evidence(self, number, sha):
        return {"pull_request": {"url": f"https://github.com/o/r/pull/{number}"}, "merge_commit": sha,
                "review_selection": {"reviewed_by": ["example-reviewer"]}}

    def test_a_delivery_note_is_its_marker_line_and_the_evidence_is_a_resolved_verified_row(self):
        """sd:3508: the evidence JSON made a quarter of the note bytes; the readers need only the line."""
        from sd_db.ship import delivery_key, note_merge

        note_merge(self.connection, self.item, self.evidence(1, "a" * 40))
        [note] = self.connection.execute("SELECT body FROM note WHERE item = ? AND body LIKE 'Code delivery %'",
                                         (self.item,)).fetchall()
        self.assertEqual(note["body"], "Code delivery https://github.com/o/r/pull/1 at " + "a" * 40)
        [row] = self.connection.execute("SELECT key, body, resolved_at FROM state WHERE kind = 'verified'").fetchall()
        self.assertEqual(row["key"], delivery_key(self.item, "a" * 40))
        self.assertEqual(json.loads(row["body"]), self.evidence(1, "a" * 40))
        self.assertIsNotNone(row["resolved_at"])  # a merge is a fact, not open work

    def test_the_read_gives_the_line_and_the_evidence_for_a_new_note_and_an_old_one(self):
        from sd_db.ship import note_merge

        old = self.evidence(1, "a" * 40)
        add_note(self.connection, self.item, "comment",
                 "Code delivery https://github.com/o/r/pull/1 at " + "a" * 40 + "\n" + json.dumps(old, sort_keys=True))
        note_merge(self.connection, self.item, self.evidence(2, "b" * 40))
        add_note(self.connection, self.item, "comment", "Code delivery https://github.com/o/r/pull/3 at " + "c" * 40)
        rows = reads.delivery_notes(self.connection, since="2000-01-01")
        bodies = [row["body"].split("\n", 1) for row in rows]
        self.assertEqual([json.loads(rest) for _, rest in bodies[:2]], [old, self.evidence(2, "b" * 40)])
        self.assertEqual(bodies[2], ["Code delivery https://github.com/o/r/pull/3 at " + "c" * 40])  # no row, no JSON

    def test_a_merge_noted_before_the_split_or_again_writes_nothing(self):
        from sd_db.ship import note_merge

        evidence = self.evidence(1, "a" * 40)
        add_note(self.connection, self.item, "comment",
                 "Code delivery https://github.com/o/r/pull/1 at " + "a" * 40 + "\n" + json.dumps(evidence))
        note_merge(self.connection, self.item, evidence)
        note_merge(self.connection, self.item, self.evidence(2, "b" * 40))
        note_merge(self.connection, self.item, self.evidence(2, "b" * 40))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM note WHERE body LIKE 'Code delivery %'").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM state WHERE kind = 'verified'").fetchone()[0], 1)

    def test_a_failed_evidence_write_leaves_no_note(self):
        """The note and its row are one transaction: a rerun finds neither and writes both."""
        from sd_db.ship import note_merge

        self.connection.execute("CREATE TRIGGER refuse BEFORE INSERT ON state BEGIN SELECT RAISE(ABORT, 'refused'); END")
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            note_merge(self.connection, self.item, self.evidence(1, "a" * 40))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM note WHERE body LIKE 'Code delivery %'").fetchone()[0], 0)
        self.connection.execute("DROP TRIGGER refuse")
        self.connection.commit()
        note_merge(self.connection, self.item, self.evidence(1, "a" * 40))
        self.assertEqual(len(reads.delivery_notes(self.connection, since="2000-01-01")), 1)

    def test_exec_note_count_counts_every_exec_note_and_nothing_else(self):
        for body in ("{}", "plain text", "{}"):
            add_note(self.connection, self.item, "exec", body)
        add_note(self.connection, self.item, "comment", "not an execution")
        self.assertEqual(reads.exec_note_count(self.connection), 3)

    def test_recent_assignments_leave_out_exec_and_older_days(self):
        from sd_db.writes import create_assignment

        author = create_assignment(self.connection, item=self.item, role="author", status="done")
        create_assignment(self.connection, item=self.item, role="exec", status="done")
        old = create_assignment(self.connection, item=self.item, role="review", status="done")
        self.connection.execute("UPDATE assignment SET started = ?, ended = ? WHERE id = ?",
                                ("2026-09-01T10:00:00Z", "2026-09-01T11:00:00Z", old))
        self.connection.execute("UPDATE assignment SET started = '2026-09-06T08:00:00Z' WHERE id = ?", (author,))
        self.connection.commit()
        self.assertEqual(reads.recent_assignments(self.connection, since="2026-09-05T12:00:00Z"), [(author, None)])

    def test_recent_assignments_carry_their_items_repository(self):
        from sd_db.writes import create_assignment

        upsert_repo(self.connection, "/repos/system", remote="git@example.invalid:x.git")
        item = create_item(self.connection, kind="work", title="In a repo", repo="/repos/system")
        run = create_assignment(self.connection, item=item, role="author", status="done")
        self.connection.execute("UPDATE assignment SET started = '2026-09-06T08:00:00Z' WHERE id = ?", (run,))
        self.connection.commit()
        self.assertIn((run, "/repos/system"), reads.recent_assignments(self.connection, since="2026-09-05T12:00:00Z"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TheAssignmentLedger(unittest.TestCase):
    """The Management page's reads (sd:2118): the ledger filters, the counts, and `ready_to_send`."""

    def setUp(self):
        from sd_db.writes import create_assignment

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "sd.db"
        initialise(path)
        self.connection = connect(path)
        self.addCleanup(self.connection.close)
        self.item = create_item(self.connection, kind="task", title="A thing")
        self.done = create_assignment(self.connection, role="merge", status="done", item=self.item)
        self.queued = create_assignment(self.connection, role="author", status="queued", item=self.item)
        self.blocked = create_assignment(self.connection, role="author", status="blocked", item=self.item)

    def test_the_filters_and_the_order(self):
        def ids(rows):
            return [row["id"] for row in rows]
        self.assertEqual(ids(reads.assignment_ledger(self.connection)), [self.blocked, self.queued, self.done])
        self.assertEqual(ids(reads.assignment_ledger(self.connection, role="merge")), [self.done])
        self.assertEqual(ids(reads.assignment_ledger(self.connection, exclude_role="merge", limit=1)), [self.blocked])
        self.assertEqual(ids(reads.assignment_ledger(self.connection, live=True)), [self.queued])
        self.assertEqual(reads.assignment_ledger(self.connection)[0]["title"], "A thing")

    def test_the_counts_and_the_items_ready_to_send(self):
        self.assertEqual(reads.assignment_counts(self.connection), {"blocked": 1, "done": 1, "queued": 1})
        self.assertEqual(reads.ready_to_send(self.connection), [])
        self.connection.execute("UPDATE item SET status = 'ready_to_send' WHERE id = ?", (self.item,))
        self.assertEqual([row["id"] for row in reads.ready_to_send(self.connection)], [self.item])


class TheSessionReads(unittest.TestCase):
    """`lane_entries` and `launch_notes`, behind the system dashboard's Working now (sd:3466)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "sd.db"
        initialise(path)
        self.connection = connect(path)
        self.addCleanup(self.connection.close)

    def test_lane_entries_are_the_latest_row_of_each_key_in_every_repository_resolved_or_not(self):
        record_state(self.connection, "checkpoint", key="lane:v1:example/a:e1", body={"status": "pending"})
        record_state(self.connection, "checkpoint", key="lane:v1:example/a:e1", body={"status": "running"})
        resolve_state(self.connection, record_state(self.connection, "checkpoint", key="lane:v1:example/b:e2",
                                                    body={"status": "merged"}))
        record_state(self.connection, "checkpoint", key="lane:v1:example/b:e3")
        record_state(self.connection, "checkpoint", key="run-7", body={"at": NOW})
        record_state(self.connection, "heartbeat", key="lane:v1:example/a:e9", body={})
        self.connection.commit()
        rows = reads.lane_entries(self.connection)
        self.assertEqual([("lane:v1:example/a:e1", '{"status": "running"}'), ("lane:v1:example/b:e2", '{"status": "merged"}'),
                          ("lane:v1:example/b:e3", None)], sorted((row["key"], row["body"]) for row in rows))

    def test_launch_notes_are_every_matching_comment_on_an_open_item_of_any_kind(self):
        task = create_item(self.connection, kind="task", title="A task", status="planning")
        report = create_item(self.connection, kind="report", title="A run report", status="planning")
        done = create_item(self.connection, kind="task", title="Finished", status="done")
        first = add_note(self.connection, task, "comment", "Fix: started a builder session in pane w1:p1")
        add_note(self.connection, task, "followup", "Fix: started a builder session in pane w1:p2")
        add_note(self.connection, task, "comment", "Unrelated: Fix: started")
        resolved = add_note(self.connection, report, "comment", "Fix: started a fix session in pane w2:p1")
        self.connection.execute("UPDATE note SET resolved_at = ? WHERE id = ?", (NOW, resolved))
        add_note(self.connection, done, "comment", "Fix: started a builder session in pane w3:p1")
        self.connection.commit()
        rows = reads.launch_notes(self.connection, "Fix: started a ")
        self.assertEqual([(first, task), (resolved, report)], [(row["id"], row["item"]) for row in rows])
        self.assertEqual("Fix: started a builder session in pane w1:p1", rows[0]["body"])


class BatchedRevisions(unittest.TestCase):
    """sd:3963. `revisions` reads many items' `item_state` revisions in one pass,
    so a screen drops its read per row; each value must be the one an edit checks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "sd.db"
        initialise(path)
        self.connection = connect(path)
        self.addCleanup(self.connection.close)

    def test_each_revision_is_the_one_the_single_read_gives(self):
        bare = create_item(self.connection, kind="task", title="No notes")
        noted = create_item(self.connection, kind="task", title="Two notes", status="planning")
        add_note(self.connection, noted, "comment", "first")
        add_note(self.connection, noted, "followup", "second")
        moved = create_item(self.connection, kind="task", title="Moved", status="planning")
        transition(self.connection, moved, "in_progress", who="operator")
        resolved = add_note(self.connection, moved, "question", "which?\nOption: Yes (Recommended)\nOption: No")
        self.connection.execute("UPDATE note SET resolved_at = ? WHERE id = ?", (NOW, resolved))
        self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (b'{"a": 1}', bare))
        self.connection.commit()
        items = [moved, bare, noted, bare]
        self.assertEqual(reads.revisions(self.connection, items),
                         {item: workflow.item_state(self.connection, item)["revision"] for item in items})

    def test_a_missing_item_is_refused_as_the_single_read_refuses_it(self):
        item = create_item(self.connection, kind="task", title="Here")
        with self.assertRaises(workflow.MissingItem) as single:
            workflow.item_state(self.connection, 999)
        with self.assertRaises(workflow.MissingItem) as batched:
            reads.revisions(self.connection, [item, 999])
        self.assertEqual(str(batched.exception), str(single.exception))

    def test_an_identifier_the_single_read_refuses_is_refused(self):
        for bad in (0, -1, "1", True, None):
            with self.subTest(bad=bad), self.assertRaises(workflow.WorkflowError):
                reads.revisions(self.connection, [bad])

    def test_no_items_is_no_revisions(self):
        self.assertEqual(reads.revisions(self.connection, []), {})

    def test_more_items_than_one_statement_may_bind_are_read_in_chunks(self):
        # 999 was SQLite's limit before 3.32; set it so the test does not depend on the build.
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        items = [create_item(self.connection, kind="task", title=f"Item {n}") for n in range(1000)]
        add_note(self.connection, items[-1], "comment", "last")
        self.connection.commit()
        found = reads.revisions(self.connection, items)
        self.assertEqual(len(found), 1000)
        self.assertEqual(found[items[-1]], workflow.item_state(self.connection, items[-1])["revision"])
        self.assertEqual(found[items[0]], workflow.item_state(self.connection, items[0])["revision"])
