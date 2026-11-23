"""The research kit's entrypoint: its verb surface and its two invariants.

`.coveragerc` keeps the non-installer `bin/` tools out of the 100% gate because
their interesting branches depend on optional local CLIs, and covers them with
focused tests instead. This is that file for `sd-research-kit`.

Two things are worth pinning rather than the whole surface. The kit took
`render [repo_dir]` and `checklinks [repo_dir ...]` before it moved into this
pack, and R10-D6 says a command resolves its repository from the current working
directory and nowhere else -- `test_verb_inventory` cannot see that, because it
reads `add_argument` calls and this tool parses its own argv. And the renderer's
stylesheet is inlined as Python because `bin/` holds Python only, so the thing
that would silently regress is the CSS going missing from the rendered page.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

from tests.clean_env import clean_environment

REPO_ROOT = Path(__file__).resolve().parents[1]
KIT = REPO_ROOT / "bin" / "sd-research-kit"
VERBS = (
    "render",
    "publish",
    "checklinks",
    "review",
    "pins",
    "fleet-pins",
    "conventions",
    "init-hook",
    "init-claude-md",
    "deck",
)
TEMPLATE = REPO_ROOT / "skills" / "sd-research-repo" / "templates" / "CLAUDE.md"
#: The template as the five research repos copied it, before it shrank from
#: 193 lines on 2026-09-24. Kept as `.txt` so no doc gate reads its retired prose.
LONG_TEMPLATE = REPO_ROOT / "tests" / "fixtures" / "research-claude-md-before-2026-09-24.txt"


def load_kit():
    """Load the entrypoint, which has no `.py` suffix to infer a loader from.

    `spec_from_file_location` returns None for an extensionless path, so the
    loader is named explicitly. This is the same shape `bin/` itself uses to
    reach its sibling modules.
    """

    name = "sd_research_kit_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(KIT))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def load_publish():
    """Load `sd_research_publish` in process, for the two hook constants.

    `bin/` is the script directory when the kit itself runs, which is how that
    module reaches `sd_lib`; loaded from a test there is no such directory on
    the path, so it is put there once. `load_kit` on its own is enough for a
    module with no `bin/` sibling to import.
    """

    here = str(REPO_ROOT / "bin")
    if here not in sys.path:
        sys.path.insert(0, here)
    return load_kit().load("sd_research_publish")


def run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(KIT), *args],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
    )


class VerbSurfaceTests(unittest.TestCase):
    def test_help_exits_zero_and_names_every_verb(self) -> None:
        result = run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for verb in VERBS:
            self.assertIn(verb, result.stdout, f"{verb} missing from usage")

    def test_an_unknown_verb_fails(self) -> None:
        # Not a plausible verb name: `publish` stood here until it became one,
        # and the test then asserted that a real verb was rejected.
        result = run("transmogrify")
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage:", result.stderr)

    def test_no_verb_fails(self) -> None:
        self.assertEqual(run().returncode, 1)


class ReviewDocstringTests(unittest.TestCase):
    """sd:1000 (eb35682afbb4): the module docstring left out two of `review`'s checks.

    The checks are read off `check()`'s own calls, so a check added there fails
    here until the docstring names it.
    """

    #: The words the docstring uses for each check `check()` calls.
    NAMED = {
        "provenance": "provenance block",
        "status_section": "Status section",
        "main_document": "`START HERE — ` title",
        "template_drift": "`CLAUDE.md` has not drifted",
        "work_items": "work items under `docs/work/`",
        "map_ledgers": "stable row IDs and a gaps section",
        "architecture_overview": "architecture overview under `05-architecture/`",
    }

    def test_the_docstring_names_every_check_review_runs(self) -> None:
        import ast
        import inspect

        load_publish()  # puts `bin/` on the path, so the class runs on its own
        module = load_kit().load("sd_research_review")
        tree = ast.parse(inspect.getsource(module.check))
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and inspect.isfunction(getattr(module, node.func.id, None))}
        self.assertEqual(set(self.NAMED), called - {"load_docs"})
        text = " ".join(module.__doc__.split())
        for check, words in self.NAMED.items():
            self.assertIn(words, text, check)


class SecondReaderCommandTests(unittest.TestCase):
    """Step 9 of the printed checklist routes the second reader through `sd-review`.

    #1194. It used to print a fixed `codex exec` line, which skipped the
    provider registry's reviewer order and blocked the pass whenever Codex was
    down. `sd-review` takes the reviewer from the chain, with consent, vendor
    independence and fallback, and `--lens research-brief` carries the research
    framing to whichever provider the chain picks. The stdin, hang and
    read-only guards the old command needed are the lane's own now.
    """

    def setUp(self) -> None:
        load_publish()  # puts `bin/` on the path, so the class runs on its own
        self.checklist = load_kit().load("sd_research_review").CHECKLIST

    def test_the_second_reader_is_sd_review_with_the_research_lens(self) -> None:
        for scope in ("worktree", "branch"):
            self.assertRegex(self.checklist, rf"sd-review --scope {scope} --lens research-brief\b")
        self.assertNotIn("codex exec", self.checklist)

    def test_the_checklist_names_no_registry_provider(self) -> None:
        # Enumerated from the shipped registry, so a provider added there is
        # checked here without anyone remembering to list it.
        registry = (REPO_ROOT / "providers.yaml").read_text(encoding="utf-8")
        block = registry.split("\nproviders:\n", 1)[1].split("\nroles:\n", 1)[0]
        names = re.findall(r"^  ([a-z][a-z0-9_-]*):", block, flags=re.MULTILINE)
        self.assertIn("codex", names)
        # A file name is not a provider: `CLAUDE.local.md` holds the consent.
        named = [name for name in names
                 if re.search(rf"\b{name}\b(?!(?:\.\w+)*\.md\b)", self.checklist, flags=re.IGNORECASE)]
        self.assertEqual(named, [])

    def test_the_lens_the_checklist_names_is_one_sd_review_takes(self) -> None:
        import sd_lib

        self.assertIn("research-brief", sd_lib.REVIEW_LENSES)

    def test_the_registry_independent_fallback_is_stated_as_such(self) -> None:
        text = " ".join(self.checklist.split())
        self.assertIn("Only if `sd-review` cannot run at all", text)
        self.assertIn("does not use the registry", text)
        self.assertIn("adversarial-gate render --lens research-brief", text)


class RepositoryFromCwdTests(unittest.TestCase):
    """R10-D6, asserted on the surface that actually parses the argument."""

    def test_no_verb_accepts_a_path(self) -> None:
        offenders = []
        for verb in VERBS:
            result = run(verb, ".")
            if result.returncode == 0 or "takes no arguments" not in result.stderr:
                offenders.append(verb)
        self.assertEqual(
            offenders,
            [],
            "R10-D6: these verbs accepted a path argument, so the command can be "
            "pointed at a checkout the caller is not standing in",
        )

    def test_the_usage_says_so(self) -> None:
        self.assertIn("There is no repo argument", run("--help").stdout)


class ConventionsTests(unittest.TestCase):
    def test_conventions_prints_a_path_that_exists(self) -> None:
        """The standard is a shipped skill reference; a moved file breaks silently.

        `conventions` is how every research repo's own CLAUDE.md is told where the
        standard lives, so a stale path here is a broken pointer in six other
        repositories rather than a broken one here.
        """

        result = run("conventions")
        self.assertEqual(result.returncode, 0, result.stderr)
        printed = Path(result.stdout.strip())
        self.assertTrue(printed.is_file(), f"{printed} does not exist")
        self.assertEqual(
            printed,
            REPO_ROOT / "skills" / "sd-research-repo" / "references" / "conventions.md",
        )


class InlinedStylesheetTests(unittest.TestCase):
    def test_the_renderer_carries_its_css(self) -> None:
        kit = load_kit()
        tokens = kit.load("sd_research_tokens")
        self.assertIn(":root", tokens.TOKENS_CSS)
        self.assertGreater(len(tokens.TOKENS_CSS), 4000, "stylesheet looks truncated")

    def test_no_stylesheet_file_ships_beside_the_renderer(self) -> None:
        """`bin/` is Python only; a `.css` there fails the lint target loudly."""

        self.assertEqual(list((REPO_ROOT / "bin").glob("*.css")), [])


#: SHA-256 of ui-design's `foundation/tokens.css` and `foundation/theme.js`,
#: which the renderer carries verbatim (sd:3584). When the foundation changes,
#: paste the new file into `bin/sd_research_tokens.py` and re-pin here with
#: `shasum -a 256 tokens.css theme.js` in that folder.
FOUNDATION_TOKENS_SHA256 = "3c2b8c08778582910c59f0e37d2cca84446e00a248a454314853d1ae5a586f60"
FOUNDATION_THEME_SHA256 = "3d4cf4fc4b54c168212325d12c5d60b5487feee78329131c9e4ea17d029b3a24"

#: A colour written as a value rather than a token: hex, a colour function, or
#: the two named colours a hand-written rule reaches for.
RAW_COLOUR = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(|(?<![\w-])(?:white|black)(?![\w-])")
CUSTOM_PROPERTY = re.compile(r"(--[\w-]+)\s*:")


class FoundationStylesheetTests(unittest.TestCase):
    """The rendered page wears the shared foundation, unchanged (sd:3584).

    The foundation's token block is the only place a colour value may appear,
    and it must arrive byte for byte: a page that restates a value inline, or
    edits one in its copy, has forked the design language.
    """

    page: str
    styles: str
    tokens: types.ModuleType
    fonts: types.ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        load_publish()

        class Converter:
            """python-markdown is not in the gate's environment; the page
            shell and its stylesheet are what these tests read."""

            def __init__(self, **_options: object) -> None:
                self.toc_tokens = [{"level": 2, "id": "one", "name": "1. One", "children": []}]

            def convert(self, _text: str) -> str:
                return '<h2 id="one">1. One</h2><p>Body text.</p><table><tr><td>x</td></tr></table>'

        fake = types.ModuleType("markdown")
        fake.Markdown = Converter  # type: ignore[attr-defined]
        with unittest.mock.patch.dict(sys.modules, {"markdown": fake}):
            render = load_kit().load("sd_research_render")
        cls.tokens = load_kit().load("sd_research_tokens")
        cls.fonts = load_kit().load("sd_research_fonts")
        figs = [("1", "pass", "held", ""), ("2", "warn", "open", ""), ("3", "fail", "broken", ""),
                ("4", "caution", "watch", ""), ("5", "flat", "count", "")]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "doc.md").write_text("# Title\n\nBody text.\n", encoding="utf-8")
            with unittest.mock.patch("sys.stdout", io.StringIO()):
                page = render.build_one(tmp, dict(src="doc.md", out="doc", title="Title", figs=figs), "TEST")
            cls.page = Path(page).read_text(encoding="utf-8")
        styles = re.findall(r"<style>(.*?)</style>", cls.page, re.S)
        cls.styles = "\n".join(s for s in styles if s != cls.fonts.FONTS_CSS)

    def test_the_token_block_is_the_foundation_copy(self) -> None:
        block = self.tokens.FOUNDATION_TOKENS_CSS
        self.assertEqual(hashlib.sha256(block.encode("utf-8")).hexdigest(), FOUNDATION_TOKENS_SHA256,
                         "the token block differs from the foundation copy")
        self.assertIn(block, self.styles)

    def outside_the_token_block(self) -> str:
        block = getattr(self.tokens, "FOUNDATION_TOKENS_CSS", "")
        return self.styles.replace(block, "") if block else self.styles

    def test_no_raw_colour_outside_the_token_block(self) -> None:
        self.assertEqual(RAW_COLOUR.findall(self.outside_the_token_block()), [])

    def test_no_rule_redefines_a_foundation_token(self) -> None:
        ours = set(CUSTOM_PROPERTY.findall(self.tokens.FOUNDATION_TOKENS_CSS))
        rest = self.outside_the_token_block()
        self.assertEqual(ours & set(CUSTOM_PROPERTY.findall(rest)), set())

    def test_the_page_loads_the_foundation_theme_control(self) -> None:
        script = self.tokens.THEME_JS
        self.assertEqual(hashlib.sha256(script.encode("utf-8")).hexdigest(), FOUNDATION_THEME_SHA256)
        head = self.page.split("</head>", 1)[0]
        self.assertIn("<script>%s</script>" % script, head)
        for choice in ("dark", "light", "system"):
            self.assertIn('data-theme-choice="%s"' % choice, self.page)

    def test_the_faces_are_ibm_plex_and_the_old_identity_is_gone(self) -> None:
        families = set(re.findall(r"font-family:'([^']+)'", self.fonts.FONTS_CSS))
        self.assertEqual(families, {"IBM Plex Sans", "IBM Plex Sans Condensed", "IBM Plex Mono"})
        for gone in ("Archivo", "Source Serif", "Mezmo"):
            self.assertNotIn(gone, self.page)

    def test_every_state_figure_carries_its_glyph(self) -> None:
        for state, glyph in (("pass", "●"), ("warn", "▲"), ("fail", "■"), ("caution", "▲")):
            self.assertRegex(self.page, r'class="n is-%s"><span class="g"[^>]*>%s</span>' % (state, glyph))
        self.assertRegex(self.page, r'class="n is-flat">5<')


class CacheVenvTests(unittest.TestCase):
    def test_the_bootstrap_venv_never_lands_beside_the_script(self) -> None:
        kit = load_kit()
        venv = kit.cache_venv()
        self.assertNotIn(str(REPO_ROOT / "bin"), str(venv))
        self.assertEqual(venv.name, "venv")
        self.assertEqual(venv.parent.name, "sd-research-kit")

    def test_xdg_cache_home_is_honoured(self) -> None:
        kit = load_kit()
        with unittest.mock.patch.dict("os.environ", {"XDG_CACHE_HOME": "/tmp/xdg-probe"}):
            self.assertEqual(
                kit.cache_venv(), Path("/tmp/xdg-probe/sd-research-kit/venv")
            )


class WorkItemCoverageTests(unittest.TestCase):
    """`review` used to pass a repository whose work items were failing.

    The research conventions govern the documents in `research.conf.py`;
    the workflow governs the items under `docs/work/`. A repository can follow both
    at once, and until the two linters were connected this one printed
    "Mechanical checks pass" while `sd-docs-lint` returned 1 in the same
    directory -- true about this tool's scope, false about the repository.
    """

    def make_repo(self, tmp: Path, *, work: bool) -> Path:
        (tmp / "research.conf.py").write_text('PROJECT = "probe"\nDOCS = []\n')
        # `review` now also checks CLAUDE.md against the pack's template, and a
        # repo without one is maximal drift. These tests are about work items,
        # so they start from a repo that is in sync on the other axis.
        (tmp / "CLAUDE.md").write_text(TEMPLATE.read_text(encoding="utf-8"))
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        if work:
            item = tmp / "docs" / "work" / "2026-01-01-probe"
            item.mkdir(parents=True)
            (item / "prd.md").write_text(
                "---\ntitle: probe\nstatus: draft\ncreated: 2026-01-01\n---\n# Probe\n"
            )
            subprocess.run(["git", "add", "docs/work"], cwd=tmp, check=True, env=clean_environment())
        return tmp

    def test_a_failing_work_item_fails_the_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw), work=True)
            result = run("review", cwd=repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("2026-01-01-probe", result.stdout)
        self.assertIn("the row is the status; prd.md carries no status: line", result.stdout)

    def test_the_paths_are_reported_repo_relative(self) -> None:
        """The lint resolves its root through git, which returns the real path.

        On macOS a temporary directory is `/var/...` to the caller and
        `/private/var/...` to git, so a prefix strip against the caller's cwd
        alone shortens nothing and the review prints absolute paths beside
        repo-relative document names.
        """

        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw), work=True)
            result = run("review", cwd=repo)
        self.assertIn("FAIL docs/work/2026-01-01-probe/prd.md", result.stdout)

    def test_a_missing_linter_is_a_failure_not_a_pass(self) -> None:
        """A gate that cannot run has not been passed.

        Warning and returning 0 would reproduce this function's own bug one
        level down -- a clean review precisely when the checking is absent.
        """

        module = load_kit().load("sd_research_review")
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "docs" / "work").mkdir(parents=True)
            real = os.path.exists
            with unittest.mock.patch.object(
                module.os.path,
                "exists",
                lambda p: False if str(p).endswith("sd-docs-lint") else real(p),
            ):
                self.assertEqual(module.work_items(str(repo)), 1)

    def test_a_repository_with_no_work_directory_is_untouched(self) -> None:
        """The cost is paid only by repositories that keep work items."""

        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw), work=False)
            result = run("review", cwd=repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Mechanical checks pass", result.stdout)
        self.assertNotIn("docs/work", result.stdout)


    def test_an_ignored_work_directory_is_not_linted(self) -> None:
        """sd:3705. A developer checkout kept local items in an ignored
        `docs/work/`, and the review failed there and passed in a clean worktree."""

        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw), work=False)
            (repo / ".gitignore").write_text("docs/work/\n")
            item = repo / "docs" / "work" / "2026-01-01-probe"
            item.mkdir(parents=True)
            (item / "prd.md").write_text("---\ntitle: probe\nstatus: draft\n---\n# Probe\n")
            result = run("review", cwd=repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("docs/work", result.stdout)


class TrackedLinkTests(unittest.TestCase):
    """sd:3705. `checklinks` reads the tracked Markdown in a git repository."""

    def check(self, repo: Path) -> int:
        module = load_kit().load("sd_research_checklinks")
        with contextlib.redirect_stdout(io.StringIO()):
            return module.check(str(repo))

    def test_an_ignored_folder_is_not_walked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=clean_environment())
            (repo / ".gitignore").write_text("state/\n")
            (repo / "README.md").write_text("[ok](README.md)\n")
            (repo / "state" / "backup").mkdir(parents=True)
            (repo / "state" / "backup" / "README.md").write_text("[gone](missing.md)\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, env=clean_environment())
            self.assertEqual(self.check(repo), 0)
            (repo / "docs").mkdir()
            (repo / "docs" / "guide.md").write_text("[gone](missing.md)\n")
            subprocess.run(["git", "add", "docs"], cwd=repo, check=True, env=clean_environment())
            self.assertEqual(self.check(repo), 1)

    def test_outside_git_every_file_is_walked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "state").mkdir()
            (repo / "state" / "README.md").write_text("[gone](missing.md)\n")
            self.assertEqual(self.check(repo), 1)


class NotAResearchRepoTests(unittest.TestCase):
    """`review` outside a research repo is a refusal, not a pass (sd:947).

    sd:10 requirement 13: no `research.conf.py` exits 2. Until then `check()`
    printed the refusal and returned 0, `main()` returned that 0, and the kit's
    `review` verb passed on any directory at all -- a cwd mistake looked like a
    clean review. 2 is distinct from 1, the mechanical-failure exit, so a caller
    can tell "this is not a research repo" from "this one has findings".
    """

    def test_no_research_conf_exits_2_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = run("review", cwd=Path(raw))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no research.conf.py", result.stderr)
        self.assertIn("not a research repo", result.stderr)
        # The refusal is the whole output: the checklist is for a research
        # repo, and a mechanical-pass line here would be the bug restated.
        self.assertNotIn("Mechanical checks pass", result.stdout)
        self.assertNotIn("The half no script can do", result.stdout)

    def test_a_research_repo_with_nothing_to_check_still_exits_0(self) -> None:
        """The control: the minimal research repo is still a pass."""

        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "research.conf.py").write_text('PROJECT = "probe"\nDOCS = []\n')
            (repo / "CLAUDE.md").write_text(TEMPLATE.read_text(encoding="utf-8"))
            result = run("review", cwd=repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Mechanical checks pass", result.stdout)


class TemplateDriftTests(unittest.TestCase):
    """Nothing looked at `CLAUDE.md`, so three findings sat for six weeks.

    The five research repos all descend from one snapshot of
    `skills/sd-research-repo/templates/CLAUDE.md` and all carry local content of
    their own. A detector that called every difference drift would fire on every
    repo forever; one that called none would be what we had. These pin the line
    between the two.
    """

    def setUp(self) -> None:
        self.module = load_kit().load("sd_research_review")
        self.template = TEMPLATE.read_text(encoding="utf-8")

    def findings(self, repo_text: str):
        return self.module.drift(self.template, repo_text)[0]

    def test_a_verbatim_copy_has_no_findings(self) -> None:
        self.assertEqual(self.findings(self.template), [])

    def test_a_whole_local_section_is_not_drift(self) -> None:
        """Every research repo adds sections. That is the repo doing its job."""

        local = self.template + (
            "\n## Fan-out, and the filenames a subagent cannot write\n\n"
            "Reading fans out; writing does not.\n"
        )
        self.assertEqual(self.findings(local), [])

    def test_a_local_paragraph_inside_a_shared_section_is_not_drift(self) -> None:
        text = self.template.replace(
            "## Style\n",
            "## Style\n\nThis repo also spells Jira tickets `PROJ-123`.\n",
        )
        self.assertEqual(self.findings(text), [])

    def test_a_filled_in_url_is_not_drift(self) -> None:
        """The template describes the mirror folder; the repo writes the URL."""

        # One line of the prompt, so a rewrap of the paragraph around it does
        # not read as the prompt having moved.
        aside = "its URL here when the repo designates one."
        self.assertIn(aside, self.template, "the template's fill-in prompt moved")
        text = self.template.replace(aside, "https://app.notion.com/p/3c9f52b1578281")
        self.assertEqual(self.findings(text), [])

    def test_a_filled_in_slot_is_not_drift(self) -> None:
        slot = "<descriptive project or decision title>"
        self.assertIn(slot, self.template, "the template's title slot moved")
        text = self.template.replace(slot, "Probe adoption decision")
        self.assertEqual(self.findings(text), [])

    def test_a_reworded_template_paragraph_is_drift(self) -> None:
        """The failure that actually happened: the repo kept the old wording."""

        current = "**Do not publish research as an artifact.**"
        self.assertIn(current, self.template)
        text = self.template.replace(current, "**Do not publish research as a Claude artifact.**")
        found = self.findings(text)
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0][1], "reworded")
        self.assertIn("Publishing", found[0][0])

    def test_a_missing_template_section_is_drift(self) -> None:
        head = "## Main document — START HERE"
        self.assertIn(head, self.template)
        before, _, rest = self.template.partition(head)
        text = before + rest.partition("\n## ")[1] + rest.partition("\n## ")[2]
        found = self.findings(text)
        self.assertIn(("`%s`" % head, "section is missing", ""), found)

    def test_a_dropped_paragraph_is_drift(self) -> None:
        gone = "Use only the directories this repo needs; do not invent new ones.\n"
        self.assertIn(gone, self.template)
        found = self.findings(self.template.replace(gone, ""))
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0][1], "gone")

    def test_a_longer_paragraph_that_says_the_block_is_not_drift(self) -> None:
        """The template is a floor: saying its block and more is not falling behind."""

        block = "Add or edit pages in `research.conf.py`, never by editing generated files."
        self.assertIn(block, self.template)
        text = self.template.replace(
            block,
            "Writes `docs/dashboard/<name>.html`. Add or edit pages in\n"
            "`research.conf.py`, never by editing generated\nfiles. Keep it that way.",
        )
        self.assertEqual(self.findings(text), [])

    def test_a_block_cut_short_is_still_drift(self) -> None:
        """Containment runs one way: the repo must say all of the template block."""

        block = "Add or edit pages in `research.conf.py`, never by editing generated files."
        text = self.template.replace(block, "Add or edit pages in `research.conf.py`.")
        found = self.findings(text)
        self.assertEqual(len(found), 1, found)
        self.assertIn("Rendering", found[0][0])

    def test_a_repo_carrying_the_long_template_drifts_only_where_the_template_changed(self) -> None:
        """The five research repos copied the 193-line template, and still carry it.

        The shrink must not report the text it removed. Three sections do drift.
        `## Parallel work` has new wording; no research repo carried the old
        one either. `## Adversarial review before publishing` names the second
        reader as `sd-review --lens research-brief` since #1194, and the long
        copy still describes a named CLI with `-s read-only`, so that drift is
        the signal the repos need. `## Layout` says an official repo you
        change is not vendored, which the long copy predates; that drift is a
        signal too.
        """

        found = self.findings(LONG_TEMPLATE.read_text(encoding="utf-8"))
        self.assertTrue(found)
        self.assertEqual({where for where, _, _ in found},
                         {"`## Layout`", "`## Parallel work`", "`## Adversarial review before publishing`"}, found)
        review = [text for where, kind, text in found if "Adversarial" in where]
        self.assertEqual(len(review), 1, review)
        self.assertIn("sd-review --lens research-brief", review[0])

    def test_local_content_is_counted_not_reported(self) -> None:
        text = self.template + "\n## Local\n\nOne local block.\n"
        found, local = self.module.drift(self.template, text)
        self.assertEqual(found, [])
        self.assertEqual(local, 1)

    def test_a_repo_with_no_claude_md_fails_the_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(self.module.template_drift(raw), 1)

    def test_a_missing_template_is_a_failure_not_a_pass(self) -> None:
        """Same rule as the missing linter: a gate that cannot run has not passed."""

        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "CLAUDE.md").write_text(self.template)
            with unittest.mock.patch.object(self.module, "TEMPLATE", raw + "/nope.md"):
                self.assertEqual(self.module.template_drift(raw), 1)

    def test_a_fenced_example_heading_does_not_invent_a_section(self) -> None:
        """Both files fence a markdown example whose body starts `## 1. First section`."""

        heads = [head for head, _ in self.module.sections(self.template)]
        self.assertNotIn("1. First section", heads)

    def test_review_reports_the_drift_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "research.conf.py").write_text('PROJECT = "probe"\nDOCS = []\n')
            (repo / "CLAUDE.md").write_text(
                self.template.replace(
                    "**Do not publish research as an artifact.**",
                    "**Do not publish research as a Claude artifact.**",
                )
            )
            result = run("review", cwd=repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL CLAUDE.md", result.stdout)
        self.assertIn("Publishing", result.stdout)

    def test_review_says_in_sync_when_it_is(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "research.conf.py").write_text('PROJECT = "probe"\nDOCS = []\n')
            (repo / "CLAUDE.md").write_text(self.template)
            result = run("review", cwd=repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CLAUDE.md: in sync with the template", result.stdout)


class MainDocumentTests(unittest.TestCase):
    """The `START HERE — ` convention, checked rather than only written down.

    sd:534. `references/conventions.md` stated the convention and closed the
    section with "`sd-research-kit review` does not currently enforce this
    title convention" -- an admission, not a check. sd:387 then found three of
    five research repos with no main document at all, and a person reading
    found it, because nothing else could.

    What is checkable is the half inside the checkout: the Markdown H1 and the
    `title`/`h1` the config renders it under. The README's entry link and the
    Notion page title are the other half and stay in the manual checklist, so
    the passing line says which surfaces it covered instead of printing a bare
    ok that a reader would take for all four.
    """

    def make_repo(self, tmp: Path, docs: str) -> Path:
        (tmp / "research.conf.py").write_text(f'PROJECT = "probe"\nDOCS = {docs}\n')
        (tmp / "CLAUDE.md").write_text(TEMPLATE.read_text(encoding="utf-8"))
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        return tmp

    def entry(self, src: str, title: str) -> str:
        return (
            "{'src': %r, 'out': %r, 'title': %r, 'h1': %r}"
            % (src, src.removesuffix(".md"), title, title)
        )

    def write(self, repo: Path, src: str, h1_line: str) -> None:
        path = repo / src
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {h1_line}\n\nBody.\n")

    def test_a_repo_with_no_main_document_fails(self) -> None:
        """sd:387's defect, which went undetected because nothing looked."""

        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw), "[%s]" % self.entry(
                "30-brief/BRIEF-thing.md", "A brief about a thing"))
            self.write(repo, "30-brief/BRIEF-thing.md", "A brief about a thing")
            result = run("review", cwd=repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no configured document's H1 starts with", result.stdout)

    def test_two_main_documents_fail_and_both_are_named(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            docs = "[%s, %s]" % (
                self.entry("30-brief/BRIEF-one.md", "START HERE — One"),
                self.entry("30-brief/BRIEF-two.md", "START HERE — Two"),
            )
            repo = self.make_repo(Path(raw), docs)
            self.write(repo, "30-brief/BRIEF-one.md", "START HERE — One")
            self.write(repo, "30-brief/BRIEF-two.md", "START HERE — Two")
            result = run("review", cwd=repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("2 carry", result.stdout)
        self.assertIn("BRIEF-one.md", result.stdout)
        self.assertIn("BRIEF-two.md", result.stdout)

    def test_a_title_the_config_renders_differently_is_a_mismatch(self) -> None:
        """The instance in sd:534: compliant in substance, wrong in title.

        A repo can carry a real main document and still have the rendered page
        and the Notion mirror disagree with its H1. That is the convention
        failing in the direction nobody looks, so it is a finding of its own
        rather than folded into the missing case.
        """

        with tempfile.TemporaryDirectory() as raw:
            docs = "[{'src': '30-brief/BRIEF-one.md', 'out': 'one', " \
                   "'title': 'Main document', 'h1': 'START HERE — One'}]"
            repo = self.make_repo(Path(raw), docs)
            self.write(repo, "30-brief/BRIEF-one.md", "START HERE — One")
            result = run("review", cwd=repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("`title` renders it as 'Main document'", result.stdout)

    def test_the_passing_line_names_the_half_it_did_not_check(self) -> None:
        """A bare ok would claim the README and Notion surfaces were read."""

        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw), "[%s]" % self.entry(
                "30-brief/BRIEF-one.md", "START HERE — One"))
            self.write(repo, "30-brief/BRIEF-one.md", "START HERE — One")
            result = run("review", cwd=repo)
        self.assertIn("ok   main document: 30-brief/BRIEF-one.md", result.stdout)
        self.assertIn("checklist's half", result.stdout)

    def test_a_repo_with_no_documents_yet_is_not_a_failure(self) -> None:
        """`init-claude-md` lays the standard into exactly that repo."""

        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw), "[]")
            result = run("review", cwd=repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL main document", result.stdout)
        self.assertNotIn("ok   main document", result.stdout)

    def test_the_convention_is_stated_once_and_the_checker_reads_it(self) -> None:
        """The marker lives in one place; the code carries no second copy.

        sd:534's own argument: restating the rule in code would create a third
        copy of a convention that already has two.
        """

        module = load_kit().load("sd_research_review")
        conventions = (
            REPO_ROOT / "skills" / "sd-research-repo" / "references"
            / "conventions.md"
        ).read_text(encoding="utf-8")
        self.assertIn("`%s`" % module.MAIN_TITLE, conventions)
        self.assertNotIn("does not currently enforce", conventions)


class LocalOverrideTests(unittest.TestCase):
    """The case a one-directional check cannot read: a *replaced* block.

    The detector shipped with a word for what a repo adds (local content, left
    alone) and a word for what it drops (drift), and none for what it replaces.
    Two of the five research repos replace the template's personal `file:///`
    Source header because their Notion folder is a team space read by people
    with no such checkout -- correct work, reported as four defects between them
    on every run, with no way to say so. A check that reports a repo forever for
    doing the right thing is a check that gets switched off, which is the
    failure the drift detector exists to prevent.
    """

    OVERRIDE_HEAD = "\n## Local overrides of the shared template\n\n"

    def setUp(self) -> None:
        self.module = load_kit().load("sd_research_review")
        self.template = TEMPLATE.read_text(encoding="utf-8")
        # A real replacement, in the shape the research repositories use:
        # the section is still there, one of its blocks says something else.
        self.replaced = self.template.replace(
            "- Absolute paths when pointing at a local file: `file:///Users/...`, "
            "not a bare path.",
            "- Local notes may use absolute paths; published pages must not.",
        )
        self.assertNotEqual(self.replaced, self.template, "the Style bullet moved")

    def report(self, text: str):
        """`template_drift`'s exit count and what it printed."""
        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "CLAUDE.md").write_text(text, encoding="utf-8")
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                count = self.module.template_drift(raw)
            return count, out.getvalue()

    def test_an_undeclared_replacement_is_still_drift(self) -> None:
        """The override is opt-in. Saying nothing changes nothing."""

        count, printed = self.report(self.replaced)
        self.assertEqual(count, 1, printed)
        self.assertIn("FAIL CLAUDE.md `## Style`", printed)

    def test_a_declared_override_is_not_drift(self) -> None:
        count, printed = self.report(
            self.replaced + self.OVERRIDE_HEAD
            + "- `## Style` — published pages are read by people with no checkout.\n"
        )
        self.assertEqual(count, 0, printed)
        self.assertIn("`## Style`: overridden locally", printed)

    def test_an_honoured_override_prints_what_it_stopped_comparing(self) -> None:
        """The standing cost of an override, on the page, on every run.

        A template change to an overridden section lands unseen. That is the
        price, and a price nobody is quoted is a price nobody re-examines.
        """

        _, printed = self.report(
            self.replaced + self.OVERRIDE_HEAD
            + "- `## Style` — published pages are read by people with no checkout.\n"
        )
        self.assertIn("(1 template block(s) not compared)", printed)
        self.assertIn("published pages are read by people with no checkout", printed)

    def test_an_override_with_no_reason_fails_and_suppresses_nothing(self) -> None:
        """Otherwise the list is a mute button, and mute buttons get used.

        Both halves matter. The entry is reported, *and* the drift it names is
        still reported under it -- an unreasoned entry that silenced the section
        while complaining about itself would be a mute button with a warning
        label, which is still a mute button.
        """

        count, printed = self.report(
            self.replaced + self.OVERRIDE_HEAD + "- `## Style`\n"
        )
        self.assertEqual(count, 2, printed)
        self.assertIn("local override with no reason", printed)
        self.assertIn("FAIL CLAUDE.md `## Style`: gone", printed)

    def test_an_override_of_a_section_the_template_lacks_fails(self) -> None:
        """A stale override is the same disease one level down: an unread claim."""

        count, printed = self.report(
            self.template + self.OVERRIDE_HEAD
            + "- `## Retired Section` — kept after the template dropped it.\n"
        )
        self.assertEqual(count, 1, printed)
        self.assertIn("the template does not have", printed)

    def test_an_override_does_not_excuse_another_section(self) -> None:
        """It suppresses the section it names and nothing else."""

        gone = "Use only the directories this repo needs; do not invent new ones.\n"
        self.assertIn(gone, self.template)
        count, printed = self.report(
            self.replaced.replace(gone, "") + self.OVERRIDE_HEAD
            + "- `## Style` — published pages are read by people with no checkout.\n"
        )
        self.assertEqual(count, 1, printed)
        self.assertIn("`## Layout`", printed)

    def test_a_reason_wrapping_onto_the_next_line_is_read_whole(self) -> None:
        count, printed = self.report(
            self.replaced + self.OVERRIDE_HEAD
            + "- `## Style` — published pages are read by people who do not\n"
            "  have this checkout, so an absolute path is unusable there.\n"
        )
        self.assertEqual(count, 0, printed)
        self.assertIn("unusable there", printed)

    def test_the_overrides_section_itself_is_not_drift(self) -> None:
        """It is a local section, like any other the repo adds."""

        found, _ = self.module.drift(
            self.template,
            self.template + self.OVERRIDE_HEAD + "- `## Style` — because.\n",
        )
        self.assertEqual(found, [])

    def opening_changed(self) -> str:
        changed = self.template.replace("Applies to this repo.", "Use these repository conventions.")
        self.assertNotEqual(changed, self.template)
        return changed

    def test_declared_opening_override_is_honoured(self) -> None:
        count, printed = self.report(
            self.opening_changed() + self.OVERRIDE_HEAD
            + "- `the opening`: use STE-Concise sentences.\n"
        )
        self.assertEqual(count, 0, printed)
        self.assertIn("the opening: overridden locally (1 template block(s) not compared)", printed)
        self.assertIn("use STE-Concise sentences", printed)

    def test_undeclared_opening_change_still_fails(self) -> None:
        count, printed = self.report(self.opening_changed())
        self.assertEqual(count, 1, printed)
        self.assertIn("FAIL CLAUDE.md the opening", printed)

    def test_opening_override_requires_a_reason(self) -> None:
        for suffix in ("", ":", " — "):
            with self.subTest(suffix=suffix):
                count, printed = self.report(
                    self.opening_changed() + self.OVERRIDE_HEAD + f"- `the opening`{suffix}\n"
                )
                self.assertEqual(count, 2, printed)
                self.assertIn("the opening: declared a local override with no reason", printed)
                self.assertIn("FAIL CLAUDE.md the opening: reworded", printed)

    def test_opening_override_does_not_excuse_section_drift(self) -> None:
        changed = self.opening_changed().replace(
            "Use only the directories this repo needs; do not invent new ones.\n", ""
        )
        count, printed = self.report(
            changed + self.OVERRIDE_HEAD + "- `the opening`: use STE-Concise sentences.\n"
        )
        self.assertEqual(count, 1, printed)
        self.assertIn("FAIL CLAUDE.md `## Layout`", printed)
        self.assertNotIn("FAIL CLAUDE.md the opening", printed)

    def test_opening_and_heading_with_same_name_are_distinct(self) -> None:
        declared = self.module.declared_overrides(
            self.OVERRIDE_HEAD + "- `the opening`: shorten the preamble.\n"
            + "- `## the opening`: rename this section.\n"
        )
        self.assertEqual(declared, {"": "shorten the preamble.", "the opening": "rename this section."})

    def test_fenced_opening_override_is_not_a_declaration(self) -> None:
        for prefix in ("", "Example:\n"):
            with self.subTest(prefix=prefix):
                count, printed = self.report(
                    self.opening_changed() + self.OVERRIDE_HEAD + prefix
                    + "```markdown\n- `the opening`: an example, not a declaration.\n```\n"
                )
                self.assertEqual(count, 1, printed)
                self.assertIn("FAIL CLAUDE.md the opening", printed)
                self.assertNotIn("overridden locally", printed)

    def test_heading_override_does_not_excuse_opening_drift(self) -> None:
        template = "# Title\n\nOriginal opening.\n\n## the opening\n\nOriginal section.\n"
        changed = "# Title\n\nChanged opening.\n\n## the opening\n\nChanged section.\n"
        findings, _ = self.module.drift(template, changed)
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            remaining, faults = self.module.apply_overrides(
                findings, changed + self.OVERRIDE_HEAD
                + "- `## the opening`: shorten this section.\n", template
            )
        self.assertEqual(faults, 0)
        self.assertEqual([entry[0] for entry in remaining], ["the opening"])

    def test_fenced_section_override_is_not_a_declaration(self) -> None:
        count, printed = self.report(
            self.replaced + self.OVERRIDE_HEAD
            + "```markdown\n- `## Style`: an example, not a declaration.\n```\n"
        )
        self.assertEqual(count, 1, printed)
        self.assertIn("FAIL CLAUDE.md `## Style`", printed)
        self.assertNotIn("overridden locally", printed)

    def test_blank_heading_does_not_authorize_an_opening_override(self) -> None:
        count, printed = self.report(
            self.opening_changed() + self.OVERRIDE_HEAD + "- `##   `: shorten the preamble.\n"
        )
        self.assertEqual(count, 1, printed)
        self.assertIn("FAIL CLAUDE.md the opening", printed)
        self.assertNotIn("overridden locally", printed)


class InitClaudeMdTests(unittest.TestCase):
    """`init-claude-md` lays the first copy, and only ever the first.

    sd:518: five repos carried a copy of a 167-line template and nothing told
    anyone to install it, so the route was unwritten and the copies drifted. The
    verb writes the route down. What it deliberately does not do is re-lay an
    existing copy -- see `LocalOverrideTests` for the reason that would be
    destructive rather than merely unhelpful.
    """

    def setUp(self) -> None:
        self.template = TEMPLATE.read_text(encoding="utf-8")

    def test_it_writes_the_template_into_a_repo_with_none(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "research.conf.py").write_text('PROJECT = "probe"\nDOCS = []\n')
            result = run("init-claude-md", cwd=repo)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                (repo / "CLAUDE.md").read_text(encoding="utf-8"), self.template
            )

    def test_what_it_writes_passes_the_drift_check_it_ships_with(self) -> None:
        """A laid copy the checker then calls drifted would be worse than none."""

        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "research.conf.py").write_text('PROJECT = "probe"\nDOCS = []\n')
            self.assertEqual(run("init-claude-md", cwd=repo).returncode, 0)
            result = run("review", cwd=repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CLAUDE.md: in sync with the template", result.stdout)

    def test_it_refuses_to_overwrite_an_existing_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "research.conf.py").write_text('PROJECT = "probe"\nDOCS = []\n')
            (repo / "CLAUDE.md").write_text("local work\n", encoding="utf-8")
            result = run("init-claude-md", cwd=repo)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                (repo / "CLAUDE.md").read_text(encoding="utf-8"), "local work\n"
            )
        self.assertIn("already exists", result.stderr)

    def test_the_refusal_names_where_a_deliberate_difference_goes(self) -> None:
        """The refusal is only fair if it says what to do instead."""

        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "research.conf.py").write_text('PROJECT = "probe"\nDOCS = []\n')
            (repo / "CLAUDE.md").write_text("local work\n", encoding="utf-8")
            result = run("init-claude-md", cwd=repo)
        self.assertIn("review", result.stderr)
        self.assertIn("Local overrides of the shared template", result.stderr)

    def test_it_refuses_outside_a_research_repo(self) -> None:
        """No `research.conf.py` means this is not the kit's repo to write in."""

        with tempfile.TemporaryDirectory() as raw:
            result = run("init-claude-md", cwd=Path(raw))
            self.assertEqual(result.returncode, 1)
            self.assertFalse((Path(raw) / "CLAUDE.md").exists())
        self.assertIn("research.conf.py", result.stderr)


class BuildFreshness(unittest.TestCase):
    """The freshness gate, against the directory the renderer actually writes.

    `sd_research_publish.DASHBOARD_DIR` replaced `build/` with
    `docs/dashboard/`, and this check kept the old spelling. Nothing noticed,
    because nothing tested it: every research repo has reported
    `not rendered yet` ever since, and the "edited newer than its build" FAIL —
    the half that actually stops a stale page being published — could not fire
    at all. A gate that cannot fail is not a gate.

    The three cases below are the whole contract: no build warns, a fresh build
    is silent, a stale build fails. The middle one is what regressed; the last
    one is what it cost.
    """

    #: A document that passes every *other* check, so a freshness finding is
    #: the only thing these tests can be reading.
    DOC = (
        "# START HERE — A probe\n"
        "\n"
        "Compiled 2026-09-21 from `owner/repo` @ `abc1234`.\n"
        "\n"
        "---\n"
        "\n"
        "## 1. Body\n"
        "\n"
        "Text.\n"
        "\n"
        "## 2. Status\n"
        "\n"
        "**Verified.** The body. **Not verified.** Everything else.\n"
    )

    def make_repo(self, tmp: Path) -> Path:
        (tmp / "research.conf.py").write_text(
            "PROJECT = 'probe'\n"
            "DOCS = [{'src': '00-overview/understanding.md', 'out': 'overview',"
            " 'title': 'START HERE — A probe', 'h1': 'START HERE — A probe'}]\n"
        )
        (tmp / "CLAUDE.md").write_text(TEMPLATE.read_text(encoding="utf-8"))
        src = tmp / "00-overview" / "understanding.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(self.DOC)
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        return tmp

    def build(self, repo: Path, *, older_than_source: bool) -> Path:
        """Write `docs/dashboard/overview.html`, dated either side of the source."""
        out = repo / "docs" / "dashboard" / "overview.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html></html>")
        src = repo / "00-overview" / "understanding.md"
        stamp = os.path.getmtime(src) + (-60 if older_than_source else 60)
        os.utime(out, (stamp, stamp))
        return out

    def test_a_document_with_no_build_is_reported_as_unrendered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            result = run("review", cwd=repo)
        self.assertIn("not rendered yet", result.stdout)

    def test_a_build_newer_than_its_source_is_silent(self) -> None:
        """The regression: a rendered page read as never rendered."""

        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            self.build(repo, older_than_source=False)
            result = run("review", cwd=repo)
        self.assertNotIn("not rendered yet", result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_source_edited_after_its_build_fails(self) -> None:
        """The check the regression disabled outright."""

        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            self.build(repo, older_than_source=True)
            result = run("review", cwd=repo)
        self.assertIn("newer than its build", result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_the_checked_directory_is_the_one_the_renderer_writes(self) -> None:
        """One spelling of the published directory, not two.

        The two modules agreeing by coincidence is how this broke; asserting
        the identity is what stops the next rename splitting them again.
        """

        bin_dir = str(REPO_ROOT / "bin")
        if bin_dir not in sys.path:
            sys.path.insert(0, bin_dir)
        review = load_kit().load("sd_research_review")
        spec = importlib.util.spec_from_file_location(
            "sd_research_publish", REPO_ROOT / "bin" / "sd_research_publish.py"
        )
        publish = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(publish)
        self.assertEqual(review.DASHBOARD_DIR, publish.DASHBOARD_DIR)


class MapLedgerTests(unittest.TestCase):
    """sd:1835 and sd:1836: a map keys its ledger by stable row IDs and names its gaps.

    Rulings #6985 and #6986 make both required and checked by `review`. Neither
    says the check fails, and the research repos predate it, so a map that
    lacks either warns and the exit stays 0.
    """

    LEDGER = (
        "| # | Capability | Verdict |\n"
        "|---|---|---|\n"
        "| C01 | Tools | shipped |\n"
        "| **C02** | Resources | open |\n"
    )
    GAPS = "## 3. Gaps\n\nC02 has no owner.\n"

    def review(self, map_text: str | None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / "research.conf.py").write_text('PROJECT = "probe"\nDOCS = []\n')
            (repo / "CLAUDE.md").write_text(TEMPLATE.read_text(encoding="utf-8"))
            if map_text is not None:
                (repo / "20-map").mkdir()
                (repo / "20-map" / "MAP-probe.md").write_text(map_text)
            return run("review", cwd=repo)

    def test_a_map_with_a_ledger_and_gaps_passes(self) -> None:
        result = self.review("# Map\n\n" + self.LEDGER + "\n" + self.GAPS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   20-map/: 1 map(s)", result.stdout)
        self.assertNotIn("WARN 20-map/", result.stdout)

    def test_a_ledger_without_row_ids_warns_and_does_not_fail(self) -> None:
        unkeyed = self.LEDGER.replace("C01", "Tools row").replace("**C02**", "Resources row")
        result = self.review("# Map\n\n" + unkeyed + "\n" + self.GAPS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARN 20-map/MAP-probe.md: no table keyed by stable row IDs",
                      result.stdout)

    def test_a_map_without_a_gaps_section_warns_and_does_not_fail(self) -> None:
        result = self.review("# Map\n\n" + self.LEDGER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARN 20-map/MAP-probe.md: no gaps section", result.stdout)

    def test_a_fenced_example_is_not_the_map(self) -> None:
        """A map that shows the shape in a code fence has not adopted it."""

        fenced = "# Map\n\n```markdown\n" + self.LEDGER + "\n" + self.GAPS + "```\n"
        result = self.review(fenced)
        self.assertIn("no table keyed by stable row IDs", result.stdout)
        self.assertIn("no gaps section", result.stdout)

    def test_a_repo_with_no_maps_says_nothing_about_them(self) -> None:
        result = self.review(None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("20-map/", result.stdout)


class ArchitectureOverviewTests(unittest.TestCase):
    """pack sd:5660: the layout gains `05-architecture/`, one overview per project.

    Every research repo predates the stage, so a missing or unrendered overview
    warns and the exit stays 0.
    """

    DOC = "05-architecture/architecture.md"
    OVERVIEW = REPO_ROOT / "skills" / "sd-research-repo" / "templates" / DOC

    def review(self, *, overview: bool, configured: bool) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            docs = (f"[{{'src': './{self.DOC}', 'out': 'architecture',"
                    " 'title': 'Architecture'}]" if configured else "[]")
            (repo / "research.conf.py").write_text(f'PROJECT = "probe"\nDOCS = {docs}\n')
            (repo / "CLAUDE.md").write_text(TEMPLATE.read_text(encoding="utf-8"))
            if overview:
                (repo / "05-architecture").mkdir()
                (repo / self.DOC).write_text(self.OVERVIEW.read_text(encoding="utf-8"))
            return run("review", cwd=repo)

    def test_a_repo_without_the_overview_warns_and_does_not_fail(self) -> None:
        result = self.review(overview=False, configured=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"WARN {self.DOC}: missing", result.stdout)

    def test_an_overview_the_config_does_not_render_warns(self) -> None:
        result = self.review(overview=True, configured=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"WARN {self.DOC}: not in research.conf.py DOCS", result.stdout)

    def test_a_configured_overview_is_named_ok(self) -> None:
        """A `./` spelling of the path is the same document."""

        result = self.review(overview=True, configured=True)
        self.assertIn(f"ok   {self.DOC}", result.stdout)
        self.assertNotIn(f"WARN {self.DOC}", result.stdout)

    def test_the_template_has_the_shape_review_checks(self) -> None:
        load_publish()  # puts `bin/` on the path, so the class runs on its own
        module = load_kit().load("sd_research_review")
        text = self.OVERVIEW.read_text(encoding="utf-8")
        self.assertTrue(module.provenance(text)[0], module.provenance(text))
        self.assertTrue(module.status_section(text)[0], module.status_section(text))
        self.assertIn("Archify", text)

    def test_every_layout_table_names_the_stage(self) -> None:
        conventions = REPO_ROOT / "skills" / "sd-research-repo" / "references" / "conventions.md"
        for path in (TEMPLATE, conventions):
            rows = [line for line in path.read_text(encoding="utf-8").splitlines()
                    if line.startswith("| `05-architecture/`")]
            self.assertEqual(len(rows), 1, path)
            self.assertIn("`architecture.md`", rows[0], path)


#: A synthetic Archify SVG export, in its shape: page-wide `:root`, `svg` and
#: `[data-theme]` rules, and `@keyframes` lines that end in a space.
ARCHIFY_EXPORT = (
    '<svg viewBox="0 0 10 10" role="img" aria-labelledby="archify-diagram-title archify-diagram-description"'
    ' data-preset="classic" data-theme="dark" tabindex="0" style="" xmlns="http://www.w3.org/2000/svg">'
    "<style>@font-face { font-family: 'Mono'; src: local('Mono'); }\n"
    "svg { font-family: 'Mono'; }\n"
    ':root, [data-theme="dark"] { --bg: #000; }\n'
    '[data-theme="light"] { --bg: #fff; }\n'
    "@keyframes archify-flow { \n  0% { opacity: 0; } \n  100% { opacity: 1; }\n}\n"
    "svg .s-x { color: var(--bg); }\n"
    "svg .sigil &gt; * { fill: none; }\n"
    'svg[data-preset="blueprint"] .c-grid, .c-lane { opacity: 0.5; }\n'
    ".c-grid { stroke: var(--bg); }\n"
    ":root, svg { --bg: #000; }\n"
    "@media (prefers-color-scheme: light) { :root, svg { --bg: #fff; } }\n"
    'svg[data-theme="light"] { --bg: #fff; }\n'
    'svg[data-theme="dark"] { --bg: #000; }\n'
    '</style>\n\n<title id="archify-diagram-title">Parts</title>'
    '<rect class="c-grid" tabindex="0"/>  \n</svg>')


class ArchifyEmbedTests(unittest.TestCase):
    """pack sd:5918: render scopes an embedded Archify SVG to the diagram.

    Archify's export styles `:root`, `svg` and `[data-theme]` for the whole
    page, and its `@keyframes` lines end in a space.
    """

    @classmethod
    def setUpClass(cls) -> None:
        load_publish()
        seen = cls.seen = []

        class Converter:
            def __init__(self, **_options: object) -> None:
                self.toc_tokens: list = []

            def convert(self, text: str) -> str:
                seen.append(text)
                return text

        fake = types.ModuleType("markdown")
        fake.Markdown = Converter  # type: ignore[attr-defined]
        with unittest.mock.patch.dict(sys.modules, {"markdown": fake}):
            cls.render = load_kit().load("sd_research_render")

    def page(self, svg: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            doc = "# Overview\n\nFrom a probe.\n\n---\n\n## 1. Diagram\n\n<div class=\"diagram\">\n%s\n</div>\n" % svg
            (Path(tmp) / "doc.md").write_text(doc, encoding="utf-8")
            with unittest.mock.patch("sys.stdout", io.StringIO()):
                self.render.build_one(tmp, dict(src="doc.md", out="doc", title="Overview"), "TEST")
        return self.seen[-1]

    def embedded(self, text: str) -> tuple[str, str]:
        svg = re.search(r"<svg\b.*?</svg>", text, re.S).group(0)
        return svg, re.search(r"<style>(.*?)</style>", svg, re.S).group(1)

    def test_every_rule_is_scoped_to_the_diagram(self) -> None:
        svg, css = self.embedded(self.page(ARCHIFY_EXPORT))
        preludes = [prelude for prelude, _ in self.render.css_blocks(css)]
        loose = [p for p in preludes if not p.startswith("@")
                 and not all(re.match(r'(\[data-theme="\w+"\] )?\.archify-diagram\b', part.strip())
                             for part in p.split(","))]
        self.assertEqual(loose, [])
        self.assertIn('[data-theme="light"] .archify-diagram {', css)
        self.assertIn('@media (prefers-color-scheme: light) {:root:not([data-theme="dark"])'
                      ':not([data-theme="light"]) .archify-diagram {', css)
        self.assertIn('.archify-diagram[data-preset="blueprint"] .c-grid, .archify-diagram .c-lane {', css)
        self.assertIn(".archify-diagram .sigil &gt; * {", css)
        self.assertTrue(svg.startswith('<svg class="archify-diagram" viewBox='), svg[:80])
        self.assertNotRegex(svg, r'\s(data-theme|tabindex)="|\sstyle=""')

    def test_no_line_ends_in_whitespace_and_none_is_blank(self) -> None:
        svg, _ = self.embedded(self.page(ARCHIFY_EXPORT))
        self.assertIn("@keyframes archify-flow {", svg)
        self.assertEqual([line for line in svg.splitlines() if line != line.rstrip() or not line.strip()], [])

    def test_a_scoped_svg_is_left_as_it_is(self) -> None:
        once = self.embedded(self.page(ARCHIFY_EXPORT))[0]
        self.assertEqual(self.render.scope_archify(once), once)

    def test_an_svg_that_is_not_archifys_is_left_as_it_is(self) -> None:
        other = '<svg viewBox="0 0 1 1"><style>svg { fill: red; }  \n</style></svg>'
        self.assertEqual(self.render.scope_archify(other), other)

    def test_a_comment_before_a_selector_is_no_part_of_it(self) -> None:
        """pack sd:5944: `/* Font */ svg` scoped as a descendant missed the root."""
        css = self.render.scope_rules("/* Font */ svg { font: serif; }", ".archify-diagram")
        self.assertEqual(css, ".archify-diagram { font: serif; }")

    def test_a_comment_before_an_at_rule_is_no_part_of_it(self) -> None:
        """pack sd:5944: `/* Theme */ @media` was scoped as a selector."""
        css = self.render.scope_rules(
            "/* Theme */ @media (prefers-color-scheme: light) {svg {fill: black}}", ".archify-diagram")
        self.assertEqual(css, '@media (prefers-color-scheme: light) {%s .archify-diagram {fill: black}}'
                         % self.render.PAGE_UNPICKED)

    def test_a_brace_in_a_comment_or_a_string_opens_no_block(self) -> None:
        """pack sd:5944: blocks match braces outside comments and strings."""
        css = '/* { */ svg { content: "}"; } .c-a { fill: red; /* } */ }'
        self.assertEqual(list(self.render.css_blocks(css)),
                         [("svg", ' content: "}"; '), (".c-a", " fill: red; /* } */ ")])


class InitHookInstallTests(unittest.TestCase):
    """`init-hook` installs one source under every trigger, or none at all.

    The contract promises a render after a commit, a merge and a checkout
    (`skills/_shared/references/publication-contract.md`), and until sd:1353
    the installer wrote `post-commit` alone -- so a pull or a branch switch
    left `build/` holding the other tree's render, looking current.

    All-or-nothing is the other half. A repository installed on one trigger
    and not the others is worse than one with no hook: it renders often enough
    that nobody notices the times it does not.
    """

    TRIGGERS = ("post-commit", "post-merge", "post-checkout")

    def make_repo(self, tmp: Path) -> Path:
        (tmp / "research.conf.py").write_text('PROJECT = "probe"\nDOCS = []\n')
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        return tmp

    def hooks(self, repo: Path) -> Path:
        return repo / ".git" / "hooks"

    def test_every_trigger_is_written_executable_and_identical(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            result = run("init-hook", cwd=repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            written = {}
            for trigger in self.TRIGGERS:
                path = self.hooks(repo) / trigger
                self.assertTrue(path.is_file(), f"{trigger} was not written")
                self.assertTrue(os.access(path, os.X_OK), f"{trigger} is not executable")
                written[trigger] = path.read_text(encoding="utf-8")
            self.assertEqual(len(set(written.values())), 1,
                             "the three triggers hold different sources")

    def test_a_foreign_file_refuses_and_writes_nothing(self) -> None:
        """The refusal leaves the repository exactly as it found it."""

        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            foreign = self.hooks(repo) / "post-merge"
            foreign.parent.mkdir(parents=True, exist_ok=True)
            foreign.write_text("#!/bin/sh\necho somebody else's hook\n")
            result = run("init-hook", cwd=repo)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("post-merge", result.stderr)
            self.assertIn("somebody else", foreign.read_text(encoding="utf-8"))
            for trigger in ("post-commit", "post-checkout"):
                self.assertFalse(
                    (self.hooks(repo) / trigger).exists(),
                    f"{trigger} was written although the install was refused")

    def test_a_partial_install_is_completed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            self.assertEqual(run("init-hook", cwd=repo).returncode, 0)
            (self.hooks(repo) / "post-checkout").unlink()
            result = run("init-hook", cwd=repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("post-checkout", result.stdout)
            self.assertTrue((self.hooks(repo) / "post-checkout").is_file())

    def test_a_complete_install_reports_itself_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            self.assertEqual(run("init-hook", cwd=repo).returncode, 0)
            stamps = {t: (self.hooks(repo) / t).stat().st_mtime_ns
                      for t in self.TRIGGERS}
            result = run("init-hook", cwd=repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already installed", result.stdout)
            for trigger, stamp in stamps.items():
                self.assertEqual((self.hooks(repo) / trigger).stat().st_mtime_ns,
                                 stamp, f"{trigger} was rewritten")

    def install(self, repo: Path, trigger: str, body: str) -> Path:
        path = self.hooks(repo) / trigger
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_an_installation_of_an_earlier_body_is_upgraded(self) -> None:
        """The population this fix has to reach, and the one it nearly missed.

        Every repository that has this hook today has an earlier body on
        `post-commit` alone. Read as somebody else's file it is refused, and
        then `init-hook` upgrades nobody: the only installations that could
        ever hold three triggers would be the ones that do not exist yet.
        """

        module = load_publish()
        for index, earlier in enumerate(module.SUPERSEDED_HOOKS):
            with self.subTest(body=index):
                with tempfile.TemporaryDirectory() as raw:
                    repo = self.make_repo(Path(raw))
                    self.install(repo, "post-commit", earlier)
                    result = run("init-hook", cwd=repo)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("upgraded", result.stdout)
                    for trigger in self.TRIGGERS:
                        path = self.hooks(repo) / trigger
                        self.assertTrue(path.is_file(), f"{trigger} was not written")
                        self.assertTrue(os.access(path, os.X_OK),
                                        f"{trigger} is not executable")
                        self.assertEqual(path.read_text(encoding="utf-8"),
                                         module.HOOK,
                                         f"{trigger} still holds the earlier body")

    def test_a_body_one_byte_off_ours_is_foreign_and_writes_nothing(self) -> None:
        """What keeps the migration from becoming a licence to clobber.

        The near miss is the case worth pinning: a file that is an earlier
        body with one character changed is not a file this pack wrote, and a
        test that only used an obviously foreign script could not tell a byte
        comparison from a heuristic.
        """

        module = load_publish()
        # Through `init-hook`, which every body carries and every body will:
        # it is the phrase that names the verb that writes the file. A probe
        # keyed on a word only some bodies contain -- `build/` was, until
        # `9ff7c73c` removed the folder -- goes vacuous on the day a new body
        # arrives without it, and a probe that stops probing while still
        # passing is worse than no probe.
        nearly = module.SUPERSEDED_HOOKS[-1].replace("init-hook", "init- hook", 1)
        self.assertIn("init-hook", module.SUPERSEDED_HOOKS[-1])
        self.assertNotEqual(nearly, module.SUPERSEDED_HOOKS[-1])
        with tempfile.TemporaryDirectory() as raw:
            repo = self.make_repo(Path(raw))
            held = self.install(repo, "post-commit", nearly)
            result = run("init-hook", cwd=repo)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("post-commit", result.stderr)
            self.assertEqual(held.read_text(encoding="utf-8"), nearly,
                             "a foreign post-commit hook was overwritten")
            for trigger in ("post-merge", "post-checkout"):
                self.assertFalse(
                    (self.hooks(repo) / trigger).exists(),
                    f"{trigger} was written although the install was refused")

    def test_every_superseded_body_is_one_this_pack_released(self) -> None:
        """The list is a claim about history, so it is checked against history.

        These digests are of the `HOOK` value at every revision that has
        carried one, read out with
        `git rev-list --all --full-history -- bin/sd_research_publish.py`.
        Recorded rather than recomputed from the file: the point is to catch
        the day a reformat, a stripped trailing space or a tidy-up edits a
        body here, which would leave the constant self-consistent and stop it
        matching the bytes on any machine.

        A commit that changes `HOOK` adds the body it replaces, and this set
        is where it is noticed if it does not. The two from this branch are
        here because a branch owns the predecessors it creates: a review ran
        the installer against the base revision's own hook and it was refused.
        The fifth is sd:1352's, which this branch merges rather than wrote: a
        merge inherits the other branch's predecessors, because its revisions
        become checkouts somebody can run `init-hook` from here. The sixth is
        the merge commit's own body.

        "Released" is the tempting reading of this set and it is the wrong
        one. Three of these six name commits that never reached `main`. What
        puts a body on a machine is a checkout, and an ancestor commit is a
        checkout.
        """

        module = load_publish()
        released = {
            # `05d4b9d3`, "Publish finished documents to the dashboard by
            # default (#1089)" -- the revision that added the hook.
            "ba26cc1e36a2b84455a83be8f620118c900af468c02661ad9ff9177fad1bb95c",
            # `fa5f5576`, "publish: add Google Drive as a destination, on
            # Notion's terms (#1091)", still the body at `47d4d147`.
            "01f8e364291b382813e7eee648ca36033880feca5b7f7b9afcb61a0ea1f9223b",
            # `4d20574b`, this branch: three triggers, one source.
            "6d81532bca2b30dabfdedee4a5bd022c8eb0f181599d0580753b825d665cfb89",
            # `dd14d501`, this branch: a file checkout measured against the
            # working tree, replaced by `8b575695`.
            "aae11a2a3b271585039918aad528278ff5aebf617dd32a1ca52947d5114b7a3e",
            # `e65d1ec4`, sd:1352's branch, merged into this one: the same
            # three triggers reached independently. Its shape is gone from the
            # merged tree and its body is not, because those revisions are
            # ancestors here and a checkout of one can install it.
            "aac6c83677dc4cf11b778e012afdec33c126107efd34935ac2c1ca6de6271b0c",
            # This branch's merge commit, superseded by the commit that gave
            # the hook `docs/dashboard/` and an explicit all-zeros guard.
            "8f3224d961a7d0394c47648684d9e5ad5c6ce85b7f563917e8f9e6b76881afb7",
            # `91d26285`, the body on main before sd:1376 declined to render
            # a research.conf.py the pull or checkout had just brought in.
            "26e6f156735dd19389a05eca4b13ac844cd540d121f1b0a2a2d45142a3ef1acb",
            # `fbd69cd5`, sd:1376's first body, which asked git only whether
            # the operation itself changed research.conf.py.
            "d4a5e7a9fb5df3aca9b3288c1e6acbc2c4da5e1bbbc4dc60d7e503c01758ee21",
            # `c2b10b20`, sd:1376's second body, which read the render record
            # after a pull or a checkout and exempted post-commit.
            "4470626f28b31de26aa04dae1e88b02b780e3ba75f30197ecaaac8a741a93336",
            # `e81f7fc0`, sd:1376's third body, which rendered a branch
            # checkout behind its upstream and queued the stale tree (sd:1991).
            "64d1cce850343632604289dcd70ba9cd7df4e94c7fcea688992b94fb1527cebb",
            # `8b2b4f07`, sd:1991's body, which rendered and checked no links
            # (sd:1840).
            "aaaa4cdc91ce1da64ae73f7138cb538010e9fc5e2f5ca0b6dcbd3ebe8b62d808",
        }
        digests = {hashlib.sha256(body.encode("utf-8")).hexdigest()
                   for body in module.SUPERSEDED_HOOKS}
        self.assertEqual(digests, released)
        self.assertNotIn(hashlib.sha256(module.HOOK.encode("utf-8")).hexdigest(),
                         released, "the current body is listed as superseded")


class RenderManifestTests(unittest.TestCase):
    """`render` writes docs/dashboard/research.json (pack sd:5658).

    The dashboard never executes research.conf.py, so a config that builds
    DOCS with code is unreadable there; the manifest is what the render
    executed, named by the sha256 of the bytes it ran.
    """

    CONF = (
        'import pathlib\n'
        'PROJECT = "Probe"\n'
        'DOCS = [dict(src=name + ".md", out=name, title=name.title(), eyebrow=42,\n'
        '             stand="One line.", links=[("Spec", "https://example.test/spec"), ("bad", 3)])\n'
        '        for name in ("one", "two")]\n'
    )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        for name in ("one", "two"):
            (self.repo / f"{name}.md").write_text("# T\n\nBody.\n", encoding="utf-8")
        (self.repo / "research.conf.py").write_text(self.CONF, encoding="utf-8")
        self.manifest = self.repo / "docs" / "dashboard" / "research.json"

    def render(self):
        """Run the real `render` in-process; publication is stubbed, so nothing leaves the directory."""

        load_publish()

        class Converter:
            def __init__(self, **_options: object) -> None:
                self.toc_tokens: list = []

            def convert(self, _text: str) -> str:
                return "<p>Body.</p>"

        fake = types.ModuleType("markdown")
        fake.Markdown = Converter  # type: ignore[attr-defined]
        with unittest.mock.patch.dict(sys.modules, {"markdown": fake}):
            render = load_kit().load("sd_research_render")
        here = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, here)
        clean = {k: v for k, v in os.environ.items() if k not in ("SD_RESEARCH_TRIGGER", "GIT_EXEC_PATH")}
        with unittest.mock.patch.dict(os.environ, clean, clear=True), \
                unittest.mock.patch.object(render, "publish", unittest.mock.Mock(return_value=[])), \
                unittest.mock.patch("sys.stdout", io.StringIO()):
            return render.main()

    def test_render_writes_the_manifest_of_what_it_executed(self) -> None:
        source = (self.repo / "research.conf.py").read_bytes()
        self.assertEqual(self.render(), 0)
        link = [["Spec", "https://example.test/spec"]]
        self.assertEqual(json.loads(self.manifest.read_text(encoding="utf-8")), {
            "version": 1, "project": "Probe", "conf_sha256": hashlib.sha256(source).hexdigest(),
            "docs": [{"src": "one.md", "out": "one", "title": "One", "stand": "One line.", "links": link,
                      "notion": False, "drive": False},
                     {"src": "two.md", "out": "two", "title": "Two", "stand": "One line.", "links": link,
                      "notion": False, "drive": False}]})
        self.assertEqual(sorted(p.name for p in self.manifest.parent.iterdir()),
                         ["one.html", "research.json", "two.html"])

    def test_a_designation_set_in_a_loop_is_a_flag_per_destination(self) -> None:
        """pack sd:5936: the dashboard reads designations here, never by running the config (system sd:5898).

        A destination is designated as `publish` reads it: `True` or a dict,
        an empty one too. None, False and a value it refuses are not.
        """
        (self.repo / "research.conf.py").write_text(
            self.CONF + 'for _doc in DOCS:\n    _doc["notion"] = dict()\n'
            'DOCS[1]["drive"] = True\nDOCS[0]["drive"] = "a folder"\n', encoding="utf-8")
        self.assertEqual(self.render(), 0)
        docs = json.loads(self.manifest.read_text(encoding="utf-8"))["docs"]
        self.assertEqual([(doc["notion"], doc["drive"]) for doc in docs], [(True, False), (True, True)])

    def test_the_digest_is_of_the_bytes_executed_not_the_file_after(self) -> None:
        """A config that rewrites itself while it runs: the manifest names what ran."""

        conf = self.repo / "research.conf.py"
        conf.write_text(self.CONF + 'pathlib.Path("research.conf.py").write_text("DOCS = []\\n")\n', encoding="utf-8")
        executed = conf.read_bytes()
        self.render()
        digest = json.loads(self.manifest.read_text(encoding="utf-8"))["conf_sha256"]
        self.assertEqual(digest, hashlib.sha256(executed).hexdigest())
        self.assertNotEqual(digest, hashlib.sha256(conf.read_bytes()).hexdigest())

    def test_a_render_stopped_before_the_manifest_leaves_the_last_one(self) -> None:
        """Pages first, manifest last: a failed page keeps the older manifest,
        whose digest the dashboard reads as an older render; the next render replaces it."""

        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text('{"version": 1, "conf_sha256": "old"}\n', encoding="utf-8")
        (self.repo / "two.md").unlink()
        with self.assertRaises(FileNotFoundError):
            self.render()
        self.assertEqual(json.loads(self.manifest.read_text(encoding="utf-8"))["conf_sha256"], "old")
        (self.repo / "two.md").write_text("Body.\n", encoding="utf-8")
        self.render()
        self.assertNotEqual(json.loads(self.manifest.read_text(encoding="utf-8"))["conf_sha256"], "old")
        self.assertFalse(list(self.manifest.parent.glob("*.partial")))


class HookTriggerTests(unittest.TestCase):
    """Each trigger asks git its own question about what moved.

    Copying the post-commit body under the other two names would install a
    hook that never fires: `git diff-tree --no-commit-id -r HEAD` prints
    nothing at all for a merge commit, and a checkout's HEAD says nothing
    about what the checkout moved. These run the installed files the way git
    runs them -- by name, with git's own argv -- rather than reading the
    source for the strings.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "research.conf.py").write_text('PROJECT = "probe"\nDOCS = []\n')
        self.git("init", "-q")
        self.git("config", "user.email", "probe@example.invalid")
        self.git("config", "user.name", "probe")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "doc.md").write_text("one\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "first")
        installed = run("init-hook", cwd=self.repo)
        self.assertEqual(installed.returncode, 0, installed.stderr)

        # The render is a stub on PATH that records its argv. The hook finds
        # the kit with `shutil.which`, so this is the whole seam, and the real
        # kit is never run against a two-file temporary repository.
        self.stub = self.root / "stub"
        self.stub.mkdir()
        self.marker = self.root / "rendered"
        kit = self.stub / "sd-research-kit"
        kit.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "with pathlib.Path(%r).open('a') as handle:\n"
            "    handle.write(' '.join(sys.argv[1:]) + chr(10))\n"
            "if sys.argv[1:] == ['checklinks']:\n"
            "    sys.exit(1 if os.environ.get('PROBE_BROKEN_LINKS') else 0)\n"
            "with pathlib.Path(%r).open('a') as handle:\n"
            "    handle.write(os.environ.get('SD_RESEARCH_TRIGGER', '-') + chr(10))\n"
            "with pathlib.Path(%r).open('a') as handle:\n"
            "    handle.write(os.environ.get('SD_SKIP_MIRROR', '-') + chr(10))\n"
            % (str(self.marker), str(self.root / "triggers"), str(self.root / "holds")))
        kit.chmod(0o755)
        # The stub records nothing, so the record a real render leaves is
        # written here: the config this repository started with has been
        # rendered once, by hand, which is what `init-hook` asks for.
        self.trust()

    def trust(self) -> None:
        """Record the tree's config as rendered, as `sd-research-kit render` does."""

        source = (self.repo / "research.conf.py").read_bytes()
        record = self.git("rev-parse", "--git-path", "sd-research-conf.sha256").stdout.strip()
        (self.repo / record).write_text(hashlib.sha256(source).hexdigest() + "\n")

    def git(self, *args: str) -> subprocess.CompletedProcess:
        # Every git call here runs with the hook switched off: the hooks are
        # installed in this repository, and a `git merge` that rendered on its
        # own would make the assertions below about git's run, not the hook's.
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True,
            capture_output=True, text=True,
            env=dict(os.environ, SD_SKIP_RENDER="1"),
        )

    def sha(self, rev: str = "HEAD") -> str:
        return self.git("rev-parse", rev).stdout.strip()

    def fire(self, trigger: str, *args: str, skip: bool = False):
        hook = self.repo / ".git" / "hooks" / trigger
        env = dict(os.environ,
                   PATH=os.pathsep.join([str(self.stub), os.environ["PATH"]]))
        env.pop("SD_SKIP_RENDER", None)
        if skip:
            env["SD_SKIP_RENDER"] = "1"
        return subprocess.run([sys.executable, str(hook), *args],
                              cwd=self.repo, env=env,
                              capture_output=True, text=True)

    def calls(self) -> list[str]:
        """Every kit verb the hook ran, in order."""
        if not self.marker.exists():
            return []
        return self.marker.read_text(encoding="utf-8").split()

    def renders(self) -> list[str]:
        return [verb for verb in self.calls() if verb == "render"]

    def triggers(self) -> list[str]:
        path = self.root / "triggers"
        return path.read_text(encoding="utf-8").split() if path.exists() else []

    def executions(self) -> int:
        path = self.root / "executed"
        return len(path.read_text(encoding="utf-8").split()) if path.exists() else 0

    def a_second_commit(self) -> None:
        (self.repo / "doc.md").write_text("two\n")
        self.git("commit", "-qam", "second")

    def test_post_commit_renders_the_commit_that_carried_a_document(self) -> None:
        self.a_second_commit()
        result = self.fire("post-commit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), ["render"])

    def test_the_render_is_followed_by_checklinks(self) -> None:
        """sd:1840: a link check that runs only by hand stops running.

        A renamed document leaves every link to it pointing nowhere, and the
        render does not notice. The hook runs the check after each render.
        """

        self.a_second_commit()
        result = self.fire("post-commit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["render", "checklinks"])

    def test_broken_links_are_reported_and_git_is_left_alone(self) -> None:
        self.a_second_commit()
        with unittest.mock.patch.dict(os.environ, PROBE_BROKEN_LINKS="1"):
            result = self.fire("post-commit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("post-commit: checklinks found broken links", result.stderr)

    def test_a_declined_render_checks_no_links(self) -> None:
        self.a_config_change()
        self.assert_declined(self.fire("post-commit"))
        self.assertEqual(self.calls(), [])

    def test_post_merge_reads_orig_head_and_not_the_merge_commit(self) -> None:
        """The defect a copied post-commit body would have.

        A merge commit has two parents, so `diff-tree --no-commit-id -r HEAD`
        suppresses the combined diff and prints nothing -- asserted here, so
        that a future edit back to that question fails rather than going
        quiet. `ORIG_HEAD` is where this branch stood before the merge.
        """

        base = self.sha()
        self.git("checkout", "-q", "-b", "side")
        (self.repo / "other.md").write_text("side\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "on the side")
        self.git("checkout", "-q", "-")
        self.a_second_commit()
        self.git("merge", "-q", "--no-ff", "--no-edit", "side")
        self.assertNotEqual(self.sha(), base)
        empty = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
        self.assertEqual(empty.stdout.strip(), "",
                         "diff-tree named files for a merge commit; the premise moved")
        result = self.fire("post-merge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), ["render"])

    def test_post_checkout_renders_a_branch_switch_that_moved_a_document(self) -> None:
        before = self.sha()
        self.a_second_commit()
        result = self.fire("post-checkout", before, self.sha(), "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), ["render"])

    def holds(self) -> list[str]:
        """What each render was told about holding its mirrors, `-` for nothing."""
        path = self.root / "holds"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def track(self, upstream: str) -> None:
        """Make the current branch track the local branch `upstream`, so
        `@{u}` resolves with no remote and no network."""
        branch = self.git("branch", "--show-current").stdout.strip()
        self.git("config", "branch.%s.remote" % branch, ".")
        self.git("config", "branch.%s.merge" % branch, "refs/heads/%s" % upstream)

    def test_post_checkout_behind_upstream_renders_and_holds_the_mirrors(self) -> None:
        """sd:1991: switching onto a branch its upstream has moved past.

        The reflog that found it: a checkout of `main` two seconds before
        `pull --ff-only`. The checkout rendered the stale tree and queued it
        for the shared mirror; a drain in those two seconds publishes it. The
        dashboard still wants the render, the queue does not want the text.
        """

        before = self.sha()
        self.a_second_commit()
        here = self.sha()
        self.git("branch", "ahead")
        self.git("checkout", "-q", "ahead")
        (self.repo / "doc.md").write_text("three\n")
        self.git("commit", "-qam", "third")
        self.git("checkout", "-q", "-")
        self.track("ahead")
        self.assertEqual(self.git("rev-list", "--count", "HEAD..@{u}").stdout.strip(), "1")
        result = self.fire("post-checkout", before, here, "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), ["render"])
        self.assertEqual(len(self.holds()), 1, self.holds())
        self.assertIn("behind", self.holds()[0])

    def test_post_checkout_level_with_upstream_queues_as_before(self) -> None:
        before = self.sha()
        self.a_second_commit()
        self.git("branch", "level")
        self.track("level")
        result = self.fire("post-checkout", before, self.sha(), "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), ["render"])
        self.assertEqual(self.holds(), ["-"])

    def test_post_checkout_without_an_upstream_queues_as_before(self) -> None:
        before = self.sha()
        self.a_second_commit()
        result = self.fire("post-checkout", before, self.sha(), "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.holds(), ["-"])

    def test_post_checkout_renders_a_file_checkout(self) -> None:
        """A document can change without HEAD moving, and usually on purpose.

        `git checkout <rev> -- doc.md` is how a page is reverted. git fires
        post-checkout with flag 0 and the same revision twice, so there is no
        range to ask about -- and skipping on that ground leaves the mirror
        holding the text the checkout just took away. The argv here is git's
        own, read off a real file checkout.
        """

        self.a_second_commit()
        here = self.sha()
        self.git("checkout", "-q", "HEAD~1", "--", "doc.md")
        self.assertEqual((self.repo / "doc.md").read_text(), "one\n")
        self.assertEqual(self.sha(), here, "the file checkout moved HEAD")
        result = self.fire("post-checkout", here, here, "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), ["render"])

    def test_restoring_head_after_a_revert_renders_again(self) -> None:
        """The clean tree that used to mean "nothing to do".

        Two file checkouts in a row. The first publishes the older text, so
        the published copy is now behind HEAD. The second restores HEAD and
        leaves a tree with no diff against it -- and a guard reading that diff
        concludes there is nothing to render, which freezes the reverted text
        in the mirror for good. A clean tree is a statement about HEAD, and
        the mirror is not a copy of HEAD.
        """

        self.a_second_commit()
        here = self.sha()
        self.git("checkout", "-q", "HEAD~1", "--", "doc.md")
        self.assertEqual(self.fire("post-checkout", here, here, "0").returncode, 0)
        self.git("checkout", "-q", "HEAD", "--", "doc.md")
        self.assertEqual((self.repo / "doc.md").read_text(), "two\n")
        self.assertEqual(
            self.git("diff", "--name-only", "HEAD").stdout.strip(), "",
            "the restored tree still differs from HEAD; the premise moved")
        result = self.fire("post-checkout", here, here, "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), ["render", "render"],
                         "the render that puts HEAD's text back never ran")

    def a_config_change(self) -> None:
        # A config that leaves a mark when it runs, so a test can tell
        # whether anything executed it.
        (self.repo / "research.conf.py").write_text(
            "import pathlib\n"
            "with pathlib.Path(%r).open('a') as handle:\n"
            "    handle.write('ran' + chr(10))\n"
            'PROJECT = "incoming"\nDOCS = []\n' % str(self.root / "executed"))
        self.git("commit", "-qam", "config from elsewhere")

    def assert_declined(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), [])
        self.assertIn("research.conf.py", result.stderr)
        self.assertIn("sd-research-kit render", result.stderr)

    def test_a_pull_that_brings_in_a_config_does_not_execute_it(self) -> None:
        """sd:1376. `render` executes research.conf.py. A post-merge arm that
        rendered on every pull ran a config the operator never read, from a
        tree they did not write, with no command of theirs in between."""

        self.git("checkout", "-q", "-b", "side")
        self.a_config_change()
        self.git("checkout", "-q", "-")
        self.a_second_commit()
        self.git("merge", "-q", "--no-ff", "--no-edit", "side")
        self.assert_declined(self.fire("post-merge"))

    def test_a_branch_switch_that_changes_the_config_does_not_execute_it(self) -> None:
        before = self.sha()
        self.a_config_change()
        self.assert_declined(self.fire("post-checkout", before, self.sha(), "1"))

    def test_a_file_checkout_of_the_config_does_not_execute_it(self) -> None:
        self.git("checkout", "-q", "-b", "side")
        self.a_config_change()
        self.git("checkout", "-q", "-")
        here = self.sha()
        self.git("checkout", "-q", "side", "--", "research.conf.py")
        self.assert_declined(self.fire("post-checkout", here, here, "0"))

    def a_declined_pull_of_a_config(self) -> None:
        self.git("checkout", "-q", "-b", "side")
        self.a_config_change()
        self.git("checkout", "-q", "-")
        self.a_second_commit()
        self.git("merge", "-q", "--no-ff", "--no-edit", "side")
        self.assert_declined(self.fire("post-merge"))

    def test_a_file_checkout_after_a_declined_pull_does_not_execute_the_config(self) -> None:
        """The #1376 review. The pull is declined; `git checkout HEAD -- doc.md`
        then finds the tree's config equal to HEAD's. An arm that asked git
        only about its own operation read that as nothing to decline, and
        executed the config the pull had brought in."""

        self.a_declined_pull_of_a_config()
        here = self.sha()
        (self.repo / "doc.md").write_text("scratch\n")
        self.git("checkout", "-q", "HEAD", "--", "doc.md")
        self.assert_declined(self.fire("post-checkout", here, here, "0"))

    def test_a_later_pull_that_leaves_the_config_alone_does_not_execute_it(self) -> None:
        self.a_declined_pull_of_a_config()
        self.git("checkout", "-q", "-b", "later")
        (self.repo / "doc.md").write_text("later\n")
        self.git("commit", "-qam", "later")
        self.git("checkout", "-q", "-")
        self.git("merge", "-q", "--no-ff", "--no-edit", "later")
        self.assert_declined(self.fire("post-merge"))

    def record(self) -> Path:
        return self.repo / self.git(
            "rev-parse", "--git-path", "sd-research-conf.sha256").stdout.strip()

    def digest(self) -> str:
        return hashlib.sha256((self.repo / "research.conf.py").read_bytes()).hexdigest()

    def real_render(self, **env: str):
        """Run the real `render` in-process, as the kit's `render` verb does."""

        spy, stop, _ = self.by_verb("render", **env)
        return spy, stop

    def by_verb(self, verb: str, **env: str):
        """Run `verb`'s read of research.conf.py in-process, as the kit does.

        `render` and `publish` run whole; `review` runs `load_docs`, its one
        read of the config. `env` is added to an environment that carries
        neither marker of an automatic render, so a run with no `env` is a
        run by hand even when this suite itself runs under a git hook.
        Returns `trust_conf` as a spy, the exit raised or None, and the label
        the dashboard was given or None. Every write `publish()` makes is
        stubbed, so nothing reaches the machine's dashboard, vault or queue,
        and so is its read of the channels setting, so none reaches its
        database.
        """

        publish = load_publish()
        with unittest.mock.patch.dict(sys.modules, {"markdown": types.ModuleType("markdown")}):
            render = load_kit().load("sd_research_render")
        review = load_kit().load("sd_research_review")
        run_verb = {"render": render.main, "publish": publish.publish_main,
                    "review": lambda: review.load_docs(os.getcwd())}[verb]
        here = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, here)
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("SD_RESEARCH_TRIGGER", "GIT_EXEC_PATH")}
        spy = unittest.mock.Mock(wraps=publish.trust_conf)
        register = unittest.mock.Mock(return_value="")
        # DOCS is empty, so nothing is converted: an empty module stands in
        # for python-markdown, which the kit re-executes to find.
        with unittest.mock.patch.dict(os.environ, dict(clean, **env), clear=True), \
                unittest.mock.patch.multiple(
                    publish, trust_conf=spy, register_root=register,
                    write_obsidian=unittest.mock.Mock(return_value=[]),
                    ignore_dashboard=unittest.mock.Mock(return_value=""),
                    migrate_legacy_queue=unittest.mock.Mock(return_value=[]),
                    enqueue=unittest.mock.Mock(return_value=[]),
                    read_channels=unittest.mock.Mock(return_value=(None, ""))), \
                unittest.mock.patch("sys.stdout", io.StringIO()):
            label = None
            try:
                run_verb()
            except SystemExit as stop:
                return spy, stop, label
            if register.called:
                label = register.call_args.args[1]
        return spy, None, label

    def test_publish_and_review_do_not_execute_an_unread_config(self) -> None:
        """sd:3513: `publish` and `review` execute the config as `render` does,
        so an automatic run of either refuses one no command by hand has
        executed, and records nothing."""

        trusted = self.record().read_text(encoding="utf-8")
        self.a_config_change()
        for verb in ("publish", "review"):
            with self.subTest(verb=verb):
                spy, stop, _ = self.by_verb(verb, SD_RESEARCH_TRIGGER="post-merge")
                self.assertIsNotNone(stop)
                self.assertIn("sd-research-kit render", str(stop.code))
                spy.assert_not_called()
        self.assertEqual(self.executions(), 0)
        self.assertEqual(self.record().read_text(encoding="utf-8"), trusted)

    def test_publish_and_review_by_hand_record_the_config(self) -> None:
        """A `publish` or `review` by hand executes the config, so it records
        it as read, and a later hook render renders it."""

        self.a_config_change()
        for verb in ("publish", "review"):
            with self.subTest(verb=verb):
                with (self.repo / "research.conf.py").open("a") as handle:
                    handle.write("# read before %s\n" % verb)
                self.record().unlink(missing_ok=True)
                _, stop, _ = self.by_verb(verb)
                self.assertIsNone(stop)
                self.assertEqual(self.record().read_text(encoding="utf-8").strip(), self.digest())

    def test_render_and_publish_give_the_dashboard_one_label(self) -> None:
        """sd:3513: with no PROJECT, render and publish each fell back to a
        label of their own, so alternating them relabelled the row on every
        run. `publish()` owns the one fallback."""

        (self.repo / "research.conf.py").write_text("DOCS = []\n")
        self.trust()
        labels = [self.by_verb(verb)[2] for verb in ("render", "publish")]
        self.assertEqual(labels[0], labels[1])
        self.assertEqual(labels[0], "REPO")

    def test_an_unrelated_commit_after_a_declined_pull_does_not_execute_the_config(self) -> None:
        """The second #1376 review. The pull is declined; the operator then
        commits nothing but doc.md. A post-commit arm exempt from the record
        read that commit as approval of the whole tree, executed the config
        the pull had brought in, and the render recorded it as read."""

        trusted = self.record().read_text(encoding="utf-8")
        self.a_declined_pull_of_a_config()
        (self.repo / "doc.md").write_text("mine\n")
        self.git("commit", "-qam", "unrelated")
        self.assert_declined(self.fire("post-commit"))
        self.assertEqual(self.record().read_text(encoding="utf-8"), trusted)

    def test_a_config_commit_renders_only_after_a_render_by_hand(self) -> None:
        """A commit is not a render: the committed config is executed by the
        hook only once a render by hand has executed it."""

        self.a_config_change()
        self.assert_declined(self.fire("post-commit"))

    def test_a_config_rendered_by_hand_renders_from_then_on(self) -> None:
        """Only a render by hand records a config as read, and from then on
        every automatic trigger renders it."""

        self.a_declined_pull_of_a_config()
        spy, stop = self.real_render()
        self.assertIsNone(stop)
        self.assertEqual(self.executions(), 1)
        spy.assert_called_once()
        self.assertEqual(self.record().read_text(encoding="utf-8").strip(), self.digest())
        here = self.sha()
        result = self.fire("post-checkout", here, here, "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), ["render"])
        (self.repo / "doc.md").write_text("mine\n")
        self.git("commit", "-qam", "after the render")
        result = self.fire("post-commit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), ["render", "render"])

    def test_the_hook_tells_the_render_it_is_automatic(self) -> None:
        """The render checks the bytes it executes, which the hook cannot:
        the config can change between the hook's read and the render's."""

        self.a_second_commit()
        result = self.fire("post-commit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.triggers(), ["post-commit"])

    def test_an_automatic_render_does_not_execute_an_unread_config(self) -> None:
        trusted = self.record().read_text(encoding="utf-8")
        self.a_config_change()
        spy, stop = self.real_render(SD_RESEARCH_TRIGGER="post-merge")
        self.assertIsNotNone(stop)
        self.assertNotIn(stop.code, (0, None))
        self.assertEqual(self.executions(), 0)
        spy.assert_not_called()
        self.assertEqual(self.record().read_text(encoding="utf-8"), trusted)
        # `sys.exit` with a message: python prints it on the way out.
        self.assertIn("sd-research-kit render", str(stop.code))

    def test_a_render_git_runs_does_not_execute_an_unread_config(self) -> None:
        """An earlier hook body, still installed, runs the render with no
        marker of its own. Git sets GIT_EXEC_PATH for every hook it runs."""

        trusted = self.record().read_text(encoding="utf-8")
        self.a_config_change()
        spy, stop = self.real_render(GIT_EXEC_PATH="/usr/libexec/git-core")
        self.assertIsNotNone(stop)
        self.assertEqual(self.executions(), 0)
        spy.assert_not_called()
        self.assertEqual(self.record().read_text(encoding="utf-8"), trusted)

    def test_an_automatic_render_of_a_read_config_records_nothing(self) -> None:
        self.a_config_change()
        self.real_render()
        for marker in ({"SD_RESEARCH_TRIGGER": "post-commit"},
                       {"GIT_EXEC_PATH": "/usr/libexec/git-core"}):
            spy, stop = self.real_render(**marker)
            self.assertIsNone(stop)
            spy.assert_not_called()
        self.assertEqual(self.executions(), 3)

    def test_a_checkout_with_no_record_does_not_execute_the_config(self) -> None:
        """A repository whose config no render has recorded, such as one the
        previous hook body rendered: its config was never shown to be read."""

        self.record().unlink()
        here = self.sha()
        self.assert_declined(self.fire("post-checkout", here, here, "0"))

    def test_a_render_records_the_config_it_executed(self) -> None:
        """The record the arms above read is written by the render itself, so
        a render by hand is what lets a pulled config render again."""

        self.record().unlink()
        self.a_config_change()
        _, stop = self.real_render()
        self.assertIsNone(stop)
        self.assertEqual(self.record().read_text(encoding="utf-8").strip(), self.digest())

    def test_the_hook_reads_the_record_render_writes(self) -> None:
        module = load_publish()
        self.assertIn('TRUSTED = "%s"' % module.TRUSTED_CONF, module.HOOK)
        self.assertIn('TRIGGER_ENV = "%s"' % module.TRIGGER_ENV, module.HOOK)

    def test_post_checkout_renders_when_no_branch_moved(self) -> None:
        """`git checkout <the branch already checked out>`, or `-b`.

        Equal revisions leave no range, and this falls into the same arm as a
        file checkout and renders. A branch that did not move is not a
        statement that the published copy matches it, for the same reason a
        clean tree is not; keeping a separate answer for it would be the
        branch-identity special case coming back in a third costume.
        """

        self.a_second_commit()
        here = self.sha()
        result = self.fire("post-checkout", here, here, "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), ["render"])

    def test_an_unrecognised_trigger_renders_nothing(self) -> None:
        """A name nobody installed this under is not a guess to make."""

        self.a_second_commit()
        foreign = self.repo / ".git" / "hooks" / "post-rewrite"
        foreign.write_bytes((self.repo / ".git" / "hooks" / "post-commit").read_bytes())
        foreign.chmod(0o755)
        result = self.fire("post-rewrite")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.renders(), [])

    def test_sd_skip_render_skips_every_trigger(self) -> None:
        self.a_second_commit()
        before = self.sha("HEAD~1")
        self.assertEqual(self.fire("post-commit", skip=True).returncode, 0)
        self.assertEqual(
            self.fire("post-checkout", before, self.sha(), "1", skip=True).returncode, 0)
        self.assertEqual(self.renders(), [])


if __name__ == "__main__":
    unittest.main()
