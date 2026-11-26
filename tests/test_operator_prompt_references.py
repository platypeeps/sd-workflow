"""Operator prompts live as reference files in existing skills (sd:10795).

The operator ruled on 2026-11-25 to fold each standing prompt into a skill
that already answers its kind of question, rather than add a skill or a
command. A prompt is reachable only if its file exists and its skill names
the trigger the operator types, so this checks both, exactly.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from clean_env import clean_environment  # noqa: E402

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


def model_invocable(skill: str) -> bool:
    head = skill_text(skill).split("\n---", 1)[0]
    return "disable-model-invocation: true" not in head


def reference(skill: str, name: str) -> str:
    return (ROOT / "skills" / skill / "references" / name).read_text(encoding="utf-8")


class ReviewRoundFixes(unittest.TestCase):
    def test_a_plain_trigger_reaches_a_skill_the_model_may_load(self):
        """sd-ship is operator-invoked only, so "check open PRs" must route elsewhere."""
        for skill, files in PROMPTS.items():
            for name, triggers in files.items():
                for trigger in triggers:
                    if trigger.startswith("/"):
                        continue
                    with self.subTest(trigger=trigger):
                        loaders = [s for s in PROMPTS if model_invocable(s)
                                   and trigger in description(s)
                                   and f"skills/{skill}/references/{name}`" in skill_text(s)
                                   or (s == skill and model_invocable(s) and trigger in description(s))]
                        self.assertTrue(loaders, trigger)

    def test_each_filed_finding_gets_its_own_ref(self):
        """A ref names one item, so one ref per run files only the first finding."""
        self.assertIn("`deep-review:<repo>:<slug>`", reference("sd-status", "deep.md"))
        self.assertNotIn("--ref research-review:<date> ", reference("sd-research-repo", "fleet-review.md"))
        self.assertIn("--ref research-review:<repo>:<slug>", reference("sd-research-repo", "fleet-review.md"))

    def test_review_batch_questions_go_to_the_review_page(self):
        """A review batch's questions are labelled on the dashboard, not answered with --answers."""
        for skill, name in (("sd-next", "triage.md"), ("sd-research-repo", "fleet-review.md")):
            text = reference(skill, name)
            with self.subTest(reference=name):
                self.assertIn("dashboard's Review page", text)
                self.assertNotRegex(text, r"Walk every open question, decision and score")
                self.assertNotRegex(text, r"review questions or scores\.?\n")

    def test_enqueue_binds_the_head_the_sweep_checked(self):
        text = reference("sd-ship", "open-prs.md")
        self.assertIn("sd-ship -C <worktree> lane enqueue", text)
        self.assertIn("--expected-head <head>", text)


def all_references() -> dict[str, str]:
    return {f"skills/{skill}/references/{name}": reference(skill, name)
            for skill, files in PROMPTS.items() for name in files}


def prescribed_commands(text: str) -> list[str]:
    """Each `sd ...` or `sd-ship ...` command a reference prescribes, inline or fenced."""
    joined = text.replace("\\\n", " ")
    inline = re.findall(r"`((?:sd|sd-ship) [^`]+)`", joined)
    fenced = [line.strip() for block in re.findall(r"```bash\n(.*?)```", joined, re.S)
              for line in block.splitlines() if line.strip().startswith(("sd ", "sd-ship "))]
    return inline + fenced


def parse(command: str) -> tuple[str, list[str], list[str]]:
    """(tool, subcommand words, flags) of a prescribed command; placeholders are not words."""
    words = shlex.split(re.sub(r"<[^>]*>", "PLACEHOLDER", command))
    tool, rest = words[0], words[1:]
    if rest[:1] == ["-C"]:
        rest = rest[2:]
    verbs: list[str] = []
    for word in rest:
        if not re.fullmatch(r"[a-z][a-z-]*", word):
            break
        verbs.append(word)
    return tool, verbs, re.findall(r"(?<![\w-])--[a-z][a-z-]*", " ".join(rest))


class PrescribedCommandsMatchTheCli(unittest.TestCase):
    """The class three review rounds found: a reference prescribes a command the CLI refuses.

    Every instance is read from the references, not from a finding: each
    prescribed command is parsed and held against the tool's own --help.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.helps: dict[tuple[str, ...], subprocess.CompletedProcess] = {}

    def help(self, tool: str, verbs: list[str]) -> subprocess.CompletedProcess:
        key = (tool, *verbs)
        if key not in self.helps:
            self.helps[key] = subprocess.run(
                [sys.executable, str(ROOT / "bin" / tool), *verbs, "--help"], capture_output=True,
                text=True, env=clean_environment(HOME=self.scratch.name), check=False)
        return self.helps[key]

    def test_each_command_names_a_real_verb_and_real_flags(self):
        seen = 0
        for path, text in all_references().items():
            for command in prescribed_commands(text):
                tool, verbs, flags = parse(command)
                with self.subTest(path=path, command=command):
                    seen += 1
                    answer = self.help(tool, verbs)
                    self.assertEqual(answer.returncode, 0, answer.stderr[-300:])
                    self.assertIn(" ".join([tool, *verbs]), answer.stdout.split("\n\n")[0].replace("\n", " "))
                    for flag in flags:
                        self.assertRegex(answer.stdout, rf"(?<![\w-]){re.escape(flag)}(?![\w-])")
        self.assertGreater(seen, 20)

    def test_each_command_carries_its_required_arguments(self):
        required = {
            ("sd", "assignments", "build"): r"build (N|sd:N)\b",
            ("sd", "assignments", "cancel"): r"cancel PLACEHOLDER",
            ("sd", "task", "note"): r"note N \S",
            ("sd", "task", "status"): r"status N \w+",
            ("sd", "task", "edit"): r"edit N --",
            ("sd", "store", "items"): r"--(belongs-to PLACEHOLDER|here)",
            ("sd-ship", "lane", "enqueue"): r"--item N .*--title .*--body-file .*--expected-head ",
            ("sd-ship", "lane", "retry"): r"retry N\b",
        }
        for path, text in all_references().items():
            for command in prescribed_commands(text):
                tool, verbs, _ = parse(command)
                pattern = required.get((tool, *verbs))
                if pattern is None:
                    continue
                with self.subTest(path=path, command=command):
                    self.assertRegex(re.sub(r"<[^>]*>", "PLACEHOLDER", command), pattern)

    def test_a_decision_note_carries_its_text(self):
        for path, text in all_references().items():
            for command in prescribed_commands(text):
                if "--kind decision" in command:
                    with self.subTest(path=path, command=command):
                        self.assertRegex(command, r'note N "<[^>]+>" --kind decision')

    def test_enqueue_names_the_delivery_claim(self):
        """The first enqueue of an item refuses without --deliver or --associate-only."""
        text = reference("sd-ship", "open-prs.md")
        enqueue = next(c for c in prescribed_commands(text) if "lane enqueue" in c)
        self.assertIn("--deliver|--associate-only", enqueue)
        self.assertIn("`--deliver` when", text)
        self.assertIn("`--associate-only` when", text)

    def test_a_ref_names_the_finding_not_the_run(self):
        """`sd task add --ref` refuses a ref an item holds: a date or rank collides on a rerun."""
        for skill, name, source in (("sd-status", "deep.md", "deep-review"),
                                    ("sd-research-repo", "fleet-review.md", "research-review")):
            text = reference(skill, name)
            with self.subTest(reference=name):
                self.assertIn(f"{source}:<repo>:<slug>", text)
                for run_key in ("<date>:<rank>", "<date>:<n>", f"{source}:<date>"):
                    self.assertNotIn(f"ref {source}:<repo>:{run_key}", text)
                    self.assertNotIn(f"`{source}:<repo>:{run_key}`", text)
                self.assertIn("already carries --ref", text)

    def test_no_reference_looks_a_ref_up_by_wildcard(self):
        """`sd store items --ref` matches one exact reference; `--grep` reads the body."""
        for path, text in all_references().items():
            with self.subTest(path=path):
                self.assertNotRegex(text, r":\*`")
        self.assertIn('sd store items --here --grep "deep-review:<repo>"', reference("sd-status", "deep.md"))

    def test_each_state_move_names_its_command(self):
        """`unpark`, `set it ready` and `retry` are verbs of real commands; say which."""
        for skill, name in (("sd-next", "triage.md"), ("sd-research-repo", "fleet-review.md"),
                            ("sd-status", "deep.md")):
            with self.subTest(reference=name):
                self.assertIn("sd task status N ready", reference(skill, name))
        triage = reference("sd-next", "triage.md")
        self.assertIn("sd task edit N --unpark", triage)
        self.assertIn("sd assignments cancel <assignment>", triage)
        self.assertIn("sd-ship -C <worktree> lane retry N", reference("sd-ship", "open-prs.md"))

    def test_triage_cancels_no_member_of_a_live_bundle(self):
        """Cancelling one member of a running bundle parks the whole run (sd:10795)."""
        triage = reference("sd-next", "triage.md")
        self.assertIn("Cancel only an assignment whose run has ended or lost its beat", triage)
        self.assertIn("A member of a live bundle waits for its run to end", triage)


if __name__ == "__main__":
    unittest.main()
