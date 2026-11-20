"""The one text layout of `sd today` and `sd-status` (sd:5098), pinned as exact text.

A fixture database with fixed timestamps, a fixed `now` and a fixed zone, so
the golden text cannot drift: a changed column, order, cap or cell fails here.
"""

from __future__ import annotations

import datetime
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import sd_db  # noqa: E402
import sd_views  # noqa: E402
from sd_db import review, today  # noqa: E402
from sd_db.writes import add_note, transition  # noqa: E402

from tests.clean_env import clean_environment  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-06T18:00:00Z"
MOMENT = datetime.datetime(2026, 9, 6, 18, 0, tzinfo=datetime.timezone.utc)
APP = "~/repos/example.test/app"
LIB = "~/repos/example.test/lib"
SHA = "c0ffee" + "0" * 34


def fill(connection) -> dict[str, int]:
    """One row or more for every section, all stamped at fixed times."""
    sd_db.upsert_repo(connection, APP, remote="https://github.com/example-owner/app")
    sd_db.upsert_repo(connection, LIB, remote="https://github.com/example-owner/lib")
    made = {}

    def item(name, title, **fields):
        made[name] = sd_db.create_item(connection, kind=fields.pop("kind", "task"), title=title, **fields)
        return made[name]

    item("report", "nightly-sweep: needs attention", kind="report", repo=APP, fields={"attention": "two stale rows"})
    asked = item("asked", "Pick a store", repo=APP)
    add_note(connection, asked, "question", "Which store holds the rows?\nOption: Pack (Recommended)\nOption: Database")
    for number in range(1, 8):
        owed = item(f"owed{number}", f"Owed work number {number}", repo=LIB, priority=number % 3)
        add_note(connection, owed, "followup", f"Chase the vendor about {number}")
    shipped = item("shipped", "Ship the parser", repo=APP, status="in_progress")
    transition(connection, shipped, "done", who="sd-ship", reason=f"delivered at {SHA} on origin/main")
    add_note(connection, shipped, "comment", f"Code delivery https://github.com/example-owner/app/pull/42 at {SHA}")
    running = item("running", "Land the cache", repo=APP)
    waiting = item("waiting", "Land the index", repo=APP)
    for number, minutes in enumerate((20, 30, 40)):
        lane(connection, "example-owner/app", f"m{number}", item=900 + number, status="merged", position=number,
             started_at="2026-09-05T10:00:00Z", finished_at=f"2026-09-05T10:{minutes:02d}:00Z")
    lane(connection, "example-owner/app", "r", item=running, status="running", position=5, step="prepare",
         started_at="2026-09-06T17:50:00Z")
    lane(connection, "example-owner/app", "w", item=waiting, status="pending", position=6)
    stuck = item("stuck", "Wire the webhook", repo=LIB)
    transition(connection, stuck, "blocked", who="operator", reason="Waits on the vendor's API key")
    item("planned", "Renew the certificate", repo=LIB, due="2026-09-09")
    item("late", "Rotate the token", repo=APP, due="2026-09-01", status="in_progress")
    transition(connection, item("swept", "Close a stale row", repo=APP), "done", who="operator", reason="bulk close")
    connection.execute("UPDATE item SET created_at = '2026-09-01T09:00:00Z', updated_at = '2026-09-01T09:00:00Z'")
    connection.execute("UPDATE note SET timestamp = '2026-09-06T15:00:00+00:00'")
    connection.commit()
    return made


def lane(connection, repository, entry, **body):
    connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                       (f"{today.LANE_KEY_PREFIX}{repository}:{entry}", NOW, json.dumps({"id": entry, **body})))


GATE = {"holders": [{"slot": 1, "pid": 7, "label": "app tests", "since": "2026-09-06T17:55:00Z", "cwd": "/tmp"}],
        "waiters": [{"place": 1, "pid": 8, "label": "lib tests", "since": "2026-09-06T17:58:00Z", "cwd": "/tmp"}]}

TODAY = """\
1 attention · 0 on this machine · 1 decisions · 2 landing · 2 queues · 1 delivered · 1 planned this week · 1 blocked · 8 unscheduled · 7 followups · 1 work

Attention (1)
ID  Repo  State     Why             Title                           When
--  ----  --------  --------------  ------------------------------  -------
#1  app   planning  two stale rows  nightly-sweep: needs attention  5 d ago

Operations, this machine (0)
Gate slots: 1 holding (app tests since 11:55 MDT) · 1 waiting (lib tests)

Decisions (1)
ID  Repo  State     Note  Title                        When
--  ----  --------  ----  ---------------------------  -------
#2  app   planning  #3    Which store holds the rows?  3 h ago

Landing now (2)
ID   Repo  State    Position  Title             When
---  ----  -------  --------  ----------------  ---------
#11  app   running  1         Land the cache    11:50 MDT
#12  app   pending  2         Land the index    12:00 MDT

Queues (2)
ID   Repo  State    Queue  Detail   Title             When
---  ----  -------  -----  -------  ----------------  --------
#11  app   running  lane   prepare  Land the cache    10 m ago
#12  app   pending  lane   -        Land the index    now

Delivered (1)
ID   Repo  State   PR   Commit     Title             When
---  ----  ------  ---  ---------  ----------------  ---------
#10  app   merged  #42  c0ffee000  Ship the parser   09:00 MDT
+1 closed by hand

Planned (1)
ID   Repo  State     Due             Title                  When
---  ----  --------  --------------  ---------------------  ------
#14  lib   planning  2026-09-09 MDT  Renew the certificate  in 3 d

Blocked (1)
ID   Repo  State    Waiting on                     Title             When
---  ----  -------  -----------------------------  ----------------  -------
#13  lib   blocked  Waits on the vendor's API key  Wire the webhook  3 h ago

Unscheduled (8)
ID  Repo  State     Priority  Title               When
--  ----  --------  --------  ------------------  -------
#5  lib   planning  P0        Owed work number 3  5 d ago
#8  lib   planning  P0        Owed work number 6  5 d ago
#3  lib   planning  P1        Owed work number 1  5 d ago
#6  lib   planning  P1        Owed work number 4  5 d ago
#9  lib   planning  P1        Owed work number 7  5 d ago
… 3 more

Followups (7)
ID  Repo  State     Note  Title                     When
--  ----  --------  ----  ------------------------  -------
#3  lib   planning  #5    Chase the vendor about 1  3 h ago
#4  lib   planning  #7    Chase the vendor about 2  3 h ago
#5  lib   planning  #9    Chase the vendor about 3  3 h ago
#6  lib   planning  #11   Chase the vendor about 4  3 h ago
#7  lib   planning  #13   Chase the vendor about 5  3 h ago
… 2 more

Work (1)
ID   Repo  State        Due              Priority  Title             When
---  ----  -----------  ---------------  --------  ----------------  -------
#15  app   in_progress  2026-09-01 late  -         Rotate the token  3 h ago
"""

STATUS = """\

Work across the shared database (2)
  Repo  Role
  ----  ---------
  app   this repo
  lib   companion
  2 landing · 1 delivered · 1 planned this week · 1 blocked · 8 unscheduled · 1 work

  Landing now (2)
  ID   Repo  State    Position  Title             When
  ---  ----  -------  --------  ----------------  ---------
  #11  app   running  1         Land the cache    11:50 MDT
  #12  app   pending  2         Land the index    12:00 MDT

  Delivered (1)
  ID   Repo  State   PR   Commit     Title             When
  ---  ----  ------  ---  ---------  ----------------  ---------
  #10  app   merged  #42  c0ffee000  Ship the parser   09:00 MDT
  +1 closed by hand

  Planned (1)
  ID   Repo  State     Due             Title                  When
  ---  ----  --------  --------------  ---------------------  ------
  #14  lib   planning  2026-09-09 MDT  Renew the certificate  in 3 d

  Blocked (1)
  ID   Repo  State    Waiting on                     Title             When
  ---  ----  -------  -----------------------------  ----------------  -------
  #13  lib   blocked  Waits on the vendor's API key  Wire the webhook  3 h ago

  Unscheduled (8)
  ID  Repo  State     Priority  Title               When
  --  ----  --------  --------  ------------------  -------
  #5  lib   planning  P0        Owed work number 3  5 d ago
  #8  lib   planning  P0        Owed work number 6  5 d ago
  #3  lib   planning  P1        Owed work number 1  5 d ago
  #6  lib   planning  P1        Owed work number 4  5 d ago
  #9  lib   planning  P1        Owed work number 7  5 d ago
  … 3 more

  Work (1)
  ID   Repo  State        Due              Priority  Title             When
  ---  ----  -----------  ---------------  --------  ----------------  -------
  #15  app   in_progress  2026-09-01 late  -         Rotate the token  3 h ago
"""


def load_status():
    path = ROOT / "bin" / "sd-status"
    loader = importlib.machinery.SourceFileLoader("sd_status_views", str(path))
    spec = importlib.util.spec_from_file_location("sd_status_views", str(path), loader=loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Zone(unittest.TestCase):
    def setUp(self):
        zone = unittest.mock.patch.dict(os.environ, {"TZ": "America/Denver"})
        zone.start()
        time.tzset()
        self.addCleanup(time.tzset)
        self.addCleanup(zone.stop)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        sd_db.initialise(home=self.home)
        self.connection = sd_db.connect(home=self.home)
        self.addCleanup(self.connection.close)


class SdToday(Zone):
    def test_the_layout_is_this_text_exactly(self):
        fill(self.connection)
        text = sd_views.render_sections(today.sections(self.connection, now=NOW, gate=GATE), now=MOMENT, width=100)
        self.maxDiff = None
        self.assertEqual(text, TODAY)

    def test_review_questions_are_a_line_under_decisions_not_rows(self):
        """sd:5130: a review batch's questions wait on the Review page; Decisions names their count."""
        fill(self.connection)
        review.file(self.connection, {
            "schema": 1, "queue": "labels", "batch": "B1", "title": "Label B1", "source": "example.test@1a2b3c4",
            "mode": "open", "context": {"purpose": "Fixture batch.", "rubric": ["Pick the option that fits."], "feeds": "This test."}, "questions": [{"subject": f"example.test/{number}", "text": "Which tier fits?",
                                           "options": ["cheap", "deep"]} for number in range(2)]}, who="tester")
        found = today.sections(self.connection, now=NOW, gate=GATE)
        lines = sd_views.render_sections(found, now=MOMENT, width=100).splitlines()
        at = lines.index("Decisions (1)")
        self.assertEqual(lines[at + 3:at + 5], [lines[at + 3], "+2 review questions wait on the dashboard's Review page"])
        self.assertIn("Which store holds the rows?", lines[at + 3])
        found["summary"]["review_questions"] = 1
        self.assertIn("+1 review question waits on the dashboard's Review page",
                      sd_views.render_sections(found, now=MOMENT, width=100).splitlines())

    def test_a_ready_grill_is_a_line_under_decisions_not_rows(self):
        """pack sd:6022: the KISS audit's seed questions wait until the operator says 'grill' in a fresh session."""
        fill(self.connection)
        grilled = sd_db.create_item(self.connection, kind="task", title="Weekly grill", source="task-ref",
                                    external_id="grill:2026-09-07")
        for number in range(2):
            add_note(self.connection, grilled, "question", f"Keep mechanism {number}?\nOption: Keep (Recommended)\nOption: Drop")
        found = today.sections(self.connection, now=NOW, gate=GATE)
        lines = sd_views.render_sections(found, now=MOMENT, width=100).splitlines()
        at = lines.index("Decisions (1)")
        self.assertEqual(lines[at + 4], f"Grill ready: 2 questions on sd:{grilled}; say 'grill' in a fresh session")
        found["summary"]["grill"] = 0
        self.assertFalse([line for line in sd_views.render_sections(found, now=MOMENT, width=100).splitlines()
                          if line.startswith("Grill ready")])

    def test_the_skill_that_answers_grill_reads_the_line_today_prints(self):
        """pack sd:6022: saying 'grill' loads sd-next, which finds the grill by the line `sd today` prints."""
        skill = (ROOT / "skills" / "sd-next" / "SKILL.md").read_text(encoding="utf-8")
        description = next(line for line in skill.splitlines() if line.startswith("description:"))
        self.assertIn('says "grill"', description)
        self.assertIn("grep '^Grill ready'", skill)
        self.assertIn("Grill ready: N questions on sd:X; say 'grill' in a fresh session", skill)
        self.assertIn("the ref `grill:<date>`", skill)
        self.assertTrue(today.GRILL.endswith("IS 'grill:')"))

    def test_an_empty_store_prints_every_heading(self):
        text = sd_views.render_sections(today.sections(self.connection, now=NOW), now=MOMENT, width=100)
        self.assertEqual(text.splitlines()[2::2], [f"{heading} (0)" for heading in sd_views.HEADINGS.values()])

    def test_two_runs_of_the_command_print_the_same_bytes(self):
        """The real clock runs here; every time printed is a stored one, so only a run across midnight moves.

        The machine's load is printed live, so a shim pins it: two runs at load 95.0 and 94.3 differed (sd:5782).
        """
        fill(self.connection)
        shim = self.home / "shim"
        shim.mkdir()
        (shim / "sitecustomize.py").write_text("import os\nos.getloadavg = lambda: (1.0, 2.0, 3.0)\n",
                                               encoding="utf-8")
        runs = [subprocess.run([sys.executable, str(ROOT / "bin" / "sd"), "today"], capture_output=True, text=True,
                               env=clean_environment(HOME=str(self.home), COLUMNS="100",
                                                     SD_GATE_SLOTS_DIR=str(self.home), PYTHONPATH=str(shim)),
                               check=True).stdout for _ in range(2)]
        self.assertEqual(runs[0], runs[1])
        self.assertIn(" · load 1.0/2.0/3.0, limit ", runs[0])
        self.assertEqual([line for line in runs[0].splitlines() if line.endswith(")") and "(" in line][:4],
                         ["Attention (1)", "Operations, this machine (0)", "Decisions (1)", "Landing now (2)"])


class Table(unittest.TestCase):
    def test_a_named_column_takes_the_width_the_others_leave(self):
        """sd:5357: a Detail or Value column flexes as Title does; every other cell stops at `CELL_MAX`."""
        heads, rows = ["ID", "Note", "Detail"], [["strict", "x" * 40, "a finding sentence " * 6]]
        sizes = sd_views.widths(heads, rows, 80, flex="Detail")
        self.assertEqual([6, sd_views.CELL_MAX, 80 - 6 - sd_views.CELL_MAX - 4], sizes)
        lines = sd_views.table(heads, rows, 80, flex="Detail")
        self.assertEqual(80, len(lines[2]))
        self.assertTrue(lines[2].endswith("…"))
        self.assertEqual("x" * (sd_views.CELL_MAX - 1) + "…", lines[2].split()[1])


class SdStatus(Zone):
    """`sd-status` prints the same tables for its repositories, indented under its one heading."""

    def test_the_views_block_is_this_text_exactly(self):
        fill(self.connection)
        section = {"available": True, "reason": "", "repos": [APP, LIB],
                   "sections": today.sections(self.connection, repos=[APP, LIB], now=NOW)}
        stream = io.StringIO()
        load_status()._render_views(section, stream.write, now=MOMENT, width=100)
        self.maxDiff = None
        self.assertEqual(stream.getvalue(), STATUS)

    def test_two_renders_print_the_same_bytes(self):
        fill(self.connection)
        status = load_status()
        runs = []
        for _ in range(2):
            section = {"available": True, "reason": "", "repos": [APP],
                       "sections": today.sections(self.connection, repos=[APP], now=NOW)}
            stream = io.StringIO()
            status._render_views(section, stream.write, now=MOMENT, width=100)
            runs.append(stream.getvalue())
        self.assertEqual(runs[0], runs[1])


if __name__ == "__main__":
    unittest.main()
