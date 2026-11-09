"""sd-presentation and sd-publish are retired; nothing names them (sd:8286)."""

import subprocess
import unittest
from pathlib import Path

from tests.clean_env import clean_environment

ROOT = Path(__file__).resolve().parents[1]
#: Either name, not followed by more of a name (`sd-publish-drive` is a live skill).
RETIRED = r"(^|[^-_[:alnum:]])sd-(presentation|publish)([^-_[:alnum:]]|$)"


class RetiredSkills(unittest.TestCase):
    def test_skill_directories_are_gone(self):
        for name in ("sd-presentation", "sd-publish"):
            self.assertFalse((ROOT / "skills" / name).exists(), name)

    def test_no_live_file_names_them(self):
        # Tracked files only; history in docs/work records the skills as they were.
        found = subprocess.run(
            ["git", "-C", str(ROOT), "grep", "-n", "-I", "-E", RETIRED, "--", ".",
             ":(exclude)docs/work", ":(exclude)tests/test_retired_skills.py"],
            capture_output=True, text=True, check=False, env=clean_environment())
        self.assertIn(found.returncode, (0, 1), found.stderr)
        self.assertEqual("", found.stdout)


if __name__ == "__main__":
    unittest.main()
