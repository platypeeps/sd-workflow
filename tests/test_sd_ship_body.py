"""The body `sd-ship` publishes is a body it accepts back (sd:1870).

`prepare` appended `Work: sd:<item>` to the body it published and refused a
`--body-file` carrying that same line, so the live body of #1236 and #1238,
fed back, was refused. `sd_ship_body.normalize` strips an owned line that
says what `sd-ship` would write and refuses the rest by line number. The
cases below run the unit, `prepare` on the shared fixture, the `body` verb
as a process, and the pull-request template.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import unittest

from sd_db.testing.remote import _git

from tests import test_sd_ship as ship_fixture
from tests import test_sd_ship_scope_lint as scope_fixture

ship = ship_fixture.ship
ROOT = pathlib.Path(__file__).resolve().parents[1]

import sd_lib  # noqa: E402 - bin/ is on sys.path once the fixture module loads
import sd_ship_body  # noqa: E402

TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"


def normalize(body: str, item: int | None = 7, **flags) -> tuple[str, tuple[str, ...]]:
    return sd_ship_body.normalize(body, item, **flags)


class NormalizeTests(unittest.TestCase):
    def test_lines_that_agree_are_stripped_and_named(self) -> None:
        body = "Summary.\n\nItem: sd:7\nWork: sd:7\nDelivers: sd:7\nRefs: sd:8\n"
        text, stripped = normalize(body)
        self.assertEqual("Summary.\n\nRefs: sd:8", text)
        self.assertEqual(("Item: sd:7", "Work: sd:7", "Delivers: sd:7"), stripped)

    def test_an_authored_with_line_is_prose_the_body_keeps(self) -> None:
        """sd:3014: sd-ship owns no attribution line, so the body keeps it unread."""
        self.assertEqual(("Summary.\n\nAuthored-with: nobody", ()), normalize("Summary.\n\nAuthored-with: nobody\n"))

    def test_a_stray_item_work_or_delivers_line_is_stripped_not_refused(self) -> None:
        """sd:2999: sd-ship writes the links itself, so another item's line is dropped."""
        body = "Summary.\n\nItem: sd:9\nWork: sd:70\nDelivers: sd:7\nDelivers: sd:8\n"
        self.assertEqual(("Summary.", ("Item: sd:9", "Work: sd:70", "Delivers: sd:7", "Delivers: sd:8")),
                         normalize(body))

    def test_a_closes_line_is_refused_and_names_the_enqueue_item_list(self) -> None:
        """sd:3704, operator ruling 2026-10-13: the enqueue's --item list is the one closing set."""
        for value in ("sd:8", "sd:8, sd:9", "sd:7", "#12", ""):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ship.Refusal, r"line 3: `Closes:[^`]*`; a body `Closes:` line closes nothing; name each item "
                                  r"the merge closes with --item on `sd-ship lane enqueue`"):
                normalize(f"Summary.\n\nCloses: {value}\n")
        body = "Summary.\n\n    Closes: sd:11\nRefs: sd:12\n"
        self.assertEqual((body.rstrip(), ()), normalize(body))

    def test_a_closes_line_in_a_fence_or_a_comment_is_an_example_and_never_closes(self) -> None:
        """A quoted `Closes:` closed sd:8 on merge, even with --associate-only (late-reviews review)."""
        bodies = {
            "backtick fence": "Example only:\n```text\nCloses: sd:8\n```\n",
            "tilde fence": "Example only:\n~~~~\nCloses: sd:8\n~~~\n~~~~\n",
            "unclosed fence": "Example only:\n```\nCloses: sd:8\n",
            "comment": "Summary.\n<!-- for example\nCloses: sd:8\n-->\n",
            "comment opened mid-line": "Summary. <!--\nCloses: sd:8 -->\n",
        }
        for name, body in bodies.items():
            with self.subTest(name):
                with self.assertRaisesRegex(ship.Refusal, r"`Closes: sd:8[^`]*`; a code block or comment holds it"):
                    normalize(body)
                indented = body.replace("\nCloses: sd:8", "\n    Closes: sd:8")
                self.assertEqual((indented.rstrip(), ()), normalize(indented))

    def test_a_column_zero_closes_line_outside_fences_and_comments_is_still_read(self) -> None:
        body = ("Summary.\n```sh\necho done\n```\n<!-- a note -->\n<!--\nRefs: sd:3\n-->\n"
                "Closes: sd:8\n~~~\n    Closes: sd:9\n~~~\n")
        with self.assertRaises(ship.Refusal) as caught:
            normalize(body)
        self.assertIn("line 9: `Closes: sd:8`", str(caught.exception))
        self.assertNotIn("sd:9", str(caught.exception))

    def test_quoted_lines_follow_commonmark_fences(self) -> None:
        body = "a\n````md\n```\nb\n```\n````\nc\n```x`y\nd\n"
        # The four-backtick fence holds lines 2-6; three backticks do not close
        # it, and a backtick fence's info string cannot hold a backtick.
        self.assertEqual({2, 3, 4, 5, 6}, sd_ship_body.quoted_lines(body))

    def test_a_stripped_line_between_blank_lines_leaves_one_gap(self) -> None:
        self.assertEqual("A\n\nB", normalize("A\n\nWork: sd:7\n\nB\n")[0])
        self.assertEqual("A\n\n\nB", normalize("A\n\n\nB\n")[0])

    def test_every_problem_is_named_in_one_refusal(self) -> None:
        with self.assertRaises(ship.Refusal) as caught:
            normalize("Closes: sd:7\nx\nCloses: #12\n")
        self.assertIn("line 1:", str(caught.exception))
        self.assertIn("line 3:", str(caught.exception))

    def test_an_indented_line_is_prose_and_stays_with_an_item(self) -> None:
        body = "Example:\n\n    Item: sd:9\n"
        self.assertEqual(("Example:\n\n    Item: sd:9", ()), normalize(body))

    def test_with_no_item_every_column_zero_owned_line_is_stripped(self) -> None:
        """sd:2999: a no-item body owns nothing, so its stray links are dropped, not refused."""
        for line in ("Work: sd:9", "Item: sd:9", "Delivers: sd:9", "Closes: sd:9"):
            with self.subTest(line=line):
                self.assertEqual(("Proposed change", (line,)), normalize(f"Proposed change\n\n{line}\n", None))
        self.assertEqual(("Proposed change\n\n Work : sd:9\n", ()), normalize("Proposed change\n\n Work : sd:9\n", None))
        self.assertEqual(("Plain.\n", ()), normalize("Plain.\n", None))

    def test_normalize_is_a_fixpoint_and_the_published_body_round_trips(self) -> None:
        bodies = ("Summary.", "Summary.\n", "Summary.\n\nWork: sd:7\n", "A\n\nItem: sd:7\n\nB\n\n\n",
                  "Summary.\r\n\r\nWork: sd:7\r\n", "Work: sd:7", "", "Refs: sd:8\nItem: sd:7",
                  "Summary.\n\nRefs: sd:8\nItem: sd:7\n")
        for body in bodies:
            with self.subTest(body=body):
                once, _ = normalize(body)
                self.assertEqual(once, normalize(once)[0])
                published = sd_ship_body.published(once, 7)
                self.assertEqual(once, normalize(published)[0])
                self.assertEqual(published, sd_ship_body.published(normalize(published)[0], 7))

    def test_the_parser_reads_the_one_constant(self) -> None:
        body = "".join(f"{key} sd:7\n" for key in sd_lib.OWNED_TRAILERS)
        self.assertEqual(list(sd_lib.OWNED_TRAILERS), [line.key for line in sd_ship_body.owned_lines(body)])


class ClosingTests(unittest.TestCase):
    def test_a_title_names_each_whole_token_once(self) -> None:
        self.assertEqual((3, 4), sd_ship_body.title_ids("Fix sd:3 and sd:4 (sd:3, xsd:5, sd:6x)"))

    def test_a_repo_less_item_refusal_says_it_has_no_repository_and_how_to_close_it(self) -> None:
        """sd:5065: a report belongs to no repository, so "another repository" misled."""
        import tempfile

        from sd_db import connect, create_item, initialise

        with tempfile.TemporaryDirectory() as scratch:
            path = pathlib.Path(scratch) / "sd.db"
            initialise(path)
            with connect(path) as connection:
                report = create_item(connection, kind="report", title="A report item")
                with self.assertRaises(ship.Refusal) as caught:
                    sd_ship_body.refuse_unclosable(connection, report + 1, "~/repos/example", [report], True)
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "closes_item_foreign")
        self.assertIn(f"sd:{report}, which belongs to no repository", str(caught.exception))
        self.assertIn(f"`sd reports acknowledge {report}`", caught.exception.workflow["next_action"])


class ClosableKindTests(unittest.TestCase):
    def test_the_closable_kinds_are_kinds_the_schema_allows(self):
        """sd:7004: `personal` left `item.kind`, so a merge closes no such row."""
        import sqlite3
        import tempfile

        from sd_db.migrate import initialise
        from sd_db.workflow import schema_kinds

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "sd.db"
            initialise(path)
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            try:
                allowed = set(schema_kinds(connection))
            finally:
                connection.close()
        self.assertEqual(set(sd_ship_body._CLOSABLE_KINDS) - allowed, set())


class SummaryHeadingTests(unittest.TestCase):
    """sd:5375: the `## Summary` group headings `<repo> sd:N` a delivering enqueue reads."""

    def ids(self, body: str) -> tuple[int, ...]:
        return sd_ship_body.summary_ids(body, ("pack",))

    def test_a_quoted_heading_neither_opens_nor_closes_the_section(self) -> None:
        """Codex review: a heading inside a fence or comment ended the Summary, so a later group was missed."""
        self.assertEqual(self.ids("## Summary\n```markdown\n## Example\n```\npack sd:2: delivered\n"), (2,))
        self.assertEqual(self.ids("## Summary\n<!--\n## Example\n-->\npack sd:3: delivered\n"), (3,))
        self.assertEqual(self.ids("## Test plan\n```\n## Summary\npack sd:4: example\n```\n"), ())

    def test_any_atx_spelling_of_the_summary_heading_opens_it(self) -> None:
        """Codex review: closing hashes and up to three leading spaces are the same heading."""
        for heading in ("## Summary ##", "  ## Summary", "## summary", "# Summary", "## Summary\r"):
            with self.subTest(heading=heading):
                self.assertEqual(self.ids(f"{heading}\npack sd:2: delivered\r\n## Test plan\npack sd:5: test\n"), (2,))
        for heading in ("### Summary", "    ## Summary", "##Summary", "## Summary of it"):
            with self.subTest(heading=heading):
                self.assertEqual(self.ids(f"{heading}\npack sd:2: delivered\n"), ())

    def test_only_this_repositorys_name_at_the_line_start_counts(self) -> None:
        body = ("## Summary\nPack sd:2: case folds\n- pack sd:3, a bullet\nsystem sd:4: elsewhere\n"
                "See pack sd:5 in prose\n  pack sd:6 indented\npack sd:22x\npack sd:2 again\n")
        self.assertEqual(self.ids(body), (2, 3))

    def test_a_refs_line_names_only_what_a_reader_sees(self) -> None:
        """Codex review: an id inside an HTML comment on a `Refs:` line exempted a delivered item."""
        self.assertEqual(sd_ship_body.referenced_ids("Refs: sd:2, sd:3\nrefs: sd:4\n  Refs: sd:5\n"), {2, 3, 4})
        self.assertEqual(sd_ship_body.referenced_ids("Refs: <!-- sd:2 --> sd:3\nRefs: sd:4 <!-- sd:5\n"), {3, 4})


class LineLengthTests(unittest.TestCase):
    """sd:5793: every commit and body line stays at 72, since some repositories lint it."""

    def refused(self, title: str = "t", body: str = "", commits=()) -> str:
        with self.assertRaises(ship.Refusal) as caught:
            sd_ship_body.refuse_long_lines(title, body, commits)
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "line_too_long")
        return str(caught.exception)

    def test_the_limits_count_githubs_squash_suffix(self) -> None:
        self.assertEqual((sd_ship_body.LINE_LIMIT, sd_ship_body.TITLE_LIMIT), (72, 63))
        sd_ship_body.refuse_long_lines("x" * 63, "y" * 72 + "\n", [("a" * 40, "z" * 72 + "\n\n" + "w" * 72)])
        self.assertIn("the title is 64 characters (limit 63)", self.refused("x" * 64))

    def test_each_long_line_is_named_with_its_place_length_and_limit(self) -> None:
        body = "## Summary\n\n" + "b" * 73 + "\nshort\n" + "c" * 80 + "\r\n"
        commits = [("1" * 40, "s" * 74 + "\n\nfine\n" + "d" * 90 + "\n"), ("2" * 40, "ok\n")]
        message = self.refused("t" * 70, body, commits)
        self.assertIn("the title is 70 characters (limit 63); body line 3 is 73 characters (limit 72); "
                      "body line 5 is 80 characters (limit 72); commit 111111111111 line 1 is 74 characters "
                      "(limit 72); commit 111111111111 line 4 is 90 characters (limit 72)", message)
        self.assertNotIn("222222222222", message)

    def test_a_long_default_merge_subject_is_told_the_short_form(self) -> None:
        """sd:6342: git's own merge subject runs past 72 on a long branch name."""
        subject = "Merge remote-tracking branch 'origin/main' into " + "b" * 30
        for commits, named in (([("1" * 40, subject + "\n")], True), ([("1" * 40, "s" * 80 + "\n")], False)):
            with self.subTest(named=named):
                with self.assertRaises(ship.Refusal) as caught:
                    sd_ship_body.refuse_long_lines("t", "", commits)
                action = caught.exception.workflow["next_action"]
                self.assertEqual('git merge --no-ff -m "chore: merge origin/main"' in action, named)

    def test_only_a_fenced_line_or_one_bare_url_is_exempt(self) -> None:
        url = "https://example.test/" + "p" * 80
        long = "x" * 73
        self.assertEqual(sd_ship_body.long_lines(f"```text\n{long}\n```\n{url}\n~~~\n{long}\n"), [])
        for line in (f" {url}", f"See {url}", f"<{url}>", f"- {url}", f"<!-- {long} -->", f"    {long}",
                     f"`{long}`", "example.test/" + "p" * 70):
            with self.subTest(line=line):
                self.assertEqual(sd_ship_body.long_lines(f"<!--\n{line}\n-->\n"), [(2, len(line))])

    def test_the_lines_sd_ship_writes_itself_fit(self) -> None:
        """`Work:` in the body; `Item:`, `Delivers:`, `Reviewed-base:` in the squash; GitHub's ids are SHA-1."""
        written = sd_ship_body.published("Words.", 9_999_999) + "".join(
            f"{key} sd:9999999\n" for key in sd_lib.OWNED_TRAILERS) + f"{sd_lib.REVIEWED_BASE_TRAILER} {'f' * 40}\n"
        self.assertEqual(sd_ship_body.long_lines(written), [])


class CitedIdTests(unittest.TestCase):
    """sd:5999: a `Closes: sd:N` trailer or a `Lane review fix, <repo> sd:N` line cites an item; nothing else does."""

    def test_only_closes_trailers_and_review_fix_lines_cite(self) -> None:
        commits = [("a" * 40, "fix: one (pack sd:2)\r\n\nKeeps the sd:3 guard.\r\nLane review fix, pack sd:4: x\r\n"
                              "\r\nCloses: sd:9\r\nRefs: sd:3\r\n"),
                   ("b" * 40, "chore: none\n\n  Lane review fix, pack sd:5: indented\nCloses: sd:10\n\nWords."),
                   ("c" * 40, "Closes: sd:11")]
        body = "Lane review fix, system sd:6: y\nSee Lane review fix, pack sd:7.\n~~~\nLane review fix, pack sd:8\n~~~\n"
        self.assertEqual(sd_ship_body.cited_ids(body, commits), (9, 6, 4))

    def test_a_subject_never_cites_and_a_closes_trailer_does(self) -> None:
        """Lane review of sd:5999, round 5 ruling: no word list catches every way to say partial."""
        self.assertEqual(sd_ship_body.cited_ids("", [("a" * 40, "fix: first part (sd:42)")]), ())
        self.assertEqual(sd_ship_body.cited_ids("", [("a" * 40, "fix: first part (sd:42)\n\nWords.\n")]), ())
        self.assertEqual(sd_ship_body.cited_ids("", [("a" * 40, "fix: the rest\n\nWords.\n\nCloses: sd:42\n")]),
                         (42,))

    def test_a_quoted_or_prose_closes_line_cites_nothing(self) -> None:
        """Lane review of sd:5999, round 6: only a block git reads as trailers closes, as `sd_lib.trailer_block`."""
        for last in ("```\nCloses: sd:42\n```", "<!--\nCloses: sd:42\n-->", "Write the trailer as\nCloses: sd:42",
                     "~~~\n\nCloses: sd:42"):
            with self.subTest(last=last):
                self.assertEqual(sd_ship_body.cited_ids("", [("a" * 40, f"fix: x\n\nWords.\n\n{last}\n")]), ())
        real = "fix: x\n\nWords.\n\nCloses: sd:42\nCo-Authored-By: a\n  b <b@example.test>\nRefs: sd:7\n"
        self.assertEqual(sd_ship_body.cited_ids("", [("a" * 40, real)]), (42,))

    def test_a_commit_the_branch_reverts_cites_nothing(self) -> None:
        """Lane review of sd:5999, round 2: the revert undoes the work, so its commit closes nothing."""
        done, undone, again = "a" * 40, "b" * 40, "c" * 40
        work = (done, "fix: implement\n\nLane review fix, pack sd:3: y.\n\nCloses: sd:2\n")
        revert = (undone, f'Revert "fix: implement (sd:2)"\n\nThis reverts commit {done}.\n')
        self.assertEqual(sd_ship_body.cited_ids("", [revert, work]), ())
        # Commits come newest first. A revert of the revert restores the work, and its citation.
        restore = (again, f'Revert "Revert ..."\n\nThis reverts commit {undone}.\n')
        self.assertEqual(sd_ship_body.cited_ids("", [restore, revert, work]), (2, 3))
        # A revert of a commit outside the branch drops nothing the branch adds.
        elsewhere = ("d" * 40, f'Revert "fix: x"\n\nThis reverts commit {"e" * 40}.\n')
        self.assertEqual(sd_ship_body.cited_ids("", [elsewhere, work]), (2, 3))

    def test_a_merge_the_branch_reverts_cites_nothing_it_brought_in(self) -> None:
        """Lane review of sd:5999, round 3: `git revert -m 1 <merge>` undoes each commit the merge brought in."""
        base, side, main, merge, undo, again = (c * 40 for c in "abcdef")
        commits = [(undo, f'Revert "Merge"\n\nThis reverts commit {merge}, reversing\nchanges made to {main}.\n'),
                   (merge, "Merge branch 'side'"), (main, "fix: mainline\n\nCloses: sd:4"),
                   (side, "fix: implement\n\nLane review fix, pack sd:3: y.\n\nCloses: sd:2\n"),
                   (base, "fix: base\n\nCloses: sd:5")]
        parents = {undo: (merge,), merge: (main, side), main: (base,), side: (base,), base: ()}
        self.assertEqual(sd_ship_body.cited_ids("", commits, parents), (4, 5))
        # A revert of the merge's revert brings the work back, and its citations.
        restore = (again, f'Revert "Revert"\n\nThis reverts commit {undo}.\n')
        self.assertEqual(sd_ship_body.cited_ids("", [restore, *commits], {again: (undo,), **parents}), (4, 2, 5, 3))


class ConventionalTitleTests(unittest.TestCase):
    """sd:6059: the title is the squash subject, so it is a Conventional Commits header."""

    def test_the_types_are_commitlints_conventional_set(self) -> None:
        self.assertEqual(sorted(sd_lib.CONVENTIONAL_TYPES),
                         ["build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style",
                          "test"])

    def test_each_type_scope_and_bang_is_accepted(self) -> None:
        for kind in sd_lib.CONVENTIONAL_TYPES:
            for title in (f"{kind}: a summary", f"{kind}(lane): a summary", f"{kind}(machine-setup)!: a summary",
                          f"{kind}!: a summary"):
                with self.subTest(title=title):
                    self.assertIsNone(sd_ship_body.refuse_unconventional_title(title))

    def test_anything_else_is_refused(self) -> None:
        for title in ("A plain title", "Fix: capital type", "fix:no space", "fix: ", "fix(): empty scope",
                      "fixes: a summary", "wip: draft", " fix: leading space", "fix(a)(b): two scopes",
                      "Lane note names merge (pack sd:5968)"):
            with self.subTest(title=title), self.assertRaises(ship.Refusal) as caught:
                sd_ship_body.refuse_unconventional_title(title)
            self.assertEqual(caught.exception.workflow["blocker"]["code"], "title_not_conventional")


class TemplateTests(unittest.TestCase):
    def test_the_template_writes_no_owned_trailer_outside_a_comment(self) -> None:
        text = re.sub(r"<!--.*?-->", "", TEMPLATE.read_text(encoding="utf-8"), flags=re.DOTALL)
        self.assertEqual([], [line.text for line in sd_ship_body.owned_lines(text)])
        self.assertIn("Refs: sd:<other>", text)

    def test_a_body_from_the_template_passes_the_enqueue_line_check(self) -> None:
        """sd:5793: an HTML comment is no exemption, and the squash carries the body's comments."""
        self.assertEqual(sd_ship_body.long_lines(TEMPLATE.read_text(encoding="utf-8")), [])


class PrepareTests(unittest.TestCase):
    setUp = ship_fixture.ShipCase.setUp
    args = ship_fixture.ShipCase.args
    operation = ship_fixture.ShipCase.operation
    prepare = ship_fixture.ShipCase.prepare

    def live(self):
        number = self.operation().state["pull_request"]["number"]
        return self.remote.pull_requests[number]

    def again(self, *extra):
        _git(self.root, "commit", "--allow-empty", "-m", "another slice\n\nAuthored-with: human")
        return self.prepare(*extra)

    def test_the_live_body_fed_back_as_a_body_file_prepares(self) -> None:
        body = self.directory / "body.md"
        body.write_text("A slice.\n")
        first = self.prepare("--body-file", str(body))
        published = self.live().body
        self.assertIn(f"\nWork: sd:{self.item}\n", published)
        body.write_text(published)
        second = self.again("--body-file", str(body))
        self.assertEqual("ready_to_send", second["phase"])
        self.assertEqual(published, self.operation().state["body"])
        self.assertEqual(("file", []), (first["body_source"], first["normalized"]))
        self.assertEqual([f"Work: sd:{self.item}"], second["normalized"])

    def test_an_edit_made_on_github_is_prepared_from_the_live_body(self) -> None:
        self.prepare()
        self.live().body = f"Edited on GitHub.\r\n\r\nCI/review scope: none.\r\n\r\nWork: sd:{self.item}\r\n"
        result = self.again()
        self.assertEqual(f"Edited on GitHub.\n\nCI/review scope: none.\n\nWork: sd:{self.item}\n",
                         self.operation().state["body"])
        self.assertEqual("live_pr", result["body_source"])

    def test_a_hand_opened_pull_request_without_a_receipt_is_prepared_from_its_live_body(self) -> None:
        # sd:1878: no receipt names the pull request, so before this the
        # default body was reviewed and squashed while GitHub showed another.
        _git(self.root, "push", "-q", "origin", "HEAD:refs/heads/topic")
        written = f"Opened by hand.\r\n\r\nCI/review scope: none.\r\n\r\nWork: sd:{self.item}\r\n"
        pull = self.remote.open_pull_request("topic", title="by hand", body=written)
        self.remote.commit_on("elsewhere", "another branch\n\nAuthored-with: human")
        self.remote.open_pull_request("elsewhere", title="another branch", body="Not this one.\n")
        result = self.prepare()
        self.assertEqual(("live_pr", [f"Work: sd:{self.item}"]), (result["body_source"], result["normalized"]))
        self.assertEqual(f"Opened by hand.\n\nCI/review scope: none.\n\nWork: sd:{self.item}\n",
                         self.operation().state["body"])
        self.assertEqual(pull.number, self.operation().state["pull_request"]["number"])

    def test_a_blank_hand_opened_body_falls_back_to_the_default(self) -> None:
        _git(self.root, "push", "-q", "origin", "HEAD:refs/heads/topic")
        self.remote.open_pull_request("topic", title="by hand", body="  \n")
        self.assertEqual("default", self.prepare()["body_source"])

    def test_a_different_item_in_the_body_file_is_dropped_and_the_own_link_written(self) -> None:
        body = self.directory / "body.md"
        body.write_text(f"A slice.\n\nWork: sd:{self.item + 1}\n")
        result = self.prepare("--body-file", str(body))
        self.assertEqual(("ready_to_send", [f"Work: sd:{self.item + 1}"]), (result["phase"], result["normalized"]))
        self.assertEqual(f"A slice.\n\nWork: sd:{self.item}\n", self.live().body)


class BodyVerbTests(unittest.TestCase):
    setUp = ship_fixture.ShipCase.setUp

    def run_verb(self, *extra: str) -> tuple[int, dict]:
        done = subprocess.run([sys.executable, str(ROOT / "bin/sd-ship"), "body", "--item", "7", "--json", *extra],
                              cwd=self.root, env=self.environment, capture_output=True, text=True,
                              timeout=ship_fixture.CLI_TIMEOUT, check=False)
        return done.returncode, json.loads(done.stdout)

    def body(self, text: str, *extra: str) -> tuple[int, dict]:
        path = self.directory / "body.md"
        path.write_text(text)
        return self.run_verb("--body-file", str(path), *extra)

    def policy(self) -> None:
        """The scope policy in the working tree only, so the checkout's own diff stays empty."""
        policy = self.root / ".github/copilot-instructions.md"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(scope_fixture.SCOPE_POLICY)

    def pull_request(self, body: str) -> int:
        """A pull request opened by hand on another branch."""
        self.remote.commit_on("elsewhere", "a hand-opened change\n\nAuthored-with: human")
        return self.remote.open_pull_request("elsewhere", title="by hand", body=body).number

    def test_the_verb_prints_the_published_body_and_its_lint(self) -> None:
        before = self.database.read_bytes()
        code, result = self.body("A slice.\n\nWork: sd:7\n")
        self.assertEqual(0, code, result)
        self.assertEqual("A slice.\n\nWork: sd:7\n", result["body"])
        self.assertEqual(["Work: sd:7"], result["normalized"])
        self.assertEqual(0, result["lint"]["exit"])
        self.assertIn("sd-docs-lint: clean", result["lint"]["output"])
        self.assertEqual(before, self.database.read_bytes())
        self.assertEqual([], self.remote.calls)

    def test_the_verb_exits_nonzero_on_a_refusal(self) -> None:
        code, result = self.body("A slice.\n\nCloses: sd:7\n")
        self.assertEqual(3, code)
        self.assertIn("line 3: `Closes: sd:7`; a body `Closes:` line closes nothing", result["error"])

    def test_a_ci_diff_with_no_scope_line_passes_the_verb(self) -> None:
        self.policy()
        (self.root / ".github/workflows").mkdir(parents=True, exist_ok=True)
        (self.root / ".github/workflows/ci.yml").write_text("on: push\n")
        _git(self.root, "add", ".github/workflows")
        _git(self.root, "commit", "-m", "touch the CI surface\n\nAuthored-with: human")
        code, result = self.body("A slice.\n")
        self.assertEqual(0, code, result)  # sd:2999: no scope line is required
        self.assertNotIn("CI/review scope:", result["lint"]["output"])  # rule 8 is retired
        self.assertNotIn("scope", result)  # nothing reads the demanded scope lines any more

    def test_a_given_pull_request_is_linted_on_its_live_body_alone(self) -> None:
        # sd:1877: the live body is the one read; sd:2999: its file listing is not.
        number = self.pull_request("Opened by hand.\r\n")
        code, result = self.run_verb("--pr", str(number))
        self.assertEqual(0, code, result)
        self.assertEqual(("live_pr", "Opened by hand.\n\nWork: sd:7\n"), (result["body_source"], result["body"]))
        self.assertNotIn("scope", result)
        self.assertFalse([call for call in self.remote.calls if call.path.split("?")[0].endswith("/files")])

    def test_a_pull_request_github_does_not_have_is_a_refusal(self) -> None:
        code, result = self.run_verb("--pr", "99")
        self.assertEqual(3, code)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
