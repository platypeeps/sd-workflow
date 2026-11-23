"""Criterion 25: a trial is a row with an expiry, and use is what decides.

`sd skill try` writes the row and prints the date; the next install run after
expiry with no `skill_use` rows removes the skill and says so. Both against a
temporary database, which is what the criterion asks for.

This file is also where hand-off 7 lands: `sd_db.testing` has to be importable
from *this* repository's test suite, not only from the library's own, and the
assertion belongs beside the first tests that need the library.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.clean_env import clean_environment

REPO_ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    """Import a `bin/` module by path -- `bin/` is not a package."""
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "bin" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sd_install = load("sd_install")
sd_skill = load("sd_skill")

import sd_db  # noqa: E402 - after the path juggling above
import sd_db.testing  # noqa: E402,F401 - hand-off 7's assertion is the import


class TheHarnessIsImportableHere(unittest.TestCase):
    """Criterion 23's pack half, asserted from the pack.

    The library's own suite asserts the same import. Both are needed: the
    library's proves the module exists, this one proves the pack's tests can
    reach it from the checkout's `lib/` (sd:3280).
    """

    def test_the_fixture_harness_is_reachable(self):
        self.assertTrue(hasattr(sd_db, "testing") or sd_db.testing is not None)


class TrialCase(unittest.TestCase):
    """A scratch home with a real database in it."""

    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self._scratch.cleanup)
        self.home = Path(self._scratch.name).resolve()
        sd_db.initialise(home=self.home)
        self.connection = sd_db.connect(sd_db.default_path(self.home))
        self.addCleanup(self.connection.close)


class TheTrialVerb(TrialCase):
    def run_try(self, name: str) -> tuple[int, str]:
        """`sd skill try <name>` through the CLI, against the scratch home."""
        done = subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "sd"), "skill", "try", name],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(self.home)},
            cwd=tempfile.gettempdir(),
        )
        return done.returncode, done.stdout + done.stderr

    def trial_rows(self) -> list:
        return sd_db.trials(self.connection)

    def test_a_trial_writes_one_row_and_prints_its_expiry(self):
        contrib = sorted(
            entry.name
            for entry in (REPO_ROOT / sd_skill.CONTRIB_DIR).iterdir()
            if entry.is_dir()
        )
        self.assertTrue(contrib, "contrib/ is empty; requirement 10 says it holds the rest")
        rc, output = self.run_try(contrib[0])
        self.assertEqual(rc, 0, output)
        rows = self.trial_rows()
        self.assertEqual([row["skill"] for row in rows], [contrib[0]])
        # The printed date is the row's expiry, not a second number computed
        # for the message. A test that only checked "some date appeared" would
        # pass against a message that lied about the row.
        self.assertIn(rows[0]["expires"][:10], output)

    def test_the_expiry_is_thirty_days_out(self):
        contrib = sorted(
            entry.name
            for entry in (REPO_ROOT / sd_skill.CONTRIB_DIR).iterdir()
            if entry.is_dir()
        )
        self.run_try(contrib[0])
        row = self.trial_rows()[0]
        started = row["started"]
        expires = row["expires"]
        self.assertEqual(
            (
                sd_skill.datetime.datetime.fromisoformat(expires)
                - sd_skill.datetime.datetime.fromisoformat(started)
            ).days,
            sd_skill.TRIAL_DAYS,
        )

    def test_a_skill_already_on_a_path_is_refused_rather_than_trialled(self):
        rc, output = self.run_try("sd-check")
        self.assertEqual(rc, 1)
        self.assertIn("already on a path", output)
        self.assertEqual(self.trial_rows(), [])

    def test_a_name_in_neither_place_is_refused_and_says_what_there_is(self):
        rc, output = self.run_try("sd-not-a-skill")
        self.assertEqual(rc, 1)
        self.assertIn("available:", output)
        self.assertEqual(self.trial_rows(), [])


class TheListVerb(TrialCase):
    """`sd skill list --usage` prints each skill's recorded uses, so a roster decision reads the data."""

    def run_list(self, *flags: str) -> tuple[int, str]:
        done = subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "sd"), "skill", "list", *flags],
            capture_output=True,
            text=True,
            env=clean_environment(HOME=str(self.home)),
            cwd=tempfile.gettempdir(),
        )
        return done.returncode, done.stdout + done.stderr

    def line(self, output: str, name: str) -> str:
        return next(line for line in output.splitlines() if line.startswith(f"{name}  "))

    def test_usage_prints_the_count_recorded_for_each_skill(self):
        for surface in ("claude", "claude", "codex"):
            sd_db.record_skill_use(self.connection, "sd-check", surface=surface)
        rc, output = self.run_list("--usage")
        self.assertEqual(rc, 0, output)
        self.assertIn("uses=3", self.line(output, "sd-check"))
        self.assertIn("uses=0", self.line(output, "sd-ship"))

    def test_usage_prints_each_skills_last_use_for_the_weekly_audit(self):
        """pack sd:5992: the KISS audit lists unused skills; an all-time count cannot say "not lately"."""
        sd_db.record_skill_use(self.connection, "sd-check", surface="claude", timestamp="2026-08-01T09:00:00Z")
        sd_db.record_skill_use(self.connection, "sd-check", surface="codex", timestamp="2026-09-02T09:00:00Z")
        rc, output = self.run_list("--usage")
        self.assertEqual(rc, 0, output)
        self.assertIn("uses=2  last=2026-09-02", self.line(output, "sd-check"))
        self.assertIn("uses=0  last=never", self.line(output, "sd-ship"))
        rc, output = self.run_list("--json")
        skills = {skill["name"]: skill for skill in json.loads(output)["skills"]}
        self.assertEqual(skills["sd-check"]["last"][:19], "2026-09-02T09:00:00")
        self.assertIsNone(skills["sd-ship"]["last"])

    def test_the_default_listing_prints_no_count(self):
        sd_db.record_skill_use(self.connection, "sd-check", surface="claude")
        rc, output = self.run_list()
        self.assertEqual(rc, 0, output)
        self.assertNotIn("uses=", output)

    def test_the_default_listing_prints_each_skills_last_use(self):
        """pack sd:5992: the audit reads the last use with no flag; the operator wants no new flags."""
        sd_db.record_skill_use(self.connection, "sd-check", surface="claude", timestamp="2026-09-02T09:00:00Z")
        rc, output = self.run_list()
        self.assertEqual(rc, 0, output)
        self.assertIn("sd-check  path  last=2026-09-02  ", self.line(output, "sd-check"))
        self.assertIn("sd-ship  path  last=never  ", self.line(output, "sd-ship"))


class TheExpiry(TrialCase):
    """The install run that removes a trial nobody used, and says so."""

    def context(self) -> "sd_install.Context":
        return sd_install.Context(
            checkout=REPO_ROOT,
            home=self.home,
            environ={
                "XDG_STATE_HOME": str(self.home / ".local" / "state"),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
            },
        )

    def test_an_expired_trial_with_no_use_is_removed_and_named(self):
        sd_db.start_trial(self.connection, "sd-watchlist", "2001-01-01T00:00:00Z")
        out = io.StringIO()
        removed = sd_install.expire_trials(self.connection, out)
        self.assertEqual(removed, ["sd-watchlist"])
        self.assertIn("sd-watchlist", out.getvalue())
        self.assertIn("no use", out.getvalue())
        self.assertEqual(sd_db.trials(self.connection), [])

    def test_an_expired_trial_that_earned_use_is_kept(self):
        sd_db.start_trial(self.connection, "sd-watchlist", "2001-01-01T00:00:00Z")
        row = sd_db.trials(self.connection)[0]
        sd_db.record_skill_use(self.connection, "sd-watchlist", timestamp=row["started"])
        out = io.StringIO()
        self.assertEqual(sd_install.expire_trials(self.connection, out), [])
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(sd_db.trials(self.connection)), 1)

    def test_use_from_before_the_trial_does_not_save_it(self):
        """The window is the trial's own, which is the whole point of the row.

        A skill somebody used last year and then put on trial has not earned
        the trial. Counting the old rows would keep it installed on the
        strength of the usage that made somebody curious in the first place.
        """
        sd_db.record_skill_use(self.connection, "sd-watchlist", timestamp="2000-01-01T00:00:00Z")
        sd_db.start_trial(self.connection, "sd-watchlist", "2001-01-01T00:00:00Z")
        out = io.StringIO()
        self.assertEqual(sd_install.expire_trials(self.connection, out), ["sd-watchlist"])

    def test_an_unexpired_trial_is_left_alone_however_unused(self):
        sd_db.start_trial(self.connection, "sd-watchlist", "2099-01-01T00:00:00Z")
        out = io.StringIO()
        self.assertEqual(sd_install.expire_trials(self.connection, out), [])
        self.assertEqual(len(sd_db.trials(self.connection)), 1)

    def test_the_whole_run_expires_removes_and_renders_in_one_pass(self):
        """`cmd_user` end to end, against this checkout and a real database.

        The pieces above are tested one at a time. This is the pass an
        operator actually gets: an expired unused trial goes, an active one
        stays and installs from `contrib/`, and the skills the paths name are
        rendered alongside it. Nothing here is mocked -- the database is the
        real schema, the checkout is this repository, and only `$HOME` is
        scratch.
        """
        sd_db.start_trial(self.connection, "sd-watchlist", "2099-01-01T00:00:00Z")
        sd_db.start_trial(self.connection, "sd-effort-calibrate", "2001-01-01T00:00:00Z")
        self.connection.commit()

        out = io.StringIO()
        self.assertEqual(sd_install.cmd_user(self.context(), out), 0)
        report = out.getvalue()

        # No library warning: the library imported and the database was there.
        # Named by their text rather than by the `warning:` prefix, which the
        # link step also uses when the scratch home's `.local/bin` is not on
        # PATH -- and in this context it never is.
        self.assertNotIn("trials unavailable", report)
        self.assertNotIn("no database at", report)
        self.assertIn("sd-effort-calibrate", report)
        self.assertIn("no use", report)

        claude = self.home / ".claude" / "skills"
        self.assertTrue((claude / "sd-watchlist" / sd_install.SKILL_FILE).is_file())
        self.assertFalse((claude / "sd-effort-calibrate").exists())
        self.assertTrue((claude / "sd-check" / sd_install.SKILL_FILE).is_file())

        remaining = {row["skill"] for row in sd_db.trials(self.connection)}
        self.assertEqual(remaining, {"sd-watchlist"})

    def test_the_install_run_renders_an_active_trial_from_contrib(self):
        sd_db.start_trial(self.connection, "sd-watchlist", "2099-01-01T00:00:00Z")
        names = [
            surface.name
            for surface in sd_install.discover_surfaces(
                REPO_ROOT, [row["skill"] for row in sd_db.active_trials(self.connection)]
            )
        ]
        self.assertIn("sd-watchlist", names)
        self.assertNotIn("sd-watchlist", sd_install.named_skills(REPO_ROOT))


class RenderingInstallsNothing(unittest.TestCase):
    """Rendering skills must not rebuild the virtualenv it renders from.

    An early version of this work did: `cmd_user` called `provision_library`, so each
    of the hundreds of fixture installs in this suite shelled out to `pip`.
    In a parallel run that replaced `sd_db` in site-packages while another
    shard was importing it, which surfaced as `No module named
    'sd_db.testing.home'` in a file that touches neither.

    The interpreter here is a script that records being called. If a render
    ever installs again, the marker appears and this fails at the cause.
    """

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name).resolve()
        self.checkout = self.home / "checkout"
        self.marker = self.home / "pip-was-run"

        # A checkout with one skill, one path naming it, and a virtualenv
        # whose interpreter is a recorder rather than a Python.
        skill = self.checkout / "skills" / "sd-probe"
        skill.mkdir(parents=True)
        (skill / sd_install.SKILL_FILE).write_text(
            "---\nname: sd-probe\n---\n\nprobe surface\n", encoding="utf-8"
        )
        (self.checkout / "skills" / sd_install.PATHS_FILE).write_text(
            json.dumps({"paths": {
                "research": {"summary": "sources to brief", "skills": ["sd-probe"]},
                "development": {"summary": "plan to ship", "skills": []},
                "act": {"summary": "brief to send", "skills": []},
            }}),
            encoding="utf-8",
        )
        interpreter = self.checkout / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{self.marker}"\n', encoding="utf-8"
        )
        interpreter.chmod(0o755)

    def context(self) -> "sd_install.Context":
        return sd_install.Context(
            checkout=self.checkout,
            home=self.home,
            environ={
                "XDG_STATE_HOME": str(self.home / ".local" / "state"),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
            },
        )

    def test_a_render_does_not_shell_out_to_pip(self) -> None:
        out = io.StringIO()
        self.assertEqual(sd_install.cmd_user(self.context(), out), 0)
        self.assertFalse(
            self.marker.exists(),
            f"the render installed: {self.marker.read_text() if self.marker.exists() else ''}",
        )

class NoLibrary:
    """A `sys.meta_path` finder that refuses `sd_db` wherever it is installed."""

    def find_spec(self, name, path=None, target=None):
        if name == "sd_db" or name.startswith("sd_db."):
            raise ImportError("sd_db is not installed (blocked by the test)")
        return None


class TheLibraryDoor(unittest.TestCase):
    """Every way `open_library` can answer.

    A library that will not import, a database that was never created: the
    installer answers both with a line and a working install of whatever it
    could still render, so neither may raise.
    """

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name).resolve()
        self.checkout = self.home / "checkout"
        self.checkout.mkdir()

    def context(self, **overrides) -> "sd_install.Context":
        fields = {
            "checkout": self.checkout,
            "home": self.home,
            "environ": {
                "XDG_STATE_HOME": str(self.home / ".local" / "state"),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
            },
        }
        fields.update(overrides)
        return sd_install.Context(**fields)

    def test_a_library_that_will_not_import_leaves_the_render_working(self) -> None:
        """No library anywhere: the blocker answers before the checkout's `lib/`.

        `open_library` goes through `sd_lib.import_sd_db`, which prepends
        `lib/` to `sys.path`; that is put back so no later test inherits it.
        """
        blocker = NoLibrary()
        sys.meta_path.insert(0, blocker)
        self.addCleanup(sys.meta_path.remove, blocker)
        saved = dict(sys.modules)
        self.addCleanup(lambda: (sys.modules.clear(), sys.modules.update(saved)))
        saved_path = list(sys.path)
        self.addCleanup(sys.path.__setitem__, slice(None), saved_path)
        for name in [n for n in sys.modules if n == "sd_db" or n.startswith("sd_db.")]:
            del sys.modules[name]
        connection, reason = sd_install.open_library(self.context())
        self.assertIsNone(connection)
        self.assertEqual(
            reason, "sd_db is not installed here: sd_db is not installed (blocked by the test); trials unavailable"
        )

    def test_a_home_with_no_database_names_the_command_that_makes_one(self) -> None:
        connection, reason = sd_install.open_library(self.context())
        self.assertIsNone(connection)
        self.assertIn("no database at", reason)
        self.assertIn("bin/sd-db init", reason)


if __name__ == "__main__":
    unittest.main()
