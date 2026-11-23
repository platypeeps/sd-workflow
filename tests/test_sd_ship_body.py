"""The body `sd-ship` publishes is a body it accepts back (sd:1870).

`prepare` appended `Work: sd:<item>` to the body it published and refused a
`--body-file` carrying that same line, so the live body of #1236 and #1238,
fed back, was refused. `sd_ship_body.normalize` strips an owned line that
says what `sd-ship` would write and refuses the rest by line number. The
cases below run the unit, `prepare` on the shared fixture, the `body` verb
as a process, and the pull-request template.
"""
from __future__ import annotations

import itertools
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

    def test_a_hand_revert_that_lists_its_commits_cites_nothing_they_cited(self) -> None:
        """pack sd:9666: a `revert(...):` commit listed the commits it undid by short sha, one per line; the
        squash still closed the items their `Closes:` trailers named. A sha it names in prose keeps its close."""
        undone, kept, other, revert = "a" * 40, "b" * 40, "c" * 40, "d" * 40
        commits = [(revert, f"revert(ship): split the renewal out (sd:2)\n\nReverted commits:\n\n"
                            f"- {undone[:9]} an explicit flag renews a claim\n"
                            f"  * {other[:9]} a lane prepare never renews\n\n"
                            f"Kept where a hunk is shared: {kept[:9]} (sd:4) keeps it.\n"),
                   (kept, "fix: shared\n\nCloses: sd:4\n"),
                   (other, "fix: prepare\n\nCloses: sd:3\n"),
                   (undone, "feat: renew\n\nCloses: sd:2\n")]
        self.assertEqual(sd_ship_body.cited_ids("", commits), (4,))
        # A list line in a commit that is no revert undoes nothing.
        notes = (revert, f"docs: history\n\nEarlier commits:\n\n- {undone[:9]} renew\n")
        self.assertEqual(sd_ship_body.cited_ids("", [notes, *commits[1:]]), (4, 3, 2))

    def test_a_hand_revert_undoes_only_the_list_it_says_it_reverted(self) -> None:
        """pack sd:9666 review: a sha-led bullet under `Kept commits:`, or in a fenced example, counted as
        an undo, and the commits' `Closes:` and review-fix citations dropped."""
        undone, kept, quoted, revert = "a" * 40, "b" * 40, "c" * 40, "d" * 40
        commits = [(revert, f"revert: split the renewal out\n\nReverted commits:\n\n"
                            f"- {undone[:9]} renew\n\nKept commits:\n\n- {kept[:9]} shared\n\n"
                            f"Reverted as below, for example:\n\n```\n- {quoted[:9]} example\n```\n"),
                   (kept, "fix: shared\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:4\n"),
                   (quoted, "fix: other\n\nCloses: sd:3\n"),
                   (undone, "feat: renew\n\nCloses: sd:2\n")]
        self.assertEqual(sd_ship_body.cited_ids("", commits), (4, 3, 5))

    def test_a_list_under_a_negated_or_other_heading_undoes_nothing(self) -> None:
        """pack sd:9666 review, round 2: `Commits not reverted:` passed the heading match, and the commits it
        listed lost their `Closes:` and review-fix citations. Only a heading that says reverted counts."""
        work, revert = "a" * 40, "d" * 40
        cited = (work, "fix: shared\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:4\n")
        for heading in ("Commits not reverted:", "Unreverted commits:", "Commits never reverted:",
                        "Left unreverted:", "Kept, not reverted:", "Reverted as below, for example:",
                        "Not reverted commits:"):
            with self.subTest(heading=heading):
                message = f"revert: split the renewal out\n\n{heading}\n\n- {work[:9]} shared\n"
                self.assertEqual(sd_ship_body.cited_ids("", [(revert, message), cited]), (4, 5))
        for heading in ("Reverted commits:", "Work moved out. Reverted commits:", "Reverts:", "Reverted:"):
            with self.subTest(heading=heading):
                message = f"revert: split the renewal out\n\n{heading}\n\n- {work[:9]} shared\n"
                self.assertEqual(sd_ship_body.cited_ids("", [(revert, message), cited]), ())

    def revert_of(self, body: str) -> tuple[str, str]:
        return ("d" * 40, f"revert: split the renewal out\n\n{body}")

    def test_a_bullet_or_indented_heading_starts_its_own_list(self) -> None:
        """pack sd:9666 review, round 3, instances 1 and 2: `- Kept commits:` and an indented `Kept commits:`
        kept the `Reverted commits:` heading above them, and their nested shas lost their citations."""
        undone, kept = "a" * 40, "b" * 40
        work = [(kept, "fix: shared\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:4\n"),
                (undone, "feat: renew\n\nCloses: sd:2\n")]
        for heading in ("- Kept commits:", "  Kept commits:", "* Kept commits:"):
            with self.subTest(heading=heading):
                revert = self.revert_of(f"Reverted commits:\n\n- {undone[:9]} renew\n{heading}\n"
                                        f"  - {kept[:9]} shared\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, *work]), (4, 5))
        # A bullet heading that says reverted opens a list too.
        revert = self.revert_of(f"Kept commits:\n\n- {kept[:9]} shared\n- Reverted commits:\n"
                                f"  - {undone[:9]} renew\n")
        self.assertEqual(sd_ship_body.cited_ids("", [revert, *work]), (4, 5))

    def test_a_fence_inside_a_list_item_is_an_example(self) -> None:
        """pack sd:9666 review, round 4: a fence indented four spaces under a list item was missed, and the
        sha bullet in it dropped a real commit's `Closes:` and review-fix citations."""
        undone, quoted = "a" * 40, "c" * 40
        revert = self.revert_of(f"- Reverted commits:\n  - {undone[:9]} renew\n\n    ```\n"
                                f"    - {quoted[:9]} example\n    ```\n")
        work = [(quoted, "fix: other\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:3\n"),
                (undone, "feat: renew\n\nCloses: sd:2\n")]
        self.assertEqual(sd_ship_body.cited_ids("", [revert, *work]), (3, 5))

    def test_a_nested_heading_ends_where_its_list_dedents(self) -> None:
        """pack sd:9666 review, round 4: a nested `Kept commits:` list stayed the heading after the list
        dedented, so a commit listed under the outer `Reverted commits:` still closed its item."""
        undone, kept, later = "a" * 40, "b" * 40, "e" * 40
        work = [(kept, "fix: shared\n\nCloses: sd:4\n"), (later, "fix: later\n\nCloses: sd:6\n"),
                (undone, "feat: renew\n\nCloses: sd:2\n")]
        for body in (f"Reverted commits:\n\n- {undone[:9]} renew\n  - Kept commits:\n"
                     f"    - {kept[:9]} shared\n- {later[:9]} later\n",
                     f"- Reverted commits:\n  - {undone[:9]} renew\n  - Kept commits:\n"
                     f"    - {kept[:9]} shared\n  - {later[:9]} later\n"):
            with self.subTest(body=body):
                self.assertEqual(sd_ship_body.cited_ids("", [self.revert_of(body), *work]), (4,))
        # A sibling of a bullet heading is not under it.
        body = f"- Reverted commits:\n  - {undone[:9]} renew\n- {later[:9]} later\n"
        self.assertEqual(sd_ship_body.cited_ids("", [self.revert_of(body), *work]), (4, 6))

    def test_a_fence_opens_only_inside_its_list_item_indent(self) -> None:
        """pack sd:9666 review, round 5, instance 1: four spaces and a fence at the top level start an
        indented code block, not a fence; reading a fence there quoted the citations after it."""
        self.assertEqual(sd_ship_body.quoted_lines("a\n    ```\nb\n"), set())
        self.assertEqual(sd_ship_body.cited_ids("    ```\nLane review fix, pack sd:5: y.\n", []), (5,))
        # Inside a list item the same indent opens a fence, and the item's end closes it.
        self.assertEqual(sd_ship_body.quoted_lines("- a\n    ```\n  b\nc\n"), {2, 3})

    def test_an_indented_code_example_lists_nothing(self) -> None:
        """pack sd:9666 review, round 6, instance 1: four spaces past the list column start an indented
        code block, not a list item, yet `    - <sha> example` under `Reverted commits:` undid that commit."""
        undone, quoted = "a" * 40, "c" * 40
        work = [(quoted, "fix: other\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:3\n"),
                (undone, "feat: renew\n\nCloses: sd:2\n")]
        for body in (f"Reverted commits:\n\n    - {quoted[:9]} example\n\n- {undone[:9]} renew\n",
                     f"Reverted commits:\n\n- {undone[:9]} renew\n\n      - {quoted[:9]} example\n"):
            with self.subTest(body=body):
                self.assertEqual(sd_ship_body.cited_ids("", [self.revert_of(body), *work]), (3, 5))

    def test_a_marker_before_the_items_text_column_is_a_sibling(self) -> None:
        """pack sd:9666 review, round 7, instance 1: ` - <sha>` one space in sits before the text column of
        `- Reverted commits:`, so it is a sibling, yet the heading held it and dropped its citations."""
        kept = "b" * 40
        cited = (kept, "fix: shared\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:4\n")
        revert = self.revert_of(f"- Reverted commits:\n - {kept[:9]} retained\n")
        self.assertEqual(sd_ship_body.cited_ids("", [revert, cited]), (4, 5))

    def test_indented_code_opens_no_comment_and_no_heading(self) -> None:
        """pack sd:9666 review, round 7, instance 2: a literal `<!--` in an indented code block opened a
        comment that hid the real list after it. Instance 3: a heading in indented code is code too."""
        undone, kept = "a" * 40, "b" * 40
        self.assertEqual(sd_ship_body.quoted_lines("- a\n\n      <!--\n\n  b\n"), set())
        self.assertEqual(sd_ship_body.quoted_lines("- a\n\n      <!--\n\n  b\n", code=True), {3})
        revert = self.revert_of(f"- Reverted commits:\n\n      <!--\n\n  - {undone[:9]} undone\n")
        self.assertEqual(sd_ship_body.cited_ids("", [revert, (undone, "feat: x\n\nCloses: sd:2\n")]), ())
        revert = self.revert_of(f"Kept commits:\n\n    Reverted commits:\n\n- {kept[:9]} kept\n")
        self.assertEqual(sd_ship_body.cited_ids("", [revert, (kept, "fix: y\n\nCloses: sd:4\n")]), (4,))

    def test_code_after_a_list_marker_opens_no_comment(self) -> None:
        """pack sd:9666 review, round 8, instance 1: `-     <!--` starts an item whose text is indented
        code, yet it opened a comment that hid the real revert after it."""
        undone = "a" * 40
        self.assertEqual(sd_ship_body.quoted_lines("-     <!--\n\n- b\n"), set())
        revert = self.revert_of(f"Reverted commits:\n\n-     <!--\n\n- {undone[:9]} actual revert\n")
        self.assertEqual(sd_ship_body.cited_ids("", [revert, (undone, "feat: x\n\nCloses: sd:2\n")]), ())

    def test_a_markdown_heading_says_reverted(self) -> None:
        """pack sd:9666 review, round 8, instance 3: `## Reverted commits:` failed the heading match, so
        the commit it listed still closed its item. Emphasis around a heading is markup too."""
        undone = "a" * 40
        for heading in ("## Reverted commits:", "### Reverted commits: ###", "**Reverted commits:**",
                        "- **Reverted:**"):
            with self.subTest(heading=heading):
                revert = self.revert_of(f"{heading}\n\n  - {undone[:9]} actual revert\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, (undone, "feat: x\n\nCloses: sd:2\n")]), ())

    def test_a_wrapped_sha_is_listed(self) -> None:
        """pack sd:9666 review, round 9: a sha in a code span, emphasis or a link under `Reverted commits:`
        named no target, so the reverted commit still closed its item. A longer word is no sha."""
        undone = "a" * 40
        cited = (undone, "feat: x\n\nCloses: sd:2\n")
        for entry in (f"`{undone[:9]}`", f"**{undone[:9]}**", f"_{undone[:9]}_", f"[{undone[:9]}](https://example.test)"):
            with self.subTest(entry=entry):
                revert = self.revert_of(f"Reverted commits:\n\n- {entry} renew\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, cited]), ())
        for entry in (f"{undone[:9]}x", f"{undone[:9]}_x"):
            with self.subTest(entry=entry):
                revert = self.revert_of(f"Reverted commits:\n\n- {entry} renew\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, cited]), (2,))

    def test_an_indented_markdown_heading_ends_the_section(self) -> None:
        """pack sd:9666 review, round 10, instance 1: `  ## Kept commits`, a heading up to three spaces in,
        left `Reverted commits:` in force, and the retained commit lost its citations."""
        kept = "b" * 40
        cited = (kept, "fix: shared\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:4\n")
        for heading in ("  ## Kept commits", "   # Kept commits", "## Kept commits"):
            with self.subTest(heading=heading):
                revert = self.revert_of(f"Reverted commits:\n\n{heading}\n\n- {kept[:9]} retained\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, cited]), (4, 5))

    def test_a_sha_runs_to_a_word_boundary(self) -> None:
        """pack sd:9666 review, round 10, instance 2: `<sha>é` and `<sha>_` matched the sha as a prefix."""
        undone = "a" * 40
        cited = (undone, "feat: x\n\nCloses: sd:2\n")
        for entry in (f"{undone[:9]}é", f"{undone[:9]}_", f"{undone[:9]}ß rest"):
            with self.subTest(entry=entry):
                revert = self.revert_of(f"Reverted commits:\n\n- {entry} renew\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, cited]), (2,))

    def test_a_closed_fence_or_a_heading_ends_the_paragraph(self) -> None:
        """pack sd:9666 review, round 10, instance 3: after a closed fence, four spaces and `<!--` start
        indented code, yet they opened a comment that hid the real revert. Instance 4 (class pass): the
        same after a Markdown heading."""
        undone = "a" * 40
        cited = (undone, "feat: x\n\nCloses: sd:2\n")
        self.assertEqual(sd_ship_body.quoted_lines("a\n```\nx\n```\n    <!--\ny\n"), {2, 3, 4})
        for opener in ("Reverted commits:\n```\nexample\n```", "Notes.\n## Reverted commits:"):
            with self.subTest(opener=opener):
                revert = self.revert_of(f"{opener}\n    <!--\n\n- {undone[:9]} renew\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, cited]), ())

    def test_an_item_that_opens_with_an_example_holds_its_nest(self) -> None:
        """pack sd:9666 review, round 11, instances 1 and 2: an item whose first line opens a fence or
        indented code was skipped whole, so an item nested under it read the `Reverted commits:` heading
        and the retained commit lost its citations."""
        kept = "b" * 40
        cited = (kept, "fix: shared\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:4\n")
        for parent in ("- ```\n  example\n  ```\n", "-     example\n"):
            with self.subTest(parent=parent):
                revert = self.revert_of(f"Reverted commits:\n\n{parent}\n  - {kept[:9]} retained\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, cited]), (4, 5))

    def test_a_thematic_break_or_setext_underline_ends_the_paragraph(self) -> None:
        """pack sd:9666 review, round 11, instance 3: after `---`, four spaces and `<!--` start indented
        code, yet they opened a comment that hid the real revert. Instance 4: a setext underline the same.
        Instance 5: `- - -` and `* * *` are breaks, not list items."""
        undone = "a" * 40
        cited = (undone, "feat: x\n\nCloses: sd:2\n")
        for opener in ("Notes.\n\n---", "Notes.\n\n***", "Notes.\n\n- - -", "Notes.\n\n* * *",
                       "Notes\n---", "Notes\n==="):
            with self.subTest(opener=opener):
                self.assertEqual(sd_ship_body.quoted_lines(f"{opener}\n    <!--\nx\n"), set())
                revert = self.revert_of(f"{opener}\n    <!--\n## Reverted commits:\n- {undone[:9]} actual revert\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, cited]), ())

    def test_a_setext_underline_makes_the_line_above_the_heading(self) -> None:
        """pack sd:9666 review, round 12, blocker 1: `====` under an indented `Kept commits` left the
        revert section in force, and `====` under `Reverted commits:` cleared it."""
        undone, kept = "a" * 40, "b" * 40
        work = [(kept, "fix: shared\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:4\n"),
                (undone, "feat: renew\n\nCloses: sd:2\n")]
        for underline in ("====", "----"):
            with self.subTest(underline=underline):
                revert = self.revert_of(f"Reverted commits:\n\n  Kept commits\n  {underline}\n\n"
                                        f"- {kept[:9]} retained\n")
                self.assertCountEqual(sd_ship_body.cited_ids("", [revert, *work]), (2, 4, 5))
                revert = self.revert_of(f"Reverted commits:\n{underline}\n\n- {undone[:9]} renew\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, *work]), (4, 5))

    def test_a_comment_opens_outside_a_code_span_and_ends_with_its_item(self) -> None:
        """pack sd:9666 review, round 12, blocker 2: `<!--` in a code span opened a comment, and a
        comment opened in a list item ran on into its siblings; both hid the real revert."""
        undone = "a" * 40
        cited = (undone, "feat: x\n\nCloses: sd:2\n")
        for opener in ("A `<!--` example.\n\nReverted commits:\n\n", "Reverted commits:\n\n- see `<!--`\n",
                       "Reverted commits:\n\n- ``a `<!--` b``\n", "Reverted commits:\n\n- <!-- scratch note\n"):
            with self.subTest(opener=opener):
                revert = self.revert_of(f"{opener}- {undone[:9]} renew\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, cited]), ())
        self.assertEqual(sd_ship_body.quoted_lines("- <!-- a\n  b\n- c\n"), {2})
        self.assertEqual(sd_ship_body.quoted_lines("x `<!--` y\nz\n"), set())
        self.assertEqual(sd_ship_body.quoted_lines("x ` <!-- y\nz\n"), {2, 3})

    def test_an_escape_or_a_wrapped_span_decides_a_comment(self) -> None:
        """pack sd:9666 review, round 13: an escaped backtick opened a code span that hid a real `<!--`,
        and a code span wrapped onto the next line did not hide the `<!--` inside it."""
        a, b, c = "a" * 7, "b" * 7, "c" * 7
        escaped = (f"Reverted commits:\n\n- {a} renew\n  \\` example <!-- `\n  - {b} retained\n"
                   f"- {c} renew\n")
        self.assertEqual(sd_ship_body._listed_undone(escaped), [a, c])
        wrapped = f"A `span\n<!-- inside` end.\n\nReverted commits:\n\n- {a} renew\n"
        self.assertEqual(sd_ship_body._listed_undone(wrapped), [a])
        cases = {
            "x \\<!-- y\nz\n": set(),  # an escaped `<` opens no comment
            "x \\\\`a <!-- b`\nz\n": set(),  # an escaped backslash leaves the span's backtick
            "x `a\n<!-- b`\nz\n": set(),  # a span closes on the paragraph's next line
            "x `a <!-- b\nz\n": {2, 3},  # no closer in the paragraph: the backtick is text
            "x `a\n\n<!-- b`\nz\n": {4, 5},  # a blank line ends the paragraph and the span
            "x `a\n- <!-- b`\n  c\n- z\n": {3},  # an item ends the paragraph and the span
            "x `a\n- `b\n  <!-- c`\n": set(),  # the item's own span wraps inside the item
        }
        for body, quoted in cases.items():
            with self.subTest(body=body):
                self.assertEqual(sd_ship_body.quoted_lines(body), quoted)

    def test_a_span_ends_with_its_paragraph_as_commonmark_reads_it(self) -> None:
        """pack sd:9666 review, round 14: a second, unmatched span on a wrapped span's closing line took
        the first span's place, so a reread exposed the `<!--` it holds; a backtick in a heading ran into
        the next paragraph; and a lazy line or a marker that cannot interrupt a paragraph ended the span.
        Each expected value is what CommonMark (markdown-it) renders."""
        a, c = "a" * 7, "c" * 7
        tail = f"\n\nReverted commits:\n\n- {a} renew\n"
        undone = {
            "A `span <!-- hidden\nends` and `` unmatched": [a],
            "- `a\nb <!-- c`": [a],  # a lazy line continues the item's paragraph
            "x `a\n2. <!-- b`": [a],  # `2.` cannot interrupt a paragraph
            "- `a\n  2. <!-- b`": [a],
            "- `a\n<!-- b`": [],  # a column-0 `<!--` starts an HTML block: CommonMark hides the rest too
            "- `a\n- <!-- b`": [a],  # a sibling item ends the paragraph; its comment ends with it
        }
        for head, found in undone.items():
            with self.subTest(head=head):
                self.assertEqual(sd_ship_body._listed_undone(head + tail), found)
        self.assertEqual(sd_ship_body.quoted_lines("# Example `\nx <!-- ` end\nLane review fix, pack sd:5\n"),
                         {3, 4})
        lazy = f"Reverted commits:\n\n- {a} renew\nwrapped on\n- {c} renew\n"
        self.assertEqual(sd_ship_body._listed_undone(lazy), [a, c])

    def test_a_paragraph_rule_for_spans_leaves_headings_and_items_alone(self) -> None:
        """pack sd:9666 review, round 15: the round 14 paragraph rules (a lazy line continues its item; a
        marker that cannot interrupt a paragraph is text) decide where a code span ends, yet they also
        moved the headings and items a revert's list is read by. A line that ends with a colon is a
        heading wherever it sits; wrapped text heads nothing; a marker opens an item unless a span is open."""
        a, b, c = "a" * 7, "b" * 7, "c" * 7
        head = "Reverted commits:\n"
        cases = {
            f"{head}\n- {a} reverted\nKept commits:\n- {b} retained\n": [a],  # blocker 1
            f"Revert x\n\n- reason one\n{head}- {a}\n": [a],  # blocker 2, case 1
            f"{head}- {a}\nKept commits:\n- {b}\n": [a],  # blocker 2, case 2
            f"{head}\n- {a} renew\nwrapped on\n  - {c}\n": [a, c],  # blocker 2, case 3
            f"{head}\n- {a} renew\nwrapped on\n- {c} renew\n": [a, c],  # wrapped text ends no section
            f"{head}\n- {a} reverted\nKept commits:\n  - {b} retained\n": [a],
            f"{head}\n- {a}\n  - {c}\n Kept commits:\n  - {b}\n": [a, c],  # a heading one space in
            f"{head}2. {a} renew\n3. {c} renew\n": [a, c],  # a list numbered past 1, no blank line
            f"Notes.\n{head}7) {a} renew\n": [a],
            f"- reason\n{head}===\n- {a}\n": [a],  # an underline under a heading that follows an item
            f"- `x\ny <!-- z`:\n\n{head}\n- {a} renew\n": [a],  # in an open span a colon heads nothing
        }
        for message, found in cases.items():
            with self.subTest(message=message):
                self.assertEqual(sd_ship_body._listed_undone(message), found)
        # A comment opened on a wrapped line ends with its item, as one on an indented line does.
        self.assertEqual(sd_ship_body.quoted_lines("- a\nx <!-- y\n- b\nLane review fix, pack sd:5\n"), set())

    def test_comment_scope_is_commonmarks_whatever_the_revert_list_reads(self) -> None:
        """pack sd:9666 review, round 16: the revert list's plain-text rules (a colon line ends its item;
        `2.` opens an item under a paragraph) also moved where a comment ends, for every citation. A
        citation's comment scope is CommonMark's; the revert list reads both scopes (round 18)."""
        a = "a" * 7
        fix = "Lane review fix, pack sd:5\n"
        cited = {f"- a\nx <!-- y:\n- b\n{fix}": (5,),  # blocker 1
                 f"Notes\n2. <!--\n{fix}-->\n": ()}  # blocker 2
        for body, ids in cited.items():
            with self.subTest(body=body):
                self.assertEqual(sd_ship_body.cited_ids(body, []), ids)
        listed = {
            f"- note\nx <!-- y. Reverted commits:\n- {a} renew\n": [a],  # the comment ends with `- note`
            # Two roles: `2.` is text to CommonMark and the comment holds on; the revert list reads an
            # item, which ends the comment. Round 16 chose CommonMark's; since round 18 either read lists.
            f"Reverted commits:\n2. <!--\n3. {a} example\n-->\n": [a],
            f"Reverted commits:\n2. {a} renew\n": [a],  # the revert list reads `2.` as an item
            f"See `x\nReverted commits:\n2. {a} example\nend` here.\n": [],  # lines in a wrapped code span
            f"Reverted commits:\n\n- {a} renew\nKept commits:\n- {'b' * 7} retained\n": [a],
        }
        for message, found in listed.items():
            with self.subTest(message=message):
                self.assertEqual(sd_ship_body._listed_undone(message), found)

    def test_a_line_a_span_or_a_comment_hides_moves_no_list_item(self) -> None:
        """pack sd:9666 review, round 17: a line inside a code span said nothing but still moved the
        revert list's nesting: its colon ended the item above it, its marker opened a false parent,
        and a comment's line ended an item by its indent. Each dropped the revert heading."""
        a = "a" * 7
        head = "Revert: split\n\n- Reverted commits:\n  See `example\n"
        listed = {
            f"{head}text:\n  end`\n  - {a}\n": [a],  # the blocker: a colon line in the span
            f"{head}  2. more\n     end`\n     - {a}\n": [a],  # its sibling: a marker in the span
            f"{head}  text:\n  end`\n  - {a}\n": [a],  # a colon line in the span, indented, heads nothing
            f"text\n2. Reverted commits:\n   see <!-- note\nhidden\n-->\n   - {a} renew\n": [a],  # a comment's line
            f"{head}- more\n  end`\n  - {a}\n": [],  # a real item ends the span, and holds what nests in it
        }
        for message, found in listed.items():
            with self.subTest(message=message):
                self.assertEqual(sd_ship_body._listed_undone(message), found)
        # The property: a revert reads the same with a line either grammar hides as without it.
        inner = ("text:", "  2. more", "Kept commits:", "     words", "  1.", "  3) x")
        wraps = (("  See `example", "     end`"), ("  see <!-- note", "  -->"))
        cases = itertools.chain(itertools.product(wraps, inner), itertools.product(wraps, inner, inner))
        for ((opener, closer), *lines), plain in itertools.product(cases, (False, True)):
            message = ["- Reverted commits:", opener, *lines, closer, f"     - {a}"]
            for number, kind, *_, spanned in sd_ship_body._blocks("\n".join(message), plain=plain):
                if (spanned or kind == "comment") and 2 < number <= 2 + len(lines):
                    with self.subTest(message=message, without=number, plain=plain):
                        without = message[:number - 1] + message[number:]
                        self.assertEqual(sd_ship_body._listed_undone("\n".join(message)),
                                         sd_ship_body._listed_undone("\n".join(without)))

    def test_a_line_with_two_roles_is_read_both_ways(self) -> None:
        """pack sd:9666 review, round 18: the revert list took from the citation read which lines a code
        span or a comment hides, and left them out. The two grammars can give one line two roles: the
        fence a `10.` item opens is a code span to CommonMark, so the fence's closing line was left
        out and the open fence held the heading and the sha. Rounds 14 to 17 each chose one role;
        now the list is read both ways, and a commit either read lists is undone."""
        a = "a" * 7
        tail = f"    Reverted commits:\n    - {a}\n"
        listed = {
            f"Revert: split\n\nNotes\n10. ```\n    example\n    ```\n{tail}": [a],  # the blocker: a span, or a fence
            f"Revert: split\n\nNotes\n10. ~~~\n    <!-- example\n    ~~~\n{tail}": [a],  # a comment, or a fence's text
            # The other role, which the revert list's own grammar alone drops (round 17 listed these):
            # the colon line ends `1. one` there, so the span runs over `2.`; CommonMark reads an item.
            f"  1. one\n  Reverts:\n    end`\n2. {a}\n      + `y\n": [a],
            f"* x\nReverted commits:\n  x <!-- y\n\n2. {a}\n": [a],  # the same, with a comment
            f"Reverted commits:\n- {a} renew\n- {a} again\n": [a, a],  # one read's repeats stay; the second adds no copy
        }
        for message, found in listed.items():
            with self.subTest(message=message):
                self.assertEqual(sd_ship_body._listed_undone(message), found)

    def test_markers_on_one_line_open_an_empty_parent(self) -> None:
        """pack sd:9666 review, round 12, blocker 3: `- - <sha>` under `Reverted commits:` undid the
        commit, though the empty parent item holds it, as it does across two lines."""
        kept = "b" * 40
        cited = (kept, "fix: shared\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:4\n")
        for entry in (f"- - {kept[:9]} retained", f"1. - {kept[:9]} retained", f"-\n  - {kept[:9]} retained"):
            with self.subTest(entry=entry):
                revert = self.revert_of(f"Reverted commits:\n\n{entry}\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, cited]), (4, 5))

    def test_a_fence_on_a_list_marker_line_is_an_example(self) -> None:
        """pack sd:9666 review, round 5, instance 2: `- ```text` opened no fence, so the sha bullet in the
        example undid a real commit and dropped its citations."""
        undone, quoted = "a" * 40, "c" * 40
        work = [(quoted, "fix: other\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:3\n"),
                (undone, "feat: renew\n\nCloses: sd:2\n")]
        for fence in ("  ```\n", ""):  # closed, or closed by the item's end
            with self.subTest(fence=fence):
                revert = self.revert_of(f"Reverted commits:\n\n- ```text\n  - {quoted[:9]} example\n"
                                        f"{fence}- {undone[:9]} renew\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, *work]), (3, 5))

    def test_a_prose_bullet_holds_the_items_nested_under_it(self) -> None:
        """pack sd:9666 review, round 5, instance 3: an item nested under `- I kept the following commit`
        still read the `Reverted commits:` heading above, and the retained commit lost its citations."""
        undone, kept = "a" * 40, "b" * 40
        work = [(kept, "fix: shared\n\nLane review fix, pack sd:5: y.\n\nCloses: sd:4\n"),
                (undone, "feat: renew\n\nCloses: sd:2\n")]
        revert = self.revert_of(f"Reverted commits:\n\n- {undone[:9]} renew\n- I kept the following commit\n"
                                f"  - {kept[:9]} retained\n")
        self.assertEqual(sd_ship_body.cited_ids("", [revert, *work]), (4, 5))

    def test_every_list_marker_under_a_reverted_heading_undoes(self) -> None:
        """pack sd:9666 review, round 3, instance 3: a `+` or numbered list under `Reverted commits:` undid
        nothing, so the reverted work still closed its item."""
        undone = "a" * 40
        for marker in ("+", "1.", "2)"):
            with self.subTest(marker=marker):
                revert = self.revert_of(f"Reverted commits:\n\n{marker} {undone[:9]} renew\n")
                self.assertEqual(sd_ship_body.cited_ids("", [revert, (undone, "feat: x\n\nCloses: sd:2\n")]), ())

    def test_a_listed_merge_leaves_its_sides_to_the_file_check(self) -> None:
        """pack sd:9666 review, round 8: a listed merge with no `This reverts commit` trailer names no
        mainline, and the first-parent guess undid the wrong side of a `-m 2` revert. The merge alone is
        undone here; `sd_lane.unreverted` reads each side's files at the head. With the trailer, `-m 1`
        drops the side commit."""
        base, side, main, merge = (c * 40 for c in "abce")
        work = [(merge, "Merge branch 'side'"), (main, "fix: mainline\n\nCloses: sd:4"),
                (side, "fix: implement\n\nLane review fix, pack sd:3: y.\n\nCloses: sd:2\n"),
                (base, "fix: base\n\nCloses: sd:5")]
        parents = {merge: (main, side), main: (base,), side: (base,), base: ()}
        listed = self.revert_of(f"Reverted commits:\n\n- {merge[:9]} the side branch\n")
        self.assertEqual(sd_ship_body.cited_ids("", [listed, *work], parents), (4, 2, 5, 3))
        trailed = self.revert_of(f"This reverts commit {merge}, reversing\nchanges made to {main}.\n")
        self.assertEqual(sd_ship_body.cited_ids("", [trailed, *work], parents), (4, 5))

    def test_a_listed_merge_the_trailer_names_keeps_the_trailers_mainline(self) -> None:
        """pack sd:9666 review, round 6, instance 2: a merge listed beside git's `This reverts commit
        <merge>, reversing changes made to <parent two>` undid both sides, and the second parent's
        commit lost its citations. The trailer names the mainline; the list adds nothing to it."""
        base, side, main, merge = (c * 40 for c in "abce")
        revert = self.revert_of(f"Reverted commits:\n\n- {merge[:9]} the side branch\n\n"
                                f"This reverts commit {merge}, reversing\nchanges made to {side}.\n")
        commits = [revert, (merge, "Merge branch 'side'"), (main, "fix: mainline\n\nCloses: sd:4"),
                   (side, "fix: implement\n\nLane review fix, pack sd:3: y.\n\nCloses: sd:2\n"),
                   (base, "fix: base\n\nCloses: sd:5")]
        parents = {merge: (main, side), main: (base,), side: (base,), base: ()}
        self.assertEqual(sd_ship_body.cited_ids("", commits, parents), (2, 5, 3))

    def test_an_ambiguous_listed_prefix_undoes_nothing(self) -> None:
        """pack sd:9666 review, round 3, instance 5: a short sha two branch commits share undid whichever came
        first; git refuses an ambiguous prefix, and so does the list."""
        first, second = "abcdef1" + "1" * 33, "abcdef1" + "2" * 33
        commits = [self.revert_of("Reverted commits:\n\n- abcdef1 one of them\n"),
                   (first, "fix: one\n\nCloses: sd:2\n"), (second, "fix: two\n\nCloses: sd:3\n")]
        self.assertEqual(sd_ship_body.cited_ids("", commits), (2, 3))

    def test_an_html_comment_in_a_revert_list_heads_nothing(self) -> None:
        """pack sd:10090: a comment line after `Reverted commits:` read as a heading that does not say
        reverted, so the listed sha kept its `Closes:` and sd:2 closed. A comment a reader never sees
        heads nothing; one that hides a heading or an item hides it."""
        undone, other = "a" * 40, "b" * 40
        work = [(undone, "feat: renew\n\nCloses: sd:2\n"), (other, "fix: prepare\n\nCloses: sd:3\n")]
        for listing, kept in (
                (f"Reverted commits:\n<!-- why -->\n- {undone[:9]} renew\n- {other[:9]} prepare\n", ()),
                (f"Reverted commits:\n- {undone[:9]} renew\n<!-- why -->\n- {other[:9]} prepare\n", ()),
                (f"Reverted commits: <!-- why -->\n- {undone[:9]} renew\n", (3,)),
                (f"Reverted commits:\n<!--\nwhy\n-->\n- {undone[:9]} renew\n", (3,)),
                (f"- Reverted commits:\n  <!--\n  why\n  -->\n  - {undone[:9]} renew\n", (3,)),
                (f"<!-- Reverted commits: -->\n- {undone[:9]} renew\n", (2, 3)),
                (f"Reverted commits:\n<!-- - {undone[:9]} renew -->\n", (2, 3)),
                (f"Reverted commits:\n<!-- why -->\nKept:\n- {undone[:9]} renew\n", (2, 3)),
                # Review of build/sd-1653: comment syntax in a code span is text, so no sha leads the item.
                (f"Reverted commits:\n- `<!-- -->`{undone[:9]} renew\n", (2, 3)),
                (f"Reverted commits:\n- `<!--` {undone[:9]} -->\n", (2, 3))):
            with self.subTest(listing=listing):
                self.assertEqual(sd_ship_body.cited_ids("", [self.revert_of(listing), *work]), kept)

    def test_the_revert_list_takes_comments_from_the_one_inline_scan(self) -> None:
        """Class pass, review of build/sd-1653 (pack sd:10090): the revert list found comments with its own
        per-line pattern, apart from the scan that tracks code spans, comments, escapes and unclosed runs
        across lines (`_comment_open`). Each construct the two read apart is a row; a comment the scan
        finds reads as a space, so it joins no two fragments into a sha."""
        undone, other = "a" * 40, "b" * 40
        work = [(undone, "feat: renew\n\nCloses: sd:2\n"), (other, "fix: prepare\n\nCloses: sd:3\n")]
        sha = undone[:9]
        for listing, kept in (
                # 1. A code span that closes on a later line: comment syntax in it is text.
                (f"Reverted commits:\n- `<!-- -->{sha} renew\n  end`\n", (2, 3)),
                (f"Reverted commits:\n- ``<!-- -->{sha} renew\n  end``\n", (2, 3)),
                # 2. A comment that opens after text and closes on a later line.
                (f"Reverted commits: <!-- why\nmore -->\n- {sha} renew\n", (3,)),
                (f"Reverted commits: <!-- a --> <!-- b\nc -->\n- {sha} renew\n", (3,)),
                (f"Reverted commits:\n- {sha} renew <!-- why\n  more -->\n- {other[:9]} prepare\n", ()),
                # 3. A comment between two fragments joins them into no sha.
                (f"Reverted commits:\n- {sha[:4]}<!-- -->{sha[4:]} renew\n", (2, 3)),
                # 4. A backtick run its paragraph never closes is text, and the comment after it a comment.
                (f"Reverted commits:\n- `<!-- why --> {sha} renew\n", (2, 3)),
                # 5. An escaped `<` opens no comment; an escaped backtick opens no span.
                (f"Reverted commits:\n- \\<!-- --> {sha} renew\n", (2, 3)),
                (f"Reverted commits:\n\\<!-- kept -->\n- {sha} renew\n", (2, 3)),
                (f"Reverted commits:\n- \\`<!-- -->{sha}` renew\n", (2, 3)),
                # 6. A comment before the sha: the item a reader sees opens with it.
                (f"Reverted commits:\n- <!-- why --> {sha} renew\n", (3,)),
                # 7. A comment open from an earlier line holds the whole line it closes on, as before.
                (f"Reverted commits:\n<!-- why\n--> - {sha} renew\n", (2, 3))):
            with self.subTest(listing=listing):
                self.assertEqual(sd_ship_body.cited_ids("", [self.revert_of(listing), *work]), kept)
        # The property: comment syntax inside a code span, however the span wraps, reads as any other text.
        spans = (("`", "`"), ("``", "``"), ("`a\n  ", "`"), ("`", "\n  b`"), ("``a\n  ", "\n  b``"))
        for (opener, closer), inner in itertools.product(spans, ("<!-- -->", "<!--", "-->", "<!-- x --> <!--")):
            for line in (f"- {opener}{inner}{closer}{sha} renew", f"- {opener}{inner}{sha}{closer} renew",
                         f"- {sha} {opener}{inner}{closer}", f"{opener}{inner}{closer}\n- {sha} renew"):
                listing = f"Reverted commits:\n{line}\n- {other[:9]} prepare\n"
                with self.subTest(listing=listing):
                    self.assertEqual(sd_ship_body._listed_undone(listing),
                                     sd_ship_body._listed_undone(listing.replace(inner, "zz zz")))

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
