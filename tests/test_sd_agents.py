"""The `agents/` tree and how the installer renders it.

Agents are the taxonomy's third kind: a bounded worker with context isolation
and a declared tool set. The declaration is the whole governance mechanism --
an agent whose prose says "read-only" and whose frontmatter says nothing is an
agent the platform will hand every tool it has -- so the contract is asserted
here rather than trusted.

The fold that brought these five in is exactly where that could have been lost.
Their upstream templates carried no `tools:` at all for two of the five; the
governed versions existed only as files somebody had edited in place under
`~/.claude/agents`, which the next install from upstream would have overwritten.
These tests are what makes that a red check instead of a quiet regression.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS = REPO_ROOT / "agents"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "sd_install_agents", REPO_ROOT / "bin" / "sd_install.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sd_install = load_module()
#: The shipped table, captured before any test patches it.
PREDECESSORS = dict(sd_install.AGENT_PREDECESSORS)

# Tools that can change the working tree. An agent describing itself as
# read-only must not hold one; `Bash` is deliberately not in this set, because
# it is how a reviewer runs `cargo check` and excluding it would either fail a
# correct agent or push the check into prose.
WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})


def frontmatter(text: str) -> tuple[dict[str, str], list[str]]:
    """The leading `---` block as scalars plus the `tools:` list.

    The same flat-scalar subset the rest of the pack parses (D-C1), with one
    addition: `tools:` is the one list-valued key an agent carries, so it is
    read as a list here rather than pretending the file is flat.
    """

    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", "no frontmatter"
    fields: dict[str, str] = {}
    tools: list[str] = []
    in_tools = False
    for line in lines[1:]:
        if line.strip() == "---":
            return fields, tools
        if in_tools and line.lstrip().startswith("- "):
            tools.append(line.split("- ", 1)[1].strip())
            continue
        in_tools = False
        key, separator, value = line.partition(":")
        if separator and not key.startswith((" ", "\t")):
            fields[key.strip()] = value.strip()
            in_tools = key.strip() == "tools"
    raise AssertionError("unterminated frontmatter")


def agent_files() -> list[Path]:
    return sorted(AGENTS.glob("sd-*.md")) if AGENTS.is_dir() else []


VERIFIER = AGENTS / "sd-claim-verifier.md"
FACT_CHECK = REPO_ROOT / "skills" / "sd-fact-check" / "SKILL.md"

#: `- **name** -- meaning`, the shape both pages write each verdict in.
VERDICT_RE = re.compile(r"^[ \t]*-[ \t]+\*\*([a-z ]+)\*\*[ \t]+\u2014[ \t]+(.+?)(?=\n[ \t]*-[ \t]+\*\*|\n[ \t]*\n|\Z)",
                        re.MULTILINE | re.DOTALL)


def verdicts(page: Path, first: str, last: str) -> dict[str, str]:
    """The verdict definitions `page` writes between `first` and `last`.

    Whitespace is flattened: the two pages wrap at different indents, and a
    line break is not a difference in meaning. Everything else is compared
    byte for byte, trailing `;` and `.` included.
    """
    body = page.read_text(encoding="utf-8")
    window = body[body.index(first):body.index(last, body.index(first))]
    return {name: " ".join(text.split()) for name, text in VERDICT_RE.findall(window)}


def skill_verdicts() -> dict[str, str]:
    return verdicts(FACT_CHECK, "Assign exactly one verdict", "Do not remove an audited claim")


def agent_verdicts() -> dict[str, str]:
    return verdicts(VERIFIER, "Exactly one verdict for this claim", "defines the same five")


class TheVerdictVocabularyIsOne(unittest.TestCase):
    """Requirement 13: the agent emits the five verdicts the skill requires.

    Nothing parsed either list, which is exactly why they drifted: the agent
    carried `supported`, `refuted`, `uncertain` and the skill carried five
    names sharing only `supported` with it. A parent running both over one
    claim set had to translate, and a translation nobody wrote down is where
    `refuted` and `contradicted` quietly stop meaning the same thing.

    The agent spells the definitions out instead of citing the skill. The
    installer copies `agents/**` verbatim into `~/.claude/agents`, and a
    worker started from there runs against another project with no
    `skills/sd-fact-check/` in it -- a citation resolves to nothing and the
    worker guesses. So this compares the meanings and not only the names: a
    copy that nothing checks is the drift this test exists to catch.

    Both sides are read from the pages. A third copy here would only move the
    drift, from between the two documents to between them and the test.
    """

    def test_the_skill_defines_five_verdicts(self) -> None:
        """The bound that keeps the equality below from passing on nothing."""
        self.assertEqual(len(skill_verdicts()), 5, sorted(skill_verdicts()))

    def test_the_agent_returns_exactly_the_skill_s_verdicts(self) -> None:
        self.assertEqual(agent_verdicts(), skill_verdicts())

    def test_the_agent_cites_no_path_for_the_meanings(self) -> None:
        """An installed copy cannot open a path relative to this checkout."""
        page = VERIFIER.read_text(encoding="utf-8")
        start = page.index("Exactly one verdict for this claim")
        # The list itself, not the paragraph under it: that paragraph names
        # the skill on purpose, as provenance. What must not appear is a path
        # a reader of the list has to open to know what a verdict means.
        window = page[start:page.index("\n\n", page.index("- **outdated**", start))]
        self.assertIn("**partially supported**", window)
        self.assertNotIn("skills/", window)


class ContractTests(unittest.TestCase):
    def test_there_are_agents_to_check(self) -> None:
        # Without this every assertion below passes over an empty list. The
        # bound is zero, not the count of the day: one agent is a legitimate
        # tree, and a test that fails on it would be asserting a roster.
        self.assertGreater(len(agent_files()), 0, "agents/ enumerated to nothing")

    def test_the_name_matches_the_file(self) -> None:
        for path in agent_files():
            with self.subTest(agent=path.name):
                fields, _ = frontmatter(path.read_text(encoding="utf-8"))
                self.assertEqual(fields.get("name"), path.stem)

    def test_every_agent_declares_tools(self) -> None:
        """The taxonomy's marker for this kind, and the reason for the fold's care."""

        for path in agent_files():
            with self.subTest(agent=path.name):
                _, tools = frontmatter(path.read_text(encoding="utf-8"))
                self.assertTrue(tools, f"{path.name} declares no tools")

    def test_a_read_only_agent_holds_no_write_tool(self) -> None:
        """Prose and frontmatter must agree about authority.

        Scoped to agents that say "read-only" about themselves, so it checks a
        claim the file makes rather than imposing one it never made.
        """

        checked = 0
        for path in agent_files():
            text = path.read_text(encoding="utf-8")
            fields, tools = frontmatter(text)
            if "read-only" not in fields.get("description", "").lower():
                continue
            checked += 1
            with self.subTest(agent=path.name):
                self.assertEqual(
                    sorted(WRITE_TOOLS & set(tools)),
                    [],
                    f"{path.name} calls itself read-only and can write",
                )
        self.assertGreater(checked, 0, "no read-only agent found to check")

    def test_no_agent_carries_the_command_marker(self) -> None:
        """`disable-model-invocation` is a command's key; an agent is dispatched."""

        for path in agent_files():
            with self.subTest(agent=path.name):
                fields, _ = frontmatter(path.read_text(encoding="utf-8"))
                self.assertNotIn("disable-model-invocation", fields)

    def test_no_agent_still_uses_the_retired_command_prefix(self) -> None:
        """`se-` as a name, not as three letters inside a hyphenated word.

        The framework's own name is the residue test's, which greps `agents/`
        with the rest of the governed tree.

        A bare substring search rejects `case-sensitive` and `false-positives`
        too -- the same false-positive class that turned up while surveying the
        vault for real callers. The lookbehind is what makes this a check for
        the retired prefix rather than for the letters.
        """

        retired = re.compile(r"(?<![\w-])se-[a-z0-9]")
        for path in agent_files():
            with self.subTest(agent=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(retired.search(text), f"{path.name} still names se-*")


class SliceBuilderTools(unittest.TestCase):
    """sd:2526, sd:3014. A builder runs gates that outlast one Bash call and
    skills its brief names; without `Skill` it cannot run `sd-review` as a
    skill. It runs tests in the foreground only, 10 minutes or less a run:
    `Monitor` stops at 30 minutes, builders sat idle about 3,400 minutes in
    7 days, and a `run_in_background` exit left idle builders unwoken for
    about 4 hours on 2026-10-10."""

    def test_the_slice_builder_holds_skill_and_waits_without_monitor(self) -> None:
        text = (AGENTS / "sd-slice-builder.md").read_text(encoding="utf-8")
        _, tools = frontmatter(text)
        self.assertIn("Skill", tools)
        self.assertNotIn("Monitor", tools)
        _, body = text.split("\n---\n", 1)
        self.assertIn("Run tests in the foreground only, in runs of 10 minutes or less", body)
        self.assertIn("Never use Bash `run_in_background`", body)
        self.assertNotIn("with `Monitor`", body)


class SliceBuilderDiskRules(unittest.TestCase):
    """sd:1792. Three builders failed `make check` with `No space left on
    device` after eleven worktrees' `target/` filled the system disk. The
    brief carries the build-size defaults, the free-space stop and the bulk
    root, because a builder reads its brief and not WORKFLOW.md."""

    def test_the_slice_builder_keeps_builds_small_and_stops_on_a_full_disk(self) -> None:
        _, body = (AGENTS / "sd-slice-builder.md").read_text(encoding="utf-8").split("\n---\n", 1)
        for needle in ("CARGO_INCREMENTAL=0", "cargo test -p", "df -h", "20 GiB", "sd.bulk_storage_root"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)


class SliceBuilderMergeSubject(unittest.TestCase):
    """sd:6342. Git's default merge subject names the branch and ran 77
    characters; enqueue refused it after review. The brief gives the short
    subject up front."""

    def test_the_slice_builder_merges_main_with_a_short_subject(self) -> None:
        _, body = (AGENTS / "sd-slice-builder.md").read_text(encoding="utf-8").split("\n---\n", 1)
        self.assertIn('git merge --no-ff -m "chore: merge origin/main" origin/main', body)
        self.assertNotIn("git merge --no-edit", body)


class SliceBuilderBodyTrailer(unittest.TestCase):
    """sd:6626. Build run 137 copied its commit's `Closes:` trailer into the
    PR body; enqueue refuses a body trailer (sd:3704) and takes items by
    `--item`, so the brief says the body carries none."""

    def test_the_slice_builder_keeps_closes_lines_out_of_the_body(self) -> None:
        _, body = (AGENTS / "sd-slice-builder.md").read_text(encoding="utf-8").split("\n---\n", 1)
        self.assertIn("No line starts with `Closes:`, `Item:` or `Delivers:`", body)


class SliceBuilderDecisionNote(unittest.TestCase):
    """sd:3112. A builder that skips an item for an operator decision writes a
    `question` note with `Option:` lines, which the dashboard shows as buttons;
    a free-text `Decision needed:` comment gave the operator nothing to click."""

    def test_the_slice_builder_records_a_decision_as_a_question_with_options(self) -> None:
        _, body = (AGENTS / "sd-slice-builder.md").read_text(encoding="utf-8").split("\n---\n", 1)
        self.assertIn("--kind question", body)
        self.assertIn("Option: <a>", body)
        self.assertNotIn("Decision needed", body)


class SliceBuilderPageShots(unittest.TestCase):
    """sd:6007. Misaligned dropdowns, missing help icons, a missing Park and
    counts in labels passed review and tests: a builder that touches a page
    captures it dark and light, desktop and phone width, and the lead looks
    before enqueue. The command the builder is told to run must exist."""

    def test_the_slice_builder_captures_touched_pages_for_the_lead(self) -> None:
        _, body = (AGENTS / "sd-slice-builder.md").read_text(encoding="utf-8").split("\n---\n", 1)
        for needle in ("dark and light, at desktop and phone width", "dashboard/dashboard.sh shots --out",
                       "Name each PNG in the report", "any page shots"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)
        launcher = (REPO_ROOT / "dashboard" / "dashboard.sh").read_text(encoding="utf-8")
        self.assertIn('  shots) shift; cmd_shots "$@" ;;', launcher)
        workflow = (REPO_ROOT / "WORKFLOW.md").read_text(encoding="utf-8")
        self.assertIn("the lead also looks at the builder's page shots", workflow)


class SliceBuilderClaimsItems(unittest.TestCase):
    """sd:5961. Items read `planning` while builders worked them: only the
    retired runner's assignment claim moved one to `in_progress`. A builder
    claims each item it takes, with the existing status verb."""

    def test_the_slice_builder_moves_each_item_it_takes_to_in_progress(self) -> None:
        _, body = (AGENTS / "sd-slice-builder.md").read_text(encoding="utf-8").split("\n---\n", 1)
        self.assertIn("sd task status N in_progress --reason", body)


class SliceBuilderBeat(unittest.TestCase):
    """sd:9915. Leads spawned builders with the Agent tool and registered no
    run; each builder's beat exited 1 and the builder treated it as a failure.
    A beat with no registered run is skipped, not a stop."""

    def test_the_slice_builder_skips_the_beat_when_no_run_is_registered(self) -> None:
        _, body = (AGENTS / "sd-slice-builder.md").read_text(encoding="utf-8").split("\n---\n", 1)
        self.assertIn("sd assignments beat N", body)
        self.assertIn("no open teammate run holds item", body)
        self.assertIn("skip every later beat", body)


class SliceBuilderTestPause(unittest.TestCase):
    """sd:3173. Lane gates timed out while builders ran their focused tests beside them;
    run through `sd gate run`, a builder's tests wait in the machine gate queue.
    sd:5409: in arrival order, bounded so the wait and the tests fit the 600 s foreground limit."""

    def test_the_slice_builder_runs_focused_tests_in_the_gate_queue(self) -> None:
        _, body = (AGENTS / "sd-slice-builder.md").read_text(encoding="utf-8").split("\n---\n", 1)
        self.assertIn("sd gate run --max-wait 240 --label", body)
        self.assertIn("every gate slot is held", body)
        self.assertIn("exits 75", body)
        self.assertNotIn("exits 69", body)


class SliceBuilderResultNote(unittest.TestCase):
    """pack sd:5963. The retired runner wrote each run's result to its item;
    a builder's report reaches only the lead's session, so the builder notes
    each item with its head, fail-first line and result lines before it reports."""

    def test_the_slice_builder_notes_each_item_before_it_reports(self) -> None:
        _, body = (AGENTS / "sd-slice-builder.md").read_text(encoding="utf-8").split("\n---\n", 1)
        step = body.split("\n8. ", 1)[1].split("\n\n", 1)[0]
        self.assertIn("sd task note N '<head>", step)
        for needle in ("fail-first line", "result lines", "one per item"):
            with self.subTest(needle=needle):
                self.assertIn(needle, step)
        self.assertLess(step.index("sd task note N"), step.index("Report, then freeze"))


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name).resolve()

    def install(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        rc = sd_install.main([*args, "--home", str(self.home)], out=out)
        return rc, out.getvalue()

    @property
    def receipt(self) -> dict:
        path = self.home / ".local" / "state" / "sd-ai-command-pack" / "installed.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_agents_land_verbatim_in_the_claude_agents_home(self) -> None:
        rc, _ = self.install("--user")
        self.assertEqual(rc, 0)
        for source in agent_files():
            target = self.home / ".claude" / "agents" / source.name
            with self.subTest(agent=source.name):
                self.assertTrue(target.is_file(), f"{target} missing")
                self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_the_receipt_records_them_as_agents_not_skills(self) -> None:
        """Kind is what `--uninstall` and the drift check read; conflating the
        two would make an agent look like a skill that lost its home."""

        self.install("--user")
        kinds = {
            row["kind"]
            for row in self.receipt["owned"]
            if row["path"].endswith(tuple(f"agents/{p.name}" for p in agent_files()))
        }
        self.assertEqual(kinds, {"agent:claude"})

    def test_agents_are_not_rendered_to_codex_or_opencode(self) -> None:
        """The stated limit, pinned so it cannot erode into a half-render.

        Codex agents are TOML with the instructions embedded; producing that is
        a translation this renderer deliberately does not do.
        """

        self.install("--user")
        codex_agents = self.home / ".codex" / "agents"
        # By stem and any extension, not by filename: Codex's native agent
        # format is `.toml`, so a check for `<name>.md` would pass over
        # exactly the render this limit exists to forbid.
        landed = sorted(
            path.name
            for path in (codex_agents.glob("*") if codex_agents.is_dir() else [])
        )
        self.assertEqual(landed, [], f"agents rendered to {codex_agents}")
        # A name that is legitimately both an agent and a skill would render to
        # the skill homes on the skill's own account. None collide today; the
        # exemption is here so a future collision fails the *render*, not this.
        skills = {surface.name for surface in sd_install.discover_surfaces(REPO_ROOT)}
        roots = (
            self.home / ".codex" / "skills",
            self.home / ".config" / "opencode" / "commands",
        )
        for source in agent_files():
            if source.stem in skills:
                continue
            for root in roots:
                with self.subTest(agent=source.name, root=root.name):
                    # Both spellings: the skill homes are directory-layout, so
                    # an agent rendered through that path would land as
                    # `<name>/SKILL.md` and a file-only glob would miss it.
                    self.assertFalse((root / source.stem).exists())
                    self.assertEqual(sorted(root.glob(f"{source.stem}.*")), [])

    def test_uninstall_takes_them_with_it(self) -> None:
        self.install("--user")
        rc, _ = self.install("--uninstall")
        self.assertEqual(rc, 0)
        for name in (p.name for p in agent_files()):
            with self.subTest(agent=name):
                self.assertFalse((self.home / ".claude" / "agents" / name).exists())

    def test_a_checkout_without_agents_still_installs(self) -> None:
        """Discovery returns nothing rather than failing on an absent directory.

        The skills half of the install is what the command exists for, and a
        checkout predating `agents/` -- or a future one that retires it -- must
        converge instead of erroring.
        """

        checkout = self.home / "checkout"
        folder = checkout / "skills" / "sd-probe"
        folder.mkdir(parents=True)
        (folder / sd_install.SKILL_FILE).write_text(
            "---\nname: sd-probe\n---\n\nprobe surface\n", encoding="utf-8"
        )
        self.assertEqual(sd_install.discover_agents(checkout), [])
        # A path names it, because criterion 24 says the installer renders
        # what a path names and refuses a checkout that names nothing.
        (checkout / "skills" / sd_install.PATHS_FILE).write_text(
            json.dumps({"paths": {
                "research": {"summary": "sources to brief", "skills": ["sd-probe"]},
                "development": {"summary": "plan to ship", "skills": []},
                "act": {"summary": "brief to send", "skills": []},
            }}),
            encoding="utf-8",
        )
        context = sd_install.Context(
            checkout=checkout,
            home=self.home,
            environ={
                "XDG_STATE_HOME": str(self.home / ".local" / "state"),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
            },
        )
        out = io.StringIO()
        self.assertEqual(sd_install.cmd_user(context, out), 0)
        self.assertNotIn("agents", out.getvalue())


class PredecessorTests(unittest.TestCase):
    """`--user` retires a hand-placed agent a shipped one replaces.

    `slice-builder.md` lived only in `~/.claude/agents`, placed by hand; the
    pack now ships it as `sd-slice-builder.md`. No receipt ever recorded the
    old file, so `prune_stale` cannot touch it, and without this the machine
    would hold two agents with one job. The table's digest set is the gate: a
    copy nobody edited goes, anything else stays and is named.

    The seeded bytes are the test's own, with the table patched to vouch for
    them, so the test does not carry a copy of the hand-placed file.
    """

    SEEDED = b"---\nname: slice-builder\n---\n\nhand-placed\n"

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name).resolve()
        self.old = self.home / ".claude" / "agents" / "slice-builder.md"
        self.old.parent.mkdir(parents=True)
        patcher = mock.patch.dict(sd_install.AGENT_PREDECESSORS, {
            "sd-slice-builder": (
                ("slice-builder", frozenset({sd_install.digest(self.SEEDED)})),
            ),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        rc = sd_install.main([*args, "--home", str(self.home)], out=out)
        return rc, out.getvalue()

    def test_the_table_names_shipped_successors_and_sha256_digests(self) -> None:
        """Read unpatched: a successor nobody ships would retire nothing, silently."""
        shipped = {path.stem for path in agent_files()}
        for successor, predecessors in PREDECESSORS.items():
            with self.subTest(successor=successor):
                self.assertIn(successor, shipped)
                for name, known in predecessors:
                    self.assertNotIn(f"{name}.md", {p.name for p in agent_files()})
                    self.assertTrue(known)
                    for value in known:
                        self.assertRegex(value, r"^[0-9a-f]{64}$")

    def test_an_unmodified_predecessor_is_retired(self) -> None:
        self.old.write_bytes(self.SEEDED)
        rc, out = self.install("--user")
        self.assertEqual(rc, 0)
        self.assertFalse(self.old.exists(), out)
        self.assertTrue((self.old.parent / "sd-slice-builder.md").is_file())
        self.assertIn(f"retired predecessor: {self.old}", out)

    def test_a_modified_predecessor_is_kept_and_reported(self) -> None:
        self.old.write_bytes(self.SEEDED + b"an edit\n")
        rc, out = self.install("--user")
        self.assertEqual(rc, 0)
        self.assertTrue(self.old.is_file())
        self.assertIn(
            f"left in place (modified; superseded by sd-slice-builder): {self.old}", out)
        _, status = self.install("--status")
        self.assertIn(f"predecessor: {self.old} remains (modified;", status)

    def test_dry_run_removes_nothing(self) -> None:
        self.old.write_bytes(self.SEEDED)
        rc, out = self.install("--user", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertTrue(self.old.is_file())
        self.assertIn(f"would retire predecessor: {self.old}", out)

    def test_status_names_a_retirable_predecessor(self) -> None:
        self.old.write_bytes(self.SEEDED)
        _, status = self.install("--status")
        self.assertIn(
            f"predecessor: {self.old} (superseded by sd-slice-builder) remains -- run --user",
            status)
        self.assertTrue(self.old.is_file())

    def test_a_symlinked_predecessor_is_kept(self) -> None:
        """A link points at a file this installer never placed; unlinking it is not ours to do."""
        source = self.home / "elsewhere.md"
        source.write_bytes(self.SEEDED)
        self.old.symlink_to(source)
        rc, out = self.install("--user")
        self.assertEqual(rc, 0)
        self.assertTrue(self.old.is_symlink())
        self.assertIn(f"left in place (a symlink; superseded by sd-slice-builder): {self.old}", out)

    def test_an_unreadable_predecessor_is_kept(self) -> None:
        self.old.write_bytes(self.SEEDED)
        original = Path.read_bytes

        def read_bytes(path: Path) -> bytes:
            if path == self.old:
                raise PermissionError(13, "Permission denied")
            return original(path)

        with mock.patch.object(sd_install.Path, "read_bytes", autospec=True,
                               side_effect=read_bytes):
            retired, skipped = sd_install.retire_predecessors(
                sd_install.discover_agents(REPO_ROOT), sd_install.agent_homes(self.home))
        self.assertEqual(retired, [])
        self.assertEqual(skipped, [(
            str(self.old),
            "unreadable (Permission denied); superseded by sd-slice-builder")])
        self.assertTrue(self.old.is_file())

    def test_a_predecessor_that_will_not_unlink_is_reported(self) -> None:
        self.old.write_bytes(self.SEEDED)
        self.place_successor()
        with mock.patch.object(sd_install.Path, "unlink", autospec=True,
                               side_effect=PermissionError(13, "Permission denied")):
            retired, skipped = sd_install.retire_predecessors(
                sd_install.discover_agents(REPO_ROOT), sd_install.agent_homes(self.home))
        self.assertEqual(retired, [])
        self.assertEqual(skipped, [(
            str(self.old),
            "could not remove (Permission denied); superseded by sd-slice-builder")])
        self.assertTrue(self.old.is_file())

    def retire(self) -> tuple[list[str], list[tuple[str, str]]]:
        return sd_install.retire_predecessors(
            sd_install.discover_agents(REPO_ROOT), sd_install.agent_homes(self.home))

    def place_successor(self, data: bytes | None = None) -> None:
        shipped = (AGENTS / "sd-slice-builder.md").read_bytes()
        (self.old.parent / "sd-slice-builder.md").write_bytes(
            shipped if data is None else data)

    def test_a_predecessor_stays_until_its_successor_is_installed(self) -> None:
        """The gate is the successor's bytes on disk, not the caller's ordering."""
        for label, data in (("absent", None), ("stale", b"an older render\n")):
            with self.subTest(successor=label):
                self.old.write_bytes(self.SEEDED)
                if data is not None:
                    self.place_successor(data)
                retired, skipped = self.retire()
                self.assertEqual(retired, [])
                self.assertEqual(skipped, [(
                    str(self.old),
                    "successor not installed; superseded by sd-slice-builder")])
                self.assertTrue(self.old.is_file())

    def test_an_unreadable_successor_counts_as_not_installed(self) -> None:
        self.old.write_bytes(self.SEEDED)
        self.place_successor()
        render = self.old.parent / "sd-slice-builder.md"
        original = Path.read_bytes

        def read_bytes(path: Path) -> bytes:
            if path == render:
                raise PermissionError(13, "Permission denied")
            return original(path)

        with mock.patch.object(sd_install.Path, "read_bytes", autospec=True,
                               side_effect=read_bytes):
            retired, skipped = self.retire()
        self.assertEqual(retired, [])
        self.assertEqual(skipped, [(
            str(self.old), "successor not installed; superseded by sd-slice-builder")])
        self.assertTrue(self.old.is_file())

    def test_a_non_regular_predecessor_is_kept_unread(self) -> None:
        """A FIFO blocks `read_bytes` until a writer opens it, which would hang `--user`."""
        self.place_successor()
        os.mkfifo(self.old)
        result: list = []
        worker = threading.Thread(target=lambda: result.append(self.retire()), daemon=True)
        worker.start()
        worker.join(5)
        if worker.is_alive():
            # Unblock the reader so the thread ends, then fail on the hang.
            os.close(os.open(self.old, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(5)
            self.fail("retire_predecessors blocked reading a FIFO")
        self.assertEqual(result, [([], [(
            str(self.old), "not a regular file; superseded by sd-slice-builder")])])
        self.assertTrue(self.old.is_fifo())

    def test_no_predecessor_is_silent(self) -> None:
        rc, out = self.install("--user")
        self.assertEqual(rc, 0)
        self.assertNotIn("predecessor", out)
        self.assertNotIn("slice-builder.md (", out)
        _, status = self.install("--status")
        self.assertNotIn("predecessor", status)


if __name__ == "__main__":
    unittest.main()
