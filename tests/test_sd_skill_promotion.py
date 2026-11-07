"""Skill CLI files isolated changes; the operator checkout is never mutated."""

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import (
    connect,
    initialise,
    registry,
    runner,
    skills_catalog,
    upsert_repo,
    workflow,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import sd_skill  # noqa: E402 - bin is not a package

#: Vocabulary that would make `sd skill promote` or `sd skill demote` sound
#: like it writes something. Neither verb touches git or a forge: each files
#: one item and returns. The help and the parser comment are read
#: against this whole family rather than against one phrase, because the
#: regression these guards exist for was never "the words `pull request`
#: appeared" -- it was "the text promised work that nothing does". A phrase
#: guard waved through a comment offering "a merge request against `main`",
#: a help string offering to "merge the move onto a path", and
#: "pull-request" spelled with a hyphen.
_FORGE_CLAIMS = (
    r"pull\s*requests?",
    r"merge\s*requests?",
    r"\bprs?\b",
    r"\bmrs?\b",
    r"\bbranch(?:es)?\b",
    r"\bcommit(?:s|ted|ting)?\b",
    r"\bpush(?:es|ed|ing)?\b",
    r"\bmerg(?:e|es|ed|ing)\b",
    r"\bgit\b",
    r"\bopen(?:s|ed|ing)?\b",
    r"\bfork(?:s|ed|ing)?\b",
    r"\brebas(?:e|es|ed|ing)\b",
    r"\bcherry\s*pick(?:s|ed|ing)?\b",
    r"\bpublish(?:es|ed|ing)?\b",
    r"\breleas(?:e|es|ed|ing)\b",
)

#: What turns a mention of that vocabulary into a denial. "no branch" and
#: "open nothing themselves" are true statements the comment exists to make,
#: so the check reads clause by clause. The two kinds of negator reach
#: differently (sd:1002, operator ruling 2026-10-03). An object negator
#: denies what its clause offers wherever it stands: "open nothing". A verb
#: negator denies only what follows it, so "open a pull request without a
#: branch" still promises the pull request. A contraction ("doesn't",
#: "won't") denies as "not" does.
_OBJECT_NEGATORS = r"\b(?:nothing|none|no\s+one)\b"
_VERB_NEGATORS = r"\b(?:no|not|never|neither|nor|without)\b|n['\u2019]t\b"
_NEGATORS = f"{_OBJECT_NEGATORS}|{_VERB_NEGATORS}"

#: Clause boundaries. `and` and `but` split as well as punctuation, so a
#: promise cannot shelter under a denial standing next to it.
_CLAUSES = r"[.,;:()]|\band\b|\bbut\b|\bwhile\b"

#: Counting words the help might use, so "file five code review tasks" is
#: measured against the one item (or, for `apply`, the one assignment) a real
#: run produces instead of being read as prose.
_COUNT_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
                "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10}

#: Where a help string says the directory ends up. The key is the directory a
#: real run of the verb names in the brief it queues, so a help string that
#: advertises the other verb's effect does not match its own verb.
_ENDPOINTS = {
    "contrib": r"\b(?:back\s+)?(?:in|on)?to\s+(?:the\s+)?contrib\b",
    "path": r"\b(?:in|on)?to\s+(?:a|an|the|one|its)\s+path\b",
}


def _normalised(text: str) -> str:
    """One lower-case line, so a line break or a hyphen cannot hide a claim."""

    return " ".join(re.sub(r"[-_]+", " ", text).split()).lower()


def _promised(text: str) -> list[str]:
    """Forge vocabulary used as a promise, ignoring what a negator denies."""

    promises: list[str] = []
    for clause in re.split(_CLAUSES, _normalised(text)):
        if re.search(_OBJECT_NEGATORS, clause):
            continue
        negator = re.search(_VERB_NEGATORS, clause)
        offered = clause[:negator.start()] if negator else clause
        promises.extend(
            pattern for pattern in _FORGE_CLAIMS if re.search(pattern, offered))
    return promises


def _stated_count(text: str) -> int | None:
    """How many things the text says it files or queues, or None if it names no number."""

    match = re.search(r"\b(?:files?|queues?)\s+(\w+)", _normalised(text))
    if match is None:
        return None
    word = match.group(1)
    return int(word) if word.isdigit() else _COUNT_WORDS.get(word)


def _stated_endpoint(text: str) -> str | None:
    """Where the text says the directory ends up, or None if it is unclear."""

    named = [name for name, pattern in _ENDPOINTS.items()
             if re.search(pattern, _normalised(text))]
    return named[0] if len(named) == 1 else None


#: The registry the system library's own `test_controls.py` seeds: two
#: `start` providers and one `url` provider, two of them independent of the
#: fixture commit's `Authored-with: codex/openai`, so a review has a reviewer
#: to order. The library reads it beside the connection's database
#: (`registry.beside`), never under `$HOME`.
_REGISTRY = """bills:
  a: {cost: subscription}
  b: {cost: subscription}
  c: {cost: plan}
providers:
  claude: {start: "claude -p", vendor: anthropic, bill: a, roles: [author, reviewer], reader: claude-json}
  codex: {start: "codex exec", vendor: openai, bill: b, roles: [author, reviewer], reader: codex-json}
  minimax: {url: "https://example.invalid/v1", model: fixture, vendor: minimax, bill: c, roles: [reviewer]}
roles:
  author: [claude, codex]
  reviewer: [codex, claude, minimax]
"""


class SkillFixture(unittest.TestCase):
    """A pack checkout, a database, and the two skills the verbs act on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "pack"
        (self.repo / "contrib/sd-candidate").mkdir(parents=True)
        (self.repo / "skills/sd-kept").mkdir(parents=True)
        for path in (
            self.repo / "contrib/sd-candidate/SKILL.md",
            self.repo / "skills/sd-kept/SKILL.md",
        ):
            path.write_text(
                "---\ndescription: Use this fixture.\n---\nA bounded fixture.\n"
            )
        (self.repo / "skills/paths.json").write_text(
            json.dumps({"paths": {"development": {"skills": ["sd-kept"]}}})
        )
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("add", ".")
        self.git("commit", "-m", "Fixture\n\nAuthored-with: codex/openai")
        self.dbpath = self.root / "sd.db"
        initialise(self.dbpath)
        (self.root / "providers.yaml").write_text(_REGISTRY)
        self.db = connect(self.dbpath)
        self.addCleanup(self.db.close)
        upsert_repo(self.db, str(self.repo), remote="https://example.invalid/pack.git")
        for replacement in (
            patch.object(sd_skill, "checkout", return_value=self.repo),
            patch.object(
                sd_skill.sd_handoff_rows,
                "connect",
                side_effect=lambda lib, write: connect(self.dbpath, write=write),
            ),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def request(self, verb, name=None, path=None, **extra):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            rc = sd_skill.run(
                argparse.Namespace(
                    verb=verb, name=name, path=path, if_revision=None, json=True,
                    **extra,
                )
            )
        self.assertEqual(rc, 0)
        return json.loads(output.getvalue())

    def reviewed(self, name="sd-kept", area=None):
        """A review whose reviewer recorded two proposals and finished.

        The reviewer half is the runner's, and a review request queues no
        assignment (system sd:5477), so the test queues one itself, marks it
        running under `claude`, and sends the result document through
        `record_review_proposals`. Returns the review item and its two
        proposal note ids.
        """
        state = self.request("review", name)
        item = state["item"]["id"]
        if area:
            held = {**json.loads(state["item"]["fields"]), "area": area}
            self.db.execute("UPDATE item SET fields=? WHERE id=?",
                            (json.dumps(held), item))
        assignment = runner.enqueue(
            self.db, [item], role="reviewer", scope="skill-review", who="test")[0]["id"]
        registry.seed(self.db, registry.read(connection=self.db))
        source = json.loads(state["item"]["fields"])["skill_review"]
        self.db.execute(
            "UPDATE assignment SET status='running', provider='claude' WHERE id=?",
            (assignment,),
        )
        path = f"skills/{name}/SKILL.md"
        document = {
            "version": 1, "item": item, "source_sha256": source["source_sha256"],
            "proposals": [
                {"path": path, "line_start": 2, "line_end": 2, "body": "Say when."},
                {"path": path, "line_start": 4, "line_end": 4, "body": "Bound it."},
            ],
        }
        state = skills_catalog.record_review_proposals(
            self.db, item, assignment, "claude", document)
        self.db.execute(
            "UPDATE assignment SET status='done' WHERE id=?", (assignment,))
        return item, [n["id"] for n in state["notes"] if n["kind"] == "proposal"]


class SkillRequests(SkillFixture):
    def assignments(self):
        return self.db.execute("SELECT count(*) FROM assignment").fetchone()[0]

    def test_promotion_and_demotion_file_a_ready_task_without_git_writes(self):
        (self.repo / "unrelated.md").write_text("Operator work")
        original = self.git("status", "--porcelain")
        head = self.git("rev-parse", "HEAD")
        promoted = self.request("promote", "sd-candidate", "development")
        demoted = self.request("demote", "sd-kept")
        for value in (promoted, demoted):
            self.assertNotIn("assignments", value)
            self.assertEqual((value["item"]["kind"], value["item"]["status"]), ("task", "ready"))
            self.assertIn("sd-ship skill", value["item"]["body"])
            self.assertEqual(value["item"]["source_commit"], head)
        # The runner is idle (system sd:5477): a filed item queues nothing.
        self.assertEqual(self.assignments(), 0)
        self.assertEqual(original, self.git("status", "--porcelain"))
        self.assertEqual(self.git("branch", "--format=%(refname:short)"), "main")
        self.assertTrue((self.repo / "contrib/sd-candidate/SKILL.md").exists())
        self.assertTrue((self.repo / "skills/sd-kept/SKILL.md").exists())

    def test_a_review_task_is_not_delivered_and_a_promotion_is(self):
        """A review writes no commit: delivery left its kind out, and now
        leaves the review task out (sd:7004). A promotion stays deliverable."""
        review = self.request("review", "sd-kept")["item"]
        promoted = self.request("promote", "sd-candidate", "development")["item"]
        self.assertFalse(skills_catalog.is_review(promoted))
        self.db.execute("UPDATE item SET status = 'ready_to_send' WHERE id IN (?, ?)", (review["id"], promoted["id"]))
        self.db.commit()
        self.assertEqual([row["id"] for row in runner.delivery_candidates(self.db)], [promoted["id"]])

    def test_proposals_are_refused_on_a_task_that_is_not_a_review(self):
        promoted = self.request("promote", "sd-candidate", "development")["item"]
        assignment = runner.enqueue(self.db, [promoted["id"]], role="reviewer", scope="skill-review", who="test")[0]["id"]
        with self.assertRaisesRegex(workflow.WorkflowError, "this skill's running reviewer"):
            skills_catalog.record_review_proposals(self.db, promoted["id"], assignment, "claude", {})

    def test_repeating_same_intent_has_one_item(self):
        first = self.request("promote", "sd-candidate", "development")
        second = self.request("promote", "sd-candidate", "development")
        self.assertEqual(first["item"]["id"], second["item"]["id"])
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM item").fetchone()[0], 1)
        self.assertEqual(self.assignments(), 0)

    def test_review_files_one_item_and_the_dashboard_writes_the_same_row(self):
        head = self.git("rev-parse", "HEAD")
        first = self.request("review", "sd-kept")
        # A task marked by its fields (sd:7004): `skill-review` left item.kind.
        self.assertEqual(first["item"]["kind"], "task")
        self.assertTrue(skills_catalog.is_review(first["item"]))
        self.assertEqual(first["item"]["source"], "skill-request")
        self.assertEqual(first["item"]["source_commit"], head)
        self.assertEqual(first["item"]["status"], "ready")
        self.assertNotIn("assignments", first)
        second = self.request("review", "sd-kept")
        self.assertEqual(first["item"]["id"], second["item"]["id"])
        # Criterion 19: the Skills screen's review is this same library call
        # with `who="dashboard"`, and `who` is outside the item's identity,
        # so the screen's request resolves to the row the CLI wrote.
        screen = skills_catalog.request(
            self.db, "sd-kept", "review", root=self.repo, home=self.root,
            who="dashboard")
        self.assertEqual(screen["item"]["id"], first["item"]["id"])
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM item").fetchone()[0], 1)
        self.assertEqual(self.assignments(), 0)

    def test_apply_after_a_reviewer_result_queues_one_isolated_change(self):
        item, notes = self.reviewed()
        applied = self.request("apply", item=item, notes=notes)
        self.assertEqual(len(applied["assignments"]), 1)
        self.assertEqual(applied["assignments"][0]["scope"], "skill-apply")
        self.assertEqual(applied["assignments"][0]["role"], "author")
        self.assertNotEqual(applied["item"]["id"], item)
        self.assertIn("sd-ship skill", applied["item"]["body"])
        brief = json.loads(applied["item"]["body"])["text"]
        accepted = json.loads(brief.split("\n\n", 1)[1])
        self.assertEqual([entry["note"] for entry in accepted], notes)
        self.assertEqual(
            [entry["body"] for entry in accepted], ["Say when.", "Bound it."])
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM assignment").fetchone()[0], 2)
        self.assertEqual(self.git("branch", "--format=%(refname:short)"), "main")

    def test_a_recorded_review_keeps_an_area_the_operator_set(self):
        """The result owns `skill_review`, not the whole `fields` (sd:8036)."""
        item, _ = self.reviewed(area="home")
        row = self.db.execute("SELECT fields FROM item WHERE id=?", (item,)).fetchone()
        self.assertEqual(json.loads(row[0]).get("area"), "home")

    def test_invalid_path_and_changed_source_do_not_file(self):
        with self.assertRaises(sd_skill.SkillRefusal):
            self.request("promote", "sd-candidate", "absent")
        (self.repo / "contrib/sd-candidate/SKILL.md").write_text("Uncommitted edit")
        with self.assertRaisesRegex(sd_skill.SkillRefusal, "commit"):
            self.request("promote", "sd-candidate", "development")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)

    def test_gateway_contains_no_git_or_github_mutation(self):
        source = (ROOT / "bin/sd_skill.py").read_text()
        for needle in (
            "subprocess",
            "git push",
            "pr create",
            "--method",
            "shutil.move",
        ):
            self.assertNotIn(needle, source)


class TheScanHoldsNoLock(SkillFixture):
    """pack sd:7853: trial and request scanned every SKILL.md (glob, read,
    hash) and ran git inside `with transaction`, so each call held the
    database's write lock for a file-system scan. Each read of the catalog's
    files or of git now runs before the transaction, and a second writer
    gets the lock throughout."""

    def locked_reads(self, call):
        """Whether the write lock was held at each file or git read `call` made."""
        held = []

        def probe():
            other = sqlite3.connect(self.dbpath, timeout=0, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
                held.append(False)
            except sqlite3.OperationalError:
                held.append(True)
            finally:
                other.close()

        snapshot, git = skills_catalog._snapshot, skills_catalog._git
        with patch.object(skills_catalog, "_snapshot", lambda *args: (probe(), snapshot(*args))[1]), \
                patch.object(skills_catalog, "_git", lambda *args: (probe(), git(*args))[1]):
            call()
        return held

    def test_a_trial_scans_the_catalog_before_its_transaction(self):
        held = self.locked_reads(lambda: skills_catalog.trial(self.db, "sd-candidate", root=self.repo, home=self.root))
        self.assertTrue(held)
        self.assertNotIn(True, held)

    def test_a_request_scans_and_reads_git_before_its_transaction(self):
        for name, action, path in (("sd-kept", "review", None), ("sd-candidate", "promote", "development"),
                                   ("sd-kept", "demote", None)):
            with self.subTest(action=action):
                held = self.locked_reads(lambda name=name, action=action, path=path: skills_catalog.request(
                    self.db, name, action, path_name=path, root=self.repo, home=self.root, who="test"))
                self.assertGreater(len(held), 2)
                self.assertNotIn(True, held)

    def test_a_repeated_request_with_an_unreviewable_skill_still_returns_its_item(self):
        """The authorship read moved before the transaction; its refusal
        still waits until no earlier item answers the same intent."""
        first = skills_catalog.request(self.db, "sd-kept", "review", root=self.repo, home=self.root, who="test")
        with patch.object(skills_catalog, "_authorship", side_effect=workflow.WorkflowError("no attribution")):
            again = skills_catalog.request(self.db, "sd-kept", "review", root=self.repo, home=self.root, who="test")
            with self.assertRaisesRegex(workflow.WorkflowError, "no attribution"):
                skills_catalog.request(self.db, "sd-candidate", "review", root=self.repo, home=self.root, who="test")
        self.assertEqual(first["item"]["id"], again["item"]["id"])


class SkillHelpText(SkillFixture):
    """What `sd skill --help` promises for `promote` and `demote`.

    The queue design landed without its documentation: the command stopped
    opening a pull request and the help text went on offering one, so an
    operator reading `--help` was told to expect a branch that nothing was
    going to write. Correcting the words left the guards pinned to the words,
    and a reviewer's mutation survey found eight restatements of the same
    promise that a `"pull request" not in text` guard waved through: a
    comment offering "a merge request against `main`", a help string offering
    to "merge the move onto a path", a comment claiming a `git mv` and a
    commit in this checkout, "queue five code review tasks", the two help
    strings swapped so each verb advertised the other's effect,
    "pull-request" hyphenated, and deleting the comment outright.

    So these guards pin the claim rather than the phrase. Each verb's help is
    measured against a real run of that verb -- how many items it files (or,
    for `apply`, assignments it queues), and which directory the filed brief
    says the skill ends up in --
    and both the help and the comment are read for any promise of forge or
    git work, in whatever wording, with denials of that work allowed.
    """

    def skill_verbs(self) -> dict[str, str]:
        """The one-line help argparse prints for each `sd skill` verb."""
        loader = importlib.machinery.SourceFileLoader(
            "sd_cli_under_test", str(ROOT / "bin/sd")
        )
        spec = importlib.util.spec_from_file_location(
            "sd_cli_under_test", str(ROOT / "bin/sd"), loader=loader
        )
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["sd_cli_under_test"] = module
        loader.exec_module(module)
        for action in module.build_parser()._subparsers._group_actions:
            for name, parser in action.choices.items():
                if name != "skill":
                    continue
                verbs = parser._subparsers._group_actions[0]
                return {
                    choice.dest: choice.help or ""
                    for choice in verbs._choices_actions
                }
        raise AssertionError("sd has no `skill` group")

    def items(self) -> int:
        return self.db.execute("SELECT count(*) FROM item").fetchone()[0]

    def filed(self, verb: str, name: str, path: str | None = None):
        """One real run of `verb` and how many items it filed."""

        before = self.items()
        result = self.request(verb, name, path)
        return result, self.items() - before

    def filed_move(self, verb: str, name: str, source: str, path: str | None = None):
        """What one real run of `verb` files: how many, and to where; it must move from `source` (sd:1002)."""

        result, count = self.filed(verb, name, path)
        move = re.search(
            r"move (contrib|skills)/\S+ to (contrib|skills)/",
            result["item"]["body"].lower(),
        )
        self.assertIsNotNone(move, "the filed brief no longer names the move")
        assert move is not None
        self.assertEqual(move.group(1), source, f"{verb} moves from the wrong tree")
        endpoint = "contrib" if move.group(2) == "contrib" else "path"
        return "file", count, endpoint

    def parser_comment(self) -> str:
        """The comment lines inside `bin/sd`'s `sd skill` parser block."""

        source = (ROOT / "bin/sd").read_text()
        start = source.index("trying = skill.add_subparsers")
        end = source.index("scanner = trying.add_parser(")
        self.assertLess(start, end, "the `sd skill` block's anchors inverted")
        return " ".join(
            line.strip().lstrip("#").strip()
            for line in source[start:end].splitlines()
            if line.strip().startswith("#")
        )

    def test_help_matches_what_each_verb_really_files(self) -> None:
        item, notes = self.reviewed()
        observed = {
            "promote": self.filed_move("promote", "sd-candidate", "contrib", "development"),
            "demote": self.filed_move("demote", "sd-kept", "skills"),
            # Neither moves a directory, so the brief names no endpoint.
            "review": ("file", self.filed("review", "sd-candidate")[1], None),
            # Apply still queues the runner's author assignment.
            "apply": ("queue", len(self.request("apply", item=item, notes=notes)["assignments"]), None),
        }
        verbs = self.skill_verbs()
        for verb, (word, count, endpoint) in observed.items():
            with self.subTest(verb=verb):
                text = _normalised(verbs[verb])
                self.assertIn(word, text)
                self.assertEqual([], _promised(text))
                self.assertEqual(count, _stated_count(text))
                self.assertEqual(endpoint, _stated_endpoint(text))
                self.assertNotRegex(
                    text, r"\b(?:tasks|assignments|reviews|items)\b")

    def test_the_parser_comment_states_the_item_and_no_local_write(self) -> None:
        comment = self.parser_comment()
        self.assertTrue(
            comment, "the `sd skill` block has to say what promote and demote do")
        text = _normalised(comment)
        self.assertEqual([], _promised(text))
        self.assertIn("file", text)
        self.assertIn("checkout", text)
        self.assertRegex(text, _NEGATORS)


class PromiseReaderTests(unittest.TestCase):
    """sd:1002: the promise reader itself, on the strings its review found it misread."""

    def test_a_promise_to_publish_is_a_forge_claim(self) -> None:
        self.assertNotEqual([], _promised(
            "queue one code review task to move a contrib/ skill onto a path and publish the result"))

    def test_a_trailing_negation_takes_back_only_what_follows_it(self) -> None:
        # Findings 1ba1f7eaabc9 and 7279b654d6bc: "without" after the promise
        # hid the pull request in front of it.
        self.assertEqual([r"pull\s*requests?", r"\bopen(?:s|ed|ing)?\b"],
                         _promised("queue one task to open a pull request without a branch"))
        for text in ("open nothing themselves", "no one opens a pull request",
                     "never pushes a branch", "queue one task with no branch"):
            with self.subTest(text=text):
                self.assertEqual([], _promised(text))

    def test_a_contracted_denial_is_a_denial(self) -> None:
        for text in ("doesn't open a pull request", "it won't push a branch", "can’t merge anything"):
            with self.subTest(text=text):
                self.assertEqual([], _promised(text))


if __name__ == "__main__":
    unittest.main()
