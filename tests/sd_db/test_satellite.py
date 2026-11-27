"""Steps 8 and 9 of the second-machine plan: the hub's serve start, and the satellite's check.

`docs/work/2026-09-22-run-the-framework-from-a-second-machine/implement.md`.
Step 8: `sd-db.sh serve` under an empty home exits non-zero naming the
resolved missing path and creates nothing (gap (h)). Step 9: the registry
travels over the wire (seam 7), and `sd_db.satellite`, which the
`satellite` stage of `local-machine-setup` runs, writes `hub.json`, refuses
beside a local database, names a build mismatch with both values and
installs the hub's `providers.yaml`. Every hub here is a loopback
`sd-db.sh serve` in a temporary folder.
"""

from __future__ import annotations

import io
import json
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from sd_db import initialise, registry, satellite
from sd_db.database import Patient
from sd_db.errors import RegistryError

from .test_wire import SD_DB, Served, environment

#: The words `machine-setup.sh status` counts as drift.
DRIFT = r"DIFFERS|MISSING|STALE|ABSENT|UNLOADED|EXTRA"

REGISTRY = """\
bills:
  fixture: { cost: subscription }
providers:
  one: { start: "one exec", vendor: fixture, bill: fixture, roles: [author], env: [] }
  two: { start: "two exec", vendor: fixture, bill: fixture, roles: [reviewer], env: [] }
roles:
  author:   [one]
  reviewer: [two]
"""


def files_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TheServeStart(unittest.TestCase):
    """Step 8, gap (h): the hub's agent under the wrong home fails closed, by name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_an_empty_home_exits_non_zero_naming_the_resolved_path_and_creates_nothing(self):
        home = self.root / "home"
        home.mkdir()
        done = subprocess.run([sys.executable, str(SD_DB), "serve", "--loopback", "--port", "0"],
                              capture_output=True, text=True, env=environment(self.root), timeout=60)
        self.assertNotEqual(done.returncode, 0)
        missing = home.resolve() / ".local/share/sd/sd.db"
        self.assertIn(f"no database at {missing}", done.stderr)
        self.assertEqual(files_under(home), [])

    def test_the_start_line_names_the_resolved_database(self):
        real = self.root / "real"
        real.mkdir()
        initialise(real / "sd.db")
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        served = Served(self.root, "link/sd.db")
        log = served.stop()
        self.assertIn(f"serving {real.resolve() / 'sd.db'} on 127.0.0.1:", log)


class TheRegistryOverTheWire(unittest.TestCase):
    """Seam 7: a session reads the hub's providers.yaml, beside the served database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.served = Served(self.root)
        self.addCleanup(self.served.stop)

    def test_a_session_reads_the_registry_bytes(self):
        (self.root / "providers.yaml").write_text(REGISTRY)
        wire = self.served.connect(None)
        self.addCleanup(wire.close)
        self.assertEqual(wire.registry_bytes(), REGISTRY.encode())

    def test_a_read_on_a_session_takes_the_hubs_file_not_a_local_path(self):
        """sd:10400: a satellite's session answers the hub's database path, which names no file on the
        satellite, so a builder's cost row lost its provider and bill. The read goes over the session."""
        (self.root / "providers.yaml").write_text(REGISTRY)
        wire = self.served.connect(None)
        self.addCleanup(wire.close)
        elsewhere = self.root / "satellite" / "providers.yaml"  # where the hub's path points on a satellite
        with mock.patch.object(registry, "beside", return_value=elsewhere):
            read = registry.read(connection=cast(Any, wire))
            self.assertEqual(sorted(read.providers), ["one", "two"])
            self.assertEqual(read.providers["one"].bill, "fixture")
            self.assertEqual(sorted(registry.read(connection=cast(Any, Patient(wire))).providers), ["one", "two"])

    def test_a_hub_without_a_registry_names_the_path(self):
        wire = self.served.connect(None)
        self.addCleanup(wire.close)
        with self.assertRaisesRegex(RegistryError, "no provider registry at .*providers.yaml on the hub"):
            wire.registry_bytes()
        # The session survives the refusal.
        self.assertEqual(wire.execute("SELECT 1").fetchone()[0], 1)


class TheSatelliteCheck(unittest.TestCase):
    """Step 9 and criterion 7, through `sd_db.satellite.run` in a temporary home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hub_root = self.root / "hub"
        self.hub_root.mkdir()
        (self.hub_root / "providers.yaml").write_text(REGISTRY)
        self.home = self.root / "satellite"
        self.home.mkdir()
        self.config = self.home / ".config/sd/hub.json"
        self.registry = self.home / ".local/share/sd/providers.yaml"

    def serve(self, **options) -> Served:
        served = Served(self.hub_root, **options)
        self.addCleanup(served.stop)
        return served

    def check(self, port: int, *, apply: bool = False) -> list[str]:
        out = io.StringIO()
        self.assertEqual(satellite.run("127.0.0.1", port, apply=apply, home=self.home, out=out), 0)
        return out.getvalue().splitlines()

    def name_hub(self, served: Served) -> None:
        """hub.json as the stage writes it, plus the loopback token file."""
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({"hub": "127.0.0.1", "port": served.port,
                                           "token_file": str(served.token_file)}))

    def test_a_missing_hub_json_is_one_missing_and_apply_writes_it(self):
        port = closed_port()
        lines = self.check(port)
        self.assertEqual([line for line in lines if "MISSING" in line],
                         [f"  MISSING {self.config} — this machine names no sd hub"])
        self.assertIn(f"  [dry-run] write {self.config}", lines)
        self.assertEqual([line for line in lines if re.search(DRIFT, line)],
                         [f"  MISSING {self.config} — this machine names no sd hub"])
        self.assertFalse(self.config.exists(), "a dry run wrote hub.json")
        self.check(port, apply=True)
        self.assertEqual(json.loads(self.config.read_text()), {"hub": "127.0.0.1", "port": port})
        again = self.check(port)
        self.assertIn(f"  ok      {self.config} names 127.0.0.1:{port}", again)

    def test_a_hub_json_naming_another_hub_differs(self):
        port = closed_port()
        self.config.parent.mkdir(parents=True)
        self.config.write_text('{"hub": "other.example.test", "port": 8769}')
        lines = self.check(port)
        self.assertIn(f"  DIFFERS {self.config} names other.example.test:8769, not 127.0.0.1:{port}", lines)

    def test_an_unreachable_hub_is_skipped_not_drift(self):
        port = closed_port()
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"hub": "127.0.0.1", "port": port}))
        lines = self.check(port)
        self.assertFalse([line for line in lines if re.search(DRIFT, line)], lines)
        skipped = [line for line in lines if line.startswith("  SKIP")]
        self.assertEqual(len(skipped), 1, lines)
        self.assertIn(f"sd hub 127.0.0.1:{port} did not answer", skipped[0])
        self.assertFalse(self.registry.exists())

    def test_a_local_database_is_extra_and_nothing_is_written(self):
        initialise(self.home / ".local/share/sd/sd.db")
        lines = self.check(closed_port(), apply=True)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith(f"  EXTRA   database {self.home / '.local/share/sd/sd.db'}"), lines)
        self.assertFalse(self.config.exists())

    def test_the_hub_registry_is_installed_and_then_matches(self):
        served = self.serve()
        self.name_hub(served)
        lines = self.check(served.port)
        self.assertIn(f"  ok      {self.config} names 127.0.0.1:{served.port}", lines)
        self.assertTrue(any(line.startswith("  ok      sd_db ") and "matches the hub's" in line for line in lines), lines)
        self.assertIn(f"  MISSING {self.registry} — the hub's providers.yaml is not here", lines)
        self.assertFalse(self.registry.exists(), "a dry run installed the registry")
        self.check(served.port, apply=True)
        self.assertEqual(self.registry.read_text(), REGISTRY)
        # The token file and hub.json are untouched by a matching run.
        self.assertIn("token_file", json.loads(self.config.read_text()))
        self.assertIn(f"  ok      {self.registry} matches the hub's", self.check(served.port))

    def test_a_changed_registry_differs_and_apply_replaces_it(self):
        served = self.serve()
        self.name_hub(served)
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text(REGISTRY.replace("one exec", "old exec"))
        lines = self.check(served.port)
        self.assertIn(f"  DIFFERS {self.registry} differs from the hub's providers.yaml", lines)
        self.check(served.port, apply=True)
        self.assertEqual(self.registry.read_text(), REGISTRY)

    def test_a_package_mismatch_differs_with_both_versions(self):
        served = self.serve_version("0.0.9")
        self.name_hub(served)
        from sd_db import __version__

        lines = self.check(served.port, apply=True)
        differs = [line for line in lines if "DIFFERS" in line]
        self.assertEqual(differs, [f"  DIFFERS sd_db package version: satellite {__version__}, hub 0.0.9 — "
                                   "run `repo-sync.sh refresh` on the hub, then rerun"])
        self.assertFalse(self.registry.exists())

    def test_an_older_satellite_is_told_to_follow_and_installs_nothing(self):
        """sd:3280: self-install is gone; `--apply` names follow, plans and writes nothing."""
        served = self.serve_version("999.0")
        self.name_hub(served)
        from sd_db import __version__

        lines = self.check(served.port, apply=True)
        self.assertEqual(lines[-1], f"  DIFFERS sd_db package version: satellite {__version__}, hub 999.0 — "
                                    "run `repo-sync.sh follow` here, then rerun")
        self.assertFalse(any("install the hub's sd_db" in line for line in lines), lines)
        self.assertFalse(self.registry.exists())

    def serve_version(self, version: str) -> Served:
        """A hub whose package says `version`: another tag of the same build."""
        program = (sys.executable, "-c", f"import sys, sd_db; sd_db.__version__ = {version!r}; "
                   "from sd_db import serve; sys.exit(serve.main(sys.argv[1:]))")
        served = Served(self.hub_root, program=program)
        self.addCleanup(served.stop)
        return served


if __name__ == "__main__":
    unittest.main()
