"""The lane's prepare gate is the one full run (sd:5800, sd:8843).

A pre-enqueue gate pass is reused only while main has not moved, so a
builder or lead gate before review doubles gate load. No skill or
workflow page may prescribe the gate-first round again.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "WORKFLOW.md",
    "skills/sd-review/SKILL.md",
    "skills/sd-ship/SKILL.md",
    "skills/sd-check/SKILL.md",
)
FORBIDDEN = (
    "--gate-check main",
    "gate check --base",
    "Before a head goes to the lane, run `sd gate check`",
    "`sd-review` runs it first",
)


class GateFirstProseTest(unittest.TestCase):
    def test_no_gate_first_prose(self) -> None:
        for name in FILES:
            text = re.sub(r"\s+", " ", (ROOT / name).read_text(encoding="utf-8"))
            found = [phrase for phrase in FORBIDDEN if phrase in text]
            with self.subTest(name=name):
                self.assertEqual(found, [], f"{name} prescribes a gate before review")


if __name__ == "__main__":
    unittest.main()
