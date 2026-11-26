"""Operator prompts live as reference files in existing skills (sd:10795).

The operator ruled on 2026-11-25 to fold each standing prompt into a skill
that already answers its kind of question, rather than add a skill or a
command. A prompt is reachable only if its file exists and its skill names
the trigger the operator types, so this checks both, exactly.
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
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
    "sd-ship": {
        "open-prs.md": ("check open PRs",),
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


class OpenPrSweep(unittest.TestCase):
    """The open-PR sweep (sd:10803), split from sd:10795 after review rounds."""

    def text(self) -> str:
        return reference("sd-ship", "open-prs.md")

    def test_sd_next_routes_the_plain_ask(self):
        self.assertIn("check open PRs", description("sd-next"))
        self.assertIn("skills/sd-ship/references/open-prs.md`", skill_text("sd-next"))

    def test_sweep_queues_nothing_it_must_cancel_after(self):
        """A wrong entry queued then cancelled sat in the lane between the two writes."""
        self.assertNotIn("lane cancel", self.text())

    def test_sweep_checks_the_worktree_branch_before_it_enqueues(self):
        text = self.text()
        check = text.index("refs/heads/<head branch>")
        self.assertLess(check, text.index("lane enqueue"))

    def test_a_fork_pr_is_reported_before_any_match(self):
        """A fork's head branch can share a name with one in the base repo."""
        text = self.text()
        fork = text.index("head.repo.full_name")
        self.assertLess(fork, text.index("lane list"))
        self.assertIn("is a fork PR: report it and act on nothing", text)

    # Class pass (sd:10803): the CLI keys each write by item, and the sweep means one PR's branch.
    # Every item-keyed command in the sweep is bound to this PR's branch before it runs, or names the PR.

    def item_keyed(self) -> list[str]:
        return [command for command in prescribed_commands(self.text())
                if re.search(r"(build|retry|note) N\b|--item N\b", command)]

    def test_every_item_keyed_command_is_enumerated(self):
        """A new item-keyed command must join the class, not slip past it."""
        kinds = sorted({" ".join(parse(command)[1]) for command in self.item_keyed()})
        self.assertEqual(kinds, ["lane enqueue", "task note"])

    def test_sweep_runs_no_write_it_cannot_bind_to_the_branch(self):
        """`build N --finding` and `lane retry N` pick by item: a write
        queued since the sweep's read sends either to another branch."""
        kinds = {" ".join(parse(command)[1]) for command in prescribed_commands(self.text())}
        self.assertFalse(kinds & {"assignments build", "lane retry"}, kinds)
        self.assertIn("the operator's Fix", self.text())
        self.assertIn("the operator's Retry", self.text())

    def test_an_item_with_two_open_prs_is_reported_only(self):
        text = self.text()
        guard = text.index("resolve to one item: report each and act on none")
        self.assertLess(guard, text.index("4. Act on the rest"))

    def test_each_note_names_its_pr(self):
        notes = [command for command in self.item_keyed() if " note N" in command]
        self.assertEqual(len(notes), 2, notes)
        for command in notes:
            with self.subTest(command=command):
                self.assertIn("<PR URL", command)

    # Class pass 2 (sd:10803), identity by proxy: a lane entry holds no PR number, so branch and head only
    # stand in for the PR. A strict match misses the PR's own entry after a catch-up push; a loose one
    # takes a replaced PR's. So an entry never names the item or allows a write: any on the branch blocks.

    def test_no_lane_entry_names_the_item(self):
        """A replacement PR on the same branch and commit would inherit the old PR's item."""
        text = self.text()
        self.assertNotIn("Its item is the lane entry's `item`", text)
        self.assertIn("A lane entry never names the item", text)
        self.assertIn("as a whole word", text)

    def test_an_entry_on_the_branch_at_any_head_blocks_the_enqueue(self):
        """A catch-up push moves the head past a failed entry's; it is still this branch's."""
        text = self.text()
        seen = text.index("whose `branch` equals the PR's head branch exactly, at any `expected_head` and in any `status`")
        self.assertLess(seen, text.index("4. Act on the rest"))
        enqueue = text.index("- Enqueue a PR")
        self.assertIn("only when the lane has no entry on its branch", text[enqueue:enqueue + 300])
        self.assertNotIn("no lane entry at its head", text)

    def test_item_entries_block_too(self):
        """The item's failed entry on another branch is not this PR's to pass by."""
        text = self.text()
        enqueue = text.index("- Enqueue a PR")
        self.assertIn("no entry of item N but `merged` ones", text[enqueue:enqueue + 300])

    def test_a_failed_entry_is_reported_with_its_own_fields(self):
        """The operator, not a head match, decides whether the entry is this PR's."""
        text = self.text()
        retry = text.index("for the operator's Retry")
        self.assertIn("`item`, `status` and `expected_head`", text[retry - 400:retry + 400])
        self.assertNotIn("at this PR's head", text)

    def test_a_running_entry_is_read_by_branch_or_item(self):
        text = self.text()
        self.assertIn("an entry on its branch or of its item is `pending` or `running`", text)

    def test_a_stacked_pr_is_reported_not_queued(self):
        """A squash merge of a parent strands each PR whose base is its branch."""
        text = self.text()
        enqueue = text.index("lane enqueue")
        self.assertLess(text.index("`base.ref`"), enqueue)
        self.assertLess(text.index("another open PR's base is this PR's head branch"), enqueue)
        self.assertLess(text.index("its base is not the repo's default branch"), enqueue)

    def test_item_is_matched_exactly(self):
        text = self.text()
        self.assertIn("equals the PR's head branch exactly", text)
        self.assertIn("as a whole word", text)


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

    def test_a_machine_run_puts_its_host_in_the_tag(self):
        """Two machines' health checks file the same finding as two items (review of sd:10800)."""
        self.assertIn("`<host>`", SHARED.read_text(encoding="utf-8"))
        for skill, name in MACHINE_RUNS:
            with self.subTest(reference=name):
                text = reference(skill, name)
                self.assertIn("this run's base tag is `health` and its tag `health:<host>`", text)
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
                                       r"|^sd store items (--parked )?(--grep 'PLACEHOLDER:' )?--json$"),
            # A task filed with no priority is refused (sd:10800 class pass).
            ("sd", "task", "add"): r"--priority PLACEHOLDER .*--ref \S+ --body ",
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


def section(title: str) -> str:
    return SHARED.read_text(encoding="utf-8").split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]


class LastRunLookup(unittest.TestCase):
    """Class pass (sd:10800): the lookup reached an item only through what its filing may not have written.

    Each instance is an item in a scratch store that one filter of the earlier lookups hid; the commands
    are read from the shared block and run, so the block cannot name a read that misses one.
    """

    FILL = {"<base>": "health", "<tag>": "health:hub", "<date>": "2026-11-20", "<slug>": "swap",
            "<title>": "swap is full", "<1-4>": "3", "<path>": "PLACEHOLDER"}

    @classmethod
    def setUpClass(cls) -> None:
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        cls.home = Path(scratch.name)
        cls.run_tool("sd-db", "init")

    @classmethod
    def run_tool(cls, tool: str, *words: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / tool), *words], capture_output=True, text=True, check=False,
            env=clean_environment(HOME=str(cls.home), XDG_CONFIG_HOME=str(cls.home)))

    def filled(self, command: str) -> list[str]:
        for placeholder, value in self.FILL.items():
            command = command.replace(placeholder, value)
        self.assertNotRegex(command, r"<[^>]*>")
        return shlex.split(command)

    def add(self, title: str, ref: str, *more: str) -> int:
        done = self.run_tool("sd", "task", "add", title, "--no-repo", "--priority", "3", "--ref", ref, "--json", *more)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)["item"]["id"]

    def lookup(self) -> dict[int, str]:
        commands = [c for c in prescribed_commands(section("Open with the last run")) if c.startswith("sd store items")]
        self.assertGreater(len(commands), 0)
        found: dict[int, str] = {}
        for command in commands:
            answer = self.run_tool(*self.filled(command))
            self.assertEqual(answer.returncode, 0, answer.stderr)
            found.update({row["id"]: row["ref"] for row in json.loads(answer.stdout)})
        return found

    def test_the_lookup_reads_every_way_an_item_was_filed_or_hidden(self):
        cases = {
            "an open item filed before the host and body-line rules": self.add("low disk", "health:2026-11-25:low-disk"),
            "an open item whose body line was edited away": self.add("load", "health:hub:2026-11-24:load", "--body", "x"),
            "a parked item with no body line": self.add("fan", "health:hub:2026-09-01:fan"),
            "a parked item with its body line": self.add("temp", "health:hub:2026-09-02:temp", "--body",
                                                         "ref: health:hub:2026-09-02:temp"),
            "a done item past the list's week": self.add("swap", "health:hub:2026-10-01:swap", "--body",
                                                         "ref: health:hub:2026-10-01:swap"),
        }
        old = cases["a done item past the list's week"]
        self.assertEqual(self.run_tool("sd", "task", "status", str(old), "done", "--reason", "fixed").returncode, 0)
        store = sqlite3.connect(self.home / ".local" / "share" / "sd" / "sd.db")
        self.addCleanup(store.close)
        store.execute("UPDATE note SET timestamp = '2026-01-01T00:00:00+00:00' WHERE item = ?", (old,))
        for name in ("a parked item with no body line", "a parked item with its body line"):
            store.execute("UPDATE item SET parked_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (cases[name],))
        store.commit()
        found = self.lookup()
        for name, item in cases.items():
            with self.subTest(case=name):
                self.assertIn(item, found)

    def test_the_filing_command_is_accepted_and_refuses_its_rerun(self):
        commands = [c for c in prescribed_commands(section("Tag what the run files")) if c.startswith("sd task add")]
        self.assertEqual(len(commands), 1, commands)
        words = [word for word in self.filled(commands[0]) if word != "PLACEHOLDER"]
        words = [word if word != "--belongs-to" else "--no-repo" for word in words]
        first = self.run_tool(*words)
        self.assertEqual(first.returncode, 0, first.stderr)
        again = self.run_tool(*words)
        self.assertEqual(again.returncode, 1)
        self.assertIn("already carries --ref health:hub:2026-11-20:swap", again.stderr)

    def test_the_lookup_drops_no_tagged_item_in_silence(self):
        """Each ref under the base tag gets a named place; none is filtered out unseen."""
        text = section("Open with the last run")
        for phrase in ("`host unknown`", "another host", "`unreadable ref`", "not as no previous run",
                       "older than a week"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        self.assertNotIn("'<tag>:'", text)


if __name__ == "__main__":
    unittest.main()
