"""The pull-request body `sd-ship` publishes, read against the lines it owns (sd:1870).

`sd-ship` writes `Work:` into the body it publishes, and `Item:` and
`Delivers:` into the squash message. A supplied `Item:`, `Work:` or
`Delivers:` line is stripped and named in the result, whatever it says
(sd:2999): `sd-ship` writes the links itself.

The fixpoint is the point: `normalize` of a body `sd-ship` published returns
the body it published before appending `Work:`, so the live pull-request body
fed back as `--body-file` prepares again without a refusal. Before this, the
body `sd-ship` itself wrote was refused as input (#1236, #1238).

Only a line at column zero is read, because that is the only form git or
rule 5 reads; an indented line is prose about a trailer, and the body keeps
it. A body with no item owns nothing, so every column-zero owned line is
stripped there, `Closes:` included.

With an item, a body `Closes:` line is refused: the enqueue's `--item` list
is the one closing set (operator ruling 2026-10-13, sd:3704, replacing the
sd:1481 body line). `Refs:` is not owned: it names related items, which stay
open.

A column-zero owned line inside a fenced code block or an HTML comment is
refused too, rather than kept: the squash carries the body as written, and
`sd_lib.demoted_trailers` would refuse the line there, at merge. Indented,
it is prose like any other example.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import sys
import tempfile

import sd_lib
from sd_ship_remote import Refusal, completed_process

BIN = pathlib.Path(__file__).resolve().parent

#: The canonical spelling of each owned key, by its case-folded name.
_CANONICAL = {key.rstrip(":").lower(): key for key in sd_lib.OWNED_TRAILERS}
_OWNED_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>" + "|".join(re.escape(key.rstrip(":")) for key in sd_lib.OWNED_TRAILERS)
    + r")[ \t]*:(?P<value>.*)$",
    re.IGNORECASE,
)


@dataclasses.dataclass(frozen=True)
class OwnedLine:
    """One owned trailer line: its 1-based number, text, canonical key and value."""

    number: int
    text: str
    key: str
    value: str
    indented: bool
    quoted: bool


#: A fence that opens or closes a fenced code block: up to three spaces, then
#: three or more backticks or tildes (CommonMark).
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
#: A Markdown heading line: up to three spaces, one to six `#`, then a space or the line's end.
_ATX = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
#: A thematic break: three or more `-`, `*` or `_`, spaces between allowed; it outranks a list item.
_BREAK = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
#: A setext underline, `=` or `-` alone, under a paragraph line: it makes that line a heading.
_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
#: A list item's marker and the spaces after it; their width is where the item's text starts.
_ITEM_MARKER = re.compile(r"^( {0,3}(?:[-*+]|[0-9]{1,9}[.)]))( +|$)")


def quoted_lines(body: str, comments: bool = True, code: bool = False) -> set[int]:
    """The 1-based numbers of the lines of `body` a fenced code block or an HTML comment holds.

    A fence closes on a line of the same character, at least as long, and
    nothing else; an unclosed fence runs to the end of the list item that
    holds it, or of the body. A fence opens at most three spaces past the
    text column of its list item, or on the item's marker line; four
    spaces at the top level start an indented code block. A comment holds a line
    when it is open where the line starts. Inside a fence a comment is text.
    With `comments` false only fences count, and a comment is text too.
    An indented code block, four spaces past its list column after a blank
    line, opens no fence, comment or list item; `code` counts its lines too.
    """
    return {number for number, kind, *_ in _blocks(body, comments) if kind in ("fence", "comment")
            or (code and kind == "code")}


def _blocks(body: str, comments: bool = True, plain: bool = False):
    """Each line of `body` with the Markdown block it sits in, read once for every reader (pack sd:9666).

    Two grammars, and each read is whole in one of them: it finds its own
    fences, comments, code spans and list items, and takes no verdict
    about a line from the other read, where the same line can be another
    thing. By default a paragraph is CommonMark's, the read citations
    get: a line indented less than its list item continues the item's
    paragraph, and a marker that cannot interrupt a paragraph is text.
    With `plain`, the grammar `_listed_undone` reads a revert's list by: a
    line that ends with a colon is a heading and ends the items it is
    indented less than, and every list marker opens an item. Inside an
    open code span neither `plain` rule applies, so a span's line ends
    and opens no item.

    Yields `(number, kind, opened, depth, text, line, spanned)`: `kind` is `fence`,
    `comment`, `code`, `setext` (an underline that makes the line above
    it a heading), `wrapped` (text indented less than the list item whose
    paragraph it continues) or `text`; `opened` counts the list items the line's
    markers open, even when a fence or code follows them; `depth` counts
    the items open after them; `text` is the line past their text column;
    `spanned` says the line starts inside a code span.

    A code span may wrap onto the next lines of its paragraph. An opening
    backtick run that no line of the paragraph closes is text, so the read
    runs again with that run taken as text, until every span closes.
    """
    literal: set[tuple[int, int]] = set()
    while True:
        rows, unclosed = _blocks_once(body, comments, literal, plain)
        if unclosed is None:
            return rows
        literal.add(unclosed)  # ponytail: a reread per unclosed run; a commit message has few


def _blocks_once(body: str, comments: bool, literal: set[tuple[int, int]], plain: bool):
    """`_blocks` once, each backtick run in `literal` taken as text; with the opener of a span left unclosed."""
    fence, fence_depth, commented, comment_depth, loose = "", 0, False, 0, True  # loose: no paragraph runs on
    depth, ticks, span_at, rows = 0, "", (0, 0), []
    items: list[int] = []  # the text column of each open list item, outermost first
    for number, line in enumerate(body.split("\n"), start=1):
        line = line.rstrip("\r").expandtabs(4)
        spanned = bool(ticks)  # in an open code span the `plain` rules rest
        rest, lazy = _continues(line, items, not loose and not commented, plain, spanned)
        commented = commented and len(items) >= comment_depth  # a comment ends with the list item that holds it
        if fence and len(items) >= fence_depth:
            fence = "" if _closes(_FENCE_RE.match(rest), fence) else fence
            loose = True  # a fence is no paragraph: indented code may follow its closing line
            rows.append((number, "fence", 0, len(items), rest, line, False))
            continue
        fence = ""  # the list item that held the fence ended, and the fence with it
        kind, opened, text = ("text", 0, rest) if lazy else _kind(rest, items, loose, commented, len(items) != depth,
                                                                    plain, spanned)
        alone = _stands_alone(kind, text, line)
        if ticks and _ends_paragraph(kind, opened, alone, len(items) != depth):
            return rows, span_at  # the paragraph ended with the span open: its opener is text
        if kind == "fence" and (opener := _FENCE_RE.match(text)):  # `_kind` matched it already
            fence, fence_depth = opener["fence"], len(items)
        elif kind != "code" and comments:
            was = commented
            commented, ticks, span_at = _inline(line, number, commented, ticks, span_at, literal)
            comment_depth = len(items) if commented and not was else comment_depth
            if ticks and alone:
                return rows, span_at  # a heading is one line: a span it opens ends with it
        loose = alone
        depth = len(items)  # a heading or a break is one line, no paragraph; `depth` finds a lazy line
        rows.append((number, "wrapped" if lazy else kind, opened, len(items), text, line, spanned))
    return rows, (span_at if ticks else None)


def _inline(line: str, number: int, commented: bool, ticks: str, span_at: tuple[int, int],
            literal: set[tuple[int, int]]) -> tuple[bool, str, tuple[int, int]]:
    """`_comment_open` on line `number`, with the opener of the code span left open after it."""
    commented, ticks, column = _comment_open(line, commented, ticks, {c for n, c in literal if n == number})
    return commented, ticks, (number, column) if ticks and column >= 0 else span_at


def _continues(line: str, items: list[int], paragraph: bool, plain: bool, spanned: bool) -> tuple[str, bool]:
    """`line` past its list column, popping each item it ends, and whether it is a lazy line.

    A line indented less than its list item continues the item's open
    `paragraph` (CommonMark's lazy continuation) unless a block starts on
    it: any list marker, or a block that interrupts a paragraph. In the
    `plain` grammar a line that ends with a colon is a heading, so it
    ends the item too, unless it starts in an open code span.
    """
    ended = items.copy()
    rest = _item_text(line, ended, opens=False)
    if paragraph and len(ended) < len(items) and not (_ITEM_MARKER.match(rest) or _interrupts(rest)) \
            and not (plain and not spanned and _heading(rest).endswith(":")):
        return line.strip(), True
    items[:] = ended
    return rest, False


#: A tag that starts an HTML block able to interrupt a paragraph (CommonMark kinds 1 to 6).
_HTML_START = re.compile(
    r"^ {0,3}<(?:!--|\?|![A-Za-z]|!\[CDATA\[|(?:script|pre|style|textarea)(?:[ \t>]|$)|/?(?:address|article|aside|"
    r"base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|"
    r"figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|"
    r"menuitem|nav|noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|"
    r"title|tr|track|ul)(?:[ \t>]|/>|$))", re.IGNORECASE)


def _interrupts(text: str) -> bool:
    """Whether `text` starts a block that ends an open paragraph: a heading, fence, break, quote, HTML
    block, or a list item with text that is a bullet or numbered 1."""
    fence = _FENCE_RE.match(text)
    if _ATX.match(text) or _BREAK.match(text) or _HTML_START.match(text) or text.lstrip(" ")[:1] == ">" \
            or (fence and not (fence["fence"][0] == "`" and "`" in fence["info"])):
        return True
    marker = _ITEM_MARKER.match(text)
    if marker is None:
        return False
    number = marker[1].strip()[:-1]
    return bool(text[marker.end():].strip()) and (not number or int(number) == 1)


def _ends_paragraph(kind: str, opened: int, alone: bool, moved: bool) -> bool:
    """Whether a line ends the paragraph above it: no paragraph text, a list item, or a list item's end."""
    return alone or kind != "text" or bool(opened) or moved


def _stands_alone(kind: str, text: str, line: str) -> bool:
    """Whether the line is no paragraph text: a fence, code, an underline, a blank, a heading or a break."""
    return kind in ("fence", "code", "setext") or not line.strip() or bool(_ATX.match(text) or _BREAK.match(text))


def _kind(rest: str, items: list[int], loose: bool, commented: bool, lazy: bool = False,
          plain: bool = False, spanned: bool = False) -> tuple[str, int, str]:
    """The block `rest`, a line past its list column, starts, the list items it opens onto `items`, and its text.

    An HTML comment open at its start holds it. Four spaces in after no
    paragraph, or after the markers it opens, start indented code. A
    thematic break or a setext underline outranks a list marker; a line
    that ended a list item (`lazy`) underlines nothing. A marker that
    cannot interrupt a paragraph is text; in the `plain` grammar only
    one that starts in an open code span is.
    """
    if commented:
        return "comment", 0, rest
    if loose and rest.startswith("    ") and rest.strip():
        return "code", 0, rest
    runs = not loose and not lazy  # an open paragraph runs on to this line
    if runs and _SETEXT.match(rest):
        return "setext", 0, rest
    if _BREAK.match(rest) or (runs and (spanned or not plain) and _ITEM_MARKER.match(rest)
                              and not _interrupts(rest)):
        return "text", 0, rest  # an empty item or one numbered past 1 cannot interrupt a paragraph
    before = len(items)
    text = _item_text(rest, items, opens=True)
    opened = len(items) - before
    if opened and text.startswith("    ") and text.strip():
        return "code", opened, text
    match = _FENCE_RE.match(text)
    if match and not (match["fence"][0] == "`" and "`" in match["info"]):
        return "fence", opened, text
    return "text", opened, text


def _closes(match, fence: str) -> bool:
    """Whether `match` (of `_FENCE_RE`) closes `fence`: the same character, as long or longer, nothing after."""
    return bool(match) and match["fence"][0] == fence[0] and len(match["fence"]) >= len(fence) \
        and not match["info"].strip()


def _item_text(line: str, items: list[int], opens: bool) -> str:
    """`line` past the text column of the list items it sits in, popping from `items` each it ends.

    With `opens`, `line` is already that text: each list marker it opens
    with pushes its item's text column, and the text after them is returned.
    """
    if opens:
        while marker := _ITEM_MARKER.match(line):
            width = len(marker[1]) + (len(marker[2]) if 1 <= len(marker[2]) <= 4 else 1)
            items.append((items[-1] if items else 0) + width)
            line = line[width:]
        return line
    if line.strip():
        while items and len(line) - len(line.lstrip(" ")) < items[-1]:
            items.pop()  # a line indented less than an item's text ends the item
    return line[items[-1]:] if items else line


#: A backslash escape of a backslash, backtick or `<`; the next `<!--`; or a backtick run.
_INLINE = re.compile(r"\\[\\`<]|<!--|`+")


def _comment_open(line: str, commented: bool, ticks: str = "", literal=frozenset()) -> tuple[bool, str, int]:
    """After `line`: whether an HTML comment is open, the backtick run of a code span still open, and its column.

    `commented` and `ticks` say what was open before `line`. Outside a
    comment a code span outranks it: `<!--` in a span is text, and so is
    an escaped `\\<` or `\\``. A span closes on a run of its length; a
    backslash in it is text. A run whose column is in `literal` is text.
    """
    position, column = 0, -1
    while True:
        if commented:
            end = line.find("-->", position)
            if end < 0:
                return True, "", -1
            commented, position = False, end + 3
        elif ticks:
            close = re.compile(r"(?<!`)" + ticks + r"(?!`)").search(line, position)
            if close is None:
                return False, ticks, column
            ticks, position, column = "", close.end(), -1
        elif (match := _INLINE.search(line, position)) is None:
            return False, "", -1
        else:
            position = match.end()
            if match[0] == "<!--":
                commented = True
            elif match[0][0] == "`" and match.start() not in literal:
                ticks, column = match[0], match.start()


def owned_lines(body: str) -> list[OwnedLine]:
    """Every line of `body` that starts with an owned key, indented or not, quoted or not."""
    found = []
    quoted = quoted_lines(body)
    for number, line in enumerate(body.split("\n"), start=1):
        match = _OWNED_RE.match(line.rstrip("\r"))
        if match is not None:
            found.append(OwnedLine(number, line.rstrip("\r"), _CANONICAL[match["key"].lower()],
                                   match["value"].strip(), bool(match["indent"]), number in quoted))
    return found


def problem(line: OwnedLine, item: int | None) -> str | None:
    """Why `line` cannot be stripped from an item's body, or None when it can."""
    if line.quoted:
        return ("a code block or comment holds it, so it reads as an example; indent it to keep it as one, "
                "or move it out of the block to make it a trailer")
    if line.key in (sd_lib.ITEM_TRAILER, sd_lib.WORK_TRAILER, sd_lib.DELIVERS_TRAILER) or item is None:
        return None  # sd:2999: sd-ship writes the links itself, so a stray one is dropped
    return ("a body `Closes:` line closes nothing; name each item the merge closes with --item "
            "on `sd-ship lane enqueue` (sd:3704)")


def normalize(body: str, item: int | None) -> tuple[str, tuple[str, ...]]:
    """`body` without the owned lines that agree with `sd-ship`, and those lines.

    Refuses, naming every offending line, when any owned line disagrees. With
    no item and nothing to strip, `body` is returned unchanged. Otherwise the
    result carries no trailing whitespace, which is what makes
    `normalize(published)` equal the body before `Work:` was appended.
    """
    read = [line for line in owned_lines(body) if not line.indented]
    if item is None and not read:
        return body, ()
    problems = [(line, reason) for line in read
                if (reason := problem(line, item)) is not None]
    if problems:
        raise Refusal("the ship adapter owns association and delivery trailers: "
                      + "; ".join(f"line {line.number}: `{line.text}`; {reason}" for line, reason in problems),
                      code="body_trailer_refused", boundary="input", state="operator_decision",
                      next_action="Remove or correct the named lines; sd-ship writes them itself.")
    stripped = {line.number for line in read}
    lines = body.split("\n")
    kept: list[str] = []
    for number, text in enumerate(lines, start=1):
        # A stripped line between two blank ones leaves one gap, not two.
        if number in stripped or (number - 1 in stripped and not text.strip()
                                  and kept and not kept[-1].strip()):
            continue
        kept.append(text)
    return "\n".join(kept).rstrip(), tuple(line.text for line in read)


#: An `sd:<n>` a title names: context for a reader, never a close (sd:3704).
_TITLE_ID = re.compile(r"\bsd:([0-9]+)\b")


def title_ids(title: str) -> tuple[int, ...]:
    """The whole `sd:<n>` tokens `title` names, in order, without repeats."""
    return tuple(dict.fromkeys(int(number) for number in _TITLE_ID.findall(title)))


def items_closed(named, item: int, deliver: bool) -> tuple[int, ...]:
    """The items a merge closes beside `item`: each of `named`, in order, without repeats.

    `named` is each item prepare's later `--item` names (sd:3704); a receipt
    prepared before that passes its title's ids, the rule it was sent under
    (sd:3014). An associate-only claim closes nothing (sd:3365).
    """
    if not deliver:
        return ()
    return tuple(dict.fromkeys(number for number in named if number != item))


def refuse_teammate_held(connection, items) -> None:
    """Refuse delivering any of `items` while an open teammate run holds it (pack sd:7221).

    A lead's in-session builder may still write its branch, and it beats
    only at commits and reports, so no silence proves it stopped: the lead's
    `--end` does. The one check of the delivered set: `sd-ship lane enqueue`
    once its cited closes are read, `sd-ship prepare`, and `sd-ship merge`
    at its closing check and again right before it publishes.
    """
    sd_lib.import_sd_db()  # every caller holds an sd_db connection, so this finds it
    from sd_db import teammate  # noqa: PLC0415
    for number in dict.fromkeys(items):
        held = teammate.holding(connection, number)
        if held:
            raise Refusal(f"sd:{number} is held by teammate run {held['id']}, whose builder may still write",
                          code="teammate_held", boundary="input", state="operator_decision",
                          next_action=f"Once its builder stops, end the run with `sd assignments beat {number} "
                                      "--end`, then deliver.")


#: The refusal code of a delivery that names a build member the operator cancelled on its own (pack sd:9222).
MEMBER_LEFT = "member_cancelled_alone"


def refuse_left(connection, branch: str, items, then: str = "nothing was published") -> None:
    """Refuse delivering any of `items` that the build bundle on `branch` left: each member the operator cancelled
    on its own (`build.left`, pack sd:9222). `sd-ship merge` checks right before it publishes, the one point every
    delivery passes; prepare, and the lane's enqueue, retry and run, refuse earlier with the same rule."""
    sd_lib.import_sd_db()  # every caller holds an sd_db connection, so this finds it
    from sd_db import build  # noqa: PLC0415
    left = sorted(build.left(connection, branch, items))
    if left:
        named = ", ".join(f"sd:{number}" for number in left)
        raise Refusal(f"{named} was cancelled on its own out of the build bundle on {branch}, so this delivery may "
                      f"not close it; {then}", code=MEMBER_LEFT, boundary="input", state="operator_decision",
                      next_action="Drop it from the delivery (its --item, or the citation), then deliver again; "
                                  "build it again to deliver it on its own branch.")


def refuse_design_waiting(connection, items, then: str = "nothing was published") -> None:
    """Refuse delivering a design item of `items` whose question waits again (`build.design_waits`): Decide reversed
    or a review withdrew its ruling after the enqueue left it in (review of sd:10096). `sd-ship merge` checks right
    before it publishes, beside `refuse_left`."""
    sd_lib.import_sd_db()  # every caller holds an sd_db connection, so this finds it
    from sd_db import build  # noqa: PLC0415
    for number in dict.fromkeys(items):
        why = build.design_waits(connection, number)
        if why:
            raise Refusal(f"sd:{number} {why}, so this delivery may not close it; {then}", code="design_waits",
                          boundary="input", state="operator_decision",
                          next_action="Rule on the question, then deliver again; or drop the design from the "
                                      "delivery (its --item and its commit).")


def refuse_unclosable(connection, item: int, repo: str, named, deliver: bool) -> None:
    """Refuse a co-delivered item the merge could not close; `repo` is `item`'s registered path.

    One pull request claims one item; each later `--item` (`named`) names
    another it delivers, and the merge closes them (operator ruling
    2026-10-13, sd:3704). An associate-only claim closes nothing, so a later
    `--item` on one is refused rather than dropped (sd:3365). `sd-ship prepare` and `sd-ship lane
    enqueue` both read it (sd:3473).
    """
    if not deliver and named:
        raise Refusal(f"an associate-only claim on sd:{item} closes nothing, and it names "
                      f"{', '.join(f'`--item {number}`' for number in named)}",
                      code="closes_need_delivery", boundary="input", state="operator_decision",
                      next_action="Drop the extra --item, or name it on the item's --deliver pull request.")
    for number in items_closed(named, item, deliver):
        row = connection.execute("SELECT repo, kind FROM item WHERE id = ?", (number,)).fetchone()
        if row is None:
            raise Refusal(f"--item names sd:{number}, which is no item; "
                          "the merge closes every item it names",
                          code="closes_item_unknown", boundary="input", state="operator_decision",
                          next_action="Correct the --item, or name a related item in a Refs: line.")
        if not row["repo"]:
            # sd:5065: a report has no repository to close it from; it closes by hand.
            close = (f"sd reports acknowledge {number}" if row["kind"] == "report"
                     else f"sd task status {number} done")
            raise Refusal(f"--item names sd:{number}, which belongs to no repository; "
                          "a merge closes only this repository's items",
                          code="closes_item_foreign", boundary="input", state="operator_decision",
                          next_action=f"Drop sd:{number} from --item, and close it after the merge with `{close}`.")
        if row["repo"] != repo:
            raise Refusal(f"--item names sd:{number}, which belongs to another "
                          "repository; a merge closes only this repository's items",
                          code="closes_item_foreign", boundary="input", state="operator_decision",
                          next_action="Close that item from its own repository, or name it in a Refs: line.")


#: A `## Summary` group heading: a column-zero line or bullet that opens `<repo> sd:<n>` (sd:5375).
_HEADING = re.compile(r"^(?:[-*] +)?([A-Za-z0-9_.-]+) sd:([0-9]+)\b")
#: A level-one or level-two ATX heading, its text without closing hashes (CommonMark).
_SECTION = re.compile(r"^ {0,3}#{1,2}(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
#: A column-zero `Refs:` line; the items it names stay open.
_REFS = re.compile(r"^Refs[ \t]*:(.*)$", re.IGNORECASE)
#: The kinds a merge closes (`close_items`) and the statuses it would close from.
_CLOSABLE_KINDS = ("work", "task", "followup")
_OPEN_STATUSES = ("planning", "ready", "in_progress", "ready_to_send")
#: How `cited_sources` names a `Closes:` trailer citation; `trailer_cited` reads it back.
TRAILER_SOURCE = "Closes trailer of commit "


def summary_ids(body: str, names) -> tuple[int, ...]:
    """The ids the `## Summary` group headings of `body` name under one of `names`, in order, once.

    A heading is a line at column zero, or a `- ` bullet, that opens with
    `<repo> sd:<n>`, as builders write one group per item (sd:5375). The
    section runs to the next `#` or `##` heading; a code block or comment
    holds examples, neither headings nor groups. Each name is compared
    case-folded.
    """
    names = {name.lower() for name in names if name}
    quoted = quoted_lines(body)
    found, inside = [], False
    for number, line in enumerate(body.split("\n"), start=1):
        line = line.rstrip("\r")
        if number in quoted:
            continue
        section = _SECTION.match(line)
        if section:
            inside = (section[1] or "").strip().lower() == "summary"
            continue
        match = _HEADING.match(line)
        if inside and match and match[1].lower() in names:
            found.append(int(match[2]))
    return tuple(dict.fromkeys(found))


def referenced_ids(body: str) -> set[int]:
    """The ids the column-zero `Refs:` lines of `body` name: related items the merge leaves open.

    Text in an HTML comment names nothing, since a reader of the page never sees it.
    """
    quoted = quoted_lines(body)
    return {int(number) for index, line in enumerate(body.split("\n"), start=1)
            if index not in quoted and (match := _REFS.match(line.rstrip("\r")))
            for number in _TITLE_ID.findall(re.sub(r"<!--.*?(?:-->|$)", "", match[1]))}


#: A `Lane review fix, <repo> sd:<n>` line: a fix the lane's review asked for, and the item it delivers (sd:5999).
_REVIEW_FIX = re.compile(r"^Lane review fix, [A-Za-z0-9_.-]+ sd:([0-9]+)\b")


#: A `Closes: sd:<n>` line, as `sd_lib._closes` reads it in a commit's trailer block.
_CLOSES = re.compile(rf"^{re.escape(sd_lib.CLOSES_TRAILER)} sd:([0-9]+)[ \t]*$")


#: The line `git revert` writes into a revert's message, with the mainline parent when it reverts a merge.
_REVERTS = re.compile(r"^This reverts commit ([0-9a-f]{7,40})\b(?:, reversing\s+changes made to ([0-9a-f]{7,40}))?",
                      re.MULTILINE)

#: A revert's subject: `git revert`'s `Revert "..."`, or a Conventional `revert:` (sd:8373).
REVERT_SUBJECT = re.compile(r"^(?:Revert\b|revert(?:\([^)]*\))?!?:)", re.IGNORECASE)
#: An item a hand-written revert names an undone commit on: one that opens with a sha, bare or in a code
#: span, emphasis or a link's text (pack sd:9666). No letter, digit or `_`, in any script, may follow it,
#: but the `_` that closes `_<sha>_`.
_SHA_LED = re.compile(r"_([0-9a-f]{7,40})_(?!\w)|[`*\[]*([0-9a-f]{7,40})(?!\w)")
#: The line that opens a list of undone commits: its last sentence is exactly `Reverted commits:`,
#: `Reverted:` or `Reverts:`. A substring match let `Commits not reverted:` undo its list (pack sd:9666).
_REVERTED_INTRO = re.compile(r"(?:^|[.!?][ \t]+)(?:reverted(?:[ \t]+commits)?|reverts):[ \t]*$", re.IGNORECASE)


def _heading(text: str) -> str:
    """`text` without the markup a heading wears: `## ` and closing `#`s, and emphasis around it."""
    return re.sub(r"^#{1,6}[ \t]+|[ \t]+#+$", "", text.strip()).strip("*_ \t")


def _listed_undone(message: str) -> list[str]:
    """The shas a hand-written revert lists as undone (pack sd:9666).

    A list item that opens with a sha, under a heading such as `Reverted
    commits:`. A heading is a line that ends with a colon, bare, indented or
    itself a list item, or any line at column zero that is no list item.
    One exception: a line with no colon that runs on from a list item's
    text with no blank line between is that item's wrapped text, and
    heads nothing; the same words could be either, and a wrapped entry is
    the likelier one, since the heading that undoes must carry its colon.
    The top level and each open list item, as `_blocks` reads them,
    hold the last heading in them: a list item's own text is its first.
    A sha item undoes when the heading of what holds it says reverted, and
    passes that on to what nests in it; any other item, or a heading that
    does not say reverted, undoes nothing. An item inside a fence, an
    indented code block or an HTML comment names nothing, and neither
    does a line that starts inside a code span, in the read that finds
    it there.

    The `plain` grammar of `_blocks` and CommonMark, the read citations
    get, can give one line two roles: the fence a `10.` item opens is a
    code span to CommonMark, which reads no item there. Neither role is
    chosen. The list is read twice in the `plain` grammar, and a commit
    either read lists is undone, so a message the two read apart closes
    nothing: once whole, with the fences, comments and code spans that
    grammar finds, and once with only its fences, over the lines
    CommonMark leaves outside a comment and a code span.
    """
    shown = "\n".join(line for _, kind, *_, line, spanned in _blocks(message) if not spanned and kind != "comment")
    found = _listed(_blocks(message, plain=True))
    return found + [sha for sha in _listed(_blocks(shown, comments=False, plain=True)) if sha not in found]


def _listed(rows) -> list[str]:
    """The shas `_listed_undone` finds in one read of a message, `rows` of `_blocks`."""
    found, undoes, previous = [], [False], ""  # undoes: per open list item, the top level first, whether its heading says reverted
    for _, kind, opened, depth, raw, line, spanned in rows:
        kind = "span" if spanned else kind
        del undoes[depth - opened + 1:]  # the items this line ended take their headings with them
        holder, text = undoes[-1], raw.strip()
        if opened:  # an item, even one that opens with a fence or code, holds what nests in it
            holder = holder and opened == 1  # each outer item of `- - x` is empty: its heading says nothing
            if kind == "text" and (sha := _SHA_LED.match(text)):
                found += [sha[1] or sha[2]] if holder else []
            else:
                holder = kind == "text" and bool(_REVERTED_INTRO.search(_heading(text)))
            undoes += [False] * (opened - 1) + [holder]
        elif kind == "setext":  # the underline makes the line above it the heading
            undoes[-1] = bool(_REVERTED_INTRO.search(_heading(previous)))
        elif kind == "text" and text and (_heading(text).endswith(":") or not line[0].isspace() or _ATX.match(raw)):
            # Any other indented line continues an item.
            undoes[-1] = bool(_REVERTED_INTRO.search(_heading(text)))
        previous = text
    return found


def kept_commits(commits, parents=None) -> list:
    """`commits`, newest first, less each one a later commit of them reverts (sd:5999 lane review, rounds 2 and 3).

    A revert of a merge also undoes each commit the merge brought in over
    the mainline parent its message names, read from `parents` (sha to
    parent shas, the range's `git rev-list --parents`). A revert that is
    itself reverted undoes nothing, so its target stays. A hand-written
    revert (`REVERT_SUBJECT`) also undoes each commit its list of reverted
    commits names (`_listed_undone`): a split listed its commits by short
    sha, and the squash closed the items they cited (pack sd:9666). A
    listed merge names no mainline, so it undoes itself alone: the lane's
    file check (`sd_lane.unreverted`) decides each commit it brought in.
    """
    parents = parents or {}

    def full(prefix: str) -> str:
        return next((sha for sha, _ in commits if sha.startswith(prefix)), prefix)

    def only(prefix: str) -> str | None:
        """The one branch commit `prefix` names; None when it names none or several, as git refuses."""
        named = [sha for sha, _ in commits if sha.startswith(prefix)]
        return named[0] if len(named) == 1 else None

    def ancestors(start: str) -> set[str]:
        seen, todo = set(), [start]
        while todo:
            sha = todo.pop()
            if sha not in seen:
                seen.add(sha)
                todo += parents.get(sha, ())
        return seen

    undone: set[str] = set()
    kept = []
    for sha, message in commits:
        if sha in undone:
            continue
        trailed = set()
        for target, mainline in _REVERTS.findall(message):
            trailed.add(full(target))
            undone |= ancestors(full(target)) - ancestors(full(mainline)) if mainline else {full(target)}
        if REVERT_SUBJECT.match(message):
            # git's trailer names a commit's mainline; listing that commit too adds nothing to it.
            # A listed merge names no mainline, so it undoes itself alone; `sd_lane.unreverted` reads
            # each commit it brought in at the head. git's trailer names a mainline (pack sd:9666).
            undone |= set(filter(None, map(only, _listed_undone(message)))) - trailed
        kept.append((sha, message))
    return kept


def cited_sources(body: str, commits, parents=None) -> dict[int, str]:
    """The ids a merge delivers by citation (operator ruling 2026-10-24, sd:5999), each with what cites it first.

    Each `Closes: sd:<n>` line in the trailer block (`sd_lib.trailer_block`)
    of a branch commit, and each `Lane review fix, <repo> sd:<n>` line of
    `body` or of a commit message. `commits` is each `(sha, message)` the
    branch adds. Only those forms (lane review, round 5 ruling): a subject
    or prose such as "first part of sd:<n>" cites nothing, since no word
    list catches every way to say partial, and a line a fenced code block
    or an HTML comment holds is an example. A commit a later branch commit
    reverts cites nothing either (`kept_commits`).
    """
    commits = kept_commits(commits, parents)
    found: dict[int, str] = {}
    for sha, message in commits:
        text = message.replace("\r\n", "\n").rstrip()
        block = sd_lib.trailer_block(text).split("\n") if "\n\n" in text else []  # a subject closes nothing
        quoted = quoted_lines(text)  # a block an open fence or comment holds is an example
        for number, line in enumerate(block, start=text.count("\n") + 2 - len(block)):
            if number not in quoted and (match := _CLOSES.match(line)):
                found.setdefault(int(match[1]), f"{TRAILER_SOURCE}{sha[:12]}")
    for text, where in ((body, "the body"), *((message, f"commit {sha[:12]}") for sha, message in commits)):
        quoted = quoted_lines(text)
        for number, line in enumerate(text.split("\n"), start=1):
            if number not in quoted and (match := _REVIEW_FIX.match(line.rstrip("\r"))):
                found.setdefault(int(match[1]), f"review fix line in {where}")
    return found


def trailer_cited(sources: dict[int, str]) -> set[int]:
    """Of `cited_sources`' answer, the ids a `Closes:` trailer cites; trailers are read first, so they win."""
    return {number for number, source in sources.items() if source.startswith(TRAILER_SOURCE)}


def cited_ids(body: str, commits, parents=None) -> tuple[int, ...]:
    """The ids `cited_sources` finds, in order."""
    return tuple(cited_sources(body, commits, parents))


#: A trailer line, as a body's last paragraph holds them.
_TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:[ \t]")


def with_cited(body: str, lines: list[str], heading: str = "Closed by citation") -> str:
    """`body` with a `## <heading>` section of `lines`, before a closing trailer paragraph (sd:5999).

    A reader of the pull request sees each close by citation, so a wrong
    one is one reopen, and each citation the merge leaves open (`Left
    open`, sd:9222). The trailers stay the body's last paragraph. A body
    that leaves a fenced code block or an HTML comment open would hide
    the section in it, so it refuses `body_block_open` (sd:9222).
    """
    if not lines:
        return body
    probe = f"{body.rstrip(chr(10))}\n\nprobe"  # a line after the body: quoted means a block is still open
    if probe.count("\n") + 1 in quoted_lines(probe):
        raise Refusal(f"the body leaves a fenced code block or an HTML comment open, so its `## {heading}` "
                      "section would sit inside it, hidden; nothing was queued", code="body_block_open",
                      boundary="input", state="operator_decision",
                      next_action="Close the fence or the comment in the body file, then enqueue again.")
    section = f"## {heading}\n\n" + "\n".join(lines) + "\n"
    text = body.rstrip("\n")
    head, _, last = text.rpartition("\n\n")
    if last and all(_TRAILER.match(line) for line in last.split("\n") if line.strip()):
        return f"{head}\n\n{section}\n{last}\n" if head else f"{section}\n{last}\n"
    return f"{text}\n\n{section}"


def _left(connection, item: int, body: str, named, numbers, branch: str) -> set[int]:
    """Of `numbers`, those a delivering merge of `item` leaves open: `item` and `named`, already closed, each a
    `Refs:` line of `body` names, and each member an operator cancelled on its own out of the build bundle that
    published `branch`, read from data so no commit, title or body text delivers it (`build.left`, review rounds
    4 and 5 of pack sd:9222)."""
    sd_lib.import_sd_db()  # every caller holds an sd_db connection, so this finds it
    from sd_db import build  # noqa: PLC0415
    return referenced_ids(body) | {item, *named} | build.left(connection, branch, numbers)


def _cited_rows(connection, item: int, repo: str, cited, named, body: str, branch: str, statuses) -> tuple[int, ...]:
    """Of `cited`, this repository's closable items in one of `statuses`, less those `_left` keeps open."""
    left = _left(connection, item, body, named, cited, branch)
    kept = []
    for number in cited:
        if number in left:
            continue
        row = connection.execute("SELECT repo, kind, status FROM item WHERE id = ?", (number,)).fetchone()
        if row is not None and row["repo"] == repo and row["kind"] in _CLOSABLE_KINDS and row["status"] in statuses:
            kept.append(number)
    return tuple(kept)


def blocked_cited(connection, item: int, repo: str, trailers, named, body: str, branch: str = "") -> tuple[int, ...]:
    """Of `trailers`, the ids a branch commit's `Closes:` trailer cites, the blocked items the merge would leave
    open unseen (sd:9918). A block may hold for a reason the commit does not settle, a followup the operator
    left, so the lane closes none on its own: enqueue refuses until `--item` names each or a `Refs:` line
    keeps it."""
    return _cited_rows(connection, item, repo, trailers, named, body, branch, ("blocked",))


def open_cited(connection, item: int, repo: str, cited, named, body: str, branch: str = "") -> tuple[int, ...]:
    """Of `cited`, the items a merge of `item` should close beyond `named`, its later `--item` list (sd:5999).

    An item counts when it belongs to `repo`, is a kind a merge closes, and
    is open, as `refuse_left_open` reads them; a `Refs:` line of `body`
    keeps one open, as does the data `_left` reads. A blocked one is
    `blocked_cited`'s.
    """
    return _cited_rows(connection, item, repo, cited, named, body, branch, _OPEN_STATUSES)


def refuse_left_open(connection, item: int, repo: str, names, title: str, body: str, named, deliver: bool,
                     branch: str = "") -> None:
    """Refuse a delivering entry whose title or Summary headings name an open item missing from --item (sd:5375).

    The --item list stays the one closing set (operator ruling 2026-10-13,
    sd:3704); this asks for it complete before anything is queued. A
    bundled pull request named only its first --item, and 18 items it
    delivered stayed open; prepare's title warning ran inside the lane,
    where no one read it. An item counts when it belongs to `repo`, is a
    kind a merge closes, and is open: done and blocked items are not
    demanded. A `Refs:` line names one the merge leaves open. `names`
    are the repository names a heading may use (`summary_ids`).
    """
    if not deliver:
        return
    named_ids = tuple(dict.fromkeys(title_ids(title) + summary_ids(body, names)))
    left = _left(connection, item, body, named, named_ids, branch)
    missing = []
    for number in named_ids:
        if number in left:
            continue
        row = connection.execute("SELECT repo, kind, status FROM item WHERE id = ?", (number,)).fetchone()
        if row is not None and row["repo"] == repo and row["kind"] in _CLOSABLE_KINDS \
                and row["status"] in _OPEN_STATUSES:
            missing.append(number)
    if missing:
        raise Refusal("the title or a Summary heading names "
                      + ", ".join(f"sd:{number}" for number in missing)
                      + ", which this merge leaves open; add "
                      + " ".join(f"--item {number}" for number in missing),
                      code="closing_set_incomplete", boundary="input", state="operator_decision",
                      next_action="Enqueue again with each item the pull request delivers named by --item, "
                                  "or name an item it leaves open in a Refs: line.")


def published(body: str, item: int) -> str:
    """The body as `sd-ship` publishes it: normalized, then one `Work:` line."""
    return f"{body}\n\n{sd_lib.WORK_TRAILER} sd:{item}\n"


#: The longest commit message or body line; some repositories lint it
#: (operator ruling 2026-10-22, sd:5793).
LINE_LIMIT = 72
#: The longest title: the squash subject is the title and ` (#<number>)`,
#: which `sd-ship merge` adds as GitHub does; five digits are budgeted.
TITLE_LIMIT = LINE_LIMIT - len(" (#NNNNN)")
#: A line that is one bare URL and nothing else, which no wrap can shorten.
_BARE_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


def long_lines(text: str) -> list[tuple[int, int]]:
    """`(line number, length)` of each line of `text` over `LINE_LIMIT`, 1-based.

    Two kinds of line are exempt, and nothing else: a line a fenced code
    block holds, and a line that is one bare URL. An HTML comment is no
    exemption: git and a commit lint read its lines.
    """
    fenced = quoted_lines(text, comments=False)
    lines = (line.rstrip("\r") for line in text.split("\n"))
    return [(number, len(line)) for number, line in enumerate(lines, start=1)
            if len(line) > LINE_LIMIT and number not in fenced and not _BARE_URL.fullmatch(line)]


def refuse_long_lines(title: str, body: str, commits) -> None:
    """Refuse a title over `TITLE_LIMIT`, or a body or commit line over `LINE_LIMIT` (sd:5793).

    `commits` is each `(sha, message)` the branch adds. The refusal names
    every offending line: where it is, its length and the limit.
    """
    found = [f"the title is {len(title)} characters (limit {TITLE_LIMIT})"] if len(title) > TITLE_LIMIT else []
    found += [f"body line {number} is {length} characters (limit {LINE_LIMIT})"
              for number, length in long_lines(body)]
    found += [f"commit {sha[:12]} line {number} is {length} characters (limit {LINE_LIMIT})"
              for sha, message in commits for number, length in long_lines(message)]
    if found:
        merge = any(message.startswith("Merge ") and long_lines(message.split("\n", 1)[0]) for _, message in commits)
        raise Refusal("lines too long: " + "; ".join(found),
                      code="line_too_long", boundary="input", state="operator_decision",
                      next_action=f"Keep the title at {TITLE_LIMIT} characters or less, wrap the named "
                                  f"body and commit lines at {LINE_LIMIT}, then enqueue again."
                                  + (' A merge subject names the branch: redo that merge with `git merge --no-ff -m '
                                     '"chore: merge origin/main" origin/main`, or, when it is the head, '
                                     '`git commit --amend -m "chore: merge origin/main"`.' if merge else ""))


def refuse_unconventional_title(title: str) -> None:
    """Refuse a title that is not a Conventional Commits header (`sd_lib.conventional_title`, sd:6059).

    The title is the squash subject, so it is what a commit lint and
    semantic-release read on the default branch.
    """
    if not sd_lib.conventional_title(title):
        raise Refusal(f'the title "{title}" is not a Conventional Commits header, '
                      "<type>[(<scope>)][!]: <summary>",
                      code="title_not_conventional", boundary="input", state="operator_decision",
                      next_action=f"Retitle with one of the types {', '.join(sd_lib.CONVENTIONAL_TYPES)}, "
                                  "such as `fix(lane): ...`, then enqueue again.")


def docs_lint(root: pathlib.Path, body: str, base: str) -> list[str]:
    """Prepare's `sd-docs-lint` of `root`, with `body` as `--pr-body`; the warnings it leaves.

    Run from the repository root with no `--work-dir`, and with Jev off.
    A failure is judged against `origin/<base>` (`lint_against_base`), so
    only what the branch introduces refuses. Lane enqueue runs the same
    lint, so a lint failure refuses in seconds there (sd:5989).
    """
    lint = ["env", "JEV_SD_DOCS_LINT=0", sys.executable, str(BIN / "sd-docs-lint")]
    if not (root / "docs/work").is_dir():
        lint.append("--body-only")
    with tempfile.TemporaryDirectory(prefix="sd-ship-lint-") as directory:
        body_path = pathlib.Path(directory) / "pr-body.md"
        body_path.write_text(body, encoding="utf-8")
        argv = [*lint, "--pr-body", str(body_path)]
        try:
            completed_process(root, argv, timeout=300)
        except Refusal as refusal:
            # Failed: run it again in full and gate only on what the
            # branch introduces, not on what origin/<base> carries (sd:1646).
            if refusal.workflow["blocker"]["code"] != "command_failed":
                raise
            return lint_against_base(root, argv, base)
    return []


def failures(result) -> list[str]:
    """The `FAIL` lines of a finished lint.

    A non-zero exit that printed no `FAIL` line is refused with its raw output:
    the lint also exits 1 on an uncaught exception, and a traceback read as
    zero failures would let a lint that never finished pass.
    """
    found = [line[len("FAIL "):] for line in result.stderr.splitlines() if line.startswith("FAIL ")]
    if result.returncode and not found:
        raise Refusal((result.stderr or result.stdout or "sd-docs-lint failed").strip()[-2000:],
                      code="docs_lint_failed", state="retryable_failure",
                      next_action="Inspect the lint error, resolve its cause, then prepare again.")
    return found


def lint_failures(tree: pathlib.Path, argv: list[str]) -> list[str]:
    """`failures` of `argv` run in `tree`, each path made relative to `tree`."""
    result = completed_process(tree, argv, timeout=300, answers=frozenset({0, 1}))
    # Longest first: a resolved `/private/var/...` contains the `/var/...` form.
    prefixes = sorted({f"{tree}/", f"{tree.resolve()}/"}, key=len, reverse=True)
    found = []
    for failure in failures(result):
        for prefix in prefixes:
            failure = failure.replace(prefix, "")
        found.append(failure)
    return found


def base_lint_failures(root: pathlib.Path, argv: list[str], base: str) -> set[str]:
    """`lint_failures` in a scratch checkout of `origin/<base>`; empty if it cannot be checked out or linted.

    Hooks are off for the checkout: a consumer's `post-checkout` is no part of a lint.
    Git runs through `completed_process`, whose timeout a large tree's checkout fits.
    """
    with tempfile.TemporaryDirectory(prefix="sd-ship-lint-base-") as directory:
        tree = pathlib.Path(directory) / "base"
        try:
            completed_process(root, ["git", "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach",
                                     "--quiet", str(tree), f"refs/remotes/origin/{base}"], timeout=300)
        except Refusal:
            return set()
        try:
            return set(lint_failures(tree, argv))
        except Refusal:
            # A base run that did not finish excuses nothing.
            return set()
        finally:
            completed_process(root, ["git", "worktree", "remove", "--force", str(tree)], timeout=300,
                              answers=frozenset(range(256)))


def lint_against_base(root: pathlib.Path, argv: list[str], base: str) -> list[str]:
    """Judge a failed docs lint against the same lint at `origin/<base>` (sd:1646).

    A tree failure the default branch already has is not the branch's to fix:
    it comes back as a warning, and only the failures the branch introduces
    refuse. The base run gets no `--pr-body`, since the body is the branch's
    own, so a body-rule failure is never excused. Under `--body-only` no tree
    rule ran, and the head's answer stands. A failure is matched by its whole
    line, so one the branch moved to another line counts as introduced.
    """
    head = lint_failures(root, argv)
    known: list[str] = []
    if head and "--body-only" not in argv:
        at = argv.index("--pr-body") if "--pr-body" in argv else len(argv)
        on_base = base_lint_failures(root, argv[:at] + argv[at + 2:], base)
        known = [failure for failure in head if failure in on_base]
    introduced = [failure for failure in head if failure not in known]
    if introduced:
        also = f"\n{len(known)} more failure(s) already on origin/{base} do not block this branch." if known else ""
        raise Refusal(f"sd-docs-lint: {len(introduced)} failure(s) this branch introduces:\n"
                      + "\n".join(f"FAIL {failure}" for failure in introduced) + also,
                      code="docs_lint_failed", state="retryable_failure",
                      next_action="Fix the failures this branch introduces, commit, then prepare again.")
    return [f"sd-docs-lint: {len(known)} failure(s) already on origin/{base}, not introduced by this branch, "
            "do not block it: " + "; ".join(known)] if known else []
