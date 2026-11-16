"""Every call to git's index lister in this repository says what its form is.

sd:841. The index holds an unmerged path once per merge stage, so a plain
listing prints that path three times while somebody is resolving a conflict.
Whether that matters at a given call site ranges from nothing at all to a
wrong answer, and the way to find out is to read the site -- which is the
problem: there are three dozen of them, and nothing made a new one announce
itself.

`Makefile:67` has claimed since item 481 that "every `ls-files` in this
repository now says what it means". It was false by the time sd:823 found two
calls that did not, and sd:823's fix removed the count from the comment
without leaving anything that would fail when the next one landed. The review
of that change re-derived the inventory by hand and found the author's had
missed two sites. That is the whole argument for this file: the inventory is
not something a person can keep, so it is enumerated here instead, from the
index, every time the suite runs.

**What a site must carry.** One of the options that fixes git's output shape
-- `--deduplicate` for a list of paths, `-u`/`-s` for merge stages,
`--error-unmatch` for a probe, `--others` for what the index does not hold --
written on the same line as the subcommand, in the half of the line the call
itself is in. A call that is deliberately plain, because plain output is what
it is testing, says so in a comment and says why.

**Which half of the line.** A declaration has to be the thing the machine
does, not a sentence beside it. `run(["git", <subcommand>])  # --deduplicate`
asks for plain output and says the opposite, and a docstring that quotes the
plain marker while explaining it is prose, not an exemption. So the line is
split at the comment that opens on it: the option counts in the half the call
is in, and the marker counts only in the comment half. Without that split
either one is a text search over the whole line, and the guard is defeated by
writing the right words anywhere near the call -- which is worse than the
hand-written list it replaces, because it looks like a declaration.

**What this does not do**, stated rather than left to be discovered. It does
not decide whether a site chose the right form; a reader does that. It reads
source text, so a call whose subcommand arrives in a variable -- `argv =
["git", name]` -- is invisible to it, and a reviewer is the only thing
standing behind that; no site in this repository is written that way, and the
one place that would have been, `surface` below, is written out instead. It
covers tracked, non-prose files, so an untracked scratch file and a page under
`docs/` are both out of scope. What it does is make the choice visible and
refuse the absence of one, which is the part that was drifting.
"""

# This module reads the whole checkout, so no changed-files fast path may
# narrow it away. `.github/scripts/select-tests.py` greps for the line below.
# select-tests: always-run

from __future__ import annotations

import dataclasses
import importlib.util
import pathlib
import re
import subprocess
import tempfile
import types
import unittest

from tests.clean_env import clean_environment

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# This module is inside the surface it scans, so the subcommand's name is
# spelled in two pieces and joined here. Written whole, it would be a bare
# call in this file's own source -- the scan below would find it, and the
# guard would fail on the guard. Every pattern in this file is built from
# this one constant, so the spelling exists once and the trick is stated once.
SUBJECT = "ls-" + "files"

#: The subcommand as a whole word. `-` is in the excluded class on both sides,
#: so `ls-files-form` -- the marker's own name, three constants down -- is not
#: a match. Without that, the marker would be a site and would need a marker.
TOKEN = re.compile(rf"(?<![\w-]){re.escape(SUBJECT)}(?![\w-])")

#: The command form: the subcommand as a shell word after `git`. The leading
#: class refuses `mygit ` and `path/to/git ` without refusing `! git ` or
#: `$(shell git `.
AFTER_GIT = re.compile(r"(?:^|[^\w./-])git\s+$")

#: What may follow the subcommand in something that runs: an option, a quoted
#: pathspec, a redirect, a pipe, a separator, the end of the line. A word
#: follows it in prose and in an error message -- "git ls-files failed;" --
#: and those are the two false positives this refuses.
ARGUMENT = tuple("-'\"|>;)&$\\")

#: The options that fix the output's shape, and therefore say what the caller
#: asked for. `-s`/`--stage` is here beside `-u`/`--unmerged` because they are
#: the same answer -- one row per stage, on purpose -- and a site that grows
#: one should not have to argue for it separately.
FORM_OPTIONS = ("--deduplicate", "--unmerged", "--error-unmatch", "--others",
                "--stage", "-u", "-s")
OPTION = re.compile(r"(?<![\w-])(?:" + "|".join(FORM_OPTIONS) + r")(?![\w-])")

#: The escape hatch, and it is built to cost something. A site whose subject
#: is plain output declares that in a comment, and the declaration is not
#: complete until it says why -- otherwise the marker becomes the thing a new
#: bare call copies to get past this file, which is the hand-written exemption
#: list again wearing a comment. `REASON_WORDS` is low on purpose: writing the
#: sentence is the toll, not its length.
PLAIN = re.compile(re.escape(SUBJECT) + r"-form: plain -- +(\S.*)$")
REASON_WORDS = 4

#: Documentation is not the subject. `CHANGELOG.md`, `CONTRIBUTING.md` and the
#: pages under `docs/` discuss this command in prose and quote whole command
#: lines a person types; the invariant here is about code that runs, and prose
#: about code is `bin/sd-docs-lint`'s to police.
PROSE_SUFFIX = ".md"

#: Neither code nor prose: a vendored asset the repository carries so that a
#: rendered page needs no network. It holds no text to scan, and reading it as
#: UTF-8 raises rather than returning nothing -- which the walk would report as
#: a collapsed scan. Excluded by directory and not by extension, so a second
#: binary asset lands in the same place or not at all. The dashboard's fonts
#: came with it from the system checkout (sd:5987) and are its static files.
ASSET_PREFIX = ("bin/fonts/", "dashboard/sd_dashboard/v2/static/fonts/")

#: Floors, not counts. They fail when the scan stops reaching the repository
#: -- a moved directory, a recogniser that matches nothing -- and they do not
#: move when a call site is added or deleted, which is the drift that put a
#: number in `Makefile:67` and made it wrong.
SITE_FLOOR = 20
FILE_FLOOR = 8


@dataclasses.dataclass(frozen=True)
class Site:
    """One call, as a reader would cite it, and whether it said what it is."""

    path: str
    line: int
    text: str
    declared: bool

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.text.strip()}"


def is_call(line: str, start: int, end: int) -> bool:
    """Whether the match at `[start:end]` in `line` is a call and not prose.

    Three questions, in the order that makes the cheap one first:

    * Inside a code span? An odd number of backticks before the match means
      the reader is inside one, which is how every page and docstring in this
      repository writes the command when it is talking about it.
    * Written as an argv element -- `"ls-files"` in a list handed to
      `subprocess` -- which is unambiguous, since nothing quotes a lone
      subcommand for any other reason.
    * Written as a shell word after `git`, and followed by something an
      argument can start with, or by a shell comment.
    """

    before, after = line[:start], line[end:]
    if before.count("`") % 2:
        return False
    if before.endswith(('"', "'")) and after.startswith(before[-1]):
        return True
    if not AFTER_GIT.search(before):
        return False
    rest = after.lstrip()
    if rest.startswith("#"):
        # A shell comment ends the command, so nothing followed the
        # subcommand: a bare call (sd:999, review-960). `#` opens a comment
        # only at the start of a word; `ls-files#x` is one word, and no call.
        return rest != after
    return rest == "" or rest.startswith(ARGUMENT)


def calls(text: str) -> list[tuple[int, str, int]]:
    """Every call in `text`, as `(line number, line, column)`."""

    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in TOKEN.finditer(line):
            if is_call(line, match.start(), match.end()):
                found.append((number, line, match.start()))
    return found


def split_comment(line: str) -> tuple[str, str]:
    """`line` as its command half and its comment half.

    `#` opens a comment in every language this scan reaches -- Python, the
    shell, `make`, YAML -- when it begins a word outside a quoted string. Both
    of those conditions earn their place: `$#` and `${#name}` are shell
    parameters, and a pathspec may hold a `#` inside quotes, so a split on the
    first `#` anywhere would cut a command in half and lose the option in it.
    """

    quote, index = "", 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index], line[index:]
        index += 1
    return line, ""


def declares_form(lines: list[str], number: int, column: int = 0) -> bool:
    """Whether the call at `column` on line `number` of `lines` says its form.

    The option must be on the call's own line: every site in this repository
    writes it there, an argv list that wraps puts the subcommand and its
    options on the first line, and "somewhere in the same statement" is not a
    place a reader can check at a glance. It must also be in the half of the
    line the call is in, so that a comment cannot answer for a command that
    did not ask -- and so that a command written out inside a comment, which
    is how a shell file parks one, still reads its own option.

    The plain marker is a comment, and only a comment. It may sit on the line
    directly above, because the line holding the subcommand is often already
    the longest one in the call. One line of reach and no more, so the marker
    cannot be mistaken for the neighbouring call's.
    """

    line = lines[number - 1]
    command, comment = split_comment(line)
    if OPTION.search(comment if column >= len(command) else command):
        return True
    for candidate in (line, lines[number - 2] if number >= 2 else ""):
        marker = PLAIN.search(split_comment(candidate)[1])
        if marker and len(marker.group(1).split()) >= REASON_WORDS:
            return True
    return False


def surface(root: pathlib.Path = REPO_ROOT) -> list[str]:
    """Every tracked path that is code rather than prose.

    From the index, so an untracked scratch file cannot fail the suite and a
    file somebody adds next month cannot escape it by not being listed here.

    `--deduplicate`: this call is itself a site, and the form it wants is a
    list of paths naming each one once. Written out rather than built from
    `SUBJECT`, so that this call is itself one of the sites the scan finds --
    a guard with an exemption for its own source is not one.
    """

    listed = subprocess.run(
        ["git", "ls-files", "-z", "--deduplicate"],
        cwd=root, capture_output=True, text=True, check=True).stdout
    return [name for name in listed.split("\0")
            if name
            and not name.endswith(PROSE_SUFFIX)
            and not name.startswith(ASSET_PREFIX)]


def scan(root: pathlib.Path = REPO_ROOT) -> tuple[list[Site], list[str]]:
    """Every call in the tracked, non-prose surface, and what could not be read.

    The unreadable files come back rather than being swallowed. A walk that
    quietly covers less than it should reports success forever, and that is
    the failure this whole file exists to refuse -- it would be a poor place
    to commit it.
    """

    found: list[Site] = []
    unreadable: list[str] = []
    for name in surface(root):
        try:
            raw = (root / name).read_bytes()
            # A NUL byte is git's own binary test: such a file holds no
            # call. A text file that does not decode still comes back.
            if b"\0" in raw:
                continue
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            unreadable.append(f"{name}: {error}")
            continue
        lines = text.splitlines()
        found.extend(Site(name, number, line, declares_form(lines, number, column))
                     for number, line, column in calls(text))
    return found, unreadable


def sites(root: pathlib.Path = REPO_ROOT) -> list[Site]:
    """Every call in the tracked, non-prose surface of `root`."""

    return scan(root)[0]


def undeclared(root: pathlib.Path = REPO_ROOT) -> list[str]:
    """Every call in `root` that does not say what its form is."""

    return sorted(str(site) for site in sites(root) if not site.declared)


class EveryCallSaysWhatItIs(unittest.TestCase):
    """The guard itself, over this repository."""

    def test_no_call_in_the_tracked_surface_is_bare(self) -> None:
        # Every offender, not the first few: the reader of this failure is
        # about to go and fix them, and `unittest` truncates by default.
        self.maxDiff = None
        self.assertEqual(
            undeclared(), [],
            "each of these calls git's index lister without saying what "
            "shape it expects back. Add the option that fixes it -- "
            f"{', '.join(FORM_OPTIONS)} -- or, if plain output is what the "
            f"site is testing, a comment reading `{SUBJECT}-form: plain -- "
            "<why>` on that line or the one above it.")

    def test_the_scan_reaches_this_repository(self) -> None:
        """A live check pointed at nothing reports success forever.

        Floors rather than the current numbers: this fails when the walk
        collapses, and stays quiet when a call site is added or removed.
        """

        found, unreadable = scan()
        self.assertEqual(unreadable, [], "files the walk could not read")
        self.assertGreaterEqual(len(found), SITE_FLOOR, found)
        self.assertGreaterEqual(len({site.path for site in found}), FILE_FLOOR)


class TheRecogniserTellsCallsFromProse(unittest.TestCase):
    """The half that decides what is a site. Both errors are expensive.

    Missing a call leaves the hole this file exists to close. Claiming one in
    prose fails the suite over a sentence, and the repository's comments and
    docstrings name this command constantly -- so the cases below are the
    exact spellings that appear in it.
    """

    def found(self, text: str) -> list[int]:
        return [number for number, _, _ in calls(text)]

    def test_an_argv_element_is_a_call(self) -> None:
        self.assertEqual(self.found(f'["git", "{SUBJECT}", "-z"]'), [1])
        self.assertEqual(self.found(f"self.git('{SUBJECT}', '--', path)"), [1])

    def test_a_shell_word_after_git_is_a_call(self) -> None:
        for line in (f"git {SUBJECT} --deduplicate -- bin",
                     f"if ! git {SUBJECT} -z -- '*.sh' >\"$list\"; then",
                     f"LINT := $(shell git {SUBJECT} --deduplicate -- bin)",
                     f"run: git {SUBJECT} -z | xargs -0 shellcheck"):
            with self.subTest(line=line):
                self.assertEqual(self.found(line), [1])

    def test_a_code_span_in_prose_is_not_a_call(self) -> None:
        for line in (f"# plain `{SUBJECT}` prints it once per stage",
                     f"    Enumerated from `git {SUBJECT}`, never from a list.",
                     f'    """Track `paths` and return `git {SUBJECT} -- spec`."""',
                     f"# No `git add` here, so `{SUBJECT}` does not see it"):
            with self.subTest(line=line):
                self.assertEqual(self.found(line), [])

    def test_an_error_message_naming_the_command_is_not_a_call(self) -> None:
        """Both shell gates print one, and neither of them runs anything.

        A word follows the subcommand instead of an argument, which is what
        separates a sentence about the command from the command.
        """

        for line in (f'  "error: git {SUBJECT} failed; cannot enumerate." >&2',
                     f"printf 'error: git {SUBJECT} failed.\\n' >&2",
                     f"enumerate it -- git grep -nI -- {SUBJECT} -- tests bin"):
            with self.subTest(line=line):
                self.assertEqual(self.found(line), [])

    def test_a_shell_comment_after_the_subcommand_is_a_call(self) -> None:
        """sd:999 (review-960). The comment ends the command; nothing followed it.

        `#` was not in `ARGUMENT`, so `git ... # enumerate the index` read as
        prose and a bare shell call written that way passed unseen. A `#` that
        does not begin a word is part of the word, and is not this command.
        """

        self.assertEqual(self.found(f"git {SUBJECT} # enumerate the index"), [1])
        self.assertEqual(self.found(f"git {SUBJECT}\t# enumerate the index"), [1])
        self.assertEqual(self.found(f"git {SUBJECT}#not-a-comment"), [])

    def test_the_markers_own_name_is_not_a_call(self) -> None:
        """Otherwise the escape hatch would need an escape hatch."""

        self.assertEqual(self.found(f"# {SUBJECT}-form: plain -- git repeats it here"), [])

    def test_a_program_whose_name_ends_in_git_is_not_this_command(self) -> None:
        for line in (f"mygit {SUBJECT} -z", f"/usr/bin/nogit {SUBJECT} -z"):
            with self.subTest(line=line):
                self.assertEqual(self.found(line), [])


class TheDeclarationHasToSaySomething(unittest.TestCase):
    """The other half: what counts as having declared a form."""

    def declared(self, *lines: str, column: int = 0) -> bool:
        return declares_form(list(lines), len(lines), column)

    def test_an_option_on_the_call_line_declares_the_form(self) -> None:
        for option in FORM_OPTIONS:
            with self.subTest(option=option):
                self.assertTrue(self.declared(f'["git", "{SUBJECT}", "{option}"]'))

    def test_an_option_on_another_line_does_not_reach(self) -> None:
        self.assertFalse(self.declared('    "--deduplicate",',
                                       f'    ["git", "{SUBJECT}"],'))

    def test_an_option_like_word_is_not_an_option(self) -> None:
        for near in ("--deduplicated", "x--others", "-under", "--stages"):
            with self.subTest(near=near):
                self.assertFalse(self.declared(f'["git", "{SUBJECT}", "{near}"]'))

    def test_a_plain_marker_with_a_reason_declares_the_form(self) -> None:
        self.assertTrue(self.declared(
            f'    ["git", "{SUBJECT}", "-z"],  # {SUBJECT}-form: plain -- '
            "the repeated path is the subject here"))

    def test_the_marker_reaches_one_line_down_and_no_further(self) -> None:
        marker = f"    raw = run(  # {SUBJECT}-form: plain -- git repeats the path here"
        call = f'        ["git", "{SUBJECT}", "-z"],'
        self.assertTrue(self.declared(marker, call))
        self.assertFalse(self.declared(marker, "        cwd=root,", call))

    def test_a_marker_with_no_reason_declares_nothing(self) -> None:
        """The toll. Without it the marker is a bare comment a new bare call
        copies, which is the hand-written exemption list in a new costume."""

        for empty in (f"# {SUBJECT}-form: plain", f"# {SUBJECT}-form: plain --",
                      f"# {SUBJECT}-form: plain -- ", f"# {SUBJECT}-form: plain -- why"):
            with self.subTest(marker=empty):
                self.assertFalse(self.declared(f'["git", "{SUBJECT}"]  {empty}'))

    def test_a_bare_call_declares_nothing(self) -> None:
        self.assertFalse(self.declared(f'["git", "{SUBJECT}"]'))
        self.assertFalse(self.declared(f'["git", "{SUBJECT}", "--", "f.py"]'))

    def test_an_option_in_a_trailing_comment_does_not_speak_for_the_command(self) -> None:
        """The command asks for plain output; the comment says the opposite.

        Without this the guard is a text search over the line, and the way
        past it is to type the option somewhere it does nothing -- a cheaper
        exemption than the marker, and a silent one.
        """

        for line in (f'    run(["git", "{SUBJECT}"])  # --deduplicate',
                     f'    run(["git", "{SUBJECT}", "--", "f"])  # not -u, on purpose'):
            with self.subTest(line=line):
                self.assertFalse(self.declared(line))

    def test_an_option_in_the_command_still_declares_with_a_comment_after(self) -> None:
        """The control for the case above: the split must not refuse a real one."""

        self.assertTrue(self.declared(
            f'    run(["git", "{SUBJECT}", "--deduplicate"])  # one row per path'))

    def test_a_call_written_inside_a_comment_reads_the_comment_as_its_half(self) -> None:
        """A parked command line is all comment, so its option is there too."""

        line = f"    # git {SUBJECT} --deduplicate -- bin"
        self.assertTrue(self.declared(line, column=line.index(SUBJECT)))

    def test_a_hash_inside_a_quoted_word_does_not_end_the_command(self) -> None:
        """A pathspec may hold one, and the option after it still counts."""

        self.assertTrue(self.declared(f"git {SUBJECT} -- 'a #b' --deduplicate"))

    def test_the_marker_outside_a_comment_declares_nothing(self) -> None:
        """A docstring about the marker, and a string holding it, are prose.

        Both spellings exist in this repository -- the paragraph above and the
        failure message this file prints both write the marker out -- so the
        marker has to mean something only where a machine would ignore it.
        """

        docstring = f'    """Paths, once each. {SUBJECT}-form: plain -- stages are the point."""'
        call = f'    return run(["git", "{SUBJECT}", "-z"])'
        self.assertFalse(self.declared(docstring, call))
        self.assertFalse(self.declared(
            f'    note = "{SUBJECT}-form: plain -- stages are the point"'
            f'; run(["git", "{SUBJECT}"])'))


class TheGuardAgainstTheGuard(unittest.TestCase):
    """A bare call landing in a tracked file must be named, in a real tree.

    The class above pins the two predicates on strings. This runs the whole
    path -- index, walk, report -- against a repository built for it, because
    that is the thing that has to work the day somebody adds a file.
    """

    def repository(self, **files: str) -> pathlib.Path:
        raw = tempfile.TemporaryDirectory()
        self.addCleanup(raw.cleanup)
        root = pathlib.Path(raw.name).resolve()
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True,
                       capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True,
                       capture_output=True)
        return root

    def test_a_new_bare_call_is_named_with_its_path_and_line(self) -> None:
        root = self.repository(
            **{"tool.py": f'x = 1\nrun(["git", "{SUBJECT}", "--", "f"])\n'})
        self.assertEqual(undeclared(root), [f'tool.py:2: run(["git", "{SUBJECT}", "--", "f"])'])

    def test_the_same_call_with_the_option_is_not_named(self) -> None:
        root = self.repository(
            **{"tool.py": f'run(["git", "{SUBJECT}", "--deduplicate"])\n'})
        self.assertEqual(undeclared(root), [])

    def test_a_bare_call_in_a_page_is_not_named(self) -> None:
        """Prose is out of scope, and the scope is the suffix, not a path."""

        root = self.repository(**{
            "NOTES.md": f"Run: git {SUBJECT} -- bin\n",
            "tool.py": f'run(["git", "{SUBJECT}", "--deduplicate"])\n'})
        self.assertEqual(surface(root), ["tool.py"],
                         "the page was tracked and had to be dropped by "
                         "suffix, not missing from the index altogether")
        self.assertEqual(undeclared(root), [])

    def test_a_file_the_walk_cannot_read_is_reported_not_skipped(self) -> None:
        """Saying nothing about what was not scanned is how a walk shrinks.

        There is no such file in this repository today, which is exactly why
        the case is built rather than waited for: the day one lands, the scan
        must say it covered less, not report the same green it always did.
        """

        root = self.repository(**{"tool.py": "x = 1\n"})
        # Undecodable bytes that would be a bare call if they were text, so
        # the walk cannot both fail to read the file and report it clean.
        # Spelled through `SUBJECT` for the reason given at the top: written
        # out, this line would be a bare call in a tracked file, and the guard
        # would be right to say so -- it did, when it was.
        # No NUL byte, so git and the walk both take it for text.
        (root / "blob.bin").write_bytes(
            b"\xff\xfe git " + SUBJECT.encode() + b" -- bin\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True,
                       capture_output=True)
        found, unreadable = scan(root)
        self.assertEqual(found, [], "the unreadable bytes must not be parsed")
        self.assertEqual([entry.split(":", 1)[0] for entry in unreadable],
                         ["blob.bin"])

    def test_a_binary_file_is_skipped_not_reported(self) -> None:
        """An icon is not code, and a walk that fails on it fails the gate.

        The dashboard's PNG icons were the first binary files the walk met;
        each one came back unreadable and failed the precheck.
        """

        root = self.repository(**{"tool.py": "x = 1\n"})
        (root / "icon.png").write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00 git " + SUBJECT.encode() + b"\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True,
                       capture_output=True, env=clean_environment())
        self.assertEqual(scan(root), ([], []))

    def test_an_untracked_file_is_not_scanned(self) -> None:
        root = self.repository(**{"tool.py": "x = 1\n"})
        (root / "scratch.py").write_text(f'run(["git", "{SUBJECT}"])\n',
                                         encoding="utf-8")
        self.assertEqual(undeclared(root), [])

    def test_a_bare_call_with_the_option_only_in_a_comment_is_named(self) -> None:
        """The whole path, over the bypass the string cases above pin."""

        text = f'run(["git", "{SUBJECT}"])  # --deduplicate'
        root = self.repository(**{"tool.py": f"{text}\n"})
        self.assertEqual(undeclared(root), [f"tool.py:1: {text}"])

    def test_a_bare_call_under_a_docstring_holding_the_marker_is_named(self) -> None:
        text = f'run(["git", "{SUBJECT}"])'
        root = self.repository(**{"tool.py": (
            f'"""Every path. {SUBJECT}-form: plain -- stages are the point."""\n'
            f"{text}\n")})
        self.assertEqual(undeclared(root), [f"tool.py:2: {text}"])


class TheFastPathCannotSkipThisGuard(unittest.TestCase):
    """`make check CHANGED=...` must run this module whatever changed.

    The fast path runs the modules the changed paths name, plus an always-run
    set. This module names no path -- it enumerates them from the index -- so
    nothing a person edits selects it. Left out of that set it would be
    skipped by every narrowed run, and a bare call landing in the edited file
    would pass the check that was supposed to catch it: sd:841's own defect,
    inside sd:841's fix. It was, until this case.

    A membership assertion would pass by agreeing with the list. This runs the
    selector over a path taken from the index at run time instead, so what is
    checked is that a narrowed run includes this module, not that two copies
    of a hand-written tuple still match.
    """

    MODULE = "tests.test_ls_files_form"
    SELECTOR = REPO_ROOT / ".github/scripts/select-tests.py"

    def selector(self) -> types.ModuleType:
        spec = importlib.util.spec_from_file_location("select_tests", self.SELECTOR)
        self.assertIsNotNone(spec, f"{self.SELECTOR} is not importable")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def probes(self) -> list[str]:
        """Tracked code paths this module's own source does not name.

        From the index, so the case cannot survive on an example that was
        hand-picked while it was still selectable. A path this file names
        would select this module by naming and prove nothing about the
        always-run set.
        """

        own = (REPO_ROOT / "tests/test_ls_files_form.py").read_text(encoding="utf-8")
        return [name for name in surface()
                if name.startswith("bin/") and name.split("/")[-1] not in own]

    def test_a_narrowed_run_over_an_unrelated_path_still_runs_this_module(self) -> None:
        selector = self.selector()
        declared = selector.always_run(selector.test_modules(REPO_ROOT))
        self.assertIn(self.MODULE, declared,
                      "this module no longer declares itself whole-tree; the "
                      f"line the selector greps for is {selector.ALWAYS_RUN_MARKER!r}")
        probes = self.probes()
        self.assertNotEqual(probes, [], "no tracked path under bin/ is unrelated")
        for probe in probes:
            selection, reason = selector.select(REPO_ROOT, [probe])
            if selection is None:
                continue
            self.assertIn(self.MODULE, selection,
                          f"a narrowed check over {probe} ({reason}) would not "
                          "run this guard, so a bare call landing in that file "
                          "would pass it")
            return
        self.fail(f"no path in {probes[:3]}... narrows, so nothing was checked")


if __name__ == "__main__":
    unittest.main()
