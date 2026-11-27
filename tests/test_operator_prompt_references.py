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
        "triage.md": ("triage", "park review"),
        "health.md": ("health check",),
        "open-threads.md": ("open threads", "collect followups"),
    },
    "sd-research-repo": {
        "fleet-review.md": ("review my research repos",),
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
        for skill in [s for s in PROMPTS if model_invocable(s)]:
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

    def test_review_batch_questions_go_to_the_review_page(self):
        """A review batch's questions are labelled on the dashboard, not answered with --answers."""
        for skill, name in (("sd-next", "triage.md"), ("sd-research-repo", "fleet-review.md")):
            with self.subTest(reference=name):
                self.assertIn("dashboard's Review page", reference(skill, name))

    def test_each_state_move_names_its_command(self):
        """`unpark`, `set it ready` and `retry` are verbs of real commands; say which."""
        for skill, name in (("sd-next", "triage.md"), ("sd-research-repo", "fleet-review.md"),
                            ("sd-status", "deep.md")):
            with self.subTest(reference=name):
                self.assertIn("sd task status N ready", reference(skill, name))
        triage = reference("sd-next", "triage.md")
        self.assertIn("sd task edit N --unpark", triage)
        self.assertIn("sd assignments cancel <assignment>", triage)

    def test_triage_cancels_no_member_of_a_live_bundle(self):
        """Cancelling one member of a running bundle parks the whole run (sd:10795)."""
        triage = reference("sd-next", "triage.md")
        self.assertIn("Cancel only an assignment whose run has ended or lost its beat", triage)
        self.assertIn("A member of a live bundle waits for its run to end", triage)

    def test_item_writes_bind_to_the_revision_read(self):
        """A status move or a cancel decided from a read carries that read's revision."""
        for skill, name in (("sd-next", "triage.md"), ("sd-research-repo", "fleet-review.md"),
                            ("sd-status", "deep.md")):
            with self.subTest(reference=name):
                self.assertIn("--if-revision <revision>", reference(skill, name))
        self.assertIn("sd assignments cancel <assignment> --if-revision <revision>",
                      reference("sd-next", "triage.md"))

    def test_deep_review_writes_only_as_its_reference_says(self):
        """sd-status writes nothing, except the deep run's items and report (sd:10795)."""
        self.assertIn("`/sd-status deep`", description("sd-status"))
        self.assertIn("**Never write, anywhere**, outside a `/sd-status deep` run", skill_text("sd-status"))

    def test_deep_review_reads_the_kiss_audit_for_new_mechanisms(self):
        """The weekly audit already counts new mechanisms; the run reads it (sd:10800 suggestion 3)."""
        self.assertIn("sd store items --grep 'KISS audit and grill' --json", reference("sd-status", "deep.md"))


class ScopeOfEachWrite(unittest.TestCase):
    """Class pass (sd:10821): a run's write reaches past what its scope or the operator chose.

    The open blockers at the split (triage.md:40, open-threads.md:9, fleet-review.md:21, deep.md:65) are
    instances; each test reads every reference, not only the one a finding named.
    """

    def test_an_answer_is_decided_as_the_run_decides_before_it_builds(self):
        """triage.md:40: "set it ready and build it" after an answer starts a fresh build of a failed item."""
        for path, text in all_references().items():
            with self.subTest(path=path):
                self.assertNotRegex(text, r"(?i)answer unblocks an item, set it ready")

    def test_no_reference_keeps_its_own_ask_and_record_rules(self):
        """open-threads.md:9: an own --answers step had no question note to answer; the shared block files one."""
        for path, text in all_references().items():
            if path == SHARED_RELATIVE:
                continue
            with self.subTest(path=path):
                self.assertNotIn("--answers", text)
                self.assertNotIn("AskUserQuestion", text)

    def test_the_fleet_review_reads_and_acts_on_the_reviewed_repos_only(self):
        """fleet-review.md:21: `sd today --here` is one checkout; its Decisions rows reach other repos."""
        text = reference("sd-research-repo", "fleet-review.md")
        self.assertNotIn("sd today --here", text)
        self.assertIn("sd today --only <name>", text)
        ask = text.split("## 6. Ask\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("items of the reviewed repos only", ask)

    # Class pass, round 3 (sd:10821): a run predicts what a press will queue. The press picks the bundle a
    # Retry takes (`_retry_row`: the one that ran last, past a newer build cancelled before its claim), so a
    # check of the bundle the run guessed missed a questioned member. No run names a bundle now: a question
    # on any item of a repository holds every press there, and the deep run presses only items it filed.

    PRESSING = ("skills/sd-next/references/triage.md", "skills/sd-research-repo/references/fleet-review.md",
                "skills/sd-status/references/deep.md")

    def test_the_pressing_runs_are_the_ones_listed(self):
        """Read from the files: every reference that prescribes a press is held to the rules below."""
        found = tuple(sorted(path for path, text in all_references().items() if "`sd assignments build N`" in text))
        self.assertEqual(found, tuple(sorted(self.PRESSING)))

    def test_no_run_predicts_which_bundle_a_press_takes(self):
        for path in self.PRESSING:
            text = all_references()[path]
            with self.subTest(path=path):
                self.assertNotIn("build_run", text)
                self.assertNotRegex(text, r"(?i)newest (one|assignment)")
                self.assertNotRegex(text, r"(?i)every member of the item's bundle")

    def test_a_question_holds_every_press_in_its_repository(self):
        """Triage and the fleet review read every item a Retry can take, so the hold needs no bundle."""
        for path in self.PRESSING[:2]:
            text = all_references()[path]
            with self.subTest(path=path):
                self.assertIn("The press, not this run, picks what a build queues", text)
                self.assertIn("holds every press in that repository", text)
                self.assertIn("planning, ready, blocked or parked", text)
                # No per-item skip stands in for the hold: a skipped item's sibling pressed it anyway.
                self.assertNotRegex(text, r"Skip an item with an open question note")

    def test_the_fleet_review_sets_ready_only_where_no_question_holds(self):
        """fleet-review.md:46: a ready item is auto-picked, so a held repository sets none ready."""
        rule = reference("sd-research-repo", "fleet-review.md").split("## 3.", 1)[1].split("\n## ", 1)[0]
        ready = next(line for line in rule.splitlines() if "unblock it and set it ready" in line)
        self.assertIn("and its repository has no open question (step 0 holds it)", ready)

    def test_the_deep_run_presses_only_items_it_filed(self):
        """A new item has no bundle: its press queues it alone. An older item's build is triage's."""
        rule = reference("sd-status", "deep.md").split("## 6.", 1)[1]
        self.assertIn("only for an item this run filed", rule)
        self.assertNotIn("is only retried with", rule)

    def test_the_deep_run_holds_a_press_in_a_questioned_repository(self):
        """deep.md:69: a claim bundles a fresh row with the repository's other queued rows (`_bundle`)."""
        rule = reference("sd-status", "deep.md").split("## 6.", 1)[1]
        self.assertNotIn("queues that item alone", rule)
        self.assertIn("planning, ready, blocked or parked", rule)
        self.assertIn("holds every press in that repository", rule)

    # Class pass, round 6 (sd:10821): the hold reads a question's state at a moment that disagrees with its
    # answer's record. Every press that follows an answer, read from the files: deep step 6, triage step 4,
    # park review, fleet review step 6.

    def test_a_build_now_answer_is_not_held_by_its_own_question(self):
        """deep.md:70: the status is written before the decision note, so the answered question reads open."""
        rule = reference("sd-status", "deep.md").split("## 6.", 1)[1]
        self.assertIn("named by its note id, does not count", rule)
        self.assertIn("Read the hold now, not from an earlier read", rule)

    def test_a_held_build_now_sets_nothing_ready(self):
        """A ready item builds without a press, so a held answer leaves its item planned."""
        rule = reference("sd-status", "deep.md").split("## 6.", 1)[1]
        self.assertIn("Held: record the answer", rule)
        self.assertIn("never set it ready", rule)
        self.assertLess(rule.index("Held: record the answer"), rule.index("`sd task status N ready --if-revision"))

    def test_a_press_after_an_answer_reads_the_questions_again(self):
        """The read before the asking is older than the answers: an answered question would still hold."""
        triage, fleet = reference("sd-next", "triage.md"), reference("sd-research-repo", "fleet-review.md")
        parts = {"triage step 4": triage.split("\n4. Ask\n", 1)[1].split("\n5. Report", 1)[0],
                 "park review": triage.split("## Park review\n", 1)[1],
                 "fleet step 6": fleet.split("## 6. Ask\n", 1)[1]}
        for name, part in parts.items():
            with self.subTest(part=name):
                self.assertIn("read its open question notes again", part)

    def test_park_review_reads_the_whole_repository_before_a_press(self):
        """triage.md:51: park review skips step 1, where the step-0 hold reads its questions."""
        park = reference("sd-next", "triage.md").split("## Park review\n", 1)[1]
        self.assertIn("open question notes of every planning, ready, blocked or parked item", park)

    def test_a_routine_ruling_queues_no_build(self):
        """deep.md:65: a routine ruling built items the operator did not choose."""
        rule = reference("sd-status", "deep.md").split("## 6.", 1)[1]
        self.assertIn("A routine ruling never builds", rule)

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
                                       r"|^sd store items (--parked )?(--grep 'PLACEHOLDER:' )?--json$"
                                       # The weekly audit item lives in the system repo, not the reviewed one.
                                       r"|^sd store items --grep 'KISS audit and grill' --json$"),
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
