"""Criterion 28: a suggestion is a row first, and an sd item only when asked.

The criterion's own test is `TheCriterion` below: a proposal row written in
each of `sd_lib.MODES` with `gh` never asked about an issue, a `publish` that
refuses with no destination, a `publish` that files exactly one sd item after
the dedup read the skill requires and asks `gh` nothing (sd:2002), and a
`sd shadow sync` that writes the tracker's open work as rows without closing
anything.

`gh` here is a recorder on `PATH` rather than `sd_db.testing`'s
`GitHubDouble`: the double routes `/repos`, `/branches`, `/collaborators` and
`/pulls`, and this criterion is about issues, which it does not serve. The
recorder logs one argv per line, so "the fixture saw no close call" and "it
filed exactly one" are both assertions about a list rather than about a mock's
call count.

Two clauses of the criterion are not claimed here, and the omission is
deliberate rather than an oversight: `commands.yaml` belongs to item B, and
the writing manifest belongs to another repository.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A `gh` that records every argv and answers the four routes this criterion
#: reaches. It never leaves the machine, and it exits 0 for `auth status` so
#: `available()` and `gh_authenticated` both see a usable credential.
GH_RECORDER = """#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["GH_CALLS"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\\n")
if argv[:1] == ["auth"]:
    sys.exit(int(os.environ.get("GH_AUTH_CODE", "0")))
if "graphql" in argv:
    print(os.environ.get("GH_SEARCH", "{}"))
    sys.exit(0)
if "--method" in argv:
    print(json.dumps({"html_url": "https://github.com/o/r/issues/9"}))
    sys.exit(0)
code = int(os.environ.get("GH_LIST_CODE", "0"))
if code:
    sys.stderr.write("the list call failed\\n")
    sys.exit(code)
print(os.environ.get("GH_OPEN_ISSUES", "[]"))
"""

#: Two open issues, in the shape `sd_db.shadow_sync.QUERY` asks for. The same
#: page answers all four buckets, so the union by url is what makes the row
#: count two rather than eight -- which is the collector's contract, restated
#: here only because a fixture that returned different nodes per bucket would
#: have tested the fixture instead.
TWO_OPEN_ISSUES = {
    "data": {
        "search": {
            "issueCount": 2,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                {
                    "__typename": "Issue",
                    "number": 11,
                    "title": "the installer leaves a stale lock",
                    "url": "https://github.com/o/r/issues/11",
                    "state": "OPEN",
                    "updatedAt": "2026-09-06T10:00:00Z",
                    "author": {"login": "example"},
                    "repository": {"nameWithOwner": "o/r"},
                },
                {
                    "__typename": "Issue",
                    "number": 12,
                    "title": "the dashboard counts a retired item",
                    "url": "https://github.com/o/r/issues/12",
                    "state": "OPEN",
                    "updatedAt": "2026-09-06T11:00:00Z",
                    "author": {"login": "example"},
                    "repository": {"nameWithOwner": "o/r"},
                },
            ],
        }
    }
}


def load(name: str, filename: str | None = None):
    """Import a `bin/` module by path -- `bin/` is not a package.

    A module already in `sys.modules` from that same file is handed back
    rather than executed again; see the same helper in
    `tests/test_sd_handoff_rows.py` for what re-executing `sd_lib` did to
    every other module in a `unittest discover` process.
    """
    path = str(REPO_ROOT / "bin" / (filename or f"{name}.py"))
    already = sys.modules.get(name)
    if already is not None and getattr(already, "__file__", None) == path:
        return already
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

sd_lib = load("sd_lib")
sd_handoff_rows = load("sd_handoff_rows")
sd_suggest = load("sd_suggest")
sd_shadow = load("sd_shadow")

import sd_db  # noqa: E402 - after the path juggling above


class Args:
    """A namespace the verbs read, built per call rather than parsed."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


class SuggestCase(unittest.TestCase):
    """A scratch home with a real database, a checkout, and a recording `gh`."""

    def setUp(self) -> None:
        self._scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self._scratch.cleanup)
        self.home = Path(self._scratch.name).resolve()
        # `HOME` before any row is written: a row's `repo` is its `~/...` key
        # (sd:1439), and a fixture written under the real home would hold a
        # key that `publish`, reading under this one, can never match.
        self.environment(HOME=str(self.home))
        sd_db.initialise(home=self.home)

        self.root = self.home / "checkout"
        item_dir = self.root / sd_lib.WORK_DIR / "an-item"
        item_dir.mkdir(parents=True)
        (item_dir / "prd.md").write_text("# an item\n", encoding="utf-8")
        for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                     ["config", "user.name", "Test"], ["add", "-A"],
                     ["commit", "-qm", "first"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True,
                           capture_output=True)
        self.root = self.root.resolve()
        self.item_dir = self.root / sd_lib.WORK_DIR / "an-item"

        self.connection = sd_db.connect(sd_db.default_path(self.home), write=True)
        self.addCleanup(self.connection.close)
        self.repo = sd_lib.stored_repo(self.root)
        sd_db.writes.upsert_repo(self.connection, self.repo)
        self.item = sd_db.writes.create_item(
            self.connection,
            kind="work",
            title="an item",
            status="in_progress",
            repo=self.repo,
            source=sd_lib.ITEM_ROW_SOURCE,
            external_id=sd_lib.external_id(self.root, self.item_dir),
        )

        self.calls = self.home / "gh-calls"
        fake_bin = self.home / "fake-bin"
        fake_bin.mkdir()
        gh = fake_bin / "gh"
        gh.write_text(GH_RECORDER, encoding="utf-8")
        gh.chmod(0o755)
        self.environment(
            GH_CALLS=str(self.calls),
            PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        )
        # `sd shadow sync` runs every tracker in `sd_db.TRACKERS`, and the
        # Jira path reads its host and credential from the environment. On a
        # machine whose shell exports them, a test that left them in place
        # would send the operator's token to the operator's Jira. Unset, the
        # library answers `configured=False` before any request.
        self.unset_environment("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_JQL")
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

    def environment(self, **values: str) -> None:
        """Set env vars for this test and put back what was there."""
        for key, value in values.items():
            was = os.environ.get(key)
            os.environ[key] = value
            self.addCleanup(lambda k=key, v=was: os.environ.__setitem__(k, v) if v is not None
                            else os.environ.pop(k, None))

    def unset_environment(self, *keys: str) -> None:
        """Remove env vars for this test and put back what was there."""
        for key in keys:
            was = os.environ.pop(key, None)
            if was is not None:
                self.addCleanup(os.environ.__setitem__, key, was)

    def local_block(self, *lines: str) -> None:
        (self.root / sd_lib.LOCAL_FILE_NAME).write_text(
            f"{sd_lib.LOCAL_BLOCK_START}\n" + "\n".join(lines) + f"\n{sd_lib.LOCAL_BLOCK_END}\n",
            encoding="utf-8",
        )

    def run_verb(self, handler, **fields) -> tuple[int, str]:
        """One verb, with its printed lines captured."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = handler(Args(**fields))
        return code, buffer.getvalue()

    def gh_calls(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def issue_calls(self) -> list[list[str]]:
        """Every recorded call that names an issues endpoint."""
        return [call for call in self.gh_calls()
                if any("issues" in part for part in call)]

    def proposals(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT body FROM note WHERE kind = ? ORDER BY id", (sd_suggest.PROPOSAL,)
        )
        return [row["body"] for row in rows]

    def items(self) -> list[dict]:
        rows = self.connection.execute("SELECT id, kind, title, repo, body FROM item ORDER BY id")
        return [dict(row) for row in rows]

    def note_id(self, body: str = "the installer leaves a stale lock") -> int:
        """One recorded proposal, written the way the verb writes it."""
        return sd_db.add_note(self.connection, self.item, sd_suggest.PROPOSAL, body)

    def shadow_rows(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT url, kind, state, title, repo FROM shadow ORDER BY url")
        return [dict(row) for row in rows]



class EnvironmentHelperTests(unittest.TestCase):
    """sd:1002: the fixture's own environment helpers put back exactly what was there."""

    def test_a_value_that_was_empty_comes_back_empty_and_an_absent_one_absent(self) -> None:
        with patch.dict(os.environ, {"SD_SUGGEST_PROBE_EMPTY": ""}):
            os.environ.pop("SD_SUGGEST_PROBE_ABSENT", None)
            probe = SuggestCase("environment")  # setUp is not run, so only the helper's cleanups are registered
            probe.environment(SD_SUGGEST_PROBE_EMPTY="x", SD_SUGGEST_PROBE_ABSENT="y")
            probe.doCleanups()
            self.assertEqual(os.environ.get("SD_SUGGEST_PROBE_EMPTY"), "")
            self.assertNotIn("SD_SUGGEST_PROBE_ABSENT", os.environ)

class TheCriterion(SuggestCase):
    """The four clauses, each as one assertion about what actually happened."""

    def test_the_row_is_written_in_every_mode_and_reaches_no_tracker(self):
        """`add` writes locally in `full`, `minimal` and `guest` alike.

        `full` is the checkout with no origin: `remote_permits_full` answers
        yes for a repository there is nobody to expose anything to, so the
        case needs no remote and asks `gh` nothing. The other two are written
        into the local block, which detection may lower but never raise.
        """
        seen = []
        for expected in sd_lib.MODES:
            if expected == "full":
                (self.root / sd_lib.LOCAL_FILE_NAME).unlink(missing_ok=True)
            else:
                self.local_block(f"mode: {expected}")
            code, out = self.run_verb(
                sd_suggest.suggest_add, item="an-item", body=f"friction in {expected}")
            self.assertEqual(0, code)
            self.assertIn(f"in {expected} mode; filed nowhere", out)
            seen.append(expected)

        self.assertEqual(list(sd_lib.MODES), seen)
        self.assertEqual(
            [f"[{mode}] friction in {mode}" for mode in sd_lib.MODES], self.proposals())
        self.assertEqual([], self.issue_calls())

    def test_publish_refuses_without_a_destination(self):
        """No `--belongs-to`, no filing -- and the refusal names the reason, not a default."""
        note = self.note_id()
        before = self.items()
        with self.assertRaises(sd_handoff_rows.RowsRefusal) as raised:
            self.run_verb(sd_suggest.suggest_publish, note=note, belongs_to="", to="")
        self.assertIn("--belongs-to PATH", str(raised.exception))
        self.assertEqual(before, self.items())
        self.assertEqual([], self.gh_calls())

    def test_publish_files_exactly_one_sd_item_and_no_issue(self):
        """One new item in the named checkout, and `gh` asked nothing (sd:2002)."""
        note = self.note_id()
        before = self.items()
        code, out = self.run_verb(
            sd_suggest.suggest_publish, note=note, belongs_to=str(self.root), to="")

        self.assertEqual(0, code)
        added = [row for row in self.items() if row not in before]
        self.assertEqual(1, len(added), added)
        self.assertEqual(("task", "the installer leaves a stale lock"),
                         (added[0]["kind"], added[0]["title"]))
        self.assertTrue(sd_lib.same_repo(self.root, added[0]["repo"]), added[0]["repo"])
        self.assertIn(f"filed {self.root.name} sd:{added[0]['id']} in ", out)
        self.assertEqual([], self.gh_calls())

    def test_the_retired_to_refuses_by_name_and_files_nothing(self):
        """`--to owner/repo` named a GitHub issue tracker; it now only refuses."""
        note = self.note_id()
        before = self.items()
        with self.assertRaises(sd_handoff_rows.RowsRefusal) as raised:
            self.run_verb(sd_suggest.suggest_publish, note=note,
                          belongs_to=str(self.root), to="o/r")
        self.assertIn("never a GitHub issue", str(raised.exception))
        self.assertEqual(before, self.items())
        self.assertEqual([], self.gh_calls())

    def test_shadow_sync_writes_the_open_issues_and_closes_nothing(self):
        """Two open issues in, two rows out, and no call that would close one."""
        self.environment(GH_SEARCH=json.dumps(TWO_OPEN_ISSUES))
        code, out = self.run_verb(sd_shadow.shadow_sync, strict=True)

        self.assertEqual(0, code)
        self.assertIn("wrote 2 shadow row(s)", out)
        self.assertEqual(
            ["https://github.com/o/r/issues/11", "https://github.com/o/r/issues/12"],
            [row["url"] for row in self.shadow_rows()])
        self.assertEqual(["open", "open"], [row["state"] for row in self.shadow_rows()])
        # The clause, as a fact about the argv list rather than about a mock:
        # nothing was asked to write, at all, anywhere.
        for call in self.gh_calls():
            self.assertNotIn("--method", call, call)
            self.assertNotIn("close", call, call)

    def test_the_proposal_template_is_gone_from_the_contributed_skill(self):
        """`contrib/sd-propose-skills/` no longer writes a vault note.

        The criterion asks for the note kind to be absent, and the check is
        the whole directory rather than `SKILL.md`: a template moved into a
        sibling file would satisfy a check that read one file and would still
        be the writing path the row replaced.
        """
        directory = REPO_ROOT / "contrib" / "sd-propose-skills"
        found = [path.relative_to(REPO_ROOT).as_posix()
                 for path in sorted(directory.rglob("*"))
                 if path.is_file() and "skill-proposal" in path.read_text(encoding="utf-8")]
        self.assertEqual([], found)


class WhatTheRowRecords(SuggestCase):
    """The body, the mode and the item the row is keyed to."""

    def test_the_mode_is_recorded_on_the_row_and_not_consulted_for_permission(self):
        """`guest` writes the row too; the mode is a fact, not a gate."""
        self.local_block("mode: guest")
        self.run_verb(sd_suggest.suggest_add, item="an-item", body="a thing")
        self.assertEqual(["[guest] a thing"], self.proposals())

    def test_the_row_hangs_off_the_item_the_flag_names(self):
        self.run_verb(sd_suggest.suggest_add, item="an-item", body="a thing")
        row = self.connection.execute(
            "SELECT item FROM note WHERE kind = ?", (sd_suggest.PROPOSAL,)).fetchone()
        self.assertEqual(self.item, row["item"])

    def test_the_write_prints_the_publish_line_that_would_file_it(self):
        """The id is only useful if the run that made it says how to use it."""
        _, out = self.run_verb(sd_suggest.suggest_add, item="an-item", body="a thing")
        note = self.connection.execute(
            "SELECT id FROM note WHERE kind = ?", (sd_suggest.PROPOSAL,)).fetchone()["id"]
        self.assertIn(f"--note {note}", out)


class WhatItRefuses(SuggestCase):
    """The states where writing or filing would be worse than stopping."""

    def test_add_refuses_an_item_directory_that_is_not_there(self):
        with self.assertRaises(sd_handoff_rows.RowsRefusal) as raised:
            self.run_verb(sd_suggest.suggest_add, item="no-such-item", body="a thing")
        self.assertIn("no work item at", str(raised.exception))
        self.assertEqual([], self.proposals())

    def test_add_refuses_an_item_the_database_has_never_seen(self):
        """Missing import refuses capture; running a read-only report cannot repair it."""
        (self.root / sd_lib.WORK_DIR / "unseen").mkdir()
        items_before = list(self.connection.execute("SELECT id FROM item"))
        with self.assertRaises(sd_handoff_rows.RowsRefusal) as raised:
            self.run_verb(sd_suggest.suggest_add, item="unseen", body="a thing")
        self.assertIn("Import the work item before recording a proposal", str(raised.exception))
        self.assertIn("`sd-status` only reports", str(raised.exception))
        self.assertEqual(items_before, list(self.connection.execute("SELECT id FROM item")))
        self.assertEqual([], self.proposals())
        self.assertEqual([], self.issue_calls())

    def test_publish_refuses_a_note_id_that_is_not_a_proposal(self):
        """A followup id is not a suggestion, and filing one would be a surprise."""
        followup = sd_db.add_note(self.connection, self.item, "followup", "not a proposal")
        with self.assertRaises(sd_handoff_rows.RowsRefusal) as raised:
            self.run_verb(sd_suggest.suggest_publish, note=followup,
                          belongs_to=str(self.root), to="")
        self.assertIn(f"no proposal note with id {followup}", str(raised.exception))
        self.assertEqual([], self.issue_calls())

    def test_publish_refuses_a_checkout_the_database_does_not_carry(self):
        """`capture_task` would refuse it too; this names the flag that was wrong."""
        note = self.note_id()
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        before = self.items()
        with self.assertRaises(sd_handoff_rows.RowsRefusal) as raised:
            self.run_verb(sd_suggest.suggest_publish, note=note,
                          belongs_to=str(elsewhere), to="")
        self.assertIn("is not a registered repository", str(raised.exception))
        self.assertEqual(before, self.items())


class TheDedupRead(SuggestCase):
    """The dedup read, actually made, before filing."""

    def publish(self, note: int) -> tuple[int, str]:
        return self.run_verb(sd_suggest.suggest_publish, note=note,
                             belongs_to=str(self.root), to="")

    def open_item(self, title: str) -> int:
        return sd_db.writes.create_item(
            self.connection, kind="task", title=title, status="planning",
            repo=self.repo)

    def test_a_title_already_open_files_nothing_and_says_where_it_is(self):
        note = self.note_id("the installer leaves a stale lock")
        existing = self.open_item("the installer leaves a stale lock")
        before = self.items()

        code, out = self.publish(note)

        self.assertEqual(0, code)
        self.assertIn(f"already open as {self.root.name} sd:{existing}; filed nothing", out)
        self.assertEqual(before, self.items())

    def test_the_match_ignores_case_and_surrounding_space(self):
        """Two people naming the same friction do not type the same capitals."""
        note = self.note_id("The Installer Leaves A Stale Lock")
        self.open_item("  the installer leaves a stale lock  ")
        before = self.items()
        code, out = self.publish(note)
        self.assertEqual(0, code)
        self.assertIn("already open as", out)
        self.assertEqual(before, self.items())

    def test_the_same_title_in_another_checkout_does_not_hold_it_back(self):
        """The read is scoped to the checkout `--belongs-to` names."""
        note = self.note_id("the installer leaves a stale lock")
        other = sd_lib.stored_repo(self.home / "other")
        sd_db.writes.upsert_repo(self.connection, other)
        sd_db.writes.create_item(
            self.connection, kind="task", title="the installer leaves a stale lock",
            status="planning", repo=other)
        before = self.items()
        _, out = self.publish(note)
        self.assertEqual(len(before) + 1, len(self.items()))
        self.assertRegex(out, r"filed \S+ sd:\d+ in ")

    def test_a_done_item_with_the_title_does_not_hold_a_new_one_back(self):
        """A closed item is history; the friction came back, so it files again."""
        note = self.note_id("the installer leaves a stale lock")
        done = self.open_item("the installer leaves a stale lock")
        self.connection.execute("UPDATE item SET status = 'done' WHERE id = ?", (done,))
        self.connection.commit()
        before = self.items()
        _, out = self.publish(note)
        self.assertEqual(len(before) + 1, len(self.items()))
        self.assertRegex(out, r"filed \S+ sd:\d+ in ")

    def test_the_title_is_the_first_line_of_the_body(self):
        """A body is a paragraph; an item title is one line, and a bounded one."""
        note = self.note_id("a short first line\nand a second paragraph")
        before = self.items()
        self.publish(note)
        added = [row for row in self.items() if row not in before][0]
        self.assertEqual("a short first line", added["title"])
        body = json.loads(added["body"])["text"]
        self.assertTrue(body.startswith("a short first line\nand a second paragraph"), body)
        self.assertIn(f"From proposal note {note}.", body)


class TheMentionWindow(unittest.TestCase):
    """sd:8200: a Jira run reads mentions inside `sd.jira_recent_days`; unset is the library's 30."""

    def test_the_jira_run_is_given_the_configured_days(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "sd-ai-command-pack" / "config.json"
            path.parent.mkdir(parents=True)
            asked = []
            library = MagicMock(TRACKERS=("github", "jira"))
            library.sync_shadow.side_effect = lambda _connection, **options: asked.append(options)
            rows = MagicMock(library=lambda: library)
            for written, days in (({"jira_recent_days": "7"}, 7), ({}, "absent")):
                with self.subTest(days=days), patch.dict(os.environ, {"XDG_CONFIG_HOME": home}), \
                        patch.object(sd_shadow, "_rows", return_value=rows), \
                        patch.object(sd_shadow, "report_sync", return_value=0), \
                        patch.object(sd_shadow.importlib.util, "find_spec", return_value=True):
                    path.write_text(json.dumps({"config": {"sd": written}}), encoding="utf-8")
                    asked.clear()
                    sd_shadow.shadow_sync(MagicMock(since=None, until=None, max_requests=None,
                                                    max_seconds=None, strict=False))
                    self.assertEqual([options.get("mention_days", "absent") for options in asked],
                                     ["absent", days])


class TheShadowSync(SuggestCase):
    """The verb over `sd_db.sync_shadow`: what it reports and what it returns."""

    def test_a_held_cursor_is_reported_and_the_rows_are_still_written(self):
        """The collector writes rows on a partial collect; the verb says both."""
        self.environment(GH_AUTH_CODE="1")
        code, out = self.run_verb(sd_shadow.shadow_sync, strict=False)
        self.assertEqual(0, code)
        self.assertIn("cursor held", out)
        self.assertNotIn("cursor moved", out)

    def test_strict_turns_a_held_cursor_into_a_non_zero_exit(self):
        """The same run, for a caller that schedules it and wants to be told."""
        self.environment(GH_AUTH_CODE="1")
        code, _ = self.run_verb(sd_shadow.shadow_sync, strict=True)
        self.assertEqual(1, code)

    def test_a_successful_collect_moves_the_cursor_and_says_from_where(self):
        self.environment(GH_SEARCH=json.dumps(TWO_OPEN_ISSUES))
        _, out = self.run_verb(sd_shadow.shadow_sync, strict=True)
        self.assertIn("cursor moved to cover from", out)

    def test_the_four_buckets_are_all_asked_and_the_union_is_by_url(self):
        """Four searches, one page each, two rows: the union is the collector's."""
        self.environment(GH_SEARCH=json.dumps(TWO_OPEN_ISSUES))
        self.run_verb(sd_shadow.shadow_sync, strict=True)
        searches = [call for call in self.gh_calls() if "graphql" in call]
        # The four buckets, then the three pull request alert searches: author and requested (sd:6712), and
        # mentioned (sd:8142), then the issue mention search (sd:8142).
        self.assertEqual(8, len(searches))
        self.assertEqual(2, len(self.shadow_rows()))

    def test_a_second_run_updates_the_rows_rather_than_duplicating_them(self):
        """`shadow` is keyed by url, and a mirror that grew every run is a log."""
        self.environment(GH_SEARCH=json.dumps(TWO_OPEN_ISSUES))
        self.run_verb(sd_shadow.shadow_sync, strict=True)
        self.run_verb(sd_shadow.shadow_sync, strict=True)
        self.assertEqual(2, len(self.shadow_rows()))


class TheShadowSyncOverTrackers(unittest.TestCase):
    """sd:361 step 6: the verb runs every tracker the library names.

    The library is a fake here on purpose. The pinned `sd_db` may or may not
    export `TRACKERS`, and the Jira branches -- the `not collected` line, the
    strict exit that a failed configured collect earns, the recovery skip --
    are the verb's to prove whatever the pin holds. The one control that the
    pin can lag is `TheShadowSync` above, on the real library.
    """

    class Synced:
        """`Synced`-shaped, with `report()` under this test's control."""

        def __init__(self, *, ok=True, reason="", configured=True, lines=()):
            self.written, self.window_start = 0, "2026-09-06T10:00:00Z"
            self.watermark_moved, self.ok = ok, ok
            self.reason, self.truncated, self.configured = reason, [], configured
            self._lines = list(lines)

        def report(self):
            return ["shadow sync: 0 row(s), watermark moved", *self._lines]

    class Library:
        """The two names the verb reads off `sd_db`, and the calls it made.

        `trackers=None` leaves `TRACKERS` unset altogether, as a pin before
        system #338 does.
        """

        def __init__(self, answers, trackers=("github", "jira")):
            self.answers, self.calls = answers, []
            if trackers is not None:
                self.TRACKERS = trackers

        def sync_shadow(self, connection, **options):
            # Defaulted as the library defaults it, so a verb that never
            # passes `tracker` fails these tests on their assertions rather
            # than on a `KeyError` in the fake.
            self.calls.append(options)
            return self.answers[options.get("tracker", "github")]

    def sync(self, answers, trackers=("github", "jira"), **fields):
        library = self.Library(answers, trackers)
        output = io.StringIO()
        with patch.object(sd_handoff_rows, "library", return_value=library), \
                patch.object(sd_handoff_rows, "connect", return_value=MagicMock()), \
                contextlib.redirect_stdout(output):
            code = sd_shadow.shadow_sync(Args(**fields))
        return code, output.getvalue().splitlines(), library.calls

    def test_an_unconfigured_tracker_is_one_line_and_does_not_fail_strict(self):
        """The item's second acceptance line: unconfigured Jira is not a failure."""
        code, lines, calls = self.sync({
            "github": self.Synced(lines=["shadow sync: contribution detail backlog: 3 queued"]),
            "jira": self.Synced(ok=False, configured=False,
                                reason="JIRA_BASE_URL and JIRA_EMAIL not set"),
        }, strict=True)
        self.assertEqual(0, code, lines)
        self.assertIn("shadow sync[jira]: not collected (JIRA_BASE_URL and JIRA_EMAIL not set)",
                      lines)
        self.assertEqual(1, len([line for line in lines if "[jira]" in line]), lines)
        self.assertEqual(["github", "jira"], [call.get("tracker") for call in calls])

    def test_a_forwarded_line_loses_the_library_head_before_gaining_the_verbs(self):
        """`Synced.report` writes `shadow sync: ` on every line; the verb must
        not stack its own head on top of it."""
        _, lines, _ = self.sync({
            "github": self.Synced(lines=["shadow sync: contribution detail backlog: 3 queued",
                                         "a line without the head"]),
            "jira": self.Synced(ok=False, configured=False, reason="JIRA_BASE_URL not set"),
        })
        self.assertIn("shadow sync[github]: contribution detail backlog: 3 queued", lines)
        self.assertIn("shadow sync[github]: a line without the head", lines)
        self.assertEqual([], [line for line in lines if "shadow sync[github]: shadow sync:" in line])
        self.assertTrue(all(line.startswith("shadow sync[") for line in lines), lines)

    def test_a_configured_tracker_that_fails_still_fails_strict(self):
        """CONTROL for the line above: `configured` is what earns the pass,
        not the tracker's name."""
        reason = ("Jira rejected the credentials (401 Unauthorized); "
                  "check JIRA_EMAIL and JIRA_API_TOKEN")
        code, lines, _ = self.sync({
            "github": self.Synced(),
            "jira": self.Synced(ok=False, configured=True, reason=reason),
        }, strict=True)
        self.assertEqual(1, code, lines)
        self.assertIn(f"shadow sync[jira]: cursor held: {reason}", lines)
        self.assertEqual(f"shadow sync[jira]: cursor held: {reason}", lines[3], lines)

    def test_an_unwindowed_custom_query_says_it_covered_no_window(self):
        """pack sd:5376: a `JIRA_JQL` run succeeds with no window and holds the cursor."""
        custom = self.Synced()
        custom.window_start, custom.watermark_moved = "", False
        code, lines, _ = self.sync({"github": self.Synced(), "jira": custom}, strict=True)
        self.assertEqual(0, code, lines)
        self.assertIn("shadow sync[jira]: custom JIRA_JQL ran unwindowed; cursor held", lines)
        self.assertEqual([], [line for line in lines if "from ;" in line or "from  " in line], lines)

    def test_a_bounded_run_is_githubs_alone_and_says_so_for_the_rest(self):
        """The 2026-09-12 decision: the recovery flags cannot reach Jira."""
        code, lines, calls = self.sync({
            "github": self.Synced(),
            "jira": self.Synced(ok=False, configured=True, reason="never asked"),
        }, since=datetime(2026, 9, 6, 10, tzinfo=timezone.utc))
        self.assertEqual(0, code, lines)
        self.assertEqual(["github"], [call.get("tracker") for call in calls])
        self.assertIn("shadow sync[jira]: skipped (recovery window is GitHub's)", lines)

    def test_a_pin_without_the_export_runs_github_alone(self):
        """The `getattr` default, so the verb's tests pass at either pin."""
        self.assertFalse(hasattr(self.Library({}, None), "TRACKERS"))
        code, lines, calls = self.sync({"github": self.Synced()}, trackers=None)
        self.assertEqual(0, code, lines)
        self.assertEqual(["github"], [call.get("tracker") for call in calls])
        self.assertEqual([], [line for line in lines if "[jira]" in line], lines)


class TheShadowRecoveryArguments(unittest.TestCase):
    """Bad recovery bounds must stop before the library or database is opened."""

    @classmethod
    def setUpClass(cls):
        cls.command = load("sd_shadow_command", "sd")

    def refuse_before_database(self, *options):
        error = io.StringIO()
        with patch.object(sd_handoff_rows, "library") as library, \
                patch.object(sd_handoff_rows, "connect") as connect, \
                contextlib.redirect_stderr(error):
            code = self.command.main(["shadow", "sync", *options])
        self.assertNotEqual(0, code, options)
        self.assertNotIn("Traceback", error.getvalue())
        library.assert_not_called()
        connect.assert_not_called()
        return error.getvalue()

    def test_invalid_or_naive_timestamps_refuse_before_database_access(self):
        for flag in ("--since", "--until"):
            for value in ("2026-09-06", "2026-09-06T10:00:00", "2026-09-06 10:00:00Z",
                          "2026-02-30T10:00:00Z", "2026-09-06T25:00:00Z",
                          "2026-09-06T10:00:00+01:60", "2026-09-06T10:00:00+25:00",
                          "not-a-time"):
                with self.subTest(flag=flag, value=value):
                    self.assertIn("timestamp", self.refuse_before_database(flag, value))

    def test_invalid_limits_refuse_before_database_access(self):
        cases = {
            "--max-requests": ("0", "-1", "1.5", "nan", "inf", "bad"),
            "--max-seconds": ("0", "-1", "nan", "inf", "-inf", "1e999", "bad"),
        }
        for flag, values in cases.items():
            for value in values:
                with self.subTest(flag=flag, value=value):
                    self.assertIn("positive", self.refuse_before_database(f"{flag}={value}"))

    def test_reversed_and_future_windows_refuse_before_database_access(self):
        for options in (
            ("--since", "2026-09-07T00:00:00Z", "--until", "2026-09-06T00:00:00Z"),
            ("--since", "9999-01-01T00:00:00Z"),
            ("--until", "9999-01-01T00:00:00Z"),
        ):
            with self.subTest(options=options):
                self.refuse_before_database(*options)


class TheShadowSyncRefusals(unittest.TestCase):
    """sd:2898: the library's two refusals are one stderr line and an exit code each.

    Since system #199 `sync_shadow` raises `SyncBusy` while another sync holds
    the lock, and `HubOnly` on a satellite. The real classes are raised here,
    so the verb is proven against the pinned library's types, not stand-ins.
    """

    @classmethod
    def setUpClass(cls):
        cls.command = load("sd_shadow_refusal_command", "sd")

    def refused(self, error):
        library = MagicMock(TRACKERS=("github", "jira"))
        library.sync_shadow.side_effect = error
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sd_handoff_rows, "library", return_value=library), \
                patch.object(sd_handoff_rows, "connect", return_value=MagicMock()) as connect, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.command.main(["shadow", "sync", "--strict"])
        connect.return_value.close.assert_called_once()
        self.assertEqual("", out.getvalue())
        return code, err.getvalue().splitlines()

    def test_a_held_lock_is_one_line_and_the_busy_code(self):
        from sd_db.shadow_sync import SyncBusy

        code, lines = self.refused(SyncBusy("another shadow sync is already running"))
        self.assertEqual(sd_shadow.EXIT_BUSY, code)
        self.assertEqual(["sd: another shadow sync is already running"], lines)

    def test_a_satellite_is_one_line_and_the_hub_only_code(self):
        from sd_db.remote import HubOnly

        code, lines = self.refused(HubOnly("shadow sync", "hub.example.test"))
        self.assertEqual(sd_shadow.EXIT_HUB_ONLY, code)
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].startswith("sd: shadow sync runs on the sd hub only;"), lines)

    def test_the_two_codes_differ_from_each_other_and_from_strict(self):
        self.assertEqual(4, len({0, 1, sd_shadow.EXIT_BUSY, sd_shadow.EXIT_HUB_ONLY}))

    def test_a_library_without_sync_busy_still_maps_hub_only(self):
        """A library older than system #199 has no `SyncBusy`; the verb still runs there."""
        from sd_db import remote

        older = MagicMock(spec=[])
        real = importlib.import_module
        with patch.object(sd_shadow.importlib, "import_module",
                          side_effect=lambda name: older if name == "sd_db.shadow_sync" else real(name)):
            self.assertEqual({remote.HubOnly: sd_shadow.EXIT_HUB_ONLY}, sd_shadow.refusals())

    def test_the_help_names_both_codes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, self.command.main(["shadow", "sync", "--help"]))
        text = " ".join(out.getvalue().split())
        self.assertIn(f"{sd_shadow.EXIT_HUB_ONLY} on a satellite", text)
        self.assertIn(f"{sd_shadow.EXIT_BUSY} while another shadow sync holds the lock", text)


class TheShadowSyncLock(SuggestCase):
    """CONTROL for the refusals above, on the real library: a held lock reaches the CLI as exit 4."""

    def test_a_sync_under_a_held_lock_is_refused_in_one_line(self):
        from sd_db.shadow_sync import sync_lock

        command = load("sd_shadow_lock_command", "sd")
        out, err = io.StringIO(), io.StringIO()
        with sync_lock(self.connection), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = command.main(["shadow", "sync", "--strict"])
        self.assertEqual(sd_shadow.EXIT_BUSY, code, err.getvalue())
        self.assertEqual(1, len(err.getvalue().splitlines()), err.getvalue())
        self.assertIn("another shadow sync is already running", err.getvalue())
        self.assertEqual([], self.shadow_rows())


class TheShadowRecoveryWindow(SuggestCase):
    """The real collector receives explicit controls and preserves its cursor contract."""

    @classmethod
    def setUpClass(cls):
        cls.command = load("sd_shadow_window_command", "sd")

    def command_sync(self, *options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = self.command.main(["shadow", "sync", *options])
        return code, output.getvalue()

    def test_ordinary_sync_keeps_the_library_defaults(self):
        self.environment(GH_SEARCH=json.dumps(TWO_OPEN_ISSUES))
        with patch.object(sd_db, "sync_shadow", wraps=sd_db.sync_shadow) as sync:
            code, _ = self.command_sync("--strict")
        self.assertEqual(0, code)
        # One call per tracker the library names, in its order, carrying the
        # tracker and nothing else: the limits stay the library's defaults.
        # A scheduled run also files alerts: GitHub's pull request and issue alerts (sd:6712), Jira's issue
        # alerts (sd:8142).
        self.assertEqual([{"tracker": name, "alerts": True} for name in sd_db.TRACKERS],
                         [call.kwargs for call in sync.call_args_list])
        self.assertEqual(2, len(self.shadow_rows()))

    def test_explicit_bounds_and_limits_reach_the_real_library(self):
        self.environment(GH_SEARCH=json.dumps(TWO_OPEN_ISSUES))
        with patch.object(sd_db, "sync_shadow", wraps=sd_db.sync_shadow) as sync:
            code, _ = self.command_sync(
                "--strict", "--since", "2026-09-06T12:00:00.750+02:00",
                "--until", "2026-09-06T11:00:00.250Z",
                "--max-requests", "4", "--max-seconds", "15.5")
        self.assertEqual(0, code)
        # Bounded, so GitHub's call is the only one; the flags are its.
        self.assertEqual([{
            "tracker": "github",
            "since": datetime(2026, 9, 6, 10, tzinfo=timezone.utc),
            "now": datetime(2026, 9, 6, 11, tzinfo=timezone.utc),
            "max_requests": 4, "max_seconds": 15.5,
        }], [call.kwargs for call in sync.call_args_list])
        self.assertEqual(2, len(self.shadow_rows()))

    def test_one_optional_limit_does_not_override_other_defaults(self):
        self.environment(GH_SEARCH=json.dumps(TWO_OPEN_ISSUES))
        with patch.object(sd_db, "sync_shadow", wraps=sd_db.sync_shadow) as sync:
            code, _ = self.command_sync("--strict", "--max-requests", "4")
        self.assertEqual(0, code)
        self.assertEqual([{"tracker": name, "max_requests": 4, "alerts": True} for name in sd_db.TRACKERS],
                         [call.kwargs for call in sync.call_args_list])

    def synced(self, **fields):
        """A real `Synced`, so `report()` is the library's and not a stand-in."""
        from sd_db.shadow_sync import Synced

        defaults = {"ok": True, "reason": "", "written": 0, "truncated": [],
                    "window_start": "2026-09-06T10:00:00Z", "watermark_moved": True}
        return Synced(**{**defaults, **fields})

    def sync_returning(self, result, *options):
        """`result` is GitHub's answer. Every other tracker answers unconfigured,
        so the lines under test are one block and the counts below stay
        counts of one report rather than of one per tracker."""
        unconfigured = self.synced(ok=False, reason="JIRA_BASE_URL not set",
                                   watermark_moved=False, configured=False)

        def sync(connection, *, tracker, **_):
            return result if tracker == "github" else unconfigured

        with patch.object(sd_db, "sync_shadow", side_effect=sync):
            return self.command_sync(*options)

    def test_a_moved_cursor_says_moved_even_when_the_collect_failed(self):
        """sd:614. `ok` is the search's success; `watermark_moved` is the cursor's.

        Two nights of logs said the cursor was held when it had in fact moved,
        because one incomplete contribution observation made `ok` false while
        the search itself had advanced. Reading the cursor line off `ok` is
        what produced that, so it is read off the cursor.
        """
        code, output = self.sync_returning(
            self.synced(ok=False, reason="one observation incomplete",
                        watermark_moved=True), "--strict")
        self.assertIn("cursor moved", output)
        self.assertNotIn("cursor held", output)
        self.assertIn("one observation incomplete", output)
        self.assertEqual(1, code, output)

    def test_a_held_cursor_on_a_failed_collect_still_says_held(self):
        """CONTROL. The case the existing tests cover must not change."""
        code, output = self.sync_returning(
            self.synced(ok=False, reason="request budget spent",
                        watermark_moved=False), "--strict")
        self.assertIn("cursor held: request budget spent", output)
        self.assertNotIn("cursor moved", output)
        self.assertEqual(1, code, output)

    def test_a_held_cursor_on_a_good_collect_is_not_a_failure(self):
        """CONTROL. Complete coverage of an old window keeps the later cursor."""
        code, output = self.sync_returning(
            self.synced(watermark_moved=False), "--strict")
        self.assertIn("existing cursor retained", output)
        self.assertNotIn("cursor held", output)
        self.assertEqual(0, code, output)

    class Reporting:
        """A `Synced`-shaped result whose `report()` this test controls.

        NOT a convenience, and no longer a workaround either. It was written
        when CI pinned a commit predating `queued` and `incomplete`, so
        `Synced(queued=3)` raised `TypeError` there while passing on every
        local venv; the pin has since moved past #301 and that asymmetry is
        gone. The stub stays for the reason that outlives the pin: naming the
        library's newer fields would make this test assert the library's
        schema, and what is under test is the VERB's forwarding, which must
        hold whatever `report()` returns -- including a line the library
        learns to emit after this test was written.
        """

        def __init__(self, lines, *, ok=True, reason="", truncated=()):
            self.written, self.window_start = 0, "2026-09-06T10:00:00Z"
            self.watermark_moved, self.ok = True, ok
            self.reason, self.truncated = reason, list(truncated)
            self._lines = list(lines)

        def report(self):
            return ["shadow sync: 0 row(s), watermark moved", *self._lines]

    def report(self, result, *, strict=False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = self.command.sd_shadow.report_sync(result, name="github", strict=strict)
        return code, output.getvalue()

    def test_the_backlog_and_the_incomplete_observations_reach_the_log(self):
        """sd:614. The verb printed neither, so a growing queue was invisible.

        The 213-deep backlog of 2026-09-12 showed only in the contribution-sync
        heartbeat; the nightly log said nothing.
        """
        code, output = self.report(self.Reporting([
            "shadow sync: contribution observation incomplete: item:42: rate limited",
            "shadow sync: contribution detail backlog: 3 queued",
        ]))
        self.assertIn("contribution detail backlog: 3 queued", output)
        self.assertIn("contribution observation incomplete: item:42: rate limited", output)
        self.assertEqual(0, code, output)

    def test_a_line_the_library_learns_to_emit_needs_no_edit_here(self):
        """The property, not the two lines that prompted it.

        A verb that named the fields it forwards would drop the next one in
        silence -- which is sd:602, in the file that just fixed sd:602.
        """
        _, output = self.report(self.Reporting(["shadow sync: something new: 4"]))
        self.assertIn("shadow sync[github]: something new: 4", output)
        self.assertNotIn("shadow sync[github]: shadow sync:", output)

    def test_the_library_line_the_verb_already_said_is_not_repeated(self):
        """CONTROL. Forwarding wholesale duplicates the truncation and reason."""
        _, output = self.report(
            self.Reporting(["shadow sync: truncated buckets: issues",
                            "shadow sync: request budget spent",
                            "shadow sync: contribution detail backlog: 3 queued"],
                           ok=False, reason="request budget spent",
                           truncated=["issues"]),
            strict=True)
        self.assertEqual(1, output.count("request budget spent"), output)
        self.assertNotIn("truncated buckets", output)
        self.assertIn("contribution detail backlog: 3 queued", output)

    def test_the_verb_does_not_say_the_same_thing_twice(self):
        """CONTROL. Forwarding the report wholesale would duplicate three lines."""
        _, output = self.sync_returning(
            self.synced(ok=False, reason="request budget spent",
                        truncated=["issues"], watermark_moved=False), "--strict")
        self.assertEqual(1, output.count("request budget spent"), output)
        self.assertEqual(1, len([line for line in output.splitlines()
                                 if "truncated" in line or "coverage is incomplete" in line]),
                         output)

    def test_saturated_single_second_window_fails_strict_and_holds_cursor(self):
        from sd_db.shadow_sync import read_watermark

        page = json.loads(json.dumps(TWO_OPEN_ISSUES))
        page["data"]["search"]["issueCount"] = 1001
        for node in page["data"]["search"]["nodes"]:
            node["updatedAt"] = "2026-09-06T10:00:00Z"
        self.environment(GH_SEARCH=json.dumps(page))
        code, output = self.command_sync(
            "--strict", "--since", "2026-09-06T10:00:00Z",
            "--until", "2026-09-06T10:00:00Z")
        self.assertEqual(1, code, output)
        self.assertIn("coverage is incomplete", output)
        self.assertIn("cursor held", output)
        self.assertIsNone(read_watermark(self.connection))
        self.assertEqual(2, len(self.shadow_rows()))

    def test_request_exhaustion_fails_strict_and_keeps_partial_rows(self):
        from sd_db.shadow_sync import read_watermark

        self.environment(GH_SEARCH=json.dumps(TWO_OPEN_ISSUES))
        code, output = self.command_sync("--strict", "--max-requests", "1")
        self.assertEqual(1, code, output)
        self.assertIn("cursor held", output)
        self.assertIsNone(read_watermark(self.connection))
        self.assertEqual(2, len(self.shadow_rows()))
        self.assertEqual(1, len([call for call in self.gh_calls() if "graphql" in call]))

    def test_time_exhaustion_fails_strict_without_moving_cursor(self):
        from sd_db.shadow_sync import read_watermark

        self.environment(GH_SEARCH=json.dumps(TWO_OPEN_ISSUES))
        code, output = self.command_sync("--strict", "--max-seconds", "0.000000001")
        self.assertEqual(1, code, output)
        self.assertIn("cursor held", output)
        self.assertIsNone(read_watermark(self.connection))
        self.assertEqual([], [call for call in self.gh_calls() if "graphql" in call])

    def test_complete_historical_window_retains_the_later_cursor(self):
        from sd_db.shadow_sync import read_watermark

        self.environment(GH_SEARCH=json.dumps(TWO_OPEN_ISSUES))
        code, _ = self.command_sync("--strict")
        self.assertEqual(0, code)
        previous = read_watermark(self.connection)
        code, output = self.command_sync(
            "--strict", "--since", "2026-09-06T10:00:00Z",
            "--until", "2026-09-06T11:00:00Z")
        self.assertEqual(0, code, output)
        self.assertIn("coverage completed", output)
        self.assertIn("existing cursor retained", output)
        self.assertNotIn("cursor moved", output)
        self.assertEqual(previous, read_watermark(self.connection))

    def test_complete_later_window_does_not_skip_an_uncovered_gap(self):
        from sd_db.shadow_sync import read_watermark

        page = json.loads(json.dumps(TWO_OPEN_ISSUES))
        for node in page["data"]["search"]["nodes"]:
            node["updatedAt"] = "2026-09-06T10:00:00Z"
        self.environment(GH_SEARCH=json.dumps(page))
        code, _ = self.command_sync(
            "--strict", "--since", "2026-09-06T10:00:00Z",
            "--until", "2026-09-06T10:00:00Z")
        self.assertEqual(0, code)
        previous = read_watermark(self.connection)
        for node in page["data"]["search"]["nodes"]:
            node["updatedAt"] = "2026-09-06T11:00:00Z"
        self.environment(GH_SEARCH=json.dumps(page))
        code, output = self.command_sync(
            "--strict", "--since", "2026-09-06T11:00:00Z",
            "--until", "2026-09-06T11:00:00Z")
        self.assertEqual(0, code, output)
        self.assertIn("existing cursor retained", output)
        self.assertEqual(previous, read_watermark(self.connection))


class TheRefusalReachesTheOperator(SuggestCase):
    """`bin/sd` had never caught `RowsRefusal`, and these two verbs raise it.

    The handler is nine lines and the rest of the file has no test that would
    notice if it went away: every other `RowsRefusal` reader is
    `bin/sd-handoff-restore`, a hook that swallows everything by design. So
    the verb is run as `bin/sd` actually runs it, in a subprocess, and what is
    asserted is what an operator sees.
    """

    def run_cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "sd"), *argv],
            cwd=self.root, capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_a_bad_item_is_one_sentence_and_not_a_traceback(self):
        done = self.run_cli("suggest", "add", "a thing", "--item", "no-such-item")
        self.assertEqual(1, done.returncode, done.stderr)
        self.assertTrue(done.stderr.startswith("sd: "), done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_publish_with_no_destination_refuses_through_the_same_handler(self):
        note = self.note_id()
        done = self.run_cli("suggest", "publish", "--note", str(note))
        self.assertEqual(1, done.returncode, done.stderr)
        self.assertIn("--belongs-to PATH", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_publish_through_the_group_files_an_sd_item(self):
        """`--belongs-to .` from inside the checkout, end to end, and no `gh` call."""
        note = self.note_id()
        before = len(self.items())
        done = self.run_cli("suggest", "publish", "--note", str(note), "--belongs-to", ".")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertRegex(done.stdout, r"filed \S+ sd:\d+ in ")
        self.assertEqual(before + 1, len(self.items()))
        self.assertEqual([], self.gh_calls())

    def test_the_retired_to_refuses_through_the_same_handler(self):
        note = self.note_id()
        done = self.run_cli("suggest", "publish", "--note", str(note), "--to", "o/r")
        self.assertEqual(1, done.returncode, done.stderr)
        self.assertIn("never a GitHub issue", done.stderr)
        self.assertEqual([], self.gh_calls())

    def test_the_row_the_group_writes_is_the_row_the_library_writes(self):
        """The wiring end to end, so the group is not a parser with no verb."""
        done = self.run_cli("suggest", "add", "a thing", "--item", "an-item")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(["[full] a thing"], self.proposals())


class WhatIsNotAPaletteEntry(unittest.TestCase):
    """Criterion 28 says filing must not be an installed entrypoint."""

    def test_there_is_no_sd_suggest_executable_in_bin(self):
        """A verb under `sd` is reachable; a `bin/sd-suggest` is *installed*.

        `bin/sd_suggest.py` is a module the group imports, and the suffix is
        the distinction the installer reads -- so the check is for the
        suffixless name, which is what would appear on a palette.
        """
        self.assertFalse((REPO_ROOT / "bin" / "sd-suggest").exists())
        self.assertTrue((REPO_ROOT / "bin" / "sd_suggest.py").exists())

    def test_the_two_verbs_are_reachable_only_through_the_group(self):
        """`sd suggest add` and `sd suggest publish`, and nothing else wires them."""
        text = (REPO_ROOT / "bin" / "sd").read_text(encoding="utf-8")
        self.assertIn("sd_suggest.suggest_add", text)
        self.assertIn("sd_suggest.suggest_publish", text)
        self.assertEqual(1, text.count('groups.add_parser(\n        "suggest"'))


class TheModuleLoader(unittest.TestCase):
    """The same guard as `tests/test_sd_handoff_rows.py`, on this copy.

    Two files carry this helper and either one re-executing `sd_lib` is
    enough to split the module in two, so each carries its own check. This
    was the copy that actually produced the failure: it sorts after
    `test_sd_lib` and so replaced the module that file had already bound.
    """

    def test_a_module_already_imported_is_not_executed_again(self) -> None:
        self.assertIs(load("sd_lib"), sys.modules["sd_lib"])
        self.assertIs(load("sd_lib"), sd_lib)

    def test_it_still_executes_a_module_that_is_not_imported_yet(self) -> None:
        """The control: a guard returning early for everything would pass the
        test above while loading nothing at all."""

        name = "sd_lib_under_another_name"
        sys.modules.pop(name, None)
        self.addCleanup(sys.modules.pop, name, None)
        fresh = load(name, "sd_lib.py")
        self.assertIs(sys.modules[name], fresh)
        self.assertIsNot(fresh, sd_lib)
        self.assertTrue(callable(fresh.parse_frontmatter))


if __name__ == "__main__":
    unittest.main()
