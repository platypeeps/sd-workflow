"""Retired skills: their directories are gone and nothing live names them.

sd-presentation and sd-publish went in sd:8286. The contrib skills below
were never used once and went in sd:9062 (grill system sd:9044); the
last group, unused for 60 days, went in sd:10156. Reinstate one from git
history when a need appears.
"""

import re
import subprocess
import unittest
from pathlib import Path

from tests.clean_env import clean_environment

ROOT = Path(__file__).resolve().parents[1]
RETIRED = (
    "sd-presentation",
    "sd-publish",
    "sd-action-inbox",
    "sd-adr-review",
    "sd-agenda",
    "sd-ask-me",
    "sd-bookmark-triage",
    "sd-capture",
    "sd-checklist",
    "sd-coherence-audit",
    "sd-compare",
    "sd-continuity-packet",
    "sd-docs-bustest",
    "sd-draft",
    "sd-draft-review",
    "sd-evaluate",
    "sd-feedback",
    "sd-grill",
    "sd-knowledge-gap",
    "sd-learn",
    "sd-literature-map",
    "sd-meeting-follow-through",
    "sd-meeting-prep",
    "sd-monitor",
    "sd-objective-plan",
    "sd-paper",
    "sd-postmortem",
    "sd-premortem",
    "sd-profile",
    "sd-propose-skills",
    "sd-publish-drive",
    "sd-rebase-hygiene",
    "sd-red-team",
    "sd-retro",
    "sd-runbook",
    "sd-rust-async",
    "sd-rust-design",
    "sd-rust-modules",
    "sd-rust-quality",
    "sd-rust-review",
    "sd-scan",
    "sd-skill-retro",
    "sd-socratic-review",
    "sd-sop",
    "sd-stakeholder-map",
    "sd-status-update",
    "sd-study-guide",
    "sd-tdd",
    "sd-thread-digest",
    "sd-tips",
    "sd-topic-radar",
    "sd-tutorial",
    "sd-typed-holes",
    "sd-video-notes",
    "sd-weekly-review",
    "sd-author",
    "sd-brief",
    "sd-decide",
    "sd-digest",
    "sd-explain",
    "sd-plan",
    "sd-proposal",
    "sd-prose-lint",
    "sd-receive-review",
    "sd-spec",
    "sd-technical-editor",
)
#: Any name, not followed by more of a name (`sd-publish` is not `sd-publish-x`).
PATTERN = r"(^|[^-_[:alnum:]])(" + "|".join(re.escape(name) for name in RETIRED) + r")([^-_[:alnum:]]|$)"


class RetiredSkills(unittest.TestCase):
    def test_skill_directories_are_gone(self):
        for name in RETIRED:
            for folder in ("skills", "contrib"):
                self.assertFalse((ROOT / folder / name).exists(), f"{folder}/{name}")

    def test_no_live_file_names_them(self):
        # Tracked files only; history in docs/work records the skills as they were.
        found = subprocess.run(
            ["git", "-C", str(ROOT), "grep", "-n", "-I", "-E", PATTERN, "--", ".",
             ":(exclude)docs/work", ":(exclude)tests/test_retired_skills.py"],
            capture_output=True, text=True, check=False, env=clean_environment())
        self.assertIn(found.returncode, (0, 1), found.stderr)
        self.assertEqual("", found.stdout)


if __name__ == "__main__":
    unittest.main()
