"""The v2 page registry (sd:2418).

What this slice promises: each v2 page registers itself from its own module in
`sd_dashboard/v2/pages/` (its section, routes, HTML file, API routes, old
screens), and everything shared is built from that enumeration: `PAGES`,
`SECTIONS`, `CLASSIC`, `SCREENS`, `/ui/sections.js`, the server's API
dispatch with its session, query and CSRF rules, and the old screens a page
takes the path of. This test names no page: it enumerates the registry.
"""

from __future__ import annotations

import json
import os
import pkgutil
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sd_dashboard import v2
from test_workflow_actions import BrowserSession

HERE = Path(__file__).resolve().parents[1]
V2 = Path(v2.__file__).resolve().parent
SHELL_JS = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
#: The rail's section names, from the design source's GROUPS in shell.js: the one place the rail's order lives.
GROUP_BLOCK = re.search(r"^  const GROUPS = \[\n(.*?)^  \];$", SHELL_JS, re.S | re.M).group(1)
GROUPS = re.findall(r"\['([A-Za-z ]+)', '[a-z0-9-]+', '[a-z]'\]", GROUP_BLOCK)
#: The pages without the shell's Read again (sd:5638), each with its reason.
AGAIN_ALLOWED = {
    "settings": "forms, not tables: a read again would drop a list typed and not saved",
}


def registry():
    from sd_dashboard.v2 import pages

    return pages


def api_paths(page):
    """Each API route's request path: its path, or the example its pattern carries."""
    return [entry.path or entry.example for entry in page.api]


class Registers:
    """A page test's own registration check: its module registers the section, address and API paths the test names."""

    page = section = route = ""
    api: tuple[str, ...] = ()

    def test_the_page_registers_itself(self):
        found = {page.name: page for page in registry().PAGES}[self.page]
        self.assertEqual((found.section, found.routes[0], tuple(api_paths(found))), (self.section, self.route, self.api))
        self.assertEqual(v2.SECTIONS[self.section], self.route)
        self.assertNotIn(self.section, v2.CLASSIC)


class TheRegistry(unittest.TestCase):
    def test_the_groups_are_read_from_the_shell(self):
        self.assertIn("Today", GROUPS)
        self.assertEqual(len(GROUPS), len(set(GROUPS)))

    def test_each_page_module_registers_one_page(self):
        pages = registry()
        modules = sorted(name for _, name, _ in pkgutil.iter_modules(pages.__path__))
        self.assertEqual(sorted(page.name for page in pages.PAGES), modules)
        self.assertTrue(modules)

    def test_a_page_module_without_a_page_fails_the_enumeration(self):
        pages = registry()
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "example_pages"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "lost.py").write_text("NOT_A_PAGE = 1\n")
            sys.path.insert(0, directory)
            try:
                with self.assertRaisesRegex(ImportError, "lost"):
                    pages.collect([str(package)], "example_pages")
            finally:
                sys.path.remove(directory)

    def test_no_route_section_or_api_path_is_claimed_twice(self):
        pages = registry().PAGES
        routes = [route for page in pages for route in page.routes]
        self.assertEqual(len(routes), len(set(routes)), routes)
        sections = [page.section for page in pages]
        self.assertEqual(len(sections), len(set(sections)), sections)
        paths = [path for page in pages for path in api_paths(page)]
        self.assertEqual(len(paths), len(set(paths)), paths)
        old = [entry.path for page in pages for entry in page.takes]
        self.assertEqual(len(old), len(set(old)), old)
        self.assertEqual(set(old) & set(routes), set())

    def test_every_section_is_a_rail_section(self):
        for page in registry().PAGES:
            if page.section == "Places":
                # sd:5663: the realm place page is a rail entry per realm place, which shell.js adds to the Places group.
                self.assertIn("GROUPS.find(([g]) => g === 'Places')[1].splice(", SHELL_JS)
                continue
            self.assertIn(page.section, GROUPS, f"{page.name} names {page.section!r}, which GROUPS in shell.js does not hold")

    def test_every_html_file_exists_and_each_route_serves_it(self):
        for page in registry().PAGES:
            self.assertTrue((V2 / page.html).is_file(), page.html)
            for route in page.routes:
                self.assertEqual(v2.PAGES[route], page.html)
        self.assertEqual(set(v2.PAGES), {route for page in registry().PAGES for route in page.routes})

    def test_every_page_has_its_page_document(self):
        """sd:3527 (review 2026-10-12): Queue, Management, Review and Settings shipped with no `docs/pages/<page>.md`."""
        missing = sorted(page.html for page in registry().PAGES
                         if not (HERE / "docs" / "pages" / page.html.replace(".html", ".md")).is_file())
        self.assertEqual(missing, [], "add docs/pages/<page>.md: the README promises one file per page")

    def test_a_pattern_route_carries_an_example_it_matches(self):
        for page in registry().PAGES:
            for entry in page.api:
                self.assertEqual(bool(entry.path), not entry.pattern, (page.name, entry))
                self.assertEqual(bool(entry.read), not entry.write, (page.name, entry))
                if entry.pattern:
                    self.assertRegex(entry.example, f"^(?:{entry.pattern})$", (page.name, entry))
                found = v2.api(entry.path or entry.example) if entry.read else v2.action(entry.path or entry.example)
                self.assertIs(found[1] if entry.read else found, entry)

    def test_the_maps_and_sections_js_come_from_the_registry(self):
        pages = registry().PAGES
        self.assertEqual(v2.SECTIONS, {page.section: page.routes[0] for page in pages})
        self.assertEqual(set(v2.SECTIONS) & set(v2.CLASSIC), set(), "a registered section is still classic")
        for page in pages:
            for label, href in page.classic.items():
                self.assertEqual(v2.SCREENS[label], href)
        body = v2.sections().decode()
        declared = dict(re.findall(r"^window\.(SHELL_\w+) = (.*);$", body, re.M))
        self.assertEqual({key: json.loads(value) for key, value in declared.items()},
                         {"SHELL_PAGES": v2.SECTIONS, "SHELL_CLASSIC": v2.CLASSIC, "SHELL_SCREENS": v2.SCREENS,
                          "SHELL_COUNTS": {"pages": {}}, "SHELL_REALMS": []})

    def test_every_page_declares_read_again_or_says_why_not(self):
        """sd:5638: every page's tables read again from one head button, the shell's (again:start in shell.js)."""
        missing, declared, twice = [], set(), []
        for page in registry().PAGES:
            html = (V2 / page.html).read_text(encoding="utf-8")
            scripts = re.findall(r'<script src="/ui/([\w-]+\.js)"></script>', html)
            if any(re.search(r"(?m)^\s*window\.PAGE_AGAIN = \{", (V2 / "static" / s).read_text(encoding="utf-8"))
                   for s in scripts if s != "shell.js" and (V2 / "static" / s).is_file()):  # sections.js is served, not a file
                declared.add(page.name)
            elif page.name not in AGAIN_ALLOWED:
                missing.append(page.name)
            if 'id="reload"' in html:
                twice.append(page.name)
        self.assertEqual(missing, [], "set window.PAGE_AGAIN = { title, run } in the page's script, or name it in AGAIN_ALLOWED")
        self.assertEqual(twice, [], "the rail's whole-page reload is #reload: a page button with that id is a second one")
        self.assertEqual(declared & set(AGAIN_ALLOWED), set(), "a page in AGAIN_ALLOWED declares PAGE_AGAIN: drop its entry")
        self.assertLessEqual(set(AGAIN_ALLOWED), {page.name for page in registry().PAGES}, "an AGAIN_ALLOWED entry names no page")

    def test_no_page_draws_its_own_read_again(self):
        """sd:5750: every Read again is the shell's (again:start), one label and icon; Home drew its own (#refresh)."""
        drawn = []
        for page in registry().PAGES:
            html = (V2 / page.html).read_text(encoding="utf-8")
            scripts = [s for s in re.findall(r'<script src="/ui/([\w-]+\.js)"></script>', html)
                       if s != "shell.js" and (V2 / "static" / s).is_file()]
            for name, text in [(page.html, html)] + [(s, (V2 / "static" / s).read_text(encoding="utf-8")) for s in scripts]:
                if re.search(r"<button\b(?:(?!</button>).)*Read again", text, re.S):
                    drawn.append(f"{page.name}: {name}")
        self.assertEqual(drawn, [], "set window.PAGE_AGAIN and let the shell draw the button")

    def test_each_page_names_its_item(self):
        for page in registry().PAGES:
            self.assertRegex(page.item, r"^sd:[1-9][0-9]*$", page.name)

    def test_dashboard_sh_pages_prints_every_section_from_the_registry(self):
        result = subprocess.run(["sh", str(HERE / "dashboard.sh"), "pages"], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "PYTHONPATH": str(HERE)}, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        for page in registry().PAGES:
            self.assertTrue(any(line.split("\t")[:3] == [page.section, page.routes[0], "new"] for line in lines), page.section)
        for section, href in v2.CLASSIC.items():
            self.assertIn(f"{section}\t{href}\tclassic", lines)
        for label, href in v2.SCREENS.items():
            self.assertIn(f"{label}\t{href}\tpalette", lines)


class TheRoutes(BrowserSession):
    def test_every_api_route_refuses_a_request_without_a_session(self):
        for page in registry().PAGES:
            for entry in page.api:
                path = entry.path or entry.example
                if entry.read:
                    status, _, body = self.request(path)
                    self.assertEqual(status, 403, path)
                    self.assertEqual(json.loads(body), {"error": f"Reload the page to read {page.section}: its session ended, as it does after an hour or when the dashboard restarts."}, path)

    def test_every_api_route_that_takes_no_query_refuses_one(self):
        for page in registry().PAGES:
            for entry in page.api:
                if entry.read and not entry.query:
                    path = (entry.path or entry.example) + "?example=1"
                    status, _, body = self.request(path, headers={"Cookie": self.cookie})
                    self.assertEqual(status, 400, path)
                    self.assertEqual(json.loads(body), {"error": f"{page.section} does not accept query parameters."}, path)

    def test_every_write_refuses_a_request_without_its_csrf_token(self):
        for page in registry().PAGES:
            for entry in page.api:
                if entry.write:
                    status, _, body = self.post(entry.path or entry.example, {}, **{"X-SD-CSRF": ""})
                    self.assertEqual(status, 403, entry)
                    self.assertIn("Open the dashboard again before saving", body["error"])

    def test_every_page_route_serves_its_page_and_each_old_screen_it_took_answers(self):
        for page in registry().PAGES:
            for route in page.routes:
                status, _, body = self.request(route)
                self.assertEqual(status, 200, route)
                self.assertIn(" · system</title>", body, route)
            for entry in page.takes:
                status, _, body = self.request(entry.path)
                self.assertEqual(status, 200, entry.path)
                # The old screen is server-drawn inside the v2 shell (sd:3586), not the page's template.
                self.assertIn('<main id="main" class="classic">', body, entry.path)
                self.assertIn(entry.path, page.classic.values(), "an old screen the page took is not in the palette")


if __name__ == "__main__":
    unittest.main()
