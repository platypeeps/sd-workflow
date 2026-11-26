"""The v2 Documents page (sd:2114).

What this slice promises: `/documents` answers under the shared policy and
loads its script before `shell.js`, and the old listing is retired
(sd:3402); `/api/documents` lists every root and every file the
server serves (`documents.roots`, `documents.documents`), with each file's
title, h1 and stand line, its derived kind and its render state, and names a
key two checkouts claim. The page registers the design's Documents commands
with the design's ids, labels, keys and risks. Pin, hide and tag are the
workflow database's view state (sd:3428): each row carries it, each command
posts `/api/documents/view` with Undo, the Hidden filter shows the hidden
files, and a store that was not read turns the six off with its reason.
Disable render is off because no switch exists, and Render and Request are
copy only.

`documents.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` uses. The browser half is a manual check: the pull
request records what was looked at (1440 px) and what was not verified (375 px
and 320 px, both schemes, a coarse pointer, focus visibility).
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import (
    documents,
    documents_screen,
    learning_screen,
    research_screen,
    server,
    source_intake,
    v2,
)
from sd_db import connect, create_item, records, writes
from support import NOW, ScreenCase
from test_run_actions import Run
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_tasks import SHELL, STAND_IN, VIEW_SHELL
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
DOCUMENTS_JS = (V2 / "static" / "documents.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
LEARNING_JS = (V2 / "static" / "learning.js").read_text(encoding="utf-8")

#: The design's Documents commands (design source products/system/designs/pages/documents.js): id, object type, label, key, risk.
COMMANDS = [
    ["document.render", "document", "Render", "r", "safe"],
    ["document.open", "document", "Open document", "o", "safe"],
    ["document.pin", "document", "Pin", "p", "undo"],
    ["document.unpin", "document", "Unpin", "p", "undo"],
    ["document.hide", "document", "Hide", "h", "undo"],
    ["document.unhide", "document", "Unhide", "h", "undo"],
    ["document.tag", "document", "Tag", "t", "undo"],
    ["document.untag", "document tag", "Untag", "u", "undo"],
    ["document.skip", "document", "Disable render", "s", "safe"],
    ["document.skip-repo", "document", "Disable render for repo", "e", "safe"],
    # build (sd:3525, review 2026-10-12 A2 F5): the design's "Run", r, undo read as a run with an Undo, and r is Render on a
    # document. It only copies the line, so it is Request, safe, and has no key until it files the request.
    ["document.request", "document request draft", "Request", None, "undo"],
    # sd:3986: a root's name and Hide are its own view row.
    ["root.rename", "document root", "Rename root", None, "undo"],
    ["root.hide", "document root", "Hide root", None, "undo"],
    ["root.unhide", "document root", "Unhide root", None, "undo"],
    # sd:5102: Publish to a destination and Publish now, declared in commands.js.
    ["document.publish", "document destination", "Publish to", None, "safe"],
    # sd:5489: a ticked destination unticks: ask first, and Undo ticks it again.
    ["document.unpublish", "document destination", "Remove copy", None, "undo"],
    ["document.queue-copy", "document", "Queue copy", None, "safe"],
    ["document.publish-now", "document", "Publish now", None, "safe"],
]

#: Seconds since the epoch, a day apart: the fixture's files and sources are dated with them.
DAY = 86400
T0 = 1790000000


def write(path: Path, text: str, when: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (when, when))
    return path


def page(title="", h1="", stand="", meta=""):
    head = f"<title>{title}</title>" if title else ""
    head += f'<meta name="description" content="{meta}">' if meta else ""
    body = f"<h1>{h1} <span>part</span></h1>" if h1 else ""
    body += f'<p class="standfirst">{stand}</p>' if stand else ""
    # A chart's own <title> never names the page.
    return f"<!doctype html><html><head>{head}</head><body><svg><title>Chart title</title></svg>{body}</body></html>"


def fixture(base: Path) -> None:
    """Two research documents (one render-stale by research.conf.py, one fresh by name), one without a source, a report,
    an empty root and a contested key."""
    lab = base / "lab"
    write(lab / "research.conf.py", 'DOCS = [{"src": "notes/PLAN.md", "out": "plan"}]\n', T0)
    write(lab / "notes" / "PLAN.md", "# plan\n", T0 + 3 * DAY)
    write(lab / "docs" / "dashboard" / "plan.html", page(title="The plan", h1="Plan", stand="What we build next."), T0 + 2 * DAY)
    write(lab / "00-overview" / "Overview.md", "# overview\n", T0)
    write(lab / "docs" / "dashboard" / "overview.html", page(title="Overview"), T0 + DAY)
    write(lab / "docs" / "dashboard" / "orphan.html", page(), T0 + 4 * DAY)
    write(base / "group" / "civic" / "docs" / "dashboard" / "brief.html",
          page(title="Brief", h1="Civic brief", stand="Not this one.", meta="The meta line wins."), T0 + 5 * DAY)
    (base / "quiet" / "docs" / "dashboard").mkdir(parents=True)
    write(base / "a" / "twin" / "docs" / "dashboard" / "x.html", page(title="A"), T0)
    write(base / "b" / "twin" / "docs" / "dashboard" / "x.html", page(title="B"), T0)


class TheDocument(ScreenCase):
    """`documents_screen.document` lists what the server serves, with what each file says and whether it is render-stale."""

    def setUp(self):
        super().setUp()
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(self.base)
        self.conf = write(self.base / "documents.conf", "label|lab|Lab\n", T0)
        self.doc = documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)

    def rows(self):
        return {f"{r['key']}/{r['file']}": r for r in self.doc["documents"]}

    def test_every_served_file_is_a_row_newest_first(self):
        self.assertEqual(list(self.rows()), ["civic/brief.html", "lab/orphan.html", "lab/plan.html", "lab/overview.html"])
        for key, row in self.rows().items():
            self.assertEqual(row["href"], f"/documents/{key}")
            self.assertIsNotNone(documents.resolve(row["key"], row["file"], self.conf, self.base), key)
        self.assertEqual(self.rows()["lab/plan.html"]["modified"], "2026-09-23T14:13:20Z")

    def test_the_roots_carry_label_count_and_whether_they_are_research(self):
        self.assertEqual([(r["key"], r["label"], r["n"], r["research"]) for r in self.doc["roots"]],
                         [("civic", "Civic", 1, False), ("lab", "Lab", 3, True), ("quiet", "Quiet", 0, False)])
        self.assertEqual(self.doc["contested"][0]["key"], "twin")
        self.assertEqual(len(self.doc["contested"][0]["paths"]), 2)

    def test_each_root_names_the_checkout_a_request_files_under(self):
        # sd:5958: Request sent no repo, so its task belonged to no repository; the root names its checkout.
        self.assertEqual({r["key"]: r["checkout"] for r in self.doc["roots"]},
                         {"civic": documents_screen._tilde(self.base / "group" / "civic"),
                          "lab": documents_screen._tilde(self.base / "lab"), "quiet": documents_screen._tilde(self.base / "quiet")})

    def test_a_research_root_names_the_checkout_key_researchs_render_takes(self):
        # sd:5884: Render posts /api/research/render {"repo": <key>}, which research_screen.checkout resolves under the
        # repo root; a root that is no research checkout under it names none, so the page says why Render is off.
        self.assertEqual([(r["key"], r["render"]) for r in self.doc["roots"]], [("civic", ""), ("lab", "lab"), ("quiet", "")])
        self.assertEqual(research_screen.checkout(self.base, "lab"), self.base / "lab")
        elsewhere = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write(elsewhere / "far" / "research.conf.py", "DOCS = []\n", T0)
        write(elsewhere / "far" / "docs" / "dashboard" / "x.html", page(title="Far"), T0)
        conf = write(self.base / "far.conf", f"root|far|Far|{elsewhere / 'far' / 'docs' / 'dashboard'}\n", T0)
        far = {r["key"]: r for r in documents_screen.document(now=NOW, config_path=conf, repo_root=self.base)["roots"]}["far"]
        self.assertEqual((far["research"], far["render"]), (True, ""))

    def test_kind_and_render_state_come_from_the_checkout(self):
        rows = self.rows()
        self.assertEqual({k: r["kind"] for k, r in rows.items()},
                         {"civic/brief.html": "report", "lab/orphan.html": "research", "lab/plan.html": "research", "lab/overview.html": "research"})
        # research.conf.py names plan's source, which changed after the page; overview's source is found by name and is older.
        self.assertEqual((rows["lab/plan.html"]["src"], rows["lab/plan.html"]["stale"]), ("notes/PLAN.md", True))
        self.assertEqual((rows["lab/overview.html"]["src"], rows["lab/overview.html"]["stale"]), ("00-overview/Overview.md", False))
        self.assertEqual((rows["lab/orphan.html"]["src"], rows["lab/orphan.html"]["stale"]), ("", False))
        self.assertEqual((rows["civic/brief.html"]["src"], rows["civic/brief.html"]["stale"]), ("", False))

    def test_two_sources_with_the_page_name_match_neither(self):
        write(self.base / "lab" / "elsewhere" / "overview.md", "# again\n", T0)
        rows = {r["file"]: r for r in documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)["documents"]}
        self.assertEqual(rows["overview.html"]["src"], "")

    def test_a_unique_source_deeper_than_three_folders_is_found(self):
        # The contract is one .md with the page's name anywhere in the checkout (sd:2417); skipped folders still hold none.
        write(self.base / "lab" / "docs" / "dashboard" / "deep.html", page(title="Deep"), T0 + DAY)
        write(self.base / "lab" / "a" / "b" / "c" / "d" / "Deep.md", "# deep\n", T0)
        write(self.base / "lab" / "a" / "b" / "c" / "d" / "storage" / "deep.md", "# skipped\n", T0)
        write(self.base / "lab" / "a" / "b" / "c" / "d" / ".cache" / "deep.md", "# skipped\n", T0)
        rows = {r["file"]: r for r in documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)["documents"]}
        self.assertEqual((rows["deep.html"]["src"], rows["deep.html"]["stale"]), ("a/b/c/d/Deep.md", False))

    def test_an_unchanged_checkout_is_not_walked_again_and_a_new_source_is_found(self):
        """sd:3530 (review 2026-10-12, A1 F5): every read walked each research checkout whole."""
        listed = []
        real = os.scandir

        def counted(path="."):
            listed.append(path)
            return real(path)

        with mock.patch.object(os, "scandir", counted):
            again = documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)
        self.assertEqual(again["documents"], self.doc["documents"])
        # The roots' own listing reads the checkout's top folder; only the walk reads the folders below it.
        lab = self.base / "lab"
        self.assertEqual([path for path in listed if Path(path).is_relative_to(lab) and Path(path) != lab
                          and not Path(path).is_relative_to(lab / "docs")], [], "an unchanged checkout was walked again")
        # A new folder deep in the tree changes its parent's mtime, so the next read walks and finds the source.
        write(self.base / "lab" / "docs" / "dashboard" / "deep.html", page(title="Deep"), T0 + DAY)
        write(self.base / "lab" / "a" / "b" / "Deep.md", "# deep\n", T0)
        rows = {r["file"]: r for r in documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)["documents"]}
        self.assertEqual(rows["deep.html"]["src"], "a/b/Deep.md")
        # A source renamed in place changes only its own folder's mtime.
        (self.base / "lab" / "a" / "b" / "Deep.md").rename(self.base / "lab" / "a" / "b" / "Other.md")
        rows = {r["file"]: r for r in documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)["documents"]}
        self.assertEqual(rows["deep.html"]["src"], "")

    def test_a_configured_source_that_is_not_a_file_is_not_a_source(self):
        # A missing src leaves the name match; a src that is a folder leaves the render state unknown, never fresh.
        write(self.base / "lab" / "research.conf.py",
              'DOCS = [{"src": "notes/GONE.md", "out": "overview"}, {"src": "notes", "out": "plan"}]\n', T0)
        (self.base / "lab" / "notes" / "PLAN.md").unlink()
        rows = {r["file"]: r for r in documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)["documents"]}
        self.assertEqual((rows["overview.html"]["src"], rows["overview.html"]["stale"]), ("00-overview/Overview.md", False))
        self.assertEqual((rows["plan.html"]["src"], rows["plan.html"]["stale"]), ("", False))

    def test_a_computed_config_is_read_through_its_render_manifest(self):
        """sd:5666: a config the safe reader refuses was read as no entries; Research reads its render's manifest (sd:5659)."""
        lab = self.base / "lab"
        conf = write(lab / "research.conf.py", 'DOCS = [{"src": s, "out": "plan"} for s in ["notes/DRAFT.md"]]\n', T0)
        write(lab / "notes" / "DRAFT.md", "# draft\n", T0 + 3 * DAY)
        manifest = {"version": 1, "project": "Lab", "conf_sha256": hashlib.sha256(conf.read_bytes()).hexdigest(),
                    "docs": [{"src": "notes/DRAFT.md", "out": "plan", "links": []}]}
        write(lab / "docs" / "dashboard" / "research.json", json.dumps(manifest), T0)

        def read():
            doc = documents_screen.document(now=NOW, config_path=self.conf, repo_root=self.base)
            root = next(r for r in doc["roots"] if r["key"] == "lab")
            plan = next(r for r in doc["documents"] if r["file"] == "plan.html")
            return root.get("conf"), root.get("manifest"), plan["src"], plan["stale"]

        self.assertEqual(read(), ("", "current", "notes/DRAFT.md", True))
        # A config changed since the render still reads the manifest, and the root says it is from an older render.
        write(lab / "research.conf.py", conf.read_text(encoding="utf-8") + "# edited\n", T0)
        self.assertEqual(read(), ("", "older", "notes/DRAFT.md", True))
        # With no manifest the root says to render once; the name match still answers.
        (lab / "docs" / "dashboard" / "research.json").unlink()
        refused, state, src, stale = read()
        self.assertIn("run sd-research-kit render", refused)
        self.assertEqual((state, src, stale), ("", "notes/PLAN.md", True))
        # A literal config reads as before: no refusal and no manifest.
        self.assertEqual([(r["conf"], r["manifest"]) for r in self.doc["roots"] if r["key"] == "lab"], [("", "")])

    def test_each_row_says_its_title_h1_and_stand_line(self):
        rows = self.rows()
        self.assertEqual([rows["lab/plan.html"][k] for k in ("title", "h1", "desc")], ["The plan", "Plan part", "What we build next."])
        self.assertEqual([rows["civic/brief.html"][k] for k in ("title", "h1", "desc")], ["Brief", "Civic brief part", "The meta line wins."])
        self.assertEqual([rows["lab/orphan.html"][k] for k in ("title", "h1", "desc")], ["", "", ""])

    def test_an_empty_meta_description_leaves_the_stand_line(self):
        # The meta wins only when it says something; an empty one falls back to the stand-class element.
        path = write(self.base / "page.html", '<html><head><meta name="description" content="  "></head>'
                     '<body><p class="lede">The lede line.</p></body></html>', T0)
        self.assertEqual(documents_screen.facts(path)["desc"], "The lede line.")

    def test_with_no_connection_the_store_is_not_claimed(self):
        self.assertEqual(self.doc["store"]["available"], False)
        self.assertIn("not read", self.doc["store"]["reason"])
        self.assertEqual((self.doc["read"], self.doc["config"]), (NOW, "<config>/project-dashboard/documents.conf"))


class TheViewState(ScreenCase):
    """Pin, hide and tag (sd:3428): the pack's `view` rows, keyed `<root key>/<file>`, on each row the page lists."""

    def setUp(self):
        super().setUp()
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(self.base)
        self.conf = self.base / "none.conf"

    def read(self, connection):
        doc = documents_screen.document(connection, now=NOW, config_path=self.conf, repo_root=self.base)
        return doc, {f"{r['key']}/{r['file']}": r for r in doc["documents"]}

    def test_each_row_carries_its_pin_hide_and_tags(self):
        writes.set_view(self.connection, "lab/plan.html", pinned=True, tags=["q4", "board"])
        writes.set_view(self.connection, "civic/brief.html", hidden=True)
        doc, rows = self.read(self.connection)
        self.assertEqual(doc["store"], {"available": True, "reason": ""})
        state = {key: (row["pinned"], row["hidden"], row["tags"]) for key, row in rows.items()}
        self.assertEqual(state, {"civic/brief.html": (False, True, []), "lab/orphan.html": (False, False, []),
                                 "lab/plan.html": (True, False, ["q4", "board"]), "lab/overview.html": (False, False, [])})

    def test_a_view_for_a_file_no_longer_served_lists_nothing(self):
        writes.set_view(self.connection, "lab/gone.html", pinned=True)
        doc, rows = self.read(self.connection)
        self.assertNotIn("lab/gone.html", rows)
        self.assertTrue(doc["store"]["available"])

    def test_an_unread_store_hides_nothing_and_says_why(self):
        # A failed read is not a negative answer: the page must know the hidden file shows only because nothing was read.
        writes.set_view(self.connection, "civic/brief.html", hidden=True)
        closed = connect(self.path)
        closed.close()
        doc, rows = self.read(closed)
        self.assertEqual(doc["store"]["available"], False)
        self.assertIn("not read", doc["store"]["reason"])
        self.assertEqual(rows["civic/brief.html"]["hidden"], False)

    def test_a_pack_without_the_view_store_says_to_pull_it(self):
        # A command pack from before sd:3430 has neither reader nor writer; the page still lists every file.
        with mock.patch.object(documents_screen, "writes", types.SimpleNamespace()):
            doc, rows = self.read(self.connection)
            self.assertEqual(len(rows), 4)
            self.assertIn("pull it", doc["store"]["reason"])
            with self.assertRaisesRegex(ValueError, "pull it"):
                documents_screen.request({"root": "lab", "file": "plan.html", "pinned": True},
                                         config_path=self.conf, repo_root=self.base)

    def test_a_write_names_a_served_file_and_a_field(self):
        def ask(payload):
            return documents_screen.request(payload, config_path=self.conf, repo_root=self.base)
        for payload in ({}, {"root": "lab", "file": "plan.html"}, {"root": "lab", "file": "plan.html", "pinned": True, "seen": 1},
                        {"root": "lab", "pinned": True}, {"root": 1, "file": "plan.html", "pinned": True},
                        {"root": "lab", "file": "gone.html", "pinned": True}, {"root": "lab", "file": "../x.html", "hidden": True},
                        {"root": "twin", "file": "x.html", "pinned": True}):
            with self.assertRaises(ValueError, msg=payload):
                ask(payload)
        write = ask({"root": "lab", "file": "plan.html", "pinned": True, "tags": ["q4"]})
        self.assertEqual(write(self.connection), {"key": "lab/plan.html", "pinned": True, "hidden": False, "tags": ["q4"]})
        # A field left out keeps its value: Undo of a pin posts pinned alone.
        self.assertEqual(ask({"root": "lab", "file": "plan.html", "pinned": False})(self.connection),
                         {"key": "lab/plan.html", "pinned": False, "hidden": False, "tags": ["q4"]})
        self.assertEqual(writes.views(self.connection), {"lab/plan.html": {"pinned": False, "hidden": False, "tags": ["q4"]}})


class TheRootViews(ScreenCase):
    """sd:3986: a root key's view row names and hides the root; documents.conf's `label|` lines are named, not read."""

    def setUp(self):
        super().setUp()
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(self.base)
        self.conf = write(self.base / "documents.conf", "label|lab|Old Lab\n", T0)

    def read(self, connection):
        doc = documents_screen.document(connection, now=NOW, config_path=self.conf, repo_root=self.base)
        return {r["key"]: r for r in doc["roots"]}, [f"{r['key']}/{r['file']}" for r in doc["documents"]], doc

    def test_a_root_row_names_and_hides_its_root_and_the_file_label_is_named_not_read(self):
        roots, rows, _ = self.read(self.connection)
        self.assertEqual([(r["label"], r["derived"], r["set"], r["hidden"], r["file_label"]) for r in roots.values()],
                         [("Civic", "Civic", None, False, None), ("Lab", "Lab", None, False, "Old Lab"),
                          ("Quiet", "Quiet", None, False, None)])
        writes.set_view(self.connection, "lab", label="Research Lab")
        writes.set_view(self.connection, "civic", hidden=True)
        roots, rows, _ = self.read(self.connection)
        self.assertEqual((roots["lab"]["label"], roots["lab"]["set"], roots["lab"]["derived"]), ("Research Lab", "Research Lab", "Lab"))
        # A hidden root stays listed for its fold, with its count; its files leave the rows, and the server still serves them.
        self.assertEqual((roots["civic"]["hidden"], roots["civic"]["n"]), (True, 1))
        self.assertEqual(rows, ["lab/orphan.html", "lab/plan.html", "lab/overview.html"])
        self.assertIsNotNone(documents.resolve("civic", "brief.html", self.conf, self.base))

    def test_an_unread_store_shows_derived_names_hides_no_root_and_brings_no_file_label_back(self):
        writes.set_view(self.connection, "lab", label="Research Lab")
        writes.set_view(self.connection, "civic", hidden=True)
        closed = connect(self.path)
        closed.close()
        roots, rows, doc = self.read(closed)
        self.assertFalse(doc["store"]["available"])
        self.assertEqual((roots["lab"]["label"], roots["civic"]["hidden"]), ("Lab", False))
        self.assertIn("civic/brief.html", rows)

    def test_a_root_write_names_a_listed_root_and_a_label_or_hidden(self):
        def ask(payload):
            return documents_screen.request(payload, config_path=self.conf, repo_root=self.base)
        for payload in ({"root": "lab"}, {"root": "lab", "pinned": True}, {"root": "lab", "tags": []},
                        {"root": "gone", "hidden": True}, {"root": "twin", "label": "Twin"},
                        {"root": "lab", "file": "plan.html", "label": "x"}, {"root": "lab", "label": 5}):
            with self.assertRaises(ValueError, msg=payload):
                ask(payload)
        self.assertEqual(ask({"root": "lab", "label": "Research Lab"})(self.connection),
                         {"key": "lab", "pinned": False, "hidden": False, "tags": [], "label": "Research Lab"})
        # Hide keeps the label; an empty label goes back to the derived name.
        self.assertEqual(ask({"root": "lab", "hidden": True})(self.connection)["label"], "Research Lab")
        ask({"root": "lab", "label": ""})(self.connection)
        roots, _, _ = self.read(self.connection)
        self.assertEqual((roots["lab"]["label"], roots["lab"]["set"], roots["lab"]["hidden"]), ("Lab", None, True))

    def test_a_rerun_of_the_label_import_writes_the_same_answer(self):
        """The failure table's import loop: stopped at row k, it is run again from the top."""
        labels = {"lab": "Research Lab", "civic": "Civic Briefs"}
        for _ in range(2):
            for key, label in labels.items():
                writes.set_view(self.connection, key, label=label)
            if _ == 0:
                first = writes.views(self.connection)
        self.assertEqual(writes.views(self.connection), first)
        self.assertEqual({key: first[key]["label"] for key in labels}, labels)


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/documents")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Documents · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "commands.js", "documents.js", "learning.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_a_view_write_lands_and_the_next_read_carries_it(self):
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(base)
        cookie = {"Cookie": self.cookie}
        with mock.patch.object(documents, "REPO_ROOT", base), mock.patch.object(documents, "CONFIG", base / "none.conf"):
            status, _, out = self.post("/api/documents/view", {"root": "lab", "file": "plan.html", "hidden": True})
            self.assertEqual((status, out), (200, {"key": "lab/plan.html", "pinned": False, "hidden": True, "tags": []}))
            # The pack judges the values: a tag past its limit is the library's 400, and nothing is written.
            status, _, out = self.post("/api/documents/view", {"root": "lab", "file": "plan.html", "tags": ["x" * 65]})
            self.assertEqual(status, 400, out)
            self.assertEqual(self.post("/api/documents/view", {"root": "lab", "file": "gone.html", "pinned": True})[0], 400)
            # The session and CSRF rules every write keeps.
            self.assertEqual(self.post("/api/documents/view", {"root": "lab", "file": "plan.html", "pinned": True},
                                       **{"X-SD-CSRF": "0" * 64})[0], 403)
            doc = json.loads(self.request("/api/documents", headers=cookie)[2])
        rows = {f"{r['key']}/{r['file']}": r for r in doc["documents"]}
        self.assertEqual((rows["lab/plan.html"]["hidden"], rows["lab/plan.html"]["tags"]), (True, []))
        self.assertEqual(doc["store"], {"available": True, "reason": ""})

    def test_the_rail_opens_the_new_page_and_the_old_listing_is_retired(self):
        self.assertEqual(v2.SECTIONS.get("Documents"), "/documents")
        self.assertNotIn("Documents", v2.CLASSIC)
        # sd:3402 retired the classic listing; `test_classic_retired` holds the 404.
        self.assertNotIn("Documents (classic)", v2.SCREENS)
        self.assertEqual(self.request("/classic/documents")[0], 404)

    def test_the_data_route_needs_a_session_takes_no_query_and_reads_the_roots(self):
        self.assertEqual(self.request("/api/documents")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/documents?repo=lab", headers=cookie)[0], 400)
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(base)
        with mock.patch.object(documents, "REPO_ROOT", base), mock.patch.object(documents, "CONFIG", base / "none.conf"):
            status, _, body = self.request("/api/documents", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual([r["key"] for r in doc["roots"]], ["civic", "lab", "quiet"])
        self.assertEqual(len(doc["documents"]), 4)


# The stand-in additions documents.js needs beyond test_v2_tasks: the shell's row, url, setContext and the window's open.
SHELL_MORE = r"""
window.shell.row = () => ROW; window.shell.setContext = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
OUT.opened = []; window.open = (u, t, f) => { OUT.opened.push([u, t, f]); };
"""


class TheScript(ScreenCase):
    """documents.js against the document `documents_screen` builds from the fixture."""

    def setUp(self):
        super().setUp()
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fixture(base)
        self.doc = documents_screen.document(now=NOW, config_path=base / "none.conf", repo_root=base)
        # The page shows the paths the document gives; a fixed one keeps the expected lines readable.
        for root in self.doc["roots"]:
            root["path"] = f"~/repos/{root['key']}/docs/dashboard"
            root["checkout"] = f"~/repos/{root['key']}"
        # The view store answered (sd:3428); the case that it did not sets its own.
        self.doc["store"] = {"available": True, "reason": ""}

    def rows_of(self, **views):
        """The document with each named row's view state set, as the server lists it after a write."""
        for row in self.doc["documents"]:
            row.update(views.get(f"{row['key']}/{row['file']}", {}))

    def run_page(self, body, answer=None, search="", row="null", shell_more=""):
        answer = answer or f"() => [200, {json.dumps(self.doc)}]"
        script = (STAND_IN + f"location.search = {json.dumps(search)}; location.origin = 'http://dash.example.test';\nvar ROW = {row};\n"
                  + MARKUP_JS + "\nconst mk = window.markup.html;\n"
                  + SHELL + SHELL_MORE + shell_more + "\n" + READ_SHELL + f"\nANSWER = {answer};\n" + COMMANDS_JS + DOCUMENTS_JS
                  + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.page_attention = window.PAGE_ATTENTION;"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    # The list grammar from shell.list (sd:2682, as Reports in sd:2527-2529). Each click goes to the listener the page put on its holder.
    CLICK = "ELS['%s'].listeners.click[0]({ target: { closest: s => s === %s ? { dataset: %s } : null } });"
    URL = "String(OUT.urls[OUT.urls.length - 1])"

    def many(self, n):
        brief = self.doc["documents"][0]
        self.doc["documents"] += [{**brief, "file": f"n{i:02}.html", "href": f"/documents/civic/n{i:02}.html",
                                   "modified": "2026-01-01T00:00:00Z"} for i in range(n)]

    def test_every_column_with_an_order_sorts_through_the_shared_header(self):
        """sd:2682: the header is shell.list's; aria-sort is on the sorted th alone, and the sort is in the URL."""
        click = self.CLICK % ("rows-head", "'button[data-sort]'", "{ sort: 'title' }")
        out = self.run_page(f"R.head = ELS['rows-head'].html; {click} R.head2 = ELS['rows-head'].html; R.url = {self.URL};"
                            f" {click} R.url2 = {self.URL};")
        sorted_th = r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"'
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["head"]), ["title", "repo", "mod", "size"])
        self.assertEqual(re.findall(sorted_th, out["R"]["head"]), [("descending", "mod")])
        self.assertEqual(re.findall(sorted_th, out["R"]["head2"]), [("ascending", "title")])
        self.assertEqual((out["R"]["url"], out["R"]["url2"]), ("sort=title&dir=asc", "sort=title&dir=desc"))
        # An older link's ?sort=col.dir still opens its order.
        out = self.run_page("R.head = ELS['rows-head'].html;", search="?sort=size.asc")
        self.assertEqual(re.findall(sorted_th, out["R"]["head"]), [("ascending", "size")])

    def test_the_legend_under_the_table_lists_exactly_the_marks_a_row_draws(self):
        """sd:5731: the help line under the table means each mark a row draws: every render state, the pin and every publish
        fill, read from the maps the rows draw from, so a state cannot reach a row without its meaning."""
        out = self.run_page("R.help = String(ELS['list-help'].html); R.render = Object.values(RENDER);"
                            " R.pub = Object.entries(window.commands.PUB_STATE);")
        keys = re.findall(r'<span class="key">(.*?)</span>(?=<span class="key">|</p>)', out["R"]["help"])
        meanings = [re.search(r"</span> ([^<]+)$", k).group(1) for k in keys]
        self.assertEqual(meanings, [w for _, _, w in out["R"]["render"]] + ["pinned"] + [w for _, w in out["R"]["pub"]])
        for cls, glyph, word in out["R"]["render"]:
            self.assertIn(f'<span class="g-{cls}" aria-hidden="true">{glyph}</span> {word}', keys)
        self.assertEqual([k for k, _ in out["R"]["pub"]], ["has", "queued", "missing", "unread", "off"])
        for state, word in out["R"]["pub"]:
            self.assertRegex(out["R"]["help"], rf'<span class="pub-notion {state}"[^>]*>.*?</span></span> {word}</span>')
        # The rows draw from the same maps: no glyph or publish-state word is spelled out a second time.
        row = re.search(r"^function row\(d\) \{\n(.*?)^\}", DOCUMENTS_JS, re.S | re.M).group(1)
        self.assertIn("RENDER[", row)
        self.assertNotRegex(row, "[▲●▨]")
        self.assertNotIn("designated, no copy yet`", COMMANDS_JS)

    def test_the_pager_numbers_pages_and_keeps_page_and_size_in_the_url(self):
        self.many(60)
        out = self.run_page("R.pager = ELS.pager.html;" + self.CLICK % ("pager", "'.list-pager [data-page]'", "{ page: '3' }")
                            + f" R.pager2 = ELS.pager.html; R.url2 = {self.URL};"
                            + self.CLICK % ("pager", "'.list-pager [data-size]'", "{ size: '100' }")
                            + f" R.pager3 = ELS.pager.html; R.url3 = {self.URL};", search="?page=2&size=25")
        total = len(self.doc["documents"])
        self.assertIn(f'<span class="range">26–50 of {total}</span>', out["R"]["pager"])
        self.assertEqual(re.findall(r'data-page="(\d+)"', out["R"]["pager"]), ["1", "2", "3"])
        self.assertIn(f'<span class="range">51–{total} of {total}</span>', out["R"]["pager2"])
        self.assertEqual(out["R"]["url2"], "page=3&size=25")
        self.assertIn(f'<span class="range">1–{total} of {total}</span>', out["R"]["pager3"])
        self.assertEqual(out["R"]["url3"], "size=100")

    def test_active_filters_show_as_chips_that_remove_one_or_clear_all(self):
        out = self.run_page("R.chips = ELS.chips.html;" + self.CLICK % ("chips", "'[data-unfilter]'", "{ unfilter: 'kind:research' }")
                            + f" R.chips2 = ELS.chips.html; R.url2 = {self.URL};"
                            + " ELS.chips.listeners.click[0]({ target: { closest: s => s === '[data-unfilter-all]' ? {} : null } });"
                            + f" R.chips3 = ELS.chips.html; R.url3 = {self.URL}; R.input = ELS.shift.value;", search="?kind=research&fresh=stale&q=plan")
        self.assertEqual(re.findall(r'data-unfilter="([\w:]+)"', out["R"]["chips"]), ["kind:research", "fresh:stale", "q"])
        self.assertIn("3 filters · 1 of ", out["R"]["chips"])
        self.assertEqual(re.findall(r'data-unfilter="([\w:]+)"', out["R"]["chips2"]), ["fresh:stale", "q"])
        self.assertEqual(out["R"]["url2"], "fresh=stale&q=plan")
        self.assertEqual((out["R"]["chips3"], out["R"]["url3"], out["R"]["input"]), ("", "", ""))

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]);")
        self.assertEqual(out["R"]["reg"], COMMANDS)

    def test_an_unread_store_turns_view_state_off_with_its_reason_and_posts_nothing(self):
        reason = "the view store was not read: boom"
        self.doc["store"], self.doc["contested"] = {"available": False, "reason": reason}, []
        out = self.run_page("""const d = C.get('lab/plan.html');
C.put({ id: 'tag:x', type: 'document tag', doc: d, tag: 'x', label: 'x' }); document.getElementById('tag-in').value = 'q4';
const ids = ['document.pin', 'document.unpin', 'document.hide', 'document.unhide', 'document.tag', 'document.untag'];
R.when = ids.map(id => cmd(id).when(cmd(id).on === 'document tag' ? C.get('tag:x') : d));
ids.forEach(id => shellRun(cmd(id), cmd(id).on === 'document tag' ? C.get('tag:x') : d)); await flush();
R.det = ELS.details.html; F.hidden = 'aside'; render(); R.empty = ELS.empty.html;""", row="'lab/plan.html'")
        # Unpin and Unhide say first that the row is not pinned or hidden; the unread store is why none is.
        self.assertEqual(out["R"]["when"], [reason, "not pinned", reason, "not hidden", reason, reason])
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["gets"], ["/api/documents"])
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn(f"were not read: {reason}. Every document shows.", out["states"][-1]["text"])
        self.assertIn(f"Tags were not read: {reason}.", out["R"]["det"])
        self.assertNotIn('id="tag-in"', out["R"]["det"])
        self.assertIn(reason, out["R"]["empty"])

    VIEW = "(path, body) => path === '/api/documents/view' ? [200, { key: body.root + '/' + body.file }] : [200, %s]"

    def view_posts(self, out):
        return [body for path, body, _ in out["posts"] if path == "/api/documents/view"]

    def test_pin_and_hide_post_the_field_they_change_and_undo_posts_it_back(self):
        self.rows_of(**{"lab/overview.html": {"pinned": True, "hidden": True}})
        out = self.run_page("""const plan = C.get('lab/plan.html'), over = C.get('lab/overview.html');
for (const [id, o] of [['document.pin', plan], ['document.hide', plan], ['document.unpin', over], ['document.unhide', over]]) {
  shellRun(cmd(id), o); await flush(); lastToast().undo(); await flush(); }
R.when = [cmd('document.pin').when(over), cmd('document.hide').when(over)];""", answer=self.VIEW % json.dumps(self.doc))
        def at(file, **change):
            return {"root": "lab", "file": file, **change}
        self.assertEqual(self.view_posts(out), [
            at("plan.html", pinned=True), at("plan.html", pinned=False), at("plan.html", hidden=True), at("plan.html", hidden=False),
            at("overview.html", pinned=False), at("overview.html", pinned=True), at("overview.html", hidden=False),
            at("overview.html", hidden=True)])
        self.assertEqual([t for t in out["toasts"] if t[1]], [["Pinned The plan", True], ["Hidden: The plan. The Hidden filter shows it.", True],
                                                             ["Unpinned Overview", True], ["Unhidden: Overview", True]])
        self.assertEqual([t for t in out["toasts"] if not t[1]], [["Pin undone · The plan", False], ["Hide undone · The plan", False],
                                                                 ["Unpin undone · Overview", False], ["Unhide undone · Overview", False]])
        # Every landed write and every Undo reads the list again, so the rows show what the store now holds.
        self.assertEqual(out["gets"], ["/api/documents"] * 9)
        self.assertEqual(out["R"]["when"], ["already pinned", "already hidden"])

    def root(self, key):
        return next(r for r in self.doc["roots"] if r["key"] == key)

    def test_rename_and_hide_a_root_post_its_view_and_undo_posts_the_value_shown(self):
        """sd:3986: Rename root asks for the name, filled with the one set; Undo posts it back. Hide root posts hidden."""
        self.root("lab").update(label="Research Lab", set="Research Lab")
        out = self.run_page("""const lab = C.get('root:lab');
R.fields = cmd('root.rename').fields(lab).map(f => [f.name, f.value, f.placeholder, !!f.required]);
R.det = ELS.details.html; R.chips = ELS.facets.html;
FIELD_VALUES.label = 'Lab Notes'; shellRun(cmd('root.rename'), lab); await flush(); lastToast().undo(); await flush();
FIELD_VALUES.label = ''; shellRun(cmd('root.rename'), lab); await flush();
shellRun(cmd('root.hide'), lab); await flush(); lastToast().undo(); await flush();""",
                            answer=self.VIEW % json.dumps(self.doc), row="'lab/plan.html'")
        self.assertEqual(out["R"]["fields"], [["label", "Research Lab", "Lab", False]])
        self.assertEqual(self.view_posts(out), [{"root": "lab", "label": "Lab Notes"}, {"root": "lab", "label": "Research Lab"},
                                                {"root": "lab", "label": ""},
                                                {"root": "lab", "hidden": True}, {"root": "lab", "hidden": False}])
        self.assertEqual([t[0] for t in out["toasts"] if t[1]],
                         ["Renamed Research Lab to Lab Notes", "Research Lab is Lab again",
                          "Hidden: Research Lab. Hidden roots, under Repo, lists it."])
        self.assertIn("Research Lab", out["R"]["chips"])
        self.assertIn('<dt>Label</dt><dd>Research Lab', out["R"]["det"])

    def test_a_refused_rename_says_why_and_the_name_shown_stays(self):
        """The failure table's first Rename: a refused write names the reason and reads nothing again."""
        out = self.run_page("""FIELD_VALUES.label = 'x'.repeat(61); shellRun(cmd('root.rename'), C.get('root:lab')); await flush();
R.chips = ELS.facets.html;""", answer="(path, body) => path === '/api/documents/view' ? [400, { error: 'a label is 1 to 60 characters' }] : [200, %s]"
                            % json.dumps(self.doc))
        self.assertEqual(out["toasts"], [["Lab not changed: a label is 1 to 60 characters", False]])
        self.assertEqual(out["gets"], ["/api/documents"])
        self.assertIn('<span class="nm">Lab</span>', out["R"]["chips"])

    def test_a_hidden_root_leaves_the_repo_chips_for_the_hidden_roots_fold(self):
        """sd:3986: a hidden root's chip goes; the fold under Repo names it with Unhide. A label| line still in the file is named."""
        self.root("civic")["hidden"] = True
        self.doc["documents"] = [r for r in self.doc["documents"] if r["key"] != "civic"]
        self.root("lab")["file_label"] = "Old Lab"
        out = self.run_page("""R.facets = ELS.facets.html; R.det = ELS.details.html; R.sub = ELS.subhead.html;
R.when = [cmd('root.hide').when(C.get('root:civic')), cmd('root.unhide').when(C.get('root:lab')), cmd('root.unhide').primary(C.get('root:civic'))];
shellRun(cmd('root.unhide'), C.get('root:civic')); await flush();""", answer=self.VIEW % json.dumps(self.doc), row="'lab/plan.html'")
        facets = out["R"]["facets"]
        self.assertNotIn('value="civic"', facets)
        self.assertIn('value="lab"', facets)
        self.assertRegex(facets, r'<details class="rootfold"[^>]*><summary>Hidden roots <span class="n">1</span></summary>')
        self.assertIn("Civic", facets.split('class="rootfold"')[1])
        self.assertEqual(out["R"]["when"], ["already hidden", "not hidden", True])
        self.assertEqual(self.view_posts(out), [{"root": "civic", "hidden": False}])
        self.assertIn("2 roots · read", out["R"]["sub"])
        self.assertIn("label|lab|Old Lab", out["R"]["det"])
        self.assertIn("not read", out["R"]["det"])

    def test_a_refused_write_says_why_offers_no_undo_and_reads_nothing_again(self):
        out = self.run_page("""shellRun(cmd('document.pin'), C.get('lab/plan.html')); await flush();""",
                            answer="(path, body) => path === '/api/documents/view' ? [400, { error: 'boom' }] : [200, %s]" % json.dumps(self.doc))
        self.assertEqual(out["toasts"], [["The plan not changed: boom", False]])
        self.assertEqual(out["gets"], ["/api/documents"])

    def test_tag_posts_the_list_with_the_typed_tag_and_untag_posts_it_without(self):
        self.rows_of(**{"lab/plan.html": {"tags": ["board"]}})
        out = self.run_page("""const d = C.get('lab/plan.html'), tag = cmd('document.tag');
const enter = v => { document.getElementById('tag-in').value = v;
  ELS.details.listeners.keydown.forEach(f => f({ target: { id: 'tag-in', value: v }, key: 'Enter', preventDefault() {} })); };
R.det = ELS.details.html; R.when = tag.when(d); enter('board'); await flush();
enter('  Q4 Plan '); await flush(); lastToast().undo(); await flush();
C.put({ id: 'tag:b', type: 'document tag', doc: d, tag: 'board', label: 'board on The plan' });
shellRun(cmd('document.untag'), C.get('tag:b')); await flush(); lastToast().undo(); await flush();""",
                            answer=self.VIEW % json.dumps(self.doc), row="'lab/plan.html'")
        self.assertIn('id="tag-in"', out["R"]["det"])
        self.assertIn('data-cmd="document.untag"', out["R"]["det"])
        self.assertIs(out["R"]["when"], True)
        def tags(*t):
            return {"root": "lab", "file": "plan.html", "tags": list(t)}
        self.assertEqual(self.view_posts(out), [tags("board", "q4-plan"), tags("board"), tags(), tags("board")])
        self.assertEqual([t[0] for t in out["toasts"]], ["The plan not changed: already tagged board", "Tagged q4-plan · The plan",
                                                        "Tag undone · The plan", "Removed tag board", "Untag undone · board on The plan"])
        self.assertEqual(out["confirms"], [], "Enter in the Tags field tags the shown document without asking")

    def test_tag_asks_with_the_last_tag_filled_in_and_names_the_tag(self):
        # REGRESSION sd:3523: after one tag, Tag on another row applied the remembered tag with no field and no label.
        out = self.run_page("""const tag = cmd('document.tag'), other = C.get('lab/overview.html');
document.getElementById('tag-in').value = 'q4';
ELS.details.listeners.keydown.forEach(f => f({ target: { id: 'tag-in', value: 'q4' }, key: 'Enter', preventDefault() {} })); await flush();
document.getElementById('tag-in').value = '';
R.fields = (tag.fields ? tag.fields(other) : []).map(f => [f.name, f.value, f.required]);
shellRun(tag, other); await flush();
FIELD_VALUES.tag = 'Board Q1'; shellRun(tag, other); await flush();""", answer=self.VIEW % json.dumps(self.doc), row="'lab/plan.html'")
        self.assertEqual(out["R"]["fields"], [["tag", "q4", True]])
        self.assertEqual(out["confirms"], ["document.tag", "document.tag"], "Tag ran on another row without showing its tag")
        self.assertEqual([t[0] for t in out["toasts"]], ["Tagged q4 · The plan", "Tagged q4 · Overview", "Tagged board-q1 · Overview"])

    def test_hidden_shows_the_hidden_documents_alone_and_open_the_rest(self):
        """Open | Hidden sits in the filter area, beside Pinned only (operator 2026-11-01; pack sd:7263 dropped All)."""
        self.rows_of(**{"civic/brief.html": {"hidden": True}, "lab/plan.html": {"pinned": True}})
        def click(v):
            return ("ELS.facets.listeners.click[0]({ target: { closest: s => s === 'button[data-aside=\"hidden\"]'"
                    f" ? {{ dataset: {{ v: '{v}' }} }} : null }} }});")
        out = self.run_page(f"""R.rows = ELS.rows.html; R.seg = ELS.facets.html; {click('aside')}
R.hidden = ELS.rows.html; R.url = {self.URL};
{click('open')} R.back = ELS.rows.html;""")
        def ids(rows):
            return re.findall(r'data-id="([^"]+)"', rows)
        # Pinned first; the hidden one is out of the list until the filter shows it.
        self.assertEqual(ids(out["R"]["rows"]), ["lab/plan.html", "lab/orphan.html", "lab/overview.html"])
        self.assertIn('data-v="aside" aria-pressed="false">Hidden <span class="n">1</span>', out["R"]["seg"])
        self.assertNotIn('data-v="all"', out["R"]["seg"])
        self.assertEqual(ids(out["R"]["hidden"]), ["civic/brief.html"])
        self.assertIn('class="hid"', out["R"]["hidden"])
        self.assertEqual(out["R"]["url"], "hidden=1")
        self.assertEqual(ids(out["R"]["back"]), ids(out["R"]["rows"]))
        # A link to the hidden view opens it.
        out = self.run_page("R.rows = ELS.rows.html;", search="?hidden=1")
        self.assertEqual(ids(out["R"]["rows"]), ["civic/brief.html"])

    def test_disable_render_is_off_with_its_reason(self):
        out = self.run_page("""const plan = C.get('lab/plan.html');
R.skip = [cmd('document.skip').when(plan), cmd('document.skip-repo').when(plan)];""")
        self.assertIn("SD_SKIP_RENDER skips every render", out["R"]["skip"][0])
        self.assertIn("skip|<key>|<file>", out["R"]["skip"][0])
        for reason in out["R"]["skip"]:
            self.assertIn("without stopping its render", reason)

    def test_render_runs_the_render_one_at_a_time_and_reads_the_rows_when_it_ends(self):
        # sd:5884: Render was a copy-only line; it runs as Research's does, through POST /api/research/render and the run
        # it follows, keyed by the root's checkout. While the run goes, Render is off; when it ends, the rows are read again.
        run = "render-20261023T190000Z"
        answer = f"""(path, body) => {{
  if (path === '/api/research/render') return [202, {{ id: '{run}', verb: 'render', state: 'queued', repo: body.repo }}];
  if (path === '/api/research/deck/{run}') {{ R.during = cmd('document.render').when(C.get('lab/plan.html')); return [200, {{ id: '{run}', state: 'done' }}]; }}
  return [200, {json.dumps(self.doc)}];
}}"""
        out = self.run_page("""const plan = C.get('lab/plan.html'), brief = C.get('civic/brief.html'), r = cmd('document.render');
R.render = [r.executes, r.cli(plan), r.when(plan), r.when(brief), r.primary(plan), r.primary(C.get('lab/overview.html')), r.sends(plan)];
R.reads = OUT.gets.filter(g => g === '/api/documents').length;
shellRun(r, plan); await flush();
R.after = r.when(plan); R.rereads = OUT.gets.filter(g => g === '/api/documents').length - R.reads;""", answer=answer)
        self.assertEqual(out["R"]["render"], [True, "cd '~/repos/lab' && sd-research-kit render", True,
                                              "not a research repo: no source renders this page", True, False,
                                              'POST /api/research/render {"repo": "lab"}'])
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/research/render", {"repo": "lab"}]])
        self.assertEqual(out["R"]["during"], f"a render is running now (run {run})")
        self.assertEqual((out["R"]["after"], out["R"]["rereads"]), (True, 1))
        self.assertEqual([t[0] for t in out["toasts"]], [f"Rendering The plan · run {run}", "Rendered · The plan"])

    def test_render_is_off_for_a_research_root_the_server_cannot_run(self):
        for root in self.doc["roots"]:
            root["render"] = ""
        out = self.run_page("R.when = cmd('document.render').when(C.get('lab/plan.html'));")
        self.assertEqual(out["R"]["when"], "~/repos/lab is not a research checkout under the repo root, so the dashboard cannot run its render")
        self.assertEqual(out["posts"], [])

    def test_the_badge_rule_drops_a_hidden_stale_document(self):
        # sd:3446: the rail's rule is the read's; a stale row the operator hid no longer lights it.
        self.assertEqual(self.doc["attention"], {"state": "caution", "n": 1, "what": "render-stale documents"})
        self.rows_of(**{"lab/plan.html": {"hidden": True}})
        self.assertEqual(documents_screen._attention(self.doc["documents"]), {"state": "ok", "n": 0, "what": "render-stale documents"})

    def test_open_opens_the_served_address_in_a_new_tab(self):
        out = self.run_page("""const o = cmd('document.open'), d = C.get('civic/brief.html'); R.cli = o.cli(d); shellRun(o, d);""")
        self.assertEqual(out["opened"], [["/documents/civic/brief.html", "_blank", "noopener"]])
        self.assertEqual(out["R"]["cli"], "open http://dash.example.test/documents/civic/brief.html")

    def test_the_rows_carry_each_render_state_and_the_rail_counts_the_stale_ones(self):
        out = self.run_page("R.rows = ELS.rows.html; R.tally = ELS.tally.html; R.sub = ELS.subhead.html;")
        rows = out["R"]["rows"]
        self.assertEqual(re.findall(r'data-id="([^"]+)"', rows), ["civic/brief.html", "lab/orphan.html", "lab/plan.html", "lab/overview.html"])
        self.assertEqual(re.findall(r'class="g g-(\w+)"', rows), ["unknown", "unknown", "caution", "ok"])
        self.assertIn("▲ 1 render-stale", out["R"]["tally"])
        # sd:5524: the tab carries the count; the line under the title names only the roots.
        self.assertIn("3 roots · read", out["R"]["sub"])
        self.assertNotIn("documents in", out["R"]["sub"])
        self.assertEqual(out["counts"], {"documents": 4})
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 1, "what": "render-stale documents"})

    def test_a_request_files_the_task_its_line_names_and_undo_cancels_it(self):
        """sd:5887: Request files through /api/items, as Capture does, and keeps the sd task add line.
        sd:5958: both name the root's checkout, so the task belongs to that repository."""
        out = self.run_page("""const was = ANSWER;
ANSWER = (p, b) => p === '/api/items' ? [201, { item: { id: 72 }, revision: 'r72' }] : /cancel-task$/.test(p) ? [200, {}] : was(p, b);
const input = ELS.shift; input.value = 'request water summary repo:lab';
input.listeners.input.forEach(f => f()); input.listeners.input.forEach(f => f());
const r = cmd('document.request'), d = C.get('draft:request');
R.r = [r.executes, r.when(d), r.cli(d)]; shellRun(r, d); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();""")
        executes, when, cli = out["R"]["r"]
        self.assertEqual((executes, when), (True, True))
        self.assertEqual(cli, "sd task add 'Document request: water summary' --belongs-to ~/'repos/lab'"
                              " --body 'repo=lab kind=research skill=sd-research-repo' --priority 3")
        self.assertEqual([p[:2] for p in out["posts"]],
                         [["/api/items", {"title": "Document request: water summary", "body": "repo=lab kind=research skill=sd-research-repo", "priority": 3,
                                          "repo": "~/repos/lab"}],
                          ["/api/items/72/cancel-task", {"reason": "withdrawn on Documents right after filing", "revision": "r72"}]])
        self.assertEqual(out["R"]["toast"], "Request filed · #72 · water summary")

    def test_a_request_names_only_a_repository_it_can_resolve(self):
        # An unknown or ambiguous repo: never falls back to the selected row's repository; an exact key or a unique prefix wins.
        self.doc["roots"].append({"key": "lab-two", "label": "Lab Two", "path": "~/repos/lab-two/docs/dashboard", "n": 0, "research": False})
        out = self.run_page("""const input = ELS.shift, r = cmd('document.request'), d = C.get('draft:request');
const ask = v => { input.value = ''; input.listeners.beforeinput.forEach(f => f()); input.value = v;
  input.listeners.input.forEach(f => f()); input.listeners.input.forEach(f => f()); return [r.when(d), d.repo, r.cli(d)]; };
R.unknown = ask('request memo repo:nowhere'); R.ambiguous = ask('request memo repo:la');
R.exact = ask('request memo repo:lab'); R.unique = ask('request memo repo:ci'); R.bare = ask('request memo repo:lab-two');""")
        R = out["R"]
        for got, word in ((R["unknown"], "nowhere"), (R["ambiguous"], "lab, lab-two")):
            self.assertIsInstance(got[0], str)
            self.assertIn(word, got[0])
            self.assertEqual(got[1:], [None, ""])
        self.assertEqual(R["exact"][:2], [True, "lab"])
        self.assertEqual(R["unique"][:2], [True, "civic"])
        self.assertNotIn("repo:", R["exact"][2])
        # sd:5958: a root that names no checkout files its request under none.
        self.assertEqual(R["bare"][:2], [True, "lab-two"])
        self.assertNotIn("--belongs-to", R["bare"][2])

    def test_a_request_takes_the_skill_its_hint_asks_for(self):
        # A report has no skill; the hint says to name one with skill:, so that token names it and leaves the title.
        out = self.run_page("""const input = ELS.shift; input.value = 'request budget memo repo:civic skill:sd-writer';
input.listeners.input.forEach(f => f()); input.listeners.input.forEach(f => f());
R.cli = cmd('document.request').cli(C.get('draft:request'));""")
        self.assertEqual(out["R"]["cli"], "sd task add 'Document request: budget memo' --belongs-to ~/'repos/civic'"
                                          " --body 'repo=civic kind=report skill=sd-writer' --priority 3")

    def test_each_read_state_is_said_in_the_slot(self):
        out = self.run_page("")
        self.assertEqual(out["states"][0]["kind"], "loading")
        # The fixture's twin key is contested: the slot names it, and the source is the config.
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("use the name twin", out["states"][-1]["text"])
        empty = {**self.doc, "roots": [], "documents": [], "contested": []}
        last = self.run_page("", answer=f"() => [200, {json.dumps(empty)}]")["states"][-1]
        self.assertEqual((last["kind"], last["title"]), ("empty", "No document roots"))
        self.assertIn("docs/dashboard", last["text"])
        failed = self.run_page("", answer="() => [500, { error: 'boom' }]")["states"][-1]
        self.assertEqual(failed["kind"], "error")
        self.assertIn("boom", failed["text"])

    def test_a_root_read_without_its_config_says_so_with_the_render_fix(self):
        """sd:5666: a computed config with no manifest, or a manifest of an older config, is said in Details and the slot."""
        self.doc["contested"] = []
        lab = next(r for r in self.doc["roots"] if r["key"] == "lab")
        lab["conf"] = "research.conf.py computes its data; run sd-research-kit render once to write docs/dashboard/research.json"
        out = self.run_page("R.det = ELS.details.html;", row="'lab/plan.html'")
        self.assertIn("research.conf.py was not read: research.conf.py computes its data; run sd-research-kit render once", out["R"]["det"])
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("Lab: research.conf.py was not read", out["states"][-1]["text"])
        lab.update(conf="", manifest="older")
        out = self.run_page("R.det = ELS.details.html;", row="'lab/plan.html'")
        self.assertIn("run sd-research-kit render in ~/repos/lab to refresh it", out["R"]["det"])
        self.assertIn("Lab: docs/dashboard/research.json is from an older research.conf.py", out["states"][-1]["text"])
        # A current manifest, like a literal config, says nothing.
        lab["manifest"] = "current"
        out = self.run_page("R.det = ELS.details.html;", row="'lab/plan.html'")
        self.assertNotIn("research.json", out["R"]["det"])
        self.assertIsNone(out["states"][-1])

    def test_repo_is_a_multiselect_dropdown_and_two_repos_show_their_union(self):
        """sd:5158: Repo is a menu of checkboxes; two picks list the documents of both."""
        box = ("const box = (v, on, off) => ELS.facets.listeners.change[0]({ currentTarget: ELS.facets, target: { dataset: { pick: 'repo' },"
               " type: 'checkbox', value: v, checked: on, getAttribute: a => a === 'aria-disabled' && off ? 'true' : null } });\n")
        out = self.run_page(box + """R.before = ELS.facets.html; box('civic', true); await flush(); R.one = ELS.rows.html;
box('lab', true); await flush(); R.two = ELS.rows.html; R.after = ELS.facets.html; R.picked = [...F.repo];""")
        before = out["R"]["before"]
        self.assertEqual(re.findall(r'<input type="checkbox" data-pick="repo" value="([^"]+)"', before), ["civic", "lab", "quiet"])
        self.assertNotIn('data-f="repo"', before)
        def ids(rows):
            return re.findall(r'data-id="([^"]+)"', rows)
        self.assertEqual(ids(out["R"]["one"]), ["civic/brief.html"])
        self.assertEqual(sorted(ids(out["R"]["two"])), ["civic/brief.html", "lab/orphan.html", "lab/overview.html", "lab/plan.html"])
        self.assertRegex(out["R"]["after"], r'<details class="fpick" data-pick="repo" open><summary[^>]*><span id="f-repo-v">2 picked</span>')
        self.assertEqual(out["R"]["picked"], ["civic", "lab"])

    # sd:5484: the page's localStorage, as the browser keeps it across sessions; KEPT is what an earlier session left.
    STORE = ("location.pathname = '/documents'; var KEPT = %s;"
             " var localStorage = { getItem: k => k in KEPT ? KEPT[k] : null, setItem: (k, v) => { KEPT[k] = String(v); } };\n")
    BANDS = r'<tr class="band" data-band="([^"]+)"[^>]*><td colspan="7">([^<]*)<span class="n">([^<]*)</span></td></tr>'

    def test_the_list_groups_by_repo_with_one_fold_band_each(self):
        """sd:5484: one band row per repo heads that repo's rows, with how many it holds; the shell folds a band row."""
        self.rows_of(**{"lab/plan.html": {"pinned": True}})
        out = self.run_page("R.rows = ELS.rows.html;")
        rows = out["R"]["rows"]
        self.assertEqual(re.findall(self.BANDS, rows), [("civic", "Civic", "1 document"), ("lab", "Lab", "3 documents")])
        # Each band heads its own repo's rows; pinned first inside the band, then the sort (newest first).
        order = re.findall(r'data-(band|id)="([^"]+)"', rows)
        self.assertEqual(order, [("band", "civic"), ("id", "civic/brief.html"), ("band", "lab"), ("id", "lab/plan.html"),
                                 ("id", "lab/orphan.html"), ("id", "lab/overview.html")])
        # Sorting by repo descending turns the bands around.
        click = self.CLICK % ("rows-head", "'button[data-sort]'", "{ sort: 'repo' }")
        out = self.run_page(f"{click} {click} R.rows = ELS.rows.html;")
        self.assertEqual([b[0] for b in re.findall(self.BANDS, out["R"]["rows"])], ["lab", "civic"])

    def test_the_destination_filter_keeps_rows_with_a_chosen_destination_on(self):
        """A destination is on when the row designates it or holds a copy; None keeps rows with neither."""
        for row in self.doc["documents"]:
            row["pub"] = {"lab/plan.html": {"choice": {"notion": True, "drive": False}, "copies": []},
                          "lab/overview.html": {"choice": {}, "copies": [{"channel": "drive", "title": "Drive", "state": "has"}]}}.get(f"{row['key']}/{row['file']}")
        def ids(out):
            return re.findall(r'data-id="([^"]+)"', out["R"]["rows"])
        out = self.run_page("R.rows = ELS.rows.html; R.facets = ELS.facets.html;", search="?dest=notion")
        self.assertEqual(ids(out), ["lab/plan.html"])
        self.assertIn('data-f="dest" data-v="none"', out["R"]["facets"])
        self.assertEqual(sorted(ids(self.run_page("R.rows = ELS.rows.html;", search="?dest=drive,notion"))), ["lab/overview.html", "lab/plan.html"])
        self.assertEqual(sorted(ids(self.run_page("R.rows = ELS.rows.html;", search="?dest=none"))), ["civic/brief.html", "lab/orphan.html"])

    def test_chosen_repos_show_only_their_bands(self):
        out = self.run_page("R.rows = ELS.rows.html;", search="?repo=lab")
        self.assertEqual([b[0] for b in re.findall(self.BANDS, out["R"]["rows"])], ["lab"])
        self.assertNotIn("civic/brief.html", out["R"]["rows"])

    def test_the_filters_persist_across_sessions_and_an_address_wins(self):
        """sd:5484: repo, kind, freshness, text, pinned, hidden, sort and size are kept per page; the next visit restores them."""
        click = self.CLICK % ("rows-head", "'button[data-sort]'", "{ sort: 'title' }")
        out = self.run_page(f"{click} R.kept = KEPT;", search="?repo=lab&kind=research&q=plan&page=2",
                            shell_more=self.STORE % "{}")
        # The page number is not kept: the next visit starts on page one.
        self.assertEqual(out["R"]["kept"], {"sd.filters./documents": "repo=lab&kind=research&q=plan&sort=title&dir=asc"})
        kept = json.dumps(out["R"]["kept"])
        out = self.run_page(f"R.rows = ELS.rows.html; R.url = {self.URL}; R.input = ELS.shift.value; R.f = [[...F.repo], [...F.kind], F.text];",
                            shell_more=self.STORE % kept)
        self.assertEqual(re.findall(r'data-id="([^"]+)"', out["R"]["rows"]), ["lab/plan.html"])
        self.assertEqual(out["R"]["f"], [["lab"], ["research"], "plan"])
        self.assertEqual((out["R"]["url"], out["R"]["input"]), ("repo=lab&kind=research&q=plan&sort=title&dir=asc", "plan"))
        # A link that names its own filters wins over what was kept, and is kept in its place.
        out = self.run_page("R.rows = ELS.rows.html; R.kept = KEPT;", search="?repo=civic", shell_more=self.STORE % kept)
        self.assertEqual(re.findall(r'data-id="([^"]+)"', out["R"]["rows"]), ["civic/brief.html"])
        self.assertEqual(out["R"]["kept"], {"sd.filters./documents": "repo=civic"})
        # A row link alone names no filter: the kept ones still apply.
        out = self.run_page("R.f = [...F.repo];", row="'lab/plan.html'", search="?row=lab/plan.html", shell_more=self.STORE % kept)
        self.assertEqual(out["R"]["f"], ["lab"])
        # Clear all is kept too, and leaves the sort, which is no filter; a store that throws keeps the filters for this visit only.
        out = self.run_page("ELS.chips.listeners.click[0]({ target: { closest: s => s === '[data-unfilter-all]' ? {} : null } }); R.kept = KEPT;",
                            shell_more=self.STORE % kept)
        self.assertEqual(out["R"]["kept"], {"sd.filters./documents": "sort=title&dir=asc"})
        throws = "var localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };\n"
        out = self.run_page("R.rows = ELS.rows.html;", search="?repo=lab", shell_more=throws)
        self.assertNotIn("civic/brief.html", out["R"]["rows"])

    def test_an_unavailable_chip_stays_focusable_says_why_and_does_nothing(self):
        box = ("ELS.facets.listeners.change[0]({ currentTarget: ELS.facets, target: { dataset: { pick: 'repo' }, type: 'checkbox',"
               " value: 'quiet', checked: true, getAttribute: a => a === 'aria-disabled' ? 'true' : null } });\n")
        out = self.run_page("""R.facets = ELS.facets.html;
const chip = (f, v) => ({ id: '', dataset: { f: f, v: v }, getAttribute: a => a === 'aria-disabled' ? 'true' : null });
ELS.facets.listeners.click.forEach(h => h({ target: { closest: () => chip('kind', 'design') } }));
""" + box + "R.picked = [...F.kind, ...F.repo];")
        facets = out["R"]["facets"]
        # A native disabled button leaves the tab order, and its title with it; the reason must stay reachable.
        self.assertNotRegex(facets, r"<(button|input)[^>]*\sdisabled[\s>]")
        design = re.search(r'<button[^>]*data-v="design"[^>]*>', facets).group(0)
        quiet = re.search(r'<input[^>]*value="quiet"[^>]*>', facets).group(0)
        for tag in (design, quiet):
            self.assertIn('aria-disabled="true"', tag)
        # A title is a hover tooltip only; the reason is an element the chip names, so focus and a screen reader reach it.
        for tag, reason in ((design, "No design documents"), (quiet, "Nothing generated yet in ~/repos/quiet/docs/dashboard")):
            ref = re.search(r'aria-describedby="([^"]+)"', tag)
            self.assertIsNotNone(ref, tag)
            said = re.search(rf'<(span|small)[^>]*id="{re.escape(ref.group(1))}"[^>]*>([^<]*)</(span|small)>', facets)
            self.assertIsNotNone(said, ref.group(1))
            self.assertIn(reason, said.group(2))
        self.assertNotIn("aria-disabled", re.search(r'<button[^>]*data-v="research"[^>]*>', facets).group(0))
        self.assertEqual(out["R"]["picked"], [])

    def test_the_row_in_the_address_is_selected_and_details_name_its_facts(self):
        out = self.run_page("R.det = ELS.details.html;", row="'lab/plan.html'")
        det = out["R"]["det"]
        self.assertIn("render-stale", det)
        self.assertIn("notes/PLAN.md", det)
        self.assertIn("What we build next.", det)
        self.assertIn('id="tag-in"', det)

    def test_a_linked_row_that_is_not_first_survives_the_first_render(self):
        # The real shell: reconcile selects the first shown row when nothing is selected, and select rewrites ?row=.
        rewrites = (r"""C.select = id => { ROW = id; };
window.shell.reconcile = o => { if (o.current != null) return o.current;"""
                    r""" const m = /data-id="([^"]+)"/.exec(ELS.rows.html || ''); if (m) { o.select(m[1]); return m[1]; } return null; };""")
        out = self.run_page("R.row = ROW; R.det = ELS.details.html;", row="'lab/overview.html'", shell_more=rewrites)
        self.assertEqual(out["R"]["row"], "lab/overview.html")
        self.assertIn("00-overview/Overview.md", out["R"]["det"])

    def test_the_page_reads_through_the_shared_reader(self):
        # read.js holds the generation, retirement and failure rules (sd:2490); the page keeps no fetch of its own.
        self.assertIn("shell.read(", DOCUMENTS_JS)
        self.assertNotIn("fetch(", DOCUMENTS_JS)
        out = self.run_page("")
        self.assertEqual(out["states"][0]["text"], "Reading the documents. Rows appear when the read finishes.")

    def test_a_document_the_next_read_drops_runs_no_command_and_loses_the_selection(self):
        out = self.run_page("""const first = ANSWER()[1];
ANSWER = () => [200, { ...first, documents: first.documents.filter(d => d.file !== 'plan.html'), attention: { state: 'ok', n: 0, what: 'render-stale documents' } }];
await load();
R.type = C.get('lab/plan.html').type; R.rows = ELS.rows.html; R.det = ELS.details.html; R.att = window.PAGE_ATTENTION;""",
                            row="'lab/plan.html'")
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertNotIn("lab/plan.html", out["R"]["rows"])
        self.assertNotIn("What we build next.", out["R"]["det"])
        self.assertIn("Civic brief", out["R"]["det"])
        self.assertEqual(out["R"]["att"], {"state": "ok", "n": 0, "what": "render-stale documents"})

    def test_an_older_read_that_answers_last_changes_nothing(self):
        out = self.run_page("""const first = ANSWER()[1], held = [];
ANSWER = () => new Promise(ok => held.push(ok));
const older = load(), newer = load();
held[1]([200, { ...first, documents: first.documents.slice(0, 1) }]); await newer;
held[0]([200, first]); await older;
R.rows = ELS.rows.html; R.type = C.get('lab/plan.html').type;""")
        self.assertEqual(re.findall(r'data-id="([^"]+)"', out["R"]["rows"]), ["civic/brief.html"])
        self.assertEqual(out["R"]["type"], "not listed")

    def test_a_failed_read_after_a_good_one_clears_the_rows_and_the_rail(self):
        out = self.run_page("""ANSWER = () => [500, { error: 'boom' }]; await load();
R.rows = ELS.rows.html; R.type = C.get('lab/plan.html').type; R.att = window.PAGE_ATTENTION; R.det = ELS.details.html;""",
                            row="'lab/plan.html'")
        self.assertEqual(re.findall(r'data-id="([^"]+)"', out["R"]["rows"]), [])
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(out["R"]["att"]["state"], "unknown")
        self.assertIn("were not read", out["R"]["det"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])

    def test_a_page_number_that_is_not_a_whole_number_reads_as_page_one(self):
        # ?page=1.5 sliced mid-page, pressed no pager button and stayed in the address (sd:2427).
        self.many(60)
        for search, page_no in (("?page=1.5", "1"), ("?page=0x2", "1"), ("?page=2", "2")):
            out = self.run_page(f"R.pager = ELS.pager.html; R.url = {self.URL};", search=search)
            current = re.findall(r'data-page="(\d+)"[^>]*aria-current="page"', out["R"]["pager"])
            self.assertEqual(current, [page_no], search)
            self.assertEqual(re.findall(r"page=(\d+)", out["R"]["url"]), [] if page_no == "1" else [page_no], search)

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", DOCUMENTS_JS)
        self.assertNotIn("setAttribute('style'", DOCUMENTS_JS)
        self.assertNotRegex(DOCUMENTS_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", DOCUMENTS_JS)
        self.assertNotRegex(DOCUMENTS_JS, r"e\.key !?== '[jk]'")
        self.assertNotIn("127.0.0.1", DOCUMENTS_JS)
        self.assertIn("window.PAGE_LIST", DOCUMENTS_JS)


def learning(connection, title, stage, **fields):
    """One `sdw.learning` base row, as the vault import files it: an idea item of source vault whose fields name its kind."""
    return create_item(connection, kind="idea", title=title, status={"queued": "planning", "in-progress": "ready"}.get(stage, "done"),
                       source="vault", external_id=f"learning:Learning/{title}.md", path=f"Learning/{title}.md", stage=stage,
                       fields={"kind": "learning", "dateCreated": "2026-01-06", **fields}, body={"markdown": ""})


def seed_learning(connection):
    """A book, an article with a URL that is no link, a book with only a URL, a video, a video only its title names,
    and a course."""
    return {
        "book": learning(connection, "Agile Retrospectives", "in-progress", **{"content-type": "book", "author": ["A. Writer", "B. Writer"],
                         "subtitle": "Making Good Teams Great", "url": "https://books.example.test/retro", "published-date": "2006",
                         "total-pages": "170", "isbn": "9780000000000", "description": "Retrospectives that work.",
                         "category": "Teams & Facilitation"}),
        "article": learning(connection, "On Queues", "queued", **{"content-type": "article", "publisher": "Example Weekly",
                            "url": "javascript:alert(1)", "category": ["Computers"]}),
        "bare": learning(connection, "A Bare Book", "done", **{"content-type": "book", "url": "https://press.example.test/bare"}),
        "video": learning(connection, "2016-06-06 - YouTube - Example Channel - A Talk", "in-progress",
                          **{"content-type": "youtube", "channel": "Example Channel", "url": "https://www.youtube.com/watch?v=x",
                             "published-date": "2016-06-06T10:00:00Z", "duration": "534", "category": ["Coding"]}),
        "named": learning(connection, "2024-02-03 - YouTube - Other Channel - Queues - A Primer", "queued",
                          **{"url": "https://www.youtube.com/watch?v=y"}),
        "course": learning(connection, "A Course", "queued", **{"content-type": "course", "url": "https://course.example.test"}),
    }


class TheBooks(ScreenCase):
    """sd:6260: `/api/documents/books` lists the learning rows that are books or articles, each with its title, author or
    source, URL and read state, and nothing else."""

    def test_a_book_row_says_its_title_author_or_source_url_and_read_state(self):
        ids = seed_learning(self.connection)
        rows = {r["id"]: r for r in learning_screen.books(self.connection, now=NOW)["rows"]}
        # sd:10634: a course is in Books, the one learning list, rather than in no view at all.
        self.assertEqual(sorted(rows), sorted([ids["book"], ids["article"], ids["bare"], ids["course"]]), "a video is no book")
        self.assertEqual(rows[ids["course"]]["kind"], "course")
        book = rows[ids["book"]]
        self.assertEqual((book["title"], book["by"], book["url"], book["host"], book["stage"], book["state"], book["kind"]),
                         ("Agile Retrospectives", "A. Writer, B. Writer", "https://books.example.test/retro", "books.example.test",
                          "in-progress", "reading", "book"))
        self.assertEqual((book["subtitle"], book["published"], book["pages"], book["isbn"], book["added"]),
                         ("Making Good Teams Great", "2006", "170", "9780000000000", "2026-01-06"))
        # The source is the publisher when no author is named, else the URL's host.
        article, bare = rows[ids["article"]], rows[ids["bare"]]
        self.assertEqual((article["by"], article["state"], article["kind"]), ("Example Weekly", "to read", "article"))
        self.assertEqual((bare["by"], bare["state"]), ("press.example.test", "done"))
        # A URL that is not http or https is no link: the page shows its text.
        self.assertEqual((article["url"], article["url_text"]), ("", "javascript:alert(1)"))

    def test_a_malformed_url_is_text_and_the_other_rows_still_read(self):
        """sd:6280: a URL urlsplit refuses no longer fails the whole read; its row shows the text, unlinked."""
        ids = seed_learning(self.connection)
        broken = learning(self.connection, "Broken Link", "queued", **{"content-type": "book", "url": "https://[broken"})
        wide = learning(self.connection, "Wide Colon", "queued", **{"content-type": "article", "url": "https://example.com\uff1a80"})
        rows = {r["id"]: r for r in learning_screen.books(self.connection, now=NOW)["rows"]}
        self.assertEqual((rows[broken]["url"], rows[broken]["host"], rows[broken]["url_text"]), ("", "", "https://[broken"))
        self.assertEqual((rows[wide]["url"], rows[wide]["url_text"]), ("", "https://example.com\uff1a80"))
        self.assertEqual(rows[ids["book"]]["url"], "https://books.example.test/retro")

    def test_each_view_names_its_categories_and_each_row_its_category(self):
        """pack sd:7669: each view sends its fixed category set; a row's category is its `category` field as text,
        and a value off the set is still sent, so the page files it under Other."""
        ids = seed_learning(self.connection)
        doc = learning_screen.books(self.connection, now=NOW)
        self.assertEqual(doc["categories"], list(learning_screen.CATEGORIES["books"]))
        rows = {r["id"]: r for r in doc["rows"]}
        self.assertEqual((rows[ids["book"]]["category"], rows[ids["article"]]["category"], rows[ids["bare"]]["category"]),
                         ("Teams & Facilitation", "Computers", ""))
        doc = learning_screen.videos(self.connection, now=NOW)
        self.assertEqual(doc["categories"], list(learning_screen.CATEGORIES["videos"]))
        self.assertEqual({r["id"]: r["category"] for r in doc["rows"]}, {ids["video"]: "Coding", ids["named"]: ""})
        self.assertTrue(all(2 < len(set(c)) <= 6 for c in learning_screen.CATEGORIES.values()))

    def test_a_row_files_under_the_first_value_its_view_names_whatever_the_case(self):
        """pack sd:7695: a list-valued category went to Other, as its joined text names no category. A row files under
        the first of its values the view's set names, in the set's spelling, compared case-insensitively; else Other ("")."""
        seed_learning(self.connection)
        made = {title: learning(self.connection, title, "queued", **{"content-type": "book", "category": value})
                for title, value in (("Listed", ["Computers", "communication", "Learning"]), ("Lower", "teams & facilitation"),
                                     ("Off", ["Computers", "Math"]), ("Odd", [None, 3, " Learning "]))}
        rows = {r["id"]: r for r in learning_screen.books(self.connection, now=NOW)["rows"]}
        self.assertEqual({title: rows[made[title]]["filed"] for title in made},
                         {"Listed": "Communication", "Lower": "Teams & Facilitation", "Off": "", "Odd": "Learning"})
        self.assertEqual(rows[made["Listed"]]["category"], "Computers, communication, Learning", "Details keeps every value")

    def test_the_route_reads_the_books(self):
        seed_learning(self.connection)
        doc = v2.pages.documents._books(types.SimpleNamespace(connection=self.connection, now=NOW))
        self.assertEqual(doc["read"], NOW)
        self.assertEqual(sorted(r["title"] for r in doc["rows"]), ["A Bare Book", "A Course", "Agile Retrospectives", "On Queues"])


class TheVideos(ScreenCase):
    """sd:6261: `/api/documents/videos` lists the learning rows that are videos, by content-type or by the
    "YYYY-MM-DD - YouTube - <channel> - <title>" name, each with its title, channel, date, URL and watched state."""

    def test_a_video_row_says_its_title_channel_date_url_and_watched_state(self):
        ids = seed_learning(self.connection)
        rows = {r["id"]: r for r in learning_screen.videos(self.connection, now=NOW)["rows"]}
        self.assertEqual(sorted(rows), sorted([ids["video"], ids["named"]]), "a book or a course is no video")
        video = rows[ids["video"]]
        self.assertEqual((video["title"], video["channel"], video["date"], video["url"], video["host"], video["stage"], video["state"],
                          video["duration"]),
                         ("A Talk", "Example Channel", "2016-06-06", "https://www.youtube.com/watch?v=x", "www.youtube.com", "in-progress",
                          "watching", "534"))
        # A row with no content-type or channel is a video by its name, which gives the channel, the date and the title.
        named = rows[ids["named"]]
        self.assertEqual((named["title"], named["channel"], named["date"], named["state"]), ("Queues - A Primer", "Other Channel", "2024-02-03", "to watch"))

    def test_the_route_reads_the_videos_and_writes_nothing(self):
        ids = seed_learning(self.connection)
        before = self.connection.execute("SELECT id, kind, status, stage FROM item ORDER BY id").fetchall()
        doc = v2.pages.documents._videos(types.SimpleNamespace(connection=self.connection, now=NOW))
        self.assertEqual(doc["read"], NOW)
        self.assertEqual(sorted(r["id"] for r in doc["rows"]), sorted([ids["video"], ids["named"]]))
        self.assertEqual(self.connection.execute("SELECT id, kind, status, stage FROM item ORDER BY id").fetchall(), before)


class TheMovedRows(ScreenCase):
    """pack sd:6262: a row `sd-db records documents` moved is in its own view and no other, whatever its content type."""

    def test_a_moved_row_shows_only_in_its_own_view(self):
        ids = seed_learning(self.connection)
        bare = learning(self.connection, "A Listed Note", "queued")
        listed = [bare, ids["video"]]
        plan = records.plan_documents(self.connection, books=listed)
        records.move_documents(self.connection, books=listed, plan=plan.digest, who="operator")
        books = {r["id"] for r in learning_screen.books(self.connection, now=NOW)["rows"]}
        videos = {r["id"] for r in learning_screen.videos(self.connection, now=NOW)["rows"]}
        # The note with no content type shows in Books; the listed video moved to Books and leaves Videos.
        self.assertEqual(books, {ids["book"], ids["article"], ids["bare"], ids["course"], bare, ids["video"]})
        self.assertEqual(videos, {ids["named"]})


class TheLearningScript(ScreenCase):
    """sd:6260: learning.js draws the Books view beside the Documents list, in the shared Details pane."""

    def run_view(self, body, docs):
        """documents.js and learning.js with the shell's view switch; /api/documents answers with no roots, each view's api with `docs`."""
        answer = ("(p) => p === '/api/documents' ? [200, { read: '2026-09-06T12:00:00Z', roots: [], documents: [], store: { available: true } }]"
                  f" : ({json.dumps(docs)})[p] || [404, {{ error: 'no ' + p }}]")
        script = (STAND_IN + "location.search = ''; location.origin = 'http://dash.example.test';\nvar ROW = null;\n"
                  + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + VIEW_SHELL + "\n" + READ_SHELL
                  + f"\nANSWER = {answer};\n" + COMMANDS_JS + DOCUMENTS_JS + LEARNING_JS
                  + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def books(self):
        seed_learning(self.connection)
        return {"/api/documents/books": [200, learning_screen.books(self.connection, now=NOW)]}

    def test_books_is_a_view_that_reads_when_it_first_shows(self):
        out = self.run_view("""R.views = window.PAGE_VIEWS.map(v => [v.id, v.label, v.api || null]); R.before = OUT.gets.slice();
R.details0 = ELS.details.html; window.shell.view.show('books'); await flush();
R.rows = ELS['books-rows'].html; R.head = ELS['books-head'].html; R.filters = ELS['books-filters'].html; R.details = ELS.details.html;
R.facets = ELS.facets.hidden; R.tally = ELS['books-tally'].html;""", self.books())
        r = out["R"]
        self.assertEqual(r["views"], [["documents", "Documents", None], ["books", "Books and courses", "/api/documents/books"],
                                      ["videos", "Videos", "/api/documents/videos"]])
        self.assertEqual(r["before"], ["/api/documents"], "Books reads only when it first shows")
        self.assertEqual(out["gets"], ["/api/documents", "/api/documents/books"])
        rows = r["rows"]
        # Title, author or source, URL and read state, each row.
        self.assertIn("<button type=\"button\">Agile Retrospectives</button><span class=\"file\">Making Good Teams Great</span>", rows)
        self.assertIn(">A. Writer, B. Writer</td>", rows)
        self.assertIn('<a class="ext" href="https://books.example.test/retro" target="_blank" rel="noopener noreferrer">books.example.test</a>', rows)
        self.assertIn('<td class="kind">reading</td>', rows)
        self.assertIn('<td class="kind">to read</td>', rows)
        self.assertIn(">Example Weekly</td>", rows)
        self.assertNotIn('href="javascript', rows)
        self.assertIn('<td class="mod">javascript:alert(1)</td>', rows)
        self.assertNotIn("A Talk", rows)
        self.assertIn('data-sort="by"', r["head"])
        self.assertIn("Read state", r["head"])
        self.assertIn('data-f="state" data-v="in-progress"', r["filters"])
        self.assertEqual(r["tally"], "4 titles")
        self.assertEqual(out["counts"]["books"], 4)
        # The first row by title shows in the one Details pane, with its facts and item link.
        self.assertIn("<h2>A Bare Book</h2>", r["details"])
        self.assertIn("<dt>Item</dt><dd><a href=\"/tasks?row=", r["details"])
        # The facets are the Documents list's: they hide while Books shows.
        self.assertTrue(r["facets"])

    def test_a_kind_tile_leads_to_the_courses(self):
        """sd:10634: courses were in no view; the Books and courses view lists them, and its Kind tile narrows to them."""
        out = self.run_view("""R.label = window.PAGE_VIEWS.find(v => v.id === 'books').label;
window.shell.view.show('books'); await flush(); R.filters = ELS['books-filters'].html;
ELS['books-filters'].listeners.click[0]({ target: { closest: s => s === '[data-f="kind"]' ? { dataset: { f: 'kind', v: 'course' } } : null } });
R.rows = ELS['books-rows'].html;""", self.books())
        r = out["R"]
        self.assertEqual(r["label"], "Books and courses")
        self.assertIn('role="group" aria-label="Kind"', r["filters"])
        self.assertIn('data-f="kind" data-v="course" aria-pressed="false" title="course: 1"', r["filters"])
        self.assertIn("A Course", r["rows"])
        self.assertNotIn("Agile Retrospectives", r["rows"])

    def test_the_state_filter_and_the_search_fields_words_narrow_the_books(self):
        """The page's one search field filters the shown view by its plain words; repo: and kind: are the list's tokens."""
        out = self.run_view("""window.shell.view.show('books'); await flush();
ELS['books-filters'].listeners.click[0]({ target: { closest: s => s === '[data-f="state"]' ? { dataset: { f: 'state', v: 'queued' } } : null } });
R.queued = ELS['books-rows'].html;
ELS['books-filters'].listeners.click[0]({ target: { closest: s => s === '[data-fclear]' ? {} : null } });
ELS.shift.value = 'writer kind:research'; ELS.shift.listeners.input.forEach(f => f({ target: ELS.shift })); await flush(); R.text = ELS['books-rows'].html;""", self.books())
        r = out["R"]
        self.assertIn("On Queues", r["queued"])
        self.assertNotIn("Agile Retrospectives", r["queued"])
        self.assertIn("Agile Retrospectives", r["text"])
        self.assertNotIn("On Queues", r["text"])

    def test_a_category_tile_per_category_and_other_narrows_the_view(self):
        """pack sd:7669: one Category tile per category of the view's set, then Other for a row off the set or with none;
        a tile narrows the rows to it, and Details names the row's category."""
        out = self.run_view("""window.shell.view.show('books'); await flush();
R.filters = ELS['books-filters'].html;
const press = v => ELS['books-filters'].listeners.click[0]({ target: { closest: s => s === '[data-f="category"]' ? { dataset: { f: 'category', v } } : null } });
press('Teams & Facilitation'); R.teams = ELS['books-rows'].html;
window.PAGE_LIST.select([...OBJ.values()].find(o => o.type === 'book' && o.title === 'Agile Retrospectives').id); R.details = ELS.details.html;
press('Teams & Facilitation'); press('other'); R.other = ELS['books-rows'].html;""", self.books())
        r = out["R"]
        tiles = re.findall(r'data-f="category" data-v="([^"]*)"', r["filters"])
        self.assertEqual([html.unescape(t) for t in tiles], [*learning_screen.CATEGORIES["books"], "other"])
        self.assertIn('title="Other: 3"', r["filters"])
        self.assertIn("Agile Retrospectives", r["teams"])
        self.assertNotIn("On Queues", r["teams"])
        self.assertIn("<dt>Category</dt><dd>Teams &amp; Facilitation</dd>", r["details"])
        self.assertIn("On Queues", r["other"])
        self.assertIn("A Bare Book", r["other"])
        self.assertNotIn("Agile Retrospectives", r["other"])

    def test_a_listed_category_counts_under_its_tile_not_other(self):
        """pack sd:7695: a row whose category is a list, one value in the set, counts under that value's tile; the page
        files by the read's `filed`, not by the joined text."""
        learning(self.connection, "Listed Book", "queued", **{"content-type": "book", "category": ["Computers", "communication"]})
        out = self.run_view("""window.shell.view.show('books'); await flush();
R.filters = ELS['books-filters'].html;
ELS['books-filters'].listeners.click[0]({ target: { closest: s => s === '[data-f="category"]' ? { dataset: { f: 'category', v: 'Communication' } } : null } });
R.rows = ELS['books-rows'].html;""", self.books())
        r = out["R"]
        self.assertIn('title="Communication: 1"', r["filters"])
        self.assertIn('title="Other: 3"', r["filters"])
        self.assertIn("Listed Book", r["rows"])
        self.assertNotIn("On Queues", r["rows"])

    def test_clear_filters_in_the_empty_view_clears_the_search_too(self):
        """sd:6285: the empty state's Clear filters cleared the read state only, so a search that matched nothing stayed."""
        out = self.run_view("""window.shell.view.show('books'); await flush();
ELS.shift.value = 'nothing-matches-this'; ELS.shift.listeners.input.forEach(f => f({ target: ELS.shift })); await flush();
R.before = ELS['books-rows'].html;
ELS['books-empty'].listeners.click[0]({ target: { closest: s => s === '[data-fclear]' ? {} : null } }); await flush();
R.after = ELS['books-rows'].html; R.shift = ELS.shift.value;""", self.books())
        r = out["R"]
        self.assertNotIn("Agile Retrospectives", r["before"])
        self.assertIn("Agile Retrospectives", r["after"])
        self.assertEqual(r["shift"], "", "the search field kept its text")

    def test_open_link_and_open_item_run_on_a_book(self):
        out = self.run_view("""window.shell.view.show('books'); await flush();
const book = [...OBJ.values()].find(o => o.type === 'book' && o.title === 'Agile Retrospectives');
const article = [...OBJ.values()].find(o => o.type === 'book' && o.title === 'On Queues');
R.reg = REG.filter(c => c.on === 'book').map(c => [c.id, c.label, c.key, c.risk]);
C.run(cmd('book.open'), book); C.run(cmd('book.open'), article); R.cli = cmd('book.item').cli(book);""", self.books())
        self.assertEqual(out["R"]["reg"], [["book.open", "Open link", "o", "safe"], ["book.item", "Open item", "i", "safe"]])
        self.assertEqual(out["opened"], [["https://books.example.test/retro", "_blank", "noopener"]])
        self.assertIn(["off: no http or https URL on this row", False], out["toasts"])
        self.assertRegex(out["R"]["cli"], r"^sd task show \d+$")

    def test_add_book_and_add_video_post_the_url_typed_and_read_the_view_again(self):
        """pack sd:7081: each view's head carries its Add, which posts learning.add with the view and reads it again."""
        docs = {**self.books(), "/api/documents/videos": [200, learning_screen.videos(self.connection, now=NOW)],
                "/api/actions/run": [200, {"action": "learning.add", "label": "Add", "line": "sd store add", "exit": 0, "output": ""}]}
        out = self.run_view("""window.shell.view.show('books'); await flush();
FIELD_VALUES = { url: 'https://press.example.test/new', title: '' }; C.run(cmd('book.add'), OBJ.get('books')); await flush();
window.shell.view.show('videos'); await flush();
FIELD_VALUES = { url: 'https://youtu.be/abc', title: 'A Talk' }; C.run(cmd('video.add'), OBJ.get('videos')); await flush();
R.reg = REG.filter(c => c.id.endsWith('.add')).map(c => [c.id, c.label, c.on]);""", docs)
        self.assertEqual(out["R"]["reg"], [["book.add", "Add book", "book list"], ["video.add", "Add video", "video list"]])
        self.assertEqual([p[:2] for p in out["posts"]], [
            ["/api/actions/run", {"action": "learning.add", "view": "books", "url": "https://press.example.test/new", "title": ""}],
            ["/api/actions/run", {"action": "learning.add", "view": "videos", "url": "https://youtu.be/abc", "title": "A Talk"}]])
        self.assertEqual(out["gets"].count("/api/documents/books"), 2, out["gets"])
        page = (V2 / "documents.html").read_text(encoding="utf-8")
        for view, noun in (("books", "book"), ("videos", "video")):
            head = page[page.index(f'<section id="view-{view}"'):]
            self.assertIn(f'<div class="tools"><button class="btn quiet" type="button" data-cmd="{noun}.add" data-obj="{view}">Add {noun}…</button></div>',
                          head[:head.index("<table")])

    def test_videos_is_a_view_with_channel_date_and_watched_state_newest_first(self):
        seed_learning(self.connection)
        docs = {"/api/documents/videos": [200, learning_screen.videos(self.connection, now=NOW)]}
        out = self.run_view("""window.shell.view.show('videos'); await flush();
R.rows = ELS['videos-rows'].html; R.head = ELS['videos-head'].html; R.details = ELS.details.html; R.tally = ELS['videos-tally'].html;
const v = [...OBJ.values()].find(o => o.type === 'video' && o.title === 'A Talk');
R.reg = REG.filter(c => c.on === 'video').map(c => c.id); C.run(cmd('video.open'), v);""", docs)
        r = out["R"]
        self.assertEqual(out["gets"], ["/api/documents", "/api/documents/videos"])
        self.assertEqual(r["tally"], "2 videos")
        self.assertEqual(out["counts"]["videos"], 2)
        # Title, channel, date, URL and watched state; the newest date first.
        self.assertLess(r["rows"].index("Queues - A Primer"), r["rows"].index("A Talk"))
        for cell in ('>Example Channel</td>', '<td class="num">2016-06-06</td>', '<td class="kind">watching</td>', '<td class="kind">to watch</td>',
                     '<a class="ext" href="https://www.youtube.com/watch?v=x" target="_blank" rel="noopener noreferrer">www.youtube.com</a>'):
            self.assertIn(cell, r["rows"])
        self.assertIn("Watched state", r["head"])
        self.assertIn('data-sort="date"', r["head"])
        self.assertIn("<h2>Queues - A Primer</h2>", r["details"])
        self.assertEqual(r["reg"], ["video.open", "video.item"])
        self.assertEqual(out["opened"], [["https://www.youtube.com/watch?v=x", "_blank", "noopener"]])

    def test_a_videos_duration_reads_as_minutes(self):
        seed_learning(self.connection)
        docs = {"/api/documents/videos": [200, learning_screen.videos(self.connection, now=NOW)]}
        out = self.run_view("""window.shell.view.show('videos'); await flush();
const v = [...OBJ.values()].find(o => o.title === 'A Talk'); ELS['videos-rows'].listeners.click[0]({ target: { closest: s => s === 'tr[data-id]' ? { dataset: { id: v.id } } : null } });
R.details = ELS.details.html;""", docs)
        self.assertIn("<h2>A Talk</h2>", out["R"]["details"])
        self.assertIn("<dt>Duration</dt><dd>8:54</dd>", out["R"]["details"])

    def test_read_again_reads_the_shown_view_so_a_failed_first_read_recovers(self):
        """sd:6280: Read again on Books reads the books again, not only the Documents list."""
        out = self.run_view("""const A = ANSWER; let n = 0;
ANSWER = (p, b) => p === '/api/documents/books' && !n++ ? [500, { error: 'database is locked' }] : A(p, b);
window.shell.view.show('books'); await flush(); R.failed = ELS['books-rows'].html;
await window.PAGE_AGAIN.run(); await flush(); R.rows = ELS['books-rows'].html;
window.shell.view.show('documents'); await flush(); await window.PAGE_AGAIN.run(); await flush();""", self.books())
        self.assertNotIn("Agile Retrospectives", out["R"]["failed"])
        self.assertIn("Agile Retrospectives", out["R"]["rows"])
        self.assertEqual(out["gets"], ["/api/documents", "/api/documents/books", "/api/documents/books", "/api/documents"])

    def test_a_failed_read_says_so_and_the_documents_list_keeps_its_details(self):
        out = self.run_view("""window.shell.view.show('books'); await flush(); R.details = ELS.details.html;""",
                            {"/api/documents/books": [500, {"error": "no such table: item"}]})
        self.assertIn({"kind": "error", "text": "The books, articles and courses were not read: no such table: item. Reload retries it.", "source": "/api/documents/books"},
                      out["states"])
        self.assertIn("The books, articles and courses were not read, so nothing is selected.", out["R"]["details"])


class TheAddActions(Run):
    """Add book and Add video (pack sd:7081): a URL becomes a to-read or to-watch learning row through `sd store add`."""

    def setUp(self):
        super().setUp()
        self.titles = []
        title = mock.patch.object(source_intake, "_title", lambda url, deadline: self.titles.append(url) or "Fetched Title - YouTube")
        title.start()
        self.addCleanup(title.stop)
        self.ids = seed_learning(self.connection)
        self.connection.commit()

    def test_add_book_files_a_book_row_with_the_typed_title(self):
        status, body = self.run_("learning.add", view="books", url="https://press.example.test/new", title="A New Book")
        self.assertEqual((status, body["exit"], body["label"]), (200, 0, "Add"), body)
        [line] = self.ran()
        self.assertTrue(line.startswith("sd store add sdw.learning A New Book --field content-type=book "
                                        "--field url=https://press.example.test/new --field dateCreated="), line)
        self.assertEqual(self.titles, [], "a typed title fetched the page")

    def test_add_video_reads_the_pages_title_and_names_a_youtube_link_youtube(self):
        status, body = self.run_("learning.add", view="videos", url="https://youtu.be/abc", title="")
        self.assertEqual((status, body["exit"]), (200, 0), body)
        self.assertTrue(self.ran()[-1].startswith("sd store add sdw.learning Fetched Title --field content-type=youtube "), self.ran())
        self.run_("learning.add", view="videos", url="https://media.example.test/talk", title="A Talk")
        self.assertIn("--field content-type=video ", self.ran()[-1])

    def test_add_refuses_a_bad_url_a_known_url_or_another_view(self):
        before = self.ran()
        for ids, said in (({"view": "books", "url": "javascript:alert(1)", "title": ""}, "http or https URL"),
                          ({"view": "books", "url": "https://example.test/a b", "title": ""}, "no spaces"),
                          ({"view": "books", "url": "https://books.example.test/retro", "title": ""}, "already in Learning"),
                          ({"view": "courses", "url": "https://example.test", "title": ""}, "books or videos"),
                          ({"view": "books", "url": "https://example.test", "title": "a/b"}, "Name the"),
                          ({"view": "books", "url": "https://example.test"}, "takes title, url, view")):
            with self.subTest(ids=ids):
                status, _, body = self.post("/api/actions/run", {"action": "learning.add", **ids})
                self.assertEqual(status, 400, body)
                self.assertIn(said, body["error"])
        self.assertEqual(self.ran(), before)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "documents", "Documents", "/documents", ("/api/documents", "/api/documents/books", "/api/documents/videos", "/api/documents/view", "/api/documents/publish", "/api/documents/unpublish",
                                                                         "/api/documents/publish-now", "/api/documents/publish-now/drain-x-20261018T000000Z")


if __name__ == "__main__":
    unittest.main()
