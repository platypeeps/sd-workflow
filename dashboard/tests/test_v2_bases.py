"""Vault base rows on Tasks and Health (sd:3977).

`/api/tasks` gives a base row its `base`, `stage` and `score`; its Details give
its stage moves, declared fields, body and mirror section. The item routes send
a base row to `records.edit_row`, and Health shows the `records-mirror`
heartbeat, read-only: Render mirror and `POST /api/records/render` are gone (sd:5134).

Every case names a temporary vault in `OBSIDIAN_VAULT` and a plugin list
declaring `blog-idea` on the `db` driver, so no write reaches the operator's vault.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import test_v2_tasks
from sd_dashboard import documents, health_screen, tasks_screen
from sd_db import create_item, workflow
from support import NOW, ScreenCase
from test_workflow_actions import BrowserSession

BASE = "System/Databases/Blog Ideas"
BLOG_IDEA = {"inbox": "planning", "accepted": "ready", "drafting": "in_progress", "published": "done", "declined": "done"}
RULES = {"fields": ["contexts", "status", "score", "dateCreated", "description"], "initial-status": "inbox",
         "transitions": {"inbox": ["accepted", "declined"], "accepted": ["drafting", "declined"],
                         "drafting": ["published", "declined"]}}


def entry(driver):
    """One `sd plugin list --json` entry declaring `blog-idea`, with the pack's `rules`."""
    return {"root": "/plugins/sdw", "readable": True, "prefix": "sdw",
            "store": {"driver": driver, "root": "$OBSIDIAN_VAULT", "bases": {"blog-idea": BASE}},
            "workflow": {"blog-idea": {"status": dict(BLOG_IDEA)}}, "rules": {"blog-idea": json.loads(json.dumps(RULES))}}


class Bases:
    """A temporary vault, the rules for it, and a Documents root serving its `docs/dashboard`."""

    driver = "db"

    def setUp(self):
        super().setUp()
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / BASE).mkdir(parents=True)
        served = documents.Root("vault", "Vault", self.vault / "docs" / "dashboard")
        for patcher in (patch.dict(os.environ, {"OBSIDIAN_VAULT": str(self.vault)}),
                        patch("sd_db.records.plugin_entries", return_value=[entry(self.driver)]),
                        patch.object(documents, "roots", return_value=[served])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.idea = self.base_row("An idea", stage="inbox", score="7")

    def base_row(self, title, *, stage, score):
        return create_item(self.connection, kind="idea", title=title, status=BLOG_IDEA[stage], source="vault",
                           external_id=f"blog-idea:{BASE}/{title}.md", path=f"{BASE}/{title}.md", stage=stage,
                           fields={"kind": "blog-idea", "score": score, "dateCreated": "2026-06-15",
                                   "description": "why it matters"},
                           body={"markdown": "the idea's body"}, created_at="2026-06-15")


class TheRows(Bases, ScreenCase):
    def test_a_base_row_carries_its_base_stage_and_score_and_no_status_move(self):
        task = self.item("Plan the review", kind="task")
        rows = {row["id"]: row for row in tasks_screen.document(self.connection, now=NOW)["rows"]}
        self.assertEqual({key: rows[self.idea][key] for key in ("base", "stage", "score", "allowed")},
                         {"base": "blog-idea", "stage": "inbox", "score": "7", "allowed": []})
        self.assertEqual([rows[task][key] for key in ("base", "stage", "score")], [None, None, None])


class TheDetails(Bases, ScreenCase):
    def test_a_base_row_shows_its_moves_fields_body_and_mirror_section(self):
        base = tasks_screen.details(self.connection, self.idea, now=NOW)["base"]
        self.assertEqual(base, {
            "kind": "blog-idea", "stage": "inbox", "stages": ["accepted", "declined"],
            "declared": ["contexts", "score", "dateCreated", "description"],
            "fields": {"score": "7", "dateCreated": "2026-06-15", "description": "why it matters"}, "body": "the idea's body", "reason": None,
            "mirror": {"path": f"{self.vault / 'docs/dashboard/base-blog-idea.html'}#item-{self.idea}",
                       "url": f"/documents/vault/base-blog-idea.html#item-{self.idea}"}})

    def test_any_other_row_has_no_base_panel(self):
        task = self.item("Plan the review", kind="task")
        self.assertIsNone(tasks_screen.details(self.connection, task, now=NOW)["base"])

    def test_rules_that_cannot_be_read_offer_no_move_and_say_why(self):
        with patch("sd_db.records.plugin_entries", return_value=[]):
            base = tasks_screen.details(self.connection, self.idea, now=NOW)["base"]
        self.assertEqual((base["stages"], base["mirror"]), ([], None))
        self.assertIn("0 registered plugins declare a `workflow` for kind 'blog-idea'", base["reason"])


class StillOnTheVaultDriver(Bases, ScreenCase):
    driver = "vault"

    def test_a_kind_the_vault_still_keeps_offers_no_move(self):
        base = tasks_screen.details(self.connection, self.idea, now=NOW)["base"]
        self.assertEqual((base["stages"], base["reason"]), ([], "plugin sdw still keeps blog-idea rows in the vault: edit the note"))


class TheRoutes(Bases, BrowserSession):
    def revision(self, item):
        return workflow.item_state(self.connection, item)["revision"]

    def notes(self, item):
        return [(note["kind"], note["body"]) for note in workflow.item_state(self.connection, item)["notes"]]

    def test_a_stage_move_writes_through_records_with_a_comment_and_a_status_change(self):
        status, _, out = self.post(f"/api/items/{self.idea}/stage", {"stage": "accepted", "revision": self.revision(self.idea)})
        self.assertEqual(status, 200, out)
        self.assertEqual((out["item"]["stage"], out["item"]["status"]), ("accepted", "ready"))
        self.assertTrue(out["mirror"]["ok"], out["mirror"])
        self.assertIn(("comment", "Stage inbox -> accepted by dashboard"), self.notes(self.idea))
        self.assertEqual(self.notes(self.idea)[-1][0], "status_change")
        # The after-write render wrote the vault mirror the rules name: the temporary one.
        self.assertTrue((self.vault / "docs/dashboard/base-blog-idea.html").is_file())

    def test_a_move_the_graph_refuses_writes_nothing(self):
        before = self.snapshot()
        status, _, out = self.post(f"/api/items/{self.idea}/stage", {"stage": "drafting", "revision": self.revision(self.idea)})
        self.assertEqual(status, 400, out)
        self.assertEqual(self.snapshot(), before)

    def test_a_base_row_stage_move_takes_the_stage_only(self):
        status, _, out = self.post(f"/api/items/{self.idea}/stage",
                                   {"stage": "accepted", "reason": "why", "revision": self.revision(self.idea)})
        self.assertEqual((status, out["error"]), (400, "A base row's stage move takes the stage only."))

    def test_a_published_stage_is_a_base_row_move_and_not_a_publication(self):
        drafting = self.base_row("Drafting", stage="drafting", score="8")
        status, _, out = self.post(f"/api/items/{drafting}/stage", {"stage": "published", "revision": self.revision(drafting)})
        self.assertEqual((status, out["item"]["stage"]), (200, "published"), out)

    def test_an_edit_sets_declared_fields_and_the_body_through_records(self):
        status, _, out = self.post(f"/api/items/{self.idea}",
                                   {"fields": {"score": "9"}, "body": "a new body", "revision": self.revision(self.idea)})
        self.assertEqual(status, 200, out)
        self.assertEqual(json.loads(out["item"]["fields"])["score"], "9")
        self.assertIn(("comment", "Set score, body by dashboard"), self.notes(self.idea))
        status, _, out = self.post(f"/api/items/{self.idea}", {"title": "Renamed", "revision": self.revision(self.idea)})
        self.assertEqual((status, out["error"]), (400, "A base row takes a stage, its declared fields or its body."))

    def test_a_stale_revision_is_a_conflict(self):
        status, _, out = self.post(f"/api/items/{self.idea}/stage", {"stage": "accepted", "revision": "0" * 64})
        self.assertEqual(status, 409, out)

    def test_other_rows_keep_their_handlers(self):
        task = self.item("Plan the review", kind="task")
        status, _, out = self.post(f"/api/items/{task}", {"priority": 1, "revision": self.revision(task)})
        self.assertEqual((status, out["item"]["priority"]), (200, 1))
        status, _, out = self.post(f"/api/items/{task}/stage", {"stage": "published", "revision": self.revision(task)})
        self.assertEqual((status, out["error"]), (400, "Publication uses the reviewed publishing workflow."))

    def test_render_mirror_is_gone_and_writes_nothing(self):
        """sd:5134: the dashboard writes no vault note; the route that rendered the mirror is gone."""
        before = self.snapshot()
        status, _, out = self.post("/api/records/render", {})
        self.assertEqual(status, 404, out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.vault / "docs/dashboard/base-blog-idea.html").exists())


class TheHeartbeat(Bases, ScreenCase):
    def rows(self):
        rows, _ = health_screen._mirror_rows(health_screen.read_mirror(self.connection))
        return rows

    def test_no_render_yet_is_unknown_never_clean(self):
        (row,) = self.rows()
        self.assertEqual((row["id"], row["type"], row["state"]), ("mir", "vault mirror", "unknown"))

    def test_a_failed_render_is_a_warning_with_its_reason_and_a_clean_one_is_ok(self):
        from sd_db import records

        # Pack #1575 renders only the HTML pages, which need the vault root and no base folder.
        moved = self.vault.with_name("moved")
        self.vault.rename(moved)
        records.render(self.connection, home=Path(self.tmp.name))
        (row,) = self.rows()
        self.assertEqual((row["state"], row["what"]), ("warning", "Vault mirror not updated"))
        self.assertIn("not a directory", row["detail"])
        moved.rename(self.vault)
        records.render(self.connection, home=Path(self.tmp.name))
        (row,) = self.rows()
        self.assertEqual((row["state"], row["facts"]["Kinds"]), ("ok", "blog-idea"))


class TheBulkDecline(Bases, ScreenCase):
    """sd:8014: the bulk Decline moves the stale inbox rows the page showed to declined, by the operator's click alone,
    and the declined rows stay listed with when and by which control."""

    def seed(self, title, score, created):
        return create_item(self.connection, kind="idea", title=title, status="planning", source="vault",
                           external_id=f"blog-idea:{BASE}/{title}.md", path=f"{BASE}/{title}.md", stage="inbox",
                           fields={"kind": "blog-idea", "score": score, "dateCreated": created}, created_at=created)

    def setUp(self):
        super().setUp()
        from datetime import date

        self.low = self.seed("Low and old", "3", "2026-01-02")
        self.unscored = self.seed("Unscored and old", None, "2026-01-03")
        self.high = self.seed("High and old", "8", "2026-01-04")
        self.fresh = self.seed("Low and new", "2", date.today().isoformat())

    def test_the_pool_lists_each_waiting_row_with_its_score_age_and_revision(self):
        from sd_dashboard import triage

        pool = {row["title"]: row for row in triage.pool(self.connection, "blog-idea", now=NOW)}
        self.assertEqual(set(pool), {"An idea", "Low and old", "Unscored and old", "High and old", "Low and new"})
        self.assertEqual((pool["Low and old"]["score"], pool["Low and old"]["age"]), (3.0, 247))
        self.assertEqual(pool["Low and old"]["revision"], workflow.item_state(self.connection, self.low)["revision"])

    def test_decline_moves_the_matching_rows_refuses_the_rest_and_lists_them_declined(self):
        from sd_dashboard import triage

        shown = [self.low, self.unscored, self.high, self.fresh]
        items = [{"item": n, "revision": workflow.item_state(self.connection, n)["revision"]} for n in shown]
        out = triage.decline({"kind": "blog-idea", "score": 6, "days": 30, "items": items}, "local")(self.connection)
        self.assertEqual(out["declined"], [self.low, self.unscored])
        self.assertEqual([row["item"] for row in out["refused"]], [self.high, self.fresh])
        stages = {n: workflow.item_state(self.connection, n)["item"]["stage"] for n in shown}
        self.assertEqual(stages, {self.low: "declined", self.unscored: "declined", self.high: "inbox", self.fresh: "inbox"})
        listed = triage.declined(self.connection, "blog-idea")
        self.assertEqual({row["item"] for row in listed}, {self.low, self.unscored})
        self.assertEqual({row["by"] for row in listed}, {"bulk Decline (score below 6, older than 30 days)"})
        self.assertTrue(all(row["at"] for row in listed))

    def test_a_row_that_moved_since_the_page_read_it_is_refused(self):
        from sd_dashboard import triage

        out = triage.decline({"kind": "blog-idea", "score": 6, "days": 30, "items": [{"item": self.low, "revision": "0" * 64}]},
                             "local")(self.connection)
        self.assertEqual((out["declined"], [row["item"] for row in out["refused"]]), ([], [self.low]))
        self.assertEqual(workflow.item_state(self.connection, self.low)["item"]["stage"], "inbox")

    def test_a_malformed_post_is_refused_before_any_write(self):
        from sd_dashboard import triage

        for payload in ({"kind": "topic", "score": 6, "days": 30, "items": [{"item": 1, "revision": "r"}]},
                        {"kind": "blog-idea", "score": "nan", "days": 30, "items": [{"item": 1, "revision": "r"}]},
                        {"kind": "blog-idea", "score": 6, "days": 30.5, "items": [{"item": 1, "revision": "r"}]},
                        {"kind": "blog-idea", "score": 6, "days": 30, "items": []}):
            with self.assertRaises(ValueError):
                triage.decline(payload, "local")

    def test_the_route_is_a_writing_page_write(self):
        from sd_dashboard.v2.pages import writing

        route = next(api for api in writing.PAGE.api if api.path == "/api/triage/decline")
        items = [{"item": self.low, "revision": workflow.item_state(self.connection, self.low)["revision"]}]
        out = route.write({"kind": "blog-idea", "score": 6, "days": 30, "items": items}, "local")(self.connection)
        self.assertEqual(out["declined"], [self.low])


class TheScriptForBases(Bases, ScreenCase):
    """tasks.js with a base row: the Base filter, the Stage and Score columns, and the stage select."""

    run_page = test_v2_tasks.TheScript.run_page

    def setUp(self):
        super().setUp()
        self.task = self.item("Plan the review", kind="task")
        self.doc = tasks_screen.document(self.connection, now=NOW)
        self.details = {str(i): tasks_screen.details(self.connection, i, now=NOW) for i in (self.idea, self.task)}

    def test_the_base_filter_the_stage_column_and_the_score_sort(self):
        out = self.run_page("""R.filters = ELS.filters.html;
ELS.filters.listeners.change[0]({ target: { dataset: { pick: 'kind' }, value: 'base:blog-idea' } });
R.board = ELS['view-board'].html; R.url = OUT.urls[OUT.urls.length - 1];""")
        # One Kind filter holds the plugin kinds (sd:5644).
        self.assertIn('<option value="base:blog-idea">blog-idea (plugin kind)</option></select>', out["R"]["filters"])
        self.assertIn("An idea", out["R"]["board"])
        self.assertNotIn("Plan the review", out["R"]["board"])
        self.assertIn("kind=base%3Ablog-idea", out["R"]["url"])
        # pack sd:6743: an idea lists only when Kind picks it, here beside the tasks.
        out = self.run_page("R.list = ELS['view-list'].html;", search="?view=list&sort=score&kind=task,base:blog-idea")
        self.assertRegex(out["R"]["list"], r"An idea</button><span class=\"fold mono\">[^<]*</span></td><td class=\"mono rp\"><span[^>]*>[^<]*</span></td>"
                         r"<td class=\"st\">Planning</td><td>inbox<span class=\"fold mono\">score 7</span></td><td class=\"mono sc\">7</td>")
        self.assertLess(out["R"]["list"].index("An idea"), out["R"]["list"].index("Plan the review"))

    def test_every_base_field_label_carries_help(self):
        # sd:5903: the Base section's stage, declared fields and mirror carry the shared help icon, as Tasks' fields do.
        out = self.run_page(f"open({self.idea}); await flush(); R.det = ELS.details.html;")
        fields = test_v2_tasks.field_help(out["R"]["det"])
        self.assertTrue({"Stage", "Mirror"} <= {name for name, _ in fields}, fields)
        self.assertEqual([name for name, helped in fields if not helped], [])

    def test_the_stage_select_posts_the_move_and_names_a_render_that_failed(self):
        echo = """(path, body) => { const row = DOC.rows.find(r => path.startsWith('/api/items/' + r.id));
  return [200, { item: { id: row.id, status: 'ready', stage: body.stage, priority: null, due: null, recurrence: null, title: row.title, repo: null },
    notes: [], revision: '1'.repeat(64), mirror: { ok: false, reason: 'the base folder is gone' } }]; }"""
        out = self.run_page(test_v2_tasks.TheScript.CHANGE + f"""open({self.idea}); await flush(); R.det = ELS.details.html;
change('det-stage', 'accepted'); await flush();""", echo)
        self.assertRegex(out["R"]["det"], r'<select id="det-stage" class="fld"><option value="inbox" selected>inbox</option>'
                                          r'<option value="accepted">accepted</option><option value="declined">declined</option></select>')
        self.assertIn(f'href="/documents/vault/base-blog-idea.html#item-{self.idea}"', out["R"]["det"])
        self.assertIn("the idea&#39;s body", out["R"]["det"])
        self.assertEqual([(p[0], {k: v for k, v in p[1].items() if k != "revision"}) for p in out["posts"]],
                         [(f"/api/items/{self.idea}/stage", {"stage": "accepted"})])
        self.assertIn(f"#{self.idea} stage → accepted · mirror not updated: the base folder is gone", [t[0] for t in out["toasts"]])
