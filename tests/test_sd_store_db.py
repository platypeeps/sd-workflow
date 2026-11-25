"""`sd store` under the `db` driver: the rows are the source, through `sd_db.records` (sd:3975).

Every case runs `bin/sd` as a subprocess against a temporary home, whose
database the fixture creates, and a temporary vault that must stay empty:
under `db` the vault holds no base notes, and no verb writes one.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from sd_db import connect, initialise, records
from sd_db.database import local_path, transaction
from sd_db.writes import add_note

from tests.clean_env import clean_environment
from tests.test_sd_store import sd_module

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SD = REPO_ROOT / "bin" / "sd"

TIP = {
    "fields": ["status", "score", "my-rating", "slug", "url"],
    "initial-status": "inbox",
    "transitions": {"inbox": ["accepted", "declined"], "accepted": ["published", "declined"]},
    "human-only": {"decline": "declined"},
    "protected-fields": ["my-rating"],
    "unique-fields": ["slug"],
    "floor": {"score": 6},
    "sections": {"order": ["Tip", "Notes"], "template": "templates/tip.md"},
}
MAP = {"inbox": "planning", "accepted": "ready", "published": "done", "declined": "done"}
TEMPLATE = "# {{title}}\n\n## Tip\n\n## Notes\n"


class DbStoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        initialise(local_path(self.home))

    def run_sd(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SD), *args], capture_output=True, text=True,
                              env=clean_environment(HOME=str(self.home), XDG_CONFIG_HOME=str(self.tmp / "config"),
                                                    OBSIDIAN_VAULT=str(self.vault)))

    def ok(self, *args: str) -> subprocess.CompletedProcess[str]:
        done = self.run_sd(*args)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def refused(self, *args: str, says: str) -> None:
        before = self.state()
        done = self.run_sd(*args)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(says, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(self.state(), before)

    def plugin(self, *, driver: str = "db", workflow: object = None, register: bool = True) -> pathlib.Path:
        root = self.tmp / "plugin"
        (root / "templates").mkdir(parents=True, exist_ok=True)
        (root / "templates" / "tip.md").write_text(TEMPLATE, encoding="utf-8")
        manifest = {"prefix": "pp", "interface": 1, "kinds": {"tip": TIP},
                    "store": {"driver": driver, "root": "$OBSIDIAN_VAULT", "bases": {"tip": "Tips"}},
                    "workflow": {"tip": {"status": MAP}} if workflow is None else workflow}
        (root / "sd-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        if register:
            self.ok("plugin", "add", str(root))
        return root

    def rows(self) -> list[dict]:
        connection = connect(local_path(self.home))
        try:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM item WHERE source = 'vault' ORDER BY id")]
        finally:
            connection.close()

    def notes(self, item: int) -> list[tuple[str, str]]:
        connection = connect(local_path(self.home))
        try:
            return [(row["kind"], row["body"]) for row in connection.execute(
                "SELECT kind, body FROM note WHERE item = ? ORDER BY id", (item,))]
        finally:
            connection.close()

    def state(self) -> tuple:
        rows = self.rows()
        return (rows, [self.notes(row["id"]) for row in rows], sorted(self.vault.rglob("*")))

    def add(self, title: str = "First tip", *extra: str) -> dict:
        self.ok("store", "add", "pp.tip", title, "--field", "score=7", *extra)
        return self.rows()[-1]


class TheRulesEntry(DbStoreFixture):
    def test_plugin_list_reports_each_kinds_validated_rules(self) -> None:
        self.plugin()
        [entry] = json.loads(self.ok("plugin", "list", "--json").stdout)
        self.assertEqual(entry["rules"]["tip"]["fields"], TIP["fields"])
        self.assertEqual(entry["rules"]["tip"]["human-only"], {"decline": "declined"})
        self.assertEqual(entry["rules"]["tip"]["floor"], {"score": 6})
        self.assertEqual(entry["store"]["driver"], "db")

    def test_the_library_reads_those_rules_through_this_pack(self) -> None:
        """`sd_db.records.rules_for` runs this checkout's `sd plugin list --json`, as an installed pack."""

        self.plugin()
        receipt = self.home / ".local/state/sd-ai-command-pack/installed.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"schema": 1, "checkout": str(REPO_ROOT), "commit": "0" * 40,
                                       "branch": "main", "dirty": False}), encoding="utf-8")
        environ = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.tmp / "config")}
        rules = records.rules_for("tip", home=self.home, environ=environ)
        self.assertEqual((rules.driver, rules.base, rules.initial, rules.human_only, rules.protected, rules.floor),
                         ("db", "Tips", "inbox", {"decline": "declined"}, ("my-rating",), {"score": 6}))
        self.assertEqual(rules.stages, MAP)

    def test_a_db_store_needs_a_workflow_map_for_every_kind(self) -> None:
        root = self.plugin(workflow={}, register=False)
        manifest = json.loads((root / "sd-plugin.json").read_text(encoding="utf-8"))
        del manifest["workflow"]
        (root / "sd-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        done = self.run_sd("plugin", "add", str(root))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("kind 'tip' has no `workflow` map", done.stderr)


class TheWrites(DbStoreFixture):
    def setUp(self) -> None:
        super().setUp()
        self.plugin()

    def test_add_writes_a_row_and_no_note(self) -> None:
        row = self.add("First tip", "--section", "Tip=Use the flag.")
        self.assertEqual((row["kind"], row["stage"], row["status"], row["path"]),
                         ("idea", "inbox", "planning", "Tips/First tip.md"))
        fields = json.loads(row["fields"])
        self.assertEqual((fields["kind"], fields["score"], fields["my-rating"]), ("tip", "7", None))
        self.assertNotIn("status", fields)
        self.assertEqual(json.loads(row["body"])["markdown"], "# First tip\n\n## Tip\n\nUse the flag.\n\n## Notes\n")
        self.assertEqual(sorted(self.vault.rglob("*")), [])

    def test_add_refuses_a_human_only_stage_and_a_protected_field(self) -> None:
        self.refused("store", "add", "pp.tip", "T", "--field", "score=7", "--field", "status=declined",
                     says="`human-only` reserves 'decline'")
        self.refused("store", "add", "pp.tip", "T", "--field", "score=7", "--field", "my-rating=5",
                     says="`protected-fields` declares 'my-rating'")

    def test_set_moves_the_stage_with_its_notes(self) -> None:
        row = self.add()
        self.ok("store", "set", "pp.tip", "First tip", "status", "accepted")
        moved = self.rows()[-1]
        self.assertEqual((moved["stage"], moved["status"]), ("accepted", "ready"))
        self.assertEqual(self.notes(row["id"])[1:], [
            ("comment", "Stage inbox -> accepted by sd store"),
            ("status_change", "planning -> ready by sd store: stage inbox -> accepted")])

    def test_set_refuses_what_an_agent_may_not_do(self) -> None:
        self.add()
        self.add("Second tip", "--field", "slug=taken")
        for args, says in ((("status", "declined"), "`human-only` reserves 'decline'"),
                           (("my-rating", "4"), "`protected-fields` declares 'my-rating'"),
                           (("status", "published"), "'published' is not among them"),
                           (("score", "5"), "`floor` puts 'score' at 6"),
                           (("slug", "taken"), "`unique-fields` declares 'slug' unique"),
                           (("colour", "red"), "declares no field 'colour'")):
            with self.subTest(args=args):
                self.refused("store", "set", "pp.tip", "First tip", *args, says=says)

    def test_set_section_edits_one_section_and_keeps_the_rest_verbatim(self) -> None:
        self.add("First tip", "--section", "Tip=Use the flag.")
        self.ok("store", "set-section", "pp.tip", "First tip", "--section", "Notes=Seen twice.")
        body = json.loads(self.rows()[-1]["body"])["markdown"]
        self.assertEqual(body, "# First tip\n\n## Tip\n\nUse the flag.\n\n## Notes\n\nSeen twice.\n")
        again = self.ok("store", "set-section", "pp.tip", "First tip", "--section", "Notes=Seen twice.")
        self.assertIn("already holds", again.stdout)

    def test_a_missing_row_refuses(self) -> None:
        self.refused("store", "set", "pp.tip", "No such tip", "score", "8", says="no tip row has the title")


class TheReads(DbStoreFixture):
    def setUp(self) -> None:
        super().setUp()
        self.plugin()
        self.add("First tip", "--section", "Tip=Use the flag.", "--field", "url=https://example.test/a")
        self.add("Second tip")
        self.ok("store", "set", "pp.tip", "Second tip", "status", "accepted")

    def test_list_reads_rows_with_status_as_the_stage(self) -> None:
        listed = json.loads(self.ok("store", "list", "pp.tip", "--json").stdout)
        self.assertEqual([(row["title"], row["status"], row["score"]) for row in listed],
                         [("First tip", "inbox", "7"), ("Second tip", "accepted", "7")])
        accepted = json.loads(self.ok("store", "list", "pp.tip", "--status", "accepted", "--json", "--full").stdout)
        self.assertEqual([(row["title"], row["body"]) for row in accepted],
                         [("Second tip", "# Second tip\n\n## Tip\n\n## Notes\n")])
        text = self.ok("store", "list", "pp.tip").stdout
        self.assertIn("inbox\t7\t?\t?\thttps://example.test/a\tFirst tip", text)

    def test_get_reads_one_row_its_fields_and_a_section(self) -> None:
        got = json.loads(self.ok("store", "get", "pp.tip", "First tip", "--json").stdout)
        self.assertEqual((got["title"], got["path"], got["fields"]["status"], got["fields"]["url"]),
                         ("First tip", "Tips/First tip.md", "inbox", "https://example.test/a"))
        self.assertNotIn("kind", got["fields"])
        self.assertEqual(self.ok("store", "get", "pp.tip", "First tip", "--section", "Tip").stdout,
                         "Use the flag.\n")
        whole = self.ok("store", "get", "pp.tip", "First tip").stdout
        self.assertTrue(whole.startswith("=== First tip ===\n---\nstatus: inbox\nscore: 7\nmy-rating:\n"), whole)
        self.assertTrue(whole.endswith("---\n# First tip\n\n## Tip\n\nUse the flag.\n\n## Notes\n"), whole)


class UniqueStatus(DbStoreFixture):
    def test_a_db_store_refuses_a_unique_status(self) -> None:
        """Under `db` the status is the stage, which no `unique-fields` check reads."""

        root = self.plugin(register=False)
        manifest = json.loads((root / "sd-plugin.json").read_text(encoding="utf-8"))
        manifest["kinds"]["tip"]["unique-fields"] = ["status"]
        (root / "sd-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        done = self.run_sd("plugin", "add", str(root))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("`unique-fields` cannot name `status`", done.stderr)


class StaleRead(DbStoreFixture):
    def test_a_write_between_the_read_and_the_edit_refuses_and_keeps_that_write(self) -> None:
        """`set` edits at the revision it read: a note added in between makes it refuse, not overwrite."""

        self.plugin()
        row = self.add()
        real = records.get_row

        def raced(*args: object, **kwargs: object) -> dict:
            found = real(*args, **kwargs)
            other = connect(local_path(self.home))
            try:
                with transaction(other):
                    add_note(other, found["id"], "comment", "another writer", session="test")
            finally:
                other.close()
            return found

        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.tmp / "config"),
               "OBSIDIAN_VAULT": str(self.vault)}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(records, "get_row", raced), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = sd_module().main(["store", "set", "pp.tip", "First tip", "score", "9"])
        self.assertEqual(code, 1, err.getvalue())
        self.assertIn("changed; reload it", err.getvalue())
        self.assertEqual(json.loads(self.rows()[-1]["fields"])["score"], "7")
        self.assertEqual(self.notes(row["id"])[-1], ("comment", "another writer"))


class NoDatabase(DbStoreFixture):
    def test_a_database_that_cannot_open_refuses_rather_than_reading_empty(self) -> None:
        self.plugin()
        local_path(self.home).unlink()
        done = self.run_sd("store", "list", "pp.tip", "--json")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("the `db` store driver", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_a_query_that_fails_after_the_open_refuses_without_a_traceback(self) -> None:
        self.plugin()
        broken = sqlite3.connect(local_path(self.home))
        broken.execute("PRAGMA foreign_keys = OFF")
        broken.execute("ALTER TABLE item RENAME TO item_gone")
        broken.commit()
        broken.close()
        done = self.run_sd("store", "list", "pp.tip", "--json")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("no such table: item", done.stderr)
        self.assertNotIn("Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()


class TheEditorsRevision(DbStoreFixture):
    """A section update guarded by the revision the editor read (sd:10621)."""

    def setUp(self) -> None:
        super().setUp()
        self.plugin()
        self.add("First tip", "--section", "Tip=Use the flag.")

    def read(self, *extra: str) -> dict:
        return json.loads(self.ok("store", "get", "pp.tip", "First tip", "--json", *extra).stdout)

    def test_get_reports_the_revision_it_read(self) -> None:
        whole = self.read()
        part = self.read("--section", "Tip")
        self.assertEqual(part, {"section": "Tip", "text": "Use the flag.", "revision": whole["revision"]})
        self.assertRegex(whole["revision"], r"^[0-9a-f]{64}$")

    def test_a_write_after_the_editors_read_refuses_the_stale_update(self) -> None:
        read = self.read("--section", "Notes")["revision"]
        self.ok("store", "set-section", "pp.tip", "First tip", "--section", "Notes=Someone else.")
        for verb in ("set-section", "set"):
            with self.subTest(verb=verb):
                self.refused("store", verb, "pp.tip", "First tip", "--revision", read,
                             "--section", "Notes=Mine.", says="changed; reload it")
        self.assertIn("Someone else.", self.read()["body"])

    def test_the_revision_the_editor_read_lets_the_update_through(self) -> None:
        for verb in ("set-section", "set"):
            with self.subTest(verb=verb):
                read = self.read()["revision"]
                self.ok("store", verb, "pp.tip", "First tip", "--revision", read,
                        "--section", f"Notes=By {verb}.")
                self.assertIn(f"By {verb}.", self.read()["body"])

    def test_an_empty_revision_refuses(self) -> None:
        done = self.run_sd("store", "set-section", "pp.tip", "First tip", "--revision", " ",
                           "--section", "Notes=Mine.")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("--revision", done.stderr)
