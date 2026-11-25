"""Real HTTP controls, with disposable registry/files and no provider processes."""
import json
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import controls
from sd_db import (
    connect,
    create_assignment,
    provider_controls,
    registry,
    reporting,
    skills_catalog,
    workflow,
)
from sd_db.writes import add_note, set_item_fields, upsert_repo

from .test_capture_screen import Fields
from .test_workflow_actions import BrowserSession

REGISTRY = """bills:
  a: {cost: subscription}
  b: {cost: subscription}
  c: {cost: plan}
providers:
  claude: {start: "claude -p", vendor: anthropic, bill: a, roles: [author, reviewer], reader: claude-json}
  codex: {start: "codex exec", vendor: openai, bill: b, roles: [author, reviewer], reader: codex-json}
  minimax: {url: "https://example.invalid/v1", model: fixture, vendor: minimax, bill: c, roles: [reviewer]}
roles:
  author: [claude, codex]
  reviewer: [codex, claude, minimax]
"""

class WorkflowControls(BrowserSession):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name).resolve()
        provider_file = root / "providers.yaml"
        provider_file.write_text(REGISTRY)
        original = registry.read
        replacement = patch.object(registry, "read", side_effect=lambda path=None, **kw: original(path or provider_file, **kw))
        replacement.start()
        self.addCleanup(replacement.stop)
        self.pack = root / "pack"
        (self.pack / "skills").mkdir(parents=True)
        (self.pack / "skills/paths.json").write_text(json.dumps({"paths": {"build": {"skills": []}}}))
        (self.pack / "contrib/sd-fixture").mkdir(parents=True)
        (self.pack / "contrib/sd-fixture/SKILL.md").write_text("---\nname: sd-fixture\ndescription: A fixture skill.\n---\nUse for testing.\n")
        replacement = patch.object(skills_catalog, "location", return_value=(self.pack, {}))
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_every_new_route_refuses_bad_session_before_work(self):
        before = self.snapshot()
        for path in ("/api/providers/configure", "/api/bills/c/cap", "/api/skills/sd-fixture/try", "/api/skill-reviews/1/apply", "/api/reports/1/acknowledge"):
            self.assertEqual(self.post(path, {}, **{"X-SD-CSRF": "0" * 64})[0], 403, path)
            self.assertEqual(self.request(path)[0], 404, path)
        self.assertEqual(before, self.snapshot())

    def test_provider_atomic_valid_alternate_and_stale_refusal(self):
        state = provider_controls.snapshot(self.connection)
        enabled = {entry["name"]: entry["enabled"] for entry in state["providers"]}
        enabled["codex"] = False
        payload = {"revision": state["revision"], "enabled": enabled, "orders": state["orders"]}
        before = self.snapshot()
        self.assertEqual(self.post("/api/providers/configure", payload)[0], 400)
        self.assertEqual(before, self.snapshot())
        payload["orders"]["reviewer"] = ["minimax", "codex", "claude"]
        self.assertEqual(self.post("/api/providers/configure", payload)[0], 200)
        before = self.snapshot()
        self.assertEqual(self.post("/api/providers/configure", payload)[0], 409)
        self.assertEqual(before, self.snapshot())

    def test_provider_subset_round_trips_without_disabling_explicit_provider(self):
        state = provider_controls.snapshot(self.connection)
        payload = {"revision": state["revision"],
            "enabled": {entry["name"]: entry["enabled"] for entry in state["providers"]},
            "orders": {**state["orders"], "reviewer": ["codex", "claude"]}}
        status, _, result = self.post("/api/providers/configure", payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["orders"], payload["orders"])
        minimax = next(entry for entry in result["providers"] if entry["name"] == "minimax")
        self.assertTrue(minimax["enabled"])
        self.assertEqual(minimax["roles"], ["reviewer"])
        self.assertEqual(minimax["ranks"], {})
        self.assertEqual(provider_controls.snapshot(self.connection), result)
        before = self.snapshot()
        self.assertEqual(self.post("/api/providers/configure", payload)[0], 409)
        self.assertEqual(self.snapshot(), before)

    START_BILL_SENTENCE = "provider 'claude' is a 'start' entry on the capped bill 'a'"

    def cap_of(self, state, name):
        return next(bill["cap_usd_month"] for bill in state["bills"] if bill["name"] == name)

    def test_a_cap_posted_on_the_url_bill_is_the_row_and_null_clears_it(self):
        """Slice 12b (sd:234): the route writes through `set_cap` with the
        dashboard's name; the answer is the snapshot, the row carries the
        number, the file's hash is unchanged, and `null` clears it."""
        state = provider_controls.snapshot(self.connection)
        self.assertIsNone(self.cap_of(state, "c"))
        status, _, result = self.post("/api/bills/c/cap", {"revision": state["revision"], "cap_usd_month": 12.5})
        self.assertEqual(status, 200)
        self.assertEqual(self.cap_of(result, "c"), 12.5)
        current = provider_controls.snapshot(self.connection)
        self.assertEqual(self.cap_of(current, "c"), 12.5)
        self.assertEqual(current["revision"], result["revision"])
        self.assertEqual(current["configuration_sha256"], state["configuration_sha256"])
        self.assertEqual(self.connection.execute("SELECT cap_usd_month FROM bill WHERE name='c'").fetchone()[0], 12.5)
        status, _, result = self.post("/api/bills/c/cap", {"revision": current["revision"], "cap_usd_month": None})
        self.assertEqual(status, 200)
        self.assertIsNone(self.cap_of(result, "c"))
        self.assertIsNone(self.connection.execute("SELECT cap_usd_month FROM bill WHERE name='c'").fetchone()[0])

    def test_a_cap_on_a_start_bill_is_the_readers_refusal_and_writes_nothing(self):
        """Clause 15.15 over HTTP: 400, the reader's sentence in the body,
        the snapshot's revision unmoved."""
        state = provider_controls.snapshot(self.connection)
        before = self.snapshot()
        status, _, result = self.post("/api/bills/a/cap", {"revision": state["revision"], "cap_usd_month": 5.0})
        self.assertEqual(status, 400)
        self.assertIn(self.START_BILL_SENTENCE, result["error"])
        self.assertIn("nothing enforces", result["error"])
        self.assertEqual(provider_controls.snapshot(self.connection)["revision"], state["revision"])
        self.assertEqual(before, self.snapshot())

    def test_a_stale_revision_an_unknown_bill_and_a_bad_number_are_refused_by_status(self):
        state = provider_controls.snapshot(self.connection)
        self.assertEqual(self.post("/api/bills/c/cap", {"revision": state["revision"], "cap_usd_month": 1.0})[0], 200)
        before = self.snapshot()
        status, _, result = self.post("/api/bills/c/cap", {"revision": state["revision"], "cap_usd_month": 2.0})
        self.assertEqual(status, 409)
        self.assertTrue(result["reload"])
        current = provider_controls.snapshot(self.connection)
        status, _, result = self.post("/api/bills/d/cap", {"revision": current["revision"], "cap_usd_month": 2.0})
        self.assertEqual(status, 400)
        self.assertIn("no bill 'd'", result["error"])
        for bad in (-1.0, "12", True, {"usd": 1}):
            status, _, result = self.post("/api/bills/c/cap", {"revision": current["revision"], "cap_usd_month": bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("finite, non-negative number", result["error"])
        self.assertEqual(self.post("/api/bills/c/cap", {"revision": current["revision"]})[0], 400)
        self.assertEqual(self.post("/api/bills/c/cap", {"revision": current["revision"], "cap_usd_month": 2.0, "who": "x"})[0], 400)
        self.assertEqual(self.post("/api/bills/c%2F..%2Fx/cap", {"revision": current["revision"], "cap_usd_month": 2.0})[0], 404)
        # The bound is the services route's, 200 characters (#435's verification
        # round): at it the name reaches the library, which has no such bill;
        # past it the route answers 404 before any connection is opened.
        status, _, result = self.post("/api/bills/" + "x" * 200 + "/cap", {"revision": current["revision"], "cap_usd_month": 2.0})
        self.assertEqual(status, 400)
        self.assertIn("no bill 'xxx", result["error"])
        self.assertEqual(self.post("/api/bills/" + "x" * 201 + "/cap", {"revision": current["revision"], "cap_usd_month": 2.0})[0], 404)
        self.assertEqual(before, self.snapshot())

    def test_a_string_cap_is_the_ledgers_refusal_and_the_cap_stays(self):
        """The route hands a string straight to `set_cap`, and `ledger._money`
        refuses it with 400; nothing is cleared."""
        state = provider_controls.snapshot(self.connection)
        self.assertEqual(self.post("/api/bills/c/cap", {"revision": state["revision"], "cap_usd_month": 12.5})[0], 200)
        current = provider_controls.snapshot(self.connection)
        before = self.snapshot()
        status, _, result = self.post("/api/bills/c/cap", {"revision": current["revision"], "cap_usd_month": "abc"})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "a cap must be a finite, non-negative number; bill 'c' was given 'abc'")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.cap_of(provider_controls.snapshot(self.connection), "c"), 12.5)

    def test_the_stopped_runners_routes_are_gone(self):
        """sd:3041: the runner stopped, so its run, prepare and control routes answer 404 and write nothing."""
        self.repo()
        item = self.item("Ready", kind="task", repo="/repos/system", branch="task/ready", status="ready")
        revision = workflow.item_state(self.connection, item)["revision"]
        before = self.snapshot()
        for path, body in (("/api/run", {"items": [item], "revisions": {str(item): revision}}),
                           (f"/api/items/{item}/prepare", {"revision": revision, "repo": "/repos/system", "branch": "x"}),
                           ("/api/runner/1/cancel", {"revision": "0" * 64}), ("/api/runner/1/requeue", {"revision": "0" * 64}),
                           ("/api/runner/1/resume", {"revision": "0" * 64}),
                           ("/api/runner/1/restore", {"revision": "0" * 64, "destination": "/tmp/x"})):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, body)[0], 404)
        self.assertEqual(before, self.snapshot())
        self.assertNotIn('action="/api/run"', self.request(f"/item/{item}")[2])

    def test_catalog_and_new_operations_reads_do_not_write(self):
        before = self.snapshot()
        with patch("subprocess.run", side_effect=AssertionError("catalog invoked a process")):
            self.assertEqual(self.request("/api/skills", headers={"Cookie": self.cookie})[0], 200)
        self.assertEqual(self.request("/static/icon.svg")[0], 200)
        self.assertEqual(before, self.snapshot())
        skill = skills_catalog.catalog(self.connection)["skills"][0]
        status, _, _ = self.post("/api/skills/sd-fixture/try", {"revision": skill["revision"]})
        self.assertEqual(status, 200)
        before = self.snapshot()
        self.assertEqual(self.post("/api/skills/sd-fixture/try", {"revision": skill["revision"]})[0], 409)
        self.assertEqual(before, self.snapshot())

    def test_report_followups_gate_acknowledgement(self):
        """A followup a person wrote still refuses the dashboard's acknowledge, named in the error, and the
        ingest's own followup stays open with it (sd:3533, sd:4755). The pack's `resolve_followups` would close
        both, so the one-click route checks first and never resolves a person's work."""
        state = reporting.ingest(self.connection, job="fixture", run_id="one", started="2026-09-08T00:00:00Z", ended="2026-09-08T00:01:00Z", exit_code=1, text="Failed", source_path="/fixture/log", attention=True)
        item = state["item"]["id"]
        person = add_note(self.connection, item, "followup", "look at this first", session="operator")
        # Another subject: `session` alone is not the ingest's mark, so a note that only claims "cron" refuses too.
        forged = add_note(self.connection, item, "followup", "claims the ingest's session", session="cron")
        state = workflow.item_state(self.connection, item)
        before = self.snapshot()
        status, _, body = self.post(f"/api/reports/{item}/acknowledge", {"revision": state["revision"]})
        self.assertEqual(status, 400)
        self.assertIn(f"resolve followup {person}", json.dumps(body))
        self.assertIn(f"resolve followup {forged}", json.dumps(body))
        self.assertEqual(before, self.snapshot())
        self.assertIn("Report source", self.request(f"/item/{item}")[2])

    def test_acknowledge_finishes_a_failure_report_and_its_ingest_followup(self):
        """sd:3533: every failure report is born with the ingest's followup, so
        a route that leaves it open can finish none of them."""
        state = reporting.ingest(self.connection, job="fixture", run_id="one", started="2026-09-08T00:00:00Z", ended="2026-09-08T00:01:00Z", exit_code=1, text="Failed", source_path="/fixture/log", attention=True)
        item = state["item"]["id"]
        self.assertEqual(self.post(f"/api/reports/{item}/acknowledge", {"revision": state["revision"]})[0], 200)
        after = workflow.item_state(self.connection, item)
        self.assertEqual(after["item"]["status"], "done")
        self.assertEqual([note["resolved_at"] is not None for note in after["notes"] if note["kind"] == "followup"], [True])

    def test_a_folded_report_shows_its_repeats_and_a_single_one_does_not(self):
        """sd:920 -- the count and the last run are what the operator needs
        from a job that fails the same way on a schedule; one `Repeats` row
        in the provenance list, only when `fields.repeats` exists."""
        runs = dict(started="2026-09-08T00:00:00Z", exit_code=1, text="Failed", source_path="/fixture/log", attention=True)
        state = reporting.ingest(self.connection, job="fixture", run_id="one", ended="2026-09-08T00:01:00Z", **runs)
        item = state["item"]["id"]
        page = self.request(f"/item/{item}")[2]
        self.assertIn("Report source", page)
        self.assertNotIn("Repeats", page)
        for run, ended in (("two", "2026-09-08T00:16:00Z"), ("three", "2026-09-08T00:31:00Z")):
            self.assertEqual(reporting.ingest(self.connection, job="fixture", run_id=run, ended=ended, **runs)["item"]["id"], item)
        page = self.request(f"/item/{item}")[2]
        self.assertIn("<dt>Repeats</dt><dd>3 runs, last 2026-09-08T00:31:00+00:00</dd>", page)
        self.assertIn("<dt>Ended</dt><dd>2026-09-08T00:01:00+00:00</dd>", page)
        self.assertEqual(page.count("Report source"), 1)

    def test_a_report_whose_fields_are_not_json_renders_with_its_controls_disabled(self):
        """sd:775 -- `item.fields` carries no `json_valid` check, so the page
        must survive a row it cannot parse.

        Unguarded, `report_controls` raised `JSONDecodeError` through
        `screens.item`; `do_GET` catches `MissingItem`, `NotFound` and the
        database errors and not `ValueError`, so the reader got no response at
        all. Fail closed: the page renders, the reason is on it, and the
        acknowledge form is withheld rather than offered against a source
        nothing could read. `reporting.acknowledge` never parses `fields`, so
        the command line the notice names still finishes the report.
        """
        state = reporting.ingest(self.connection, job="fixture", run_id="two", started="2026-09-08T00:00:00Z", ended="2026-09-08T00:01:00Z", exit_code=0, text="Fine", source_path="/fixture/log")
        item = state["item"]["id"]
        self.connection.execute("UPDATE item SET fields=? WHERE id=?", ("{not json", item))
        self.connection.commit()
        status, _, page = self.request(f"/item/{item}")
        self.assertEqual(status, 200)
        self.assertIn("Report source", page)
        self.assertIn("not valid JSON", page)
        self.assertNotIn(f'action="/api/reports/{item}/acknowledge"', page)
        self.assertNotIn("Acknowledge report", page)
        revision = workflow.item_state(self.connection, item)["revision"]
        finished = reporting.acknowledge(self.connection, item, expected_revision=revision, who="fixture")
        self.assertEqual(finished["item"]["status"], "done")

    def test_absent_empty_and_malformed_fields_withhold_the_acknowledge_alike(self):
        """sd:873 -- one meaning for a `fields` that cannot say the report is
        clean: NULL, '' and `{not json` are one case, here as at retention,
        the bulk clean and the backlog (`reporting.UNREADABLE_FIELDS`).

        Before, `json.loads(row["fields"] or "{}")` turned NULL and '' into an
        empty document, so the page rendered every provenance field as "Not
        recorded" and offered the one-click acknowledge -- the control that
        would close, unexamined, the row retention and the bulk clean had
        declined so that a person would look at it. This page is that
        person's surface: it renders the row, says why it is here, and leaves
        the finishing to the command line, whose acknowledge names a `who`.

        `NaN` and `{"a": Infinity}` are the review's case: `json.loads`
        accepts those constants and SQLite's `json_valid` does not, so a row
        holding either was unreadable to retention and the bulk clean while
        the page parsed it and offered the form. The two nested arrays are
        the verification review's: SQLite's `json_valid` stops at its depth
        limit (1000 in 3.53), Python's decoder reads on to its own recursion
        limit, so 1100 deep the page parsed what retention refused and 10000
        deep it raised `RecursionError` past `do_GET`, and the reader got no
        response. The page now asks SQLite first, so its verdict is theirs.
        """
        for index, value in enumerate((None, "", "{not json", "NaN", '{"a": Infinity}',
                                       "[" * 1100 + "]" * 1100, "[" * 10000 + "]" * 10000)):
            with self.subTest(fields=value[:24] if isinstance(value, str) else value):
                state = reporting.ingest(self.connection, job="fixture", run_id=f"absent-{index}",
                                         started="2026-09-08T00:00:00Z", ended="2026-09-08T00:01:00Z",
                                         exit_code=0, text="Fine", source_path="/fixture/log")
                item = state["item"]["id"]
                self.connection.execute("UPDATE item SET fields=? WHERE id=?", (value, item))
                self.connection.commit()
                status, _, page = self.request(f"/item/{item}")
                self.assertEqual(status, 200)
                self.assertIn("Report source", page)
                self.assertIn("not valid JSON", page)
                self.assertIn(f"Read its log, then acknowledge report {item} in a terminal: ", page)
                self.assertIn(f"sd reports acknowledge {item}", page)
                self.assertNotIn("Not recorded", page)
                self.assertNotIn(f'action="/api/reports/{item}/acknowledge"', page)
                self.assertNotIn("Acknowledge report", page)
                revision = workflow.item_state(self.connection, item)["revision"]
                finished = reporting.acknowledge(self.connection, item, expected_revision=revision, who="fixture")
                self.assertEqual(finished["item"]["status"], "done")


class ItemRepository(BrowserSession):
    """sd:452 -- the item screen's change set matches `sd task edit`'s.

    `repo` was the one `workflow.USER_FIELDS` member the screen never offered,
    so a task could only be attached to or detached from a checkout from the
    CLI. The write path already existed; what is new is the widget.
    """

    def details_form(self, item):
        """The `Save details` form only, so a whole-page parse proves nothing about another form's field."""
        panel = str(controls.item_controls(self.connection, item))
        return re.search(rf'<form[^>]*action="/api/items/{item}"[^>]*>.*?</form>', panel).group(0)

    def test_the_change_set_matches_sd_task_edit_and_the_blank_choice_is_first(self):
        remote_less = "/repos/no-remote"
        upsert_repo(self.connection, remote_less)
        self.repo("/repos/system")
        item = self.item("Unattached", kind="task")
        markup = self.details_form(item)
        parsed = Fields(markup)

        # The emitted field list, which is the deliverable. `revision` is the
        # hidden stale-edit guard; the rest are `workflow.USER_FIELDS`.
        self.assertEqual(sorted(parsed.fields), ["body", "due", "kind", "priority", "repo", "revision", "title"])
        self.assertEqual(sorted(workflow.USER_FIELDS | {"revision"}), sorted(parsed.fields))
        self.assertIn("data-changed-fields", markup)

        options = parsed.options["repo"]
        self.assertEqual([entry["attributes"]["value"] for entry in options],
                         ["", remote_less, "/repos/system"])
        self.assertEqual(options[0]["text"], "No repository")
        # A checkout with no remote is still a checkout a task can sit in.
        self.assertIn(remote_less, [entry["attributes"]["value"] for entry in options])

        # Nothing is marked selected, so the browser selects the first option
        # and `dataset.initialValue` becomes "" -- an unchanged field, which
        # `data-changed-fields` omits rather than sending as a null.
        self.assertEqual([entry for entry in options if "selected" in entry["attributes"]], [])

    def test_set_then_clear_then_an_unrelated_edit_are_three_different_payloads(self):
        self.repo("/repos/system")
        item = self.item("Moves house", kind="task")
        self.assertIsNone(workflow.item_state(self.connection, item)["item"]["repo"])

        # 1. Set: the value the widget submits when a path is chosen.
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "repo": "/repos/system"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["repo"], "/repos/system")
        self.assertIn('value="/repos/system" selected', self.details_form(item))

        # 2. Clear: `dashboard.js` turns the blank option's "" into JSON null,
        # the same write the CLI's `--no-repo` makes.
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "repo": None})
        self.assertEqual(status, 200)
        self.assertIsNone(state["item"]["repo"])

        # 3. Leave it alone: an *absent* key, which is not a null. An omitted
        # key never reaches `workflow._fields`, so a title edit on a task that
        # has a repository must not detach it.
        revision = workflow.item_state(self.connection, item)["revision"]
        self.assertEqual(self.post(f"/api/items/{item}",
            {"revision": revision, "repo": "/repos/system"})[0], 200)
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "title": "Renamed"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["title"], "Renamed")
        self.assertEqual(state["item"]["repo"], "/repos/system")

    def test_the_option_set_already_contains_every_path_a_task_row_can_hold(self):
        """Why there is no `No longer registered` fallback option.

        `priority` needs one -- `item.priority` has no constraint, so the
        column holds values the form never offered and `item_controls` appends
        the stored one. `item.repo` is `REFERENCES repo(path) ON DELETE
        RESTRICT`: the database refuses to strand a task on a path it does not
        hold, so `_checkouts` enumerating the table is already complete.
        """
        self.repo("/repos/gone")
        item = self.item("Filed against a checkout", kind="task", repo="/repos/gone")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM repo WHERE path = '/repos/gone'")
        self.connection.rollback()
        options = Fields(self.details_form(item)).options["repo"]
        self.assertEqual([entry["attributes"]["value"] for entry in options], ["", "/repos/gone"])
        self.assertEqual([entry["attributes"]["value"] for entry in options
                          if "selected" in entry["attributes"]], ["/repos/gone"])
        # Selected, so `initialValue` is the stored path: an unrelated edit
        # leaves the field unchanged and sends no `repo` key.
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "title": "Renamed"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["repo"], "/repos/gone")

    def test_a_work_item_is_offered_no_repository_field_because_the_library_refuses_one(self):
        first = self.repo("/repos/system")
        upsert_repo(self.connection, first)
        other = self.repo("/repos/other")
        upsert_repo(self.connection, other)
        item = self.item("Work with a source identity", kind="work", repo=first, path="docs/work/x/prd.md")
        markup = self.details_form(item)
        self.assertNotIn("repo", Fields(markup).fields)
        # Why the field is absent rather than present: the library refuses the
        # reassignment, and the refusal takes the whole save with it.
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, result = self.post(f"/api/items/{item}",
            {"revision": revision, "title": "Renamed", "repo": other})
        self.assertEqual(status, 400)
        self.assertIn("source identity", result["error"])
        self.assertEqual(workflow.item_state(self.connection, item)["item"]["title"],
                         "Work with a source identity")


class ItemKind(BrowserSession):
    """sd:743 -- the item screen reclassifies a task through `edit_item`."""

    details_form = ItemRepository.details_form

    def test_a_task_offers_the_hand_kinds_and_a_work_item_offers_none(self):
        item = self.item("Errand", kind="task")
        options = Fields(self.details_form(item)).options["kind"]
        self.assertEqual([entry["attributes"]["value"] for entry in options], list(workflow.HAND_KINDS))
        self.assertEqual([entry["attributes"]["value"] for entry in options
                          if "selected" in entry["attributes"]], ["task"])
        self.repo("/repos/system")
        work = self.item("Work", kind="work", repo="/repos/system")
        self.assertNotIn('name="kind"', str(controls.item_controls(self.connection, work)))

    def test_a_store_older_than_migration_009_offers_only_the_kinds_its_check_admits(self):
        """The page reads through `connect(write=False)`, which accepts a store
        that predates the migration widening `item.kind`; on one, `edit_item`
        refuses the kinds it added, so the select must not offer them.
        The store here is narrowed the way migration 009 widened it: the CHECK
        text edited in place under `writable_schema`, with a schema-cookie
        bump so the next connection reads the narrow list. Only the CHECK is
        older; the property under test is the CHECK and nothing else. The
        edit goes through the library's own `connect`, because
        `tests/test_one_store.py` is the one place that may open
        this file any other way.
        """
        item = self.item("Errand", kind="task")
        narrower = connect(self.path)
        current = narrower.execute("SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0]
        # Anchored on the three kinds 009 added that remain: 046 dropped `personal` (sd:7004).
        narrow = re.sub(r",\s*'followup',\s*'work-idea', 'personal-idea'\)\)", "))", current)
        self.assertNotEqual(narrow, current, "the CHECK did not carry the three kinds, so this proves nothing")
        cookie = narrower.execute("PRAGMA schema_version").fetchone()[0]
        narrower.execute("PRAGMA writable_schema = ON")
        narrower.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'item'", (narrow,))
        # The cookie tells every other connection its cached schema is stale.
        narrower.execute(f"PRAGMA schema_version = {cookie + 1}")
        narrower.execute("PRAGMA writable_schema = RESET")
        narrower.commit()
        narrower.close()
        reader = connect(self.path, write=False)
        self.addCleanup(reader.close)
        self.assertEqual(workflow.schema_kinds(reader), tuple(kind for kind in workflow.schema_kinds(reader)
                                                             if kind not in ("followup", "work-idea", "personal-idea")))
        panel = str(controls.item_controls(reader, item))
        form = re.search(rf'<form[^>]*action="/api/items/{item}"[^>]*>.*?</form>', panel).group(0)
        options = Fields(form).options["kind"]
        self.assertEqual([entry["attributes"]["value"] for entry in options], ["task"])
        # What the select offers is what the library accepts, and nothing
        # more: the same store, through a writer, refuses a withheld kind.
        writer = connect(self.path)
        self.addCleanup(writer.close)
        with self.assertRaisesRegex(workflow.WorkflowError, "no item kind 'work-idea'"):
            workflow.edit_item(writer, item, {"kind": "work-idea"}, who="test",
                               expected_revision=workflow.item_state(writer, item)["revision"])

    def test_a_kind_change_is_written_and_noted_and_a_produced_kind_is_refused(self):
        item = self.item("Errand", kind="task")
        revision = workflow.item_state(self.connection, item)["revision"]
        before = self.snapshot()
        self.assertEqual(self.post(f"/api/items/{item}", {"revision": revision, "kind": "report"})[0], 400)
        self.assertEqual(before, self.snapshot())
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "kind": "work-idea"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["kind"], "work-idea")
        self.assertEqual((state["notes"][-1]["body"], state["notes"][-1]["session"]),
                         ("Changed kind task -> work-idea by dashboard", "dashboard"))


class ItemStatusOnly(BrowserSession):
    """sd:772 -- a personal item got the status control alone.

    A followup shared that branch until sd:816. `edit_item` stopped refusing
    its details at sd:809, so the panel now renders both forms for it and
    `FollowupDetails` below owns what the details form holds. The branch went
    with the `personal` kind (sd:7004): no kind takes the task statuses
    without its details.
    """

    def panel(self, item):
        return str(controls.item_controls(self.connection, item))

    def status_form(self, item):
        found = re.search(rf'<form[^>]*action="/api/items/{item}/status"[^>]*>.*?</form>', self.panel(item))
        return found.group(0) if found else None

    def details_form(self, item):
        return re.search(rf'<form[^>]*action="/api/items/{item}"[^>]*>', self.panel(item))

    def test_followup_offers_the_task_statuses(self):
        for kind in ("followup",):
            with self.subTest(kind=kind):
                item = self.item(f"A {kind} item", kind=kind)
                markup = self.status_form(item)
                self.assertIsNotNone(markup)
                options = Fields(markup).options["status"]
                self.assertEqual([entry["attributes"]["value"] for entry in options],
                                 list(workflow.TASK_STATUSES))
                self.assertEqual([entry["attributes"]["value"] for entry in options
                                  if "selected" in entry["attributes"]], ["planning"])

    def test_a_followup_now_gets_a_details_form(self):
        """sd:816 -- the hint that named `sd task edit ID` is gone, because
        the form it stood in for is on the panel."""
        followup = self.item("A followup item", kind="followup")
        self.assertIsNotNone(self.details_form(followup))
        self.assertIn('name="title"', self.panel(followup))
        self.assertNotIn("with sd task edit", self.panel(followup))

    def test_the_rendered_status_form_closes_and_reopens_a_followup(self):
        item = self.item("Come back to this", kind="followup")
        for target in ("done", "planning"):
            revision = Fields(self.status_form(item)).fields["revision"]["value"]
            status, _, state = self.post(f"/api/items/{item}/status", {"revision": revision, "status": target})
            self.assertEqual((status, state["item"]["status"]), (200, target))
            selected = [entry["attributes"]["value"] for entry in Fields(self.status_form(item)).options["status"]
                        if "selected" in entry["attributes"]]
            self.assertEqual(selected, [target])

    def test_done_refuses_an_open_followup_and_names_its_resolve_action(self):
        """sd:10407: Done resolved a person's followup unasked; it refuses now, naming the action."""
        item = self.item("Close me", kind="task")
        note = add_note(self.connection, item, "followup", "Ask example.test")
        self.connection.commit()
        revision = Fields(self.status_form(item)).fields["revision"]["value"]
        status, _, answer = self.post(f"/api/items/{item}/status", {"revision": revision, "status": "done"})
        self.assertNotEqual(status, 200)
        self.assertIn(f"resolve followup {note}", json.dumps(answer))
        self.assertEqual(workflow.item_state(self.connection, item)["item"]["status"], "planning")

    def test_a_followup_with_a_queued_assignment_shows_the_hint_and_no_select(self):
        """The queued run owns the status, not the details. A task with a
        queued assignment keeps its `Save details` form for the same reason:
        `workflow.edit_item` consults no assignment, so withholding the form
        would hide a save the library still accepts.
        """
        item = self.item("Queued", kind="followup")
        create_assignment(self.connection, item=item, role="author", status="queued")
        panel = self.panel(item)
        self.assertIn("Status is controlled by the active workflow.", panel)
        self.assertIsNone(self.status_form(item))
        self.assertIsNotNone(self.details_form(item))

    def test_task_and_work_keep_both_forms(self):
        task = self.item("Errand", kind="task")
        self.assertIsNotNone(self.details_form(task))
        self.assertEqual([entry["attributes"]["value"] for entry in Fields(self.status_form(task)).options["status"]],
                         list(workflow.TASK_STATUSES))
        self.repo("/repos/system")
        upsert_repo(self.connection, "/repos/system")
        work = self.item("Work", kind="work", repo="/repos/system")
        self.assertIsNotNone(self.details_form(work))
        self.assertEqual([entry["attributes"]["value"] for entry in Fields(self.status_form(work)).options["status"]],
                         workflow.allowed_statuses(self.connection, work))
        self.assertNotIn("done", workflow.allowed_statuses(self.connection, work))

    def test_kinds_with_their_own_workflow_still_render_no_item_controls(self):
        for kind in ("report",):
            with self.subTest(kind=kind):
                self.assertEqual(self.panel(self.item(f"A {kind} item", kind=kind)), "")


class IdeaClose(BrowserSession):
    """pack sd:9302 -- a hand idea gets the status control alone: planning and done.

    A writing piece closes through its stages and a vault row moves by stage
    only (sd:3972), so neither gets one.
    """

    #: The three groups, as kind and source: work-idea, personal-idea and an idea with no piece.
    HAND_IDEAS = (("work-idea", None), ("work-idea", "drafts"), ("personal-idea", "drafts"), ("idea", "drafts"))
    panel, status_form, details_form = ItemStatusOnly.panel, ItemStatusOnly.status_form, ItemStatusOnly.details_form

    def test_each_hand_idea_renders_a_close_control_and_no_details_form(self):
        for kind, source in self.HAND_IDEAS:
            with self.subTest(kind=kind, source=source):
                item = self.item(f"A {kind} item", kind=kind, source=source)
                self.assertIsNotNone(self.status_form(item), "no status form for a hand idea")
                options = Fields(self.status_form(item)).options["status"]
                self.assertEqual([entry["attributes"]["value"] for entry in options], ["planning", "done"])
                self.assertIn('name="reason"', self.status_form(item))
                self.assertIsNone(self.details_form(item))
                self.assertIn(f'action="/api/items/{item}/status"', self.render(f"/item/{item}"))

    def test_a_reclassified_idea_shows_its_current_status_selected(self):
        # pack sd:9380: an idea reclassified from a task at ready showed planning selected, so one press wrote planning.
        item = self.item("A reclassified idea", kind="work-idea", source="drafts")
        self.connection.execute("UPDATE item SET status = 'ready' WHERE id = ?", (item,))
        self.connection.commit()
        options = Fields(self.status_form(item)).options["status"]
        self.assertEqual([(entry["attributes"]["value"], "selected" in entry["attributes"]) for entry in options],
                         [("ready", True), ("planning", False), ("done", False)])

    def test_the_rendered_form_closes_with_a_reason_and_reopens(self):
        for kind, source in self.HAND_IDEAS:
            item = self.item(f"A {kind} item", kind=kind, source=source)
            for target, reason in (("done", "not doing it"), ("planning", "")):
                with self.subTest(kind=kind, source=source, target=target):
                    self.assertIsNotNone(self.status_form(item), "no status form for a hand idea")
                    revision = Fields(self.status_form(item)).fields["revision"]["value"]
                    status, _, state = self.post(f"/api/items/{item}/status",
                                                 {"revision": revision, "status": target, "reason": reason})
                    self.assertEqual((status, state["item"]["status"]), (200, target))
            history = [n["body"] for n in workflow.item_state(self.connection, item)["notes"]
                       if n["kind"] == "status_change"]
            self.assertIn("not doing it", history[-2] if len(history) > 2 else "")

    def test_a_writing_piece_and_a_vault_idea_render_no_close_control(self):
        piece = self.item("A piece", kind="idea", source="writing-piece", external_id="a-piece")
        set_item_fields(self.connection, piece, piece="a-piece")
        vault = self.item("A vault idea", kind="idea", source="vault",
                          external_id="topic:System/Databases/Topics/A vault idea.md",
                          stage="active", fields={"kind": "topic"})
        self.connection.commit()
        for item in (piece, vault):
            with self.subTest(item=item):
                self.assertEqual(self.panel(item), "")
        # The piece's page reads its writing checkout; the panel above is what that page renders.
        self.assertNotIn(f'action="/api/items/{vault}/status"', self.render(f"/item/{vault}"))


class HostileItemStorage(BrowserSession):
    """sd:870 and sd:874 -- the item screen survives what the store permits.

    `item.body` and `item.fields` are plain `TEXT` with no `json_valid` check
    (`sd_db/schema/001_initial.sql:51-52`), so a row neither column can be read
    from is a row the store accepts. sd:775 guarded `fields` on the report
    panel; `screens.artifact` and `controls.item_controls` read `body` the same
    way and were not guarded, and `workflow.item_state` hashes the whole row
    before either of them runs.

    The consequence each time is worse than an error page. `do_GET`
    (`source:dashboard/sd_dashboard/server.py::do_GET`) catches `MissingItem`, `NotFound`, `SdDbError` and
    `sqlite3.Error`. `json.JSONDecodeError` and `TypeError` are none of those,
    so the failure raised through the handler and the reader got no response at
    all -- `RemoteDisconnected`, with the traceback on the server's stderr.
    """

    def panel(self, item):
        return str(controls.item_controls(self.connection, item))

    def details_form(self, item):
        found = re.search(rf'<form[^>]*action="/api/items/{item}"[^>]*>.*?</form>',
                          self.panel(item), re.S)
        return found.group(0) if found else None

    def unreadable(self, kind="task"):
        item = self.item("Unreadable", kind=kind, body={"text": "was readable once"})
        self.connection.execute("UPDATE item SET body = ? WHERE id = ?", ("{not json", item))
        self.connection.commit()
        return item

    def test_an_item_whose_body_is_not_json_renders_instead_of_dropping_the_reader(self):
        """sd:870 -- `screens.artifact` parsed `body` with no guard."""
        for kind in ("task", "followup"):
            with self.subTest(kind=kind):
                item = self.unreadable(kind)
                status, _, page = self.request(f"/item/{item}")
                self.assertEqual(status, 200)
                self.assertIn("not valid JSON", page)
                self.assertNotIn("was readable once", page)

    def test_an_unreadable_body_withholds_the_details_field_and_keeps_the_rest(self):
        """sd:870 -- an empty textarea here would offer a save that overwrites
        the stored body with the blank the reader never typed. Withhold the
        field: `data-changed-fields` omits what is not rendered, so the save
        that stays available cannot touch `body`.
        """
        item = self.unreadable()
        markup = self.details_form(item)
        self.assertIsNotNone(markup)
        self.assertEqual(sorted(Fields(markup).fields),
                         ["due", "kind", "priority", "repo", "revision", "title"])
        self.assertNotIn('name="body"', markup)
        self.assertIn("not valid JSON", markup)

    def test_a_save_beside_an_unreadable_body_leaves_the_stored_body_alone(self):
        """The withheld field is not a silent clear: the stored bytes stay."""
        item = self.unreadable()
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "title": "Renamed"})
        self.assertEqual((status, state["item"]["title"]), (200, "Renamed"))
        self.assertEqual(state["item"]["body"], "{not json")

    def test_a_blob_in_fields_renders_the_item_page(self):
        """sd:874 -- TEXT affinity converts every numeric storage class to text,
        so `json.loads` never sees an `int` from these columns. BLOB is the one
        class affinity leaves alone: it comes back as `bytes`, and
        `workflow.item_state` json.dumps-es the row to compute the revision.
        Valid JSON in the BLOB dies there too, so this is a type the revision
        computation cannot serialise, not malformed data.
        """
        item = self.binary()
        status, _, page = self.request(f"/item/{item}")
        self.assertEqual(status, 200)
        self.assertIn("Binary fields", page)

    def binary(self):
        item = self.item("Binary fields", kind="task")
        self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (b'{"report": {}}', item))
        self.connection.commit()
        return item

    def test_a_post_on_a_blob_backed_item_answers_instead_of_dropping_the_reader(self):
        """sd:874 again, on the other verb. `item_state` is what every mutation
        returns and `server.Dashboard._json` dumps that whole state, so making
        the revision hash survive a BLOB only moved the `TypeError` from the
        GET to the POST beside it -- `do_POST` names no more of them than
        `do_GET` does, and the reader lost the connection either way. The
        encoding the hash uses is the encoding the response uses.
        """
        item = self.binary()
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}/status",
                                     {"revision": revision, "status": "ready"})
        self.assertEqual((status, state["item"]["status"]), (200, "ready"))
        self.assertEqual(state["item"]["fields"], {"sd_db_bytes_hex": b'{"report": {}}'.hex()})


class FollowupDetails(BrowserSession):
    """sd:816 -- a followup gets the details the library already accepts.

    sd:809 (system PR #360) put `followup` in `workflow.DETAIL_KINDS`, so
    `edit_item` takes its title, body, priority, due date and repository. The
    panel still offered the status control alone and a hint naming
    `sd task edit ID`, and `screens.artifact` decoded `{"text": ...}` for
    `task`, `report` and the skill-review kind only -- so every followup filed with
    `sd task add --kind followup` showed its body as raw JSON.

    `kind` stays out of the form. `edit_item` would take it, but reclassifying
    a followup is not one of the five fields #360 named, and the select would
    be a widget sd:816 did not ask for.
    """

    def panel(self, item):
        return str(controls.item_controls(self.connection, item))

    def details_form(self, item):
        found = re.search(rf'<form[^>]*action="/api/items/{item}"[^>]*>.*?</form>',
                          self.panel(item), re.S)
        return found.group(0) if found else None

    def test_a_followup_body_renders_as_text_not_as_its_stored_json(self):
        item = self.item("Ask the vendor", kind="followup", body={"text": "Call **them** back"})
        status, _, page = self.request(f"/item/{item}")
        self.assertEqual(status, 200)
        self.assertNotIn("&quot;text&quot;", page)
        self.assertIn("<strong>them</strong>", page)

    def test_a_task_reclassified_to_any_hand_kind_still_shows_its_text(self):
        # The kind select keeps the `{"text": ...}` body; the artifact decode
        # named four kinds and showed the other three their braces (PR #332
        # review). Every hand kind, so a kind added to `HAND_KINDS` is covered.
        for kind in workflow.HAND_KINDS:
            with self.subTest(kind=kind):
                item = self.item("Errand", kind="task", body={"text": "Buy **milk**"})
                if kind != "task":
                    revision = workflow.item_state(self.connection, item)["revision"]
                    status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "kind": kind})
                    self.assertEqual((status, state["item"]["kind"]), (200, kind))
                page = self.request(f"/item/{item}")[2]
                self.assertNotIn("&quot;text&quot;", page)
                self.assertIn("<strong>milk</strong>", page)

    def test_the_followup_details_form_offers_what_the_library_accepts_and_saves(self):
        self.repo("/repos/system")
        item = self.item("Ask the vendor", kind="followup", body={"text": "Call them"})
        markup = self.details_form(item)
        self.assertIsNotNone(markup)
        parsed = Fields(markup)
        self.assertEqual(sorted(parsed.fields),
                         ["body", "due", "priority", "repo", "revision", "title"])
        status, _, state = self.post(f"/api/items/{item}", {
            "revision": parsed.fields["revision"]["value"],
            "title": "Ask the vendor twice", "priority": 2, "due": "2026-09-30",
            "repo": "/repos/system", "body": "Called **twice**"})
        self.assertEqual((status, state["item"]["title"]), (200, "Ask the vendor twice"))
        self.assertEqual(json.loads(state["item"]["body"])["text"], "Called **twice**")
        self.assertEqual(state["item"]["repo"], "/repos/system")
        self.assertIn("<strong>twice</strong>", self.request(f"/item/{item}")[2])


class BulkAcknowledgeRetired(BrowserSession):
    def test_the_clean_preview_and_the_batch_routes_are_gone(self):
        """A clean run files no report since the quiet-run rule, so the Reports page lost its clean preview and
        batch; `sd reports acknowledge --all-clean` stays the CLI verb. Both routes answer 404 and write nothing."""
        before = self.snapshot()
        body = {"before": "2026-09-10T00:00:00+00:00", "plan": "0" * 64, "who": "operator"}
        self.assertEqual(self.post("/api/reports/acknowledge-clean", body)[0], 404)
        self.assertEqual(self.request("/api/reports/clean?before=2026-09-10", headers={"Cookie": self.cookie})[0], 404)
        self.assertEqual(before, self.snapshot())
