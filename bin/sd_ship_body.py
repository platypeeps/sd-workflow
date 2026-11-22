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


def quoted_lines(body: str, comments: bool = True) -> set[int]:
    """The 1-based numbers of the lines of `body` a fenced code block or an HTML comment holds.

    A fence closes on a line of the same character, at least as long, and
    nothing else; an unclosed fence runs to the end. A comment holds a line
    when it is open where the line starts. Inside a fence a comment is text.
    With `comments` false only fences count, and a comment is text too.
    """
    quoted: set[int] = set()
    fence = ""
    commented = False
    for number, line in enumerate(body.split("\n"), start=1):
        line = line.rstrip("\r")
        if fence:
            quoted.add(number)
            match = _FENCE_RE.match(line)
            if match and match["fence"][0] == fence[0] and len(match["fence"]) >= len(fence) and not match["info"].strip():
                fence = ""
            continue
        if commented:
            quoted.add(number)
        match = _FENCE_RE.match(line)
        if not commented and match and not (match["fence"][0] == "`" and "`" in match["info"]):
            quoted.add(number)
            fence = match["fence"]
            continue
        rest = line
        while comments:
            if commented:
                end = rest.find("-->")
                if end < 0:
                    break
                commented, rest = False, rest[end + 3:]
            else:
                start = rest.find("<!--")
                if start < 0:
                    break
                commented, rest = True, rest[start + 4:]
    return quoted


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
#: A commit's own `Closes:` trailer closes a parked item too: one waiting on the bundle that carries it (sd:9918).
_CITED_STATUSES = (*_OPEN_STATUSES, "blocked")


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


def kept_commits(commits, parents=None) -> list:
    """`commits`, newest first, less each one a later commit of them reverts (sd:5999 lane review, rounds 2 and 3).

    A revert of a merge also undoes each commit the merge brought in over
    the mainline parent its message names, read from `parents` (sha to
    parent shas, the range's `git rev-list --parents`). A revert that is
    itself reverted undoes nothing, so its target stays.
    """
    parents = parents or {}

    def full(prefix: str) -> str:
        return next((sha for sha, _ in commits if sha.startswith(prefix)), prefix)

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
        for target, mainline in _REVERTS.findall(message):
            undone |= ancestors(full(target)) - ancestors(full(mainline)) if mainline else {full(target)}
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
                found.setdefault(int(match[1]), f"Closes trailer of commit {sha[:12]}")
    for text, where in ((body, "the body"), *((message, f"commit {sha[:12]}") for sha, message in commits)):
        quoted = quoted_lines(text)
        for number, line in enumerate(text.split("\n"), start=1):
            if number not in quoted and (match := _REVIEW_FIX.match(line.rstrip("\r"))):
                found.setdefault(int(match[1]), f"review fix line in {where}")
    return found


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


def open_cited(connection, item: int, repo: str, cited, named, body: str, branch: str = "") -> tuple[int, ...]:
    """Of `cited`, the items a merge of `item` should close beyond `named`, its later `--item` list (sd:5999).

    An item counts when it belongs to `repo`, is a kind a merge closes, and
    is open or blocked; a blocked one waits on the commit that cites it
    (sd:9918). A `Refs:` line of `body` keeps one open, as does the data
    `_left` reads.
    """
    left = _left(connection, item, body, named, cited, branch)
    kept = []
    for number in cited:
        if number in left:
            continue
        row = connection.execute("SELECT repo, kind, status FROM item WHERE id = ?", (number,)).fetchone()
        if row is not None and row["repo"] == repo and row["kind"] in _CLOSABLE_KINDS \
                and row["status"] in _CITED_STATUSES:
            kept.append(number)
    return tuple(kept)


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
