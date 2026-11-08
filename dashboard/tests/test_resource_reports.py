"""Legacy inspection cannot execute repository configuration."""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sd_dashboard import reports_screen

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixture_collectors", HERE / "collectors.py")
collectors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collectors)

#: The view budget a stand-in tile runs under: a loaded gate starves a five-second one (sd:6777, sd:7144).
ROOM = 60.0


class Patches:
    """Several patches as one reusable context, so a test can enter `tile` once per subtest."""

    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        for item in self.patches:
            self.stack.enter_context(item)

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


class Resources(unittest.TestCase):
    def test_a_stand_in_tile_outwaits_the_production_budget(self):
        """sd:7144: a five-second view budget refused a one-line tile on a loaded gate."""
        tile, _ = self.tile("import sys\nsys.stdout.write('{\"sections\": []}')\n")
        production = dict(reports_screen.VIEW_SECONDS)
        with tile:
            self.assertTrue(all(reports_screen.VIEW_SECONDS[area] > production[area] for area in production))
        self.assertEqual(reports_screen.VIEW_SECONDS, production)

    def test_research_config_is_data_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "research.conf.py"
            marker = root / "executed"
            config.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\nA=dict(src='index.md', title='Title')\nDOCS=[dict(**A, out='read' + 'ing')]\n")
            self.assertEqual(collectors.read_research_config(config)["DOCS"], [{"src": "index.md", "title": "Title", "out": "reading"}])
            self.assertFalse(marker.exists())
            config.write_text(f"DOCS=__import__('pathlib').Path({str(marker)!r}).write_text('bad')\n")
            with self.assertRaises(ValueError):
                collectors.read_research_config(config)
            self.assertFalse(marker.exists())

    def test_research_document_fields_are_validated_before_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "research.conf.py"
            for document in (None, "index.md", {"src": "index.md"}, {"out": "reading"},
                             {"src": 7, "out": "reading"}, {"src": "index.md", "out": []},
                             {"src": " ", "out": "reading"}, {"src": "index.md", "out": ""}):
                with self.subTest(document=document):
                    config.write_text(f"DOCS=[{document!r}]\n")
                    with self.assertRaisesRegex(ValueError, "research document 1"):
                        collectors.read_research_config(config)

    def test_computed_research_config_warns_without_hiding_valid_neighbor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            computed = root / "computed"
            literal = root / "literal"
            computed.mkdir()
            literal.mkdir()
            marker = root / "executed"
            (computed / "research.conf.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
                "A=dict(src='index.md', title='Computed')\n"
                "for doc, out in ((A, 'reading'),):\n    doc['out']=out\nDOCS=[A]\n")
            (literal / "research.conf.py").write_text(
                "DOCS=[dict(src='index.md', out='reading', title='Literal document')]\n")
            (literal / "index.md").write_text("A valid document remains observable.\n")
            with patch.object(collectors, "REPO_ROOT", root), patch.object(collectors, "git_facts", return_value=None):
                rows = {row["name"]: row for row in collectors.collect_research()}
            self.assertEqual(set(rows), {"computed", "literal"})
            self.assertIn("run sd-research-kit render once to write docs/dashboard/research.json", rows["computed"]["error"])
            self.assertEqual(rows["computed"]["docs"], [])
            self.assertEqual(rows["literal"]["error"], "")
            self.assertEqual(rows["literal"]["docs"][0]["title"], "Literal document")
            self.assertEqual(rows["literal"]["docs"][0]["words"], 5)
            with patch.dict(os.environ, {"REPO_ROOT": str(root)}):
                html = json.dumps(reports_screen.collect("research"))
            self.assertIn("Literal document", html)
            self.assertIn("computed could not be fully observed", html)
            self.assertIn("run sd-research-kit render once to write docs/dashboard/research.json", html)
            self.assertNotIn("does not run", html)
            self.assertFalse(marker.exists())

    def tile(self, source, *, seconds=ROOM):
        """A stand-in for `sd_tile.py`: a real child, read the way the real one is.

        Its view budget is `seconds`, not the five of production: a one-line script ran past five on a loaded gate
        and the view refused it (sd:7144). A test of the budget itself passes `seconds=None`.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        script = Path(directory.name) / "tile.py"
        script.write_text(source)
        patches = [patch.object(reports_screen, "TILE", script)]
        if seconds is not None:
            patches.append(patch.dict(reports_screen.VIEW_SECONDS, dict.fromkeys(reports_screen.VIEW_SECONDS, seconds)))
        return Patches(patches), Path(directory.name)

    def test_resource_observations_are_kept_as_data_in_every_view(self):
        payload = {"sections": [{"heading": "Healthy content"}], "rows": [
            {"what": "<script>repo alert</script>", "detail": '<img src=x onerror="bad()">'}]}
        tile, _ = self.tile("import os, sys\nsys.stdout.write(os.environ['TILE_PAYLOAD'])\n")
        with tile, patch.dict(os.environ, {"TILE_PAYLOAD": json.dumps(payload)}):
            for area, _ in reports_screen.VIEWS:
                with self.subTest(area=area):
                    # Data, not markup: the page's markup.tile escapes it (test_v2_management, _notes, _tasks).
                    self.assertEqual(reports_screen.collect(area), {"title": dict(reports_screen.VIEWS)[area],
                                     "sections": payload["sections"], "rows": payload["rows"]})
            payload["rows"] = []
            with patch.dict(os.environ, {"TILE_PAYLOAD": json.dumps(payload)}):
                self.assertEqual(reports_screen.collect("toolbox")["rows"], [])

    def test_a_tile_sends_sections_of_text_and_markup_is_refused(self):
        """pack sd:7527: a tile prints data that /ui/markup.js tile draws escaped; an html key, or any other shape, is refused."""
        tile, _ = self.tile("import os, sys\nsys.stdout.write(os.environ['TILE_PAYLOAD'])\n")
        good = [{"figures": [["jobs", "2"]]}, {"heading": "Cron jobs"}, {"listing": ["a line"]},
                {"table": {"columns": [["Job", "text"], ["Exit", "num"]], "search": "filter jobs",
                           "rows": [[{"text": "sync", "title": "t", "href": "https://example.test/"}, {"text": "7", "num": True}]]}}]
        with tile, patch.dict(os.environ, {"TILE_PAYLOAD": json.dumps({"title": "Toolbox", "sections": good, "rows": []})}):
            self.assertEqual(reports_screen.collect("toolbox"), {"title": "Toolbox", "sections": good, "rows": []})
        with tile, patch.dict(os.environ, {"TILE_PAYLOAD": json.dumps({"sections": [], "rows": [], "paths": ["/r/a"]})}):
            self.assertEqual(reports_screen.collect("research")["paths"], ["/r/a"])
        with tile, patch.dict(os.environ, {"TILE_PAYLOAD": json.dumps({"sections": [], "rows": [], "paths": [3]})}):
            with self.assertRaisesRegex(ValueError, "incomplete output"):
                reports_screen.collect("research")
        table = good[3]["table"]
        for sections in (None, "<p>x</p>", [{"html": "<p>x</p>"}], [{"heading": 3}], [{"heading": "a", "listing": []}],
                         [{"figures": [["jobs"]]}], [{"listing": [None]}],
                         [{"table": dict(table, rows=[[{"text": "sync"}]])}],
                         [{"table": dict(table, rows=[[{"text": "x", "href": "javascript:bad()"}, {"text": "7"}]])}],
                         [{"table": dict(table, rows=[[{"text": "x", "onclick": "bad()"}, {"text": "7"}]])}]):
            with self.subTest(sections=sections), tile, patch.dict(os.environ, {"TILE_PAYLOAD": json.dumps({"html": "<p>x</p>", "sections": sections})}):
                with self.assertRaisesRegex(ValueError, "incomplete output"):
                    reports_screen.collect("toolbox")

    def test_every_tab_prints_sections_the_view_accepts(self):
        """pack sd:7527: each sd_tile.py builder's helpers make the sections `collect` checks."""
        spec = importlib.util.spec_from_file_location("sections_sd_tile", HERE / "sd_tile.py")
        sd_tile = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sd_tile)
        sections = [sd_tile.figures([("jobs", 2), ("drift", None)]), sd_tile.heading("Cron jobs"), sd_tile.listing(["x"]),
                    sd_tile.table([("Job", "text"), ("Exit", "num")], [[{"text": "sync", "title": "t"}, sd_tile.num(None)]])]
        self.assertTrue(reports_screen._sections(sections), sections)
        self.assertEqual(sections[3]["table"]["rows"], [[{"text": "sync", "title": "t"}, {"text": "\u2014", "num": True}]])
        self.assertEqual((sd_tile.table([("Job", "text")], []), sd_tile.listing([]), sd_tile.figures([])), (None, None, None))

    def test_resource_observations_refuse_incomplete_rows(self):
        tile, _ = self.tile("import os, sys\nsys.stdout.write(os.environ['TILE_PAYLOAD'])\n")
        for rows in (None, {}, ["warning"], [{"what": "warning"}], [{"what": "warning", "detail": 3}]):
            with self.subTest(rows=rows), tile, patch.dict(os.environ, {"TILE_PAYLOAD": json.dumps({"sections": [], "rows": rows})}):
                with self.assertRaisesRegex(ValueError, "incomplete observations"):
                    reports_screen.collect("research")

    def test_a_view_passes_its_start_and_the_tile_counts_its_deadline_from_it(self):
        # sd:2501: the tile counted from its own main(), so a slow interpreter start spent the margin and the view's kill won.
        tile, directory = self.tile("import json, sys\nopen(sys.argv[0] + '.argv', 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                                    "sys.stdout.write(json.dumps({'sections': [], 'rows': []}))\n")
        with tile:
            before = time.clock_gettime(time.CLOCK_MONOTONIC)
            reports_screen.collect("toolbox")
            after = time.clock_gettime(time.CLOCK_MONOTONIC)
        calls = [json.loads(line) for line in (directory / "tile.py.argv").read_text().splitlines()]
        self.assertEqual([call[0] for call in calls], ["toolbox"])
        for call in calls:
            self.assertEqual(len(call), 2, call)
            self.assertTrue(before <= float(call[1]) <= after, call)
        spec = importlib.util.spec_from_file_location("since_sd_tile", HERE / "sd_tile.py")
        sd_tile = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sd_tile)
        deadlines = []

        class Collectors:
            @staticmethod
            @contextlib.contextmanager
            def set_deadline(seconds, *, started):
                deadlines.append(started)
                yield

        since = time.clock_gettime(time.CLOCK_MONOTONIC) - 2.0
        with patch.object(sd_tile, "load_collectors", lambda: Collectors), \
                patch.dict(sd_tile.TABS, {"toolbox": lambda collectors: {"sections": []}}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sd_tile.main(["toolbox", f"{since:.6f}"]), 0)
        # Two seconds spent starting come off the deadline, never on top of it.
        self.assertLessEqual(deadlines[0], time.monotonic() - 2.0)

    def test_failed_tile_is_refused_with_its_reason(self):
        tile, _ = self.tile("import sys\nsys.stdout.write('{}')\nsys.exit('research: collector broke')\n")
        with tile, self.assertRaisesRegex(ValueError, "research: collector broke"):
            reports_screen.collect("research")

    def assertGone(self, pid_file):
        pids = [int(line) for line in pid_file.read_text().split()]
        deadline = time.monotonic() + 2
        for pid in pids:
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    self.fail(f"process {pid} outlived a refused resource view")
                time.sleep(0.01)

    # A child and a grandchild that each record their pid, so the refusal can
    # be shown to leave neither behind.
    SPAWN = ("import os, subprocess, sys\n"
             "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
             "open(sys.argv[0] + '.pids', 'w').write(f'{os.getpid()}\\n{grandchild.pid}\\n')\n")

    def test_overlong_tile_is_cut_at_the_limit_without_waiting_for_exit(self):
        tile, directory = self.tile(self.SPAWN +
            "while True:\n    sys.stdout.write('x' * 8192); sys.stdout.flush()\n")
        # Cut at the byte ceiling while reading. The tile never exits, so a
        # check made after exit would meet the time ceiling first, and the
        # refusal would name time, not bytes: no elapsed bound to lose under
        # load (sd:2667).
        with tile, self.assertRaisesRegex(ValueError, "stopped at its budget: .*more than its budget of 64 KB"):
            reports_screen.collect("toolbox")
        self.assertGone(directory / "tile.py.pids")

    def test_slow_tile_is_killed_within_its_budget(self):
        # The tile leaves a marker only if its minute runs out, so a view that
        # returns without it killed the tile rather than waited (sd:2667).
        tile, directory = self.tile(self.SPAWN + "import time\ntime.sleep(60)\n"
                                    "open(sys.argv[0] + '.finished', 'w').close()\n", seconds=None)
        with tile, self.assertRaisesRegex(ValueError, "stopped at its budget: .*ran past its budget of 5 seconds") as refused:
            reports_screen.collect("vault")
        self.assertFalse((directory / "tile.py.finished").exists(), "the view waited for the tile instead of killing it")
        self.assertGone(directory / "tile.py.pids")
        # The page's route answers the refusal and its reason rather than an empty view.
        with patch.object(reports_screen, "collect", Mock(side_effect=refused.exception)):
            status, answer = reports_screen.view("vault")
        self.assertEqual(status, 503)
        self.assertIn("Vault could not be observed: resource inspection was stopped at its budget", answer["error"])

    def test_every_view_declares_a_ceiling_inside_the_tile_contract(self):
        self.assertEqual(set(reports_screen.VIEW_SECONDS), {area for area, _ in reports_screen.VIEWS})
        self.assertTrue(all(0 < seconds <= 5 for seconds in reports_screen.VIEW_SECONDS.values()))


class CronJobFiles(unittest.TestCase):
    """Toolbox reads job files where cron-jobs.sh does, not from the checkout."""

    def test_the_config_jobs_dir_is_read_and_an_extra_dir_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = root / "config/cron-jobs/jobs"
            jobs.mkdir(parents=True)
            extra = root / "extra"
            extra.mkdir()
            (jobs / "one.job").write_text("A\n")
            (jobs / "two.job").write_text("B\n")
            (extra / "two.job").write_text("C\n")
            env = {"SYSTEM_TOOLS_CONFIG": str(root / "config"), "CRON_JOBS_EXTRA_DIRS": str(extra),
                   "CRON_JOBS_HOST": "mini"}
            with patch.dict(os.environ, env):
                found = collectors.cron_job_files()
            self.assertEqual(found, [jobs / "one.job", extra / "two.job"])

    def test_this_hosts_folder_overrides_and_other_hosts_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = root / "config/cron-jobs/jobs"
            (jobs / "mini").mkdir(parents=True)
            (jobs / "studio").mkdir()
            (jobs / "one.job").write_text("A\n")
            (jobs / "mini/one.job").write_text("B\n")
            (jobs / "mini/mine.job").write_text("C\n")
            (jobs / "studio/theirs.job").write_text("D\n")
            env = {"SYSTEM_TOOLS_CONFIG": str(root / "config"), "CRON_JOBS_HOST": "Mini"}
            with patch.dict(os.environ, env):
                os.environ.pop("CRON_JOBS_EXTRA_DIRS", None)
                found = collectors.cron_job_files()
            self.assertEqual(found, [jobs / "mini/mine.job", jobs / "mini/one.job"])

    def test_no_host_folder_reads_the_shared_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = root / "config/cron-jobs/jobs"
            jobs.mkdir(parents=True)
            (jobs / "one.job").write_text("A\n")
            env = {"SYSTEM_TOOLS_CONFIG": str(root / "config"), "CRON_JOBS_HOST": "mini"}
            with patch.dict(os.environ, env):
                os.environ.pop("CRON_JOBS_EXTRA_DIRS", None)
                found = collectors.cron_job_files()
            self.assertEqual(found, [jobs / "one.job"])
