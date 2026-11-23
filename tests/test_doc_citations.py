"""A `path:line` citation still points at the thing the prose says it does.

A line number is invalidated by any insertion above its target, and nothing
watched them. Step 8-iv demonstrated the failure inside a single branch:
growing `bin/sd` from 1,553 lines to 2,006 moved `frontmatter()` from 1231 to
1248, the reader's `.strip('"')` from 1252 to 1276, and `status_filter` from
1350 to 1378. All three were correct on `main`, all three were wrong on the
branch that changed the file, and the branch that broke them was also the
branch editing the document that carried them.

The planning review rule -- `.claude/rules/sd-planning-adversarial-review.md`,
and the contract it points at -- already asks for this sweep, and it did catch
those three. But it only runs at a planning convergence boundary. A pure code
change that edits no `prd.md`, `design.md` or `implement.md` breaks citations
with nothing to notice; 8-iv was swept only because it happened to edit
`design.md` as well. This runs on every change instead.

**The rule is adjacency.** A citation that directly follows a backticked
symbol -- `` `status_filter` (`bin/sd:1378`) `` -- is a claim *about that
symbol*, and the symbol must appear at the cited line. A citation with prose
between it and the nearest backticked token is making some other claim, and is
skipped rather than guessed at: an earlier draft took the nearest symbol within
90 characters and mis-attributed `bin/sd-status:501-506`, which is an accurate
citation to a docstring that does not happen to repeat the key name. A gate
whose failures need interpreting teaches people to interpret failures away.

**Nothing is skipped silently any more.** The predecessor filtered: four
`continue`s in a row, each dropping a population and none of them naming it.
Whichever ran first hid everything the later ones would have said, so the
order of the chain decided the report, and nobody had written the order down.
Removing the last of them moved the compared population from 44 to 1 and every
test in this module stayed green -- which is the argument for classifying
rather than filtering, and it is not an argument about tidiness.

`classify()` now returns one row per `path:line` token in the corpus, each
carrying a reason from `REASONS`, and `census()` counts them. Conservation is
asserted: the buckets sum to the tokens found and no row carries a reason from
outside the vocabulary. That is a partition property rather than a threshold,
so it does not drift when prose is reorganised, and it is what would have
caught each of these on the day it was introduced rather than one at a time by
being bitten.

Measured over the corpus at 2026-09-16 -- 5,965 tokens -- and re-measurable by
running the module, which prints the census on every run. A snapshot, and the
only defensible kind: it is dated by the commit that carries it, and `census()`
is what a reader should run rather than trust it.

**Every reason here says what it did, not what it declined to do.** The bucket
that used to read `line-into-code` is `anchored-line-into-code`, because at
`0 0 0` the old name read as "no citations into code" and meant "no
*symbol-anchored* ones" -- and an unanchored one was the thing nobody was
resolving (sd:811). A census line that overstates its coverage is the same
defect as a silent `continue`; it just takes longer to find.

=========================  ======  ======  ======
reason                       live  archiv   total
=========================  ======  ======  ======
`compared`                      0       3       3
`no-adjacent-anchor`          576   2,378   2,954
`elided-path`                 212   2,180   2,392
`archived-stale`                0     325     325
`anchor-not-a-symbol`          25     130     155
`separator-not-adjacent`       23     111     134
`declared-absent`               1       0       1
`absent-but-present`            0       0       0
`target-missing`                0       0       0
`escapes-checkout`              0       0       0
`quoted`                        1       0       1
`quoted-not-there`              0       0       0
`anchored-line-into-code`       0       0       0
`line-past-end`                 0       0       0
=========================  ======  ======  ======

Each reason, with why it exists:

* **`compared`** -- the anchor was checked against the cited line. One of the
  four buckets that can fail, with `target-missing`, `absent-but-present`
  and `quoted-not-there`;
  `test_the_red_buckets_are_empty` is what spends the other two.
* **`no-adjacent-anchor`** and **`elided-path`** -- the token matched no
  anchoring shape, or names a line and no file because the prose named the
  file already. Both are tokens, neither is anchorable, and between them they
  are 89% of the corpus. Counted rather than dropped so that a regex tightened
  by accident shows up as a bucket moving.
* **`archived-stale`** -- an archived document citing a line that has moved,
  a line its file no longer has, or a file since deleted. Reported, never
  failed: the ruling was to take the
  coverage and not buy it by editing the historical record. Archives are *in*
  the corpus now; the predecessor never opened them and said nothing about it.
* **`anchor-not-a-symbol`** -- the token before the citation is a path or a
  phrase, so there is nothing to check a line against.
* **`separator-not-adjacent`** -- a bare comma or semicolon between the two
  halves. Question 4's subject, and its own bucket on purpose: folded into
  `no-adjacent-anchor` the comma population is uncountable, which is how it
  stayed at "0" for three days.
* **`target-missing`** -- a path inside the checkout with no file at it. This
  is a stale citation and it is **red**. Empty today.
* **`declared-absent`** -- the same, but the citation carries
  `[absent: <reason>]`. Not a failure and not the same bucket, because the
  marker is a claim that can itself go stale.
* **`absent-but-present`** -- that claim, gone stale: an `[absent: ...]` whose
  target exists. **Red**, and empty today. Stated as a contract in an earlier
  draft of this docstring with nothing enforcing it, which is the silencer
  shape this module is about; `test_the_red_buckets_are_empty` enforces it now.
* **`escapes-checkout`** -- the path resolved outside the tree. Silent by
  design and the only one of these that is a security refusal. It has never
  fired on real content, and it is its own bucket so that the silence stays
  visible instead of being inferred from one it used to share with a live
  defect.
* **`quoted`** -- the citation carries `[quoted: <path:line>]` or
  `[quoted: source:<path>::<symbol>]` and that source really carries this
  citation. See below.
* **`quoted-not-there`** -- it carries one whose line does not. Red: a reason
  that parses but does not hold is worse than free text, because it looks
  checked.
* **`anchored-line-into-code`** -- a live *anchored* citation whose target is
  not markdown. Red since sd:525: cite `source:<path>::<symbol>` or write
  prose. Named for what it covers since sd:811, because the unanchored case is
  not in it and its `0` was being read as though it were.
* **`line-past-end`** -- the line, or the end of the range, is past the last
  line of the file. Asked of every citation whose path is a file, anchored or
  not, and **red**: `bin/sd_skill.py:217` into an 89-line file passed every
  gate for a week because the three "not a claim about a symbol" buckets each
  returned before anything opened the target (sd:811). An insertion cannot
  reach it, since a file only grows. A deletion can: shortening a cited code
  file turns this red for a lane that touched no page, and that is the
  decision taken (review-951, B2) -- the cited line is then gone, not moved,
  so the red is a true positive, and the failure line names the citing page
  and line, the whole range, the file's length today and the two fixes.
  sd:525's objection was to reds on lines that had merely moved; this rule
  cannot fire on one. Red with no carve-out since sd:829: the nine live
  past-end citations the rule was built over sat on an enumerated, checked
  list as `line-past-end-carried` from 2026-09-14 until each was settled on
  its own page -- every one dropped the number and kept the file name, and
  one, `drawPlugins` in `dashboard/app.js` (a file retired at sd:719 step
  6), also says in prose, with an `[absent: ...]` note beside the symbol,
  that it is gone on purpose. A
  tenth the rule cannot see, a unique-suffix path, was fixed by hand on the
  same pass. The list, its liveness test and its ceiling went with the last
  row, and so did the marker's power to stand this check down: `absent`
  claims a file is gone, and a file that is here to be opened is not.

**The nine `path:line` sites in this module's own prose are step 8-iv's
record, and none of them is a claim about today's tree.** Lines 1231 and 1378
of `bin/sd` are the two numbers the first paragraph quotes -- `frontmatter()`
and `status_filter` *on the branch that moved them* -- and lines 501-506 of
`bin/sd-status` are where an earlier draft's 90-character rule mis-attributed
a docstring. All three have moved again since: `frontmatter` is at 1259 today
and `status_filter` at 1421. Repointing them would rewrite the record rather
than repair it, which is the ruling this module already makes for
`CHANGELOG.md` -- "the one place a reference that no longer resolves is still
correct". The six fixture literals reusing 1378 quote the same record, and the
regexes they exercise never open a file.
`test_the_historical_numbers_are_not_repointed` pins all nine, so a later lane
cannot tidy them into today's lines and lose what they are evidence of; sd:799
filed them as stale and this is the answer to that filing. Nothing gates a
`path:line` inside a `.py` file, here or anywhere -- the corpus is markdown --
so saying it is the only thing that can.

(Spelled without backticks above on purpose. The whole token is a citation
like any other, and `test_the_quoted_example_is_carried_once_and_only_by_
marker_after` requires that `marker_after` be the one place in this file that
types the 1231 one.)

**What `line-past-end` does not reach, measured rather than waved at.** Of the
5,965 tokens, 693 have a path that resolves to a tracked file and are the ones
this rule opens. The other 5,272 do not resolve, and saying which is the only
honest way to state the rule's coverage:

* 2,392 carry **no path at all** -- the elided form, `` `:391-414` ``, whose
  file is named by the prose around it. Resolving one means deciding how far
  back to read, which is the 90-character rule this module threw out. The
  largest unchecked class, and it is unchecked on purpose.
* 2,289 name a path inside the checkout that **matches no tracked file by
  suffix, nor, failing that, by basename** -- a fixture name, a deleted file,
  a renamed one. (Every class below was matched the same way: the path as a
  suffix of a tracked path first, and its basename alone only when no suffix
  matched.)
* 413 name a path whose **basename or suffix matches more than one** tracked
  file. Picking one is a guess and a gate that guesses teaches people to argue
  with it.
* 175 name a path that **resolves to exactly one tracked file by suffix**,
  where the citation elided the leading directory -- `` `prepare-release.py:338` ``
  for a file under `bin/`. This one is not a guess, and measuring it found a
  live past-end citation the rule as written does not catch:
  a contributed skill's `SKILL.md:126` in the 2026-09-05 page, against a
  file of 114 lines (the skill has since been retired). Thirty-five more sit in
  archives. Whether the rule should follow a unique suffix was a decision, not
  an oversight, and sd:829 took it: no. Against that one live finding stand
  the 413 above, where a suffix rule has to guess, so a path resolves only
  when it names a tracked file exactly, and the one live finding was fixed by
  hand -- the number dropped, the file name kept.
* 2 resolve **outside the checkout** and 1 names a **directory**.

Like the table above, these are a dated snapshot and not an assertion: the
rule they describe is what the tests pin, and the numbers move when the corpus
does.

**The corpus is every tracked markdown file, asked of git**, with
`CHANGELOG.md` excluded by name carrying rule 7's reason: the changelog names
paths as they were at the time, the one place a reference that no longer
resolves is still correct. The predecessor globbed `docs/**/*.md`, which held
the gate off `AGENTS.md`, `README.md`, `CONTRIBUTING.md` and every
`skills/**/SKILL.md` without ever saying so.

**The two markers.** `[absent: <reason>]` says a cited file is gone on
purpose. `[quoted: <path:line>]` says a citation is an example rather than a
claim -- the answer to "can a document explain this gate without tripping it",
found by being caught, when this item's own PRD reproduced a self-test
verbatim and `make check` failed on that file. Both require a reason: an
exemption nobody has to justify is a silencer with better manners. `absent`
keeps free text, because its claim -- the target is gone -- is one the gate
checks directly. `quoted` does not: nothing but the reason says where the
example came from, so under D4a the reason **is** a `path:line` the gate
opens, and a reason that does not carry the citation lands in
`quoted-not-there` rather than exempting anything.

Since sd:765 the reason can also be `source:<path>::<symbol>`, and a
`path:line` into code no longer pins its number. Into a markdown page the
named line must carry the citation, as before. Into code, the line number is
a hint the gate does not check or repair, kept for the one legacy marker: the
file must carry the citation somewhere, and nothing more is asked. That is a
relaxation, not sd:525's rule -- sd:525 removes a line number into code, and
this keeps one unchecked. `source:<path>::<symbol>` is the precise form: the
one declaration of that name must carry the citation between its first and
last line. Found by being caught again: the one live marker quotes this
module's own docstring by line, and a blank line added above it turned three
tests red with no repair but a hand edit in another item's page.

Because the file-wide check is satisfied by any line, this module must carry
the legacy marker's quoted citation on exactly one line, inside
`marker_after`. Every fixture spells it split, and
`test_the_quoted_example_is_carried_once_and_only_by_marker_after` fails
the day one of them types it whole; otherwise deleting the example would
leave the marker green.

Three shapes have no symbol to compare a line against, and saying so is more
honest than a number that implies one was found: the bare comma and semicolon
(134), the elided path (2,392), and the token with no anchoring shape at all
(2,954). Since sd:811 they are not *unresolved*, which is what this paragraph
used to imply and what the census was read as saying -- each one whose path is
a file still has its line looked up, and a line the file does not have is
`line-past-end` whatever shape the prose around it takes. What is not checked
is the claim: no symbol stands beside them to check one against.

**sd:525: the symbol is authoritative and a line into code is not.** Every
insertion above a cited line used to turn this gate red for a lane that was
not editing documentation, and the gate had begun deciding where code went:
`bin/sd_lib.py` grew two sections appended at the end of the file with comments
saying why, and a docstring was held to one line to keep a count fixed. The
owner's ruling was to remove the incentive rather than tolerate it.

So a live `path:line` citation anchored to a symbol and pointing into anything
other than markdown is `anchored-line-into-code`, and red, whether or not the
line is right today: it will not be right after the next insertion, and the
lane that inserts is not the lane that wrote it. The stable spelling is
`source:<path>::<symbol>`, which `source_declaration_error` resolves by
declaration and which no insertion can break. A claim about a line that is not
a declaration is written as prose naming the enclosing declaration; losing
that line number was accepted. `dashboard/app.js` had no locator, being the
only non-Python file under `bin/` and `dashboard/` until sd:719 step 6
retired it, so its citations were prose.

Markdown targets keep `path:line`: a line in a page is `bin/sd-docs-lint` rule
6's subject and the repointer's, not this rule's. Archives are records and keep
whatever they cite. The migration was made by the repointer, which rewrites a
line into code as its `source:` locator when the anchor is declared exactly
once and refuses otherwise.
"""

# This module reads the whole checkout, so no changed-files fast path may
# narrow it away. `.github/scripts/select-tests.py` greps for the line below.
# select-tests: always-run

from __future__ import annotations

import collections
import errno
import os
import pathlib
import re
import subprocess
import tempfile
import typing
import unicodedata
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# A backticked token, then only whitespace or an opening paren, then the
# citation. Anything else between them and this is not a claim about the token.
PAIR = re.compile(r"`([^`\n]+)`\s*\(?`([A-Za-z0-9_./-]+):(\d+)(?:-(\d+))?`")

#: The parenthesised comma and semicolon, accepted; the bare ones, not.
#:
#: Measured before it was written: of the three punctuation-shaped near misses
#: in the corpus, the parenthesised comma is a real anchored citation and the
#: two bare ones are *list separators*, where the anchor of each citation is
#: the token to its right. A left-scanning regex widened to the bare comma
#: takes the tail of the previous list item as the anchor and reports a symbol
#: nobody claimed was there -- the mis-attribution
#: `test_prose_between_a_symbol_and_a_citation_breaks_the_anchor` exists to
#: prevent, arriving through punctuation instead of prose. So the wrapping
#: parenthesis is the discriminator, and it is the *only* discriminator.
#:
#: The anchor class also excludes parentheses, and the reason once given for
#: that -- "so the leading `\(` cannot bind to one inside the anchor" -- is
#: not true: the `\(` must be followed immediately by a backtick, and the
#: anchor is backtick-delimited, so a parenthesis inside the anchor can never
#: be taken for the wrapping one. The exclusion costs a call-shaped anchor,
#: ``(`frontmatter()`, `bin/sd:1`)``, which falls through to
#: `SEPARATED_PAIR` and is filed `separator-not-adjacent`, never compared --
#: while the same anchor unparenthesised is compared, because `SYMBOL`
#: accepts the call shape and `PAIR` admits it.
#:
#: Measured 2026-09-12 and left alone: zero instances in the corpus. Widening
#: a documented discriminator with no live instance is the move this module
#: refuses everywhere else, so the false reason goes and the constraint
#: stays. If a live instance appears, admit parentheses to the anchor class
#: and expect one row to move from `separator-not-adjacent` to `compared`.
PAREN_PAIR = re.compile(
    r"\(`([^`()\n]+)`\s*[,;]\s*`([A-Za-z0-9_./-]+):(\d+)(?:-(\d+))?`\)")

#: The bare separator, matched in order to be *counted* rather than compared.
#: Question 4's whole subject. It needs its own bucket: folded into
#: `no-adjacent-anchor` the comma population is uncountable, which is how it
#: stayed at "0" for three days. A named regex rather than a character
#: distance on purpose -- sweeping a distance bound from one character to ten
#: moved the bucket from 0 to 132 with no principled stopping point, which is
#: not a classification, it is a dial.
SEPARATED_PAIR = re.compile(
    r"`([^`\n]+)`\s*[,;]\s*`([A-Za-z0-9_./-]*):(\d+)(?:-(\d+))?`")

#: Every `path:line` token, including the elided form that names no file.
#: `PAIR`'s path class with `+` relaxed to `*`. The relaxation is what admits
#: a non-citation, so it was measured rather than assumed: across the live
#: corpus it adds only the elided form and introduces no match whose path is
#: neither a file name nor empty. A backticked ISO timestamp does not match --
#: `2026-09-06T19:11:39Z` puts a colon where the pattern needs a closing
#: backtick -- and one written to minute precision would surface as a
#: `no-adjacent-anchor` row rather than as a silent drop, which is the point
#: of counting rather than filtering.
TOKEN = re.compile(r"`([A-Za-z0-9_./-]*):(\d+)(?:-(\d+))?`")

SYMBOL = re.compile(r"^\.?[A-Za-z_][A-Za-z0-9_.]*(\(.*\))?$")
EXTENSION = re.compile(r"\.(md|py|js|json|sh|ya?ml|toml|txt|lock)$")

#: The two markers, and their grammar, taken from 0.71.34. The reason is
#: required, the marker follows its citation on the same line with nothing
#: non-blank between them, and it covers exactly one citation.
#: Every line terminator the grammar names, not just `\n`. 0.71.34 is explicit
#: -- "Reasons may not span a line terminator, `\r` and U+2028/U+2029
#: included" -- and a reason class excluding only `\n` would let
#: `[absent: x\u2028y]` through, which is a marker spanning a line
#: suppressing a missing-target failure. A guard that fails open on an exotic
#: separator is worth less than no guard, because it reads as covered.
TERMINATORS = "\n\r\u2028\u2029"

MARKER = re.compile(r"\[(quoted|absent):[ \t]*([^\]" + TERMINATORS + r"]*?)[ \t]*\]")

#: A `quoted` reason is a `path:line` or a `source:<path>::<symbol>`, and
#: nothing else. The path shape is TOKEN's own, so a reason cannot name
#: something a citation could not.
#:
#: Where the gate looks depends on what the reason names. Into markdown, the
#: line is where the quoted text must be. Into code, a `path:line` holds when
#: the file carries the quoted text at all: the line number is a hint the gate
#: does not check or repair, kept for the one legacy marker, because an
#: insertion above it by an unrelated lane used to turn the gate red. The
#: `source:` form is the precise spelling for code: the quoted text must sit
#: inside that one declaration, which no insertion elsewhere can move (sd:765).
QUOTED_REASON = re.compile(
    r"source:([A-Za-z0-9_./-]+)::([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z0-9_./-]+):(\d+)")

# The cited line is where the symbol is *introduced*; prose cites a `def` line
# and the reader looks at the lines under it. Wide enough to survive a
# signature wrapped across lines, narrow enough that a symbol used a hundred
# lines away cannot satisfy it.
WINDOW = 2

#: The closed vocabulary. Conservation is asserted against it: every token in
#: the corpus lands in exactly one of these, the buckets sum to the tokens
#: found, and no row carries a reason from outside. That is a partition
#: property rather than a threshold, so it cannot drift when prose is
#: reorganised -- and it is what would have caught each of the four silencers
#: this module used to carry on the day it was introduced, rather than one at
#: a time by being bitten.
REASONS = frozenset({
    "compared",
    "archived-stale",
    "escapes-checkout",
    "target-missing",
    "declared-absent",
    "absent-but-present",
    "anchor-not-a-symbol",
    "elided-path",
    "separator-not-adjacent",
    "no-adjacent-anchor",
    "quoted",
    "quoted-not-there",
    "anchored-line-into-code",
    "line-past-end",
})

class Citation(typing.NamedTuple):
    """One `path:line` token in one document, with why it was or was not checked."""

    doc: pathlib.Path
    anchor: str
    path: str
    target: pathlib.Path | None
    start: int
    end: int
    reason: str
    #: The line of `doc` the citation is written on, 1-based. Set on every
    #: row, not only the ones that need it: `describe` reads it, the carried
    #: list sd:829 deleted was keyed on it, and a field populated on one
    #: branch of `classify` would be a trap for the next reader who reached
    #: for it from another.
    line: int


#: `CHANGELOG.md` is excluded by name, carrying rule 7's stated reason: the
#: changelog names paths as they were at the time, which is the one place a
#: reference that no longer resolves is still correct. It holds 9 tokens, 6 of
#: them naming paths that do not exist; every one of those is correct.
CHANGELOG = "CHANGELOG.md"


def is_symbol(token: str) -> bool:
    """A name a line can be checked against, as opposed to a path or a phrase."""

    return bool(SYMBOL.match(token)) and "/" not in token and not EXTENSION.search(token)


def points_into_code(path: str) -> bool:
    """Is this citation target code for sd:525's rule, rather than a page?

    Everything but markdown. A line in a page is `bin/sd-docs-lint` rule 6's
    subject and the repointer's; a line anywhere else moves under an insertion
    no documentation lane made, and `source:<path>::<symbol>` does not.

    Markdown is `.md` or `.markdown`, in any case. Since sd:765 this also
    decides whether a quoted reason's line is the claim, so a `NOTES.MD:1`
    read as code was satisfied by any line of the page (sd:794).
    """

    return not path.lower().endswith((".md", ".markdown"))


def numbered_lines(text: str) -> list[str]:
    """`text` split into the lines `ast` numbers: on `\\n`, `\\r\\n` and `\\r` only.

    `str.splitlines()` also breaks on a form feed, `\\x1c`-`\\x1e`, `\\x85`,
    U+2028 and U+2029, none of which ends a line for the parser. A declaration's
    `lineno` indexed into `splitlines()` then lands below the declaration after
    any such character above it: the window read the wrong lines, green for a
    neighbour carrying the text and red for the declaration itself (sd:794).
    """

    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def file_lines(target: pathlib.Path) -> list[str]:
    """This file's lines, numbered the way `numbered_lines` numbers them.

    **The claim is about NUMBERING, not about opening.** Every reader that
    turns a cited file into numbered lines numbers it this way: `line_count`,
    and through it `within_file` and `describe`; `quotes`' markdown branch;
    `names_its_symbol`; `anchor_lines`' fallback; `calls_by_name`; and
    `quoted_repoint`. Four other functions read a file directly, and not one of
    them can give a different answer than this for the same line (review-959):

    * `quotes`' `source:<path>::<symbol>` branch reads the cited file itself,
      because `ast` reports line numbers for the exact string it was handed and
      a second read need not be that string; it numbers that string with the
      same `numbered_lines`.
    * `source_declaration_error` and `declaration_lines` read the cited file
      and never split it at all. Their numbers are `ast`'s own `lineno`, which
      is the authority `numbered_lines` was written to mirror.
    * `quotes`' `path:line`-into-code branch reads the cited file and numbers
      nothing: it is a substring test over the whole text, and the line in the
      marker is a hint that branch does not check.
    * `undecoded_text` reads bytes, for the one guard whose subject is the
      newlines themselves, and numbers nothing.

    `classify`, `repoint_document`, `stable_source_citations` and
    `rule_path_citations` read the citing *page*, which is not a cited file.

    `file_lines` is `numbered_lines` minus the empty final element, so the two
    agree on the text of line N for every N that `file_lines` has: a reader on
    either of them reads the same line (sd:846). This paragraph is an
    enumeration, and an enumeration recited in prose drifts -- sd:794 moved the
    `source:` window and the use test onto `numbered_lines` and left
    `names_its_symbol`, `anchor_lines`' fallback and the markdown branches on
    `splitlines()`, which counted two lines more than the parser past this
    module's own U+2028 fixture, and sd:822 then wrote that every reader came
    through here while three of them did not. So
    `test_every_direct_read_of_a_cited_file_is_named_here` enumerates the four
    from this module's syntax tree and fails if a fifth appears or a name here
    goes missing, rather than trusting the prose again.

    A trailing newline *ends* the last line, it does not start another, so the
    empty final element `split("\\n")` leaves behind is dropped. Without that
    an off-by-one lets a citation one line past the end read as in range, which
    is the commonest spelling of the defect `line_count` exists to find.
    Decoded with replacement: these readers compare and discard, and a byte
    that will not decode should fail a row, not the run.
    """

    lines = numbered_lines(target.read_text(encoding="utf-8", errors="replace"))
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def undecoded_text(target: pathlib.Path) -> str:
    """This file's bytes as text, with no newline translation of any kind.

    `read_text` opens in universal-newline mode, so every `\\r\\n` and every
    bare `\\r` reaches the caller as `\\n`: a guard that searches its own
    module for a carriage return would find none however many it carried, and
    pass. `read_bytes` was the right choice and an unanchored one -- this
    module holds no carriage return of its own, so nothing here could show the
    difference (sd:846). `invisible_separators` is where the guard reads, and
    it takes the file to read, so the difference can be shown on a fixture that
    has one (review-959).
    """

    return target.read_bytes().decode("utf-8")


#: Every terminator `str.splitlines()` breaks on that `ast` does not count as
#: ending a line. As code points, not as escapes: the escapes are what the
#: guard below permits in a fixture, so writing the list itself that way puts
#: the characters it hunts one editor slip away from being the characters
#: themselves -- which is exactly what happened while this constant was being
#: written, and the guard caught it (review-959).
INVISIBLE_SEPARATORS = tuple(chr(code) for code in (
    0x0D, 0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029))


def invisible_separators(target: pathlib.Path) -> dict[str, int]:
    """Which of `INVISIBLE_SEPARATORS` this file carries literally, and how many.

    Through `undecoded_text`, so no newline translation hides one: `read_text`
    turns every `\\r\\n` and every bare `\\r` into `\\n` on the way in, and the
    guard would then report a clean file whatever carriage returns it carried.

    A parameter rather than `__file__`, which is the whole reason this is a
    function (review-959). The guard's only subject used to be this module,
    which holds no carriage return of its own, so running it could not tell a
    byte read from a text read: reverting the read to `read_text` survived all
    126 tests. Given a path, the guard itself can be asked of a fixture that
    does carry one.

    A `\\r\\n` pair is one line ending, not a separator: the tokenizer ends a
    line there as it does at `\\n`, so a CRLF checkout of this module holds no
    fixture character on that account (sd:999, review-956). Only a `\\r` left
    over once the pairs are gone is counted.
    """

    text = undecoded_text(target).replace("\r\n", "\n")
    return {f"U+{ord(char):04X}": text.count(char)
            for char in INVISIBLE_SEPARATORS if char in text}


def cited_file_openers() -> dict[str, list[int]]:
    """Every module-level function that reads a file other than the citing page.

    Enumerated from this module's own syntax tree, because `file_lines`'
    docstring names this set in prose and prose drifts: the claim there has
    been wrong twice (sd:822, review-959). A `read_text` or `read_bytes` inside
    a module-level `def`, keyed by the function and carrying the lines it is
    on. Reads whose receiver is the name `doc` are the citing page, a different
    subject. Methods are not module-level: a test that reads back the scratch
    file it just wrote is not a reader of a cited file.
    """

    import ast

    tree = ast.parse(undecoded_text(pathlib.Path(__file__)))
    found: dict[str, list[int]] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            opened = call.func
            if not (isinstance(opened, ast.Attribute)
                    and opened.attr in ("read_text", "read_bytes")):
                continue
            if isinstance(opened.value, ast.Name) and opened.value.id == "doc":
                continue
            found.setdefault(node.name, []).append(call.lineno)
    return found


def line_count(target: pathlib.Path) -> int:
    """How many lines this file has, counted the way `file_lines` reads them."""

    return len(file_lines(target))


def within_file(target: pathlib.Path, start: int, end: int,
                counted: dict[str, int] | None = None) -> bool:
    """Does this file have the lines `start`-`end` a citation claims it has?

    The one question no gate asked of an unanchored citation, which is why
    `bin/sd_skill.py:217` into an 89-line file passed every gate (sd:811).
    It is asked of the *larger* of the two lines, so `a-b` is caught when
    `b` is past the end even though `a` is not, and so is `b-a` written
    backwards. Files start at line 1, so `:0` is past the end as well.

    Only a deletion can make this false for a citation that was true: an
    insertion lengthens a file, so growing one never turns this red. Deleting
    or shortening a cited code file does, for a lane that touched no page,
    and that is intended: the cited line is then gone, not moved, and the
    red is a true positive. sd:525's objection was to a red on a line that
    had merely moved; this rule cannot fire on one.
    """

    # `counted` is one `classify()` call's memo and nothing wider. Seven
    # hundred citations resolve to a tracked file and a handful of files carry
    # most of them, so without it the corpus scan reads `bin/sd` a hundred
    # times over and the module's runtime doubles. A cache that outlived the
    # call would have to answer "has this file changed", which is a question
    # a gate should not be guessing at -- the fixtures rewrite their targets
    # mid-test on purpose.
    # One return, not a memoised branch beside an unmemoised one. The two
    # spellings were written first and a mutation that made the predicate read
    # `start` in place of `end` survived, because every caller took the other
    # branch: duplicated logic where only one copy is reached is an untested
    # copy, which is this module's own subject.
    if counted is None:
        counted = {}
    key = str(target)
    if key not in counted:
        counted[key] = line_count(target)
    return 1 <= min(start, end) and max(start, end) <= counted[key]


def describe(row: "Citation") -> str:
    """One failure line: where the citation is, what it says, and what is there.

    The whole range, not `row.start`: a range is judged by its larger line, so
    printing the smaller one named a line the file has and sent the reader to
    look at it (review-951). The citing line is printed so the page can be
    opened at the claim, and a file target carries its length today, which is
    the number the claim is being measured against.
    """

    lines = (f"{row.start}" if row.start == row.end
             else f"{row.start}-{row.end}")
    where = f"{row.doc.relative_to(REPO_ROOT)}:{row.line}: `{row.path}:{lines}`"
    if row.target is not None and row.target.is_file():
        where += f" -- {row.path} has {line_count(row.target)} lines"
    return where


def is_under_repo(target: pathlib.Path) -> bool:
    """Containment only: does this path resolve inside the checkout?

    **The `resolve()` is load-bearing and is not an implementation detail.**
    `Path.is_relative_to` is lexical, so
    `(REPO_ROOT / ".." / ".." / "etc" / "passwd").is_relative_to(REPO_ROOT)`
    is `True` without it and `False` with it. Reading this predicate's name as
    "containment" and dropping the resolve would turn the security refusal
    into an invitation.

    It no longer answers "and does a file exist there". That second question
    is a *stale citation* -- the thing this module was built to catch -- and
    it used to be discarded through the same `continue` as the refusal.
    """

    try:
        resolved = target.resolve()
    except OSError:
        return False
    return resolved.is_relative_to(REPO_ROOT.resolve())


def corpus(root: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Every tracked markdown file, asked of git, `CHANGELOG.md` excluded.

    Asked of git rather than globbed so that the corpus is the repository's
    own answer to what it tracks. The predecessor globbed `docs/**/*.md`,
    which silently held the gate off every markdown file above `docs/` --
    `AGENTS.md`, `README.md`, `CONTRIBUTING.md`, every `skills/**/SKILL.md` --
    and said nothing about it.

    Archived documents are *in* the corpus and are compared. A stale citation
    in an archive is reported as `archived-stale` and fails nothing: the
    ruling was to take the coverage and not buy it by editing the historical
    record.
    """

    # Not resolved: `contained` does the containment test against a resolved
    # base of its own, and a caller that passed `/var/...` needs the documents
    # back under `/var/...` rather than under the `/private/var/...` the
    # platform resolves it to.
    root = root or REPO_ROOT
    try:
        listed = subprocess.run(
            # `--deduplicate` and the basename rule both match
            # `bin/sd-docs-lint`'s enumerator, deliberately. A conflicted index
            # carries one entry per stage, so without the flag the same
            # document is read once per stage and conservation counts its
            # tokens twice; backbone item 481 records that for the other
            # reader. The `--` keeps a path that looks like an option out of
            # the option list.
            ["git", "-C", str(root), "ls-files", "-z", "--deduplicate",
             "--", "*.md"],
            capture_output=True, text=True, check=True).stdout.split("\0")
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - no git
        listed = [str(p.relative_to(root)) for p in sorted(root.rglob("*.md"))]
    return contained(root, [root / name for name in listed
                            if name and not name.endswith(CHANGELOG)])


def marker_after(flat: str, raw: str, end: int) -> tuple[str, str] | None:
    """The marker covering the citation that ends at `end`, or `None`.

    0.71.34's grammar: the reason is required, the marker follows the citation
    on the same line with nothing non-blank between them, and it covers one
    citation.

    **"Nothing non-blank" admits one closing parenthesis**, because the
    commonest citation shape in this repository wraps itself in one --
    ``frontmatter() (`bin/sd:1231`)`` -- and the marker has to be able to
    follow it. Taking the grammar's words literally would leave the
    parenthesised form, which is most of the corpus, unable to carry a marker
    at all. One, not any number: a run of them is prose.

    The line rule survives the flattening because the flattening is
    offset-preserving -- one space per newline, one character for one -- so an
    offset in `flat` is the same offset in `raw`. The match is taken on `flat`
    and the line test is then made against `raw`.
    """

    cursor = end
    seen_paren = False
    while cursor < len(flat):
        if flat[cursor] in " \t":
            cursor += 1
        elif flat[cursor] == ")" and not seen_paren:
            seen_paren = True
            cursor += 1
        else:
            break
    match = MARKER.match(flat, cursor)
    if match is None:
        return None
    if any(mark in raw[end:match.end()] for mark in TERMINATORS):
        return None
    kind, reason = match.group(1), match.group(2).strip()
    if not reason:
        return None
    # D4a. `absent` keeps free text: it claims a file is gone, which the gate
    # checks directly by looking for the file. `quoted` claims the citation is
    # an example copied from somewhere, and nothing but the reason says where,
    # so a free-text reason is an exemption bought once and never re-read --
    # `[quoted: anything]` silenced a citation forever. The reason is now the
    # evidence: a `path:line` or a `source:<path>::<symbol>` the gate opens. A
    # malformed one is not a marker, so the citation falls through and is
    # checked like any other claim.
    if kind == "quoted" and QUOTED_REASON.fullmatch(reason) is None:
        return None
    return kind, reason


def anchor_for(flat: str, span: tuple[int, int]) -> tuple[str, bool] | None:
    """The backticked token this citation is a claim about, and whether it is adjacent.

    Returns `(anchor, True)` for the two adjacent shapes, `(anchor, False)` for
    a bare comma or semicolon -- counted rather than compared -- and `None`
    when no anchoring shape reaches the token at all.

    Each candidate is accepted only when its *citation half* covers exactly the
    token being classified, span for span. An approximate test was written
    first and it mis-filed a real parenthesised pair as a bare separator, which
    is the kind of error a census hides rather than reports: the token still
    lands in a bucket, just the wrong one, and conservation stays green.
    """

    for pattern, trailing in ((PAIR, 0), (PAREN_PAIR, 1), (SEPARATED_PAIR, 0)):
        for match in pattern.finditer(flat, max(0, span[0] - 400), span[1] + 2):
            if (match.start(2) - 1, match.end() - trailing) == span:
                return match.group(1), pattern is not SEPARATED_PAIR
    return None


def quotes(reason: str, token: str, doc: pathlib.Path,
           root: pathlib.Path | None = None) -> bool:
    """Does the source a `quoted` reason names really carry `token`?

    `token` is the citation as written, backticks and all, so the check is
    that the source really does quote this citation rather than merely
    mention the same file. What "carry" means follows `QUOTED_REASON`:

    * `path:line` into markdown -- that line carries it. A line past the end,
      or a line that does not carry it, answers no.
    * `path:line` into anything else -- some line of the file carries it. The
      line number is a hint this function does not check and the repointer
      does not repair, kept for the one legacy marker: before sd:765 an edit
      above the gate module's own docstring turned three tests red, and the
      only repair was a hand edit in another item's page. Deleting the quoted
      text from the file still answers no -- provided no other line of the
      file carries it, which is why this module spells its fixtures split.
    * `source:<path>::<symbol>` -- the one declaration of `symbol` in that
      Python file carries it, between its first and last line. Declared
      twice, not at all, or in a file that does not parse, answers no.

    A reason naming a file outside the checkout, or no file, answers no, and
    the citation is then classified as the claim it looks like.

    **`doc` cannot be its own source.** A page whose marker names the page
    itself proves the citation by pointing at the citation: the line the
    reason names is the line the marker sits on, so the token is trivially
    there and the exemption certifies itself. That is the shape this whole
    device exists to remove, arriving through the mechanism that removes it,
    so the same document is refused outright rather than only the same line
    -- quoting a *different* line of the same page is the same circle drawn
    wider.
    """

    parsed = QUOTED_REASON.fullmatch(reason)
    if parsed is None:
        return False
    symbol = parsed.group(2)
    path, line = (parsed.group(1), "") if symbol else (parsed.group(3), parsed.group(4))
    # `repoint_document` supports a root that is not the checkout, so resolving
    # through the global one made every marker in a custom root read as
    # unquoted: a current quoted marker then produced a same-line Move instead
    # of no move at all. The default keeps `classify`'s caller unchanged.
    base = REPO_ROOT if root is None else root
    source = base / path
    if not (source.resolve().is_relative_to(base.resolve()) if root is not None
            else is_under_repo(source)) or not source.is_file():
        return False
    if source.resolve() == doc.resolve():
        return False
    if symbol:
        import ast

        # A page declares no symbol, however it parses: the same refusal
        # `source_declaration_error` makes before it parses (sd:999).
        if not points_into_code(path):
            return False
        try:
            text = source.read_text(encoding="utf-8")
            spans = declared_spans(ast.parse(text, filename=path), symbol)
        except (OSError, UnicodeError, SyntaxError, ValueError):
            return False
        if len(spans) != 1:
            return False
        first, last = spans[0]
        return token in "\n".join(numbered_lines(text)[first - 1:last])
    if points_into_code(path):
        return token in source.read_text(encoding="utf-8", errors="replace")
    try:
        number = int(line)
    except ValueError:
        # `\d+` is unbounded and CPython refuses to convert a string of more
        # than 4,300 digits, so a long enough run of digits parses as a reason
        # and then raises out of `classify`. A gate that crashes on a document
        # is worse than one that fails it: the run reports nothing at all
        # rather than one bad row. An unconvertible line number is a line the
        # file does not have, which is already a failed quote.
        return False
    lines = file_lines(source)
    return 1 <= number <= len(lines) and token in lines[number - 1]


def classify(docs: list[pathlib.Path] | None = None) -> list[Citation]:
    """One row per `path:line` token in the corpus, each with why it was kept or not.

    **The chain stopped being a chain.** The predecessor filtered: four
    `continue`s in a row, each dropping a population and none of them naming
    it. Whichever ran first hid everything the later ones would have said, so
    the order of the chain decided the report and nobody had written the order
    down. A classification cannot do that -- every token gets a row, the
    reasons are a closed set, and the buckets are asserted to partition the
    corpus.

    **Iteration is over tokens, not over anchor-citation pairs.** A citation's
    anchor can itself be a `path:line` token, so one `PAREN_PAIR` match can
    contain two `TOKEN` matches. The subject token is compared, the anchor
    token gets its own row on its own terms, and no token is consumed by being
    another token's anchor. Without that rule the buckets under-count against
    the corpus and conservation fails on the real tree rather than on a
    fixture.

    **The corpus is a parameter and that is not a convenience.**
    `escapes-checkout` has no live instance and never has had one, and
    `quoted` has as many as this item's own pages carry. A function that
    fetches its own input can only be tested against whatever it fetches, so
    without this the two branches that matter most would be unreachable in a
    test and conservation would be proved only on the shapes that happen to
    exist today.
    """

    rows: list[Citation] = []
    counted: dict[str, int] = {}
    for doc in (corpus() if docs is None else docs):
        raw = doc.read_text(encoding="utf-8", errors="replace")
        # Newlines flattened: a citation routinely wraps away from its symbol.
        # One character for one, so offsets carry over to `raw` unchanged.
        flat = raw.replace("\n", " ")
        archived = "archive" in doc.parts
        for match in TOKEN.finditer(flat):
            path, start = match.group(1), int(match.group(2))
            end = int(match.group(3) or match.group(2))
            # The flattening is offset-preserving -- one space per newline --
            # so an offset in `flat` is the same offset in `raw`, and the
            # newlines before it are this token's line number.
            where = raw.count("\n", 0, match.start()) + 1
            marker = marker_after(flat, raw, match.end())
            if marker and marker[0] == "quoted":
                # D4a. The reason names where the example was copied from, and
                # the gate goes and looks. An exemption that cannot fail is
                # the silencer this module exists to remove, so this one is
                # made to fail: move the quoted text and the row goes red.
                if quotes(marker[1], match.group(0), doc):
                    reason = "quoted"
                elif archived:
                    # An archive is a record. Its quoted source moving is the
                    # same event as its cited target moving, and that is
                    # `archived-stale` by policy everywhere else in this
                    # module. Red here would mean archiving a page turns a
                    # later, unrelated edit into a build failure -- including
                    # for this very item, once it is archived.
                    reason = "archived-stale"
                else:
                    reason = "quoted-not-there"
                rows.append(
                    Citation(doc, "", path, None, start, end, reason, where))
                continue
            if not path:
                rows.append(
                    Citation(doc, "", path, None, start, end, "elided-path",
                             where))
                continue
            found = anchor_for(flat, match.span())
            anchor = found[0] if found else ""
            # sd:811. Asked of *every* citation whose path is a file, before
            # the anchoring buckets get to claim it. The three buckets below
            # -- no-adjacent-anchor, separator-not-adjacent,
            # anchor-not-a-symbol -- never opened their target, so a citation
            # in prose could name any line at all and land in one of them
            # counted as "not a claim about a symbol" rather than as wrong.
            # `bin/sd_skill.py:217` into an 89-line file spent a week there.
            # The question is asked of the file, not of the anchor, so the
            # answer does not depend on how the prose around it is shaped.
            # Nor on a marker: `[absent: ...]` claims the file is gone, and a
            # file that is here to be opened is not gone. Until sd:829 the
            # marker stood this check down, which let a past-end line into a
            # file that exists be greened by appending a reason nothing
            # re-checks.
            target = REPO_ROOT / path
            if (is_under_repo(target) and target.is_file()
                    and not within_file(target, start, end, counted)):
                if archived:
                    # The archive ruling, unchanged: a record citing a line
                    # its file no longer has is the same event as a record
                    # citing a symbol that moved, and that is reported, not
                    # failed. 37 of the 49 live here.
                    reason = "archived-stale"
                else:
                    reason = "line-past-end"
                rows.append(
                    Citation(doc, anchor, path, target, start, end, reason, where))
                continue
            if found is None:
                rows.append(
                    Citation(doc, "", path, None, start, end,
                             "no-adjacent-anchor", where))
                continue
            anchor, adjacent = found
            if not adjacent:
                rows.append(
                    Citation(doc, anchor, path, None, start, end,
                             "separator-not-adjacent", where))
                continue
            if not is_symbol(anchor):
                rows.append(
                    Citation(doc, anchor, path, None, start, end,
                             "anchor-not-a-symbol", where))
                continue
            if not is_under_repo(target):
                rows.append(
                    Citation(doc, anchor, path, None, start, end,
                             "escapes-checkout", where))
                continue
            if not target.is_file():
                # An archive naming a file that has since been deleted is a
                # record, not a defect: `archived-stale` reports it and fails
                # nothing. Live, the same shape is the stale citation this
                # module exists to catch, and `target-missing` is red.
                if archived:
                    reason = "archived-stale"
                elif marker and marker[0] == "absent":
                    reason = "declared-absent"
                else:
                    reason = "target-missing"
                rows.append(
                    Citation(doc, anchor, path, target, start, end, reason, where))
                continue
            reason = "compared"
            if marker and marker[0] == "absent":
                # The marker claims the target is gone and it is not. The claim
                # is the thing that went stale, so it is its own bucket and it
                # is red -- an absence marker nobody rechecks is a silencer
                # that outlives its reason.
                reason = "absent-but-present"
            elif archived and not names_its_symbol(anchor, target, start, end):
                reason = "archived-stale"
            elif not archived and points_into_code(path):
                # sd:525. The symbol is authoritative and the number is not: a
                # line into code goes stale at the next insertion above it,
                # made by a lane that was not editing documentation.
                reason = "anchored-line-into-code"
            rows.append(
                    Citation(doc, anchor, path, target, start, end, reason, where))
    return rows


def names_its_symbol(anchor: str, target: pathlib.Path, start: int, end: int) -> bool:
    """Does the cited window carry the anchor? The one comparison this gate makes."""

    lines = file_lines(target)
    window = "\n".join(lines[max(0, start - 1 - WINDOW):end + WINDOW])
    return anchor.rstrip("()") in window


def census(docs: list[pathlib.Path] | None = None) -> collections.Counter:
    """How many tokens landed in each bucket. Criterion 6's report."""

    return collections.Counter(row.reason for row in classify(docs))


def contained(root: pathlib.Path, documents) -> list[pathlib.Path]:
    """The documents this test will open: real files inside `root`.

    `is_under_repo` guards the file a citation *names*. This guards the file
    the citation is *in*, which nothing was watching. `glob` returns a symlink
    as readily as a regular file and `read_text` follows it, so a tracked
    `docs/current.md -> /etc/passwd` would have CI read a file of the
    document tree's choosing -- the same hole, entered from the other side.

    Resolved before the containment test, because an unresolved path compares
    as relative to the root while pointing anywhere at all.
    """
    base = root.resolve()
    kept = []
    for doc in documents:
        try:
            resolved = doc.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(base):
            kept.append(doc)
    return kept


def anchored_citations() -> list[tuple[pathlib.Path, str, pathlib.Path, int, int]]:
    """The `compared` bucket, in the shape the staleness test has always read."""

    return [(row.doc, row.anchor, row.target, row.start, row.end)
            for row in classify() if row.reason == "compared"]


# ------------------------------------------------ the symbol preference (R13-D1)
#
# sd:431, prose rule 1: a `path:line` into code should name the symbol,
# `source:<path>::<symbol>`, because the number goes stale at the next
# insertion above it and the name does not. A `[quoted: ...]` reason needs its
# line by construction (sd:568), and a page cited by line is
# `bin/sd-docs-lint`'s subject and the repointer's; neither is counted.
#
# The population is the *unanchored* citations. Since sd:525 a live anchored
# `path:line` into code is `anchored-line-into-code`, which is red, so what is
# left is the token nobody anchored -- `no-adjacent-anchor`,
# `separator-not-adjacent`, `anchor-not-a-symbol` -- which the gate opens only
# to check the line is in range.
#
# Until sd:1374 this counted only a line inside a `def` or a `class` of a
# Python file. That made the count depend on the code, not on the prose: an
# insertion above a cited line could carry it out of every symbol, and the
# count fell with no document changed. Every such citation now counts,
# wherever its line sits, so only an edit to the prose moves the count.

#: Live `path:line` citations into code, per document. A ratchet on
#: violations, never a census: each entry may fall and may not rise, and an
#: entry that reaches zero is deleted. Per document, so one page cannot offset
#: a new violation in another. Archived records are outside this live-prose gate.
#:
#: Re-measured for sd:1374 on `4ce3abc9`, when the population stopped being
#: "inside a `def` or a `class`": 34 and 45 then became 52 and 81. The rise is
#: the citations the old population could not see, and not new ones. One of
#: them is the case the row measured: prd.md cited `bin/sd-status:877` for the
#: `handoff.resolve_root` call, and code motion had carried that line into a
#: module comment, where the count stopped counting it. The count fell by one
#: and read as a cleanup, but the citation was as stale as before. That
#: citation now names `handoff_section`, which is why prd.md stands at 80.
SYMBOL_ANCHORED_CITATIONS = {
    "docs/work/2026-09-05-the-pack-runs-a-team-process-for-one-person/implement.md": 46,
    "docs/work/2026-09-05-the-pack-runs-a-team-process-for-one-person/prd.md": 76,
}


def symbol_anchored_citations(
        docs: list[pathlib.Path] | None = None) -> dict[str, int]:
    """Live `path:line` citations into code, per document.

    Over `classify`'s rows, so the population is the one the gate already
    reads and no second tokeniser exists. A row is counted when its document
    is live, it is not `quoted`, and its target is a code file inside the
    checkout. Where the line sits does not matter (sd:1374): the count used to
    ask whether a `def` or a `class` held it, and unrelated code motion that
    carried a stale citation into a module comment took it out of the count,
    which the ratchet then read as a cleanup. A line number into code goes
    stale at the next insertion above it wherever it lands, so every one is
    counted, and a count falls only when a citation is rewritten or removed.
    """

    counts: collections.Counter = collections.Counter()
    for row in classify(docs):
        if "archive" in row.doc.parts or row.reason == "quoted" or not row.path:
            continue
        if not points_into_code(row.path):
            continue
        target = REPO_ROOT / row.path
        if not (is_under_repo(target) and target.is_file()):
            continue
        counts[row.doc.relative_to(REPO_ROOT).as_posix()] += 1
    return dict(counts)



class DocCitationTests(unittest.TestCase):
    def test_every_anchored_citation_names_its_symbol_at_the_cited_line(self) -> None:
        # `names_its_symbol`, not a second copy of it. This loop used to build
        # the window itself, which made it a seventh reader of a cited file
        # with nothing pinning it to the other six: a revert to `splitlines()`
        # here changed no fixture, because this test only ever runs over the
        # live corpus (sd:846 NB2). The function it now calls is pinned.
        stale = []
        for doc, anchor, target, start, end in anchored_citations():
            if not names_its_symbol(anchor, target, start, end):
                stale.append(
                    f"{doc.relative_to(REPO_ROOT)}: `{anchor}` is not at"
                    f" {target.relative_to(REPO_ROOT)}:{start}")
        self.assertEqual(stale, [], "\n".join(stale))

    def test_the_scan_reaches_the_documents(self) -> None:
        """The control, and deliberately not a threshold on what it found.

        A `PAIR` that matched nothing -- a tightened regex, a moved document
        tree -- would make the test above pass over any number of stale
        citations without comparing a single one. But asserting *how many*
        citations exist makes the control fail whenever the prose is
        reorganised, while the invariant still holds. So this asserts the tree
        was reached and at least one citation was compared; that `PAIR` itself
        works is proved by fixture in the test below rather than by counting.
        """

        self.assertNotEqual(corpus(), [], "the document tree was not reached at all")
        self.assertTrue(anchored_citations() or stable_source_citations(REPO_ROOT), "no citation was compared")

        # Conservation, which is the assertion the two above cannot make. Every
        # token lands in exactly one bucket and no bucket is invented, so a new
        # silencer cannot be added without either naming itself in `REASONS` or
        # breaking this line. A threshold drifts when prose is reorganised; a
        # partition does not.
        counts = census()
        tokens_found = sum(len(TOKEN.findall(
            doc.read_text(encoding="utf-8", errors="replace").replace("\n", " ")))
            for doc in corpus())
        self.assertEqual(sum(counts.values()), tokens_found,
                         "a token was dropped between the scan and the census")
        self.assertLessEqual(set(counts), REASONS,
                             f"reasons outside the vocabulary: {set(counts) - REASONS}")

        # Criterion 6: what was actually validated, rather than assumed.
        # `.github/scripts/run-tests.sh` runs each module as
        # `python -m unittest <module> > <shard>.log 2>&1` with no `-b`, so
        # this lands in `unittest-output.log` on every run.
        # Over `REASONS`, not over `counts`: a bucket that fell to zero is a
        # result, and iterating the counter would delete it from the report.
        print("citation census: " + ", ".join(
            f"{reason}={counts[reason]}" for reason in sorted(REASONS)))

    def test_the_changelog_exclusion_is_by_basename_and_not_by_root_path(self) -> None:
        """The docstring says `CHANGELOG.md` is excluded. It said it of one file.

        The test compared the whole relative path against the literal, so the
        rule read "the CHANGELOG at the root" while the prose read "a file
        called CHANGELOG.md". Only the root one is tracked today, so nothing
        was wrong and nothing would have said so when a second one appeared.
        `bin/sd-docs-lint` already excludes by basename; matching it is the
        point, since two readers of the same corpus disagreeing is the defect.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "docs").mkdir()
            (root / "CHANGELOG.md").write_text("root\n", encoding="utf-8")
            (root / "docs" / "CHANGELOG.md").write_text("nested\n", encoding="utf-8")
            (root / "docs" / "kept.md").write_text("kept\n", encoding="utf-8")
            names = sorted(doc.name for doc in corpus(root))
            self.assertEqual(names, ["kept.md"], f"corpus was {names}")

    def test_the_red_buckets_are_empty(self) -> None:
        """The docstring calls four buckets red. Until this, nothing made them so.

        `anchored_citations()` filters to `compared`, so the stale-symbol test
        never sees `target-missing` or `absent-but-present`: a live citation to
        a deleted file, or an `[absent: ...]` on a file that came back, landed
        in its bucket, was counted, and left the suite green. Counting a defect
        is not catching it. The reasons named here are the ones the module
        documents as failures, and this is the assertion that spends them.

        `quoted-not-there` joined them with D4a: a `[quoted:]` reason that
        parses as a `path:line` but does not carry the citation is worse than
        free text, because it looks checked.
        """

        rows = classify()
        fixes = {
            "anchored-line-into-code": (
                " cite `source:<path>::<symbol>` instead (`python3"
                " tests/test_doc_citations.py --repoint --apply` rewrites each"
                " one whose symbol is declared once) or say it in prose; sd:525"),
            "line-past-end": (
                " the file no longer has that line: repoint the citation to"
                " where the content moved, or, if the content is gone, drop"
                " the number, keep the file name, and say in prose -- an"
                " `[absent: <reason>]` note beside the symbol -- that it went"
                " on purpose; sd:811, sd:829"),
        }
        for reason in ("target-missing", "absent-but-present", "quoted-not-there",
                       "anchored-line-into-code", "line-past-end"):
            offenders = [
                describe(row) for row in rows if row.reason == reason
            ]
            self.assertEqual(offenders, [], f"{reason}:{fixes.get(reason, '')}\n"
                             + "\n".join(offenders))

    def test_a_citation_cannot_send_this_test_outside_the_checkout(self) -> None:
        """A citation is a string in a document, and this test opens what it names.

        `REPO_ROOT / path` returns the absolute path when `path` is absolute
        and follows `..` out of the tree, so without this an edit to any
        document under `docs/` could make CI read a file of its choosing.
        """

        self.assertFalse(is_under_repo(pathlib.Path("/etc/passwd")))
        self.assertFalse(is_under_repo(REPO_ROOT / ".." / ".." / "etc" / "passwd"))
        self.assertTrue(is_under_repo(REPO_ROOT / "bin" / "sd"))

    def test_containment_is_not_lexical_and_the_resolve_is_what_makes_it_so(self) -> None:
        """The mutation this predicate invites, named so it cannot be made quietly.

        `Path.is_relative_to` is lexical. Read the predicate's name as
        "containment" and drop the `resolve()` -- which is how a reviewer
        reading the phrase literally would implement it -- and the escape
        below starts answering `True`. The control beside it is what makes
        this test mean anything: the same path is `True` under the lexical
        comparison, so it is the resolve that refuses, not the shape.
        """

        escape = REPO_ROOT / ".." / ".." / "etc" / "passwd"
        self.assertTrue(escape.is_relative_to(REPO_ROOT))
        self.assertFalse(is_under_repo(escape))

    def test_a_missing_target_inside_the_checkout_is_a_stale_citation(self) -> None:
        """The second question that used to share the refusal's `continue`.

        "This path escapes the checkout" is a security refusal and is silent
        forever. "This path is inside the checkout and there is no file
        there" is a stale citation -- precisely what this module was built to
        catch -- and it was discarded through the same branch. Split, the
        first is `escapes-checkout` and the second is `target-missing`, which
        fails.
        """

        missing = REPO_ROOT / "no-such-file-here.md"
        self.assertTrue(is_under_repo(missing), "the path is inside the checkout")
        self.assertFalse(missing.is_file(), "and there is no file at it")

    def test_prose_between_a_symbol_and_a_citation_breaks_the_anchor(self) -> None:
        """The rule is adjacency, and adjacency has to actually be required.

        Without this, a `PAIR` that tolerated arbitrary text between the two
        would reintroduce the mis-attribution the docstring describes, and the
        gate would start reporting accurate citations as stale.
        """

        self.assertTrue(PAIR.search("`status_filter` (`bin/sd:1378`)"))
        self.assertIsNone(PAIR.search("`status_filter` is reported by `bin/sd:1378`"))


class TheSymbolPreference(unittest.TestCase):
    """R13-D1: a line number inside a symbol should be the symbol's name."""

    def test_line_citations_into_a_symbol_match_their_baseline(self) -> None:
        """The ratchet. Equality, for the reason leg b's baseline is one.

        A count that rose is a new `path:line` into code that
        `source:<path>::<symbol>` or the file alone could have said; a count that fell is
        the ordinary good case, and the entry moves with it in the same
        change. `assertLessEqual` would let the record go stale.
        """
        self.assertEqual(symbol_anchored_citations(), SYMBOL_ANCHORED_CITATIONS, """
The live `path:line` citations into code no longer match their baseline.

Above: measured first, baseline second. A count that rose is a new `path:line`
into code: cite `source:<path>::<symbol>` instead, or the file alone in prose.
A count that fell is a citation rewritten or removed, since code motion cannot
move one out of the count (sd:1374): lower the entry in
`SYMBOL_ANCHORED_CITATIONS` in the same change, and delete it at zero.""")

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        (self.root / "bin").mkdir()
        (self.root / "bin" / "tool.py").write_text(
            "LIMIT = 1\n"                     # 1: outside every symbol
            "\n"
            "def render():\n"                 # 3
            "    return LIMIT\n"              # 4: inside `render`
            "\n"
            "class Store:\n"                  # 6
            "    KIND = 'x'\n"                # 7: inside `Store`, no method
            "\n"
            "    def open(self):\n"           # 9
            "        return self.KIND\n",     # 10: inside `open`
            encoding="utf-8")
        # One name declared twice, which `source:<path>::<symbol>` refuses.
        (self.root / "bin" / "twins.py").write_text(
            "class Disk:\n"                   # 1
            "    def open(self):\n"           # 2
            "        return 'disk'\n"         # 3: inside `Disk.open`
            "\n"
            "class Net:\n"                    # 5
            "    def open(self):\n"           # 6
            "        return 'net'\n"          # 7: inside `Net.open`
            "\n"
            "def solo():\n"                   # 9
            "    return Disk()\n",            # 10: inside `solo`, declared once
            encoding="utf-8")
        (self.root / "docs").mkdir()
        # A page cited by line, and the source a `[quoted: ...]` reason names.
        (self.root / "docs" / "notes.md").write_text(
            "LIMIT\nthe example reads `bin/tool.py:4`\n", encoding="utf-8")

    def measured(self, prose: str) -> dict[str, int]:
        page = self.root / "docs" / "page.md"
        page.write_text(prose, encoding="utf-8")
        with mock.patch.dict(globals(), {"REPO_ROOT": self.root}):
            return symbol_anchored_citations([page])

    def test_a_line_inside_a_function_or_a_class_is_counted(self) -> None:
        self.assertEqual(self.measured("see `bin/tool.py:4` and `bin/tool.py:7`"
                                       " and `bin/tool.py:10`\n"),
                         {"docs/page.md": 3})

    def test_a_line_outside_every_symbol_is_counted_too(self) -> None:
        """sd:1374. Code motion used to carry a stale citation out of every
        declaration and out of the count with it, and the fall read as a
        cleanup. A module-level line moves under an insertion like any other."""
        self.assertEqual(self.measured("see `bin/tool.py:1`\n"), {"docs/page.md": 1})

    def test_a_line_into_code_that_is_not_python_is_counted(self) -> None:
        (self.root / "bin" / "tool.sh").write_text("#!/bin/sh\necho one\n", encoding="utf-8")
        self.assertEqual(self.measured("see `bin/tool.sh:2`\n"), {"docs/page.md": 1})

    def test_a_quoted_reason_and_a_markdown_target_are_exempt(self) -> None:
        """CONTROLS: sd:568's marker needs its line; a page is not code."""
        self.assertEqual(self.measured("see `bin/tool.py:4`\n"), {"docs/page.md": 1},
                         "the same citation unmarked is counted, or the marker"
                         " below exempts nothing")
        self.assertEqual(self.measured(
            "`bin/tool.py:4` [quoted: docs/notes.md:2]\nand `docs/notes.md:1`\n"), {})

    def test_a_line_inside_a_name_declared_twice_is_counted_too(self) -> None:
        """sd:1374. `source:bin/twins.py::open` names two declarations, so the
        remedy there is the file alone in prose; the line still moves."""
        self.assertIsNotNone(source_declaration_error(self.root, "bin/twins.py", "open"))
        self.assertEqual(self.measured("see `bin/twins.py:3` and `bin/twins.py:7`\n"),
                         {"docs/page.md": 2})


class TheMarkerGrammar(unittest.TestCase):
    """0.71.34's grammar, and the four ways of not writing a marker.

    `[quoted: <reason>]` is the answer to "can a document quote a citation
    without making it a claim". It was found by being caught: this item's own
    PRD reproduced the module's self-test verbatim and `make check` failed on
    *that file*, because quoting the example asserted something false about
    this repository. A gate that cannot be shown an example cannot be
    documented.

    The negatives carry the weight. A marker with no reason is the mute button
    -- `[quoted]` under another name -- and is rejected for the same reason
    `until` is required of an accepted protection gap: an exemption nobody has
    to justify is a silencer with better manners.
    """

    #: Split on purpose, and every fixture below builds from this. The live
    #: marker in sd:5's `design.md` quotes this module by `path:line`, which
    #: sd:765 made file-wide; a fixture typing the token whole would satisfy
    #: that marker for ever, whatever happened to `marker_after`'s example.
    QUOTABLE = "`bin/sd:" + "1231`"

    def quoting(self, token: str) -> str:
        """A `path:line` reason naming a line of this file that carries `token`.

        Searched rather than written down. D4a makes the reason evidence the
        gate opens, so a fixture asserting the positive case has to name a
        line that really carries the token -- and a hard-coded number here
        would be the same stale citation this module exists to catch, in the
        test that proves it catches them.
        """

        here = pathlib.Path(__file__)
        for number, line in enumerate(file_lines(here), 1):
            if token in line and "QUOTABLE = " not in line:
                return f"{here.relative_to(REPO_ROOT)}:{number}"
        raise AssertionError(f"no line of {here.name} carries {token}")

    def reason_for(self, text: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "doc.md").write_text(text + "\n", encoding="utf-8")
            rows = classify([root / "doc.md"])
            self.assertEqual(len(rows), 1, f"{len(rows)} tokens in {text!r}")
            return rows[0].reason

    def reason_in_checkout(self, sources: dict[str, str], text: str,
                           doc: str = "docs/doc.md") -> str:
        """The one row `text` yields, in a fixture checkout holding `sources`.

        A marker's reason is resolved against the checkout, so a fixture that
        wants a source whose lines it controls has to be the checkout. Written
        for sd:765, whose cases are about which line of the source is asked.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name, body in {**sources, doc: text + "\n"}.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(body, encoding="utf-8")
            with mock.patch.dict(globals(), {"REPO_ROOT": root}):
                rows = classify([root / doc])
            self.assertEqual(len(rows), 1, f"{len(rows)} tokens in {text!r}")
            return rows[0].reason

    #: A page with the example on line 3, and a line 1 that does not carry it.
    PAGE = {"notes.md": f"# notes\n\nthe example is {QUOTABLE} here\n"}

    def test_a_marker_with_a_reason_exempts_the_citation_it_follows(self) -> None:
        self.assertEqual(
            self.reason_for(
                f"`f` ({self.QUOTABLE}) [quoted: {self.quoting(self.QUOTABLE)}]"),
            "quoted")

    def test_a_reason_that_is_not_a_path_line_does_not_exempt(self) -> None:
        """D4a. The reason the marker shipped with, and what it bought.

        `[quoted: the docstring's own stale example]` reads like a
        justification and is not one: nothing opens it, so it exempts the
        citation forever. It is not a marker now, and the citation under it
        is checked like any other claim.
        """

        # The exact bucket, not merely "not quoted". A regression that kept
        # treating the sentence as a marker would land in `quoted-not-there`
        # and pass an assertNotEqual, while the contract here is stronger:
        # a malformed reason is not a marker at all, so the citation falls
        # through and is checked like any other -- and `bin/x.py` does not
        # exist, so it is `target-missing`.
        self.assertEqual(
            self.reason_for("`f` (`bin/x.py:1`) [quoted: an English sentence]"),
            "target-missing")

    def test_a_reason_naming_a_line_that_does_not_carry_the_citation(self) -> None:
        """The failing direction, which is the whole point of D4a.

        A reason that parses but does not hold is worse than free text,
        because it looks checked. It lands in its own red bucket rather than
        in `quoted`, so the census shows it and the gate fails.
        """

        # Into a page, where the line is still the claim. Asked of a page since
        # sd:765: into code the number is a hint, so this module's own line 1
        # no longer fails -- the file carries the token further down.
        self.assertEqual(
            self.reason_in_checkout(self.PAGE, f"`f` ({self.QUOTABLE}) [quoted: notes.md:1]"),
            "quoted-not-there")
        self.assertEqual(
            self.reason_in_checkout(self.PAGE, f"`f` ({self.QUOTABLE}) [quoted: notes.md:3]"),
            "quoted", "the control: the line that carries it")

    def test_a_separator_in_a_quoted_page_does_not_shift_the_line_it_asks_for(self) -> None:
        """sd:846 NB2. `quotes` reads a page's lines the way the parser counts them.

        sd:822 moved this branch onto `file_lines` and no fixture held it
        there: a revert to `splitlines()` changed nothing any test could see.
        A page whose first line carries a U+2028 is one line longer under
        `splitlines()` and every line below it is numbered one too high, so
        the marker that really does name the carrying line reads as
        `quoted-not-there` -- a correct exemption failed -- and the line above
        it reads as `quoted`, which exempts a citation nothing checked. Both
        directions are asserted, so a reader that merely shifts cannot pass.
        """
        page = {"notes.md": "head\u2028tail\npad\n"
                            f"the example is {self.QUOTABLE} here\n"}
        self.assertEqual(
            self.reason_in_checkout(page, f"`f` ({self.QUOTABLE}) [quoted: notes.md:3]"),
            "quoted", "line 3 carries it, as git and every editor number the page")
        self.assertEqual(
            self.reason_in_checkout(page, f"`f` ({self.QUOTABLE}) [quoted: notes.md:4]"),
            "quoted-not-there",
            "the page has three lines; 4 is `splitlines()`'s number for the third")

    def test_a_page_cannot_be_its_own_quoted_source(self) -> None:
        """The circle: the reason names the line the marker sits on.

        `[quoted: doc.md:1]` written on line 1 of `doc.md` passes a
        content check trivially -- the token is there because the marker is
        there -- so the exemption certifies itself. Found by Copilot on #870.
        The whole document is refused as a source, not just the one line:
        quoting a different line of the same page draws the same circle
        wider.
        """

        # Asked of `quotes` directly, and with a control. A fixture written
        # through `classify` cannot reach this guard: `quotes` resolves the
        # reason against REPO_ROOT, so a document in a temporary directory
        # fails earlier, for the wrong reason, and the assertion passes while
        # proving nothing. The first version of this test did exactly that.
        here = pathlib.Path(__file__)
        reason = self.quoting(self.QUOTABLE)
        self.assertTrue(
            quotes(reason, self.QUOTABLE, REPO_ROOT / "docs" / "some-other-page.md"),
            "the control: any other page may cite this line")
        self.assertFalse(
            quotes(reason, self.QUOTABLE, here),
            "but this file may not cite itself")

    def test_a_failed_quote_in_an_archive_is_archived_stale_not_red(self) -> None:
        """An archive is a record, and its sources move like anything else.

        Every other reason in this module treats an archived page's drift as
        `archived-stale` rather than a failure. Without this the quoted
        branch would be the one exception, so archiving a page would turn a
        later unrelated edit into a build failure -- including for the very
        item that introduced the marker, once it is archived.
        """

        self.assertEqual(
            self.reason_in_checkout(
                self.PAGE, f"`f` ({self.QUOTABLE}) [quoted: notes.md:1]",
                doc="docs/archive/2026-01-old/design.md"),
            "archived-stale")

    def test_a_reason_naming_a_file_that_is_not_there(self) -> None:
        self.assertEqual(
            self.reason_for(f"`f` ({self.QUOTABLE}) [quoted: no-such-file-here.md:1]"),
            "quoted-not-there")

    def test_a_reason_whose_line_number_will_not_convert(self) -> None:
        """`\\d+` is unbounded; CPython refuses more than 4,300 digits.

        Found by Copilot on #870 and real: `int()` raises `ValueError` there,
        which would leave `classify` crashing on a document instead of
        failing one row, so the whole gate reports nothing.
        """

        digits = "9" * 4301
        self.assertEqual(
            self.reason_in_checkout(
                self.PAGE, f"`f` ({self.QUOTABLE}) [quoted: notes.md:{digits}]"),
            "quoted-not-there")

    def test_a_reason_naming_a_line_past_the_end_of_its_file(self) -> None:
        self.assertEqual(
            self.reason_in_checkout(
                self.PAGE, f"`f` ({self.QUOTABLE}) [quoted: notes.md:999999]"),
            "quoted-not-there")

    # ------------------------------------------------------------ sd:765
    #
    # A reason into code. The gate module's docstring carries the one live
    # example, quoted by line from another item's page; any edit above it
    # turned three tests red, and the only repair was that page.

    #: Python with the example in a docstring at line 5, and a same-file
    #: mention outside that declaration at line 8.
    CODE = {"bin/tool.py": (
        "def other():\n"                        # 1
        "    pass\n"                            # 2
        "\n\n"                                  # 3-4
        f'def marker_after():\n    """The shape is {QUOTABLE}."""\n'  # 5-6
        "\n"                                    # 7
        f"MENTION = '{QUOTABLE}'\n")}           # 8

    def inserted(self, sources: dict[str, str], lines: int = 7) -> dict[str, str]:
        return {name: "# inserted\n" * lines + body for name, body in sources.items()}

    def test_a_line_into_code_is_a_hint_and_an_insertion_above_it_breaks_nothing(self) -> None:
        text = f"`f` ({self.QUOTABLE}) [quoted: bin/tool.py:6]"
        self.assertEqual(self.reason_in_checkout(self.CODE, text), "quoted")
        self.assertEqual(self.reason_in_checkout(self.inserted(self.CODE), text), "quoted")

    def test_a_line_into_code_still_fails_when_the_file_does_not_carry_it(self) -> None:
        """The failing direction survives: deleting the example is still red."""
        self.assertEqual(
            self.reason_in_checkout(
                {"bin/tool.py": "def marker_after():\n    pass\n"},
                f"`f` ({self.QUOTABLE}) [quoted: bin/tool.py:1]"),
            "quoted-not-there")

    def test_a_line_into_a_page_is_still_the_claim_after_an_insertion(self) -> None:
        """CONTROL. A page keeps `path:line`, so its moved example fails."""
        self.assertEqual(
            self.reason_in_checkout(
                self.inserted(self.PAGE), f"`f` ({self.QUOTABLE}) [quoted: notes.md:3]"),
            "quoted-not-there")

    def test_a_source_locator_reason_names_the_declaration_that_carries_it(self) -> None:
        text = f"`f` ({self.QUOTABLE}) [quoted: source:bin/tool.py::marker_after]"
        self.assertEqual(self.reason_in_checkout(self.CODE, text), "quoted")
        self.assertEqual(
            self.reason_in_checkout(self.inserted(self.CODE, 400), text), "quoted")

    def test_a_source_locator_reason_is_scoped_to_its_declaration(self) -> None:
        """`other` is declared once and the file carries the example -- elsewhere."""
        self.assertEqual(
            self.reason_in_checkout(
                self.CODE, f"`f` ({self.QUOTABLE}) [quoted: source:bin/tool.py::other]"),
            "quoted-not-there")

    def test_a_source_locator_reason_that_does_not_resolve_to_one_declaration(self) -> None:
        body = self.CODE["bin/tool.py"]
        for name, sources, symbol in (
                ("renamed away", self.CODE, "gone"),
                ("declared twice", {"bin/tool.py": body + body}, "marker_after"),
                ("not Python",
                 {"bin/tool.py": f"function marker_after() {{ {self.QUOTABLE} }}\n"},
                 "marker_after"),
                ("no file", {}, "marker_after")):
            with self.subTest(name):
                self.assertEqual(
                    self.reason_in_checkout(
                        sources,
                        f"`f` ({self.QUOTABLE}) [quoted: source:bin/tool.py::{symbol}]"),
                    "quoted-not-there")

    def test_a_source_locator_reason_into_a_page_is_not_a_declaration(self) -> None:
        """A page that happens to parse as Python declares nothing: the symbol
        branch refuses markdown the way `source_declaration_error` does."""
        self.assertEqual(
            self.reason_in_checkout(
                {"notes.md": self.CODE["bin/tool.py"]},
                f"`f` ({self.QUOTABLE}) [quoted: source:notes.md::marker_after]"),
            "quoted-not-there")

    def test_a_malformed_source_locator_is_not_a_marker(self) -> None:
        """No `::symbol`, so no reason, so the citation is checked as a claim."""
        self.assertEqual(
            self.reason_in_checkout(
                self.CODE, "`f` (`bin/x.py:1`) [quoted: source:bin/tool.py]"),
            "target-missing")

    def test_the_historical_marker_survives_an_edit_above_the_line_it_names(self) -> None:
        """The defect itself, on this checkout's archived page and real module.

        `design.md` quotes bin/sd:1231 from this module by line. With a blank
        line inserted above that line in a copy of this module, the page's
        marker must still be `quoted` and the repointer must have nothing to
        say about it -- the three tests that went red were those two answers.
        """
        page = (REPO_ROOT / "docs" / "work" / "archive" / "2026-09"
                / "2026-09-04-the-citation-gate-skips-what-it-cannot-match" / "design.md")
        relative = pathlib.Path(__file__).resolve().relative_to(REPO_ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            copy = root / page.relative_to(REPO_ROOT)
            copy.parent.mkdir(parents=True)
            copy.write_text(page.read_text(encoding="utf-8"), encoding="utf-8")
            module = root / relative
            module.parent.mkdir(parents=True)
            module.write_text(
                "\n" + pathlib.Path(__file__).read_text(encoding="utf-8"), encoding="utf-8")
            with mock.patch.dict(globals(), {"REPO_ROOT": root}):
                rows = [row for row in classify([copy]) if row.path == "bin/sd"
                        and row.start == 1231]
            _, moves, refusals = repoint_document(copy, root)
        self.assertTrue(
            rows, f"the page no longer quotes {self.QUOTABLE}, so this proves nothing")
        self.assertEqual({row.reason for row in rows}, {"quoted"})
        self.assertEqual((moves, refusals), ([], []))

    def test_the_quoted_example_is_carried_once_and_only_by_marker_after(self) -> None:
        """B1 of the sd:765 review: the live marker has to be able to fail.

        A `path:line` reason into code is file-wide, so any line of this module
        carrying the token satisfies sd:5's marker. The first head of sd:765
        typed it whole in seven fixtures, and replacing the example in
        `marker_after`'s docstring left the whole module green. One line, and
        inside `marker_after`, is what keeps "delete the example and the row
        goes red" true.
        """
        import ast

        here = pathlib.Path(__file__)
        text = here.read_text(encoding="utf-8")
        carrying = [number for number, line in enumerate(text.split("\n"), 1)
                    if self.QUOTABLE in line]
        self.assertEqual(len(carrying), 1,
                         f"{self.QUOTABLE} is typed whole at lines {carrying} of"
                         f" {here.name}; build fixtures from QUOTABLE instead")
        spans = declared_spans(ast.parse(text), "marker_after")
        self.assertEqual(len(spans), 1, spans)
        first, last = spans[0]
        self.assertTrue(first <= carrying[0] <= last,
                        f"line {carrying[0]} is outside marker_after ({first}-{last})")

    def test_a_source_locator_reason_reads_a_one_line_declaration(self) -> None:
        """The window includes the declaration's first line, which is its last too."""
        sources = {"bin/tool.py": (
            f"def one(): return '{self.QUOTABLE}'\n"
            f"LIMIT = '{self.QUOTABLE}'\n")}
        for symbol in ("one", "LIMIT"):
            with self.subTest(symbol=symbol):
                self.assertEqual(
                    self.reason_in_checkout(
                        sources,
                        f"`f` ({self.QUOTABLE}) [quoted: source:bin/tool.py::{symbol}]"),
                    "quoted")

    def test_a_line_into_a_page_is_the_claim_whatever_the_suffix_case(self) -> None:
        """sd:794 N2. `.MD` and `.markdown` are pages; `.txt` is still file-wide.

        The example sits on line 2 and the reason names line 1. Read as code,
        any line of the file satisfies it, so both used to answer `quoted`.
        """
        for name in ("NOTES.MD", "notes.Md", "n.markdown", "N.MARKDOWN"):
            with self.subTest(page=name):
                sources = {name: f"pad\nthe example is {self.QUOTABLE} here\n"}
                self.assertEqual(
                    self.reason_in_checkout(sources, f"`f` ({self.QUOTABLE}) [quoted: {name}:1]"),
                    "quoted-not-there")
                self.assertEqual(
                    self.reason_in_checkout(sources, f"`f` ({self.QUOTABLE}) [quoted: {name}:2]"),
                    "quoted", "the control: the line that carries it")
        self.assertEqual(
            self.reason_in_checkout(
                {"notes.txt": f"pad\nthe example is {self.QUOTABLE} here\n"},
                f"`f` ({self.QUOTABLE}) [quoted: notes.txt:1]"),
            "quoted", "CONTROL: a text file is not a page, so its number is a hint")

    def test_a_source_locator_window_counts_lines_the_way_the_parser_does(self) -> None:
        """sd:794 N3. `splitlines()` breaks where `ast` does not, and shifts the window.

        S15 put two form-feed lines above `other`, which carries the example,
        and cited `f`: the shifted window read `other` and answered `quoted`.
        S16 and S17 put a form feed in a comment and U+2028 in a string above
        `f`, which carries it: the window slid past `f` and answered red. CRLF
        is the control, numbered alike by both.
        """
        body = f"    return '{self.QUOTABLE}'\n"
        cases = (
            ("S15 form feeds above a neighbour",
             "\x0c\n\x0c\ndef other():\n" + body + "\ndef f():\n    pass\n", "quoted-not-there"),
            ("S16 form feed in a comment", "# a \x0c comment\ndef f():\n" + body, "quoted"),
            ("S17 U+2028 in a string", "X = 'a\u2028b'\ndef f():\n" + body, "quoted"),
            ("vertical tab and \\x1c-\\x1e, \\x85, U+2029",
             "X = 'a\x0b\x1c\x1d\x1e\x85\u2029b'\ndef f():\n" + body, "quoted"),
            ("CONTROL: CRLF", ("X = 1\ndef f():\n" + body).replace("\n", "\r\n"), "quoted"),
        )
        for name, source, expected in cases:
            with self.subTest(name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = pathlib.Path(tmp)
                    (root / "bin").mkdir()
                    # Bytes, so no newline translation touches the fixture.
                    (root / "bin" / "tool.py").write_bytes(source.encode("utf-8"))
                    (root / "doc.md").write_text(
                        f"`f` ({self.QUOTABLE}) [quoted: source:bin/tool.py::f]\n",
                        encoding="utf-8")
                    with mock.patch.dict(globals(), {"REPO_ROOT": root}):
                        rows = classify([root / "doc.md"])
                self.assertEqual([row.reason for row in rows], [expected])

    def test_this_module_spells_its_invisible_separators_as_escapes(self) -> None:
        """sd:822 NB3. A literal U+2028 in a fixture is a fixture an editor deletes.

        S17 and the U+2029 case carried the character itself. An editor that
        strips "unusual line terminators" leaves both tests green while each
        tests nothing, and a diff shows no change. Read as bytes, so no newline
        translation hides one; every terminator `splitlines()` breaks on that
        `\\n` is not, so a fixture written with any of them is caught the same
        way.
        """
        self.assertEqual(invisible_separators(pathlib.Path(__file__)), {},
                         "write these as escapes, not as the character")

    def test_the_separator_guard_reads_bytes_and_not_translated_text(self) -> None:
        """sd:846 NB5. Reading the file as text would hide what the guard hunts.

        The guard above is a search for characters that universal newlines
        translates away, so `read_text` would make it report a clean file
        whatever it carried. This runs **the guard**, not `undecoded_text`
        beneath it: the earlier version of this test called `undecoded_text`
        directly, which left the guard's own read unpinned, and reverting it to
        `pathlib.Path(__file__).read_text()` was still green across all 126
        tests of the module (review-959). This module holds no carriage return
        of its own and so cannot show the difference; a fixture with a CRLF and
        a bare CR can, and holds one stray carriage return by bytes and none by
        text. The CRLF's own `\\r` is a line ending and is not counted.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fixture = pathlib.Path(tmp) / "carriage.txt"
            fixture.write_bytes(b"a\r\nb\rc\n")
            self.assertEqual(invisible_separators(fixture), {"U+000D": 1})
            self.assertEqual(fixture.read_text(encoding="utf-8").count("\r"), 0,
                             "the premise: universal newlines translates both away")

    def test_a_crlf_line_ending_is_not_an_invisible_separator(self) -> None:
        """sd:999 (review-956). A CRLF checkout is not a module full of fixtures.

        The tokenizer ends a line at `\\r\\n` exactly as it does at `\\n`, so the
        `\\r` of that pair is a line ending and never a character inside a
        fixture. Counted as one, every line of this module on a `core.autocrlf`
        checkout read as a literal separator, and the guard above failed a file
        that holds none. A bare `\\r` is still the character the guard hunts.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fixture = pathlib.Path(tmp) / "crlf.py"
            fixture.write_bytes(b"A = 1\r\nB = 2\r\n")
            self.assertEqual(invisible_separators(fixture), {})
            fixture.write_bytes(b"A = 1\r\nB = '\r'\r\n")
            self.assertEqual(invisible_separators(fixture), {"U+000D": 1})

    def test_every_direct_read_of_a_cited_file_is_named_here(self) -> None:
        """review-959. `file_lines`' docstring lists them; the list is enumerated.

        That docstring claimed every reader of a cited file came through
        `file_lines` while `quotes` opened one on two of its three branches,
        and the narrowing that followed moved the claim the wrong way -- it
        said "every reader that OPENS a cited file" when opening is exactly
        what those two lines do. A list written in prose beside the code it
        lists goes stale the next time a reader is added. So it is enumerated
        from the syntax tree instead, and each name has to appear in the claim.
        """
        openers = cited_file_openers()
        self.assertEqual(
            sorted(openers),
            ["declaration_lines", "file_lines", "quotes",
             "source_declaration_error", "undecoded_text"],
            "a reader of a cited file was added or removed; say so in"
            " `file_lines`' docstring and then update this list")
        self.assertEqual(
            len(openers["quotes"]), 2,
            "`quotes` opens a cited file on its `source:` and its"
            " into-code branch; its markdown branch goes through `file_lines`")
        claim = file_lines.__doc__ or ""
        for name in sorted(openers):
            if name == "file_lines":
                continue
            self.assertIn(f"`{name}`", claim,
                          f"`{name}` reads a cited file without going through"
                          " `file_lines`; the numbering claim must name it")

    def test_a_marker_with_no_reason_does_not_exempt(self) -> None:
        self.assertNotEqual(self.reason_for("`f` (`bin/x.py:1`) [quoted:]"), "quoted")

    def test_a_marker_whose_reason_is_blank_does_not_exempt(self) -> None:
        self.assertNotEqual(self.reason_for("`f` (`bin/x.py:1`) [quoted: ]"), "quoted")

    def test_anything_non_blank_between_the_two_breaks_the_marker(self) -> None:
        self.assertNotEqual(
            self.reason_for("`f` (`bin/x.py:1`) see [quoted: bin/sd:1]"), "quoted")

    def test_a_marker_on_the_next_line_does_not_exempt(self) -> None:
        """Testable only because the flattening is offset-preserving.

        `classify` flattens with one space per newline, so an offset in the
        flattened text is the same offset in the raw text and the line rule
        can still be asked. Under a genuinely lossy flatten this case could
        not fail, and the grammar's line clause would be decoration.
        """

        self.assertNotEqual(
            self.reason_for("`f` (`bin/x.py:1`)\n[quoted: bin/sd:1]"), "quoted")

    def test_no_line_terminator_the_grammar_names_can_be_spanned(self) -> None:
        """All four, because the first version of this checked only `\n`.

        0.71.34 is explicit: "Reasons may not span a line terminator, `\r`
        and U+2028/U+2029 included". A reason class excluding only `\n` lets
        `[absent: x\u2028y]` through, and that marker suppresses a
        missing-target failure -- a guard failing open on an exotic separator,
        which is worth less than no guard because it reads as covered. Each
        terminator is asserted separately so a partial fix cannot pass.
        """

        for name, mark in (("newline", "\n"), ("carriage return", "\r"),
                           ("line separator", "\u2028"),
                           ("paragraph separator", "\u2029")):
            with self.subTest(terminator=name):
                self.assertNotEqual(
                    self.reason_for(f"`f` (`bin/x.py:1`) [quoted: bin/sd{mark}:1]"),
                    "quoted", f"a reason spanning a {name} exempted the citation")

    def test_a_marker_covers_one_citation_and_not_the_next(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "doc.md").write_text(
                f"`f` ({self.QUOTABLE}) [quoted: {self.quoting(self.QUOTABLE)}]"
                " and `g` (`bin/x.py:2`)\n",
                encoding="utf-8")
            reasons = [row.reason for row in classify([root / "doc.md"])]
        self.assertEqual(reasons.count("quoted"), 1, reasons)


class TheSeparatorIsNarrowedByAParenthesis(unittest.TestCase):
    """Criterion 4: the comma shape, and why it is not simply admitted.

    Measured before it was decided. Of the punctuation-shaped near misses in
    the corpus, the parenthesised comma is a real anchored citation and the
    bare ones are **list separators** whose citations are anchored by the
    token to their *right*. Widening `PAIR` to the bare comma makes a
    left-scanning regex take the tail of the previous list item as the anchor
    and report a symbol nobody claimed -- the mis-attribution
    `test_prose_between_a_symbol_and_a_citation_breaks_the_anchor` was written
    to prevent, arriving through punctuation rather than prose.

    The last two fixtures are the point. A synthetic negative proves the regex
    is narrow; only the shapes taken verbatim from the corpus prove it is
    narrow enough for this corpus.
    """

    def test_a_parenthesised_comma_is_an_anchored_citation(self) -> None:
        self.assertTrue(PAREN_PAIR.search("(`status_filter`, `bin/sd:1378`)"))

    def test_a_parenthesised_semicolon_is_too(self) -> None:
        self.assertTrue(PAREN_PAIR.search("(`status_filter`; `bin/sd:1378`)"))

    def test_a_bare_comma_is_not(self) -> None:
        self.assertIsNone(PAREN_PAIR.search("`status_filter`, `bin/sd:1378`"))

    def test_a_bare_semicolon_is_not(self) -> None:
        self.assertIsNone(PAREN_PAIR.search("`status_filter`; `bin/sd:1378`"))

    def test_a_real_comma_separated_list_is_not(self) -> None:
        """Verbatim from `docs/work/.../implement.md`: three files, listed."""

        self.assertIsNone(PAREN_PAIR.search(
            "returns three files: `AGENTS.md:14`, `skills/sd-check/SKILL.md:40` and"))

    def test_a_real_list_inside_a_parenthesis_anchors_on_no_symbol(self) -> None:
        """Verbatim from `design.md`. `PAREN_PAIR` matches it; the anchor rule declines it.

        This is the shape that made the first, approximate `anchor_for` wrong:
        it filed the pair as a bare separator. Matching it and then declining
        it on the anchor is the correct path, and the two are different
        buckets, so the census can tell them apart.
        """

        text = "(`bin/sd-review:537`, `skills/sd-review/SKILL.md:39`)"
        self.assertTrue(PAREN_PAIR.search(text))
        self.assertFalse(is_symbol("bin/sd-review:537"))


class TheReasonsThatHaveNoLiveInstance(unittest.TestCase):
    """`escapes-checkout` and `quoted`, reachable only because the corpus is a parameter.

    `escapes-checkout` has never fired on real content -- measured over the
    whole corpus, no citation anywhere resolves outside the checkout -- and a
    function that fetches its own input can only be tested against whatever it
    fetches. Splitting the predicate is what lets that silence stay visible
    rather than being inferred from a silence it used to share with a live
    defect.
    """

    def classify_one(self, text: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "doc.md").write_text(text + "\n", encoding="utf-8")
            rows = classify([root / "doc.md"])
            self.assertEqual(len(rows), 1, f"{len(rows)} tokens in {text!r}")
            return rows[0].reason

    def test_a_citation_escaping_the_checkout_is_named_and_not_merged(self) -> None:
        self.assertEqual(
            self.classify_one("`frontmatter` (`../../etc/passwd:1`)"),
            "escapes-checkout")

    def test_a_missing_target_inside_the_checkout_is_a_different_bucket(self) -> None:
        self.assertEqual(
            self.classify_one("`frontmatter` (`bin/no-such-file:1`)"),
            "target-missing")

    def test_an_absent_marker_moves_it_out_of_the_failing_bucket(self) -> None:
        self.assertEqual(
            self.classify_one(
                "`frontmatter` (`bin/no-such-file:1`) [absent: deleted in 0.72.0]"),
            "declared-absent")

    def test_an_absent_marker_on_a_file_that_exists_is_the_claim_going_stale(self) -> None:
        """The pair to the test above, and the one that makes the marker honest.

        `[absent: ...]` is the only marker that asserts something about the
        world rather than about the prose, so it is the only one that can be
        contradicted by the world. A file that comes back -- restored, renamed
        back, un-deleted -- leaves the marker behind saying it is gone. Without
        this the marker is a permanent exemption bought once and never re-read,
        which is the thing this module exists to stop.
        """

        self.assertEqual(
            self.classify_one(
                "`is_symbol` (`tests/test_doc_citations.py:232`) [absent: never was]"),
            "absent-but-present")

    def test_the_elided_form_names_no_file_and_is_counted(self) -> None:
        self.assertEqual(self.classify_one("as written at `:1378`"), "elided-path")


# An explicit declaration locator carries no line claim. Keep the existing
# path:line rule above strict; only this spelling survives line movement.
STABLE_SOURCE = re.compile(r"`source:([A-Za-z0-9_./-]+)::([A-Za-z_][A-Za-z0-9_]*)`")


#: Living documents that sit above `docs/`. `CONTRIBUTING.md` is where the
#: convention itself is written down, example included, so a corpus that skips
#: it would let the one citation every reader copies rot first and unnoticed.
ROOT_DOCUMENTS = ("CONTRIBUTING.md",)


def stable_source_citations(root: pathlib.Path) -> list[tuple[pathlib.Path, str, str]]:
    """Explicit source:path::symbol locators; existing pytest node IDs are untouched."""
    # The archive rule is this reader's own, stated here rather than inherited
    # from `contained`. `classify` deliberately *does* read archives -- it
    # reports a stale archived citation instead of failing it -- so a shared
    # filter would have made one rule's corpus an accident of the other's.
    # The same living pages `anchored-line-into-code` fails and the repointer
    # rewrites, since sd:525: a locator the repointer writes into `AGENTS.md`
    # or a skill would otherwise be checked by nothing.
    documents = [d for d in corpus(root) if "archive" not in d.parts]
    documents += [root / name for name in ROOT_DOCUMENTS if (root / name).is_file()]
    documents = list(dict.fromkeys(documents))
    found = []
    for doc in contained(root, documents):
        found.extend((doc, path, symbol) for path, symbol in
                     STABLE_SOURCE.findall(doc.read_text(encoding="utf-8")))
    return found


def source_declaration_error(root: pathlib.Path, path: str, symbol: str) -> str | None:
    """Resolve a Python top-level declaration without reading outside the checkout.

    Functions, classes and assigned names are declarations; comments, strings
    and call sites cannot keep a deleted definition's citation passing. This
    also handles extensionless Python entrypoints such as bin/sd-docs-lint.

    Each cause is named before the last one is assumed (sd:794, sd:822).
    `is_file()` answers no for a directory and for a symlink loop as readily
    as for nothing there, and "target is missing" sent a reader looking for a
    file that exists. A markdown page is refused before it is parsed: one that
    happens to parse as Python said "found 0", which reads as a renamed symbol.
    """
    import ast

    try:
        target = (root / path).resolve()
        if not target.is_relative_to(root.resolve()):
            return None  # Preserve the line rule's containment exclusion.
        if not points_into_code(path):
            return f"{path}: a markdown page declares no Python symbol"
        try:
            target.stat()
        except (FileNotFoundError, NotADirectoryError):
            return f"{path}: target is missing"
        except OSError as error:
            if error.errno == errno.ELOOP:
                return f"{path}: target is a symlink loop"
            raise
        if target.is_dir():
            return f"{path}: target is a directory"
        if not target.is_file():
            return f"{path}: target is not a regular file"
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=path)
    except (OSError, UnicodeError, SyntaxError, ValueError) as error:
        return f"{path}: cannot read a Python source declaration: {error}"

    declarations = declared_at(tree, symbol)
    if len(declarations) != 1:
        return (f"{path}::{symbol}: expected one declaration at module or class "
                f"level, found {len(declarations)}")
    return None


def declared_at(tree, symbol: str) -> list[int]:
    """The lines at which a parsed module declares `symbol`.

    Module level, then one level into each class. A test method is a
    declaration a document cites by name as readily as a function is, and
    `TestCase` puts every one of them inside a class. Not deeper: a name
    defined inside a function body is a local, and a citation to one is a
    claim about an implementation detail that has no stable identity.

    Positions rather than a count, because the repointer below needs the line
    and the locator rule needs the count, and two walks would be two answers.
    """
    return [first for first, _ in declared_spans(tree, symbol)]


def declared_spans(tree, symbol: str) -> list[tuple[int, int]]:
    """`declared_at`'s walk, returning each declaration's first and last line.

    The one walk both answers come from. A `[quoted: source:<path>::<symbol>]`
    reason needs the extent, because the quoted text is looked for inside the
    declaration -- a docstring, a constant -- rather than at its first line.
    """
    import ast

    bodies = [tree.body]
    bodies += [node.body for node in tree.body if isinstance(node, ast.ClassDef)]
    found: list[tuple[int, int]] = []
    for body in bodies:
        for node in body:
            span = (node.lineno, getattr(node, "end_lineno", None) or node.lineno)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found += [span] * (node.name == symbol)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                found += [span] * sum(
                    isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
                    and name.id == symbol
                    for target in targets for name in ast.walk(target)
                )
    return found


# ---------------------------------------------------------------- repointing
#
# sd:592. The gate above catches a moved citation; nothing repointed one.
#
# MEASURED, over the 200 commits before this one, by pairing removed and added
# document lines that differ only inside a `path:line` token: 238 citation
# line numbers were rewritten by hand, spread over 45 commits -- better than
# one commit in five. 147 of them named source files, which nothing covers at
# all; the other 91 named markdown, where `--update-citations` re-baselines
# the manifest but leaves the number in the prose for a person to fix. So the
# whole 238 was hand work, and the shortcut it invites is a blunt regex over
# every citation in the page, which has already caused a defect once.
#
# THE RULE THIS FOLLOWS. A citation is repointed from its ANCHORED TEXT, never
# from the citation string: find where the thing the citation is a claim about
# lives now, and write that number. Two shapes, one walk. A symbol-anchored
# citation is a claim about the symbol, so the symbol's declaration is what is
# looked for. A `[quoted: path:line]` reason is a claim that the source quotes
# this citation verbatim, so the citation token itself is what is looked for.
#
# WHAT IT REFUSES. Two candidate lines, or none, and it moves nothing and says
# which citation it left alone. A repointer that silently picks the first match
# is the hand shortcut with a tool wrapped around it.

#: One rewrite the repointer would make, and one it declined to make.
Move = collections.namedtuple("Move", "doc citation was now")
Refusal = collections.namedtuple("Refusal", "doc citation reason")


def declaration_lines(root: pathlib.Path, path: str, symbol: str) -> list[int]:
    """The lines where a Python file declares `symbol`, or none it cannot read.

    The same walk `source_declaration_error` counts, returning positions
    instead of a verdict, so the locator rule and the repointer cannot
    disagree about what a declaration is.
    """
    import ast

    try:
        tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return []
    return declared_at(tree, symbol)


def anchor_lines(root: pathlib.Path, path: str, anchor: str) -> list[int]:
    """Every line that could be what the anchored symbol moved to.

    The declaration first, because a symbol that is declared once is
    unambiguous however many times it is used. Only when the target is not
    Python, or the name is not declared at its top level, does this fall back
    to the lines that mention it -- and a name mentioned twice then refuses,
    which is the point.
    """
    # `SYMBOL` accepts a call-shaped anchor with arguments, and `rstrip("()")`
    # removes only the trailing parenthesis: `render("x")` became `render("x"`,
    # which is not an identifier, so the AST lookup never saw `render` and the
    # fallback searched malformed text. An otherwise unique moved declaration
    # was then reported as gone. Take the callee before the first paren.
    name = anchor.split("(", 1)[0].lstrip(".")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        declared = declaration_lines(root, path, name)
        if len(declared) == 1:
            return declared
    try:
        lines = file_lines(root / path)
    except OSError:
        return []
    # The FULL anchor, not the callee. `.replace("\n", " ")` is discriminating
    # exactly because of its arguments; truncating it to `.replace` made a
    # citation this repository already carries match three lines and refuse as
    # ambiguous. Only the AST lookup above wants the bare name.
    needle = anchor.rstrip("()")
    # On an identifier boundary, never as a substring, when the needle IS a
    # bare name. `foo in line` made a line carrying only `foobar` a candidate
    # for a removed `foo`, and as the sole match it was taken -- so the tool
    # rewrote a citation to unrelated text instead of refusing. A needle
    # carrying punctuation keeps the literal search; it is specific already.
    if re.fullmatch(r"\.?[A-Za-z_][A-Za-z0-9_.]*", needle):
        found = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])")
        return [n for n, line in enumerate(lines, 1) if found.search(line)]
    return [n for n, line in enumerate(lines, 1) if needle in line]


def calls_by_name(target: pathlib.Path, start: int, end: int, name: str) -> bool:
    """Does the cited window mention `name` as an identifier? The use-site test.

    Not `names_its_symbol`, whose substring match gave both wrong answers
    (sd:765). A stale `get` whose old window held `budget` read as a use and
    refused, and an anchor `helper(a, b)` cited at `helper(a,b)` read as no use
    and converted, because the argument spelling differs. The callee alone, on
    an identifier boundary, answers both.

    The boundary is the parser's, not `\\w`'s (sd:822). Python normalises an
    identifier with NFKC before it looks the name up, so a fullwidth `get()`
    (U+FF47 U+FF45 U+FF54) calls `get`; `\\w` never matched it, and the tool
    converted that use onto the declaration. So the window is normalised
    first. And `\\w` stops at U+0301 and U+00B7, which continue an identifier,
    so `get` followed by either read as a use of `get` when it is another
    name; the neighbour test is `str.isidentifier`, the rule the parser
    applies. `name` needs no normalising: it comes from the page, and
    `anchored_repoint` admits only ASCII. What remains is that a mention in a
    comment or a string counts as a use. That is the safe direction, a
    refusal, and `tokenize` over a window cut from the middle of a file fails
    on the indentation it cannot see, which is worse than a refusal.
    """
    lines = file_lines(target)
    window = unicodedata.normalize(
        "NFKC", "\n".join(lines[max(0, start - 1 - WINDOW):end + WINDOW]))
    for found in re.finditer(re.escape(name), window):
        before = window[found.start() - 1:found.start()] if found.start() else ""
        after = window[found.end():found.end() + 1]
        if not continues_identifier(before) and not continues_identifier(after):
            return True
    return False


def continues_identifier(char: str) -> bool:
    """Would `char` continue an identifier it follows? The window's edge does not.

    `bool(char)` is the edge, and it is load-bearing in both directions:
    `("_" + "").isidentifier()` is True, so without it the empty string the
    caller passes at offset 0 of the window, and again past its last
    character, reads as a continuation. The occurrence at either edge is then
    not a use, and the citation converts onto the declaration -- the wrong
    conversion this tool exists to refuse.
    `test_the_use_test_reads_the_first_and_last_character_of_its_window`
    kills that mutation on both edges (sd:846).
    """
    return bool(char) and ("_" + char).isidentifier()


def inside(root: pathlib.Path, path: str) -> pathlib.Path | None:
    """The cited file, when it is a real file inside `root`. Containment only."""
    try:
        target = (root / path).resolve()
    except OSError:
        return None
    return target if target.is_relative_to(root.resolve()) and target.is_file() else None


def anchored_repoint(root: pathlib.Path, flat: str, match: re.Match) -> tuple | str | None:
    """Shape one: a citation anchored to a backticked symbol.

    `None` when this tool has nothing to say -- no adjacent symbol, no file, or
    a citation that is already right. A string when it refuses and why. A
    `(span, text)` pair when the number should be rewritten.
    """
    found = anchor_for(flat, match.span())
    if found is None or not found[1] or not is_symbol(found[0]):
        return None
    anchor, path = found[0], match.group(1)
    target = inside(root, path)
    if target is None:
        return None
    if points_into_code(path):
        # sd:525. A line into code is not repointed to another line, which the
        # next insertion would break again: it becomes the declaration locator,
        # right number or wrong. Only a symbol declared exactly once can be
        # named that way, and anything else is a claim for prose. The leading
        # dot is kept: `.replace(...)` is an attribute, and stripping it would
        # name an unrelated top-level `replace` instead of refusing.
        name = anchor.split("(", 1)[0]
        declared = (declaration_lines(root, path, name)
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else [])
        if len(declared) != 1:
            return (f"`{anchor}` is declared {len(declared)} times in {path}, so"
                    " no source: locator names it; say it in prose")
        # One declaration is not yet the claim. A cited line that carries the
        # name while the declaration is elsewhere is a USE -- `d.get('x')`,
        # `'a'.replace(...)`, `helper()` -- and the prose is about that call,
        # not about a same-named definition. A window without the name is a
        # stale citation to the declaration, which is what converts.
        start, end = int(match.group(2)), int(match.group(3) or match.group(2))
        if (not start - WINDOW <= declared[0] <= end + WINDOW
                and calls_by_name(target, start, end, name)):
            return (f"`{anchor}` at {path}:{start} is a use, not its declaration at"
                    f" {declared[0]}; say it in prose")
        return (match.start(1), match.end()), f"source:{path}::{name}`"
    start = int(match.group(2))
    end = int(match.group(3) or match.group(2))
    if names_its_symbol(anchor, target, start, end):
        return None
    candidates = anchor_lines(root, path, anchor)
    if not candidates:
        return f"`{anchor}` is gone from {path}"
    if len(candidates) > 1:
        listed = ", ".join(str(n) for n in candidates[:5])
        return f"`{anchor}` is at {len(candidates)} lines of {path} ({listed}); ambiguous"
    moved = candidates[0]
    text = f"{moved}-{moved + end - start}`" if match.group(3) else f"{moved}`"
    return (match.start(2), match.end()), text


def quoted_repoint(
    doc: pathlib.Path, root: pathlib.Path, flat: str, match: re.Match, reason: str
) -> tuple | str | None:
    """Shape two: a `[quoted: path:line]` reason, whose anchored text is the citation.

    sd:568's marker is a citation as much as the token it exempts, and it goes
    stale the same way -- so the same walk repoints it, looking for the line
    that carries this citation verbatim rather than for a symbol.

    What moves here is the REASON, never the citation it covers. A quoted
    citation is a quotation of somebody else's citation, deliberately inert;
    rewriting its number would edit the example. The first draft of this tool
    did exactly that -- it proposed rewriting a citation a page quotes, which
    is the PR #868 defect arriving through the tool built to prevent it. The
    caller therefore routes a quoted citation here and nowhere
    else, and `None` from this function means the marker is already right.

    Only a `path:line` into markdown ever moves. Into code the number is a
    hint `quotes` does not check, so there is nothing to move while the file
    carries the example, and a `source:` reason has no number at all (sd:765).
    """
    marker = (reason,)
    if quotes(marker[0], match.group(0), doc, root):
        return None
    span = MARKER.search(flat, match.end())
    if span is None or span.group(2).strip() != reason:
        return None
    parsed = QUOTED_REASON.fullmatch(reason)
    if parsed is not None and parsed.group(2):
        # sd:765. A declaration locator has no number to move, so a failed one
        # is a claim for a person: the declaration was renamed, duplicated, or
        # no longer carries the example. Each cause is named before the last
        # one is assumed (sd:794): a missing file, a file that is not Python,
        # and the page itself all used to print "is not inside one declaration".
        path, symbol = parsed.group(1), parsed.group(2)
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root.resolve()):
            return f"[quoted: {reason}] names a file outside the checkout"
        if resolved == doc.resolve():
            return f"[quoted: {reason}] names the page it sits on, which cannot quote itself"
        problem = source_declaration_error(root, path, symbol)
        if problem is not None:
            return f"[quoted: {reason}] -- {problem}"
        return (f"[quoted: {reason}] -- {match.group(0)} is not inside one"
                f" declaration of `{symbol}` in {path}")
    path, _, _ = reason.rpartition(":")
    source = inside(root, path)
    if source is None or source.resolve() == doc.resolve():
        return f"[quoted: {reason}] names no readable source"
    lines = file_lines(source)
    carrying = [n for n, line in enumerate(lines, 1) if match.group(0) in line]
    if not carrying:
        return f"[quoted: {reason}] -- {match.group(0)} is gone from {path}"
    if len(carrying) > 1:
        listed = ", ".join(str(n) for n in carrying[:5])
        return f"[quoted: {reason}] -- {match.group(0)} is at {listed}; ambiguous"
    return span.span(2), f"{path}:{carrying[0]}"


def repoint_document(
    doc: pathlib.Path, root: pathlib.Path = REPO_ROOT
) -> tuple[str, list[Move], list[Refusal]]:
    """One document, repointed: the new text, what moved, and what refused to.

    Read strictly, unlike every other reader in this module. They compare and
    discard; this one hands its result to `--apply`, and `errors="replace"`
    would substitute U+FFFD for a byte it could not decode and then write that
    substitution back over the file. A reader that mangles is a wrong answer;
    a writer that mangles is a lost one, so this raises instead.
    """
    raw = doc.read_text(encoding="utf-8")
    flat = raw.replace("\n", " ")
    edits: list[tuple[tuple[int, int], str]] = []
    moves: list[Move] = []
    refusals: list[Refusal] = []
    for match in TOKEN.finditer(flat):
        if not match.group(1):
            continue
        # Routed, not chained. A citation covered by a valid `quoted` marker is
        # a quotation and its own number is never touched; only the marker's
        # reason can move. Falling through from one shape to the other is how
        # the first draft proposed rewriting a quoted example.
        marker = marker_after(flat, raw, match.end())
        if marker is not None and marker[0] == "quoted":
            outcome = quoted_repoint(doc, root, flat, match, marker[1])
        else:
            outcome = anchored_repoint(root, flat, match)
        if outcome is None:
            continue
        if isinstance(outcome, str):
            refusals.append(Refusal(doc, match.group(0), outcome))
            continue
        span, text = outcome
        edits.append((span, text))
        moves.append(Move(doc, match.group(0), raw[span[0]:span[1]], text))
    for (begin, stop), text in sorted(edits, reverse=True):
        raw = raw[:begin] + text + raw[stop:]
    return raw, moves, refusals


def repointable() -> list[pathlib.Path]:
    """The documents this tool will rewrite: living pages, never the archive.

    An archived page is a record of what was true when it was archived, and
    `classify` already reports a stale archived citation without failing it.
    Repointing one would rewrite the record, which is the same objection rule
    6 makes to re-anchoring a citation below a Log heading.

    Deduplicated, and that is not tidiness. `corpus()` already enumerates every
    tracked markdown file and `ROOT_DOCUMENTS` names some of the same pages, so
    a document reached twice is written twice by `--apply` and reported twice
    by the dry run -- a reader counting the moves is told there are two where
    there is one.
    """
    documents = [doc for doc in corpus() if "archive" not in doc.parts]
    documents += [REPO_ROOT / name for name in ROOT_DOCUMENTS
                  if (REPO_ROOT / name).is_file()]
    seen: dict[pathlib.Path, None] = {}
    for doc in documents:
        seen.setdefault(doc.resolve(), None)
    return list(seen)


def repoint_main(argv: list[str]) -> int:
    """`--repoint` names every move; `--repoint --apply` takes them.

    Dry run is the default and not a flag, because the failure mode this tool
    exists to avoid is an unread bulk rewrite of citation numbers.
    """
    apply = "--apply" in argv
    moved = refused = 0
    for doc in repointable():
        text, moves, refusals = repoint_document(doc)
        for move in moves:
            print(f"{doc}: {move.citation} -> {move.now.rstrip('`')}")
        for refusal in refusals:
            print(f"{doc}: REFUSED {refusal.citation}: {refusal.reason}")
        moved += len(moves)
        refused += len(refusals)
        if apply and moves:
            doc.write_text(text, encoding="utf-8")
    verb = "repointed" if apply else "would repoint"
    print(f"{verb} {moved} citation(s); refused {refused}")
    return 1 if refused else 0


class ACitationPastTheEndOfItsFile(unittest.TestCase):
    """sd:811. The line is resolved whether or not a symbol stands beside it.

    Three buckets used to swallow this. `no-adjacent-anchor`,
    `separator-not-adjacent` and `anchor-not-a-symbol` all mean "not a claim
    about a symbol", and each of them returned before anything opened the
    cited file -- so a citation in prose could name any line at all, including
    one the file does not have, and be counted as classified.
    `bin/sd_skill.py:217` into an 89-line file passed `make check` green.

    The census said so and nobody could read it: `line-into-code 0 0 0` looks
    like "no citations into code" and means "no *symbol-anchored* ones". The
    bucket is `anchored-line-into-code` now, and this one carries what its
    silence used to cover.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        (self.root / "bin").mkdir()
        (self.root / "bin" / "tool.py").write_text(
            "def render():\n    return 1\n", encoding="utf-8")
        (self.root / "notes.md").write_text("one\ntwo\n", encoding="utf-8")
        (self.root / "docs").mkdir()

    def reason_for(self, text: str, name: str = "page.md") -> str:
        doc = self.root / "docs" / name
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(text, encoding="utf-8")
        with mock.patch.dict(globals(), {"REPO_ROOT": self.root}):
            rows = classify([doc])
        self.assertEqual(len(rows), 1, f"{len(rows)} tokens in {text!r}")
        return rows[0].reason

    def test_an_unanchored_citation_past_the_end_is_red(self) -> None:
        """The filed instance's shape: prose, no symbol beside it, line 9 of 2."""

        self.assertEqual(
            self.reason_for("The reader is over at `bin/tool.py:9` today.\n"),
            "line-past-end")

    def test_the_same_citation_in_range_is_left_where_it_was(self) -> None:
        """CONTROL. Only the line number differs, and the bucket must not move."""

        self.assertEqual(
            self.reason_for("The reader is over at `bin/tool.py:2` today.\n"),
            "no-adjacent-anchor")

    def test_a_range_is_judged_by_its_end(self) -> None:
        """`1-9` starts inside the file. The claim still reaches past the end."""

        self.assertEqual(
            self.reason_for("Read `bin/tool.py:1-9` for the whole of it.\n"),
            "line-past-end")

    def test_an_anchored_citation_past_the_end_is_this_bucket_and_not_the_code_rule(
            self) -> None:
        """Precedence, stated rather than left to branch order.

        `anchored-line-into-code` says "the number will go stale". This says
        "the number is already impossible". Reporting the weaker one would
        send the reader to the repointer, which cannot rewrite a line that
        does not exist.
        """

        self.assertEqual(
            self.reason_for("`render` (`bin/tool.py:9`)\n"), "line-past-end")

    def test_a_bare_separator_does_not_hide_it_either(self) -> None:
        self.assertEqual(
            self.reason_for("`render`, `bin/tool.py:9`\n"), "line-past-end")

    def test_an_anchor_that_is_not_a_symbol_does_not_hide_it_either(self) -> None:
        self.assertEqual(
            self.reason_for("`bin/tool.py` (`bin/tool.py:9`)\n"), "line-past-end")

    def test_a_markdown_target_is_read_the_same_way(self) -> None:
        """Not a code-only rule. A page has a last line too."""

        self.assertEqual(self.reason_for("see `notes.md:9`\n"), "line-past-end")
        self.assertEqual(self.reason_for("see `notes.md:2`\n"), "no-adjacent-anchor")

    def test_a_trailing_newline_does_not_add_a_line(self) -> None:
        """The off-by-one that would let the commonest spelling through.

        `"one\\ntwo\\n".split("\\n")` is three elements and the file has two
        lines. Count the empty tail and `notes.md:3` reads as in range.
        """

        self.assertEqual(line_count(self.root / "notes.md"), 2)
        self.assertEqual(self.reason_for("see `notes.md:3`\n"), "line-past-end")

    def test_an_archived_page_reports_and_does_not_fail(self) -> None:
        """The standing archive ruling, unchanged. 37 of the 49 live here."""

        self.assertEqual(
            self.reason_for("The reader is at `bin/tool.py:9`.\n", "archive/old.md"),
            "archived-stale")

    def test_a_quoted_example_is_still_an_example(self) -> None:
        """A page explaining this gate must be able to print a past-end citation."""

        (self.root / "other.md").write_text(
            "# other\n\nthe example writes `bin/tool.py:9` here\n", encoding="utf-8")
        self.assertEqual(
            self.reason_for("`render` (`bin/tool.py:9`) [quoted: other.md:3]\n"),
            "quoted")

    def test_an_absent_marker_does_not_hide_it_either(self) -> None:
        """`[absent: ...]` claims a file is gone. A line past the end of a file
        that exists is not that claim, and the marker must not cover it.

        The past-end check used to stand down for any citation carrying the
        marker, so an unanchored `bin/tool.py:9 [absent: ...]` landed in
        `no-adjacent-anchor` with nothing checking the reason: the file could
        grow back past line 9, or the symbol come back, and the page stayed
        green. That is a marker no re-check can fail, which is the silencer
        shape this module refuses (review of sd:829's PR, Copilot). The
        disposition for content that went on purpose is prose -- drop the
        number, keep the file name, say so in an `[absent: <reason>]` note
        beside the symbol -- and a marked past-end citation is red like an
        unmarked one.
        """

        self.assertEqual(
            self.reason_for("`render()` at `bin/tool.py:9`"
                            " [absent: removed when step 3 shipped]\n"),
            "line-past-end")
        # CONTROL: a marked citation to a line the file has is unchanged.
        self.assertEqual(
            self.reason_for("`render()` at `bin/tool.py:2`"
                            " [absent: still here]\n"),
            "no-adjacent-anchor")

    def test_a_token_whose_path_is_not_a_file_is_untouched(self) -> None:
        """`sd:811` is an item reference and `TOKEN` matches it.

        Nearly three thousand rows in the live corpus have this shape. The
        check is gated on there being a file to open, so none of them moves;
        without that gate every item reference in the repository turns red.
        """

        self.assertEqual(self.reason_for("filed as `sd:811` today\n"),
                         "no-adjacent-anchor")

    def test_an_insertion_cannot_turn_this_red(self) -> None:
        """sd:525's objection, answered rather than assumed away.

        The rule removed in sd:525 went red when a lane that was not editing
        documentation inserted a line above a cited one. This one cannot go
        red on an insertion: it only lengthens a file, so a citation that was
        in range stays in range. Only a deletion reaches it, and then the line
        really is gone -- a red a code-only lane can raise, and is meant to.
        """

        self.assertEqual(self.reason_for("see `bin/tool.py:2`\n"),
                         "no-adjacent-anchor")
        target = self.root / "bin" / "tool.py"
        target.write_text("# inserted\n" * 20 + target.read_text(encoding="utf-8"),
                          encoding="utf-8")
        self.assertEqual(self.reason_for("see `bin/tool.py:2`\n"),
                         "no-adjacent-anchor")
        target.write_text("def render():\n", encoding="utf-8")
        self.assertEqual(self.reason_for("see `bin/tool.py:2`\n"), "line-past-end")

    def test_a_fixture_document_outside_the_checkout_does_not_crash_the_scan(
            self) -> None:
        """`REPO_ROOT` is not moved here, and the document is not under it.

        The shape every other fixture class in this module uses: a page under
        a temporary directory, classified against the real checkout, citing a
        real file. The carried list sd:829 deleted named its documents
        relative to `REPO_ROOT`, and taking that name of a document outside
        it raised `ValueError` out of the whole scan. A gate that crashes on
        a document reports nothing at all rather than one bad row, which is
        the failure this module's own docstring opens by naming. The list is
        gone; the guard against the next `relative_to` in the scan stays.
        """

        with tempfile.TemporaryDirectory() as tmp:
            doc = pathlib.Path(tmp) / "doc.md"
            doc.write_text("`frontmatter` (`bin/sd_skill.py:217`)\n",
                           encoding="utf-8")
            self.assertEqual([row.reason for row in classify([doc])],
                             ["line-past-end"])

    def test_the_bucket_fails_the_gate(self) -> None:
        """Counting a defect is not catching it: the bucket has to be red."""

        (self.root / "docs" / "page.md").write_text(
            "The reader is at `bin/tool.py:9`.\n", encoding="utf-8")
        with mock.patch.dict(globals(), {"REPO_ROOT": self.root}):
            with self.assertRaisesRegex(AssertionError, "line-past-end"):
                DocCitationTests().test_the_red_buckets_are_empty()

    def test_the_failure_names_the_whole_range_the_citing_line_and_the_fix(
            self) -> None:
        """A range judged by its end has to be reported by its whole range.

        The first message printed `row.start` alone, so `bin/tool.py:1-9`
        failed as "`bin/tool.py:1`" -- a line the file has -- and the lane
        reading it was sent to look at a line that is fine (review-951, B2).
        A red that a code-only deletion can raise on a page the lane does not
        own is only acceptable if the message says what to do: which page and
        line cites it, how long the file is today, and the two fixes.
        """

        (self.root / "docs" / "page.md").write_text(
            "intro\n\nRead `bin/tool.py:1-9` for the whole of it.\n",
            encoding="utf-8")
        with mock.patch.dict(globals(), {"REPO_ROOT": self.root}):
            with self.assertRaises(AssertionError) as caught:
                DocCitationTests().test_the_red_buckets_are_empty()
        message = str(caught.exception)
        self.assertIn("docs/page.md:3: `bin/tool.py:1-9`", message)
        self.assertIn("bin/tool.py has 2 lines", message)
        self.assertIn("repoint", message)
        self.assertIn("[absent: <reason>]", message)

    def test_line_zero_is_past_the_end_too(self) -> None:
        """Files start at line 1. `:0` names nothing, and only the lower bound says so.

        Every other case here is caught by the upper bound alone, so without
        this the `1 <=` half of `within_file` is an untested clause.
        """

        self.assertEqual(self.reason_for("see `bin/tool.py:0`\n"), "line-past-end")

    def test_a_reversed_range_is_judged_by_its_larger_line(self) -> None:
        """`9-2` into a 2-line file. The end is in range; the claim is not.

        Judged by `end` alone this was `no-adjacent-anchor` (review-951, B3):
        a range written backwards still reaches line 9, whichever way round it
        is spelled, so the predicate reads the larger of the two.
        """

        self.assertEqual(self.reason_for("see `bin/tool.py:9-2`\n"), "line-past-end")

    def test_a_range_with_a_zero_at_either_end_is_judged_by_its_smaller_line(
            self) -> None:
        """`2-0` into a 2-line file. The larger line is in range; the smaller is 0.

        sd:838 asked whether a range whose end precedes its start is a gate
        failure or input to normalise. It is judged, not normalised, and both
        of its lines are: `:0` alone has `start == end`, and `9-2` is caught
        by its larger line, so between them the predicate's `min(start, end)`
        was reachable by neither -- a predicate reading `start` in its place
        passed `2-0` as `no-adjacent-anchor`, a citation to a line no file has,
        and one reading `end` passed `0-2` the same way. Both spellings, so
        that neither substitution survives.
        """

        for spelling in ("2-0", "0-2"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    self.reason_for(f"see `bin/tool.py:{spelling}`\n"),
                    "line-past-end")

class TheHistoricalNumbersInThisModule(unittest.TestCase):
    """sd:799's nine stale sites, answered by pinning them instead of moving them.

    Lines 1231 and 1378 of `bin/sd`, and 501-506 of `bin/sd-status`, are step
    8-iv's record and an earlier draft's mis-attribution. `frontmatter` is at
    1259 today and `status_filter` at 1421, so all three read as stale to
    anyone who checks them against the tree -- and repointing them would
    delete the evidence the module's first paragraph is built on.

    Nothing gates a `path:line` inside a `.py` file, so without this the next
    reader to notice the mismatch either repoints them, losing the record, or
    files them stale again. This is the assertion that makes the ruling
    checkable: the counts are exact, so a repoint fails here and an extra
    historical number cannot be smuggled in beside them.
    """

    def occurrences(self) -> collections.Counter:
        """Every whole `path:line` token typed in this file, counted.

        Not flattened. A citation token cannot span a line terminator, and
        `TOKEN` scanned across a file joined into one 130KB line costs
        twenty-five seconds of backtracking for an answer identical to this
        one.
        """

        return collections.Counter(
            f"{path}:{start}" + (f"-{end}" if end else "")
            for path, start, end
            in TOKEN.findall(pathlib.Path(__file__).read_text(encoding="utf-8")))

    def test_the_historical_numbers_are_not_repointed(self) -> None:
        counts = self.occurrences()
        self.assertEqual(counts["bin/sd:1378"], 7, "step 8-iv's status_filter line")
        self.assertEqual(counts["bin/sd:1231"], 1, "step 8-iv's frontmatter line")
        self.assertEqual(counts["bin/sd-status:501-506"], 1,
                         "the 90-character rule's mis-attribution")

    def test_they_are_stale_against_the_tree_which_is_the_whole_point(self) -> None:
        """The control. Green here would mean the ruling is about nothing."""

        self.assertNotIn(
            "status_filter",
            numbered_lines((REPO_ROOT / "bin" / "sd").read_text(
                encoding="utf-8", errors="replace"))[1377])
        self.assertNotIn(
            "def frontmatter",
            numbered_lines((REPO_ROOT / "bin" / "sd").read_text(
                encoding="utf-8", errors="replace"))[1230])
        # The third record, which the two above do not reach. The claim it
        # carries is that those lines are "a docstring that does not happen to
        # repeat the key name"; they are a `#:` comment block today, so the
        # window holds no docstring quote at all. Without this the count above
        # would keep pinning a range that had quietly become accurate again.
        window = numbered_lines((REPO_ROOT / "bin" / "sd-status").read_text(
            encoding="utf-8", errors="replace"))[500:506]
        self.assertNotIn('"""', "\n".join(window))


class StableSourceCitationTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.target = self.root / "bin" / "tool"
        self.target.parent.mkdir()
        self.target.write_text("def render():\n    pass\n", encoding="utf-8")

    def test_inserted_lines_do_not_break_a_declaration_locator(self) -> None:
        self.assertIsNone(source_declaration_error(self.root, "bin/tool", "render"))
        self.target.write_text("# inserted\n" * 100 + self.target.read_text(), encoding="utf-8")
        self.assertIsNone(source_declaration_error(self.root, "bin/tool", "render"))

    def test_deleted_renamed_or_comment_only_symbols_fail(self) -> None:
        for source in (
            "", "def renamed():\n    pass\n", "# def render():\ntext = 'render'\n", "render()\n"
        ):
            with self.subTest(source=source):
                self.target.write_text(source, encoding="utf-8")
                self.assertIn(
                    "found 0", source_declaration_error(self.root, "bin/tool", "render") or ""
                )

    def test_duplicate_declarations_fail_as_ambiguous(self) -> None:
        self.target.write_text(self.target.read_text() * 2, encoding="utf-8")
        self.assertIn(
            "found 2", source_declaration_error(self.root, "bin/tool", "render") or ""
        )

    def test_constants_and_classes_are_declarations(self) -> None:
        self.target.write_text("LIMIT = 2\nLABEL: str = 'name'\nclass Reader:\n    pass\n")
        for symbol in ("LIMIT", "LABEL", "Reader"):
            with self.subTest(symbol=symbol):
                self.assertIsNone(source_declaration_error(self.root, "bin/tool", symbol))

    def test_missing_inside_target_fails(self) -> None:
        self.assertIn(
            "target is missing",
            source_declaration_error(self.root, "bin/missing", "render") or "",
        )

    def test_outside_targets_are_never_opened(self) -> None:
        from unittest import mock

        link = self.root / "escape"
        link.symlink_to(self.root.parent / "outside.py")
        with mock.patch.object(pathlib.Path, "read_text", side_effect=AssertionError("opened")):
            for path in (str(self.root.parent / "outside.py"), "../outside.py", "escape"):
                with self.subTest(path=path):
                    self.assertIsNone(source_declaration_error(self.root, path, "render"))

    def test_only_explicit_locators_are_scanned_and_archives_stay_excluded(self) -> None:
        docs = self.root / "docs"
        archive = docs / "archive"
        archive.mkdir(parents=True)
        (docs / "current.md").write_text(
            "`render` (`source:bin/tool::render`) and `gh` (`GH_RECORDER`)\n"
            "`render` (`bin/tool:1`) and `tests/missing.py::test_historical`\n", encoding="utf-8"
        )
        (archive / "old.md").write_text("`source:bin/missing::gone`\n", encoding="utf-8")
        self.assertEqual(
            stable_source_citations(self.root), [(docs / "current.md", "bin/tool", "render")]
        )

    def test_root_documents_are_scanned_beside_the_docs_tree(self) -> None:
        """`CONTRIBUTING.md` states the convention, so its own example must resolve.

        Scoping the corpus to `docs/**` would leave the citation every reader
        copies as the only one nothing checks.
        """

        docs = self.root / "docs"
        docs.mkdir()
        (docs / "current.md").write_text("`source:bin/tool::render`\n", encoding="utf-8")
        contributing = self.root / "CONTRIBUTING.md"
        contributing.write_text("prefer `source:bin/tool::render`\n", encoding="utf-8")
        self.assertIn(
            (contributing, "bin/tool", "render"), stable_source_citations(self.root)
        )
        contributing.write_text("prefer `source:bin/tool::deleted`\n", encoding="utf-8")
        self.assertIn(
            "found 0",
            source_declaration_error(self.root, "bin/tool", "deleted") or "",
        )

    def test_a_skill_page_is_scanned_and_its_missing_symbol_is_red(self) -> None:
        """The repointer writes into every living page, so every living page is read.

        A corpus scoped back to `docs/**` would pass a locator in a skill whose
        symbol was renamed away, and nothing else checks that page.
        """

        skill = self.root / "skills" / "sd-tool" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("run `render` (`source:bin/tool::deleted`)\n", encoding="utf-8")
        self.assertIn((skill, "bin/tool", "deleted"), stable_source_citations(self.root))
        failures = [problem for _, path, symbol in stable_source_citations(self.root)
                    if (problem := source_declaration_error(self.root, path, symbol))]
        self.assertEqual(len(failures), 1)
        self.assertIn("found 0", failures[0])

    def test_a_symlinked_document_is_never_read(self) -> None:
        """The document is an input too, not only the file its citation names.

        `glob` returns a symlink as readily as a file and `read_text` follows
        it, so without this a tracked `docs/current.md` could point anywhere
        and have CI read it. Asserted on both walks, because both glob the
        same tree.
        """

        docs = self.root / "docs"
        docs.mkdir()
        outside = self.root.parent / f"outside-{os.getpid()}.md"
        outside.write_text(
            "`source:bin/tool::render` and `render` (`bin/tool:1`)\n", encoding="utf-8"
        )
        self.addCleanup(outside.unlink)
        (docs / "escape.md").symlink_to(outside)
        (docs / "real.md").write_text("`source:bin/tool::render`\n", encoding="utf-8")

        collected = stable_source_citations(self.root)
        self.assertEqual([doc for doc, _, _ in collected], [docs / "real.md"])
        self.assertEqual(contained(self.root, [docs / "escape.md"]), [])

        with mock.patch.dict(globals(), {"REPO_ROOT": self.root}):
            self.assertEqual(anchored_citations(), [])

    def test_existing_line_citations_still_reject_line_movement(self) -> None:
        """Into a page, where `path:line` is still the form, a move still fails."""
        from unittest import mock

        docs = self.root / "docs"
        docs.mkdir()
        notes = self.root / "notes.md"
        notes.write_text("render\n", encoding="utf-8")
        (docs / "current.md").write_text("`render` (`notes.md:1`)\n", encoding="utf-8")
        notes.write_text("# inserted\n" * 100 + notes.read_text(), encoding="utf-8")
        with mock.patch.dict(globals(), {"REPO_ROOT": self.root}):
            with self.assertRaisesRegex(AssertionError, "is not at"):
                DocCitationTests().test_every_anchored_citation_names_its_symbol_at_the_cited_line()

    def test_scan_control_accepts_a_corpus_using_only_stable_locators(self) -> None:
        from unittest import mock

        docs = self.root / "docs"
        docs.mkdir()
        (docs / "current.md").write_text("`source:bin/tool::render`\n", encoding="utf-8")
        with mock.patch.dict(globals(), {"REPO_ROOT": self.root}):
            self.assertEqual(anchored_citations(), [])
            DocCitationTests().test_the_scan_reaches_the_documents()

    def test_every_explicit_source_locator_resolves_in_the_live_corpus(self) -> None:
        citations = stable_source_citations(REPO_ROOT)
        # Empty would pass the loop below on no evidence (sd:999).
        self.assertTrue(citations, "no source:<path>::<symbol> locator was found")
        failures = []
        for doc, path, symbol in citations:
            problem = source_declaration_error(REPO_ROOT, path, symbol)
            if problem:
                failures.append(f"{doc.relative_to(REPO_ROOT)}: {problem}")
        self.assertEqual(failures, [], "\n".join(failures))


#: **Which skills must qualify the citation: all of them, and none is named.**
#:
#: The rule has no exception list because the reason is structural rather than
#: per-skill. A skill is installed to the platform home and read with the
#: reader's cwd in whatever checkout encloses it (R10-D6); nothing makes that
#: checkout this one. `sd-research-repo` is read from a research repo,
#: `sd-review`/`sd-ship` from whatever repository is being developed.
#: So a rule path any of them cites resolves against a checkout that need not
#: have the file. No skill is exempt, so no skill has to be remembered, and
#: both ends of this scan are walked from the filesystem: the documents from
#: `skills/`, the paths that matter from `.claude/rules/`.
#:
#: The predecessor hardcoded `FOREIGN_SKILL = "skills/sd-research-repo"`, which
#: was narrower than the defect it was written for -- four skills carried the
#: citation and one was watched.
SKILLS_DIR = "skills"

#: **Which paths are in scope: the pack's authored rules, enumerated.**
#:
#: Not "every path that happens to resolve in this checkout", which was the
#: predecessor's test and is wrong the moment it leaves one skill. Run broadly
#: it flags `.github/sd-review.json`, `.github/workflows/sd-review-route.yml`
#: and `.github/sd-status.json` -- per-repository configuration that the
#: *reader's* checkout is supposed to carry, which `bin/sd_setup_github.py`
#: writes there. Those are correct unqualified, and rewriting them to name the
#: pack would be the worse bug.
#:
#: What makes `.claude/rules/` different is that it is the one surface the pack
#: authors, tells the reader to go and *read* for authoritative content, and
#: cannot put in the reader's checkout:
#:
#:   * it is not installed -- `bin/sd_install.py` carries zero `.claude/rules`
#:     references, so it cannot be fanned out;
#:   * it cannot be shipped as a copy either -- the caps table is allowed
#:     exactly two copies and `tests/test_workflow_policy.py::ReviewTable`
#:     enforces that, so a third beside a skill fails the gate;
#:   * and resolving it against the wrong checkout is silent. The reader finds
#:     no file, reads no cap, and the review pass proceeds as if it had one.
#:     Confirmed absent in all seven research repos on this machine.
#:
#: So the invariant is the only one left: the prose names the checkout that
#: holds it. The rule files themselves are globbed, not listed, so a rule added
#: next to this one is covered on the day it is written.
RULES_DIR = ".claude/rules"

#: A backticked path with a directory separator. Only those make a claim about
#: some checkout's layout; a bare `CLAUDE.md` or `research.conf.py` is a name
#: the research-repo standard defines and not a pointer into this repository.
#: The leading `\.?` is load-bearing and was missing in the first draft: every
#: path this check exists for begins `.claude/`, so without it the scan matched
#: nothing and the live test passed over the defect it was written to catch.
BACKTICKED_PATH = re.compile(r"`(\.?[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:md|py|json|toml|ya?ml|sh))`")
#: Word-bounded, and that is the whole point. An unbounded `pack` is satisfied
#: by "the package documentation", "the packaging notes" or "unpack the brief",
#: so an unqualified citation with any of those within the window read as
#: qualified and the scan passed it -- verified against all three before this
#: was tightened. A guard with a false negative is worse than no guard, because
#: the class then looks clean. The boundary still matches what the prose
#: actually says: `pack`, `pack's`, the `pack` in `sd-ai-command-pack`
#: (whose preceding `-` is a non-word character), and `sd-workflow`, the
#: repository's name since sd:6027, which has no `pack` in it.
QUALIFIER = re.compile(r"\bpack\b|\bsd-workflow\b", re.IGNORECASE)

#: A sentence-end candidate: a terminator before whitespace or the end of the
#: text. A dot inside a backticked path is followed by a letter, so it ends
#: nothing. Neither does the dot of `e.g.`, `i.e.` or `etc.`: "the pack's
#: rules (e.g. `<path>`)" is one sentence (review round 1 on sd:999), and the
#: capital alone does not settle "e.g. Makefile". `sentence_ends` keeps a
#: candidate only when the next sentence opens like one.
#:
#: The qualifier is read in the citation's own sentence, on *both* sides of
#: the citation, which the first version did not. English puts the
#: qualification either way round -- "the cap is in the sd-workflow
#: checkout's `<path>`" and "`<path>` ... that file lives only in the
#: sd-workflow checkout" are the same statement -- and a guard that
#: accepts one word order and not the other enforces a house style instead of
#: the invariant. Both forms are live in `skills/` today.
#:
#: A sentence, not a character window (sd:999, review-836). The 100-character
#: window that came before let a neighbouring sentence vouch for the path:
#: "`<path>`. See the pack's release notes." read as qualified.
SENTENCE_END = re.compile(r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)[.!?](?=\s|$)")

#: Markup a sentence may open on before its first word: emphasis, a link, a
#: parenthesis or a quote.
SENTENCE_OPENER_MARKUP = "*_[(\"'\u201c\u2018"


def sentence_ends(flat: str) -> list[int]:
    """Where each sentence of `flat` ends, as offsets just past the terminator.

    The next sentence opens on a capital in any script, a digit or a code
    span, after any opening markup. "**Read** `<path>`", "`<path>` holds"
    and "Über" each open one (review round 3 on sd:999). An ASCII-capital
    test merged those into the sentence before, so a pack mention there
    vouched for an unrelated citation. A lower-case word after the dot does
    not open a sentence: that is an abbreviation the lookbehinds missed.
    """

    ends = []
    for end in SENTENCE_END.finditer(flat):
        opener = flat[end.end():end.end() + 40].lstrip().lstrip(SENTENCE_OPENER_MARKUP)
        if not opener or opener[0].isupper() or opener[0].isdigit() or opener[0] == "`":
            ends.append(end.end())
    return ends

#: The one way the next sentence may carry the qualifier: by opening on the
#: file just cited. "`<path>` gives the cap. That file lives only in the
#: sd-workflow checkout" is one claim in two sentences, and
#: a skill page writes it that way.
BACK_REFERENCE = re.compile(r"\s*(?:(?:that|this|the)\s+(?:rule\s+)?file|it)\b", re.IGNORECASE)

#: A blank line ends a paragraph, and nothing in the next one is about this
#: citation.
PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")


def skill_documents(root: pathlib.Path) -> list[pathlib.Path]:
    """Every authored `*.md` under `skills/`, walked from the filesystem.

    Enumeration, not a roster. A skill that acquires a pack-path citation next
    month is covered the day it is written, with nobody updating anything --
    which is the property the hardcoded predecessor did not have.
    """

    return contained(root, sorted((root / SKILLS_DIR).rglob("*.md")))


def pack_rule_paths(root: pathlib.Path) -> set[str]:
    """The pack's authored rule files, as the paths a skill would cite them by.

    Globbed, so the scope grows with the directory rather than with anyone's
    memory of what is in it.
    """

    rules = root / RULES_DIR
    return {
        str(rule.relative_to(root))
        for rule in contained(root, sorted(rules.rglob("*.md")))
    }


def rule_path_citations(root: pathlib.Path) -> list[tuple[pathlib.Path, str, bool]]:
    """Every citation of a pack rule file from any skill, and whether the prose
    beside it names the checkout that holds it.

    Enumerated from disk on both axes: the documents come from walking
    `skills/`, and what counts as a rule path comes from walking
    `.claude/rules/`. Neither is a list anybody maintains.
    """

    rule_paths = pack_rule_paths(root)
    found: list[tuple[pathlib.Path, str, bool]] = []
    for doc in skill_documents(root):
        for paragraph in PARAGRAPH_BREAK.split(doc.read_text(encoding="utf-8")):
            # Whitespace collapsed: the qualifier routinely wraps away from the
            # path, and an indented continuation line is still one space.
            flat = " ".join(paragraph.split())
            for match in BACKTICKED_PATH.finditer(flat):
                cited = match.group(1)
                if cited in rule_paths:
                    found.append((doc, cited, citation_is_qualified(flat, match)))
    return found


def citation_is_qualified(flat: str, match: re.Match[str]) -> bool:
    """Whether the citation's sentence, or a next one about the file, names the pack.

    The citation itself is excluded on purpose: a cited path with `pack` as a
    segment would otherwise qualify itself, which is a citation vouching for
    its own resolution.
    """

    ends = sentence_ends(flat)
    start = max((end for end in ends if end <= match.start()), default=0)
    stop = min((end for end in ends if end > match.end()), default=len(flat))
    if QUALIFIER.search(flat[start:match.start()]) or QUALIFIER.search(flat[match.end():stop]):
        return True
    following = flat[stop:min((end for end in ends if end > stop), default=len(flat))]
    return bool(BACK_REFERENCE.match(following) and QUALIFIER.search(following))


def unqualified_rule_paths(root: pathlib.Path) -> list[str]:
    """The failures, as `<document>: <path>` lines."""

    return [
        f"{doc.relative_to(root)}: `{cited}` lives only in this pack, cited to a reader"
        " standing in some other checkout without naming the pack"
        for doc, cited, qualified in rule_path_citations(root)
        if not qualified
    ]


class ForeignCheckoutCitationTests(unittest.TestCase):
    def test_no_rule_path_is_cited_as_if_the_reader_s_checkout_had_it(self) -> None:
        problems = unqualified_rule_paths(REPO_ROOT)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_scan_reaches_the_skills(self) -> None:
        """The control, and it is not a formality.

        The first draft's regex rejected a leading dot, so it matched none of
        the `.claude/...` paths this check exists for and the test above passed
        on the unfixed tree. Asserting that the skills were globbed is not
        enough; both ends of the walk have to have produced something and a
        citation has to have been classified.
        """

        self.assertNotEqual(skill_documents(REPO_ROOT), [], "no skill document was walked")
        self.assertNotEqual(pack_rule_paths(REPO_ROOT), set(), "no rule file was walked")
        self.assertNotEqual(rule_path_citations(REPO_ROOT), [], "no rule citation was classified")

    def test_the_walk_covers_every_skill_and_not_a_named_one(self) -> None:
        """The generalisation, asserted rather than assumed.

        The predecessor hardcoded `skills/sd-research-repo` and so watched one
        skill while four carried the citation. Three things are checked. Every
        directory holding a `SKILL.md` is reached by the walk -- computed from
        disk on both sides, so adding a skill cannot quietly fall outside it.
        Every document is reached, counted by `os.walk` rather than by the
        walk under test (sd:999, review-836): a walk that collapsed to
        `SKILL.md` alone still reaches every skill directory, and misses the
        `references/` and `templates/` pages that cite the rule too. And the
        citations actually classified come from more than one skill, which a
        walk that had silently collapsed back to a single directory could not
        satisfy.
        """

        authored = {
            skill.parent for skill in (REPO_ROOT / SKILLS_DIR).rglob("SKILL.md")
        }
        self.assertGreater(len(authored), 1, "the skills tree did not enumerate")
        walked = {doc.parent for doc in skill_documents(REPO_ROOT)}
        self.assertEqual(authored - walked, set(), "a skill directory was not walked")

        expected = {
            pathlib.Path(top) / name
            for top, _, names in os.walk(REPO_ROOT / SKILLS_DIR)
            for name in names
            if name.endswith(".md") and not os.path.islink(os.path.join(top, name))
        }
        self.assertTrue(any(doc.name != "SKILL.md" for doc in expected),
                        "no nested skill document to check the walk against")
        self.assertEqual(expected - set(skill_documents(REPO_ROOT)), set(),
                         "a skill document was not walked")

        cited_by = {doc.relative_to(REPO_ROOT).parts[1] for doc, _, _ in rule_path_citations(REPO_ROOT)}
        self.assertGreater(len(cited_by), 1, f"only one skill was scanned: {sorted(cited_by)}")

    def fixture(self) -> pathlib.Path:
        """A checkout with the cap file and an empty `skills/` tree."""

        import tempfile

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        (root / SKILLS_DIR).mkdir()
        (root / ".claude" / "rules").mkdir(parents=True)
        (root / ".claude" / "rules" / "caps.md").write_text("| Cap |\n", encoding="utf-8")
        return root

    def test_an_unqualified_pack_path_is_caught_and_a_qualified_one_is_not(self) -> None:
        """The guard against the guard: the defect this class exists for.

        Both halves matter. Without the first the check can never fail; without
        the second it fails on every correctly-written citation and gets
        deleted the next time someone needs the build green.
        """

        root = self.fixture()
        skill = root / SKILLS_DIR / "sd-example"
        (skill / "references").mkdir(parents=True)

        document = skill / "SKILL.md"
        document.write_text("its cap is in\n`.claude/rules/caps.md`.\n", encoding="utf-8")
        self.assertEqual(len(unqualified_rule_paths(root)), 1)

        document.write_text(
            "its cap is in the pack's\n`.claude/rules/caps.md`.\n", encoding="utf-8")
        self.assertEqual(unqualified_rule_paths(root), [])

    def test_any_skill_is_watched_and_the_failure_names_the_one_at_fault(self) -> None:
        """The generalisation, at fixture scale.

        Three skills, none of them the one the predecessor hardcoded, and only
        the middle one unqualified. A guard scoped to a named skill reports
        nothing here; this one reports exactly the offender, by name. The two
        well-written neighbours are the other direction -- a guard that fires
        on correct prose is worse than the bug, because it gets deleted.
        """

        root = self.fixture()
        for name, prose in (
            ("sd-alpha", "its cap is in the sd-workflow checkout's"),
            ("sd-beta", "its cap is in"),
            ("sd-gamma", "read the caps in the pack's"),
        ):
            skill = root / SKILLS_DIR / name
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                f"{prose}\n`.claude/rules/caps.md`.\n", encoding="utf-8")

        problems = unqualified_rule_paths(root)
        self.assertEqual(len(problems), 1, "\n".join(problems))
        self.assertIn(f"{SKILLS_DIR}/sd-beta/SKILL.md", problems[0])

    def test_a_citation_in_a_nested_skill_document_is_caught(self) -> None:
        """sd:999 (review-836). `references/` is a skill document too.

        `skills/sd-research-repo/references/conventions.md` cites the rule
        file, so a walk of `SKILL.md` files alone would pass it unread.
        """

        root = self.fixture()
        nested = root / SKILLS_DIR / "sd-example" / "references"
        nested.mkdir(parents=True)
        (nested.parent / "SKILL.md").write_text("# sd-example\n", encoding="utf-8")
        (nested / "conventions.md").write_text(
            "its cap is in\n`.claude/rules/caps.md`.\n", encoding="utf-8")

        problems = unqualified_rule_paths(root)
        self.assertEqual(len(problems), 1, "\n".join(problems))
        self.assertIn(f"{SKILLS_DIR}/sd-example/references/conventions.md", problems[0])

    def test_the_qualifier_counts_on_either_side_of_the_citation(self) -> None:
        """The word order is prose, not the invariant.

        "the pack's `<path>`" and "`<path>` ... that file lives only in the
        sd-workflow checkout" say the same thing, and both are live in
        `skills/` today. A guard that reads only backwards passes the first
        and fails the second, which makes the guard a style rule. The third
        case is the one that must still fail: a mention in a later sentence
        about something else does not qualify anything.
        """

        root = self.fixture()
        skill = root / SKILLS_DIR / "sd-example"
        skill.mkdir()
        document = skill / "SKILL.md"

        document.write_text(
            "the cap is on that row in\n`.claude/rules/caps.md`.\nThat file lives only in"
            " the sd-workflow checkout.\n", encoding="utf-8")
        self.assertEqual(unqualified_rule_paths(root), [])

        document.write_text(
            "the cap is on that row in\n`.claude/rules/caps.md`.\n"
            f"{'Read it before promoting the item. ' * 6}It ships with the pack.\n",
            encoding="utf-8")
        self.assertEqual(len(unqualified_rule_paths(root)), 1)

    def test_a_pack_mention_in_another_sentence_does_not_qualify_a_citation(self) -> None:
        """sd:999 (review-836). The word has to be about this citation.

        A 100-character window let a neighbouring sentence vouch for the path:
        "`<path>`. See the pack's release notes." read as qualified, and so did
        a sentence before the citation and a paragraph after it. Each decoy
        here passed the window. The qualifier now has to be in the citation's
        own sentence, or in the next one when that sentence opens by pointing
        back at the file. The second half keeps the forms the skills use.
        """

        root = self.fixture()
        skill = root / SKILLS_DIR / "sd-example"
        skill.mkdir()
        document = skill / "SKILL.md"

        for decoy in (
            "the cap is in\n`.claude/rules/caps.md`. See the pack's release notes.\n",
            "The pack is large. The cap is in\n`.claude/rules/caps.md`.\n",
            "the cap is in\n`.claude/rules/caps.md`.\n\nThat file lives only in the pack.\n",
            # A sentence may open on markup or a non-ASCII capital (review round 3).
            "See the pack's notes. **Read** `.claude/rules/caps.md` for the cap.\n",
            "See the pack's notes. `.claude/rules/caps.md` holds the cap.\n",
            "See the pack's notes. Über-caps live in `.claude/rules/caps.md`.\n",
        ):
            with self.subTest(decoy=decoy):
                document.write_text(decoy, encoding="utf-8")
                self.assertEqual(len(unqualified_rule_paths(root)), 1)

        for real in (
            "the cap is in\n`.claude/rules/caps.md`. That file lives only in the"
            " sd-workflow checkout.\n",
            "the cap is in\n`.claude/rules/caps.md`.\nIt ships only with the pack.\n",
            "the cap is in the pack's\n`.claude/rules/caps.md`, which no other checkout has.\n",
            # An abbreviation's dot is not a sentence end (review round 1).
            "Read the sd-workflow checkout's rules (e.g.\n`.claude/rules/caps.md`).\n",
            "Read the rules (e.g. `.claude/rules/caps.md`, i.e. the caps) in the pack.\n",
            "From the pack, read the caps, notes, etc. in `.claude/rules/caps.md`.\n",
            # A wrap inside the back-reference is still one (review round 2).
            "the cap is in\n`.claude/rules/caps.md`. That\n  file lives only in the pack.\n",
            "the cap is in\n`.claude/rules/caps.md`. This rule\n   file ships with the pack.\n",
        ):
            with self.subTest(real=real):
                document.write_text(real, encoding="utf-8")
                self.assertEqual(unqualified_rule_paths(root), [])

    def test_a_word_containing_pack_does_not_qualify_a_citation(self) -> None:
        """The false negative from the other side, and the reason for `\\b`.

        The first version matched `pack` as a bare substring, so "the package
        documentation", "the packaging notes" and "unpack the brief" all read
        as naming this pack and let an unqualified citation through. Each of
        the three was confirmed to slip through before the boundary was added.
        A guard that cannot fail is worse than no guard: the class it watches
        then looks clean.

        The second half is the one that matters as much -- the boundary must
        still accept what the prose really says, or the guard fires on every
        correct citation and gets deleted the next time the build is red.
        """

        root = self.fixture()
        skill = root / SKILLS_DIR / "sd-example"
        skill.mkdir()
        document = skill / "SKILL.md"

        for decoy in (
            "the package documentation says the cap is in",
            "see the packaging notes; the cap is in",
            "unpack the brief first. The cap is in",
            "the cap is in",
        ):
            with self.subTest(lead=decoy):
                document.write_text(f"{decoy}\n`.claude/rules/caps.md`.\n", encoding="utf-8")
                self.assertEqual(len(unqualified_rule_paths(root)), 1)

        for real in (
            "the cap is in the sd-workflow checkout's",
            "the caps live in the pack's",
            "read it in the pack at",
        ):
            with self.subTest(lead=real):
                document.write_text(f"{real}\n`.claude/rules/caps.md`.\n", encoding="utf-8")
                self.assertEqual(unqualified_rule_paths(root), [])

    def test_paths_that_are_not_pack_rules_are_left_alone(self) -> None:
        """The scope, and why it is the rules directory rather than everything.

        Each of these is correct *because* it resolves against the reader's
        checkout, and rewriting it to name the pack would be the worse bug this
        test exists to prevent:

        * `references/x.md` ships beside the installed skill.
        * `.github/sd-review.json`, `.github/workflows/sd-review-route.yml`
          and `.github/sd-status.json` are per-repository configuration
          written into the reader's checkout by `bin/sd_setup_github.py`; the
          reader's copy is the one that governs. A draft of this class that
          flagged every path resolving in this checkout reported all of them.
          The fixture carries all three (sd:999, review-836): one left out is
          a scope regression nothing here would see.
        * a path this checkout does not have is naming another repository,
          which is what the prose beside it says.
        """

        root = self.fixture()
        skill = root / SKILLS_DIR / "sd-example"
        skill.mkdir()
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "sd-review.json").write_text("{}\n", encoding="utf-8")
        (root / ".github" / "workflows" / "route.yml").write_text("on: push\n", encoding="utf-8")
        (root / ".github" / "sd-status.json").write_text("{}\n", encoding="utf-8")
        (skill / "SKILL.md").write_text(
            "read `references/conventions.md`, the repository's `.github/sd-review.json`,"
            " its `.github/sd-status.json`, the route in `.github/workflows/route.yml`,"
            " and `local-adversarial-gate/core.md`\n",
            encoding="utf-8")
        self.assertEqual(unqualified_rule_paths(root), [])


class CitationRepointerTests(unittest.TestCase):
    """sd:592. The tool that makes a moved citation a read rather than arithmetic.

    Measured over the 200 commits before this one: 238 citation line numbers
    rewritten by hand across 45 commits. 147 named source files, which nothing
    covered; 91 named markdown, where `--update-citations` re-baselines the
    manifest and leaves the number in the prose for a person. The shortcut that
    invites is a blunt regex over the numbers in a page, and that shortcut has
    already moved two markers that were about other things.

    So the anchored text is what is looked for, never the citation string, and
    two candidates or none move nothing.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        (self.root / "bin").mkdir()
        self.source = self.root / "bin" / "tool.py"
        self.source.write_text("def render():\n    return 1\n", encoding="utf-8")
        # A page, which keeps `path:line`: the number-moving half of the tool.
        self.notes = self.root / "notes.md"
        self.notes.write_text("render\nmore\n", encoding="utf-8")
        self.doc = self.root / "page.md"

    def page(self, body: str) -> pathlib.Path:
        self.doc.write_text(body, encoding="utf-8")
        return self.doc

    def test_a_line_into_code_becomes_its_declaration_locator(self) -> None:
        """sd:525. Not moved to the new line, which the next insertion breaks."""
        self.page("The renderer is `render` (`bin/tool.py:1`).\n")
        self.source.write_text(
            "# inserted\n" * 9 + self.source.read_text(encoding="utf-8"), encoding="utf-8")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(refusals, [])
        self.assertEqual([(move.citation, move.now) for move in moves],
                         [("`bin/tool.py:1`", "source:bin/tool.py::render`")])
        self.assertIn("`render` (`source:bin/tool.py::render`)", text)
        self.assertIsNone(source_declaration_error(self.root, "bin/tool.py", "render"))

    def test_a_line_into_code_that_is_still_right_is_converted_too(self) -> None:
        """The number being right today is not the claim sd:525 accepts."""
        self.page("The renderer is `render` (`bin/tool.py:1-2`).\n")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(refusals, [])
        self.assertEqual([move.now for move in moves], ["source:bin/tool.py::render`"])
        self.assertIn("`source:bin/tool.py::render`", text)

    def test_a_range_into_a_page_keeps_its_width_when_it_moves(self) -> None:
        self.page("The renderer is `render` (`notes.md:1-2`).\n")
        self.notes.write_text(
            "# inserted\n" * 9 + self.notes.read_text(encoding="utf-8"), encoding="utf-8")
        text, _, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(refusals, [])
        self.assertIn("`notes.md:10-11`", text)

    def test_green_a_citation_into_a_page_that_is_still_right_is_not_touched(self) -> None:
        """CONTROL. A repointer that rewrites a correct citation is a churn engine."""
        original = self.page("The renderer is `render` (`notes.md:1`).\n").read_text(
            encoding="utf-8")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual((moves, refusals), ([], []))
        self.assertEqual(text, original)

    def test_an_anchor_written_with_arguments_finds_its_declaration(self) -> None:
        """`SYMBOL` accepts a call-shaped anchor, so the callee must be taken.

        `rstrip("()")` removed only the trailing parenthesis, leaving
        `render("x"`. That is not an identifier, so the AST lookup never saw
        `render`: a declaration that exists exactly once was reported gone.
        """
        self.page('The renderer is `render("x")` (`bin/tool.py:1`).\n')
        self.source.write_text(
            "# inserted\n" * 9 + self.source.read_text(encoding="utf-8"), encoding="utf-8")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(refusals, [])
        self.assertEqual([move.now for move in moves], ["source:bin/tool.py::render`"])
        self.assertIn("`source:bin/tool.py::render`", text)

    def test_a_removed_anchor_is_not_repointed_onto_a_longer_name(self) -> None:
        """The fallback matches an identifier, never a substring.

        With `needle in line`, a line carrying only `renderer` was a candidate
        for a removed `render`; as the sole match it was taken, so the tool
        rewrote a citation to unrelated text instead of refusing. In a tool
        that writes pages, that is the worst available outcome.
        """
        self.notes.write_text("# pad\n" * 5 + "renderer = 1\n", encoding="utf-8")
        self.page("The renderer is `render` (`notes.md:1`).\n")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(moves, [])
        self.assertIn("is gone from", refusals[0].reason)
        self.assertIn("`notes.md:1`", text)

    def test_green_a_longer_name_still_moves_when_it_is_the_anchor(self) -> None:
        """CONTROL. The boundary must not stop a real match from being found."""
        self.notes.write_text("# pad\n" * 5 + "renderer\n", encoding="utf-8")
        self.page("The renderer is `renderer` (`notes.md:1`).\n")
        _, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(refusals, [])
        self.assertEqual([move.now for move in moves], ["6`"])

    def test_an_ambiguous_anchor_refuses_rather_than_guessing(self) -> None:
        """Two candidates, so it moves nothing and says which citation it left."""
        self.notes.write_text("helper()\n" + "# pad\n" * 10 + "helper()\n", encoding="utf-8")
        self.page("The helper is `helper` (`notes.md:6`).\n")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(moves, [])
        self.assertEqual([refusal.citation for refusal in refusals], ["`notes.md:6`"])
        self.assertIn("ambiguous", refusals[0].reason)
        self.assertIn("`notes.md:6`", text)

    def test_a_symbol_declared_twice_in_code_refuses_rather_than_guessing(self) -> None:
        self.source.write_text("def helper():\n    pass\n" * 2, encoding="utf-8")
        self.page("The helper is `helper` (`bin/tool.py:3`).\n")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(moves, [])
        self.assertIn("declared 2 times in bin/tool.py", refusals[0].reason)
        self.assertIn("`bin/tool.py:3`", text)

    def test_a_vanished_anchor_refuses_rather_than_deleting(self) -> None:
        self.page("The renderer is `render` (`bin/tool.py:1`).\n")
        self.source.write_text("# nothing here\n" * 5, encoding="utf-8")
        _, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(moves, [])
        self.assertIn("declared 0 times in bin/tool.py", refusals[0].reason)

    def test_an_anchor_that_is_not_a_declaration_refuses_to_prose(self) -> None:
        """`None` and `.replace(...)` were the census's two; neither has a locator."""
        self.source.write_text("def render():\n    return None\n", encoding="utf-8")
        self.page("It returns `None` (`bin/tool.py:2`).\n")
        _, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(moves, [])
        self.assertIn("say it in prose", refusals[0].reason)

    def test_an_attribute_anchor_is_not_converted_onto_a_same_named_function(self) -> None:
        """`.replace(...)` is a method call; a top-level `replace` is something else."""
        self.source.write_text(
            "def replace():\n    pass\ntext = 'a'.replace('a', 'b')\n", encoding="utf-8")
        self.page('It flattens with `.replace("a", "b")` (`bin/tool.py:3`).\n')
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(moves, [])
        self.assertIn("say it in prose", refusals[0].reason)
        self.assertNotIn("source:bin/tool.py::replace", text)

    def test_a_bare_name_at_a_use_site_is_not_converted_onto_its_declaration(self) -> None:
        """A use is a claim about the call, not about the definition it resolves to.

        `get` cited where `d.get('x')` runs means `dict.get`; a `Store.get`
        declared once, further down, is something else. Counting declarations
        cannot tell the two apart, so the cited line is asked: the anchor is
        there and the declaration is not, and that refuses to prose.
        """
        self.source.write_text(
            "def lookup(d):\n"                     # 1
            "    return d.get('x')\n"              # 2
            "\n\n"                                 # 3-4
            "def run_all():\n"                     # 5
            "    text = 'a'.replace('a', 'b')\n"   # 6
            "    return helper()\n"                # 7
            + "# pad\n" * 10 +                     # 8-17
            "class Store:\n"                       # 18
            "    def get(self):\n"                 # 19
            "        return 1\n"                   # 20
            "\n\n"                                 # 21-22
            "def replace():\n"                     # 23
            "    pass\n"                           # 24
            "\n\n"                                 # 25-26
            "def helper():\n"                      # 27
            "    pass\n",                          # 28
            encoding="utf-8")
        for anchor, line in (("get", 2), ("replace", 6), ("helper()", 7),
                             (".get", 2), ("Store.get", 19)):
            with self.subTest(anchor=anchor):
                self.page(f"It reads with `{anchor}` (`bin/tool.py:{line}`).\n")
                text, moves, refusals = repoint_document(self.doc, self.root)
                self.assertEqual(moves, [])
                self.assertIn("say it in prose", refusals[0].reason)
                self.assertIn(f"`bin/tool.py:{line}`", text)
        for anchor, line, name in (("get", 19, "get"), ("helper()", 27, "helper")):
            with self.subTest(control=anchor):
                self.page(f"It reads with `{anchor}` (`bin/tool.py:{line}`).\n")
                _, moves, refusals = repoint_document(self.doc, self.root)
                self.assertEqual(refusals, [])
                self.assertEqual([move.now for move in moves],
                                 [f"source:bin/tool.py::{name}`"])

    def test_the_use_test_matches_the_callee_on_an_identifier_boundary(self) -> None:
        """sd:765 R1 and R2. The substring test gave a wrong answer each way.

        R1: a stale `get` cited where the old window holds only `budget` is a
        stale citation to the declaration, and it refused as a use. R2: an
        anchor `helper(a, b)` cited at `helper(a,b)` is a use, and it converted,
        because the spelling of the arguments differs.
        """
        self.source.write_text(
            "def lookup():\n"                      # 1
            "    budget = geté() + éget()\n"       # 2
            "    return helper(a,b)\n"             # 3
            + "# pad\n" * 10 +                     # 4-13
            "def get():\n"                         # 14
            "    pass\n"                           # 15
            "\n\n"                                 # 16-17
            "def helper(a, b):\n"                  # 18
            "    pass\n",                          # 19
            encoding="utf-8")
        self.page("It reads with `get` (`bin/tool.py:1`).\n")
        _, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(refusals, [], "R1: `budget`, `geté` and `éget` are not uses of `get`")
        self.assertEqual([move.now for move in moves], ["source:bin/tool.py::get`"])
        self.page("It calls `helper(a, b)` (`bin/tool.py:3`).\n")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(moves, [], "R2: `helper(a,b)` is a use of `helper`")
        self.assertIn("is a use, not its declaration at 18", refusals[0].reason)
        self.assertIn("`bin/tool.py:3`", text)

    def test_the_use_test_reads_an_identifier_the_way_the_parser_does(self) -> None:
        """sd:822 NB1. `\\w` was not the identifier rule, and it erred both ways.

        Python normalises an identifier with NFKC before it looks it up, so a
        fullwidth `get()` (U+FF47 U+FF45 U+FF54) calls `get`: a use `\\w` never
        saw, so the tool converted it onto the declaration -- a wrong
        conversion, the class of defect this tool must not produce. U+0301 (a
        combining acute) and U+00B7 (a middle dot) continue an identifier where
        `\\w` stops, so `get` followed by either is another name and not a use
        of `get`; those refused. The premise is executed, not asserted: the
        fixture is run, and the fullwidth call reaches `get` while the other
        two names reach nothing. Each direction is its own subTest, so a fix
        for one cannot pass for the other.
        """
        source = (
            "def lookup():\n"                                   # 1
            "    return \uff47\uff45\uff54()\n"                 # 2
            "\n\n\n\n"                                          # 3-6
            "def other():\n"                                    # 7
            "    return get\u0301() + get\u00b7()\n"            # 8
            + "# pad\n" * 10 +                                  # 9-18
            "def get():\n"                                      # 19
            "    return 42\n")                                  # 20
        namespace: dict[str, typing.Any] = {}
        exec(source, namespace)
        self.assertEqual(namespace["lookup"](), 42, "the premise: fullwidth get() is get()")
        with self.assertRaises(NameError):
            namespace["other"]()
        self.source.write_text(source, encoding="utf-8")
        with self.subTest("a fullwidth call is a use"):
            self.page("It reads with `get` (`bin/tool.py:2`).\n")
            text, moves, refusals = repoint_document(self.doc, self.root)
            self.assertEqual(moves, [], "NFKC: fullwidth get() calls get, so line 2 is a use")
            self.assertIn("is a use, not its declaration at 19", refusals[0].reason)
            self.assertIn("`bin/tool.py:2`", text)
        with self.subTest("a name that continues past `get` is not a use"):
            self.page("It reads with `get` (`bin/tool.py:8`).\n")
            _, moves, refusals = repoint_document(self.doc, self.root)
            self.assertEqual(refusals, [], "U+0301 and U+00B7 continue an identifier")
            self.assertEqual([move.now for move in moves], ["source:bin/tool.py::get`"])

    def test_the_use_test_reads_the_first_and_last_character_of_its_window(self) -> None:
        """sd:846 NB1. The two edges, where the neighbour the test asks for is empty.

        `calls_by_name` passes `""` for the character before an occurrence at
        offset 0 of the window, and again for the character after one that
        ends at its last. `("_" + "").isidentifier()` is True, so without the
        `bool(char)` guard both read as a continuation, the use is not seen,
        and the citation converts onto a declaration it is not about -- the
        wrong-conversion class. The guard was load-bearing on both edges and
        the whole module passed without it, so each edge is its own subTest
        and a fix for one cannot pass for the other.

        The window is `lines[start - 1 - WINDOW:end + WINDOW]`, so a citation
        to line 1 starts it at the first line of the file and a citation
        `WINDOW` lines above the last ends it at the last character of the
        last -- with no trailing newline, which `file_lines` would otherwise
        drop into an empty final line and hide the edge.
        """
        leading = (
            "get()\n"                          # 1: the window opens on this character
            + "# pad\n" * 12 +                 # 2-13
            "def get():\n"                     # 14
            "    pass\n")                      # 15
        trailing = (
            "def get():\n"                     # 1
            "    pass\n"                       # 2
            + "# pad\n" * 10 +                 # 3-12
            "x = get")                         # 13: no newline; the window's last char
        for name, source, line, declared in (
                ("at column 0 of the window's first line", leading, 1, 14),
                ("at the last character of its last line", trailing, 13 - WINDOW, 1)):
            with self.subTest(name):
                self.source.write_text(source, encoding="utf-8")
                self.page(f"It reads with `get` (`bin/tool.py:{line}`).\n")
                text, moves, refusals = repoint_document(self.doc, self.root)
                self.assertEqual(moves, [], "the use at the window's edge was not seen")
                self.assertIn(f"is a use, not its declaration at {declared}",
                              refusals[0].reason)
                self.assertIn(f"`bin/tool.py:{line}`", text)

    def test_a_separator_above_a_cited_use_does_not_slide_the_use_test(self) -> None:
        """sd:822 NB2 R2. The use test counts lines the way the parser does.

        sd:794 moved it onto `numbered_lines` and nothing held it there: no
        fixture put a form feed or a U+2028 above a cited use. Under
        `splitlines()` the separators on line 1 are WINDOW + 1 lines of their
        own, the window for line 3 slides up past the use, and the use converts
        -- a wrong conversion. The declaration is where `ast` puts it either
        way, which is what makes the two counts disagree.
        """
        for name, first in (("U+2028 in a string", "X = '" + "\u2028" * (WINDOW + 1) + "'"),
                            ("form feeds in a comment", "# " + "\x0c" * (WINDOW + 1))):
            with self.subTest(name):
                self.source.write_bytes(
                    (first + "\n"                          # 1
                     "def lookup():\n"                     # 2
                     "    return get()\n"                  # 3
                     + "# pad\n" * 10 +                    # 4-13
                     "def get():\n"                        # 14
                     "    pass\n").encode("utf-8"))        # 15
                self.page("It reads with `get` (`bin/tool.py:3`).\n")
                text, moves, refusals = repoint_document(self.doc, self.root)
                self.assertEqual(moves, [], "the window slid above the use")
                self.assertIn("is a use, not its declaration at 14", refusals[0].reason)
                self.assertIn("`bin/tool.py:3`", text)

    def test_a_separator_in_a_page_does_not_slide_the_staleness_window(self) -> None:
        """sd:822 NB3. One file, one line count: `names_its_symbol` reads it too.

        A page carrying WINDOW + 1 invisible separators on its first line is
        numbered by every editor, by git and by this module as the file it
        looks like. `splitlines()` numbers it WINDOW + 1 lines longer, the
        window for a correct citation slides above the anchor, and the gate
        calls the citation stale -- so the repointer rewrites a citation that
        was right, which is the failure mode it exists to avoid. The anchor is
        at exactly one line, so nothing ambiguity could hide the move.
        """
        self.notes.write_bytes(
            ("head" + "\u2028" * (WINDOW + 1) + "tail\n"       # 1
             + "pad\n" * 8 +                                   # 2-9
             "the render call is here\n"                       # 10
             "pad\n").encode("utf-8"))                         # 11
        self.page("The renderer is `render` (`notes.md:10`).\n")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(moves, [], "the citation is right; the window slid off the anchor")
        self.assertEqual(refusals, [])
        self.assertIn("`notes.md:10`", text)

    def test_a_separator_does_not_shift_the_line_a_moved_citation_is_sent_to(self) -> None:
        """sd:846 NB2. `anchor_lines`' fallback numbers candidates the same way.

        The staleness gate above and this search are two readers of one file,
        and only the first was pinned: reverting the fallback to
        `splitlines()` changed no fixture. With a U+2028 on line 1 the anchor
        really is on line 10 and `splitlines()` calls it 11, so the repointer
        writes a number the page did not have -- it repairs a stale citation
        into a wrong one, which is worse than leaving it stale. The anchor
        appears once, so nothing ambiguity could hide the move.
        """
        self.notes.write_bytes(
            ("head\u2028tail\n"                                # 1
             + "pad\n" * 8 +                                   # 2-9
             "the render call is here\n"                       # 10
             "pad\n").encode("utf-8"))                         # 11
        self.page("The renderer is `render` (`notes.md:1`).\n")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(refusals, [])
        self.assertEqual([move.now for move in moves], ["10`"])
        self.assertIn("`notes.md:10`", text)

    def test_the_use_test_window_is_WINDOW_lines_each_way(self) -> None:
        """sd:822 NB2 R1. Nothing pinned the width of the use-test window.

        A use WINDOW lines under the cited line, or above it, is inside the
        window and refuses; one line further either way is outside it, and the
        citation is a stale one to the declaration, which converts. A window
        of the cited lines alone passed every test before this one, and under
        it the citation two lines above a use converted -- the use test
        answering a question it had not been asked.
        """
        self.source.write_text(
            "# pad\n" * 6                                       # 1-6
            + "x = get()\n"                                     # 7
            + "# pad\n" * 10 +                                  # 8-17
            "def get():\n"                                      # 18
            "    pass\n",                                       # 19
            encoding="utf-8")
        for line, expected in ((7 - WINDOW, "use"), (7 - WINDOW - 1, "converts"),
                               (7 + WINDOW, "use"), (7 + WINDOW + 1, "converts")):
            with self.subTest(line=line, expected=expected):
                self.page(f"It reads with `get` (`bin/tool.py:{line}`).\n")
                _, moves, refusals = repoint_document(self.doc, self.root)
                if expected == "use":
                    self.assertEqual(moves, [])
                    self.assertIn("is a use, not its declaration at 18", refusals[0].reason)
                else:
                    self.assertEqual(refusals, [])
                    self.assertEqual([move.now for move in moves],
                                     ["source:bin/tool.py::get`"])

    def test_a_declaration_at_either_edge_of_the_window_converts(self) -> None:
        """The window's three bounds, which no test pinned (mutations Md, Me, R8).

        The declaration sits WINDOW lines under the cited line, inside a
        cited range but more than WINDOW lines under its start, and WINDOW
        lines above the cited line (sd:822 R8). Each is a citation to the
        declaration. The use on line 2 is inside the range's first lines, so
        ignoring the range end refuses it as a use whether the mutation
        narrows the bound alone or the window as well; dropping the lower
        bound refuses the third as a use of the name its own window declares.
        """
        self.source.write_text(
            "# a\nx = render\n" + "# a\n" * 5 + "def render():\n    pass\n" + "# a\n" * 3,
            encoding="utf-8")
        for citation in (f"bin/tool.py:{8 - WINDOW}", "bin/tool.py:1-6",
                         f"bin/tool.py:{8 + WINDOW}"):
            with self.subTest(citation=citation):
                self.page(f"The renderer is `render` (`{citation}`).\n")
                _, moves, refusals = repoint_document(self.doc, self.root)
                self.assertEqual(refusals, [])
                self.assertEqual([move.now for move in moves], ["source:bin/tool.py::render`"])

    def test_a_failed_source_reason_names_its_own_cause(self) -> None:
        """sd:794 N4. Five causes printed the one message meant for the sixth.

        A missing file, a file that is not Python, a markdown page, the page
        itself and a path out of the checkout all said the example "is not
        inside one declaration". Then (sd:822 NB4) a directory and a symlink
        loop said "target is missing", and a markdown page that happens to
        parse as Python -- `notes.md` here is two bare names -- said "found 0",
        which reads as a renamed symbol. The page itself is also named through
        `..` (sd:822 R6): a check that compared the unresolved paths sent that
        spelling on to be parsed as Python. Nothing is rewritten in any case.

        A file with a NUL byte is refused as unreadable, not raised out of the
        run (sd:999, review-945). Python before 3.12 raised `ValueError` there,
        which the two sibling readers of the same file already catch.
        """
        (self.root / "bin" / "run").write_text("#!/bin/sh\necho {\n", encoding="utf-8")
        (self.root / "bin" / "nul.py").write_bytes(b"def render():\n    pass\n\0\n")
        (self.root / "docs.md").write_text("# notes\n\n- a (b\n", encoding="utf-8")
        os.symlink("loop", self.root / "bin" / "loop")
        self.source.write_text("def render():\n    pass\n" * 2, encoding="utf-8")
        for path, symbol, cause in (
                ("bin/gone.py", "render", "bin/gone.py: target is missing"),
                ("bin/tool.py/x", "render", "bin/tool.py/x: target is missing"),
                ("bin", "render", "bin: target is a directory"),
                ("bin/loop", "render", "bin/loop: target is a symlink loop"),
                ("bin/run", "render", "bin/run: cannot read a Python source declaration"),
                ("bin/nul.py", "render", "bin/nul.py: cannot read a Python source declaration"),
                ("docs.md", "render", "docs.md: a markdown page declares no Python symbol"),
                ("notes.md", "render", "notes.md: a markdown page declares no Python symbol"),
                ("page.md", "render", "names the page it sits on"),
                ("bin/../page.md", "render", "names the page it sits on"),
                ("../../etc/passwd", "render", "names a file outside the checkout"),
                ("bin/tool.py", "render", "found 2"),
                ("bin/tool.py", "gone", "found 0")):
            with self.subTest(path=path, symbol=symbol):
                marker = f"[quoted: source:{path}::{symbol}]"
                self.page(f"`render` (`bin/tool.py:1`) {marker}\n")
                text, moves, refusals = repoint_document(self.doc, self.root)
                self.assertEqual(moves, [])
                self.assertEqual(len(refusals), 1, refusals)
                self.assertIn(cause, refusals[0].reason)
                self.assertNotIn("is not inside one declaration", refusals[0].reason)
                self.assertIn(marker, text)
        self.source.write_text("def render():\n    pass\n", encoding="utf-8")
        self.page("`render` (`bin/tool.py:1`) [quoted: source:bin/tool.py::render]\n")
        _, _, refusals = repoint_document(self.doc, self.root)
        self.assertIn("is not inside one declaration of `render`", refusals[0].reason,
                      "CONTROL: one declaration that does not carry it keeps its message")

    def test_a_quoted_reason_moves_and_the_citation_it_covers_does_not(self) -> None:
        """sd:568's marker is a citation too, and its anchored text is the citation.

        The citation itself stays put even though its own symbol has moved:
        a quoted citation is somebody else's example, and rewriting it edits
        the example rather than repairing a claim.
        """
        (self.root / "other.md").write_text(
            "# other\n\npad\npad\nthe example writes `bin/tool.py:1` here\n", encoding="utf-8")
        self.page("`render` (`bin/tool.py:1`) [quoted: other.md:2]\n")
        self.source.write_text("# inserted\n" * 9 + "def render():\n", encoding="utf-8")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(refusals, [])
        self.assertEqual([move.now for move in moves], ["other.md:5"])
        self.assertIn("[quoted: other.md:5]", text)
        self.assertIn("`bin/tool.py:1`", text)

    def test_a_separator_in_a_quoted_source_does_not_shift_the_reason_it_writes(self) -> None:
        """sd:846 NB2. The reason is rewritten to the parser's line, not to one more.

        `quoted_repoint` reads its source through `file_lines` like everything
        else since sd:822, and a revert to `splitlines()` here changed no
        fixture either. With a U+2028 on line 1 the example really is on line
        5 and `splitlines()` calls it 6, so the tool would repair a stale
        marker into one that names a line not carrying the example -- which
        `quotes` then reads as `quoted-not-there`, turning a marker the tool
        just "fixed" red.
        """
        (self.root / "other.md").write_bytes(
            ("head\u2028tail\n"                                      # 1
             + "pad\n" * 3 +                                         # 2-4
             "the example writes `bin/tool.py:1` here\n").encode("utf-8"))  # 5
        self.page("`render` (`bin/tool.py:1`) [quoted: other.md:2]\n")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(refusals, [])
        self.assertEqual([move.now for move in moves], ["other.md:5"])
        self.assertIn("[quoted: other.md:5]", text)

    def test_a_quoted_source_that_no_longer_carries_the_citation_refuses(self) -> None:
        (self.root / "other.md").write_text("# other\n\nnothing\n", encoding="utf-8")
        self.page("`render` (`bin/tool.py:1`) [quoted: other.md:2]\n")
        _, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(moves, [])
        self.assertIn("is gone from other.md", refusals[0].reason)

    def test_a_quoted_reason_into_code_has_no_number_to_move(self) -> None:
        """sd:765. Into code the number is a hint, and a `source:` reason has none.

        A `path:line` whose file still carries the example moves nothing and
        refuses nothing, however far the example moved. A `source:` reason
        whose declaration no longer carries it refuses and names the symbol,
        since there is no number a tool could write.
        """
        self.source.write_text(
            "# inserted\n" * 9 + 'def render():\n    """writes `bin/tool.py:1`"""\n',
            encoding="utf-8")
        self.page("`render` (`bin/tool.py:1`) [quoted: bin/tool.py:2]\n")
        self.assertEqual(repoint_document(self.doc, self.root)[1:], ([], []))
        self.page("`render` (`bin/tool.py:1`) [quoted: source:bin/tool.py::render]\n")
        self.assertEqual(repoint_document(self.doc, self.root)[1:], ([], []))
        self.source.write_text("def render():\n    pass\n", encoding="utf-8")
        text, moves, refusals = repoint_document(self.doc, self.root)
        self.assertEqual(moves, [])
        self.assertEqual(len(refusals), 1, refusals)
        self.assertIn("one declaration of `render` in bin/tool.py", refusals[0].reason)
        self.assertIn("[quoted: source:bin/tool.py::render]", text)

    def test_this_repository_has_nothing_to_repoint(self) -> None:
        """The two gates agreeing: a green corpus gives the repointer no work.

        It is also the regression test for the routing defect the live corpus
        found in this tool's first draft, which proposed rewriting a citation a
        page quotes -- the PR #868 mistake arriving through the tool built to
        prevent it. Any fall-through from the quoted shape to the anchored one
        turns this red.
        """
        documents = repointable()
        self.assertTrue(documents, "no document was offered to the repointer")
        proposed = []
        for doc in documents:
            _, moves, refusals = repoint_document(doc)
            proposed += [f"{doc}: {move.citation} -> {move.now}" for move in moves]
            proposed += [f"{doc}: REFUSED {r.citation}: {r.reason}" for r in refusals]
        self.assertEqual(proposed, [])

    def test_no_document_is_offered_to_the_repointer_twice(self) -> None:
        """`corpus()` and `ROOT_DOCUMENTS` overlap, and `CONTRIBUTING.md` is
        the overlap. Reached twice, `--apply` writes it twice and the dry run
        counts its moves twice.

        The control is that the overlap is real: an assertion about duplicates
        on a list that could not contain one proves nothing.
        """
        documents = repointable()
        self.assertEqual(len(documents), len(set(documents)))
        self.assertTrue(
            {REPO_ROOT / name for name in ROOT_DOCUMENTS} & set(corpus()),
            "ROOT_DOCUMENTS no longer overlaps the corpus, so this proves nothing")

    def test_the_corpus_this_tool_writes_to_excludes_the_archive(self) -> None:
        """An archived page is a record. `classify` refuses to fail one; this
        refuses to rewrite one, for the same reason rule 6 will not re-anchor a
        citation below a Log heading."""
        self.assertEqual([doc for doc in repointable() if "archive" in doc.parts], [])
        self.assertTrue(any("archive" in doc.parts for doc in corpus()),
                        "the corpus carries no archived page, so this proves nothing")

    def test_the_repoint_command_itself_runs_in_the_unittest_shard(self) -> None:
        """sd:525. `--repoint` was dispatched only under `__main__`.

        `.github/scripts/run-tests.sh` runs `python -m unittest <module>`, which
        never reaches that branch, so the command CONTRIBUTING.md tells a
        person to run had no CI coverage: its dispatch, its report and its exit
        status could all break with the suite green. This runs it as a person
        would, against this checkout, read-only.
        """
        import sys

        result = subprocess.run(
            [sys.executable, str(pathlib.Path(__file__).resolve()), "--repoint"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        report = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, report)
        self.assertIn("would repoint 0 citation(s); refused 0", result.stdout, report)


class InsertionIsHarmlessToASymbolTests(unittest.TestCase):
    """sd:525's acceptance: lines inserted above a cited symbol break nothing.

    Asked of the gate's own test methods under a fixture root, so what is
    proved is what CI runs: the stable locator still resolves after the
    insertion, and a bare `path:line` into code fails the red-bucket test --
    before the insertion as well as after, because a line that is right today
    is the same claim waiting for the next insertion.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.target = self.root / "bin" / "tool.py"
        self.target.parent.mkdir()
        self.target.write_text("def render():\n    return 1\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        self.stable = self.root / "docs" / "stable.md"
        self.stable.write_text("`render` (`source:bin/tool.py::render`)\n", encoding="utf-8")

    def insert_above_the_symbol(self) -> None:
        self.target.write_text(
            "# inserted\n" * 7 + self.target.read_text(encoding="utf-8"), encoding="utf-8")

    def gate(self) -> None:
        """The two live-corpus tests this change relies on, against the fixture."""
        with mock.patch.dict(globals(), {"REPO_ROOT": self.root}):
            DocCitationTests().test_the_red_buckets_are_empty()
            DocCitationTests().test_every_anchored_citation_names_its_symbol_at_the_cited_line()
            StableSourceCitationTests().test_every_explicit_source_locator_resolves_in_the_live_corpus()

    def test_the_symbol_form_survives_an_insertion(self) -> None:
        self.gate()
        self.insert_above_the_symbol()
        self.assertEqual(declaration_lines(self.root, "bin/tool.py", "render"), [8],
                         "the insertion did not move the symbol, so this proves nothing")
        self.assertIsNone(source_declaration_error(self.root, "bin/tool.py", "render"))
        self.gate()

    def test_a_bare_line_into_code_fails_the_gate_before_and_after(self) -> None:
        (self.root / "docs" / "line.md").write_text(
            "`render` (`bin/tool.py:1`)\n", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "anchored-line-into-code"):
            self.gate()
        self.insert_above_the_symbol()
        with self.assertRaisesRegex(AssertionError, "anchored-line-into-code"):
            self.gate()

    def test_the_bucket_is_code_only_and_live_only(self) -> None:
        """CONTROLS. A page keeps `path:line`; an archive keeps what it cited."""
        (self.root / "notes.md").write_text("render\n", encoding="utf-8")
        page = self.root / "docs" / "page.md"
        page.write_text("`render` (`notes.md:1`)\n", encoding="utf-8")
        archived = self.root / "docs" / "archive" / "old.md"
        archived.parent.mkdir()
        archived.write_text("`render` (`bin/tool.py:1`)\n", encoding="utf-8")
        with mock.patch.dict(globals(), {"REPO_ROOT": self.root}):
            self.assertEqual([row.reason for row in classify([page])], ["compared"])
            self.assertEqual([row.reason for row in classify([archived])], ["compared"])
        self.gate()


if __name__ == "__main__":
    import sys

    if "--repoint" in sys.argv:
        raise SystemExit(repoint_main(sys.argv[1:]))
    unittest.main()
