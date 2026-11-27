"""Every item reference a person reads names its repository: `app sd:12`, never a bare `sd:12` (sd:5124).

One fixture database holds items in two repositories and one in none. Each human surface renders from it,
and `bare` finds a `sd:<n>` or `#<n>` that names a fixture item with no repository word before it. A table
column headed ID may stay bare beside a Repo column; Note, PR and Number columns hold no item. Machine-read
text (`--json`, trailers, `--ref` keys) is out of scope and keeps the bare form.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.machinery
import importlib.util
import io
import os
import re
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import sd_db  # noqa: E402
import sd_lane  # noqa: E402
import sd_notes  # noqa: E402
import sd_views  # noqa: E402
import sd_work  # noqa: E402
from sd_db import brief, today  # noqa: E402
from sd_db.writes import add_note, transition  # noqa: E402

from tests.clean_env import clean_environment  # noqa: E402

APP = "~/repos/example.test/app"
LIB = "~/repos/example.test/lib"
NOW = "2026-09-06T18:00:00Z"
MOMENT = datetime.datetime(2026, 9, 6, 18, 0, tzinfo=datetime.timezone.utc)
#: The words a reference may start with: each fixture repository's short name, and the word for none.
WORDS = ("app", "lib", "fleet")
#: Table columns whose cells may hold a bare number: ID beside Repo, and columns that hold no item.
EXEMPT_HEADS = ("Note", "PR", "Number")
#: Words before `#<n>` that make it a note (`note #7`, `[followup #7`), a pull request or an issue, not an item.
NOT_ITEM = re.compile(r"(?:\[[\w-]+|note|PR|pull request|issue) $")
#: What an exempt cell holds: a bare number, as `#12` or `sd:12`.
CELL = re.compile(r"(?:#|sd:)?\d+")
TOKEN = re.compile(r"(?<![\w/.-])(sd:|#)(\d+)\b")


def _load(name: str, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(BIN / name))
    spec = importlib.util.spec_from_file_location(module_name, str(BIN / name), loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


status = _load("sd-status", "sd_status_item_refs")


def _blank_columns(lines: list[str]) -> list[str]:
    """Each table's exempt columns blanked: a dash rule marks a table, the line above it is its header."""
    out, spans = [], []
    for index, line in enumerate(lines):
        if line.strip() and set(line.strip()) <= {"-", " "} and index:
            head = lines[index - 1]
            runs = [(found.start(), found.end()) for found in re.finditer(r"-+", line)]
            names = [head[start:end].strip() if start < len(head) else "" for start, end in runs]
            spans = [run for run, name in zip(runs, names, strict=True)
                     if name in EXEMPT_HEADS or (name == "ID" and "Repo" in names)]
            out.append(line)
            continue
        if not line.strip():
            spans = []
        for start, end in spans:  # columns align to their rule, so a cell sits inside its dash run
            if CELL.fullmatch(line[start:end].strip()):  # a line under the table is no cell
                line = line[:start] + " " * len(line[start:end]) + line[end:]
        out.append(line)
    return out


def bare(text: str, items: set[int]) -> list[str]:
    """The lines holding a reference to one of `items` with no repository word before it."""
    found = []
    for line in _blank_columns(text.splitlines()):
        for match in TOKEN.finditer(line):
            if int(match.group(2)) not in items:
                continue
            before = line[:match.start()]
            if match.group(1) == "sd:" and before.endswith(tuple(f"{word} " for word in WORDS)):
                continue
            if match.group(1) == "#" and NOT_ITEM.search(before):
                continue
            found.append(line.strip())
            break
    return found


class ItemRefs(unittest.TestCase):
    """Each surface renders from one fixture and names every item with its repository."""

    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(unittest.mock.patch.dict(os.environ, clean_environment(HOME=str(self.home)), clear=True))
        sd_db.initialise(home=self.home)
        self.connection = sd_db.connect(home=self.home)
        self.addCleanup(self.connection.close)
        self.made = self.fill(self.connection)
        self.items = set(self.made.values())

    def fill(self, connection) -> dict[str, int]:
        sd_db.upsert_repo(connection, APP, remote="https://github.com/example-owner/app")
        sd_db.upsert_repo(connection, LIB, remote="https://github.com/example-owner/lib")
        made = {}

        def item(name, title, **fields):
            made[name] = sd_db.create_item(connection, kind=fields.pop("kind", "task"), title=title, **fields)
            return made[name]

        item("report", "nightly-sweep: needs attention", kind="report", fields={"attention": "two stale rows"})
        asked = item("asked", "Pick a store", repo=APP)
        add_note(connection, asked, "question", "Which store holds the rows?\nOption: Pack (Recommended)\nOption: Database")
        owed = item("owed", "Owed work", repo=LIB, priority=2)
        add_note(connection, owed, "followup", "Chase the vendor")
        stuck = item("stuck", "Wire the webhook", repo=LIB)
        transition(connection, stuck, "blocked", who="operator", reason="Waits on the vendor's API key")
        item("planned", "Renew the certificate", repo=LIB, due="2026-09-09")
        item("loose", "A fleet chore", priority=1)
        connection.execute("UPDATE item SET created_at = '2026-09-01T09:00:00Z', updated_at = '2026-09-01T09:00:00Z'")
        connection.execute("UPDATE note SET timestamp = '2026-09-06T15:00:00+00:00'")
        connection.commit()
        return made

    def assertNamed(self, text: str) -> None:
        self.assertTrue(text.strip(), "the surface rendered nothing")
        self.assertEqual(bare(text, self.items), [], text)

    def test_the_checker_finds_a_bare_reference(self) -> None:
        number = self.made["owed"]
        self.assertEqual(bare(f"next: sd:{number} first", self.items), [f"next: sd:{number} first"])
        self.assertEqual(bare(f"next: #{number} first", self.items), [f"next: #{number} first"])
        self.assertEqual(bare(f"next: lib sd:{number} first, note #{number}", self.items), [])

    def test_sd_today(self) -> None:
        found = today.sections(self.connection, now=NOW)
        self.assertNamed(sd_views.render_sections(found, now=MOMENT, width=160))

    def test_sd_task_list_and_show(self) -> None:
        rows = [dict(sd_db.item_by_id(self.connection, item)) for item in sorted(self.items)]
        with contextlib.redirect_stdout(io.StringIO()) as said:
            sd_work._print_rows(rows)
        self.assertNamed(said.getvalue())
        self.assertIn(f"lib sd:{self.made['owed']}  planning  Owed work", said.getvalue())
        self.assertIn(f"fleet sd:{self.made['loose']}  planning  A fleet chore", said.getvalue())

    def test_sd_note_list(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(sd_notes.cmd_list(types.SimpleNamespace(item=self.made["owed"]), out, err), 0, err.getvalue())
        self.assertNamed(out.getvalue())

    def test_the_note_brief(self) -> None:
        held = brief.note_brief(self.connection, LIB, branch=None)
        self.assertNamed(held.text)

    def test_sd_status_merge_lane_and_its_failed_rows(self) -> None:
        lane = {"available": True, "reason": "", "repository": "example-owner/lib", "entries": [
            {"item": self.made["stuck"], "branch": "task/webhook", "status": "failed", "step": "prepare",
             "reason": "the gate failed"},
            {"item": self.made["planned"], "branch": "task/cert", "status": "pending"}]}
        stream = io.StringIO()
        status._render_lane(lane, stream.write)
        self.assertNamed(stream.getvalue())
        titles = "\n".join(row["title"] for row in status._lane_rows(lane))
        self.assertNamed(titles)

    def test_sd_ship_lane_watch(self) -> None:
        stuck, planned = self.made["stuck"], self.made["planned"]
        queue = types.SimpleNamespace(repository="example-owner/lib")
        ended = {stuck: ({"status": "failed", "reason": "a blocking finding"}, None),
                 planned: ({"status": "merged", "merge_commit": "c0ffee" * 7}, None)}
        lines: list[str] = []
        modes: list[bool] = []

        def read(queue: object, work: object, *, write: bool = True) -> dict:
            modes.append(write)
            return ended
        with unittest.mock.patch.object(sd_lane, "on_hub", read):
            self.assertEqual(sd_lane.watch_items(queue, [stuck, planned], lines.append), 1)
        self.assertEqual(modes, [False], "the watch poll reads without the write lock (sd:8819)")
        self.assertNamed("\n".join(lines))


if __name__ == "__main__":
    unittest.main()
