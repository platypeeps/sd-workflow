"""Operator prompts live as reference files in existing skills (sd:10795).

The operator ruled on 2026-11-25 to fold each standing prompt into a skill
that already answers its kind of question, rather than add a skill or a
command. A prompt is reachable only if its file exists and its skill names
the trigger the operator types, so this checks both, exactly.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: skill -> reference file -> the triggers the operator types.
PROMPTS = {
    "sd-next": {
        "triage.md": ("triage", "park review"),
        "health.md": ("health check",),
        "open-threads.md": ("open threads", "collect followups"),
    },
    "sd-research-repo": {
        "fleet-review.md": ("review my research repos",),
    },
    "sd-ship": {
        "open-prs.md": ("check open PRs",),
    },
    "sd-status": {
        "deep.md": ("/sd-status deep",),
    },
}


def skill_text(skill: str) -> str:
    return (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")


def description(skill: str) -> str:
    return next(line for line in skill_text(skill).splitlines()
                if line.startswith("description:"))


class OperatorPromptReferences(unittest.TestCase):
    def test_each_reference_exists_and_has_a_heading(self):
        for skill, files in PROMPTS.items():
            for name in files:
                path = ROOT / "skills" / skill / "references" / name
                with self.subTest(path=str(path.relative_to(ROOT))):
                    self.assertTrue(path.is_file())
                    self.assertTrue(path.read_text(encoding="utf-8").startswith("# "))

    def test_skill_cites_each_reference_with_its_triggers_on_one_line(self):
        for skill, files in PROMPTS.items():
            lines = skill_text(skill).splitlines()
            for name, triggers in files.items():
                with self.subTest(skill=skill, reference=name):
                    cited = [line for line in lines if f"references/{name}`" in line]
                    self.assertEqual(len(cited), 1, cited)
                    for trigger in triggers:
                        self.assertRegex(cited[0], rf"['\"`]{re.escape(trigger)}['\"`]")

    def test_model_invoked_skill_descriptions_name_the_triggers(self):
        """A plain ask loads the skill only through its description."""
        for skill in ("sd-next", "sd-research-repo"):
            for triggers in PROMPTS[skill].values():
                for trigger in triggers:
                    with self.subTest(skill=skill, trigger=trigger):
                        self.assertIn(trigger, description(skill))

    def test_references_carry_no_home_path(self):
        """The repo is public: a prompt names no operator path."""
        for skill, files in PROMPTS.items():
            for name in files:
                text = (ROOT / "skills" / skill / "references" / name).read_text(encoding="utf-8")
                with self.subTest(skill=skill, reference=name):
                    self.assertNotRegex(text, r"~/|/(Users|Volumes)/")

    def test_open_prs_routes_fixes_through_the_build_verb(self):
        text = (ROOT / "skills" / "sd-ship" / "references" / "open-prs.md").read_text(encoding="utf-8")
        for phrase in ("sd assignments build N --finding", "Past round 3",
                       "--kind decision", "does not manage"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
