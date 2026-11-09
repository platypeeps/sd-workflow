"""sd-presentation and sd-publish are retired; nothing names them (sd:8286)."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETIRED = re.compile(r"\bsd-(presentation|publish)\b(?![-_\w])")
#: History records the skills as they were; a live page must not.
SKIP = ("docs/work/", "tests/test_retired_skills.py")


class RetiredSkills(unittest.TestCase):
    def test_skill_directories_are_gone(self):
        for name in ("sd-presentation", "sd-publish"):
            self.assertFalse((ROOT / "skills" / name).exists(), name)

    def test_no_live_file_names_them(self):
        hits = []
        for top in ("skills", "contrib", "tests", "bin", "lib", "docs"):
            for path in (ROOT / top).rglob("*"):
                rel = path.relative_to(ROOT).as_posix()
                if not path.is_file() or rel.startswith(SKIP):
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                hits += [rel for line in text.splitlines() if RETIRED.search(line)]
        for top in ROOT.glob("*.md"):
            text = top.read_text(encoding="utf-8")
            hits += [top.name for line in text.splitlines() if RETIRED.search(line)]
        self.assertEqual([], sorted(set(hits)))


if __name__ == "__main__":
    unittest.main()
