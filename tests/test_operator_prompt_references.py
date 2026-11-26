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
        "health.md": ("health check",),
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
        for skill in ("sd-next",):
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


def model_invocable(skill: str) -> bool:
    head = skill_text(skill).split("\n---", 1)[0]
    return "disable-model-invocation: true" not in head


def reference(skill: str, name: str) -> str:
    return (ROOT / "skills" / skill / "references" / name).read_text(encoding="utf-8")


class OpenPrSweepMovedOut(unittest.TestCase):
    def test_no_skill_carries_or_routes_the_open_pr_sweep(self):
        """The open-PR sweep moved to sd:10803 (ruling on sd:10795)."""
        self.assertFalse((ROOT / "skills" / "sd-ship" / "references" / "open-prs.md").exists())
        for skill in ("sd-next", "sd-ship"):
            with self.subTest(skill=skill):
                self.assertNotIn("open-prs", skill_text(skill))
                self.assertNotIn("check open PRs", skill_text(skill))


class ReviewRoundFixes(unittest.TestCase):
    def test_a_plain_trigger_reaches_a_skill_the_model_may_load(self):
        """A plain trigger must reach a skill the model may load."""
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

def all_references() -> dict[str, str]:
    found = {f"skills/{skill}/references/{name}": reference(skill, name)
             for skill, files in PROMPTS.items() for name in files}
    found[SHARED_RELATIVE] = SHARED.read_text(encoding="utf-8")
    return found


#: The one copy of the decide-ask-record rules every operator run follows (sd:10800).
SHARED_RELATIVE = "skills/_shared/references/decide-ask-record.md"
SHARED = ROOT / SHARED_RELATIVE
#: What makes a question routine; only the shared block may say it.
ROUTINE = "money, risk, a standing ruling or scope"
#: Runs about one machine: their refs name the host, so two machines never share one.
MACHINE_RUNS = (("sd-next", "health.md"),)


class SharedDecideAskRecord(unittest.TestCase):
    """Each operator run names the shared block, a ref tag and a RESULT line (sd:10800)."""

    def test_the_shared_block_holds_the_rules(self):
        text = SHARED.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# "))
        for phrase in (ROUTINE, "AskUserQuestion", "(Recommended)", "--answers", "veto table",
                       "RESULT: filed <n>, built <n>, asked <n>"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_each_reference_names_the_block_a_ref_tag_and_a_result_line(self):
        for skill, files in PROMPTS.items():
            for name in files:
                text = reference(skill, name)
                with self.subTest(skill=skill, reference=name):
                    self.assertIn("references/decide-ask-record.md", text)
                    self.assertRegex(text, r"`--ref [a-z-]+(:<repo>|:<host>)?:<date>:<slug>`")
                    self.assertRegex(text, r"(?m)^End with .*`RESULT: ")
                    self.assertRegex(text, r"(?i)budget: about \d+ minutes")

    def test_each_citing_skill_ships_the_block(self):
        """Only a SKILL.md citation copies a shared file into the installed skill."""
        for skill in PROMPTS:
            with self.subTest(skill=skill):
                self.assertIn("references/decide-ask-record.md`", skill_text(skill))

    def test_the_last_run_lookup_reaches_done_and_parked_items_of_any_age(self):
        """A plain list hides done items past a week and every parked item (review of sd:10800)."""
        text = SHARED.read_text(encoding="utf-8")
        lookup = text.split("## Open with the last run", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("--since", lookup)
        self.assertIn("`sd store items --grep '<tag>:' --json`", lookup)
        self.assertIn("`sd store items --parked --grep '<tag>:' --json`", lookup)
        # --grep reads the title, body and notes, never the ref: the body must carry it.
        self.assertIn("--body 'ref: <tag>:<date>:<slug>", text)

    def test_a_machine_run_puts_its_host_in_the_tag(self):
        """Two machines' health checks file the same finding as two items (review of sd:10800)."""
        self.assertIn("`<host>`", SHARED.read_text(encoding="utf-8"))
        for skill, name in MACHINE_RUNS:
            with self.subTest(reference=name):
                text = reference(skill, name)
                self.assertIn("this run's tag is `health:<host>`", text)
                self.assertNotRegex(text, r"--ref health:<date>")

    def test_no_other_file_repeats_the_routine_definition(self):
        repeats = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "skills").rglob("*.md"))
                   if path != SHARED and ROUTINE in path.read_text(encoding="utf-8")]
        self.assertEqual(repeats, [])


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
        self.assertGreater(seen, 0)

    def test_each_command_carries_its_required_arguments(self):
        required = {
            ("sd", "assignments", "build"): r"build (N|sd:N)\b",
            ("sd", "assignments", "cancel"): r"cancel PLACEHOLDER",
            ("sd", "task", "note"): r"note N \S",
            ("sd", "task", "status"): r"status N \w+",
            ("sd", "task", "edit"): r"edit N --",
            # A run's follow-up reads every repo's tagged items in one window (sd:10800).
            ("sd", "store", "items"): (r"--(belongs-to PLACEHOLDER|here)"
                                       r"|^sd store items (--parked )?--grep 'PLACEHOLDER:' --json$"),
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

    def test_no_reference_looks_a_ref_up_by_wildcard(self):
        """`sd store items --ref` matches one exact reference; `--grep` reads the body."""
        for path, text in all_references().items():
            with self.subTest(path=path):
                self.assertNotRegex(text, r":\*`")

    # Class pass (sd:10795): no reference predicts, from an earlier read, whether a write applies. The write
    # checks in its own transaction; the reference binds it to what was read and says what a refusal means.

    def test_no_reference_prefilters_a_build_press(self):
        """The press refuses an item it cannot build; a list read earlier says nothing by then."""
        for path, text in all_references().items():
            with self.subTest(path=path):
                self.assertNotRegex(text, r"(with no|unless a build is already) queued or running")
                if "sd assignments build N`" in text:
                    self.assertIn("the press refuses", text)


if __name__ == "__main__":
    unittest.main()
