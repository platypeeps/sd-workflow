"""Delivery evidence: the trailer block git reads, and who may record one.

Three guards, one mechanic. `git interpret-trailers --parse` reads a message's
*last* paragraph and nothing else, so a single blank line demotes a trailer to
prose and every reader downstream sees exactly what it would see if the author
had never written it. That is sd:5, which landed on main with its `Delivers:`
line one paragraph too high and had to be re-recorded by an empty commit.

Each guard is called directly rather than through the command that owns it,
and each has a control asserting the shape that must still pass. A test that
only asserts a refusal cannot tell a guard that fires correctly from one that
fires always, and `_delivery_reason` in particular refuses for six distinct
reasons -- a fixture that tripped an earlier one would "pass" while proving
nothing about the trailer check it was written for.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import sd_lib  # noqa: E402
import sd_work  # noqa: E402

#: The message that cost sd:5 its delivery, reduced to the line that did it.
DEMOTED = (
    "chore(work): record the delivery\n"
    "\n"
    "Body paragraph.\n"
    "\n"
    "Delivers: sd:7\n"
    "\n"
    "Co-Authored-By: Someone <nobody@example.invalid>\n"
)

#: The same message with the same two trailers, contiguous. Every assertion
#: about `DEMOTED` is paired with one about this, because the difference
#: between them is one blank line and that is the entire subject.
CONTIGUOUS = (
    "chore(work): record the delivery\n"
    "\n"
    "Body paragraph.\n"
    "\n"
    "Delivers: sd:7\n"
    "Co-Authored-By: Someone <nobody@example.invalid>\n"
)


#: What GitHub appends to a squash whose message already ends in a trailer
#: block: its co-author line, contiguously. Measured over `origin/main` at
#: a593db65: every one of the 187 squashes whose body did *not* end in a
#: trailer block got the line as a new paragraph, and both of the 2 whose
#: body did (dependabot's `Signed-off-by:`) got it joined to the block.
GITHUB_COAUTHOR = "Co-authored-by: Someone <nobody@example.invalid>\n"

#: A prose paragraph above the trailers, spelled as assistant-written bodies
#: ended before sd:3014 dropped it from the template.
ATTRIBUTION = (
    "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
    "https://claude.ai/code/session_01EXAMPLE\n"
)

#: A squashed pull request composed per `.github/PULL_REQUEST_TEMPLATE.md`
#: since sd:640: attribution paragraph, then the trailer block, then what
#: GitHub appends. The trailers stay the last paragraph.
SQUASHED_TEMPLATE = (
    "fix(thing): the change (#99)\n"
    "\n"
    "## Summary\n"
    "\n"
    "- The change.\n"
    "\n"
    + ATTRIBUTION
    + "\n"
    "Item: sd:7\n"
    "Delivers: sd:7\n"
    "Refs: sd:8\n"
    + GITHUB_COAUTHOR
)

#: The same pull request in the order the template had before sd:640, which
#: is the shape of 9c789ad3 (#887): trailers, then attribution, then GitHub's
#: line as a paragraph of its own. Four of the seven `Delivers:` merges on
#: main had this shape, and `--delivered-by` refused every one.
SQUASHED_ATTRIBUTION_LAST = (
    "fix(thing): the change (#99)\n"
    "\n"
    "## Summary\n"
    "\n"
    "- The change.\n"
    "\n"
    "Item: sd:7\n"
    "Delivers: sd:7\n"
    "Refs: sd:8\n"
    "\n"
    + ATTRIBUTION
    + "\n"
    + GITHUB_COAUTHOR
)

TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"


def git_trailers(message: str) -> set[str]:
    """The trailer names `git interpret-trailers --parse` reads out of `message`."""
    parsed = subprocess.run(
        ["git", "interpret-trailers", "--parse"], input=message,
        capture_output=True, text=True, check=True, timeout=30)
    return {line.partition(":")[0] for line in parsed.stdout.splitlines() if line}


class TrailerBlockTests(unittest.TestCase):
    """`sd_lib.trailer_block` and `sd_lib.demoted_trailers`, on their own."""

    def test_a_squash_in_the_template_order_keeps_its_trailers(self) -> None:
        """sd:640. Attribution above, trailers last, GitHub's line joined on.

        Asserted against git as well as against the two readers, because git
        is the reader whose answer decides whether the delivery exists.
        """
        self.assertEqual((), sd_lib.demoted_trailers(SQUASHED_TEMPLATE))
        block = sd_lib.trailer_block(SQUASHED_TEMPLATE).splitlines()
        self.assertIn("Delivers: sd:7", block)
        self.assertIn("Refs: sd:8", block)
        self.assertEqual({"Item", "Delivers", "Refs", "Co-authored-by"},
                         git_trailers(SQUASHED_TEMPLATE))

    def test_a_squash_with_the_attribution_last_loses_its_trailers(self) -> None:
        """The control, and the defect: the same body in the old order."""
        self.assertEqual(("Item: sd:7", "Delivers: sd:7"),
                         sd_lib.demoted_trailers(SQUASHED_ATTRIBUTION_LAST))
        self.assertNotIn("Delivers: sd:7",
                         sd_lib.trailer_block(SQUASHED_ATTRIBUTION_LAST).splitlines())
        self.assertEqual({"Co-authored-by"}, git_trailers(SQUASHED_ATTRIBUTION_LAST))

    def test_the_pull_request_template_ends_in_the_trailer_block(self) -> None:
        """The template is the shape every hand-written body starts from, so
        it is pinned here: its last paragraph is trailers and nothing else,
        none of them a line `sd-ship` owns (sd:1870), and it carries no
        attribution paragraph (sd:3014)."""
        template = TEMPLATE.read_text(encoding="utf-8")
        block = sd_lib.trailer_block(template).splitlines()
        self.assertTrue(block)
        for line in block:
            self.assertRegex(line, r"^[A-Za-z-]+: \S")
        self.assertIn("Refs:", {line.partition(" ")[0] for line in block})
        self.assertFalse({line.partition(" ")[0] for line in block} & set(sd_lib.OWNED_TRAILERS))
        self.assertNotIn("Generated with", template)
        self.assertEqual((), sd_lib.demoted_trailers(template))

    def test_a_blank_line_before_the_last_block_demotes_the_trailer(self) -> None:
        self.assertEqual(("Delivers: sd:7",), sd_lib.demoted_trailers(DEMOTED))

    def test_a_contiguous_block_demotes_nothing(self) -> None:
        """The control. Same trailers, same message, one blank line fewer."""
        self.assertEqual((), sd_lib.demoted_trailers(CONTIGUOUS))

    def test_the_block_is_the_last_paragraph_and_git_agrees(self) -> None:
        """Not asserted against a second implementation of the same slice.

        `git interpret-trailers --parse` is the reader whose answer decides
        whether a delivery exists, so the property under test is agreement
        with it and not agreement with a rule restated here.
        """
        for message, expected in ((DEMOTED, {"Co-Authored-By"}),
                                  (CONTIGUOUS, {"Delivers", "Co-Authored-By"})):
            with self.subTest(message=message.splitlines()[0]):
                parsed = subprocess.run(
                    ["git", "interpret-trailers", "--parse"], input=message,
                    capture_output=True, text=True, check=True, timeout=30)
                self.assertEqual(
                    expected,
                    {line.partition(":")[0] for line in parsed.stdout.splitlines() if line},
                )
                self.assertEqual(
                    {line.partition(":")[0] for line in parsed.stdout.splitlines() if line},
                    {line.partition(":")[0]
                     for line in sd_lib.trailer_block(message).splitlines() if line},
                )

    def test_a_quoted_trailer_in_prose_is_not_a_demoted_one(self) -> None:
        """The column-zero anchor, which is what keeps this usable here.

        This repository's commit messages discuss `Delivers:` constantly. An
        unanchored match would make every one of them a finding, and a check
        that fires on its own documentation is a check nobody leaves on.
        """
        message = (
            "docs: explain the trailer\n"
            "\n"
            "The squash should carry `Delivers: sd:7`, and an indented\n"
            "  Delivers: sd:7\n"
            "is prose about one.\n"
            "\n"
            "Item: sd:7\n"
        )
        self.assertEqual((), sd_lib.demoted_trailers(message))

    def test_a_message_that_is_only_a_trailer_block_demotes_nothing(self) -> None:
        self.assertEqual((), sd_lib.demoted_trailers("Delivers: sd:7\n"))


class DeliveryReasonTests(unittest.TestCase):
    """`sd_work._delivery_reason`: what a task's closure may name as evidence.

    A real repository, because every refusal here is a fact about git that a
    stubbed `git_output` would let the test invent. The repository has no
    remote, which is the `_verified_tip` branch that requires `main`.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name).resolve()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "file.txt").write_text("one\n", encoding="utf-8")
        self.git("add", "file.txt")
        self.git("commit", "-q", "-m", CONTIGUOUS)
        self.delivered = self.git("rev-parse", "HEAD")
        self.row = {"id": 7, "repo": str(self.root)}

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(self.root), *args],
                                capture_output=True, text=True, check=True, timeout=30)
        return result.stdout.strip()

    def test_a_contiguous_delivers_trailer_on_main_is_recorded(self) -> None:
        """The control, and the only passing shape this function has."""
        self.assertEqual(
            f"delivered at {self.delivered} on refs/heads/main",
            sd_work._delivery_reason(self.row, self.delivered),
        )

    def test_a_demoted_delivers_trailer_is_named_as_demoted(self) -> None:
        """Not "no trailer". The author wrote one; git will not read it back.

        Told apart on purpose: "carries no `Delivers:`" sends a caller to write
        a line that is already there, which is how sd:5 needed a second commit
        to say what the first one said.
        """
        (self.root / "file.txt").write_text("two\n", encoding="utf-8")
        self.git("commit", "-q", "-a", "-m", DEMOTED)
        commit = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(sd_work.WorkRefusal, "outside the trailer block"):
            sd_work._delivery_reason(self.row, commit)

    def test_an_item_only_merge_is_sent_to_the_after_the_fact_repair(self) -> None:
        """sd:1913. `Item:` where `Delivers:` was meant is an associate-only
        merge; the refusal names the one verb that can deliver it."""
        (self.root / "file.txt").write_text("four\n", encoding="utf-8")
        self.git("commit", "-q", "-a", "-m", "feat: the whole item\n\nItem: sd:7\n")
        commit = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(sd_work.WorkRefusal,
                                    rf"carries no `Delivers: sd:7`.*sd work deliver 7 {commit} --associated --reason"):
            sd_work._delivery_reason(self.row, commit)

    def test_a_commit_naming_neither_trailer_is_sent_to_the_ways_out(self) -> None:
        """sd:2565. A no-item squash carries neither `Delivers:` nor `Item:`
        (sd:2171), and the refusal named no way out of it."""
        (self.root / "file.txt").write_text("six\n", encoding="utf-8")
        self.git("commit", "-q", "-a", "-m", "feat: shipped with no item\n\nAuthored-with: human\n")
        commit = self.git("rev-parse", "HEAD")
        with self.assertRaises(sd_work.WorkRefusal) as caught:
            sd_work._delivery_reason(self.row, commit)
        for way_out in ("--item 7 --deliver", "`Closes: sd:7`", "--reason TEXT"):
            with self.subTest(way_out=way_out):
                self.assertIn(way_out, str(caught.exception))

    def test_a_commit_with_no_delivers_trailer_is_refused(self) -> None:
        (self.root / "file.txt").write_text("three\n", encoding="utf-8")
        self.git("commit", "-q", "-a", "-m", "chore: no trailer at all\n")
        commit = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(sd_work.WorkRefusal, "carries no `Delivers: sd:7`"):
            sd_work._delivery_reason(self.row, commit)

    def test_two_delivers_trailers_on_one_commit_are_read_one_per_line(self) -> None:
        """e6c2cb20 (#869) carries `Delivers: sd:580` and `Delivers: sd:572`.

        `git log --format=%(trailers:key=Delivers,valueonly)` prints those as
        `sd:580sd:572` when the caller drops the newlines, and a reader that
        joined the values would hold one id naming nothing (sd:640, note
        #1225). Every reader in `bin/` takes the block a line at a time; this
        pins that for the one a task's closure goes through, and for the one
        `sd work deliver` and `sd-status` share.
        """
        (self.root / "file.txt").write_text("five\n", encoding="utf-8")
        message = (
            "fix(gates): two gates in one squash\n"
            "\n"
            "Delivers: sd:7\n"
            "Delivers: sd:9\n"
            "Co-Authored-By: Someone <nobody@example.invalid>\n"
        )
        self.git("commit", "-q", "-a", "-m", message)
        commit = self.git("rev-parse", "HEAD")
        for item in (7, 9):
            with self.subTest(item=item):
                self.assertEqual(
                    f"delivered at {commit} on refs/heads/main",
                    sd_work._delivery_reason({"id": item, "repo": str(self.root)}, commit),
                )
                self.assertTrue(sd_lib._closes(message, f"sd:{item}"))
        self.assertFalse(sd_lib._closes(message, "sd:7sd:9"))
        with self.assertRaisesRegex(sd_work.WorkRefusal, "carries no `Delivers: sd:8`"):
            sd_work._delivery_reason({"id": 8, "repo": str(self.root)}, commit)

    def test_a_delivers_trailer_for_another_item_is_refused(self) -> None:
        """The row's own id, never any `Delivers:` line the commit happens to
        carry -- a batched squash names several and closes exactly one."""
        with self.assertRaisesRegex(sd_work.WorkRefusal, "carries no `Delivers: sd:8`"):
            sd_work._delivery_reason({"id": 8, "repo": str(self.root)}, self.delivered)

    def test_a_commit_off_the_default_branch_is_not_reachable(self) -> None:
        self.git("checkout", "-q", "-b", "side")
        (self.root / "file.txt").write_text("four\n", encoding="utf-8")
        self.git("commit", "-q", "-a", "-m", CONTIGUOUS)
        commit = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "main")
        with self.assertRaisesRegex(sd_work.WorkRefusal, "not reachable from refs/heads/main"):
            sd_work._delivery_reason(self.row, commit)

    def test_an_abbreviated_commit_is_refused(self) -> None:
        with self.assertRaisesRegex(sd_work.WorkRefusal, "full lowercase commit ID"):
            sd_work._delivery_reason(self.row, self.delivered[:12])
        # sd:5065: the refusal names the command that prints the full ID.
        with self.assertRaisesRegex(sd_work.WorkRefusal, f"`git rev-parse {self.delivered[:12]}`"):
            sd_work._delivery_reason(self.row, self.delivered[:12])

    def test_a_local_only_delivery_off_main_names_the_switch(self) -> None:
        """sd:5065: the refusal names the command that fixes it."""
        self.git("checkout", "-q", "-b", "side")
        with self.assertRaisesRegex(sd_work.WorkRefusal, r"`git -C \S+ switch main`"):
            sd_work._delivery_reason(self.row, self.delivered)

    def test_a_task_belonging_to_no_checkout_has_nothing_to_verify(self) -> None:
        with self.assertRaisesRegex(sd_work.WorkRefusal, "belongs to no checkout"):
            sd_work._delivery_reason({"id": 7, "repo": None}, self.delivered)

    def test_a_commit_in_another_checkout_is_verified_there(self) -> None:
        """sd:1569. #1304 was filed in one repository and fixed in another,
        and the row's own checkout "has no commit" for the SHA that fixed it.
        Named with `checkout`, the SHA is verified in that repository and the
        sentence says where, because `origin/main` alone would name the
        row's branch, not the one that carries the commit."""
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        other = pathlib.Path(elsewhere.name).resolve()
        subprocess.run(["git", "-C", str(other), "init", "-q", "-b", "main"],
                       check=True, timeout=30)
        row = {"id": 7, "repo": str(other)}
        with self.assertRaisesRegex(sd_work.WorkRefusal, "--delivered-in"):
            sd_work._delivery_reason(row, self.delivered)
        self.assertEqual(
            f"delivered at {self.delivered} on refs/heads/main in {self.root}",
            sd_work._delivery_reason(row, self.delivered, str(self.root)),
        )
        # The row's own checkout, named explicitly, is the same sentence as
        # naming none: nothing downstream reads a second spelling for it.
        self.assertEqual(
            f"delivered at {self.delivered} on refs/heads/main",
            sd_work._delivery_reason(self.row, self.delivered, str(self.root)),
        )

    def test_a_task_with_no_checkout_is_verified_where_it_was_delivered(self) -> None:
        self.assertEqual(
            f"delivered at {self.delivered} on refs/heads/main in {self.root}",
            sd_work._delivery_reason({"id": 7, "repo": None}, self.delivered,
                                     str(self.root)),
        )


class TaskDeliveryCLITests(unittest.TestCase):
    """The whole of sd:591, through the real `sd` binary on a scratch database.

    `sd work deliver` refuses a `kind=task` row, so before this the only way
    to close one was `sd task status done`, which recorded who and when and
    nothing about what shipped it. Four items closed on 2026-09-12 lost their
    SHA that way. The evidence now goes on the transition, in the sentence
    `sd_db.progress` writes for a work item, so one query over the status
    history answers "what delivered this" for either kind.
    """

    def host(self):
        """One `TaskCLI` fixture: a scratch HOME and its own sd database."""
        from tests.test_sd_work import TaskCLI

        class Case(TaskCLI):
            def runTest(self) -> None:  # pragma: no cover - fixture host only
                pass

        case = Case()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def repository(self, case):
        """A registered checkout the scratch database will accept a task in."""
        import sd_db
        import sd_db.repos

        root = (case.home / "checkout").resolve()
        root.mkdir()
        for command in (("init", "-q", "-b", "main"), ("config", "user.name", "Fixture"),
                        ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(root), *command], check=True, timeout=30)
        with sd_db.connect(sd_db.default_path(case.home), write=True) as connection:
            sd_db.repos.upsert_repo(connection, sd_lib.stored_repo(root), managed=1)
        return root

    def commit(self, root: pathlib.Path, message: str, content: str) -> str:
        (root / "file.txt").write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True, timeout=30)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", message],
                       check=True, timeout=30)
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True,
                              timeout=30).stdout.strip()

    def statuses(self, case, item: int) -> list[str]:
        """The item's status-change history, which is where the evidence lands."""
        import sd_db

        with sd_db.connect(sd_db.default_path(case.home), write=False) as connection:
            return [row["body"] for row in connection.execute(
                "SELECT body FROM note WHERE item = ? AND kind = 'status_change' ORDER BY id",
                (item,))]

    def test_a_task_closes_carrying_the_commit_that_delivered_it(self) -> None:
        case = self.host()
        root = self.repository(case)
        state = json.loads(case.call("task", "add", "--priority", "3", "Fix the thing", "--json",
                                     cwd=root).stdout)
        item = state["item"]["id"]
        sha = self.commit(root, f"fix: the thing\n\nDelivers: sd:{item}\n", "one\n")

        done = json.loads(case.call("task", "status", item, "done",
                                    "--delivered-by", sha, "--json", cwd=root).stdout)
        self.assertEqual("done", done["item"]["status"])
        self.assertIn(f"delivered at {sha} on refs/heads/main", self.statuses(case, item)[-1])

    def test_a_delivered_close_keeps_a_persons_followup_open(self) -> None:
        """sd:10430: a `--delivered-by` close is a delivery: it keeps a person's followup open
        and owed, as `sd work deliver` does, and `--resolve-followups` still resolves it."""
        case = self.host()
        root = self.repository(case)
        items = []
        for title in ("Keep the followup", "Resolve the followup"):
            item = json.loads(case.call("task", "add", "--priority", "3", title, "--json",
                                        cwd=root).stdout)["item"]["id"]
            note = json.loads(case.call("task", "note", item, "Ask example.test", "--kind",
                                        "followup", "--json", cwd=root).stdout)["note"]["id"]
            sha = self.commit(root, f"fix: {title}\n\nDelivers: sd:{item}\n", title)
            items.append((item, note, sha))
        (kept, kept_note, kept_sha), (resolved, resolved_note, resolved_sha) = items
        done = json.loads(case.call("task", "status", kept, "done", "--delivered-by", kept_sha,
                                    "--json", cwd=root).stdout)
        self.assertEqual("done", done["item"]["status"])
        self.assertIsNone(next(n for n in done["notes"] if n["id"] == kept_note)["resolved_at"])
        self.assertEqual([kept_note], json.loads(done["item"]["fields"])["kept_followups"])
        done = json.loads(case.call("task", "status", resolved, "done", "--delivered-by",
                                    resolved_sha, "--resolve-followups", "--json", cwd=root).stdout)
        self.assertTrue(next(n for n in done["notes"] if n["id"] == resolved_note)["resolved_at"])

    def test_a_plain_close_records_no_commit_which_is_the_gap(self) -> None:
        """The control, and the state the flag exists to leave behind.

        Closing without `--delivered-by` still works and still records nothing
        about delivery. Asserted so a later change that started inventing
        evidence for an unverified close would fail here rather than pass
        quietly as an improvement.
        """
        case = self.host()
        root = self.repository(case)
        state = json.loads(case.call("task", "add", "--priority", "3", "Fix the thing", "--json",
                                     cwd=root).stdout)
        item = state["item"]["id"]
        case.call("task", "status", item, "done", cwd=root)
        self.assertNotIn("delivered at", self.statuses(case, item)[-1])

    def on_branch(self, case, root: pathlib.Path, branch: str) -> int:
        """A task worked on `branch`, as the build records it."""
        import sd_db

        item = json.loads(case.call("task", "add", "--priority", "3", "Fix the thing", "--json", cwd=root).stdout)["item"]["id"]
        with sd_db.connect(sd_db.default_path(case.home), write=True) as connection:
            connection.execute("UPDATE item SET branch = ? WHERE id = ?", (branch, item))
        return item

    def status(self, case, root: pathlib.Path, item: int) -> str:
        return json.loads(case.call("store", "item", item, "--json", cwd=root).stdout)["item"]["status"]

    def test_a_row_on_its_own_branch_with_no_merge_recorded_stays_open(self) -> None:
        """sd:1990: three fleet items closed this way while their branch had no
        pull request, and one never reached main."""
        case = self.host()
        root = self.repository(case)
        item = self.on_branch(case, root, "fleet/fixture-sd1")
        refused = case.call("task", "status", item, "done", code=1, cwd=root)
        self.assertIn("worked on branch fleet/fixture-sd1, and no merge of it is recorded", refused.stderr)
        self.assertEqual("planning", self.status(case, root, item))

    def test_a_row_on_its_own_branch_closes_with_a_reason_and_records_it(self) -> None:
        case = self.host()
        root = self.repository(case)
        item = self.on_branch(case, root, "fleet/fixture-sd1")
        case.call("task", "status", item, "done", "--reason", "superseded by another merge", cwd=root)
        self.assertEqual("done", self.status(case, root, item))
        self.assertIn("superseded by another merge", self.statuses(case, item)[-1])

    def test_a_row_on_its_own_branch_closes_with_its_merge(self) -> None:
        """Named on the close, or already recorded by `sd-ship`'s comment."""
        case = self.host()
        root = self.repository(case)
        item = self.on_branch(case, root, "fleet/fixture-sd1")
        sha = self.commit(root, f"fix: the thing\n\nDelivers: sd:{item}\n", "one\n")
        case.call("task", "status", item, "done", "--delivered-by", sha, cwd=root)
        self.assertEqual("done", self.status(case, root, item))
        merged = self.on_branch(case, root, "fleet/fixture-sd2")
        case.call("task", "note", merged, "--body",
                  f"Code delivery https://github.example.test/o/r/pull/7 at {sha}", cwd=root)
        case.call("task", "status", merged, "done", cwd=root)
        self.assertEqual("done", self.status(case, root, merged))

    def test_a_row_on_the_default_branch_still_closes_plainly(self) -> None:
        """The control: a row with no branch of its own has nothing to land."""
        case = self.host()
        root = self.repository(case)
        for branch in ("main", "origin/master"):
            with self.subTest(branch=branch):
                item = self.on_branch(case, root, branch)
                case.call("task", "status", item, "done", cwd=root)
                self.assertEqual("done", self.status(case, root, item))

    def test_an_unverifiable_commit_leaves_the_task_open(self) -> None:
        """Refused before the status moves, not after: a task closed and then
        found to have no evidence is the hole, not a smaller version of it."""
        case = self.host()
        root = self.repository(case)
        state = json.loads(case.call("task", "add", "--priority", "3", "Fix the thing", "--json",
                                     cwd=root).stdout)
        item = state["item"]["id"]
        sha = self.commit(root, "fix: the thing, with no trailer\n", "one\n")

        refused = case.call("task", "status", item, "done", "--delivered-by", sha,
                            code=1, cwd=root)
        self.assertIn("carries no `Delivers:", refused.stderr)
        readback = json.loads(case.call("store", "item", item, "--json", cwd=root).stdout)
        self.assertEqual("planning", readback["item"]["status"])

    def second_repository(self, case, name: str, register: bool = True) -> pathlib.Path:
        """Another checkout on the same scratch database, registered or not."""
        import sd_db
        import sd_db.repos

        root = (case.home / name).resolve()
        root.mkdir()
        for command in (("init", "-q", "-b", "main"), ("config", "user.name", "Fixture"),
                        ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(root), *command], check=True, timeout=30)
        if register:
            with sd_db.connect(sd_db.default_path(case.home), write=True) as connection:
                sd_db.repos.upsert_repo(connection, sd_lib.stored_repo(root), managed=1)
        return root

    def test_a_fix_shipped_in_another_repository_closes_the_task_where_it_was_filed(
            self) -> None:
        """sd:1569, the #1304 shape: filed in one checkout, delivered by a
        commit in another. Without `--delivered-in` the row's checkout has no
        such commit; with it the commit is verified there, the task closes,
        and the row keeps the checkout it was filed in."""
        case = self.host()
        filed = self.repository(case)
        shipped = self.second_repository(case, "pack")
        state = json.loads(case.call("task", "add", "--priority", "3", "Fix the thing", "--json",
                                     cwd=filed).stdout)
        item = state["item"]["id"]
        sha = self.commit(shipped, f"fix: the thing\n\nDelivers: sd:{item}\n", "one\n")

        refused = case.call("task", "status", item, "done", "--delivered-by", sha,
                            code=1, cwd=shipped)
        self.assertIn(f"has no commit {sha}", refused.stderr)
        self.assertIn("--delivered-in", refused.stderr)

        done = json.loads(case.call("task", "status", item, "done", "--delivered-by", sha,
                                    "--delivered-in", str(shipped), "--json",
                                    cwd=filed).stdout)
        self.assertEqual("done", done["item"]["status"])
        self.assertEqual(state["item"]["repo"], done["item"]["repo"])
        self.assertIn(f"delivered at {sha} on refs/heads/main in "
                      f"{sd_lib.stored_repo(shipped)}", self.statuses(case, item)[-1])

    def test_delivered_in_names_only_a_registered_checkout(self) -> None:
        case = self.host()
        filed = self.repository(case)
        stray = self.second_repository(case, "stray", register=False)
        item = json.loads(case.call("task", "add", "--priority", "3", "Fix the thing", "--json",
                                    cwd=filed).stdout)["item"]["id"]
        sha = self.commit(stray, f"fix: the thing\n\nDelivers: sd:{item}\n", "one\n")

        refused = case.call("task", "status", item, "done", "--delivered-by", sha,
                            "--delivered-in", str(stray), code=1, cwd=filed)
        self.assertIn("is not a registered repository", refused.stderr)
        readback = json.loads(case.call("store", "item", item, "--json", cwd=filed).stdout)
        self.assertEqual("planning", readback["item"]["status"])

    def test_delivered_in_needs_delivered_by(self) -> None:
        case = self.host()
        filed = self.repository(case)
        item = json.loads(case.call("task", "add", "--priority", "3", "Fix the thing", "--json",
                                    cwd=filed).stdout)["item"]["id"]
        refused = case.call("task", "status", item, "done", "--delivered-in", str(filed),
                            code=1, cwd=filed)
        self.assertIn("--delivered-in names where --delivered-by", refused.stderr)

    def test_work_deliver_on_a_task_names_the_flag_that_records_it(self) -> None:
        """The refusal sd:591 was filed against, with the remedy in it.

        The library's own sentence -- "this operation is for work items" --
        leaves a caller holding a real merged SHA with nowhere to put it, and
        the workaround it produced was a hand-written note.
        """
        case = self.host()
        root = self.repository(case)
        state = json.loads(case.call("task", "add", "--priority", "3", "Fix the thing", "--json",
                                     cwd=root).stdout)
        item = state["item"]["id"]
        sha = self.commit(root, f"fix: the thing\n\nDelivers: sd:{item}\n", "one\n")

        refused = case.call("work", "deliver", item, sha, code=1, cwd=root)
        self.assertIn(f"--delivered-by {sha}", refused.stderr)
        self.assertIn("ordinary task", refused.stderr)

    def test_a_work_item_is_sent_to_work_deliver(self) -> None:
        """sd:772's control. A work item has a checkout and its own delivery
        verb, so the refusal keeps naming that verb and leaves the row open."""
        import sd_db
        import sd_db.writes

        case = self.host()
        root = self.repository(case)
        state = json.loads(case.call("task", "add", "--priority", "3", "Ship the feature", "--json",
                                     cwd=root).stdout)
        item = state["item"]["id"]
        with sd_db.connect(sd_db.default_path(case.home), write=True) as connection:
            sd_db.writes.set_item_fields(connection, item, kind="work")
            connection.commit()
        sha = self.commit(root, f"feat: the feature\n\nDelivers: sd:{item}\n", "one\n")

        refused = case.call("task", "status", item, "done", "--delivered-by", sha,
                            code=1, cwd=root)
        self.assertIn(f"item {item} is a work item; `sd work deliver {item} {sha}` "
                      "records its delivery", refused.stderr)
        readback = json.loads(case.call("store", "item", item, "--json", cwd=root).stdout)
        self.assertEqual("planning", readback["item"]["status"])

    def test_a_repository_less_item_says_there_is_nothing_to_verify(self) -> None:
        """sd:772. A followup or personal item takes task statuses since sd:768.
        Both rows here are filed off every checkout, so neither belongs to one
        and no commit can be verified for either. A followup that does carry a
        checkout (sd:809) is refused for the other reason, which
        `test_a_followup_with_a_checkout_still_takes_no_delivery_evidence`
        pins. The refusal stands; it names the kind and the reason, not
        `sd work deliver`, which refuses the same row one call later.

        The close hint follows the installed library, whichever build that is.
        A build from before `TASK_STATUS_KINDS` refuses `done` for these kinds,
        and a hint telling the caller to do what that build refuses would be
        the same wrong direction in a new place. CI carried such a build until
        sd:809 moved the pin: the symbol is absent at `dc03956e` and present at
        `workflow.py:83` in `09260ad4`, the pin sd:809 set, so CI has taken
        the closing branch since, as a developer venv already did. The test reads `hasattr`
        rather than the pin, so both branches stay live: where the build closes
        them the hint is asserted and the close is carried out; where it does
        not, the hint is asserted absent."""
        import sd_db.workflow as workflow

        closes = hasattr(workflow, "TASK_STATUS_KINDS")
        for kind in ("followup",):
            with self.subTest(kind=kind, closes=closes):
                case = self.host()
                state = json.loads(case.call("task", "add", "--priority", "3", f"A {kind}", "--kind", kind,
                                             "--json").stdout)
                item = state["item"]["id"]
                self.assertIsNone(state["item"]["repo"])
                sha = "0" * 40

                refused = case.call("task", "status", item, "done", "--delivered-by",
                                    sha, code=1)
                reason = (f"{kind} item {item} belongs to no checkout, so --delivered-by "
                          "has nothing to verify")
                hint = "; close it without --delivered-by"
                self.assertIn(reason + (hint if closes else "\n"), refused.stderr)
                self.assertNotIn("work item", refused.stderr)
                self.assertNotIn("sd work deliver", refused.stderr)
                readback = json.loads(case.call("store", "item", item, "--json").stdout)
                self.assertEqual("planning", readback["item"]["status"])
                if closes:
                    closed = json.loads(case.call("task", "status", item, "done",
                                                  "--json").stdout)
                    self.assertEqual("done", closed["item"]["status"])

    def test_a_followup_with_a_checkout_still_takes_no_delivery_evidence(self) -> None:
        """sd:809. A followup filed in a checkout now carries it, so it no longer
        reads "belongs to no checkout". It still is not a task, and only a
        task's move to done records a delivering commit, so the refusal is that
        one and the row does not move."""
        case = self.host()
        root = self.repository(case)
        state = json.loads(case.call("task", "add", "--priority", "3", "A review finding", "--kind", "followup",
                                     "--json", cwd=root).stdout)
        item = state["item"]["id"]
        self.assertEqual(state["item"]["repo"], sd_lib.stored_repo(root))

        refused = case.call("task", "status", item, "done", "--delivered-by", "0" * 40,
                            code=1, cwd=root)
        self.assertIn(f"followup item {item} takes no --delivered-by; only a task's "
                      "move to done records one", refused.stderr)
        self.assertNotIn("belongs to no checkout", refused.stderr)
        readback = json.loads(case.call("store", "item", item, "--json", cwd=root).stdout)
        self.assertEqual(readback, state)

    def test_the_readme_states_the_reason_each_kind_is_actually_refused(self) -> None:
        """sd:809. The README's delivery paragraph answers "why was my
        `--delivered-by` refused", and it named one reason for both kinds:
        a followup, like a personal item, belongs to no checkout. A followup
        filed in a registered checkout carries one, and is refused for the
        other reason instead, so the paragraph owed the reader two.

        This reads the reasons off the CLI rather than off the README, and
        asks the README to carry both. The clauses come out of the two
        refusals `_delivery_row` raises, sliced at the punctuation the
        messages already use, so a reworded refusal makes the documentation
        follow the code rather than making this test a transcription of
        either. What it pins is that both reasons are stated and that each is
        attached to the row that gets it; what it cannot pin is a false
        sentence added elsewhere in the same paragraph."""
        case = self.host()
        root = self.repository(case)

        unscoped = json.loads(case.call("task", "add", "--priority", "3", "A loose end", "--kind", "followup",
                                        "--json").stdout)["item"]["id"]
        scoped = json.loads(case.call("task", "add", "--priority", "3", "A review finding", "--kind", "followup",
                                      "--json", cwd=root).stdout)["item"]["id"]
        sha = "0" * 40
        no_checkout = case.call("task", "status", unscoped, "done", "--delivered-by", sha,
                                code=1).stderr
        not_a_task = case.call("task", "status", scoped, "done", "--delivered-by", sha,
                               code=1, cwd=root).stderr

        # "sd: followup item 1 belongs to no checkout, so --delivered-by has
        # nothing to verify; ..." -> "belongs to no checkout"
        first = no_checkout.split(f"item {unscoped} ", 1)[1].split(", so ", 1)[0]
        # "sd: followup item 2 takes no --delivered-by; only a task's move to
        # done records one" -> "only a task's move to done records"
        second = not_a_task.split("; ", 1)[1].rsplit(" one", 1)[0]
        self.assertEqual("belongs to no checkout", first)

        readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
        paragraph = readme.split("A task a commit delivered closes with that commit named", 1)[1]
        paragraph = paragraph.split("`sd store items --open`", 1)[0]
        for clause, item in ((first, unscoped), (second, scoped)):
            with self.subTest(clause=clause):
                self.assertIn(clause, paragraph,
                              f"the README does not state why item {item} was refused")
        self.assertIn("followup", paragraph)

    def test_the_close_hint_follows_the_library_that_would_close_it(self) -> None:
        """Both library shapes, on whichever build is installed. A stand-in
        `workflow` is enough: `_status_reason` reads `item_state` and the
        kind list and nothing else before it refuses."""
        import argparse
        import types

        row = {"id": 5, "kind": "followup", "repo": None}
        args = argparse.Namespace(item=5, status="done", delivered_by="0" * 40, reason=None)
        for kinds, hinted in (((), False), (("task", "followup"), True)):
            with self.subTest(task_status_kinds=kinds):
                workflow = types.SimpleNamespace(item_state=lambda _c, _i: {"item": row})
                if kinds:
                    workflow.TASK_STATUS_KINDS = kinds
                with self.assertRaises(sd_work.WorkRefusal) as refused:
                    sd_work._status_reason(workflow, None, args)
                self.assertEqual(hinted, str(refused.exception).endswith(
                    "; close it without --delivered-by"))

    def test_the_other_kinds_are_not_called_work_items_either(self) -> None:
        """The two branches sd:772 adds beside the followup one. A repo-less
        idea closes by hand since pack sd:9302, so it is told to close without
        the flag, and a kind with a checkout but no task closure is told only
        tasks take it."""
        import sd_db
        import sd_db.writes

        case = self.host()
        idea = json.loads(case.call("task", "add", "An idea", "--kind", "work-idea",
                                    "--json").stdout)["item"]["id"]
        refused = case.call("task", "status", idea, "done", "--delivered-by", "0" * 40,
                            code=1)
        self.assertIn(f"work-idea item {idea} belongs to no checkout, so --delivered-by "
                      "has nothing to verify; close it without --delivered-by\n", refused.stderr)

        root = self.repository(case)
        report = json.loads(case.call("task", "add", "--priority", "3", "A report", "--json",
                                      cwd=root).stdout)["item"]["id"]
        with sd_db.connect(sd_db.default_path(case.home), write=True) as connection:
            sd_db.writes.set_item_fields(connection, report, kind="report")
            connection.commit()
        refused = case.call("task", "status", report, "done", "--delivered-by", "0" * 40,
                            code=1, cwd=root)
        self.assertIn(f"report item {report} takes no --delivered-by; only a task's "
                      "move to done records one", refused.stderr)


class AssociatedDeliveryCLITests(unittest.TestCase):
    """sd:1590: `sd work deliver --associated --reason` closes a work item whose
    whole-item merge carried `Item:` where `Delivers:` was meant.

    Seen on system sd:1577 (#592): prepared without `--deliver`, so the squash
    named the item and delivered nothing, and every verb that could close the
    row refused it. The fixture is `TaskDeliveryCLITests`'s, borrowed rather
    than inherited so its tests do not run twice.
    """

    REASON = "prepared without --deliver; the merge is the whole item"

    host = TaskDeliveryCLITests.host
    repository = TaskDeliveryCLITests.repository
    commit = TaskDeliveryCLITests.commit
    statuses = TaskDeliveryCLITests.statuses

    def work_item(self, case, root: pathlib.Path) -> int:
        """A `kind=work` row in a row-status checkout, as `deliver_work` requires."""
        import sd_db
        import sd_db.writes

        item = json.loads(case.call("task", "add", "--priority", "3", "Ship the feature", "--json",
                                    cwd=root).stdout)["item"]["id"]
        with sd_db.connect(sd_db.default_path(case.home), write=True) as connection:
            sd_db.writes.set_item_fields(connection, item, kind="work")
            connection.commit()
        return item

    def status(self, case, root: pathlib.Path, item: int) -> str:
        return json.loads(case.call("store", "item", item, "--json",
                                    cwd=root).stdout)["item"]["status"]

    def test_an_item_merge_closes_only_through_associated_with_a_reason(self) -> None:
        case = self.host()
        root = self.repository(case)
        item = self.work_item(case, root)
        sha = self.commit(root, f"feat: the feature (#1)\n\nItem: sd:{item}\n", "one\n")

        refused = case.call("work", "deliver", item, sha, code=1, cwd=root)
        self.assertIn("no Delivers trailer", refused.stderr)
        self.assertEqual("planning", self.status(case, root, item))

        done = json.loads(case.call("work", "deliver", item, sha, "--associated",
                                    "--reason", self.REASON, "--json", cwd=root).stdout)
        self.assertEqual("done", done["item"]["status"])
        fields = done["item"]["fields"]
        completion = (json.loads(fields) if isinstance(fields, str) else fields)["completion"]
        self.assertEqual(("delivered", sha, "Item", self.REASON),
                         (completion["outcome"], completion["commit"],
                          completion["trailer"], completion["after_the_fact"]))
        self.assertIn(f"delivered at {sha} on refs/heads/main", self.statuses(case, item)[-1])

    def test_associated_without_a_reason_refuses_and_leaves_the_row_open(self) -> None:
        case = self.host()
        root = self.repository(case)
        item = self.work_item(case, root)
        sha = self.commit(root, f"feat: the feature (#1)\n\nItem: sd:{item}\n", "one\n")
        for extra in ((), ("--reason", "  ")):
            with self.subTest(extra=extra):
                refused = case.call("work", "deliver", item, sha, "--associated", *extra,
                                    code=1, cwd=root)
                self.assertIn("--associated needs --reason", refused.stderr)
                self.assertIn(f"`sd work deliver {item} {sha} --associated --reason TEXT`", refused.stderr)
        self.assertEqual("planning", self.status(case, root, item))

    def test_a_reason_without_associated_is_refused(self) -> None:
        """Otherwise `--reason` on an ordinary delivery would be dropped unread."""
        case = self.host()
        root = self.repository(case)
        item = self.work_item(case, root)
        sha = self.commit(root, f"feat: the feature\n\nDelivers: sd:{item}\n", "one\n")
        refused = case.call("work", "deliver", item, sha, "--reason", self.REASON,
                            code=1, cwd=root)
        self.assertIn("--reason belongs to --associated", refused.stderr)
        self.assertEqual("planning", self.status(case, root, item))

    def test_associated_still_needs_the_item_trailer(self) -> None:
        """The control: `--associated` accepts the `Item:` trailer and no other."""
        case = self.host()
        root = self.repository(case)
        item = self.work_item(case, root)
        sha = self.commit(root, f"feat: the feature\n\nRefs: sd:{item}\n", "one\n")
        refused = case.call("work", "deliver", item, sha, "--associated",
                            "--reason", self.REASON, code=1, cwd=root)
        self.assertIn(f"no Item trailer for sd:{item}", refused.stderr)
        self.assertEqual("planning", self.status(case, root, item))

    def test_associated_closes_a_task_with_the_delivery_sentence_and_reason(self) -> None:
        """sd:1913. A task has no work receipt; its `Item:` merge is verified and
        the move to done records the delivery sentence and the reason."""
        case = self.host()
        root = self.repository(case)
        item = json.loads(case.call("task", "add", "--priority", "3", "Fix the thing", "--json",
                                    cwd=root).stdout)["item"]["id"]
        sha = self.commit(root, f"fix: the thing\n\nItem: sd:{item}\n", "one\n")
        done = json.loads(case.call("work", "deliver", item, sha, "--associated",
                                    "--reason", self.REASON, "--json", cwd=root).stdout)
        self.assertEqual("done", done["item"]["status"])
        self.assertIn(f"delivered at {sha} on refs/heads/main (after the fact: {self.REASON})",
                      self.statuses(case, item)[-1])

    def test_associated_closes_a_followup_the_same_way(self) -> None:
        import sd_db
        import sd_db.writes

        case = self.host()
        root = self.repository(case)
        item = json.loads(case.call("task", "add", "--priority", "3", "Follow the thing up", "--json",
                                    cwd=root).stdout)["item"]["id"]
        with sd_db.connect(sd_db.default_path(case.home), write=True) as connection:
            sd_db.writes.set_item_fields(connection, item, kind="followup")
            connection.commit()
        sha = self.commit(root, f"fix: the follow-up\n\nItem: sd:{item}\n", "one\n")
        case.call("work", "deliver", item, sha, "--associated", "--reason", self.REASON, cwd=root)
        self.assertEqual("done", self.status(case, root, item))
        self.assertIn(f"delivered at {sha} on refs/heads/main (after the fact: {self.REASON})",
                      self.statuses(case, item)[-1])

    def followup(self, case, root: pathlib.Path) -> int:
        import sd_db
        import sd_db.writes

        item = json.loads(case.call("task", "add", "--priority", "3", "Follow the thing up", "--json",
                                    cwd=root).stdout)["item"]["id"]
        with sd_db.connect(sd_db.default_path(case.home), write=True) as connection:
            sd_db.writes.set_item_fields(connection, item, kind="followup")
            connection.commit()
        return item

    def test_associated_on_a_done_followup_refuses_rather_than_record_nothing(self) -> None:
        """sd:3346. The move to done is the receipt, and a row already done does
        not move: the command exited 0 and wrote nothing, so `sd-status` went on
        calling the item unmarked. The refusal names the way to a receipt."""
        case = self.host()
        root = self.repository(case)
        item = self.followup(case, root)
        sha = self.commit(root, f"fix: the follow-up\n\nItem: sd:{item}\n", "one\n")
        case.call("task", "status", item, "done", cwd=root)
        refused = case.call("work", "deliver", item, sha, "--associated", "--reason", self.REASON,
                            code=1, cwd=root)
        self.assertIn(f"sd task status {item} planning", refused.stderr)
        self.assertNotIn("delivered at", self.statuses(case, item)[-1])

    def test_associated_again_on_its_own_receipt_is_a_no_op(self) -> None:
        """The control: a second run naming the commit the receipt names succeeds."""
        case = self.host()
        root = self.repository(case)
        item = self.followup(case, root)
        sha = self.commit(root, f"fix: the follow-up\n\nItem: sd:{item}\n", "one\n")
        for _ in range(2):
            case.call("work", "deliver", item, sha, "--associated", "--reason", self.REASON, cwd=root)
        self.assertEqual(1, sum("delivered at" in line for line in self.statuses(case, item)))

    def test_associated_on_a_task_still_needs_the_item_trailer(self) -> None:
        case = self.host()
        root = self.repository(case)
        item = json.loads(case.call("task", "add", "--priority", "3", "Fix the thing", "--json",
                                    cwd=root).stdout)["item"]["id"]
        sha = self.commit(root, f"fix: the thing\n\nRefs: sd:{item}\n", "one\n")
        refused = case.call("work", "deliver", item, sha, "--associated",
                            "--reason", self.REASON, code=1, cwd=root)
        self.assertIn(f"carries no `Item: sd:{item}` trailer", refused.stderr)
        self.assertEqual("planning", self.status(case, root, item))


class ShipMergeGuardTests(unittest.TestCase):
    """`sd-ship merge` refuses to dispatch a squash that demotes its own trailer.

    The one moment the pack composes a message that git will parse later, so
    this reaches the real composition through the real merge path rather than
    asserting about a string. `tests.test_sd_ship` owns the fixture; it is
    hosted here rather than subclassed so this module collects its own tests
    and not that file's forty.
    """

    def host(self):
        """One `ShipCase` fixture, set up and torn down with this test."""
        from tests.test_sd_ship import ShipCase

        class Case(ShipCase):
            def runTest(self) -> None:  # pragma: no cover - fixture host only
                pass

        case = Case()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def merge_operation(self, case):
        from tests.test_sd_ship import _git

        case.prepare()
        return case.operation("merge", "--manual", "--expected-head",
                              _git(case.root, "rev-parse", "HEAD"))

    def test_a_well_formed_squash_message_still_merges(self) -> None:
        """The control. Without it, a guard that refused every merge passes."""
        self.assertEqual("merged", self.merge_operation(self.host()).merge()["phase"])

    def test_a_body_ending_in_a_trailer_line_is_refused_before_dispatch(self) -> None:
        from tests.test_sd_ship import ship

        case = self.host()
        operation = self.merge_operation(case)
        # `merge` appends `Item:` after a blank line, so a body whose own last
        # line reads as a trailer lands one paragraph above the block and git
        # never reads it back. Nothing about the line looks wrong in the PR.
        operation.state["body"] = (
            operation.state["body"].rstrip() + f"\n\nDelivers: sd:{case.item}")
        with self.assertRaisesRegex(ship.Refusal, "outside the trailer block"):
            operation.merge()
        self.assertFalse([call for call in case.remote.calls if call.method == "PUT"],
                         "the squash must not be dispatched")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
