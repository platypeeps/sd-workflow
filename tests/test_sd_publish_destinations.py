"""sd-publish names each destination once in its argument and gives it a contract (sd:8024)."""

import re
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "sd-publish" / "SKILL.md"


class Destinations(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")

    def named(self) -> list[str]:
        return re.search(r"`destination=([a-z|-]+)`", self.text).group(1).split("|")

    def test_google_doc_and_google_sheet_are_destinations(self):
        self.assertIn("google-doc", self.named())
        self.assertIn("google-sheet", self.named())

    def test_every_destination_has_a_contract(self):
        contracts = {
            re.sub(r"\s+", "-", label.strip().lower())
            for label in re.findall(r"^   - \*\*([^*]+)\*\* —", self.text, re.M)
        }
        self.assertEqual(sorted(set(self.named()) - contracts), [])


if __name__ == "__main__":
    unittest.main()
