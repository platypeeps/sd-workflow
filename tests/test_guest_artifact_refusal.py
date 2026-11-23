"""sd:405 -- the guest-mode artifact refusal, performed rather than described.

`WORKFLOW.md` stated the refusal as a mechanical property: "every writing skill
refuses the upstream tree". Nothing refused anything. The rule existed once, as
a bullet in the planning skill page addressed to an agent, and no code path
consulted `sd_lib.mode()` before a planning artifact was written -- which is how
two checkouts that resolve to `guest` came to carry 162 committed `prd.md` files
between them with nothing objecting on any of those occasions.

These tests are that refusal. The unit half asserts the sentence `sd_lib` hands
back through the injected `ask` seam, so no case here reaches the network. The
end-to-end half runs `bin/sd-review --scope planning` -- the one command
the planning sequence puts between writing the triad and promoting it -- as a
real process against a real repository holding a real `prd.md`, with a `gh` stub
on `PATH` answering for the remote, and asserts exit 2 and one sentence rather
than a traceback.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_lib  # noqa: E402

from tests import row_seed  # noqa: E402

VIEWER = ({"login": "example"}, "")

#: A `gh api` that answers from a fixture: the transport is real, the network
#: is not. `__REPO__` is replaced per case so one stub covers fork and not.
GH_STUB = """#!/bin/sh
case "$2" in
  user) printf '%s' '{"login": "example"}' ;;
  */collaborators) printf '%s' '[{"login": "example", "permissions": {"push": true}}]' ;;
  *) printf '%s' '__REPO__' ;;
esac
"""

FORK_JSON = (
    '{"full_name": "example/thing", "fork": true, '
    '"parent": {"full_name": "acme/thing"}, '
    '"permissions": {"admin": true, "push": true}}'
)
OWN_JSON = (
    '{"full_name": "example/thing", "fork": false, '
    '"permissions": {"admin": true, "push": true}}'
)
#: sd:1347 -- a repository this account may push to but does not administer.
#: The first of the three questions, which no repository row may answer for.
NOT_ADMIN_JSON = (
    '{"full_name": "example/thing", "fork": false, '
    '"permissions": {"admin": false, "push": true}}'
)


def answers(*, full: bool) -> dict:
    """The three answers `remote_permits_full` asks for, yes or a plain no."""

    repo = {
        "full_name": "example/thing",
        "fork": not full,
        "permissions": {"admin": True, "push": True, "pull": True},
    }
    if not full:
        repo["parent"] = {"full_name": "acme/thing"}
    return {
        sd_lib.VIEWER_QUERY: VIEWER,
        sd_lib.REPOSITORY_QUERY: (repo, ""),
        sd_lib.COLLABORATOR_QUERY: ([{"login": "example", "permissions": {"push": True}}], ""),
    }


class Asker:
    """Answers from a fixture and records what it was asked."""

    def __init__(self, table: dict) -> None:
        self.table = table
        self.asked: list[str] = []

    def __call__(self, endpoint: str, root: pathlib.Path):
        self.asked.append(endpoint)
        if endpoint not in self.table:
            raise AssertionError(f"the code asked {endpoint}, which this case does not answer")
        return self.table[endpoint]


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()

    def git(self, cwd: pathlib.Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)

    def make_repo(self, origin: str = "https://github.com/example/thing.git") -> pathlib.Path:
        root = self.tmp / "repo"
        root.mkdir(parents=True)
        self.git(root, "init", "-b", "main")
        self.git(root, "config", "user.email", "test@example.com")
        self.git(root, "config", "user.name", "Test User")
        (root / "README.md").write_text("seed\n", encoding="utf-8")
        self.git(root, "add", "README.md")
        self.git(root, "commit", "-m", "seed")
        self.git(root, "remote", "add", "origin", origin)
        return root

    def write_mode(self, root: pathlib.Path, value: str) -> None:
        (root / sd_lib.LOCAL_FILE_NAME).write_text(
            f"{sd_lib.LOCAL_BLOCK_START}\nmode: {value}\n{sd_lib.LOCAL_BLOCK_END}\n",
            encoding="utf-8",
        )

    def write_item(self, root: pathlib.Path, slug: str = "2026-01-01-a-thing") -> pathlib.Path:
        """A real planning artifact on disk, the shape a planning step writes."""

        item = root / sd_lib.WORK_DIR / slug
        item.mkdir(parents=True)
        (item / "prd.md").write_text(
            "---\n"
            "title: A thing\n"
            "created: 2026-01-01\n"
            "---\n"
            "\n## Requirements\n\n- [ ] one\n",
            encoding="utf-8",
        )
        # The status and branch are the row's; `run_review` reads this home.
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        row_seed.seed_row(home, root, item, "planning", branch="topic")
        return item


class TheRule(Fixture):
    """`sd_lib.guest_artifact_refusal`: the one place the rule lives in code."""

    def test_a_planning_artifact_in_guest_mode_is_refused_by_name(self) -> None:
        root = self.make_repo()
        self.write_mode(root, "guest")
        sentence = sd_lib.guest_artifact_refusal(
            root, ["docs/work/2026-01-01-a-thing/prd.md"], ask=Asker(answers(full=True))
        )
        self.assertIn("guest mode", sentence)
        self.assertIn("docs/work/2026-01-01-a-thing/prd.md", sentence)
        self.assertIn("stay out of the repository on every branch", sentence)  # no integration branch (sd:3136)

    def test_all_three_refused_trees_are_named(self) -> None:
        self.assertEqual(
            sd_lib.GUEST_REFUSED_DIRS, ("docs/work", "docs/spec", "docs/decisions"),
            "the three trees WORKFLOW.md and bin/sd-ship already spell",
        )
        root = self.make_repo()
        self.write_mode(root, "guest")
        for path in ("docs/work/i/prd.md", "docs/spec/a.md", "docs/decisions/d.md"):
            with self.subTest(path=path):
                self.assertTrue(
                    sd_lib.guest_artifact_refusal(root, [path], ask=Asker(answers(full=True)))
                )

    def test_an_unrelated_path_is_not_refused_and_asks_nothing(self) -> None:
        """The pure half runs first, so nothing to refuse never costs a call."""

        root = self.make_repo()
        self.write_mode(root, "guest")
        ask = Asker({})
        self.assertEqual(sd_lib.guest_artifact_refusal(root, ["bin/sd_lib.py"], ask=ask), "")
        self.assertEqual(ask.asked, [])

    def test_a_full_mode_repository_writes_where_it_always_did(self) -> None:
        root = self.make_repo()
        self.assertEqual(
            sd_lib.guest_artifact_refusal(
                root, ["docs/work/i/prd.md"], ask=Asker(answers(full=True))
            ),
            "",
        )

    def test_a_written_full_the_remote_lowers_is_still_refused(self) -> None:
        """Detection is a ceiling: `mode: full` on a fork resolves guest here."""

        root = self.make_repo()
        self.write_mode(root, "full")
        self.assertIn(
            "guest mode",
            sd_lib.guest_artifact_refusal(
                root, ["docs/work/i/prd.md"], ask=Asker(answers(full=False))
            ),
        )

    def test_the_refusal_is_one_sentence_as_its_docstring_says(self) -> None:
        """sd:1000 (16bf4688292a): the refusal carried a second sentence."""
        root = self.make_repo()
        self.write_mode(root, "full")
        said = sd_lib.guest_artifact_refusal(root, ["docs/work/i/prd.md"], ask=Asker(answers(full=False)))
        self.assertTrue(said.endswith("."), said)
        self.assertNotIn(". ", said[:-1], said)
        self.assertIn("detection is a ceiling", said)

    def test_prefixes_that_only_look_like_the_refused_trees_pass(self) -> None:
        root = self.make_repo()
        self.write_mode(root, "guest")
        for path in ("docs/workbook/a.md", "docs/specification.md", "adocs/work/i/prd.md"):
            with self.subTest(path=path):
                self.assertEqual(
                    sd_lib.guest_artifact_refusal(root, [path], ask=Asker({})), ""
                )


class TheDecisionsOptOut(Fixture):
    """`guest_allow: docs/decisions` lets one guest repository take decision records (sd:2168).

    The operator ruled it per repository, opt-in: `docs/decisions/` stays
    refused by default, and `docs/work/` and `docs/spec/` stay refused always.
    """

    PATHS = ["docs/decisions/0001-a-choice.md", "docs/work/2026-01-01-a-thing/prd.md"]

    def guest(self, allow: str | None) -> pathlib.Path:
        root = self.make_repo()
        line = "" if allow is None else f"guest_allow: {allow}\n"
        (root / sd_lib.LOCAL_FILE_NAME).write_text(
            f"{sd_lib.LOCAL_BLOCK_START}\nmode: guest\n{line}{sd_lib.LOCAL_BLOCK_END}\n", encoding="utf-8")
        return root

    def test_by_default_a_decision_record_is_refused(self) -> None:
        root = self.guest(None)
        self.assertEqual(sd_lib.guest_refused_dirs(root), sd_lib.GUEST_REFUSED_DIRS)
        sentence = sd_lib.guest_artifact_refusal(root, self.PATHS[:1], ask=Asker(answers(full=True)))
        self.assertIn("docs/decisions/0001-a-choice.md", sentence)

    def test_the_opt_out_admits_decision_records_and_nothing_else(self) -> None:
        root = self.guest("docs/decisions")
        self.assertEqual(sd_lib.guest_refused_dirs(root), ("docs/work", "docs/spec"))
        sentence = sd_lib.guest_artifact_refusal(root, self.PATHS, ask=Asker(answers(full=True)))
        self.assertNotIn("docs/decisions", sentence)
        self.assertIn("docs/work/2026-01-01-a-thing/prd.md", sentence)
        self.assertEqual(sd_lib.guest_artifact_refusal(root, self.PATHS[:1], ask=Asker(answers(full=True))), "")

    def test_a_tree_that_stays_refused_cannot_be_opted_out(self) -> None:
        for tree in ("docs/work", "docs/spec/", "docs/decisions, docs/work", "docs"):
            with self.subTest(tree=tree):
                root = self.guest(tree)
                with self.assertRaisesRegex(sd_lib.ConfigError, r"guest_allow: only docs/decisions"):
                    sd_lib.guest_refused_dirs(root)
                shutil.rmtree(root)


class TheReviewGate(Fixture):
    """End to end: the write path planning mandates, refused as a process.

    A planning review runs `sd-review --scope planning` over the triad in the
    working tree and step 5 gates `planning -> ready` on that lane, so this is
    where a guest-mode checkout finds out before the artifact is promoted or
    committed. Red against `origin/main`, where `sd-review` consulted no mode.
    """

    def path_holding(self, *tools: str) -> pathlib.Path:
        bindir = self.tmp / "path"
        bindir.mkdir(exist_ok=True)
        for tool in tools:
            found = shutil.which(tool)
            self.assertIsNotNone(found, f"{tool} must be installed to run this test")
            link = bindir / tool
            if not link.exists():
                link.symlink_to(str(found))
        return bindir

    def run_review(self, root: pathlib.Path, repo_json: str) -> subprocess.CompletedProcess:
        bindir = self.path_holding("git", "python3")
        stub = bindir / "gh"
        stub.write_text(GH_STUB.replace("__REPO__", repo_json), encoding="utf-8")
        stub.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = str(bindir)
        env["HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir(exist_ok=True)
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "sd-review"), "--scope", "planning", "--explain"],
            cwd=str(root), env=env, capture_output=True, text=True, timeout=300,
        )

    def test_guest_mode_refuses_the_active_items_planning_documents(self) -> None:
        root = self.make_repo()
        self.write_item(root)
        done = self.run_review(root, FORK_JSON)
        self.assertEqual(done.returncode, 2, f"stdout={done.stdout}\nstderr={done.stderr}")
        self.assertIn("guest mode", done.stderr)
        self.assertIn("docs/work/2026-01-01-a-thing/prd.md", done.stderr)
        self.assertIn("stay out of the repository on every branch", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_the_same_repository_unforked_reaches_the_reviewer(self) -> None:
        """The control: the refusal is about the mode, not about the fixture.

        The success is asserted positively, because absence is not evidence.
        Written as two `assertNotIn`s alone this passed on any failure that did
        not happen to use the guest wording -- a usage error, a missing `gh`, an
        `--explain` that fell over for its own reasons -- so the one thing its
        name claims, that the reviewer was reached, was the one thing nothing in
        it established. The exit code says the run succeeded and the explanation
        naming the scope says it was this review that produced it.
        """

        root = self.make_repo()
        self.write_item(root)
        done = self.run_review(root, OWN_JSON)
        self.assertEqual(done.returncode, 0,
                         f"stdout={done.stdout}\nstderr={done.stderr}")
        # The explanation's own scope row and footer, not the bare word (sd:1002).
        self.assertRegex(done.stdout, r"(?m)^  scope +planning  \(")
        self.assertRegex(done.stdout, r"(?m)^sd-review: explain only[,;] ")
        self.assertNotIn("Traceback", done.stderr)
        self.assertNotIn("guest mode", done.stderr)
        self.assertNotIn("stay out of the repository", done.stderr)


#: sd:789 -- a `gh api` for the ship adapter's own transport, which appends a
#: query string to the collaborator page it asks for. `__PEOPLE__` is the
#: collaborator list, so one stub covers a remote the operator alone holds and
#: one a second collaborator may push to.
SHIP_GH_STUB = """#!/bin/sh
case "$2" in
  user) printf '%s' '{"login": "example"}' ;;
  */collaborators*) printf '%s' '__PEOPLE__' ;;
  *) printf '%s' '__REPO__' ;;
esac
"""

#: sd:789 -- the other half of the demotion: a `gh` that cannot answer at all.
#: The adapter's `run` turns a non-zero exit into a `Refusal`, `_mode_ask`
#: hands that back as the error half of the pair, and `remote_permits_full`
#: reads it as `answered=False` -- guest, with no answer behind it.
UNREACHABLE_GH_STUB = """#!/bin/sh
echo 'gh: could not resolve host: api.github.com' >&2
exit 1
"""

ALONE = '[{"login": "example", "permissions": {"push": true}}]'
WITH_MALLORY = '[{"login": "example", "permissions": {"push": true}}, {"login": "mallory", "permissions": {"push": true}}]'

#: sd:853 -- the second remote, whose name has the first one's as a prefix.
#: `example/thing` is a prefix of `example/thing-two`, and the demotion marker is
#: built from the name, so the shorter marker is a prefix of the longer note's
#: body. That is the collision the idempotence key has to survive.
OWN_TWO_JSON = (
    '{"full_name": "example/thing-two", "fork": false, '
    '"permissions": {"admin": true, "push": true}}'
)


class TheDemotionAnswer(Fixture):
    """`sd_lib.mode_answer`: the mode, and the answer that lowered it if one did.

    sd:789. `sd_lib.mode` handed back a word, and the word `guest` cannot say
    whether the operator wrote it or the remote imposed it, nor why. The
    demotion note needs the second thing, so the resolver returns it.
    """

    def test_a_written_full_the_remote_lowers_returns_the_answer(self) -> None:
        root = self.make_repo()
        self.write_mode(root, "full")
        resolved, lowered = sd_lib.mode_answer(root, ask=Asker(answers(full=False)))
        self.assertEqual(resolved, "guest")
        self.assertEqual(lowered, sd_lib.RemoteAnswer(False, True, "example/thing is a fork of acme/thing",
                                                      sd_lib.FORK_QUESTION))

    def test_a_full_the_remote_confirms_was_not_lowered(self) -> None:
        root = self.make_repo()
        self.assertEqual(sd_lib.mode_answer(root, ask=Asker(answers(full=True))), ("full", None))

    def test_a_written_guest_was_not_lowered_and_asks_nothing(self) -> None:
        root = self.make_repo()
        self.write_mode(root, "guest")
        ask = Asker({})
        self.assertEqual(sd_lib.mode_answer(root, ask=ask), ("guest", None))
        self.assertEqual(ask.asked, [])

    def test_the_word_alone_still_comes_from_mode(self) -> None:
        root = self.make_repo()
        self.write_mode(root, "full")
        self.assertEqual(sd_lib.mode(root, ask=Asker(answers(full=False))), "guest")

    def test_the_readers_of_the_word_alone_are_enumerated_not_recited(self) -> None:
        """sd:789 -- `mode`'s docstring names who still wants the word alone.

        This change moved one of them: `sd-status` prints the reason beside
        the word now, so it reads `mode_answer` and is no longer a reader of
        `mode`. A recited list drifts the next time a caller moves, so the
        list is checked against the callers `bin/` actually holds.
        """

        callers = set()
        for path in sorted((REPO_ROOT / "bin").iterdir()):
            if not path.is_file() or path.name == "sd_lib.py":
                continue
            if "sd_lib.mode(" in path.read_text(encoding="utf-8", errors="replace"):
                callers.add(path.name)
        self.assertEqual(callers, {"sd_suggest.py", "sd_setup_github.py"}, sorted(callers))
        status = (REPO_ROOT / "bin" / "sd-status").read_text(encoding="utf-8")
        self.assertIn("sd_lib.mode_answer(", status)
        self.assertNotIn("sd_lib.mode(", status)
        doc = sd_lib.mode.__doc__ or ""
        for named in ("sd-suggest", "GitHub setup", "guest_artifact_refusal"):
            self.assertIn(named, doc, f"{named} reads `mode` and the docstring does not say so")
        self.assertIn("Not `sd-status`", doc, "sd-status reads `mode_answer` now; the docstring says which")

    def test_the_note_names_the_remote_first_and_the_answer_second(self) -> None:
        marker, body = sd_lib.demotion_note(
            "example/thing", sd_lib.RemoteAnswer(False, True, "example/thing lets mallory push too")
        )
        self.assertEqual(marker, "Mode demoted to guest on example/thing")
        self.assertTrue(body.startswith(marker + "\n"), body)
        self.assertIn("the remote answered: example/thing lets mallory push too", body)
        self.assertIn("shared tree", body)

    def test_an_unanswered_question_is_said_as_one(self) -> None:
        _, body = sd_lib.demotion_note(
            "example/thing", sd_lib.RemoteAnswer(False, False, "gh could not be run: no such file")
        )
        self.assertIn("the remote could not be asked: gh could not be run", body)
        self.assertNotIn("the remote answered", body)

    def test_a_remote_that_could_not_be_asked_is_returned_as_the_demotion_too(self) -> None:
        """sd:789 -- `answered=False` lowers the mode, so it is handed back.

        The two demotions differ in one word of the note and in nothing else:
        a remote that says `no` and a remote that cannot be asked both leave
        a run that was written `full` running as `guest`. `demotion_note`
        composes a body for the second ("the remote could not be asked: ..."),
        and `Ship.resolve_mode` writes whenever the second value is not None.
        So `mode_answer` must return the answer on this route as well -- a
        `None` here would drop the note silently for the one case whose
        reason the operator cannot go and read off the remote afterwards.
        """

        root = self.make_repo()
        self.write_mode(root, "full")
        unreachable = "gh could not be run: [Errno 2] No such file or directory: 'gh'"
        ask = Asker({sd_lib.VIEWER_QUERY: (None, unreachable)})
        resolved, lowered = sd_lib.mode_answer(root, ask=ask)
        self.assertEqual(resolved, "guest")
        self.assertIsNotNone(lowered, "the unreachable remote lowered the mode; the note needs its answer")
        assert lowered is not None  # for the type checker; the assertion above is the test
        self.assertFalse(lowered.full)
        self.assertFalse(lowered.answered, "the question was not put, so it was not answered")
        self.assertIn(unreachable, lowered.reason)
        # The rest of the route, from this answer to the sentence on the item.
        marker, body = sd_lib.demotion_note("example/thing", lowered)
        self.assertTrue(body.startswith(marker + "\n"), body)
        self.assertIn("the remote could not be asked", body)
        self.assertNotIn("the remote answered", body)

    def test_a_repository_name_that_prefixes_another_does_not_share_its_key(self) -> None:
        """sd:853 -- the idempotence key is the marker *line*, not the marker.

        The marker is built from the repository name, so one marker is a
        prefix of another whenever one name is a prefix of another --
        `example/thing` of `example/thing-two`, which is what a renamed or forked
        sibling looks like. Looking a note up by the bare marker would read
        the longer name's note as the shorter name's own. The key carries the
        terminator, and no repository name holds a newline, so it ends at the
        name and matches one marker only.
        """

        answer = sd_lib.RemoteAnswer(False, True, "someone else may push too")
        short_marker, short_body = sd_lib.demotion_note("example/thing", answer)
        long_marker, long_body = sd_lib.demotion_note("example/thing-two", answer)
        self.assertTrue(
            long_marker.startswith(short_marker),
            "the premise of this test: the bare markers do collide",
        )
        short_key = sd_lib.demotion_note_key(short_marker)
        long_key = sd_lib.demotion_note_key(long_marker)
        self.assertTrue(short_body.startswith(short_key))
        self.assertTrue(long_body.startswith(long_key))
        self.assertFalse(
            long_body.startswith(short_key),
            "example/thing-two's note must not answer for example/thing",
        )
        self.assertFalse(short_body.startswith(long_key))

    def test_the_written_modes_detection_leaves_alone_are_stated_once(self) -> None:
        """sd:854 -- `remote_can_lower` is the one statement of that rule.

        `mode_answer` returns a `None` demotion for a written `guest` or
        `minimal` because the remote is never asked about one. `sd-ship`'s
        merge-time ownership check reaches `remote_permits_full` by a
        different route, so it has to ask the same question itself -- and it
        used to answer it by re-reading the local block and re-listing the two
        words at the call site. Both now read the one predicate, and this
        pins the two against each other over every value `mode:` can hold.
        """

        root = self.make_repo()
        lowering = answers(full=False)
        for written in ("", *sd_lib.MODES):
            with self.subTest(written=written):
                if written:
                    self.write_mode(root, written)
                else:
                    (root / sd_lib.LOCAL_FILE_NAME).unlink(missing_ok=True)
                resolved, lowered = sd_lib.mode_answer(root, ask=Asker(lowering))
                self.assertEqual(
                    sd_lib.remote_can_lower(sd_lib.written_mode(root)),
                    lowered is not None,
                    f"mode_answer and remote_can_lower disagree about {written!r} -> {resolved}",
                )
        self.assertEqual(sorted(sd_lib.SETTLED_MODES), ["guest", "minimal"])
        self.assertTrue(set(sd_lib.SETTLED_MODES) < set(sd_lib.MODES))

    def test_the_merge_time_check_reads_the_rule_and_does_not_restate_it(self) -> None:
        """sd:854 -- the call site no longer carries its own copy of the rule."""

        source = (REPO_ROOT / "bin" / "sd-ship").read_text(encoding="utf-8")
        start = source.index("    def merge_ownership(")
        body = source[start:source.index("    def note_demotion(", start)]
        self.assertIn("sd_lib.remote_can_lower(", body)
        self.assertNotIn("sd_lib.local_block(", body)
        self.assertNotIn('"minimal"', body, "the list of settled modes lives in sd_lib, once")


class ShipMergeFixture(Fixture):
    """The `Ship` harness the demotion and merge-authority cases share.

    Criterion 11 of sd:10: a `mode: full` repository gains a second
    collaborator, `sd-ship` refuses to push the triad to that remote, "and the
    item carries a demotion note". `remote_permits_full` had two callers, the
    mode resolver and the ownership check, and neither wrote one. Both go
    through `Ship` here: `resolve_mode`, which `prepare` calls before the body
    and before the push, and `owned`, which `merge` calls at merge time. A
    fixture database in a scratch HOME, a real repository, and a `gh` stub on
    PATH answering for the remote; nothing here reaches the operator's
    database or the network.
    """

    def setUp(self) -> None:
        super().setUp()
        import importlib.machinery
        import importlib.util

        try:
            from sd_db import connect, create_item, initialise, upsert_repo
        except ImportError as error:  # pragma: no cover - CI installs the library
            self.skipTest(f"sd_db is not importable here: {error}")
        home = self.tmp / "home"
        home.mkdir()
        self.database = home / ".local/share/sd/sd.db"
        initialise(self.database)
        self.connection = connect(self.database)
        self.addCleanup(self.connection.close)
        self.root = self.make_repo()
        self.git(self.root, "checkout", "-b", "topic")
        self.write_mode(self.root, "full")
        # `runner_merge: auto` on the row, so the merge-time case can assert that
        # a demotion changes the repository's `runner_merge` not at all: the
        # policy is the operator's standing setting, and a remote that gained
        # a collaborator is a reason to stop this merge, not to rewrite it.
        upsert_repo(self.connection, str(self.root), remote="https://github.com/example/thing.git",
                    runner_merge="auto")
        self.item = create_item(
            self.connection, kind="work", title="a thing", status="in_progress", repo=str(self.root), branch="topic"
        )
        loader = importlib.machinery.SourceFileLoader("sd_ship_demotion_tested", str(REPO_ROOT / "bin" / "sd-ship"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        self.ship = importlib.util.module_from_spec(spec)
        loader.exec_module(self.ship)
        self.bindir = self.tmp / "path"
        self.bindir.mkdir()
        for tool in ("git", "python3"):
            found = shutil.which(tool)
            self.assertIsNotNone(found, f"{tool} must be installed to run this test")
            (self.bindir / tool).symlink_to(str(found))
        self.environment = dict(os.environ, HOME=str(home), PATH=str(self.bindir))
        self.patched = mock.patch.dict(os.environ, self.environment, clear=True)
        self.patched.start()
        self.addCleanup(self.patched.stop)

    def runner_merge(self, value: str) -> None:
        """Set the repository row's `runner_merge`, the operator's standing decision."""

        from sd_db import upsert_repo

        upsert_repo(self.connection, str(self.root), remote="https://github.com/example/thing.git",
                    runner_merge=value)

    def remote(self, people: str, repo: str = OWN_JSON) -> None:
        stub = self.bindir / "gh"
        stub.write_text(SHIP_GH_STUB.replace("__PEOPLE__", people).replace("__REPO__", repo), encoding="utf-8")
        stub.chmod(0o755)

    def unreachable_remote(self) -> None:
        """A `gh` that fails every call: the remote that cannot be asked."""

        stub = self.bindir / "gh"
        stub.write_text(UNREACHABLE_GH_STUB, encoding="utf-8")
        stub.chmod(0o755)

    def operation(self, command: str = "prepare"):
        extra = ["--manual", "--expected-head", "HEAD"] if command == "merge" else []
        args = self.ship.parser().parse_args([command, "--item", str(self.item), "--json", *extra])
        return self.ship.Ship(self.root, self.connection, self.database, args)

    def notes(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT kind, body FROM note WHERE item = ? AND body LIKE 'Mode demoted%' ORDER BY id", (self.item,)
        ).fetchall()
        for row in rows:
            self.assertEqual(row["kind"], sd_lib.DEMOTION_NOTE_KIND)
        return [row["body"] for row in rows]


class TheDemotionNote(ShipMergeFixture):
    """sd:789 -- the note `sd-ship` writes on the item when the remote lowers the mode.

    Criterion 11 of sd:10: a `mode: full` repository gains a second
    collaborator, `sd-ship` refuses to push the triad to that remote, "and the
    item carries a demotion note". `remote_permits_full` had two callers, the
    mode resolver and the ownership check, and neither wrote one.
    """

    def test_a_collaborator_the_remote_names_lowers_the_mode_and_the_item_carries_the_note(self) -> None:
        self.remote(WITH_MALLORY)
        self.assertEqual(self.operation().resolve_mode(), "guest")
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith("Mode demoted to guest on example/thing\n"), notes[0])
        self.assertIn("example/thing lets mallory push too", notes[0])
        self.assertEqual(
            (self.root / sd_lib.LOCAL_FILE_NAME).read_text(encoding="utf-8").count("mode: full"), 1,
            "the written line is the operator's; detection never edits it",
        )

    def test_a_remote_that_cannot_be_reached_lowers_the_mode_and_the_item_carries_the_note(self) -> None:
        """sd:789 -- the unanswerable remote, through `Ship.resolve_mode`.

        The consumer of `mode_answer`'s second value. `gh` here exits non-zero
        on every call, which is the shape of no network and of a remote that
        refused the call; the run comes out `guest` and the note says the
        remote could not be asked, rather than no note at all.
        """

        self.unreachable_remote()
        self.assertEqual(self.operation().resolve_mode(), "guest")
        notes = self.notes()
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith("Mode demoted to guest on example/thing\n"), notes[0])
        self.assertIn("the remote could not be asked", notes[0])
        self.assertIn("could not resolve host", notes[0])
        self.assertNotIn("the remote answered", notes[0])
        self.assertEqual(
            (self.root / sd_lib.LOCAL_FILE_NAME).read_text(encoding="utf-8").count("mode: full"), 1,
            "a remote that could not be asked is not permission to edit the written line either",
        )

    def test_a_second_lowering_for_the_same_item_and_remote_writes_no_second_note(self) -> None:
        self.remote(WITH_MALLORY)
        delivery = self.operation()
        self.assertEqual(delivery.resolve_mode(), "guest")
        # `prepare` resolves twice, before the body and before the push, and
        # a rerun after the refusal resolves twice more. One note.
        self.assertEqual(delivery.resolve_mode(), "guest")
        self.assertEqual(self.operation().resolve_mode(), "guest")
        self.assertEqual(len(self.notes()), 1, self.notes())

    def test_a_remote_the_operator_alone_holds_lowers_nothing_and_writes_nothing(self) -> None:
        self.remote(ALONE)
        self.assertEqual(self.operation().resolve_mode(), "full")
        self.assertEqual(self.notes(), [])

    def test_a_written_guest_is_not_a_demotion(self) -> None:
        self.write_mode(self.root, "guest")
        self.remote(WITH_MALLORY)
        self.assertEqual(self.operation().resolve_mode(), "guest")
        self.assertEqual(self.notes(), [])

    def test_the_merge_time_refusal_names_the_collaborator_and_leaves_the_same_note(self) -> None:
        """The ownership check `merge` runs is the other caller, and it shares the marker.

        The row says `manual` here. Since sd:1347 the row is what decides
        whether co-ownership stops a merge, so the refusing path is the one a
        `manual` row selects; the `auto` row's override is
        `TheRowAuthorizedMerge` below. The final assertion is unchanged in
        substance: a demotion stops this merge and rewrites no policy.
        """

        self.runner_merge("manual")
        self.remote(WITH_MALLORY)
        delivery = self.operation("merge")
        with self.assertRaisesRegex(self.ship.Refusal, "mallory"):
            delivery.merge_ownership()
        self.assertEqual(len(self.notes()), 1, self.notes())
        # sd:1000 (3057e71978d9): the branch was pushed before this answer, so
        # the note must not say artifacts were held back.
        self.assertNotIn("were not pushed", self.notes()[0])
        self.assertIn("came at merge time", self.notes()[0])
        # The push-time lowering and the merge-time one are the same demotion.
        self.assertEqual(delivery.resolve_mode(), "guest")
        self.assertEqual(len(self.notes()), 1, self.notes())
        self.assertEqual(
            self.connection.execute(
                "SELECT runner_merge FROM repo WHERE path = ?", (str(self.root),)
            ).fetchone()["runner_merge"],
            "manual",
            "a demotion stops this merge; it does not rewrite the operator's standing policy",
        )

    def test_a_remote_that_lets_the_merge_through_writes_nothing(self) -> None:
        self.remote(ALONE)
        self.assertEqual(self.operation("merge").merge_ownership().get("full_name"), "example/thing")
        self.assertEqual(self.notes(), [])

    def repoint(self, origin: str, repo_json: str, people: str = WITH_MALLORY) -> None:
        """Move origin, the row's remote and the stub's answer together.

        `Ship.__init__` refuses a checkout whose origin and row disagree, so
        the three have to move as one. This is the shape of a repository that
        was renamed or moved between two runs against the same item.
        """

        from sd_db import upsert_repo

        self.git(self.root, "remote", "set-url", "origin", origin)
        upsert_repo(self.connection, str(self.root), remote=origin,
                    runner_merge="auto")
        self.remote(people, repo_json)

    def test_a_second_remote_whose_name_extends_the_first_still_gets_its_own_note(self) -> None:
        """sd:853 -- one note per item *and remote*, when one name prefixes the other.

        The idempotence key was the first `len(marker)` characters of the
        body, and the marker is built from the repository name. `example/thing`
        is a prefix of `example/thing-two`, so once the longer name had left its
        note the shorter name's lookup matched it and wrote nothing: the
        remote actually refusing the push left no reason on the item, and the
        note that was there named a different remote.
        """

        self.repoint("https://github.com/example/thing-two.git", OWN_TWO_JSON)
        self.assertEqual(self.operation().resolve_mode(), "guest")
        self.repoint("https://github.com/example/thing.git", OWN_JSON)
        self.assertEqual(self.operation().resolve_mode(), "guest")
        notes = self.notes()
        self.assertEqual(len(notes), 2, notes)
        self.assertTrue(notes[0].startswith("Mode demoted to guest on example/thing-two\n"), notes[0])
        self.assertTrue(notes[1].startswith("Mode demoted to guest on example/thing\n"), notes[1])
        # And the key is still a key: neither remote writes a second note.
        self.assertEqual(self.operation().resolve_mode(), "guest")
        self.assertEqual(len(self.notes()), 2, self.notes())

    def test_a_written_guest_is_not_a_demotion_at_merge_time_either(self) -> None:
        """sd:854 -- the merge-time reader applies the written-mode rule too.

        `merge_ownership` does not reach `remote_permits_full` through
        `mode_answer`, so the `None` that resolver returns for a written
        `guest` never arrives here: the answer it holds is a plain `no` from
        the ownership check, and a `no` about a repository already written
        down as `guest` lowered nothing. Nothing pinned that half before
        sd:854 -- only the `resolve_mode` half was covered -- so dropping the
        guard wrote a demotion note for a demotion that never happened.
        """

        self.write_mode(self.root, "guest")
        self.runner_merge("manual")
        self.remote(WITH_MALLORY)
        delivery = self.operation("merge")
        with self.assertRaisesRegex(self.ship.Refusal, "mallory"):
            delivery.merge_ownership()
        self.assertEqual(self.notes(), [], "a written guest was not lowered by anyone")

    def test_prepare_and_merge_resolve_through_the_noting_methods(self) -> None:
        """The wiring: the two `prepare` resolutions and the `merge` ownership read.

        A `Ship` method nobody calls writes no note. `prepare` is not run here
        end to end -- its harness is `tests/test_sd_ship.py` -- so the call
        sites are pinned by reading the adapter: two mode resolutions, two
        ownership reads, both in `merge`, and no remaining direct call to
        `sd_lib.mode` or, outside the wrapper, to `GitHub.owned`.
        """

        source = (REPO_ROOT / "bin" / "sd-ship").read_text(encoding="utf-8")
        self.assertEqual(source.count("mode = self.resolve_mode()"), 2)
        self.assertEqual(source.count("sd_lib.mode("), 0)
        self.assertEqual(source.count("self.merge_ownership()"), 2)
        self.assertEqual(source.count("self.api.owned()"), 1, "only the wrapper reads it now")



class TheQuestionThatAnswered(Fixture):
    """sd:1347 -- `RemoteAnswer` says *which* question said no, and `coownership_only` reads it.

    The merge gate has to tell a co-ownership answer from the other two and
    from no answer at all. Matching the refusal's wording would do neither: it
    breaks when the sentence changes, and "lets ... push too" cannot be
    distinguished from a question that was never put. So the question is a
    named field, and the gate's test is a positive predicate over it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.root = self.make_repo()

    def answer_for(self, repo: dict, people: list) -> sd_lib.RemoteAnswer:
        return sd_lib.remote_permits_full(self.root, ask=Asker({
            sd_lib.VIEWER_QUERY: VIEWER,
            sd_lib.REPOSITORY_QUERY: (repo, ""),
            sd_lib.COLLABORATOR_QUERY: (people, ""),
        }))

    def test_each_refusing_question_names_itself(self) -> None:
        alone = [{"login": "example", "permissions": {"push": True}}]
        crowd = alone + [{"login": "mallory", "permissions": {"push": True}}]
        own = {"full_name": "example/thing", "fork": False, "permissions": {"admin": True, "push": True}}

        admin = self.answer_for({**own, "permissions": {"admin": False, "push": True}}, alone)
        self.assertEqual(admin.question, sd_lib.ADMIN_QUESTION)
        fork = self.answer_for({**own, "fork": True, "parent": {"full_name": "acme/thing"}}, alone)
        self.assertEqual(fork.question, sd_lib.FORK_QUESTION)
        coowned = self.answer_for(own, crowd)
        self.assertEqual(coowned.question, sd_lib.COOWNED_QUESTION)
        self.assertEqual(coowned.others, ("mallory",), "the receipt lists the names; it does not parse the sentence")
        permitted = self.answer_for(own, alone)
        self.assertEqual((permitted.full, permitted.question, permitted.others), (True, "", ()))

    def test_only_an_answered_coownership_no_passes_the_predicate(self) -> None:
        alone = [{"login": "example", "permissions": {"push": True}}]
        crowd = alone + [{"login": "mallory", "permissions": {"push": True}}]
        own = {"full_name": "example/thing", "fork": False, "permissions": {"admin": True, "push": True}}

        self.assertTrue(sd_lib.coownership_only(self.answer_for(own, crowd)))
        self.assertFalse(sd_lib.coownership_only(self.answer_for(own, alone)), "a yes is not an override")
        self.assertFalse(sd_lib.coownership_only(
            self.answer_for({**own, "permissions": {"admin": False, "push": True}}, alone)))
        self.assertFalse(sd_lib.coownership_only(
            self.answer_for({**own, "fork": True, "parent": {"full_name": "acme/thing"}}, alone)))
        self.assertFalse(sd_lib.coownership_only(None))
        # The unanswerable remote: `remote_permits_full`'s rule that a question
        # nobody could put is not a permission has to survive this gate too.
        unanswered = sd_lib.remote_permits_full(self.root, ask=Asker(
            {sd_lib.VIEWER_QUERY: (None, "gh could not be run")}))
        self.assertFalse(unanswered.answered)
        self.assertEqual(unanswered.question, "")
        self.assertFalse(sd_lib.coownership_only(unanswered))


class TheRowAuthorizedMerge(ShipMergeFixture):
    """sd:1347 -- the repository row overrides the co-ownership answer, and only that one.

    Merge authority was repository ownership, and its third question --
    "nobody else may push" -- is false of every co-authored repository, so
    `sd-ship merge` refused sd:1337 in `example-org/benchmark` and the
    whole Mezmo lane ended at `ready_to_send` for a human to finish by hand.
    The row is the operator's per-repository decision about exactly that, so
    `runner_merge: auto` answers the third question and nothing else.

    The fixture is `TheDemotionNote`'s: a real repository, a real database in
    a scratch HOME, a `gh` stub on PATH. The row it writes says `auto`.
    """

    def forget_the_row(self) -> None:
        """Leave the gate with nothing to read, after identity has been resolved.

        `item.repo` is `REFERENCES repo(path) ON DELETE RESTRICT`, so an
        item-bound merge can never *reach* this gate with its row missing --
        the database forbids removing it. The state is real all the same: a
        `--no-item` merge runs from a checkout whose origin matches no row at
        all, and lands on the same lookup with the same nothing to read. The
        constraint is lifted for the delete alone, because the point here is
        what the gate does when the answer is absent, not how it got absent.
        """
        self.connection.execute("PRAGMA foreign_keys = OFF")
        self.connection.execute("DELETE FROM repo WHERE path = ?", (str(self.root),))
        self.connection.commit()
        self.connection.execute("PRAGMA foreign_keys = ON")

    def test_a_coowned_repository_the_row_says_auto_for_merges(self) -> None:
        self.runner_merge("auto")
        self.remote(WITH_MALLORY)
        delivery = self.operation("merge")
        self.assertEqual(delivery.merge_ownership().get("full_name"), "example/thing")
        self.assertEqual(self.notes(), [], "nothing was lowered, so nothing was demoted")

    def test_the_authority_names_the_other_pushers_and_is_not_durable_yet(self) -> None:
        """The audit trail for a loosened guard, held until a merge is dispatched.

        The record is complete here -- repository, row value, who else may
        push, and what the remote said -- because a reader of the receipt must
        be able to tell this from a merge the account owned outright without
        going back to the remote. What it is not is written: ownership
        clearing is not merging, and `merge` runs CI, the review gate and the
        protection re-read after this point. `merge` stores it with the
        dispatch phase, and `tests/test_sd_ship.py` takes that end to end.
        """

        self.runner_merge("auto")
        self.remote(WITH_MALLORY)
        delivery = self.operation("merge")
        delivery.merge_ownership()
        recorded = delivery.row_authority
        self.assertEqual(recorded["repository"], "example/thing")
        self.assertEqual(recorded["runner_merge"], "auto")
        self.assertEqual(recorded["other_pushers"], ["mallory"])
        self.assertIn("mallory", recorded["remote_said"])
        self.assertNotIn("row_authorized_merge", delivery.state)
        self.assertNotIn("row_authorized_merge", delivery.merge_extras())
        # Twice, because `merge` reads ownership twice: still the same answer,
        # and still nothing durable.
        delivery.merge_ownership()
        self.assertEqual(delivery.row_authority, recorded)
        self.assertNotIn("row_authorized_merge", self.operation("merge").state)

    def test_an_ownership_only_read_retracts_an_earlier_read_s_authority(self) -> None:
        """A second ownership read that needs no row takes the first one's claim back.

        `merge` reads ownership more than once, and the reads can disagree: a
        collaborator removed between them turns a row-authorized answer into
        an ownership-only one. The dispatch stores whatever the last read
        left, so the last read has to say `None` rather than say nothing, or
        the merge would carry an authority it no longer needs.
        """

        self.runner_merge("auto")
        self.remote(WITH_MALLORY)
        delivery = self.operation("merge")
        delivery.merge_ownership()
        self.assertIsNotNone(delivery.row_authority)
        self.remote(ALONE)
        delivery.merge_ownership()
        self.assertIsNone(delivery.row_authority)

    def test_a_coowned_repository_the_row_says_manual_for_still_refuses(self) -> None:
        self.runner_merge("manual")
        self.remote(WITH_MALLORY)
        with self.assertRaisesRegex(self.ship.Refusal, "mallory"):
            self.operation("merge").merge_ownership()

    def test_a_coowned_repository_with_no_row_still_refuses(self) -> None:
        """An absent row is not a grant; neither is a database that cannot be read."""

        self.runner_merge("auto")
        self.remote(WITH_MALLORY)
        delivery = self.operation("merge")
        self.forget_the_row()
        with self.assertRaisesRegex(self.ship.Refusal, "mallory"):
            delivery.merge_ownership()
        self.assertNotIn("row_authorized_merge", delivery.state)

    def test_a_database_that_cannot_be_read_is_not_a_grant(self) -> None:
        """Every way of not getting an explicit `auto` refuses, including a broken store."""

        import sqlite3

        self.runner_merge("auto")
        self.remote(WITH_MALLORY)
        delivery = self.operation("merge")
        self.assertTrue(delivery.row_merges(), "the premise: this row does say auto")
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        delivery.connection = empty
        self.assertFalse(delivery.row_merges(), "no readable repo table is not a grant")

    def test_a_row_for_another_repository_does_not_authorize_this_merge(self) -> None:
        """The row that answers must be the row for the repository being merged.

        `registered_for` returns the checkout's own row the moment its path is
        registered, without consulting the origin, so a row whose `remote`
        went stale -- one `git remote set-url origin` away, and nothing
        watches for it -- would hand this gate an `auto` the operator set for
        a different repository. The receipt cannot show the substitution: it
        is built from the live remote answer, so it would name this
        repository truthfully while the authority came from another. The row
        is made stale after identity resolved, because `Ship.__init__`
        refuses a checkout whose origin and item row disagree and a stale row
        is what a later run finds.
        """

        self.runner_merge("auto")
        self.remote(WITH_MALLORY)
        delivery = self.operation("merge")
        self.connection.execute("UPDATE repo SET remote = ? WHERE path = ?",
                                ("https://github.com/example/elsewhere.git", str(self.root)))
        self.connection.commit()
        self.assertFalse(delivery.row_merges(), "example/elsewhere's policy does not speak for example/thing")
        with self.assertRaisesRegex(self.ship.Refusal, "mallory"):
            delivery.merge_ownership()
        self.assertNotIn("row_authorized_merge", delivery.state)

    def test_an_ssh_checkout_matches_an_https_row_for_the_same_repository(self) -> None:
        """One repository written two ways is still one repository.

        The comparison is on the slug and not on `sd_db.repos.same_remote`,
        which normalises a `.git` suffix and a trailing slash and nothing
        else -- deliberately, so it reads these two forms as two repositories.
        Rows on a machine may hold one form while the checkouts use the other,
        which is the ordinary state here, so a stricter comparison would
        refuse the common case rather than the wrong-row one.
        """

        self.git(self.root, "remote", "set-url", "origin", "git@github.com:example/thing.git")
        self.runner_merge("auto")  # stores the https form
        self.remote(WITH_MALLORY)
        delivery = self.operation("merge")
        self.assertTrue(delivery.row_merges())
        self.assertEqual(delivery.merge_ownership().get("full_name"), "example/thing")
        self.assertEqual(self.notes(), [])

    def test_a_repository_the_account_does_not_administer_still_refuses(self) -> None:
        """A row cannot grant admin the account does not hold; GitHub would refuse anyway."""

        self.runner_merge("auto")
        self.remote(ALONE, NOT_ADMIN_JSON)
        with self.assertRaisesRegex(self.ship.Refusal, "do not administer"):
            self.operation("merge").merge_ownership()

    def test_a_fork_still_refuses(self) -> None:
        """Merging a fork's pull request is a different act with a different blast radius."""

        self.runner_merge("auto")
        self.remote(ALONE, FORK_JSON)
        with self.assertRaisesRegex(self.ship.Refusal, "is a fork of"):
            self.operation("merge").merge_ownership()

    def test_an_unanswerable_remote_still_refuses(self) -> None:
        """`answered=False` is not an answer about co-ownership, so no row overrides it."""

        self.runner_merge("auto")
        self.unreachable_remote()
        with self.assertRaises(self.ship.Refusal):
            self.operation("merge").merge_ownership()
        self.assertNotIn("row_authorized_merge", self.operation("merge").state)

    def test_a_repository_the_account_owns_outright_merges_with_no_row(self) -> None:
        """The regression guard: three yeses need no row, and consult none."""

        self.remote(ALONE)
        delivery = self.operation("merge")
        self.forget_the_row()
        self.assertEqual(delivery.merge_ownership().get("full_name"), "example/thing")
        self.assertEqual(self.notes(), [])
        self.assertNotIn("row_authorized_merge", delivery.state,
                         "ownership answered; the receipt claims no override")

    def test_a_written_guest_is_not_lowered_by_an_auto_row_either(self) -> None:
        """sd:854 still holds on the overriding path: no demotion, so no note."""

        self.write_mode(self.root, "guest")
        self.runner_merge("auto")
        self.remote(WITH_MALLORY)
        self.assertEqual(self.operation("merge").merge_ownership().get("full_name"), "example/thing")
        self.assertEqual(self.notes(), [], "a written guest was not lowered by anyone")


if __name__ == "__main__":
    unittest.main()
