"""Writing pieces: `document` feeds /api/writing (sd:2125); the old screen was retired in sd:3402.

`document` is each piece's stage, gates and next moves as `writing.piece_state` gives them, its files' sizes, and its
draft up to `DRAFT_CHARS` (pack sd:7242). The repository goes out by its folder name only, so no local path leaves. Its `attention` is the
page's rail badge (sd:3446, `_attention`). Each piece's `realm` is `places.realm_of` of its repo, the rule Tasks' Realm
filter uses (sd:5064). Its `grill` is its open question notes and the count of its rulings (pack sd:6700): Details
walks the questions, and Apply answers waits until none is open. Its `ideas` are the vault's inbox blog ideas
(pack sd:7244), the `sdw.blog-idea` rows `records.list_rows` reads, highest score first: IDEAS of them beside the inbox
count, each with a link only for http. A failed read is unknown with its reason, never an empty inbox. Each idea's
`area` (pack sd:7670) is one of `AREAS`, read from its `contexts` field, the vault's Personal-or-Work axis (`area`
holds its topic); another value or none is no area (""). `areas` counts each, and `rows` holds the highest IDEAS
overall and of each area, so the column filtered to one area still shows that area's best. Its `mirror` is
the Drive folder its stage mirrors to and each mirrored file's Drive copies (`publishing.piece_mirror`, pack sd:7243);
a queue not read is unknown with its reason.
"""

import json
import math
import re
from pathlib import Path

from sd_db import paths, records, writing
from sd_db.workflow import WorkflowError

from . import publishing
from .decisions import RULING, parse
from .markup import href
from .places import realm_of, realms

#: Draft characters a piece carries to the editor, whole paragraphs only: the longest draft measured 2026-11-02 held
#: 19,347 bytes and all 28 held 310,061, so a draft past this is cut and says so (pack sd:7242). A first paragraph
#: past it keeps an excerpt cut at a word, so a long draft never reads as no draft.
DRAFT_CHARS = 64_000


def _draft(document: str | None) -> tuple[list[str], bool, int]:
    """The draft's paragraphs up to DRAFT_CHARS, whether that cut any, and its word count, from the text after `## Draft`."""
    prose = document.split("## Draft", 1)[1] if document and "## Draft" in document else ""
    paragraphs = [" ".join(block.split()) for block in prose.split("\n\n") if block.strip() and not block.lstrip().startswith("#")]
    kept, size = [], 0
    for paragraph in paragraphs:
        size += len(paragraph)
        if size > DRAFT_CHARS:
            if not kept:  # pack sd:7242 review: an excerpt, cut at the last word that fits, or mid-word if none does
                excerpt = paragraph[:DRAFT_CHARS]
                at_word = paragraph[DRAFT_CHARS] == " " or " " not in excerpt
                kept.append(excerpt if at_word else excerpt.rsplit(" ", 1)[0])
            return kept, True, len(prose.split())
        kept.append(paragraph)
    return kept, False, len(prose.split())


#: A gate's view icon reads its report at /api/writing/<item>/<gate> (pack sd:7242), cut at REPORT_CHARS: of the 60
#: reports measured 2026-11-02 the largest held 261,321 bytes and all held 1,766,726, so a cut is rare and says so.
REPORT = r"/api/writing/([1-9][0-9]{0,17})/(research|fact-check|adversarial)"
REPORT_CHARS = 512_000


def report(connection, path: str):
    """One piece report, read only: `{item, name, text, cut}`, or a 404 that names the missing file or the non-piece."""
    item, gate = re.fullmatch(REPORT, path).groups()
    item, name = int(item), writing.REPORTS[gate]
    try:
        text = writing.read_report(connection, item, gate, REPORT_CHARS)
    except WorkflowError as error:
        return 404, {"error": f"#{item}: {error}"}
    except OSError as error:  # a failed read is not a missing file: it says its cause
        return 500, {"error": f"#{item} {name} not read: {error.strerror or error}"}
    if text is None:
        return 404, {"error": f"#{item} has no {name}."}
    return 200, {"item": item, "name": name, "text": text[:REPORT_CHARS], "cut": len(text) > REPORT_CHARS}


#: Inbox ideas the Ideas column shows; the header counts them all.
IDEAS = 10

#: An idea's areas (pack sd:7670), in the order the column's tiles show; RWBP is the brand website.
AREAS = ("Personal", "Work", "RWBP")


def read_ideas(connection, kind="blog-idea") -> list[dict]:
    return records.list_rows(connection, kind, stage="inbox")


def _score(value) -> float | None:
    """A finite number, or None: the fields are JSON, so a score may be a bool, 'nan', 'inf', '1e999', an inf literal
    or 10**400, which float() refuses with OverflowError. One NaN in /api/writing fails the page's JSON parse, and one
    raise fails the reply, for every piece (pack sd:7244 review)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _text(value) -> str | None:
    """`value` when it is text, else None: a JSON field may hold any value, an inf number too."""
    return value if isinstance(value, str) else None


def _area(value) -> str:
    """The first of `value`'s words that names an area, as `AREAS` spells it, else "": a field may hold any JSON."""
    named = {area.casefold(): area for area in AREAS}
    for word in value if isinstance(value, list) else [value]:
        if isinstance(word, str) and word.strip().casefold() in named:
            return named[word.strip().casefold()]
    return ""


def _ideas(connection, kind="blog-idea", *, now) -> dict:
    """The inbox blog ideas, or tips (sd:8013), highest score first, or why they were not read. `pool` is every
    waiting row with its age and revision and `declined` every declined row with when and by what, for the bulk
    Decline and its Declined filter (sd:8014)."""
    from . import triage

    try:
        got = read_ideas(connection, kind)
        pool, declined = triage.pool(connection, kind, now=now), triage.declined(connection, kind)
    except Exception as error:  # noqa: BLE001 - a failed read says its cause, never an empty inbox
        return {"state": "unknown", "reason": str(error) or type(error).__name__, "count": None, "rows": [],
                "pool": [], "declined": []}
    rows = []
    for row in got:
        fields = row["fields"]
        url = _text(fields.get("url")) or ""
        rows.append({"item": row["id"], "title": row["title"], "score": _score(fields.get("score")), "url": url,
                     "href": href(url) if url else None, "created": _text(fields.get("dateCreated")),
                     "area": _area(fields.get("contexts"))})
    rows.sort(key=lambda idea: (idea["score"] is None, -(idea["score"] or 0), idea["title"].casefold()))
    areas = {area: n for area in (*AREAS, "") if (n := sum(idea["area"] == area for idea in rows))}
    kept = {id(idea) for idea in rows[:IDEAS]}
    for area in areas:
        kept.update(id(idea) for idea in [idea for idea in rows if idea["area"] == area][:IDEAS])
    return {"state": "read", "reason": "", "count": len(rows), "top": IDEAS, "areas": areas,
            "rows": [idea for idea in rows if id(idea) in kept], "pool": pool, "declined": declined}


def document(connection, *, now):
    """What the v2 Writing page reads: every piece, parked ones too, with its gates, moves and files."""
    from .hoa_screen import checkout

    pieces = []
    found = realms(connection)
    rules = found["list"] if found["state"] in ("read", "partial") else None
    hoa = checkout(connection)[0]
    try:
        kit = publishing.kit()
        mirrored = (kit.QUEUE, kit.receipts(), kit.PIECE_FOLDERS), ""
    except Exception as error:  # noqa: BLE001 - a queue not read is unknown, never "no copy"
        mirrored = (None, None, {}), f"the pack's mirror queue was not read: {error}"
    for row in writing.list_pieces(connection, include_parked=True):
        full = writing.piece_state(connection, row["id"])
        state = full["writing"]
        try:
            files = writing.piece_files(row)
        except (OSError, WorkflowError):
            files = {}
        draft, cut, words = _draft(state["document"])
        gates = state["gates"]
        claims = state.get("publications") or []
        live = next((claim for claim in reversed(claims) if _text(claim["url"])), None)
        pieces.append({
            "id": row["id"], "title": row["title"], "stage": row["stage"], "parked": state["parked"], "piece": row["piece"],
            "repo": Path(row["repo"]).name, "realm": realm_of(row["repo"], row["kind"], rules, hoa), "folder": "content-parked" if (row["path"] or "").startswith("content-parked/") else "content",
            "updated": row["updated_at"], "revision": full["revision"],
            "gates_ok": gates["ok"], "problems": gates["problems"], "owner": state["owner"],
            "ready_recorded": bool(row["ready_digest"]) and row["ready_digest"] == gates["digest"],
            "publication": {"claim": live["id"], "url": live["url"], "phase": live["phase"], "href": href(live["url"])} if live else None,
            "next": state["available_stages"], "corrections": state["correction_stages"],
            "files": files, "draft": draft, "draft_cut": cut, "words": words, "grill": _grill(full["notes"]),
            # pack sd:6977: adversarial.md against the draft by its digest stamp; Apply review shows only when current.
            "adversarial": gates.get("companions", {}).get("adversarial"),
            "mirror": _mirror(row, state["parked"], *mirrored),
        })
    return {"read": now, "stages": list(writing.STAGES), "pieces": pieces, "attention": _attention(pieces), "ideas": _ideas(connection, now=now),
            "tips": _ideas(connection, "tip", now=now),
            "realms": {key: found[key] for key in ("state", "source", "problems")}}


def _mirror(row, parked: bool, kept: tuple, unread: str) -> dict:
    """The piece's `mirror`: `piece_mirror` with the Drive folder its stage mirrors to, None for none (parked, or a
    stage before review), and what was not read; unknown (`folder` None, `unread` the reason) when the queue was not."""
    if unread:
        return {"folder": None, "copies": {}, "unread": unread}
    queue, receipts, folders = kept
    try:
        fields = json.loads(row["fields"] or "{}")
    except ValueError:
        fields = None
    folder = None if parked else folders.get(row["stage"])
    found = publishing.piece_mirror(paths.expand(row["repo"]), row["piece"], folder,
                                    fields if isinstance(fields, dict) else {}, queue=queue, receipts=receipts)
    return {**found, "unread": "" if isinstance(fields, dict) else "its fields are not a JSON object"}


def _grill(notes) -> dict:
    """The piece's open questions, oldest first, with their options; and how many questions it has ruled on."""
    asked = [{"note": note["id"], "question": question, "options": options}
             for note in notes if note["kind"] == "question" and note["resolved_at"] is None
             for question, options in [parse(note["body"])]]
    return {"open": asked, "answered": sum(1 for note in notes if note["kind"] == "decision" and RULING.match(note["body"]))}


def _gate_state(piece) -> str:
    """A piece's loudest gate lamp, as writing.js `gates` lights them: a verdict not pass is warning; an unstamped
    research file, an unresolved finding or another gate problem with its file present is caution."""
    states = []
    if any("research.md" in problem for problem in piece["problems"]) and piece["files"].get("research.md"):
        states.append("caution")
    for name in ("fact-check", "adversarial"):
        found = [problem for problem in piece["problems"] if name in problem]
        if not found or not piece["files"].get(f"{name}.md"):
            continue
        if any("verdict is not pass" in problem for problem in found):
            states.append("warning")
        elif any("unresolved" in problem for problem in found):
            states.append("caution")
        elif not any("no explicit current gate record" in problem for problem in found):
            states.append("caution")
    return "warning" if "warning" in states else "caution" if states else ""


def _attention(pieces) -> dict:
    """The rail badge (sd:3446): active pieces by their loudest gate lamp."""
    from .v2.pages import attention

    return attention((_gate_state(piece) for piece in pieces if not piece["parked"]),
                     {"warning": "pieces fail a gate", "caution": "pieces need a gate"})
