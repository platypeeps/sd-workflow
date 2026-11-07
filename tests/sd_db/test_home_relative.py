"""Migration 014 and the library under two homes (sd:1439).

The tests design.md names under "Tests that catch a missed site": the schema
sweep, the round trip, the collision, the replay, the write guard, the status
count and the two-home test. Each enumerates from the schema or drives a real
path, not a list typed from the design.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import (
    connect,
    create_item,
    judgment,
    ledger,
    paths,
    reads,
    removal,
    repos,
    workflow,
)
from sd_db.jobs.cli import command_status
from sd_db.migrate import migrate
from sd_db.schema import SCHEMA_DIR, SCHEMA_VERSION
from sd_db.testing import make_store
from sd_db.testing.wire import hub_only
from sd_db.writes import record_cost, record_skill_use, set_item_fields, upsert_repo

WHEN = "2026-09-24T00:00:00+00:00"

#: Every column of every table, classified. `key` columns must hold no path
#: under `$HOME` after 014; `history` keeps what it said (prd R5); `hub-only`
#: names a path only the hub reads; `none` never holds a repository path. A
#: column this map does not name fails the sweep, so a new column is
#: classified before it passes.
COLUMNS = {
    "assignment": {"history": "scope result",
                   "none": "id item role provider status started ended cost after parent lane "
                           "budget_minutes budget_usd phase run_count queued_at"},
    "bill": {"none": "name cost_basis cap_usd_month"},
    "cost": {"key": "repo",
             "none": "id call_id timestamp provider bill role assignment pass owner_pid tokens_in "
                     "tokens_out usd window_minutes used_percent source"},
    "item": {"key": "repo path external_id",
             "history": "fields body title",
             "none": "id kind branch status stage priority due source shipped_at created_at updated_at "
                     "source_commit piece parked_at gate_generation ready_digest recurrence "
                     "recurrence_anchor recurrence_lead"},
    "judgment": {"history": "answer questions ordering changed override", "key": "location",
                 "none": "id timestamp caller stage arm pair shadow provider model primitive "
                         "question_id outcome cause confidence tokens_in tokens_out duration_ms usd "
                         "override_source override_at server_ms probabilities threshold run_id "
                         "prompt_hash load_avg parent error_class error_detail"},
    "note": {"history": "body", "hub-only": "output_path",
             "none": "id item timestamp kind session resolved_at started ended exit_code harness_session"},
    "provider": {"none": "name enabled reason author_rank reviewer_rank"},
    "publication_claim": {"history": "payload",
                          "none": "id item active_item state created_at updated_at"},
    "repo": {"key": "path", "none": "remote mode runner_merge managed ci lane_host pieces_source created_at updated_at "
                      "channels clone_profiles dependabot jira_project declarations"},
    "repo_protection": {"key": "repo", "history": "body reason",
                        "none": "observed_at status default_branch"},
    "request_outcome": {"none": "id committed_at"},
    "runner_lease": {"key": "repo", "none": "run branch exclusive acquired_at released_at"},
    "runner_run": {"key": "repo detached_from", "hub-only": "work_path retained_path log_path",
                   "history": "detail delivery_proof ignored_manifest quarantine final",
                   "none": "id assignment run branch owner journal_version start_step end_step "
                           "end_action outcome supervisor_pid supervisor_pgid supervisor_start "
                           "provider vendor base_head authored_head reviewed_head cancel_requested "
                           "created_at updated_at released_at step heartbeat_at model session_id "
                           "builder_pgid builder_start review_round doubts"},
    "shadow": {"none": "id tracker repo url number kind title state author first_seen last_seen reason "
                       "merged_at closed_at branch linked_issues updated_at created_at filed status assignee "
                       "review_decision checks mergeable"},
    "skill_use": {"key": "cwd", "none": "id timestamp skill surface mode"},
    "state": {"key": "key", "history": "body", "none": "id kind timestamp resolved_at"},
    "trial": {"none": "id skill started expires"},
    # pack sd:8050: a vote's text is the board's record, kept as written.
    "vote": {"key": "repo", "history": "topic motion type moved seconded votes tally outcome follow_up source notes",
             "none": "id key date state item revision created_at updated_at"},
}


def classified() -> dict[tuple[str, str], str]:
    return {(table, column): kind
            for table, kinds in COLUMNS.items()
            for kind, names in kinds.items()
            for column in names.split()}


def reverse_script(name: str = "014_home_relative_repo_paths.sql") -> str:
    """A migration's reverse, from the `--   ` lines of its header. 014's by default."""
    text = (SCHEMA_DIR / name).read_text(encoding="utf-8")
    return "\n".join(line[4:] for line in text.splitlines() if line.startswith("--   "))


def rows(path: Path) -> dict[str, list[tuple]]:
    """Every row of every table, sorted, so a reinserted row compares equal."""
    connection = sqlite3.connect(path)
    try:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {name: sorted(connection.execute(f"SELECT * FROM {name}").fetchall(), key=repr)
                for name in names}
    finally:
        connection.close()


def version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


class HomeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "a"
        self.home.mkdir()
        self.use_home(self.home)

    def use_home(self, home: Path):
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)


class AThirteenStore(HomeCase):
    """A store at 13 whose every key column holds a path under the home.

    The history and hub-only columns hold one too, so the sweep can show
    they were left as written.
    """

    def setUp(self):
        super().setUp()
        self.database = self.root / "sd.db"
        make_store(self.database, version=13, rows=0)
        one, two = str(self.home / "repos/one"), str(self.home / "repos/two")
        self.one, self.two = one, two
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            for path in (one, two, "/opt/outside"):
                connection.execute(
                    "INSERT INTO repo (path, status_source, created_at, updated_at) "
                    "VALUES (?, 'row', ?, ?)", (path, WHEN, WHEN))
                connection.execute(
                    "INSERT INTO repo_protection (repo, observed_at, status, body) "
                    "VALUES (?, ?, 'unknown', ?)", (path, WHEN, f'{{"root": "{path}"}}'))
            item = (
                "INSERT INTO item (id, kind, title, status, repo, path, source, external_id, fields, "
                "body, created_at, updated_at, gate_generation) VALUES (?, ?, ?, 'planning', ?, ?, ?, ?, ?, ?, ?, ?, 0)")
            connection.execute(item, (1, "work", "one", one, "docs/work/2026-09-24-x/prd.md", "docs/work",
                                      f"{one}::docs/work/2026-09-24-x", f'{{"report": {{"source_path": "{one}/x"}}}}',
                                      f'{{"text": "{one}"}}', WHEN, WHEN))
            connection.execute(item, (2, "work", "two", two, f"{two}/docs/work/2026-09-24-y/prd.md", "register",
                                      "register-2", None, None, WHEN, WHEN))
            connection.execute(item, (3, "work", "three", two, "pieces/p.md", "writing-piece",
                                      f"{two}::pieces/p.md", None, None, WHEN, WHEN))
            connection.execute(item, (4, "task", "four", "/opt/outside", None, None, None, None, None, WHEN, WHEN))
            connection.execute(item, (5, "task", "five", None, None, None, None, None, None, WHEN, WHEN))
            connection.execute("INSERT INTO assignment (id, item, role, status) VALUES (1, 1, 'author', 'running')")
            connection.execute(
                "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, "
                "created_at, updated_at) VALUES ('r1', 1, 1, ?, 'feat/x', 'runner', ?, ?, ?, ?)",
                (one, str(self.home / "work/r1"), str(self.home / "keep/r1"), WHEN, WHEN))
            connection.execute(
                "INSERT INTO runner_lease (run, repo, branch, exclusive, acquired_at) "
                "VALUES ('r1', ?, 'feat/x', 1, ?)", (one, WHEN))
            for key in (f"{one}:status_source", f"{two}:pieces_source"):
                connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('verified', ?, ?, '{}')",
                                   (key, WHEN))
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', 'c', ?, ?)",
                               (WHEN, f'{{"repo": "{one}"}}'))
            connection.execute("INSERT INTO note (item, timestamp, kind, body, output_path) VALUES (1, ?, 'exec', ?, ?)",
                               (WHEN, f"ran in {one}", str(self.home / ".local/share/sd/executions/1.log")))
            for repo in (one, "/private/tmp/elsewhere"):
                connection.execute("INSERT INTO cost (timestamp, repo, source) VALUES (?, ?, 'run')", (WHEN, repo))
            connection.execute("INSERT INTO skill_use (timestamp, skill, cwd) VALUES (?, 'sd-ship', ?)", (WHEN, one))
        connection.close()
        self.before = rows(self.database)
        self.copy = self.root / "pre.db"
        shutil.copyfile(self.database, self.copy)


class TheMigration(AThirteenStore):
    def test_the_sweep_leaves_no_key_under_the_home(self):
        """Criteria 1 and 2, from the schema rather than a list."""
        result = migrate(self.database)
        self.assertEqual((result.before, result.after), (13, SCHEMA_VERSION))
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        found = set()
        for (table,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
            for column in connection.execute(f"PRAGMA table_info({table})"):
                found.add((table, column[1]))
        known = classified()
        self.assertEqual(sorted(found - set(known)), [], "classify these columns in COLUMNS")
        self.assertEqual(sorted(set(known) - found), [], "COLUMNS names a column the schema lacks")
        after = rows(self.database)
        # 043 appends `note.harness_session` after 014, NULL on every row.
        self.assertEqual({row[-1] for row in after["note"]}, {None})
        after["note"] = sorted((row[:-1] for row in after["note"]), key=repr)
        for (table, column), kind in known.items():
            values = [value[0] for value in connection.execute(f"SELECT {column} FROM {table}")]
            if kind == "key":
                under = [value for value in values
                         if isinstance(value, str) and value.startswith(str(self.home))]
                self.assertEqual(under, [], f"{table}.{column}")
        for table in ("note", "assignment", "judgment", "publication_claim"):
            self.assertEqual(after[table], self.before[table], table)
        # 019 adds `request_outcome` after 014, empty, and 048 `vote`.
        self.assertEqual(after.pop("request_outcome"), [])
        self.assertEqual(after.pop("vote"), [])
        self.assertEqual({table: len(value) for table, value in after.items()},
                         {table: len(value) for table, value in self.before.items()})
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        # History and hub-only paths are what they were.
        body = connection.execute("SELECT fields, body FROM item WHERE id = 1").fetchone()
        self.assertIn(self.one, body[0])
        self.assertIn(self.one, body[1])
        self.assertEqual(connection.execute("SELECT work_path FROM runner_run").fetchone()[0],
                         str(self.home / "work/r1"))
        # The shapes 014 writes.
        self.assertEqual(sorted(value[0] for value in connection.execute("SELECT path FROM repo")),
                         ["/opt/outside", "~/repos/one", "~/repos/two"])
        self.assertEqual(connection.execute("SELECT path FROM item WHERE id = 2").fetchone()[0],
                         "docs/work/2026-09-24-y/prd.md")
        self.assertEqual(connection.execute("SELECT external_id FROM item WHERE id = 1").fetchone()[0],
                         "~/repos/one::docs/work/2026-09-24-x")
        self.assertEqual(sorted(value[0] for value in connection.execute(
            "SELECT key FROM state WHERE kind = 'verified'")),
            ["~/repos/one:status_source", "~/repos/two:pieces_source"])
        self.assertEqual(sorted(value[0] for value in connection.execute("SELECT repo FROM cost")),
                         ["/private/tmp/elsewhere", "~/repos/one"])

    def test_the_reverse_returns_every_row_byte_identical(self):
        """Criterion 3."""
        migrate(self.database)
        self.assertNotEqual(rows(self.database), self.before)
        connection = sqlite3.connect(self.database, isolation_level=None)
        paths.install(connection)
        connection.execute("PRAGMA foreign_keys = ON")
        # 026 to 015 came after 014 and are reversed first, newest first:
        # 014's reverse is written against the table at 14, without
        # `repo.managed`, `repo.ci`, `repo.satellite_gate`, `repo.lane_host`, 021's to 023's
        # `judgment` columns or `runner_run.detached_from`. 026's reverse puts
        # `status_source` back at `row`, which every row here held, and 025's
        # puts `satellite_gate` back so 020's can drop it. 041's goes first of
        # all: the one later column on `item`, whose rows this compares, after
        # 044's `shadow` merge columns, 043's `note.harness_session` and 042's build columns on `runner_run`.
        connection.executescript(reverse_script("048_vote.sql"))
        connection.executescript(reverse_script("044_shadow_merge_state.sql"))
        connection.executescript(reverse_script("043_note_harness_session.sql"))
        connection.executescript(reverse_script("042_runner_run_build.sql"))
        connection.executescript(reverse_script("041_recurrence_lead.sql"))
        connection.executescript(reverse_script("026_repo_drop_status_source.sql"))
        connection.executescript(reverse_script("025_repo_drop_satellite_gate.sql"))
        connection.executescript(reverse_script("024_repo_lane_host.sql"))
        connection.executescript(reverse_script("023_judgment_error.sql"))
        connection.executescript(reverse_script("022_judgment_batch_children.sql"))
        connection.executescript(reverse_script("021_judgment_call_context.sql"))
        connection.executescript(reverse_script("020_repo_satellite_gate.sql"))
        connection.executescript(reverse_script("019_request_outcome.sql"))
        connection.executescript(reverse_script("018_runner_run_repo_nullable.sql"))
        connection.executescript(reverse_script("017_judgment_compare_arms.sql"))
        connection.executescript(reverse_script("016_repo_ci.sql"))
        connection.executescript(reverse_script("015_repo_managed.sql"))
        connection.executescript(reverse_script())
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()
        self.assertEqual(version(self.database), 13)
        self.assertEqual(rows(self.database), self.before)

    def test_a_replay_changes_nothing(self):
        migrate(self.database)
        connection = sqlite3.connect(self.database)
        # 014 names `repo.status_source`, which 026 drops, so the replay runs at 25.
        connection.executescript(reverse_script("026_repo_drop_status_source.sql"))
        migrated = rows(self.database)
        paths.install(connection)
        with connection:
            connection.executescript(
                (SCHEMA_DIR / "014_home_relative_repo_paths.sql").read_text(encoding="utf-8"))
        connection.close()
        self.assertEqual(rows(self.database), migrated)

    def test_a_collision_aborts_the_file_at_13(self):
        connection = sqlite3.connect(self.database)
        with connection:
            connection.execute("INSERT INTO repo (path, created_at, updated_at) VALUES ('~/repos/one', ?, ?)",
                               (WHEN, WHEN))
        connection.close()
        before = rows(self.database)
        with self.assertRaises(sqlite3.IntegrityError):
            migrate(self.database)
        self.assertEqual(version(self.database), 13)
        self.assertEqual(rows(self.database), before)

    def test_a_migrate_without_the_functions_fails_closed_at_13(self):
        with mock.patch("sd_db.migrate.paths.install", lambda connection: None):
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such function: sd_home_relative"):
                migrate(self.database)
        self.assertEqual(version(self.database), 13)
        self.assertEqual(rows(self.database), self.before)


class TheWriters(HomeCase):
    """Every library writer stores the key; `upsert_repo` refuses a bypass."""

    def setUp(self):
        super().setUp()
        self.database = self.home / ".local/share/sd/sd.db"
        migrate(self.database, home=self.home)
        self.connection = connect(self.database, home=self.home)
        self.addCleanup(self.connection.close)
        self.checkout = self.home / "repos/one"
        self.checkout.mkdir(parents=True)
        git(self.checkout, "init", "-q", "-b", "main")

    @hub_only
    def test_each_writer_stores_the_key(self):
        c = self.connection
        repos.add(c, self.checkout, home=self.home)
        self.assertEqual([row["path"] for row in repos.registered(c)], ["~/repos/one"])
        item = create_item(c, kind="task", title="t", repo=str(self.checkout))
        self.assertEqual(c.execute("SELECT repo FROM item WHERE id = ?", (item,)).fetchone()[0], "~/repos/one")
        other = create_item(c, kind="task", title="u")
        set_item_fields(c, other, repo=str(self.checkout))
        self.assertEqual(c.execute("SELECT repo FROM item WHERE id = ?", (other,)).fetchone()[0], "~/repos/one")
        record_cost(c, source="run", repo=str(self.checkout))
        record_cost(c, source="run", repo="/opt/outside")
        self.assertEqual(sorted(row[0] for row in c.execute("SELECT repo FROM cost")),
                         ["/opt/outside", "~/repos/one"])
        c.execute("INSERT INTO bill (name, cost_basis) VALUES ('b', 'metered')")
        c.commit()
        ledger.reserve(c, bill="b", bound=1.0, call_id="home-relative",
                       owner_pid=os.getpid(), repo=str(self.checkout))
        self.assertEqual(c.execute("SELECT repo FROM cost WHERE call_id = 'home-relative'")
                         .fetchone()[0], "~/repos/one")
        record_skill_use(c, "sd-ship", cwd=str(self.checkout))
        self.assertEqual(c.execute("SELECT cwd FROM skill_use").fetchone()[0], "~/repos/one")

    def test_a_judgment_location_is_stored_as_the_key(self):
        def location(value):
            row_id = judgment.record(
                self.connection, caller="c", stage="s", provider="p", primitive="noul",
                outcome="ok", location=value)
            return self.connection.execute(
                "SELECT location FROM judgment WHERE id = ?", (row_id,)).fetchone()[0]
        self.assertEqual(location(str(self.checkout)), "~/repos/one")
        self.assertEqual(location("~/repos/one"), "~/repos/one")
        self.assertEqual(location(str(self.home)), "~")
        self.assertEqual(location("/opt/outside"), "/opt/outside")
        self.assertEqual(repos.absolute_under_home(self.connection)["judgment.location"], 0)

    def test_upsert_repo_refuses_an_absolute_path_under_the_home(self):
        with self.assertRaisesRegex(paths.PathRefused, "~/repos/one"):
            upsert_repo(self.connection, str(self.checkout))
        self.assertEqual(upsert_repo(self.connection, "/opt/outside"), "/opt/outside")
        self.assertEqual(upsert_repo(self.connection, "~/repos/one"), "~/repos/one")


class TheStatusCount(HomeCase):
    """Criterion 6: `status` counts key values absolute under this home."""

    def status(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(command_status([]), 0)
        return out.getvalue()

    def test_zero_after_the_migration_and_one_after_a_raw_insert(self):
        database = self.home / ".local/share/sd/sd.db"
        migrate(database, home=self.home)
        self.assertIn("sd-db: 0 key value(s) absolute under this home", self.status())
        raw = sqlite3.connect(database)
        with raw:
            raw.execute("INSERT INTO repo (path, created_at, updated_at) VALUES (?, ?, ?)",
                        (str(self.home / "repos/one"), WHEN, WHEN))
        raw.close()
        text = self.status()
        self.assertIn("sd-db: 1 key value(s) absolute under this home", text)
        self.assertIn("sd-db:   repo.path: 1", text)
        raw = sqlite3.connect(database)
        with raw:
            raw.execute("INSERT INTO judgment (timestamp, caller, stage, provider, primitive, "
                        "outcome, location) VALUES (?, 'c', 's', 'p', 'noul', 'ok', ?)",
                        (WHEN, str(self.home / "repos/one")))
        raw.close()
        self.assertIn("sd-db:   judgment.location: 1", self.status())


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def maintenance_children(checkout: Path) -> list:
    """The maintenance or gc children one commit in `checkout` starts, from trace2.

    An unconfigured repository starts `git maintenance run --auto --detach`
    after a commit. That child holds `.git/objects/maintenance.lock` while it
    runs, and a `copytree` of the checkout races it (sd:1454).
    """
    with tempfile.TemporaryDirectory() as scratch:
        trace = Path(scratch) / "trace.json"
        subprocess.run(["git", "-C", str(checkout), "commit", "--allow-empty", "-qm", "probe"],
                       check=True, capture_output=True,
                       env={**os.environ, "GIT_TRACE2_EVENT": str(trace)})
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    return [event["argv"] for event in events if event.get("event") == "child_start"
            and {"maintenance", "gc"} & set(event.get("argv", []))]


class TwoHomes(HomeCase):
    """Criterion 4 for the library's verbs: written under A, read under B.

    Any "not registered", git failure or missing file under `B` names a site
    that still compares or opens the stored value as an absolute path.
    """

    def setUp(self):
        super().setUp()
        checkout = self.home / "repos/one"
        prd = checkout / "docs/work/2026-09-24-an-item/prd.md"
        prd.parent.mkdir(parents=True)
        prd.write_text("---\ntitle: an item\ncreated: 2026-09-24\n---\n\nbody\n", encoding="utf-8")
        git(checkout, "init", "-q", "-b", "main")
        # No background maintenance: its lock file races the copytree below (sd:1454).
        git(checkout, "config", "maintenance.auto", "false")
        git(checkout, "config", "gc.auto", "0")
        git(checkout, "config", "user.email", "fixture@example.invalid")
        git(checkout, "config", "user.name", "Fixture")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-qm", "an item")
        database = self.home / ".local/share/sd/sd.db"
        migrate(database, home=self.home)
        connection = connect(database, home=self.home)
        with connection:
            repos.add(connection, checkout, home=self.home)
        # The row `work register` filed until pack sd:7064 retired it.
        key = repos.row_for(connection, str(checkout))["path"]
        create_item(connection, kind="work", status="planning", title="an item", repo=str(checkout),
                    path="docs/work/2026-09-24-an-item/prd.md", source="docs/work",
                    external_id=f"{key}::docs/work/2026-09-24-an-item/prd.md")
        self.task = workflow.capture_task(connection, title="a task", repo=str(checkout), who="fixture")
        connection.close()
        # The second machine: another login, the same `~/repos` layout.
        self.other = self.root / "b"
        shutil.copytree(self.home, self.other, symlinks=True)
        self.use_home(self.other)
        self.checkout = self.other / "repos/one"
        self.connection = connect(self.other / ".local/share/sd/sd.db", home=self.other)
        self.addCleanup(self.connection.close)

    def test_the_fixture_checkout_starts_no_background_maintenance(self):
        # sd:1454: a detached `git maintenance` holding objects/maintenance.lock
        # makes the `copytree` above raise shutil.Error.
        self.assertEqual(maintenance_children(self.checkout), [])

    @hub_only
    def test_every_lookup_finds_the_row_written_under_the_other_home(self):
        c = self.connection
        here = str(self.checkout)
        self.assertEqual(repos.registered_for(c, here, None), "~/repos/one")
        self.assertIsNotNone(repos.row_for(c, here))
        self.assertIsNotNone(repos.row_for(c, "~/repos/one"))
        self.assertEqual(repos.set_runner_merge(c, here, "auto")[0], "~/repos/one")
        self.assertEqual({row["title"] for row in reads.brief_items(c, here)}, {"an item", "a task"})
        self.assertEqual({row["title"] for row in reads.backlog_items(c, repo=here)} & {"an item", "a task"},
                         {"an item", "a task"})
        plan = removal.plan_repo(c, here, with_items=True, home=self.other)
        self.assertNotIn("not registered", repr(plan))

    def test_the_disk_path_is_the_one_under_this_home(self):
        row = self.connection.execute("SELECT repo, path FROM item WHERE title = 'an item'").fetchone()
        disk = paths.expand(row["repo"])
        self.assertEqual(disk, self.checkout)
        self.assertTrue((disk / row["path"]).is_file())
        self.assertEqual(git(disk, "rev-parse", "--show-toplevel"), str(self.checkout))


#: The equality lookups on a `path` or `repo` column in `sd_db`, per file,
#: that were read on 2026-09-24 and bind a value taken from a stored row --
#: `row["repo"]`, `run["repo"]`, a `repo.path` found by `repos.row_for` --
#: or a column that is not a repository path. A stored value is already the
#: key, so equality is right for them. A lookup whose argument comes from a
#: caller must probe `paths.keys` with `IN (...)` instead.
EQUALITY_LOOKUPS = {
    "progress.py": (5, "stored row values; `item.path` is repo-relative; `shadow.repo` is a slug"),
    "recovery.py": (1, "`_pieces` takes the row path `reimport` found with `row_for`"),
    "repos.py": (9, "`set_channels`, `set_clones`, `set_dependabot`, `set_github_issues`, `set_jira_project` and "
                 "`set_declaration` "
                 "write the row path "
                 "they read through `paths.keys` in the same transaction; `seed_clones` the path `row_for` or `add` gave; "
                 "`mine_issues` and `intake_issues` match `shadow.repo`, a slug"),
    "removal.py": (8, "`_plan_repo` rebinds `path` to the row `row_for` found, and P7's vote lookup binds it; "
                   "`_detach` reads it off the plan"),
    "runner.py": (6, "every argument is an item or run row's `repo`"),
    "runner_exec.py": (1, "the item row's `repo`"),
    "ship.py": (1, "the item row's `repo`"),
    "votes.py": (2, "`_get` and `rows` bind the row path `_stored` read through `repos.row_for`"),
    "writes.py": (3, "`upsert_repo`, whose guard refuses an unconverted path, writes and reads back its row "
                  "(pack sd:7366); `settle_shadow` "
                  "sets `shadow.repo`, a slug, and looks nothing up by it"),
}


class TheEqualityLookups(unittest.TestCase):
    """Design test 7, a heuristic: a new `path = ?` or `repo = ?` fails here.

    It cannot tell a stored value from a caller's; a person reads the new
    site, and either probes `paths.keys` or adds it above with the reason.
    """

    def test_no_lookup_is_added_without_a_reason(self):
        import re

        import sd_db
        pattern = re.compile(r"\b(?:path|repo) *= *\?")
        package = Path(sd_db.__file__).parent
        found = {}
        for source in sorted(package.rglob("*.py")):
            count = len(pattern.findall(source.read_text(encoding="utf-8")))
            if count:
                found[source.relative_to(package).as_posix()] = count
        self.assertEqual(found, {name: count for name, (count, _) in EQUALITY_LOOKUPS.items()})


if __name__ == "__main__":
    unittest.main()
