"""The v2 Writing page (sd:2125).

What this slice promises: `/writing` answers under the shared policy and loads the reader and its script before `shell.js`,
and the old list is retired (sd:3402). `/api/writing` is every piece, parked ones too, with
the stage, gate problems, next and correction stages that `writing.piece_state` gives, each file's size, and the opening
of the draft; it names the repository by its folder only and writes nothing. The page draws the board with four gate
lamps per piece; Stage asks first and posts the item stage route with the piece's revision, Correct posts it with a
reason, and Park and Revive post their routes and undo each other. Publish and Capture are copy only.

`writing.js` runs under JavaScriptCore (osascript) against the stand-in page and shell `test_v2_tasks` uses, with the real
reader (`read.js`). The browser half -- the look at 1440 and 375 px, drag and the confirm dialog -- is a manual check.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from sd_dashboard import hoa_screen, places, server, v2, writing_screen
from sd_dashboard.v2.pages import writing as writing_page
from sd_db import upsert_repo, writing
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_tasks import COMMANDS_JS, SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "writing.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
WRITING_CSS = (V2 / "static" / "writing.css").read_text(encoding="utf-8")
NOW = "2026-09-23T12:00:00Z"


class Pieces(BrowserSession):
    """Two row-owned pieces in a writing checkout: one at review with its reports, one parked at drafting."""

    def seed(self):
        self.root = Path(self.tmp.name) / "writing"
        ids = {}
        for slug, stage in (("fixture", "review"), ("parked", "drafting")):
            folder = self.root / "content" / "2026" / slug
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "index.md").write_text(f"---\ntitle: {slug.capitalize()} piece\nstatus: {stage}\n---\n\n## Draft\n\n"
                                             "A carefully scoped fixture argument.\n\nA second paragraph here.\n")
            upsert_repo(self.connection, str(self.root), pieces_source="file")
            state = writing.import_piece(self.connection, str(self.root), f"2026/{slug}", who="operator")
            ids[slug] = state["item"]["id"]
            digest = state["writing"]["gates"]["digest"]
            (folder / "research.md").write_text(f"Research fixture\n<!-- reconciled-with-draft: {digest} gen=0 -->\n")
            (folder / "adversarial.md").write_text("Adversarial fixture report\n")
            if slug == "fixture":
                (folder / "fact-check.md").write_text("Fact-check fixture report\n")
        upsert_repo(self.connection, str(self.root), pieces_source="row")
        state = writing.piece_state(self.connection, ids["parked"])
        writing.park_piece(self.connection, ids["parked"], expected_revision=state["revision"], who="operator")
        return ids


class TheDocument(Pieces):
    def setUp(self):
        super().setUp()
        self.ids = self.seed()

    def test_each_piece_carries_its_repos_realm(self):
        # sd:5064: places.realm_of of the piece's repo, as on Tasks; without realms.conf a repo's realm is unknown.
        conf = Path(self.tmp.name) / "realms.conf"
        conf.write_text(f"realm|brand|Brand|{self.root}\n", encoding="utf-8")
        with mock.patch.object(places, "REALMS_CONFIG", conf):
            doc = writing_screen.document(self.connection, now=NOW)
        self.assertEqual({p["realm"] for p in doc["pieces"]}, {"brand"})
        self.assertEqual(doc["realms"]["state"], "read")
        conf.write_text("realm|brand|Brand|/elsewhere/*\n", encoding="utf-8")
        with mock.patch.object(places, "REALMS_CONFIG", conf):
            self.assertEqual({p["realm"] for p in writing_screen.document(self.connection, now=NOW)["pieces"]}, {"personal"})
        with mock.patch.object(places, "REALMS_CONFIG", Path(self.tmp.name) / "absent.conf"):
            doc = writing_screen.document(self.connection, now=NOW)
        self.assertEqual(({p["realm"] for p in doc["pieces"]}, doc["realms"]["state"]), ({None}, "missing"))

    def test_a_piece_of_the_hoa_checkout_is_realm_hoa_before_a_glob_that_claims_it(self):
        """pack sd:7687: the hoa checkout's pieces are HOA's, as its items are on Tasks, with or without a realm list."""
        conf = Path(self.tmp.name) / "realms.conf"
        conf.write_text(f"realm|brand|Brand|{self.root}\n", encoding="utf-8")
        hoa = mock.patch.object(hoa_screen, "checkout", return_value=(str(self.root), Path(self.root), []))
        for path in (conf, Path(self.tmp.name) / "absent.conf"):
            with self.subTest(path=path.name), hoa, mock.patch.object(places, "REALMS_CONFIG", path):
                self.assertEqual({p["realm"] for p in writing_screen.document(self.connection, now=NOW)["pieces"]}, {"hoa"})

    def test_each_piece_carries_its_stage_gates_moves_files_and_draft(self):
        doc = writing_screen.document(self.connection, now=NOW)
        by = {p["id"]: p for p in doc["pieces"]}
        piece = by[self.ids["fixture"]]
        state = writing.piece_state(self.connection, self.ids["fixture"])
        self.assertEqual((piece["stage"], piece["parked"], piece["piece"], piece["repo"], piece["folder"], piece["owner"]),
                         ("review", False, "2026/fixture", "writing", "content", "row"))
        self.assertEqual((piece["next"], piece["corrections"]), (["ready"], ["researching", "drafting"]))
        self.assertEqual(piece["revision"], state["revision"])
        self.assertEqual(piece["problems"], state["writing"]["gates"]["problems"])
        self.assertFalse(piece["gates_ok"])
        self.assertFalse(piece["ready_recorded"])
        self.assertEqual(piece["draft"], ["A carefully scoped fixture argument.", "A second paragraph here."])
        self.assertEqual(piece["words"], 9)
        self.assertEqual(piece["files"]["fact-check.md"]["bytes"], len("Fact-check fixture report\n"))
        self.assertIsNone(by[self.ids["parked"]]["files"]["fact-check.md"])
        # A parked piece is listed, and it has no moves until it is revived.
        self.assertEqual((by[self.ids["parked"]]["parked"], by[self.ids["parked"]]["next"]), (True, []))

    def test_each_piece_carries_its_drive_folder_and_each_files_copies(self):
        # pack sd:7243: the stage gives the folder, with no designation; fields.published and the queue give each copy.
        import tempfile

        from sd_dashboard import publishing
        from sd_db import paths

        queue = Path(self.enterContext(tempfile.TemporaryDirectory())) / "pending-mirror-syncs"
        self.enterContext(mock.patch.object(publishing.kit(), "QUEUE", queue))
        url = "https://docs.google.com/document/d/example-doc/edit"
        fields = {"published": {f"{paths.key(str(self.root))}::2026-fixture-research": {"drive": url}}}
        self.connection.execute("UPDATE item SET fields = ?, stage = 'review' WHERE id = ?",
                                (json.dumps(fields), self.ids["fixture"]))
        self.connection.commit()
        by = {p["id"]: p for p in writing_screen.document(self.connection, now=NOW)["pieces"]}
        mirror = by[self.ids["fixture"]]["mirror"]
        self.assertEqual((mirror["folder"], mirror["unread"]), ("Writing/Drafts", ""))
        self.assertEqual([(c["channel"], c["state"], c["url"]) for c in mirror["copies"]["research.md"]],
                         [("drive", "published", url)])
        # A parked piece mirrors nothing, whatever its stage.
        self.assertEqual((mirror["copies"]["index.md"], by[self.ids["parked"]]["mirror"]["folder"]), ([], None))

    def test_a_mirror_not_read_is_unknown_with_its_reason(self):
        # A pack without the queue says why; it is never "no copy".
        from sd_dashboard import publishing

        with mock.patch.object(publishing, "kit", side_effect=ImportError("no sd_research_publish")):
            pieces = writing_screen.document(self.connection, now=NOW)["pieces"]
        self.assertEqual([p["mirror"]["folder"] for p in pieces], [None, None])
        self.assertIn("no sd_research_publish", pieces[0]["mirror"]["unread"])

    def test_each_piece_carries_its_whole_draft_up_to_a_bound(self):
        """pack sd:7242: Show draft opens the whole `## Draft`, not its first 6 paragraphs; a draft past DRAFT_CHARS is cut
        at a paragraph and says so, so one long file cannot swell /api/writing."""
        index = self.root / "content" / "2026" / "fixture" / "index.md"
        paragraphs = [f"Paragraph {n} of the fixture draft." for n in range(1, 10)]
        index.write_text("---\ntitle: Fixture piece\nstatus: review\n---\n\n## Draft\n\n" + "\n\n".join(paragraphs) + "\n")
        piece = {p["id"]: p for p in writing_screen.document(self.connection, now=NOW)["pieces"]}[self.ids["fixture"]]
        self.assertEqual((piece["draft"], piece["draft_cut"]), (paragraphs, False))
        with mock.patch.object(writing_screen, "DRAFT_CHARS", 3 * len(paragraphs[0])):
            piece = {p["id"]: p for p in writing_screen.document(self.connection, now=NOW)["pieces"]}[self.ids["fixture"]]
        self.assertEqual((piece["draft"], piece["draft_cut"], piece["words"]), (paragraphs[:3], True, 54))

    def test_a_first_paragraph_past_the_bound_keeps_an_excerpt_cut_at_a_word(self):
        """pack sd:7242 review: a first paragraph longer than DRAFT_CHARS left no paragraph, so Show draft said "no
        draft yet". Its excerpt, cut at the last word boundary within the bound, stands in, and the draft says cut."""
        index = self.root / "content" / "2026" / "fixture" / "index.md"
        words = [f"word{n:04}" for n in range(1000)]
        index.write_text("---\ntitle: Fixture piece\nstatus: review\n---\n\n## Draft\n\n" + " ".join(words) + "\n\nA second one.\n")
        with mock.patch.object(writing_screen, "DRAFT_CHARS", 95):
            piece = {p["id"]: p for p in writing_screen.document(self.connection, now=NOW)["pieces"]}[self.ids["fixture"]]
        # 10 words of 8 characters and their 9 spaces take 89; an eleventh would take 98.
        self.assertEqual((piece["draft"], piece["draft_cut"], piece["words"]), ([" ".join(words[:10])], True, 1003))
        with mock.patch.object(writing_screen, "DRAFT_CHARS", 5):  # no word fits: the excerpt is cut mid-word
            piece = {p["id"]: p for p in writing_screen.document(self.connection, now=NOW)["pieces"]}[self.ids["fixture"]]
        self.assertEqual((piece["draft"], piece["draft_cut"]), (["word0"], True))

    def test_the_ideas_are_the_inbox_blog_ideas_highest_score_first(self):
        """pack sd:7244: the Ideas column reads the `sdw.blog-idea` rows through `records.list_rows`: inbox only, highest
        score first, a link only for http, at most IDEAS of them beside the inbox count."""
        from sd_db import create_item

        made = {title: create_item(self.connection, kind="idea", title=title, status="planning", source="vault",
                                   path=f"Blog Ideas/{title}.md", stage=stage,
                                   fields={"kind": kind, "score": score, "url": url, "dateCreated": "2026-10-01"})
                for title, kind, stage, score, url in (
                    ("Low idea", "blog-idea", "inbox", "3", "https://example.test/low"),
                    ("High idea", "blog-idea", "inbox", "9", "javascript:alert(1)"),
                    ("Unscored idea", "blog-idea", "inbox", "", ""),
                    ("Declined idea", "blog-idea", "declined", "10", ""),
                    ("A tip", "tip", "inbox", "10", ""))}
        ideas = writing_screen.document(self.connection, now=NOW)["ideas"]
        pool, declined = ideas.pop("pool"), ideas.pop("declined")
        self.assertEqual(sorted(r["title"] for r in pool), ["High idea", "Low idea", "Unscored idea"])
        self.assertEqual([r["title"] for r in declined], ["Declined idea"])
        def row(title, score, url, link):
            return {"item": made[title], "title": title, "score": score, "url": url, "href": link, "created": "2026-10-01", "area": ""}
        self.assertEqual(ideas, {"state": "read", "reason": "", "count": 3, "top": 10, "areas": {"": 3}, "rows": [
            row("High idea", 9.0, "javascript:alert(1)", None), row("Low idea", 3.0, "https://example.test/low", "https://example.test/low"),
            row("Unscored idea", None, "", None)]})
        with mock.patch.object(writing_screen, "IDEAS", 1):
            ideas = writing_screen.document(self.connection, now=NOW)["ideas"]
        self.assertEqual((ideas["count"], [r["title"] for r in ideas["rows"]]), (3, ["High idea"]))

    def test_each_idea_has_its_area_and_each_area_keeps_its_highest_ideas(self):
        """pack sd:7670: an idea's area is Personal, Work or RWBP, read from its `contexts` field; another value or none is
        no area. The column counts each area, and `rows` holds the highest IDEAS overall and of each area, so a filtered
        column still shows an area's best ideas."""
        from sd_db import create_item

        def idea(title, score, contexts):
            return create_item(self.connection, kind="idea", title=title, status="planning", source="vault",
                               path=f"Blog Ideas/{title}.md", stage="inbox",
                               fields={"kind": "blog-idea", "score": score, "contexts": contexts})
        idea("Top personal", "9", ["Personal"])
        idea("Next personal", "8", "personal")
        idea("Work idea", "4", ["Work"])
        idea("Brand idea", "2", "RWBP")
        idea("Elsewhere idea", "1", ["Somewhere"])
        with mock.patch.object(writing_screen, "IDEAS", 1):
            ideas = writing_screen.document(self.connection, now=NOW)["ideas"]
        self.assertEqual(ideas["areas"], {"Personal": 2, "Work": 1, "RWBP": 1, "": 1})
        self.assertEqual([(r["title"], r["area"]) for r in ideas["rows"]],
                         [("Top personal", "Personal"), ("Work idea", "Work"), ("Brand idea", "RWBP"), ("Elsewhere idea", "")])
        self.assertEqual(writing_screen.AREAS, ("Personal", "Work", "RWBP"))

    def test_an_ideas_score_or_date_that_is_no_finite_number_or_text_is_unset(self):
        """pack sd:7244 review: the vault's fields are JSON that `json.loads` reads, so a score may be 'nan', 'inf',
        '1e999', a NaN or Infinity literal, 10**400 or true; a date may be any of those or an object. A score that is no finite
        number and a date that is no text read as unset, so one idea cannot put NaN in /api/writing and fail the page's
        JSON parse for every piece."""
        from sd_db import create_item

        # 10**400 is a valid JSON integer that float() refuses with OverflowError.
        bad = ("nan", "inf", "-inf", "1e999", float("nan"), float("inf"), True, [3], {"n": 3}, 10**400, -10**400)
        for n, value in enumerate(bad):
            create_item(self.connection, kind="idea", title=f"Odd idea {n:02}", status="planning", source="vault",
                        path=f"Blog Ideas/Odd idea {n:02}.md", stage="inbox",
                        fields={"kind": "blog-idea", "score": value, "dateCreated": value})
        with mock.patch.object(writing_screen, "IDEAS", len(bad)):
            doc = writing_screen.document(self.connection, now=NOW)
        json.dumps(doc, allow_nan=False)  # raises ValueError on NaN or Infinity
        self.assertEqual([(r["score"], r["created"]) for r in doc["ideas"]["rows"]],
                         [(None, value if isinstance(value, str) else None) for value in bad])

    def test_a_publication_url_that_is_no_text_is_no_publication(self):
        """pack sd:7242 review, same class: a claim's state is JSON that `json.loads` reads, so its url may be a
        number: the claim table's json_valid check refuses a NaN literal, but 1e999 passes it and reads as inf. Such a
        claim is no live publication, and the document stays plain JSON."""
        claim = "INSERT INTO publication_claim (id, item, payload, state, created_at, updated_at) VALUES (?, ?, '{}', ?, ?, ?)"
        for n, url in enumerate(("1e999", "-1e999", "7")):
            self.connection.execute(claim, (f"c{n}aim0000000", self.ids["fixture"], '{"phase": "published", "url": %s}' % url, NOW, NOW))
        self.connection.commit()
        doc = writing_screen.document(self.connection, now=NOW)
        json.dumps(doc, allow_nan=False)
        self.assertIsNone({p["id"]: p for p in doc["pieces"]}[self.ids["fixture"]]["publication"])

    def test_a_claim_state_or_item_fields_with_an_int_past_the_digit_limit_leave_the_page_readable(self):
        """pack sd:7329: json.loads refuses an integer literal past 4,300 digits with ValueError, and the claim
        table's json_valid check accepts one. One such claim or one such field raised out of piece_state, so
        /api/writing failed for every piece and the item page failed for that one. The number reads as unset."""
        big = "9" * 5000
        item = self.ids["fixture"]
        self.connection.execute(
            "INSERT INTO publication_claim (id, item, payload, state, created_at, updated_at) VALUES (?, ?, '{}', ?, ?, ?)",
            ("c0aim0000000", item, '{"phase": %s, "url": %s}' % (big, big), NOW, NOW))
        held = self.connection.execute("SELECT fields FROM item WHERE id = ?", (item,)).fetchone()[0]
        self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (held[:-1] + ', "weight": %s}' % big, item))
        self.connection.commit()
        doc = writing_screen.document(self.connection, now=NOW)
        json.dumps(doc, allow_nan=False)
        self.assertIsNone({p["id"]: p for p in doc["pieces"]}[item]["publication"])
        # The item page's controls name the claim's phase as text, whatever the claim holds.
        from sd_dashboard import controls

        row = dict(self.connection.execute("SELECT * FROM item WHERE id = ?", (item,)).fetchone())
        self.assertIn("Publication", str(controls.writing_controls(self.connection, row)))

    def test_the_inbox_tips_are_read_beside_the_ideas(self):
        """sd:8013: Writing holds the Tips queue beside Ideas, so Today's Tips link has a home."""
        from sd_db import create_item

        for title, stage, score in (("Good tip", "inbox", "7"), ("Used tip", "accepted", "9")):
            create_item(self.connection, kind="idea", title=title, status="planning", source="vault",
                        path=f"Tips/{title}.md", stage=stage, fields={"kind": "tip", "score": score})
        tips = writing_screen.document(self.connection, now=NOW)["tips"]
        self.assertEqual((tips["state"], tips["count"], [r["title"] for r in tips["rows"]]), ("read", 1, ["Good tip"]))

    def test_a_failed_ideas_read_is_unknown_with_its_reason_and_the_pieces_still_read(self):
        with mock.patch.object(writing_screen, "read_ideas", side_effect=sqlite3.OperationalError("no such column: stage")):
            doc = writing_screen.document(self.connection, now=NOW)
        self.assertEqual(doc["ideas"], {"state": "unknown", "reason": "no such column: stage", "count": None, "rows": [],
                                       "pool": [], "declined": []})
        self.assertEqual(len(doc["pieces"]), 2)

    def test_a_report_reads_one_piece_file_read_only_up_to_a_bound(self):
        """pack sd:7242: a gate's view icon reads its report through /api/writing/<id>/<gate>: the file beside the
        piece's index.md, cut at REPORT_CHARS; a missing file or a non-piece is a 404 that says which."""
        item, parked = self.ids["fixture"], self.ids["parked"]
        before = self.snapshot()
        self.assertEqual(writing_screen.report(self.connection, f"/api/writing/{item}/fact-check"),
                         (200, {"item": item, "name": "fact-check.md", "text": "Fact-check fixture report\n", "cut": False}))
        self.assertEqual(self.snapshot(), before)
        with mock.patch.object(writing_screen, "REPORT_CHARS", 10):
            self.assertEqual(writing_screen.report(self.connection, f"/api/writing/{item}/fact-check")[1]["text"], "Fact-check")
            self.assertIs(writing_screen.report(self.connection, f"/api/writing/{item}/fact-check")[1]["cut"], True)
        self.assertEqual(writing_screen.report(self.connection, f"/api/writing/{parked}/fact-check"),
                         (404, {"error": f"#{parked} has no fact-check.md."}))
        self.assertEqual(writing_screen.report(self.connection, "/api/writing/999999/research")[0], 404)
        self.assertEqual(writing_page.REPORT, writing_screen.REPORT, "the route and the reader parse one path")

    def test_a_published_piece_carries_its_claims_url_and_a_link_only_for_http(self):
        # sd:3465: the writing claim URL becomes a link; an address in another scheme stays text.
        claim = "INSERT INTO publication_claim (id, item, payload, state, created_at, updated_at) VALUES (?, ?, '{}', ?, ?, ?)"
        self.connection.execute(claim, ("c1aim0000000", self.ids["fixture"], json.dumps({"phase": "published", "url": "https://example.test/p"}), NOW, NOW))
        self.connection.execute(claim, ("c2aim0000000", self.ids["parked"], json.dumps({"phase": "published", "url": "javascript:alert(1)"}), NOW, NOW))
        self.connection.commit()
        by = {p["id"]: p for p in writing_screen.document(self.connection, now=NOW)["pieces"]}
        self.assertEqual(by[self.ids["fixture"]]["publication"],
                         {"claim": "c1aim0000000", "url": "https://example.test/p", "phase": "published", "href": "https://example.test/p"})
        self.assertIsNone(by[self.ids["parked"]]["publication"]["href"])

    def test_each_piece_carries_its_adversarial_reports_currency_against_the_draft(self):
        """pack sd:6977: the digest stamp Apply review gates on; an unstamped report is not current however new it is."""
        folder = self.root / "content" / "2026" / "fixture"
        by = {p["id"]: p for p in writing_screen.document(self.connection, now=NOW)["pieces"]}
        self.assertEqual(by[self.ids["fixture"]]["adversarial"]["state"], "unstamped")
        digest = writing.piece_state(self.connection, self.ids["fixture"])["writing"]["gates"]["digest"]
        (folder / "adversarial.md").write_text(f"<!-- reconciled-with-draft: {digest} on 2026-09-20 gen=0 -->\n\nReport\n")
        by = {p["id"]: p for p in writing_screen.document(self.connection, now=NOW)["pieces"]}
        self.assertEqual(by[self.ids["fixture"]]["adversarial"], {"state": "current", "detail": f"draft {digest}"})

    def test_each_piece_carries_its_open_grill_questions_and_its_rulings(self):
        # pack sd:6700: the piece's open question notes, oldest first, each with its options; and the count it ruled.
        from sd_dashboard import decisions
        from sd_db import workflow

        item = self.ids["fixture"]
        asked = [workflow.add_item_note(self.connection, item, kind="question", who="claude", body=body)["note"]["id"]
                 for body in ("Who is this for?\nOption: Engineers (Recommended)\nOption: Managers",
                             "What is the one point?\nOption: Rows win (Recommended)\nOption: Files win")]
        grill = {p["id"]: p for p in writing_screen.document(self.connection, now=NOW)["pieces"]}[item]["grill"]
        self.assertEqual(grill, {"open": [{"note": asked[0], "question": "Who is this for?",
                                           "options": ["Engineers (Recommended)", "Managers"]},
                                          {"note": asked[1], "question": "What is the one point?",
                                           "options": ["Rows win (Recommended)", "Files win"]}],
                                 "answered": 0})
        revision = workflow.item_state(self.connection, item)["revision"]
        decisions.answer(self.connection, item, asked[0], "Engineers (Recommended)", expected_revision=revision,
                         who="dashboard")
        grill = {p["id"]: p for p in writing_screen.document(self.connection, now=NOW)["pieces"]}[item]["grill"]
        self.assertEqual(([q["note"] for q in grill["open"]], grill["answered"]), ([asked[1]], 1))

    def test_no_field_holds_a_local_path(self):
        text = json.dumps(writing_screen.document(self.connection, now=NOW))
        self.assertNotIn(self.tmp.name, text)
        self.assertNotIn(str(self.root), text)

    def test_the_document_writes_nothing(self):
        before = self.snapshot()
        writing_screen.document(self.connection, now=NOW)
        self.assertEqual(self.snapshot(), before)


class ThePage(Pieces):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/writing")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Writing · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "commands.js", "writing.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_old_one_is_retired(self):
        self.assertEqual(v2.SECTIONS.get("Writing"), "/writing")
        self.assertNotIn("Writing", v2.CLASSIC)
        # sd:3402 retired the classic screen; `test_classic_retired` holds the 404.
        self.assertNotIn("Writing (classic)", v2.SCREENS)
        self.assertEqual(self.request("/classic/writing")[0], 404)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/writing")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/writing?view=editor", headers=cookie)[0], 400)
        status, _, body = self.request("/api/writing", headers=cookie)
        self.assertEqual((status, json.loads(body)["pieces"]), (200, []))

    def test_a_piece_the_page_reads_is_one_the_stage_and_park_routes_accept(self):
        ids = self.seed()
        cookie = {"Cookie": self.cookie}
        digest = writing.piece_state(self.connection, ids["fixture"])["writing"]["gates"]["digest"]
        for artifact in ("fact-check", "adversarial"):
            writing.record_gate(self.connection, ids["fixture"], artifact, verdict="pass", findings=[], reason="Explicit fixture verdict",
                                reviewed_digest=digest, who="operator")
        piece = next(p for p in json.loads(self.request("/api/writing", headers=cookie)[2])["pieces"] if p["id"] == ids["fixture"])
        self.assertEqual(self.post(f"/api/items/{piece['id']}/stage", {"revision": piece["revision"], "stage": "ready"})[0], 200)
        piece = next(p for p in json.loads(self.request("/api/writing", headers=cookie)[2])["pieces"] if p["id"] == ids["fixture"])
        self.assertEqual((piece["stage"], piece["ready_recorded"]), ("ready", True))
        self.assertEqual(self.post(f"/api/items/{piece['id']}/park", {"revision": piece["revision"]})[0], 200)

    def test_the_stage_and_park_writes_queue_the_piece_and_a_failed_queue_changes_neither(self):
        # pack sd:7243: no one designates a piece; the dashboard's Stage and Park ask the pack to queue it after their
        # write, as the sd writing verbs do. Failure table, "Verb or page write, then enqueue": the write stands.
        from sd_dashboard import publishing

        ids = self.seed()
        cookie = {"Cookie": self.cookie}
        queued = []
        enqueue = self.enterContext(mock.patch.object(publishing.kit(), "enqueue_pieces",
                                                      side_effect=lambda connection, items: queued.append(items) or []))
        piece = next(p for p in json.loads(self.request("/api/writing", headers=cookie)[2])["pieces"] if p["id"] == ids["fixture"])
        self.assertEqual(self.post(f"/api/items/{piece['id']}/stage", {"revision": piece["revision"], "stage": "review"})[0], 200)
        self.assertEqual(queued, [{ids["fixture"]}])
        enqueue.side_effect = OSError("queue unwritable")
        piece = next(p for p in json.loads(self.request("/api/writing", headers=cookie)[2])["pieces"] if p["id"] == ids["fixture"])
        self.assertEqual(self.post(f"/api/items/{piece['id']}/park", {"revision": piece["revision"]})[0], 200)
        self.assertEqual(enqueue.call_count, 2)
        piece = next(p for p in json.loads(self.request("/api/writing", headers=cookie)[2])["pieces"] if p["id"] == ids["fixture"])
        self.assertTrue(piece["parked"])


def piece(id, title, stage, **fields):
    entry = {"id": id, "title": title, "stage": stage, "parked": False, "piece": f"2026/{title.lower().replace(' ', '-')}", "repo": "writing-pack",
             "folder": "content", "updated": "2026-09-20T10:00:00+00:00", "revision": f"{id:064x}", "gates_ok": False, "problems": [],
             "owner": "row", "ready_recorded": False, "publication": None, "next": [], "corrections": [],
             "files": {"index.md": {"bytes": 4096, "mtime": "2026-09-20T10:00:00Z"}, "research.md": {"bytes": 2048, "mtime": "2026-09-19T10:00:00Z"},
                       "fact-check.md": {"bytes": 1024, "mtime": "2026-09-19T10:00:00Z"}, "adversarial.md": {"bytes": 1024, "mtime": "2026-09-19T10:00:00Z"}},
             "draft": ["First paragraph.", "Second paragraph."], "draft_cut": False, "words": 1200, "grill": {"open": [], "answered": 0},
             "adversarial": {"state": "stale", "detail": "reconciled with 0123456789ab, draft is now ba9876543210"}}
    entry.update(fields)
    return entry


#: One piece per lamp story: a drafting piece with a missing fact-check, a ready-gated review piece, a parked one and a
#: file-owned one.
DOC = {"read": NOW, "stages": list(writing.STAGES), "pieces": [
    piece(11, "Three Budgets", "drafting", next=["review"], corrections=["researching"],
          problems=["fact-check.md is missing", "fact-check has no explicit current gate record; record its verdict and findings",
                    "adversarial has no explicit current gate record; record its verdict and findings"],
          files={"index.md": {"bytes": 4096, "mtime": "2026-09-20T10:00:00Z"}, "research.md": {"bytes": 2048, "mtime": "2026-09-19T10:00:00Z"},
                 "fact-check.md": None, "adversarial.md": {"bytes": 1024, "mtime": "2026-09-19T10:00:00Z"}}),
    piece(12, "Gated Review", "review", gates_ok=True, next=["ready"], corrections=["researching", "drafting"]),
    piece(13, "Parked Idea", "accepted", parked=True),
    piece(14, "File Owned", "review", owner="file", next=["ready"], gates_ok=True),
]}
DOC["attention"] = writing_screen._attention(DOC["pieces"])
#: pack sd:7244: the inbox ideas, highest score first, IDEAS of them beside the inbox count.
DOC["ideas"] = {"state": "read", "reason": "", "count": 207, "top": 10, "areas": {"Personal": 150, "Work": 50, "": 7}, "rows": [
    {"item": 501, "title": "Gates are cheaper than rework", "score": 9.0, "url": "https://example.test/gates",
     "href": "https://example.test/gates", "created": "2026-10-01", "area": "Work"},
    {"item": 502, "title": "An idea with no source", "score": None, "url": "", "href": None, "created": "2026-09-30", "area": "Personal"}]}

#: One piece per gate lamp the badge counts, beside the stories above (sd:3446).
LAMPS = [
    piece(21, "Failed Check", "review", problems=["fact-check verdict is not pass", "fact-check finding F1 is unresolved"]),
    piece(22, "Open Finding", "review", problems=["adversarial finding A2 is unresolved"]),
    piece(23, "Stale Research", "drafting", problems=["research.md is unstamped against this draft"]),
    piece(24, "No Research", "drafting", problems=["research.md is missing"], files={"index.md": {"bytes": 1, "mtime": NOW}}),
    piece(25, "Other Problem", "review", problems=["adversarial file names another draft"]),
    piece(26, "Unrecorded", "review", problems=["fact-check has no explicit current gate record; record its verdict and findings"]),
]

#: The design's piece commands (design source products/system/designs/pages/writing.js): id, label, key, risk, bulk.
#: Stage is `confirm` here, not the design's `undo`: going back is a correction, which takes a reason.
COMMANDS = [
    ["piece.stage", "Stage", "s", "confirm", False],
    ["piece.correct", "Correct", "o", "safe", False],
    ["piece.park", "Park", "p", "undo", True],
    ["piece.revive", "Revive", "v", "undo", True],
    ["piece.readiness", "Readiness", "r", "safe", False],
    ["piece.publish", "Publish", "u", "confirm", False],
    ["piece.draft", "Show draft", "d", "safe", False],
    ["piece.grill", "Grill", None, "safe", False],
    ["piece.apply-answers", "Apply answers", None, "safe", False],
    ["piece.review", "Refresh review", None, "safe", False],
    ["piece.apply-review", "Apply review", None, "safe", False],
    ["queue.decline", "Decline", None, "confirm", False],
]

SHELL_MORE = r"""
window.shell.row = () => null; window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
window.shell.setContext = () => {}; C.pick = () => {};
var DIALOG = null, pageMake = document.createElement;
document.createElement = t => { var e = pageMake(t); if (t === 'dialog') { e.querySelector = s => document.getElementById(s.slice(1)); DIALOG = e; } return e; };
var navigator = {};
"""


class TheScript(unittest.TestCase):
    """writing.js against a document with one piece per lamp story."""

    def run_page(self, body, answer=None, search="", prelude=""):
        answer = answer or "(path) => path === '/api/writing' ? [200, DOC] : [200, { revision: 'f'.repeat(64) }]"
        script = (STAND_IN + f"location.search = {json.dumps(search)};\nvar DOC = {json.dumps(DOC)};\n" + MARKUP_JS
                  + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + READ_SHELL + f"\nANSWER = {answer};\n" + prelude + COMMANDS_JS + PAGE_JS
                  + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.page_attention = window.PAGE_ATTENTION; OUT.html = Object.fromEntries(Object.entries(ELS).map(([k, e]) => [k, e.html]));"
                  + " OUT.text = Object.fromEntries(Object.entries(ELS).map(([k, e]) => [k, e.textContent])); OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    #: The realms /ui/sections.js hands a signed-in page, and the shell's view switch with the hidden views it was told.
    REALMS = ("""globalThis.SHELL_REALMS = [{ id: 'brand', label: 'Brand' }, { id: 'work', label: 'Work' }, { id: 'editor', label: 'Clash' }, { id: 'menu', label: 'Clash' }, { id: 'btn', label: 'Clash' }];
var VIEW = 'personal'; OUT.hidden = {};
window.shell.view = { current: () => VIEW, show: () => true, count: () => {}, hide: (id, on) => { OUT.hidden[id] = on; } };
DOC.pieces.forEach(p => { p.realm = p.id === 12 ? 'brand' : 'personal'; });
""")

    def test_realm_views_hold_their_pieces_and_a_realm_with_none_hides(self):
        # sd:5064: Personal, then the realms; a piece is in its repo's realm (places.realm_of), and Work, which holds no piece,
        # hides. A realm named editor would take this page's editor panel, so it is dropped, and so are menu and btn, the
        # layout menu's ids (view-menu, view-btn, sd:5641); b and e stay Writing's keys. sd:7213: no All view, and a stays
        # unused, so the realms keep the keys they had.
        out = self.run_page("""R.views = window.PAGE_VIEWS.map(v => [v.id, v.key]); R.personal = ELS['view-board'].html;
VIEW = 'brand'; document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'brand' } })); await flush(); R.brand = ELS['view-board'].html;""",
                            prelude=self.REALMS)
        r = out["R"]
        self.assertEqual(r["views"], [["personal", "p"], ["brand", "r"], ["work", "w"], ["hoa", "h"]])
        self.assertEqual(out["hidden"], {"brand": False, "work": True, "hoa": True})  # Personal never hides (sd:5642)
        def has(board):
            return ["Three Budgets" in board, "Gated Review" in board, "File Owned" in board]
        self.assertEqual((has(r["personal"]), has(r["brand"])), ([True, False, True], [False, True, False]))
        # Each realm reads again, so the reader's objects are the pieces the view shows.
        self.assertEqual(out["gets"], ["/api/writing"] * 2)

    def test_every_piece_sits_in_one_view_and_an_old_all_link_opens_personal(self):
        """sd:7213: with no All view, a piece of no realm, of a realm list not read (None), of a realm with no view here
        (editor is dropped, ghost is not configured) shows in Personal; a realm's piece in its realm's view."""
        realms = self.REALMS + ("var REALM_OF = { 11: null, 12: 'brand', 13: 'editor', 14: 'ghost' };\n"
                                "DOC.pieces.forEach(p => { p.realm = REALM_OF[p.id]; p.parked = false; });\n"
                                "location.hash = '#view=all'; var history = { state: null, replaceState: (s, t, u) => { OUT.replaced = u; } };\n")
        out = self.run_page("""R.views = window.PAGE_VIEWS.map(v => v.id); R.board = {};
for (const v of R.views) { VIEW = v; document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: v } })); await flush(); R.board[v] = ELS['view-board'].html; }""",
                            prelude=realms)
        r = out["R"]
        self.assertNotIn("all", r["views"])
        where = {title: [v for v, board in r["board"].items() if title in board]
                 for title in ("Three Budgets", "Gated Review", "Parked Idea", "File Owned")}
        self.assertEqual(where, {"Three Budgets": ["personal"], "Gated Review": ["brand"], "Parked Idea": ["personal"],
                                 "File Owned": ["personal"]})
        self.assertEqual(out["hidden"], {"brand": False, "work": True, "hoa": True})
        self.assertEqual(out["replaced"], "#view=personal", "an old #view=all link did not land on Personal")

    def test_an_hoa_piece_sits_in_the_hoa_view_not_personal(self):
        """pack sd:7687: a piece of the hoa checkout (realm hoa, places.realm_of) shows under HOA, which then shows, and
        leaves Personal, as on Tasks. A realm line named hoa takes no second view."""
        realms = self.REALMS + "DOC.pieces.forEach(p => { p.realm = p.id === 12 ? 'hoa' : null; p.parked = false; });\n"
        out = self.run_page("""R.views = window.PAGE_VIEWS.map(v => v.id); R.board = {};
for (const v of R.views) { VIEW = v; document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: v } })); await flush(); R.board[v] = ELS['view-board'].html; }""",
                            prelude=realms)
        r = out["R"]
        self.assertEqual(r["views"], ["personal", "brand", "work", "hoa"])
        self.assertEqual([v for v, board in r["board"].items() if "Gated Review" in board], ["hoa"])
        self.assertIn("Three Budgets", r["board"]["personal"])
        self.assertEqual(out["hidden"], {"brand": True, "work": True, "hoa": False})
        taken = self.run_page("R.views = window.PAGE_VIEWS.map(v => [v.id, v.label]);",
                              prelude=realms + "SHELL_REALMS.push({ id: 'hoa', label: 'Hoa2' });\n")["R"]["views"]
        self.assertEqual(taken, [["personal", "Personal"], ["brand", "Brand"], ["work", "Work"], ["hoa", "HOA"]])

    def test_the_board_has_a_tips_column_beside_ideas_and_each_has_its_anchor(self):
        """sd:8013: Today links /writing#ideas and /writing#tips; the Tips column follows Ideas."""
        tips = ("DOC.tips = { state: 'read', reason: '', count: 33, top: 10, areas: {}, rows: [{ item: 601, title: 'Shell tip',"
                " score: 6, url: '', href: null, created: '2026-10-01', area: '' }] };\n")
        board = self.run_page("", prelude=tips)["html"]["view-board"]
        self.assertLess(board.index('id="ideas"'), board.index('id="tips"'))
        self.assertLess(board.index('id="tips"'), board.index('data-drop="stage:accepted"'))
        col = board.split('id="tips"')[1].split('data-drop="stage:accepted"')[0]
        self.assertIn('<span class="label">Tips · vault</span><span class="n" title="inbox tips in the vault">33</span>', col)
        self.assertIn("Shell tip", col)
        self.assertIn('<a href="/tasks?base=tip">Tasks</a>', col)

    #: sd:8014: the Blog Ideas inbox's waiting rows with their age, and one row a bulk Decline moved.
    QUEUED = ("DOC.ideas.pool = ["
              "{ item: 701, title: 'Stale low', score: 3, age: 90, revision: 'a'.repeat(64) },"
              "{ item: 702, title: 'Stale unscored', score: null, age: 45, revision: 'b'.repeat(64) },"
              "{ item: 703, title: 'Stale high', score: 8, age: 90, revision: 'c'.repeat(64) },"
              "{ item: 704, title: 'Fresh low', score: 2, age: 5, revision: 'd'.repeat(64) }];\n"
              "DOC.ideas.declined = [{ item: 650, title: 'Gone stale', at: '2026-09-01T10:00:00Z',"
              " by: 'bulk Decline (score below 6, older than 30 days)' }];\n")

    def test_each_inbox_has_a_bulk_decline_that_asks_and_posts_the_rows_it_names(self):
        """sd:8014: the button names the rows the defaults move; the press asks first and posts those rows."""
        answer = ("(path, body) => path === '/api/writing' ? [200, DOC] : path === '/api/triage/decline'"
                  " ? [200, { declined: body.items.map(i => i.item), refused: [], by: 'x' }] : [404, {}]")
        out = self.run_page("R.board = ELS['view-board'].html; R.cli = cmd('queue.decline').cli(C.get('queue:blog-idea'), { score: '9', days: '60' });"
                            " R.said = cmd('queue.decline').consequence(C.get('queue:blog-idea'), { score: '9', days: '60' });"
                            " shellRun(cmd('queue.decline'), C.get('queue:blog-idea')); await flush();",
                            answer=answer, prelude=self.QUEUED)
        ideas = out["R"]["board"].split('id="ideas"')[1].split('id="tips"')[0]
        label = "Decline 2 below score 6 older than 30 days"
        self.assertIn(f'data-cmd="queue.decline" data-obj="queue:blog-idea" title="{label}"><span class="lbl">{label}</span></button>',
                      ideas)
        self.assertEqual(out["R"]["cli"],
                         "no sd verb: Decline 2 below score 9 older than 60 days posts /api/triage/decline: #701 Stale low · #703 Stale high")
        self.assertEqual(out["confirms"], ["queue.decline"])
        self.assertEqual(out["R"]["said"], "Decline 2 below score 9 older than 60 days in Blog Ideas inbox: #701 Stale low"
                         " · #703 Stale high. They stay listed under Declined; nothing is removed.")
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/triage/decline", {"kind": "blog-idea", "score": 6, "days": 30, "items": [
            {"item": 701, "revision": "a" * 64}, {"item": 702, "revision": "b" * 64}]}]])
        self.assertIn(["Declined 2 in Blog Ideas inbox", False], out["toasts"])

    def test_the_declined_filter_is_off_until_pressed_and_lists_when_and_by(self):
        """sd:8014: declined rows stay visible: the Declined filter lists each with when and by which control."""
        out = self.run_page("""R.off = ELS['view-board'].html;
ELS['view-board'].listeners.click.forEach(f => f({ target: { closest: s => s === '[data-declined]' ? { dataset: { declined: 'ideas' } } : null } }));
R.on = ELS['view-board'].html;""", prelude=self.QUEUED)
        self.assertIn('data-declined="ideas" aria-pressed="false">Declined <span class="n">1</span>', out["R"]["off"])
        self.assertNotIn("Gone stale", out["R"]["off"])
        self.assertIn('data-declined="ideas" aria-pressed="true"', out["R"]["on"])
        self.assertIn("#650 Gone stale", out["R"]["on"])
        self.assertIn("by bulk Decline (score below 6, older than 30 days)", out["R"]["on"])

    def test_a_realm_tab_counts_open_pieces_only(self):
        """sd:8018: a published or declined piece leaves the tab's count."""
        closed = ("DOC.pieces.push(Object.assign({}, DOC.pieces[1], { id: 15, title: 'Out Already', stage: 'published' }),"
                  " Object.assign({}, DOC.pieces[1], { id: 16, title: 'Said No', stage: 'declined' }));\n")
        out = self.run_page("", prelude=closed)
        self.assertEqual(out["counts"]["personal"], 4, "sd:8018: the tab counted a closed piece")

    def test_personal_shows_with_no_piece_in_it_and_says_where_the_pieces_are(self):
        # sd:5642: Personal is a tab for personal writing, as on Tasks: it stays when every piece's repo is in a realm line,
        # and its empty board says why, not that no piece is registered.
        brand = self.REALMS + "DOC.pieces.forEach(p => { p.realm = 'brand'; }); DOC.realms = { state: %s };\n"
        out = self.run_page("", prelude=brand % "'read'")
        self.assertEqual(out["hidden"], {"brand": False, "work": True, "hoa": True}, "Personal was hidden, or a realm was not told")
        self.assertEqual(out["states"][-1]["title"], "No personal pieces")
        self.assertEqual(out["states"][-1]["text"], "Pieces from a repo a realm claims show in that realm: writing-pack under Brand."
                         " dashboard.realms on Settings decides which repos each realm claims.")
        # A realm list that was not read leaves each piece's realm unknown (places.realm_of None); the board says so.
        out = self.run_page("", prelude=brand % "'error'")
        self.assertIn("The dashboard.realms list was not read", out["states"][-1]["text"])

    # sd:5641: Board and Editor are one icon button whose menu lists the two, the picker Tasks uses (shell.viewPick).
    VIEW_MENU = r"""const items = ['board', 'editor'].map(v => { const b = El('item-' + v); b.dataset.view = v; b.attrs = {};
  b.setAttribute = (k, x) => { b.attrs[k] = String(x); }; b.getAttribute = k => k in b.attrs ? b.attrs[k] : null; b.focus = () => { document.activeElement = b; OUT.focus.push(v); }; return b; });
OUT.focus = []; const btn = document.getElementById('view-btn'), menu = document.getElementById('view-menu'); btn.attrs = {}; btn.setAttribute = (k, x) => { btn.attrs[k] = String(x); };
btn.focus = () => { document.activeElement = btn; OUT.focus.push('button'); };
menu.querySelectorAll = () => items; menu.hidden = true;
R.wired = !!btn.listeners.click; if (!R.wired) return;
"""

    def test_the_view_menu_picks_the_layout_and_the_button_shows_its_icon(self):
        out = self.run_page(self.VIEW_MENU + """window.writingView('board'); R.board = [btn.attrs['aria-label'], items.map(b => b.attrs['aria-checked'])];
btn.listeners.click[0]({ target: btn }); R.open = [menu.hidden, btn.attrs['aria-expanded'], OUT.focus.slice()];
menu.listeners.click[0]({ target: { closest: s => s === '[data-view]' ? items[1] : null } }); await flush();
R.pick = [menu.hidden, btn.attrs['aria-label'], items.map(b => b.attrs['aria-checked']), OUT.focus.at(-1), ELS['view-editor'].hidden, ELS['view-board'].hidden];
R.icon = btn.html;""")
        r = out["R"]
        self.assertTrue(r["wired"], "writing.js wires no view menu on #view-btn")
        self.assertEqual(r["board"], ["View: Board", ["true", "false"]])
        self.assertEqual(r["open"], [False, "true", ["board"]])
        self.assertEqual(r["pick"], [True, "View: Editor", ["false", "true"], "button", False, True])
        self.assertIn('<use href="#i-pen-line"/>', r["icon"])
        self.assertEqual(out["urls"][-1], {"view": "editor"})

    def test_the_view_menu_markup_is_the_tasks_picker_with_board_and_editor(self):
        page = (V2 / "writing.html").read_text(encoding="utf-8")
        self.assertIn('<button type="button" class="viewpick-btn" id="view-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="view-menu" aria-label="View: Board">', page)
        self.assertIn('<div class="actmenu viewmenu" id="view-menu" role="menu" aria-label="View" hidden>', page)
        self.assertEqual(re.findall(r'role="menuitemradio" data-view="(\w+)" aria-checked="(\w+)"><svg class="i" aria-hidden="true"><use href="#i-([\w-]+)"/></svg><span>(\w+)</span><kbd>(v \w)</kbd>', page),
                         [("board", "true", "kanban", "Board", "v b"), ("editor", "false", "pen-line", "Editor", "v e")])
        self.assertNotIn('class="seg" role="group" aria-label="View"', page)

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.label, c.key, c.risk, !!c.bulk]); R.exec = cmd('piece.publish').executes;")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        self.assertIs(out["R"]["exec"], False)

    def test_stage_asks_first_posts_the_next_stage_with_the_revision_and_rereads(self):
        out = self.run_page("shellRun(cmd('piece.stage'), C.get('12')); await flush();")
        self.assertEqual(out["confirms"], ["piece.stage"])
        self.assertEqual(out["posts"], [["/api/items/12/stage", {"stage": "ready", "revision": f"{12:064x}"}, 64]])
        self.assertEqual(out["gets"], ["/api/writing", "/api/writing"], "the page did not reread after the write")
        self.assertEqual(out["toasts"][-1], ["#12 Review → Ready · readiness recorded", False])

    #: pack sd:6700: piece 12 with two open grill questions, one with options and one in words, and one answered.
    GRILLED = ("DOC.pieces[1].grill = { open: [{ note: 71, question: 'Who is this for?', options: ['Engineers', 'Managers'] },"
               " { note: 72, question: 'What is the one point?', options: [] }], answered: 1 };\n")

    def test_grill_asks_first_and_posts_fix_with_grill_ask(self):
        out = self.run_page("R.off = [cmd('piece.grill').when(C.get('12')), cmd('piece.grill').when(C.get('13'))];"
                            " shellRun(cmd('piece.grill'), C.get('12')); await flush();",
                            answer="(path) => path === '/api/writing' ? [200, DOC] : [200, { fix: { session: 'grill', pane: 'w9:p1' } }]")
        self.assertEqual(out["R"]["off"], [True, True])
        self.assertEqual(out["confirms"], ["piece.grill"])
        self.assertEqual(out["posts"][0][:2], ["/api/items/12/fix", {"grill": "ask", "revision": f"{12:064x}"}])
        self.assertEqual(out["toasts"][-1], ["Grill started · #12 · herdr pane w9:p1", False])
        self.assertEqual(out["gets"][-1], "/api/writing", "the page did not read again once the session started")
        out = self.run_page("R.off = cmd('piece.grill').when(C.get('12'));", prelude=self.GRILLED)
        self.assertEqual(out["R"]["off"], "2 grill questions open; answer them first")

    def test_a_failed_grill_or_apply_review_launch_reads_the_pieces_again(self):
        """pack sd:7783: a failed launch (5xx) notes its cause on the piece, and a post with no answer may have noted it,
        so either moves the revision the page holds: Writing reads again, or the next press is refused stale. A refusal
        that wrote nothing (400) reads nothing."""
        newer = "DOC.pieces[1].adversarial = { state: 'current', detail: 'draft ba9876543210' };\n"
        failed = "(path) => path === '/api/writing' ? [200, DOC] : [503, { error: 'Fix failed: herdr.sh launch: no session.' }]"
        silent = "(path) => { if (path !== '/api/writing') throw new TypeError('Load failed'); return [200, DOC]; }"
        refused = "(path) => path === '/api/writing' ? [200, DOC] : [400, { error: 'item 12 is done' }]"
        # A press refused stale (409) wrote nothing, but the page holds an old revision: it reads again, as `write` does.
        stale = "(path) => path === '/api/writing' ? [200, DOC] : [409, { error: 'item 12 changed; read it again' }]"
        for command in ("piece.grill", "piece.apply-review"):
            for answer, reads in ((failed, 1), (silent, 1), (stale, 1), (refused, 0)):
                out = self.run_page(f"const before = OUT.gets.length; shellRun(cmd('{command}'), C.get('12')); await flush();"
                                    " await flush(); R.reads = OUT.gets.slice(before).filter(g => g === '/api/writing').length;",
                                    answer=answer, prelude=newer)
                self.assertEqual(out["R"]["reads"], reads, (command, answer))

    def test_apply_answers_waits_for_every_answer_then_posts_fix_with_grill_apply(self):
        out = self.run_page("R.off = [cmd('piece.apply-answers').when(C.get('12')), cmd('piece.apply-answers').when(C.get('11'))];",
                            prelude=self.GRILLED)
        self.assertEqual(out["R"]["off"], ["2 grill questions open; answer them first", "no grill question answered yet; press Grill first"])
        done = "DOC.pieces[1].grill = { open: [], answered: 2 };\n"
        out = self.run_page("R.on = cmd('piece.apply-answers').when(C.get('12')); shellRun(cmd('piece.apply-answers'), C.get('12')); await flush();",
                            answer="(path) => path === '/api/writing' ? [200, DOC] : [200, { fix: { session: 'apply-answers', pane: 'w9:p2' } }]",
                            prelude=done)
        self.assertIs(out["R"]["on"], True)
        self.assertEqual(out["confirms"], ["piece.apply-answers"])
        self.assertEqual(out["posts"][0][:2], ["/api/items/12/fix", {"grill": "apply", "revision": f"{12:064x}"}])
        self.assertEqual(out["toasts"][-1], ["Apply answers started · #12 · herdr pane w9:p2", False])

    def test_details_walks_the_open_questions_and_posts_each_answer(self):
        # An option button posts that option; the free-text one posts what is typed. Each answer reads the pieces again.
        click = ("const d = ELS.details; d.querySelector = s => s === '#grill-text' ? { value: '  The cost of a gate  ' } : null;"
                 " const at = v => ({ target: { closest: s => s === '[data-grill-answer]' ? { dataset: { grillAnswer: v } } : null } });")
        out = self.run_page("document.dispatchEvent(new CustomEvent('shell:open', { detail: '12' })); R.html = ELS.details.html; " + click
                            + " d.listeners.click.forEach(f => f(at('Managers'))); await flush();", prelude=self.GRILLED)
        html = out["R"]["html"]
        self.assertIn("Question 1 of 2", html)
        self.assertIn("Who is this for?", html)
        self.assertIn('data-grill-answer="Engineers"', html)
        self.assertIn("1 answered", html)
        self.assertEqual(out["posts"], [["/api/decisions/answer", {"item": 12, "note": 71, "option": "Managers", "revision": f"{12:064x}"}, 64]])
        self.assertEqual(out["gets"][-1], "/api/writing")
        words = "DOC.pieces[1].grill.open.shift();\n"
        out = self.run_page("document.dispatchEvent(new CustomEvent('shell:open', { detail: '12' })); R.html = ELS.details.html; " + click
                            + " d.listeners.click.forEach(f => f(at(''))); await flush();", prelude=self.GRILLED + words)
        self.assertIn("Question 1 of 1", out["R"]["html"])
        self.assertIn('id="grill-text"', out["R"]["html"])
        self.assertEqual(out["posts"][0][1], {"item": 12, "note": 72, "option": "The cost of a gate", "revision": f"{12:064x}"})

    def test_refresh_review_starts_the_pieces_adversarial_review(self):
        """pack sd:6699: Refresh review posts the shared adversarial route with kind piece."""
        run = "{ id: 'piece-12-20260920T100000Z', kind: 'piece', item: 12, result: '/w/adversarial.md'"
        answer = (f"(path) => path === '/api/writing' ? [200, DOC] : path === '/api/adversarial' ? [202, {run}, state: 'queued' }}]"
                  f" : [200, {run}, state: 'done', filed: 'written' }}]")
        out = self.run_page("shellRun(cmd('piece.review'), C.get('12')); await flush(); await flush();", answer=answer)
        self.assertEqual(out["posts"][0][:2], ["/api/adversarial", {"kind": "piece", "target": "12"}])
        toasts = [t[0] for t in out["toasts"]]
        self.assertIn("Review started · run piece-12-20260920T100000Z", toasts)
        self.assertIn("Adversarial review written · run piece-12-20260920T100000Z · /w/adversarial.md", toasts)
        self.assertEqual(out["gets"][-1], "/api/writing", "the page did not read again once the review landed")

    def test_apply_review_shows_once_adversarial_md_answers_the_current_draft(self):
        """pack sd:6699: Apply review asks first, then posts Fix with apply: adversarial and the revision. pack sd:6977:
        the report's digest stamp decides, not file times: a newer stale report stays off, an older current one is on."""
        stale = "DOC.pieces[1].files['adversarial.md'] = { bytes: 1024, mtime: '2026-09-21T10:00:00Z' };\n"
        out = self.run_page("R.off = [cmd('piece.apply-review').when(C.get('12')), cmd('piece.apply-review').when(C.get('11'))];",
                            prelude=stale)
        self.assertEqual(out["R"]["off"], ["adversarial.md is stale: reconciled with 0123456789ab, draft is now ba9876543210;"
                                           " run Refresh review first"] * 2)
        newer = "DOC.pieces[1].adversarial = { state: 'current', detail: 'draft ba9876543210' };\n"
        out = self.run_page("R.on = cmd('piece.apply-review').when(C.get('12')); shellRun(cmd('piece.apply-review'), C.get('12')); await flush();",
                            answer="(path) => path === '/api/writing' ? [200, DOC] : [200, { fix: { session: 'apply-review', pane: 'w9:p1' } }]",
                            prelude=newer)
        self.assertIs(out["R"]["on"], True)
        self.assertEqual(out["confirms"], ["piece.apply-review"])
        self.assertEqual(out["posts"][0][:2], ["/api/items/12/fix", {"apply": "adversarial", "revision": f"{12:064x}"}])
        self.assertEqual(out["toasts"][-1], ["Apply review started · #12 · herdr pane w9:p1", False])

    def test_the_page_loads_the_shared_commands_before_its_script(self):
        page = (V2 / "writing.html").read_text(encoding="utf-8")
        self.assertLess(page.index('<script src="/ui/commands.js">'), page.index('<script src="/ui/writing.js">'))

    def test_off_moves_name_their_reason(self):
        out = self.run_page("const w = (id, key) => cmd(id).when(C.get(key));"
                            " R.off = [w('piece.stage', '13'), w('piece.stage', '14'), w('piece.park', '14'), w('piece.revive', '12'), w('piece.correct', '13'), w('piece.stage', '11')];")
        self.assertEqual(out["R"]["off"], ["Parked. Revive it first; a parked piece keeps its stage.",
                                           "The piece files own this piece; the dashboard moves it after the writing cutover.",
                                           "the piece files own this piece; the dashboard moves it after the writing cutover",
                                           "not parked", "parked; revive first", True])

    def test_park_posts_its_route_and_undo_revives_with_the_revision_park_returned(self):
        out = self.run_page("shellRun(cmd('piece.park'), C.get('12')); await flush(); lastUndo().undo(); await flush();")
        self.assertEqual(out["posts"], [["/api/items/12/park", {"revision": f"{12:064x}"}, 64], ["/api/items/12/revive", {"revision": "f" * 64}, 64]])
        self.assertEqual(out["toasts"][0], ["#12 parked", True])
        self.assertEqual(out["toasts"][-1], ["Park undone · #12 Gated Review", False])

    def test_correct_asks_for_a_reason_and_posts_it_as_a_correction(self):
        out = self.run_page("shellRun(cmd('piece.correct'), C.get('12')); ELS['to-in'].value = 'drafting'; ELS['why-in'].value = 'The argument needs a rewrite';"
                            " DIALOG.returnValue = 'ok'; DIALOG.onclose(); await flush();")
        self.assertEqual(out["posts"], [["/api/items/12/stage", {"stage": "drafting", "correct": True, "reason": "The argument needs a rewrite", "revision": f"{12:064x}"}, 64]])
        self.assertEqual(out["toasts"][-1], ["#12 corrected to Drafting", False])

    def test_a_correction_without_a_reason_posts_nothing(self):
        out = self.run_page("shellRun(cmd('piece.correct'), C.get('12')); ELS['why-in'].value = '  '; DIALOG.returnValue = 'ok'; DIALOG.onclose(); await flush();")
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["toasts"][-1], ["#12 not moved.", False])

    def test_a_refused_write_says_why_and_reads_again(self):
        answer = "(path) => path === '/api/writing' ? [200, DOC] : [409, { error: 'item 12 changed; reload it before applying this change' }]"
        out = self.run_page("shellRun(cmd('piece.stage'), C.get('12')); await flush();", answer=answer)
        self.assertEqual(out["toasts"][-1], ["#12 Gated Review not changed: item 12 changed; reload it before applying this change", False])
        self.assertEqual(out["gets"], ["/api/writing", "/api/writing"])
        self.assertEqual(out["states"][-2]["kind"], "loading", "a refused write reads again with load()")

    def test_publish_stays_copy_only_and_capture_posts_the_idea(self):
        """sd:5887: Enter captures through /api/actions/run; Publish posts outside, so it stays copy only and posts nothing."""
        out = self.run_page("""const was = ANSWER;
ANSWER = (p, b) => p === '/api/actions/run' ? [200, { action: b.action, label: 'Capture idea', line: 'l', exit: R.code || 0, output: 'kind not installed', cut: false }] : was(p, b);
shellRun(cmd('piece.publish'), Object.assign({}, C.get('12'))); await flush(); R.posted = OUT.posts.length;
const enter = () => ELS['shift-in'].listeners.keydown[0]({ key: 'Enter', preventDefault() {} });
ELS['shift-in'].value = 'A new idea'; enter(); await flush(); R.cleared = ELS['shift-in'].value;
R.code = 1; ELS['shift-in'].value = 'Another idea'; enter(); await flush(); R.kept = ELS['shift-in'].value;""")
        self.assertEqual(out["R"]["posted"], 0)
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/actions/run", {"action": "idea.capture", "text": "A new idea"}],
                                                         ["/api/actions/run", {"action": "idea.capture", "text": "Another idea"}]])
        toasts = [t[0] for t in out["toasts"]]
        self.assertIn("Idea captured in the vault inbox: A new idea", toasts)
        self.assertEqual(toasts[-1], "Idea not captured: sd store add sdw.blog-idea 'Another idea' --field status=inbox exited 1: kind not installed")
        # A failed capture puts the idea back, so nothing typed is lost.
        self.assertEqual((out["R"]["cleared"], out["R"]["kept"]), ("", "Another idea"))

    def test_the_board_lamps_strip_and_badge_come_from_the_document(self):
        out = self.run_page("")
        board = out["html"]["view-board"]
        # Columns in stage order: Drafting holds #11, Review holds #12 and #14.
        self.assertEqual(re.findall(r'<article class="card[^"]*"[^>]*data-id="(\d+)"', board), ["11", "12", "14"])
        self.assertNotIn('data-id="13"', board, "a parked piece shows only with Parked on")
        card = re.search(r'<article class="card"[^>]*data-id="11".*?</article>', board, re.S).group(0)
        self.assertEqual(re.findall(r'data-s="(\w+)"', card), ["ok", "ok", "queued", "unknown"])
        self.assertIn("<b>3</b> active", out["html"]["strip"])
        self.assertEqual(out["page_attention"], {"state": "ok", "n": 0, "what": "pieces need a gate"})
        # sd:6701: Open | Parked, the shell's set-aside group, with each count (pack sd:7263 dropped All).
        self.assertEqual(re.findall(r'data-v="(\w+)" aria-pressed="(\w+)">\w+ <span class="n">(\d+)</span>', out["html"]["parked-seg"]),
                         [("open", "true", "3"), ("aside", "false", "1")])

    def test_the_ideas_column_lists_the_inbox_ideas_and_the_strip_counts_them(self):
        """pack sd:7244: each idea card has its score and its title, a link only where the read gave an http href; the
        header and the strip carry the inbox count. A failed read keeps the column unknown with its reason."""
        out = self.run_page("")
        col = re.search(r'<div class="col vault".*?</div></div></div>', out["html"]["view-board"], re.S).group(0)
        self.assertIn('<span class="n" title="inbox ideas in the vault">207</span>', col)
        self.assertEqual(re.findall(r'<span class="tag">([^<]+)</span>', col), ["9/10", "no score"])
        self.assertIn('<a class="ext" href="https://example.test/gates" target="_blank" rel="noopener noreferrer">Gates are cheaper than rework</a>', col)
        self.assertIn('<div class="t">An idea with no source</div>', col)
        self.assertIn("Highest 2 of 207 inbox ideas.", col)
        self.assertNotIn("data-id=", col, "an idea card took a piece's selection")
        self.assertIn("<b>207</b> ideas in the vault inbox", out["html"]["strip"])
        self.assertNotIn("vault ideas not read", out["html"]["strip"])
        unread = "DOC.ideas = { state: 'unknown', reason: 'no such column: stage', count: null, rows: [] };\n"
        out = self.run_page("", prelude=unread)
        self.assertIn("Not read: no such column: stage", out["html"]["view-board"])
        self.assertIn("vault ideas not read", out["html"]["strip"])

    def test_the_ideas_column_has_an_area_tile_per_area_and_a_tile_narrows_it(self):
        """pack sd:7670: Personal, Work and RWBP tiles, each with its count, then No area while one has none; a tile leaves
        that area's ideas and counts them, a second press clears it, and each card names its area."""
        out = self.run_page("""const press = v => ELS['view-board'].listeners.click.forEach(f => f({ target: { closest: s =>
  s === '[data-f="area"]' ? { dataset: { f: 'area', v } } : null } }));
R.all = ELS['view-board'].html; press('Work'); R.work = ELS['view-board'].html; press('Work'); R.off = ELS['view-board'].html;""")
        def col(board):
            return re.search(r'<div class="col vault".*?</div></div></div>', board, re.S).group(0)
        r = out["R"]
        self.assertEqual(re.findall(r'data-f="area" data-v="([^"]*)" aria-pressed="(\w+)"', col(r["all"])),
                         [("Personal", "false"), ("Work", "false"), ("RWBP", "false"), ("none", "false")])
        self.assertIn('title="RWBP: 0"', col(r["all"]))
        self.assertIn('title="No area: 7"', col(r["all"]))
        self.assertIn('<span class="area">Work</span>', col(r["all"]))
        self.assertIn('data-v="Work" aria-pressed="true"', col(r["work"]))
        self.assertIn("Gates are cheaper than rework", col(r["work"]))
        self.assertNotIn("An idea with no source", col(r["work"]))
        self.assertIn("Highest 1 of 50 Work ideas.", col(r["work"]))
        self.assertIn("An idea with no source", col(r["off"]))

    def test_the_vault_columns_body_takes_the_stretching_row_with_tiles_or_without(self):
        """pack sd:7773: `.col` gave two rows, header and body; the tiles made the vault column's children three, so the
        tiles took the 1fr row and stretched to the board's height (1069 px in headless Chrome), far above the ideas.
        Whatever the column holds, the rows `writing.css` gives it are one per child and only the last, the body, is 1fr."""
        def children(board):
            # The direct children of the vault column: each child's tag and classes.
            found, depth, inside, done = [], 0, None, False

            class Walk(HTMLParser):
                def handle_starttag(self, tag, attrs):
                    nonlocal depth, inside
                    cls = (dict(attrs).get("class") or "").split()
                    if inside is None and tag == "div" and cls[:2] == ["col", "vault"]:
                        inside = depth
                    elif inside is not None and not done and depth == inside + 1:
                        found.append((tag, set(cls)))
                    depth += 1

                def handle_endtag(self, tag):
                    nonlocal depth, done
                    depth -= 1
                    if inside is not None and depth == inside:
                        done = True

            Walk().feed(board)
            return found

        def rows_for(kids):
            # The grid-template-rows of the most specific `.col` rule that matches a vault column with these children.
            best = None
            for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", WRITING_CSS, flags=re.S)):
                rows = re.search(r"grid-template-rows:\s*([^;]+);", body)
                for sel in (s.strip() for s in sels.split(",")):
                    has = re.fullmatch(r"\.col\.vault:has\(> \.([\w-]+)\)", sel)
                    if rows and (sel in (".col", ".col.vault") or (has and any(has.group(1) in c for _, c in kids))):
                        if best is None or len(sel) >= len(best[0]):
                            best = (sel, rows.group(1).strip())
            return re.findall(r"minmax\([^)]*\)|\S+", best[1])

        tiled = children(self.run_page("")["html"]["view-board"])
        unread = "DOC.ideas = { state: 'unknown', reason: 'no such column: stage', count: null, rows: [] };\n"
        bare = children(self.run_page("", prelude=unread)["html"]["view-board"])
        self.assertEqual([tag for tag, _ in tiled], ["header", "div", "div"])
        self.assertIn("idea-areas", tiled[1][1])
        self.assertEqual([tag for tag, _ in bare], ["header", "div"])
        for kids in (tiled, bare):
            rows = rows_for(kids)
            self.assertEqual(len(rows), len(kids), (rows, kids))
            self.assertEqual([r for r in rows if "1fr" in r], [rows[-1]], rows)

    def test_no_tile_or_control_is_wider_than_its_column(self):
        """pack sd:8070: the vault column's Decline button, nowrap, was wider than its 15rem column. `.col` is a grid
        with an implicit auto track, so the track grew to the button and every tile under it ran into the next column
        (headless Chrome, 1024 to 1440 wide). Each grid from the column down to a tile caps its track at the column,
        a tile's top row (id, tags, four lamps) wraps, and a button in a column stays one line (sd:5729) but shrinks
        to the column and cuts its label with an ellipsis; the title keeps the whole label."""
        css = re.sub(r"/\*.*?\*/", "", WRITING_CSS, flags=re.S)

        def body(selector):
            return " ".join(b for sels, b in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                            if selector in (s.strip() for s in sels.split(",")))

        board = self.run_page("")["html"]["view-board"]
        # The chain the markup draws: column > body > drop > tile > top row, and the Decline button in a column.
        for cls in ("col", "drop", "card", "top", "queue-tools", "btn", "lbl"):
            self.assertRegex(board, rf'class="(?:[^"]* )?{cls}[ "]', cls)
        for grid in (".col", ".drop", ".card"):
            self.assertIn("display: grid", body(grid), grid)
            self.assertRegex(body(grid), r"grid-template-columns:\s*minmax\(0, 1fr\)", grid)
        self.assertRegex(body(".col .btn"), r"max-width:\s*100%", "a nowrap button widens its column")
        for rule in (r"min-width:\s*0", r"overflow:\s*hidden", r"text-overflow:\s*ellipsis"):
            self.assertRegex(body(".col .btn > .lbl"), rule, "a button's label runs past its column")
        self.assertRegex(board, r'data-cmd="queue\.decline"[^>]*title="Decline [^"]+"><span class="lbl">Decline ')
        self.assertRegex(body(".card .top"), r"flex-wrap:\s*wrap", "a tile's lamps run past the tile")

    def test_a_pressed_no_area_tile_that_vanishes_on_a_read_again_clears(self):
        """pack sd:7695: No area pressed, then a read again in which every idea has an area: the tile is gone, so the
        filter clears and the column shows the highest ideas again, not an empty column no tile can clear."""
        out = self.run_page("""const press = v => ELS['view-board'].listeners.click.forEach(f => f({ target: { closest: s =>
  s === '[data-f="area"]' ? { dataset: { f: 'area', v } } : null } }));
press('none'); R.none = ELS['view-board'].html;
DOC.ideas.areas = { Personal: 157, Work: 50 }; window.PAGE_AGAIN.run(); await flush(); R.again = ELS['view-board'].html;""")
        def col(board):
            return re.search(r'<div class="col vault".*?</div></div></div>', board, re.S).group(0)
        r = out["R"]
        self.assertIn('data-v="none" aria-pressed="true"', col(r["none"]))
        self.assertEqual(re.findall(r'data-f="area" data-v="([^"]*)" aria-pressed="(\w+)"', col(r["again"])),
                         [("Personal", "false"), ("Work", "false"), ("RWBP", "false")])
        self.assertIn("Gates are cheaper than rework", col(r["again"]))
        self.assertIn("Highest 2 of 207 inbox ideas.", col(r["again"]))

    def test_parked_shows_the_parked_pieces_alone_and_an_older_all_link_shows_open(self):
        def board(search):
            return re.findall(r'<article class="card[^"]*"[^>]*data-id="(\d+)"', self.run_page("", search=search)["html"]["view-board"])
        self.assertEqual(board("?parked=1"), ["13"])
        self.assertEqual(sorted(board("?parked=all")), ["11", "12", "14"])

    def test_the_badge_rule_lights_what_the_cards_lamps_light(self):
        # sd:3446: the rail's rule is writing_screen's; each card's lamps stay the page's. They must agree, piece by piece.
        global DOC
        saved = DOC
        DOC = {**saved, "pieces": saved["pieces"] + LAMPS}
        DOC["attention"] = writing_screen._attention(DOC["pieces"])
        try:
            out = self.run_page("")
        finally:
            DOC = saved
        board = out["html"]["view-board"]
        for entry in DOC["pieces"] + LAMPS:
            if entry["parked"]:
                continue
            card = re.search(rf'<article class="card[^"]*"[^>]*data-id="{entry["id"]}".*?</article>', board, re.S).group(0)
            lit = re.findall(r'data-s="(\w+)"', card)
            shown = "warning" if "warning" in lit else "caution" if "caution" in lit else ""
            self.assertEqual(writing_screen._gate_state(entry), shown, entry["title"])
        self.assertEqual(out["page_attention"], {"state": "warning", "n": 1, "what": "pieces fail a gate"})
        self.assertEqual([writing_screen._gate_state(entry) for entry in LAMPS], ["warning", "caution", "caution", "", "caution", ""])

    def test_the_editor_shows_the_draft_read_only_and_each_files_facts(self):
        out = self.run_page("open('11');", search="?view=editor")
        editor = out["html"]["view-editor"]
        self.assertIn("<p>First paragraph.</p><p>Second paragraph.</p>", editor)
        self.assertIn('aria-readonly="true"', editor)
        self.assertNotIn("contenteditable", editor)
        self.assertIn("writing-pack/content/2026/three-budgets/index.md", editor)
        self.assertIn("Read only", editor)

    def test_show_draft_selects_the_piece_and_opens_its_whole_draft_in_the_editor(self):
        """pack sd:7242: Show draft is a row and palette command; it selects the piece, opens the editor and says whether
        the editor shows the whole draft. Details offer it through the command bar, not a button of their own."""
        out = self.run_page("R.icon = cmd('piece.draft').icon; R.off = cmd('piece.draft').when(C.get('12'));"
                            " shellRun(cmd('piece.draft'), C.get('12')); await flush();")
        editor = out["html"]["view-editor"]
        self.assertEqual((out["R"]["icon"], out["R"]["off"]), ("file-text", True))
        self.assertIn("#12 Gated Review", editor)
        self.assertIn("<p>First paragraph.</p><p>Second paragraph.</p>", editor)
        self.assertIn("The whole draft, 1,200 words. Edit it in its file.", editor)
        self.assertEqual(out["urls"][-1], {"view": "editor"})
        self.assertFalse(any("Show draft" in t[0] for t in out["toasts"]), "Show draft toasted; the editor is its answer")
        self.assertNotIn('id="open-ed"', out["html"]["details"])
        cut = "DOC.pieces[1].draft_cut = true;\n"
        out = self.run_page("shellRun(cmd('piece.draft'), C.get('12')); await flush();", prelude=cut)
        self.assertIn("The first 2 paragraphs of 1,200 words; the rest stays in the file.", out["html"]["view-editor"])
        out = self.run_page("R.off = cmd('piece.draft').when(C.get('11'));", prelude="DOC.pieces[0].draft = []; DOC.pieces[0].words = 0;\n")
        self.assertEqual(out["R"]["off"], "no draft yet")

    def test_each_gate_row_has_a_refresh_and_a_view_icon(self):
        """pack sd:7242: Draft reads again and opens Show draft; Research and Fact-check copy the skill line, since no
        route runs them; Adversarial runs Refresh review. Each report's view opens it in the editor's evidence pane,
        read through /api/writing/<id>/<gate>; a missing file's view is off and says missing."""
        report = "[200, { item: 11, name: 'adversarial.md', text: 'Adversarial report body', cut: false }]"
        answer = ("(path) => path === '/api/writing' ? [200, DOC] : path === '/api/writing/11/adversarial' ? " + report
                  + " : [200, {}]")
        out = self.run_page("open('11'); await flush(); R.details = ELS.details.html;"
                            " const view = { target: { closest: s => s === '[data-gate-view]' ? { dataset: { gateView: 'adversarial' } } : null } };"
                            " ELS.details.listeners.click.forEach(f => f(view)); await flush(); await flush(); R.editor = ELS['view-editor'].html;",
                            answer=answer)
        gates = re.search(r'<ul class="gates">.*?</ul>', out["R"]["details"], re.S).group(0)
        rows = re.findall(r"<li>.*?</li>", gates, re.S)
        self.assertEqual(len(rows), 4)
        draft, research, fact, adversarial = rows
        self.assertIn('data-gate-again=""', draft)
        self.assertIn('data-cmd="piece.draft" data-obj="11"', draft)
        self.assertIn('data-copy="claude &quot;/sd-research piece=2026/three-budgets&quot;"', research)
        self.assertIn('data-gate-view="research"', research)
        self.assertIn('data-copy="claude &quot;/sd-fact-check input=content/2026/three-budgets/index.md&quot;"', fact)
        self.assertIn("disabled", fact)
        self.assertIn("Fact-check: missing", fact)
        self.assertIn('data-cmd="piece.review" data-obj="11"', adversarial)
        self.assertIn('data-gate-view="adversarial"', adversarial)
        self.assertIn("Adversarial report body", out["R"]["editor"])
        self.assertIn('aria-pressed="true">Adversarial</button>', out["R"]["editor"])
        self.assertIn("/api/writing/11/adversarial", out["gets"])

    def test_each_gate_row_links_its_drive_copy_or_says_why_not(self):
        """pack sd:7243: each gate row has a Drive icon. A delivered copy is a link that opens in a new tab; a queued
        one, a missing file, a stage that mirrors nothing and an unread queue are off and say why."""
        url = "https://docs.google.com/document/d/example-doc/edit"
        mirror = ("DOC.pieces[0].mirror = { folder: 'Writing/Drafts', unread: '', copies: {"
                  " 'index.md': [{ channel: 'drive', state: 'published', url: '%s' }],"
                  " 'research.md': [{ channel: 'drive', state: 'queued', url: null }],"
                  " 'fact-check.md': [], 'adversarial.md': [] } };\n" % url)
        def rows(out):
            return re.findall(r"<li>.*?</li>", re.search(r'<ul class="gates">.*?</ul>', out["R"]["details"], re.S).group(0), re.S)

        draft, research, fact, adversarial = rows(self.run_page("open('11'); await flush(); R.details = ELS.details.html;",
                                                                prelude=mirror))
        self.assertIn(f'href="{url}" target="_blank" rel="noopener" data-drive="index.md" aria-label="Draft: open the Drive copy"', draft)
        self.assertIn('data-drive="research.md" aria-label="Research: Drive copy queued, not yet drained"', research)
        self.assertIn('aria-label="Fact-check: Drive copy missing"', fact)
        self.assertIn('aria-label="Adversarial: Drive copy not drained yet; it goes to Writing/Drafts"', adversarial)
        early = mirror.replace("folder: 'Writing/Drafts'", "folder: null")
        self.assertIn("a piece mirrors from Review on", rows(self.run_page("open('11'); await flush(); R.details = ELS.details.html;",
                                                                          prelude=early))[3])
        unread = "DOC.pieces[0].mirror = { folder: null, unread: 'the queue was not read', copies: {} };\n"
        self.assertIn("Drive copy unknown: the queue was not read", rows(self.run_page(
            "open('11'); await flush(); R.details = ELS.details.html;", prelude=unread))[0])

    def test_a_reread_drops_the_cached_reports_so_two_versions_in_one_second_differ(self):
        """pack sd:7328: a report is cached per piece, file and whole-second change time. Two versions written in one
        second share a key, so Draft's refresh kept the old text beside the new gate state. Every read of the pieces
        drops the cache, so the open report reads again."""
        answer = ("(path) => path === '/api/writing' ? [200, DOC] : path === '/api/writing/11/adversarial'"
                  " ? [200, { item: 11, name: 'adversarial.md', text: BODY, cut: false }] : [200, {}]")
        out = self.run_page("open('11'); await flush();"
                            " const view = { target: { closest: s => s === '[data-gate-view]' ? { dataset: { gateView: 'adversarial' } } : null } };"
                            " ELS.details.listeners.click.forEach(f => f(view)); await flush(); await flush(); R.first = ELS['view-editor'].html;"
                            " BODY = 'Report version two';"
                            " const again = { target: { closest: s => s === '[data-gate-again]' ? {} : null } };"
                            " ELS.details.listeners.click.forEach(f => f(again)); await flush(); await flush(); R.second = ELS['view-editor'].html;",
                            answer=answer, prelude="var BODY = 'Report version one';\n")
        self.assertIn("Report version one", out["R"]["first"])
        self.assertIn("Report version two", out["R"]["second"])

    def test_the_details_link_the_claims_url(self):
        # sd:3465: "Already published" named the claim only; the Details now link where it went, http and https only.
        claim = {"claim": "c1aim0000000", "url": "https://example.test/p", "phase": "published", "href": "https://example.test/p"}
        bare = dict(claim, url="javascript:alert(1)", href=None)
        answer = ("(path) => path === '/api/writing' ? [200, Object.assign({}, DOC, { pieces: DOC.pieces.map(p => p.id === 12 ? Object.assign({}, p, { publication: "
                  + json.dumps(claim) + " }) : p.id === 14 ? Object.assign({}, p, { publication: " + json.dumps(bare) + " }) : p) })] : [200, {}]")
        out = self.run_page("const open = k => document.dispatchEvent(new CustomEvent('shell:open', { detail: k }));"
                            " open('12'); await flush(); R.linked = ELS.details.html; open('14'); await flush(); R.bare = ELS.details.html;", answer=answer)
        self.assertIn('<dt>Published</dt><dd><a class="ext" href="https://example.test/p" target="_blank" rel="noopener noreferrer">https://example.test/p</a></dd>',
                      out["R"]["linked"])
        self.assertIn("<dt>Published</dt><dd><code>javascript:alert(1)</code></dd>", out["R"]["bare"])
        self.assertNotIn('href="javascript', out["R"]["bare"])

    def test_a_failed_read_clears_the_page_and_says_so(self):
        out = self.run_page("", answer="() => [500, { error: 'database is locked' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("The pieces were not read: database is locked", out["states"][-1]["text"])
        self.assertEqual(out["page_attention"], {"state": "unknown", "n": 0, "what": "pieces not read"})
        self.assertIn("The pieces were not read. Reload retries it.", out["html"]["details"])

    def test_the_script_adds_no_sink_no_sample_data_and_no_reader_of_its_own(self):
        self.assertNotIn("innerHTML", PAGE_JS)
        self.assertNotIn("WRITING_DATA", PAGE_JS)
        self.assertNotIn("mockup", PAGE_JS)
        self.assertNotIn("contenteditable", PAGE_JS.replace('[contenteditable="true"]', ""))
        self.assertIn("window.shell.read({", PAGE_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "writing", "Writing", "/writing", ("/api/writing", "/api/writing/1/research",
                                                                  "/api/triage/decline")


if __name__ == "__main__":
    unittest.main()
