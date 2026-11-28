"""`sd store`: the checks that do not need a row, and the renderers it shares with the `db` driver.

The vault driver is gone (sd:4627), so the notes it wrote and read are no longer
under test here. What stays: manifest validation, the refusals `store add` makes
before it opens the database, the vault checks `add` still makes for title
collisions, and the pure renderers (`render_value`, `render_field`,
`fill_sections`, `edit_section`) that `get`, `list --full`, `add` and `set-section`
use. The database verbs are in `test_sd_store_db.py`.

Every subprocess case runs `bin/sd` with `XDG_CONFIG_HOME` and `OBSIDIAN_VAULT`
pointed into a temporary directory, so neither the developer's machine config nor
the developer's vault is ever touched.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import itertools
import json
import pathlib
import random
import subprocess
import sys
import tempfile
import unittest

from sd_db import initialise
from sd_db.database import local_path

from tests.clean_env import clean_environment

try:                          # a probe, not a dependency; see the test that uses it
    import yaml
except ImportError:           # pragma: no cover - depends on the developer's venv
    yaml = None               # type: ignore[assignment]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SD = REPO_ROOT / "bin" / "sd"

_SD_MODULE = None


def sd_module():
    """`bin/sd` imported as a module, for the cases that cannot afford a subprocess.

    Every other case in this file runs the real command, which is the point of
    them. The brute-force check renders tens of thousands of values, and a
    subprocess each would make it minutes rather than seconds.
    """

    global _SD_MODULE
    if _SD_MODULE is None:
        loader = importlib.machinery.SourceFileLoader("sd_under_test", str(SD))
        spec = importlib.util.spec_from_file_location("sd_under_test", str(SD), loader=loader)
        assert spec is not None
        _SD_MODULE = importlib.util.module_from_spec(spec)
        loader.exec_module(_SD_MODULE)
    return _SD_MODULE


TIP_KIND: dict[str, object] = {
    "fields": ["status", "score"],
    "initial-status": "inbox",
    "transitions": {"inbox": ["approved", "declined"]},
    "sections": {"order": ["Tip"], "template": "tip.md"},
}

# The shape the `store` block takes, in one place, so a case can break exactly
# one part of it.
STORE: dict[str, object] = {
    "driver": "db",
    "root": "$OBSIDIAN_VAULT",
    "bases": {"tip": "System/Databases/Tips"},
}


def workflow_for(kinds: dict[str, dict[str, object]]) -> dict[str, object]:
    """A `workflow` map for each kind: every word of its ladder at `planning`, as the `db` driver needs."""

    declared: dict[str, object] = {}
    for name, kind in kinds.items():
        words = {str(kind["initial-status"])}
        for source, targets in (kind.get("transitions") or {}).items():  # type: ignore[attr-defined]
            words.update([source, *targets])
        declared[name] = {"status": {word: "planning" for word in sorted(words)}}
    return declared


class StoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.config_home = self.tmp / "config"
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        initialise(local_path(self.home))

    def run_sd(self, *args: str, vault: str | None = "") -> subprocess.CompletedProcess[str]:
        env = clean_environment(HOME=str(self.home), XDG_CONFIG_HOME=str(self.config_home))
        # `vault=None` is "the knob is unset", which is a case with its own
        # test; the default is the fixture's own vault.
        env.pop("OBSIDIAN_VAULT", None)
        if vault is not None:
            env["OBSIDIAN_VAULT"] = vault or str(self.vault)
        return subprocess.run(
            [sys.executable, str(SD), *args], capture_output=True, text=True, env=env)

    def plugin(self, *, kinds: object = None, store: object = STORE,
               register: bool = True) -> pathlib.Path:
        """A registered plugin that keeps `tip` rows, with a template, for the fixture's vault."""

        root = self.tmp / "pack"
        root.mkdir(parents=True, exist_ok=True)
        (root / "tip.md").write_text("\n## Tip\n", encoding="utf-8")
        declared = {"tip": dict(TIP_KIND)} if kinds is None else kinds
        body: dict[str, object] = {"prefix": "pp", "interface": 1, "kinds": declared}
        if store is not None:
            body["store"] = store
            if isinstance(store, dict) and store.get("driver") == "db" and isinstance(declared, dict):
                body["workflow"] = workflow_for(declared)  # type: ignore[arg-type]
        (root / "sd-plugin.json").write_text(json.dumps(body), encoding="utf-8")
        if register:
            done = self.run_sd("plugin", "add", str(root))
            self.assertEqual(done.returncode, 0, done.stderr)
        return root

    def add(self, title: str = "T", *, vault: str | None = "") -> subprocess.CompletedProcess[str]:
        return self.run_sd("store", "add", "pp.tip", title, "--field", "score=7", vault=vault)

    def titles(self) -> list[str]:
        done = self.run_sd("store", "list", "pp.tip", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        return [row["title"] for row in json.loads(done.stdout)]


class ResolutionTests(StoreFixture):
    """The manifest lookups every verb makes."""

    def test_add_reads_no_vault(self) -> None:
        """pack sd:5133: with the base notes retired, `add` neither probes nor scans the vault."""

        self.plugin()
        (self.vault / "Learning").mkdir()
        (self.vault / "Learning" / "Shared name.md").write_text("A note.\n", encoding="utf-8")
        for title, vault in (("Unset", None), ("Absent", str(self.tmp / "absent")), ("Shared name", "")):
            done = self.add(title, vault=vault)
            self.assertEqual(done.returncode, 0, f"{title}: {done.stderr}")
        self.assertEqual(self.titles(), ["Unset", "Absent", "Shared name"])
        self.assertFalse((self.tmp / "absent").exists())

    def test_an_unknown_prefix_and_an_unknown_kind_refuse_differently(self) -> None:
        self.plugin()
        for reference, expected in (("zz.tip", "no registered plugin has prefix"),
                                    ("pp.absent", "declares no kind")):
            with self.subTest(reference=reference):
                done = self.run_sd("store", "list", reference)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(expected, done.stderr)

    def test_a_reference_that_is_not_prefix_dot_kind_is_a_usage_error(self) -> None:
        self.plugin()
        for reference in ("tip", "pp.tip.extra"):
            with self.subTest(reference=reference):
                done = self.run_sd("store", "list", reference)
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("<prefix>.<kind>", done.stderr)

    def test_a_plugin_with_kinds_and_no_store_refuses_at_use_time(self) -> None:
        """Absent stays absent: `sys` declares no `store` and registers fine."""

        self.plugin(store=None)
        done = self.run_sd("store", "list", "pp.tip")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("declares no `store`", done.stderr)


class ManifestTests(StoreFixture):
    def refused(self, store: object, *, kinds: object = None) -> str:
        root = self.plugin(store=store, kinds=kinds, register=False)
        done = self.run_sd("plugin", "add", str(root))
        self.assertEqual(done.returncode, 1, done.stdout)
        return done.stderr

    def test_an_unknown_driver_refuses_by_name(self) -> None:
        self.assertIn("the driver(s) are db", self.refused({**STORE, "driver": "vualt"}))

    def test_the_removed_vault_driver_refuses_and_names_the_fix(self) -> None:
        """sd:4627: a manifest that still says `vault` is told, not left to a default."""

        said = self.refused({**STORE, "driver": "vault"})
        self.assertIn("'vault', which was removed", said)
        self.assertIn('to "db"', said)

    def test_a_root_that_is_a_literal_path_refuses(self) -> None:
        """The `pack.py:146` defect, refused where it would be committed.

        A vault path written into a manifest is portable to one machine, and
        the manifest is the file most likely to be copied to another.
        """

        for root in ("/Users/someone/Documents/Vault", "~/Documents/Vault", "$lowercase"):
            with self.subTest(root=root):
                self.assertIn(
                    "environment variable reference",
                    self.refused({**STORE, "root": root}))

    def test_every_key_of_the_block_is_required(self) -> None:
        for key in sorted(STORE):
            with self.subTest(key=key):
                self.assertIn(
                    f"declares no {key!r}",
                    self.refused({k: v for k, v in STORE.items() if k != key}))

    def test_an_unknown_key_refuses(self) -> None:
        self.assertIn("unknown key(s): cache", self.refused({**STORE, "cache": True}))

    def test_coverage_is_checked_in_both_directions(self) -> None:
        """A base with no kind is dead; a kind with no base is a store verb that cannot run."""

        self.assertIn(
            "'topic', which is not a declared kind",
            self.refused({**STORE, "bases": {"tip": "a", "topic": "b"}}))
        self.assertIn(
            "kind 'topic' has no `store.bases` entry",
            self.refused(
                STORE,
                kinds={"tip": dict(TIP_KIND), "topic": dict(TIP_KIND)}))

    def test_a_base_that_leaves_the_vault_refuses(self) -> None:
        for base in ("/etc", "../../etc", ""):
            with self.subTest(base=base):
                self.refused({**STORE, "bases": {"tip": base}})

    def test_a_store_with_no_kinds_refuses(self) -> None:
        root = self.tmp / "pack"
        root.mkdir(parents=True, exist_ok=True)
        (root / "sd-plugin.json").write_text(
            json.dumps({"prefix": "pp", "interface": 1, "store": STORE}), encoding="utf-8")
        done = self.run_sd("plugin", "add", str(root))
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("declares no `kinds` to keep", done.stderr)

    def test_a_registered_store_is_reported_by_plugin_list(self) -> None:
        self.plugin()
        done = self.run_sd("plugin", "list")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("store: db at $OBSIDIAN_VAULT", done.stdout)


class DeclarationTests(StoreFixture):
    """The 8-i gap 8-iv closes: a key that governs nothing registers clean."""

    def test_transitions_without_a_status_field_refuses_at_registration(self) -> None:
        root = self.plugin(
            kinds={"tip": {"fields": ["score"], "initial-status": "inbox",
                           "transitions": {"inbox": ["approved"]}}},
            register=False)
        done = self.run_sd("plugin", "add", str(root))
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("transitions", done.stderr)

    def test_human_only_without_a_status_field_refuses_at_registration(self) -> None:
        root = self.plugin(
            kinds={"tip": {"fields": ["score"], "initial-status": "inbox",
                           "human-only": {"publish": "published"}}},
            register=False)
        done = self.run_sd("plugin", "add", str(root))
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("human-only", done.stderr)

    def test_a_statusless_kind_is_still_registerable(self) -> None:
        """`initial-status` is deliberately not part of the tightening.

        It is required of every kind, so requiring a `status` field for it
        would make a statusless kind unregisterable and `status_filter`'s
        refusal unreachable -- the case
        `test_status_on_a_kind_without_one_refuses_instead_of_matching_nothing`
        exists to cover.
        """

        root = self.plugin(
            kinds={"tip": {"fields": ["score"], "initial-status": "inbox"}}, register=False)
        self.assertEqual(self.run_sd("plugin", "add", str(root)).returncode, 0)


class AddRefusalTests(StoreFixture):
    """What `add` refuses before it opens the database: the shared arguments, templates and sections."""

    def setUp(self) -> None:
        super().setUp()
        self.kind: dict[str, object] = {
            "fields": ["status", "score", "contexts"],
            "initial-status": "inbox",
            "transitions": {"inbox": ["approved", "declined"]},
            "sections": {"order": ["Tip", "Score"], "template": "tip.md"},
        }

    def build(self, template: str = "\n## Tip\n\n## Score\n") -> None:
        root = self.plugin(kinds={"tip": self.kind}, register=False)
        (root / "tip.md").write_text(template, encoding="utf-8")
        self.assertEqual(self.run_sd("plugin", "add", str(root)).returncode, 0)

    def file(self, name: str, text: str) -> str:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_a_field_given_twice_with_plain_equals_is_still_a_mistake(self) -> None:
        """`+=` exists so this stays an error. Folding every duplicate into a
        list would make `--field score=7 --field score=8` a two-item list."""

        self.build()
        done = self.run_sd(
            "store", "add", "pp.tip", "T", "--field", "score=7", "--field", "score=8")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("given twice", done.stderr)

    def test_mixing_the_two_spellings_on_one_field_is_refused(self) -> None:
        self.build()
        done = self.run_sd(
            "store", "add", "pp.tip", "T", "--field", "contexts=Personal",
            "--field", "contexts+=Work")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("given twice", done.stderr)

    def test_a_list_is_refused_on_a_field_something_compares_as_one_value(self) -> None:
        """`status`, `floor` and `unique-fields` all read a field as a scalar.
        Without this the list reached them where a string was expected."""

        self.kind["floor"] = {"score": 6}
        self.build()
        for field in ("status", "score"):
            with self.subTest(field=field):
                # `score` is given once, as a list. Adding a scalar `score=9`
                # alongside would trip the duplicate guard first and the case
                # would pass without ever reaching what it is about.
                extra = [] if field == "score" else ["--field", "score=9"]
                done = self.run_sd(
                    "store", "add", "pp.tip", f"T-{field}",
                    *extra, "--field", f"{field}+=x")
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("single value", done.stderr)

    def test_a_section_the_template_does_not_render_is_refused(self) -> None:
        self.build()
        done = self.run_sd(
            "store", "add", "pp.tip", "T", "--field", "score=9",
            "--section", "Provenance=Nowhere to go.")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("renders no `## Provenance`", done.stderr)

    def test_an_indented_heading_is_refused_too(self) -> None:
        """CommonMark renders a heading indented by up to three spaces, so
        `   ## Score` reaches Obsidian as one while `body_headings`, which reads
        column 0, does not see it."""

        self.build()
        done = self.run_sd(
            "store", "add", "pp.tip", "T", "--field", "score=9",
            "--section", "Tip=text\n   ## Score\nsmuggled")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("its own `## ` heading", done.stderr)

    def test_a_misspelled_placeholder_is_refused_rather_than_shipped(self) -> None:
        """Left in place it would reach every note written from that template
        as literal text, and be noticed by a reader rather than an author."""

        self.build(template="\n# {{titel}}\n\n## Tip\n\n## Score\n")
        done = self.run_sd(
            "store", "add", "pp.tip", "T", "--field", "score=9")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("{{titel}}", done.stderr)

    def test_section_text_carrying_its_own_heading_is_refused(self) -> None:
        """Otherwise the note is written with a heading outside
        `sections.order`, which the order check has just approved."""

        self.build()
        done = self.run_sd(
            "store", "add", "pp.tip", "T", "--field", "score=9",
            "--section", "Tip=text\n## Score\nsmuggled")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("its own `## ` heading", done.stderr)

    @unittest.skipUnless(yaml is not None, "PyYAML is not installed")
    def test_an_empty_list_renders_bare_and_reads_back_as_none(self) -> None:
        """`key:` and `key: []` are different values, not two spellings of one.

        Called directly: `parse_assignments` appends a value for every `+=`, so
        no CLI input builds an empty list. This pins `render_field`'s contract,
        and the reason the bare form is written anyway -- it is what the corpus
        carries for an unfilled list, as `used-by:` and `my-rating:` show.
        """

        rendered = sd_module().render_field("used-by", [])
        self.assertEqual(rendered, "used-by:\n")
        assert yaml is not None
        self.assertIsNone(yaml.safe_load(rendered)["used-by"])
        self.assertEqual(yaml.safe_load("used-by: []")["used-by"], [])

    def test_fill_sections_takes_the_headings_own_line_ending(self) -> None:
        """Called directly, because `read_template` normalises the only input
        the CLI has. Hardcoding "\\n" here put two endings in one file, and
        would do so again the day a template is read with `newline=""`."""

        filled = sd_module().fill_sections(
            "\r\n## Tip\r\n\r\n## Score\r\n", {"Tip": "one\ntwo"})
        self.assertEqual(filled, "\r\n## Tip\r\n\r\none\r\ntwo\r\n\r\n## Score\r\n")
        self.assertNotIn("\n", filled.replace("\r\n", ""))

    def test_a_field_the_kind_does_not_declare_is_refused_from_a_file_too(self) -> None:
        self.build()
        done = self.run_sd(
            "store", "add", "pp.tip", "T",
            "--field-file", f"nope={self.file('v.txt', 'x')}")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("declares no field", done.stderr)

    def test_a_missing_file_refuses_rather_than_writing_an_empty_value(self) -> None:
        self.build()
        done = self.run_sd(
            "store", "add", "pp.tip", "T", "--field", "score=9",
            "--section-file", f"Tip={self.tmp / 'absent.txt'}")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("cannot read", done.stderr)

    def test_an_empty_file_refuses(self) -> None:
        """`pack.py`'s rule, kept: a note written from an empty file is a note
        nobody meant to write, and it is written silently."""

        self.build()
        tip = self.file("tip.txt", "   \n\n")
        done = self.run_sd(
            "store", "add", "pp.tip", "T", "--field", "score=9",
            "--section-file", f"Tip={tip}")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("is empty", done.stderr)

    def test_a_file_that_is_not_utf8_refuses(self) -> None:
        path = self.tmp / "tip.bin"
        path.write_bytes(b"\xff\xfe\x00")
        self.build()
        done = self.run_sd(
            "store", "add", "pp.tip", "T", "--field", "score=9",
            "--section-file", f"Tip={path}")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("cannot read", done.stderr)

    def test_a_pair_with_no_separator_is_a_usage_error(self) -> None:
        self.build()
        done = self.run_sd(
            "store", "add", "pp.tip", "T", "--field", "score=9",
            "--section-file", str(self.file("tip.txt", "text")))
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("is not name=path", done.stderr)

    def test_a_pair_with_a_separator_and_no_path_is_a_usage_error(self) -> None:
        """`--field-file score=` is a different mistake from `--field-file
        score`, and says so rather than failing later as an unreadable ''."""

        self.build()
        done = self.run_sd("store", "add", "pp.tip", "T", "--field-file", "score=")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("no path after the separator", done.stderr)

    def test_both_spellings_of_one_field_is_the_error_a_repeat_already_was(self) -> None:
        self.build()
        done = self.run_sd(
            "store", "add", "pp.tip", "T",
            "--field", "score=9",
            "--field-file", f"score={self.file('score.txt', '8')}")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("given twice", done.stderr)

    def test_set_refuses_the_positional_and_the_flag_together(self) -> None:
        """10b-iv gave `set` the file spellings; mixing them is still wrong.

        `--field-file` is on `set` now, because the `pack.py` verbs it replaces
        moved several keys in one write. What is refused is no longer the flag
        but the *mix*: `set k t score --field-file x=y` has one field named
        positionally and another named by flag, and no reading of that is
        obviously right. Exit 2 rather than 1, and it is checked before the
        path is opened -- a wrong invocation should be told so, not told that
        the file it should not have named is missing.
        """

        self.build()
        done = self.run_sd("store", "set", "pp.tip", "T", "score", "--field-file", "x=y")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("not both", done.stderr)

    def test_a_title_cannot_walk_out_of_the_kind_directory(self) -> None:
        """Containment does the work rather than a forbidden-character list."""

        self.build()
        for title in ("../escaped", "..", ".", "", "a/b", "sub/../../out"):
            done = self.run_sd("store", "add", "pp.tip", title, "--field", "score=9")
            self.assertEqual(done.returncode, 2, f"{title!r} accepted: {done.stdout}")


class RenderTests(unittest.TestCase):
    """The pure renderers `get`, `list --full`, `add` and `set-section` share."""

    @unittest.skipUnless(yaml is not None, "PyYAML absent; the concrete cases above still run")
    def test_no_short_value_over_the_yaml_indicators_breaks_a_real_parser(self) -> None:
        """The rule `NEEDS_QUOTING` encodes, derived rather than recalled.

        Two hand-written enumerations of the indicator set were each
        incomplete. The first missed the five values that open `- `, `? `,
        `,`, `]` and `}`. The second, written *after* that lesson and believed
        to be the whole class, still missed a leading `'` -- which opens a
        single-quoted scalar and swallows the rest of the block -- along with a
        trailing `:` (`note: See also:` is an unterminated mapping key) and an
        embedded tab, which `render_value` lets past its control-character
        check on purpose.

        So this does not check a list. It renders every string up to length
        three over the alphabet of YAML indicators, plus a random sample of
        longer ones, and requires that a real parser reads back what was
        written. A future edit to `NEEDS_QUOTING` that reopens any hole fails
        here without anyone having to have thought of the case.

        `render_value` is called in process rather than through `sd store set`
        because the subprocess cost is per value and there are tens of
        thousands of them; the end-to-end path is covered by the two cases
        above.
        """

        alphabet = list("-?:,[]{}>|&*!%@`#'\"= \tab1.~")
        values = ["".join(p) for n in (1, 2, 3) for p in itertools.product(alphabet, repeat=n)]
        rng = random.Random(11)
        values += ["".join(rng.choice(alphabet) for _ in range(rng.randint(4, 9)))
                   for _ in range(4000)]
        broken = []
        for value in values:
            try:
                rendered = sd_module().render_value(value)
            except Exception:
                continue          # refused outright, which is a valid answer
            try:
                read_back = yaml.safe_load(f"k: {rendered}\n")["k"]
            except Exception as error:
                broken.append((value, rendered, type(error).__name__))
                continue
            if isinstance(read_back, str) and read_back != value:
                broken.append((value, rendered, f"read back as {read_back!r}"))
        self.assertEqual(broken[:10], [], f"{len(broken)} of {len(values)} values do not survive")

    def test_edit_section_takes_the_headings_own_line_ending(self) -> None:
        """Called directly, because the CLI normalises the only input it has.

        Hardcoding a newline here is what put two endings in one file when
        `fill_sections` did it. This is one of three sites, which is why
        `line_ending` is a named helper rather than a fourth copy of the
        expression.
        """

        edit_section = sd_module().edit_section
        note = "---\r\nstatus: inbox\r\n---\r\n\r\n## Tip\r\n\r\nold\r\n"
        replaced = edit_section(note, "Tip", "one\ntwo", ["Tip", "Score"])
        self.assertEqual(
            replaced, "---\r\nstatus: inbox\r\n---\r\n\r\n## Tip\r\n\r\none\r\ntwo\r\n")
        self.assertNotIn("\n", replaced.replace("\r\n", ""))

        created = edit_section(note, "Score", "s", ["Tip", "Score"])
        self.assertNotIn("\n", created.replace("\r\n", ""))
        self.assertIn("## Score\r\n\r\ns\r\n", created)


class EditSectionTests(unittest.TestCase):
    """`edit_section` called directly: the db driver splices a row's body through it.

    The cases were vault-note tests until the vault driver went (sd:4627); the
    function stayed, so they now run on text, with no note or database.
    """

    ORDER = ["Tip", "Score", "Provenance"]

    def edit(self, text: str, name: str, body: str) -> str:
        return sd_module().edit_section(text, name, body, self.ORDER)

    def test_the_body_outside_the_edited_section_is_untouched(self) -> None:
        text = "## Tip\n\nkeep me\n\n## Score\n\ngone\n\n## Provenance\n\nkeep me too\n"
        self.assertEqual(
            self.edit(text, "Score", "x"),
            "## Tip\n\nkeep me\n\n## Score\n\nx\n\n## Provenance\n\nkeep me too\n")

    def test_a_fenced_heading_does_not_end_the_section_it_sits_in(self) -> None:
        text = "## Tip\n\nRun it:\n\n```markdown\n## Score\nnot a heading\n```\n\n## Score\n\nreal score\n"
        after = self.edit(text, "Tip", "replaced")
        self.assertNotIn("not a heading", after)
        self.assertIn("## Score\n\nreal score\n", after)
        self.assertEqual(after.count("## Score"), 1)

    def test_a_fenced_heading_is_not_the_section_that_gets_edited(self) -> None:
        text = "## Tip\n\n```markdown\n## Score\nfenced\n```\n\n## Score\n\nreal\n"
        after = self.edit(text, "Score", "written")
        self.assertIn("```markdown\n## Score\nfenced\n```", after)
        self.assertIn("## Score\n\nwritten\n", after)

    def test_a_tilde_fence_does_not_close_a_backtick_fence(self) -> None:
        text = "## Tip\n\n```\n~~~\n## Score\nstill fenced\n```\n\n## Score\n\nreal\n"
        after = self.edit(text, "Score", "written")
        self.assertIn("## Score\nstill fenced\n", after)
        self.assertIn("## Score\n\nwritten\n", after)

    def test_a_longer_fence_is_not_closed_by_a_shorter_one(self) -> None:
        text = "## Tip\n\n````markdown\n```\n## Score\n````\n\n## Score\n\nreal score\n"
        after = self.edit(text, "Score", "written")
        self.assertIn("````markdown\n```\n## Score\n````\n", after)
        self.assertIn("## Score\n\nwritten\n", after)
        self.assertNotIn("real score", after)

    def test_an_info_string_holding_a_backtick_does_not_open_a_fence(self) -> None:
        text = "## Tip\n\n```` `x` ````\n\n## Score\n\nreal\n"
        after = self.edit(text, "Score", "written")
        self.assertEqual(after, "## Tip\n\n```` `x` ````\n\n## Score\n\nwritten\n")

    def test_an_indented_heading_is_the_section_it_looks_like(self) -> None:
        text = "## Tip\n\nt\n\n   ## Score\n\nindented\n"
        after = self.edit(text, "Score", "written")
        self.assertEqual(after.count("## Score"), 1, f"a second heading was created:\n{after}")
        self.assertNotIn("indented", after)
        self.assertIn("written", after)

    def test_an_indented_heading_ends_the_section_above_it(self) -> None:
        text = "## Tip\n\nmine\n\n  ## Provenance\n\nnot mine\n"
        after = self.edit(text, "Tip", "replaced")
        self.assertEqual(after, "## Tip\n\nreplaced\n\n  ## Provenance\n\nnot mine\n")

    def test_a_missing_section_is_created_in_the_declared_order(self) -> None:
        text = "## Tip\n\ntip text\n\n## Provenance\n\nwhere from\n"
        after = self.edit(text, "Score", "8/10")
        self.assertEqual(
            after, "## Tip\n\ntip text\n\n## Score\n\n8/10\n\n## Provenance\n\nwhere from\n")

    def test_a_section_with_no_later_neighbour_is_appended(self) -> None:
        after = self.edit("## Tip\n\ntip text\n", "Provenance", "a link")
        self.assertEqual(after, "## Tip\n\ntip text\n\n## Provenance\n\na link\n")

    def test_an_appended_heading_does_not_land_on_an_unterminated_last_line(self) -> None:
        after = self.edit("## Tip\n\nno newline at the end", "Score", "s")
        self.assertEqual(after, "## Tip\n\nno newline at the end\n\n## Score\n\ns\n")

    def test_an_empty_body_leaves_one_blank_line_and_no_text(self) -> None:
        after = self.edit("## Tip\n\nold\n\n## Score\n\ns\n", "Tip", "")
        self.assertEqual(after, "## Tip\n\n## Score\n\ns\n")


if __name__ == "__main__":
    unittest.main()
