"""Books and Videos: the rows behind the Documents page's Books and Videos views (sd:6260, sd:6261).

The page is `v2/documents.html`; it holds no rows. Each view reads one JSON document, `/api/documents/books` and
`/api/documents/videos`, built here from the vault's learning base rows (`records.list_rows(connection, "learning")`,
the `sdw.learning` kind). Nothing here writes: a row's stage and kind stay as the store holds them.

**Which rows.** A row moved to a view (`fields.document-type`, pack sd:6262) is in that view and no other, whatever
its content type: `sd-db records documents` moves it, and it then leaves Tasks and Today. Of the rest, Books are the
rows whose `content-type` is `book`, `article` or `course` (sd:10634: a course had no view). Videos are the rows whose
`content-type` is `youtube` or `video`, and any learning row titled `YYYY-MM-DD - YouTube - <channel> - <title>`, the
name the vault's video notes carry. Other learning rows are in neither.

**What one row says.** A book: its title, its author or source (the `author` list, else the `publisher`, else the
URL's host), its URL and its read state. A video: its title less the dated prefix, its channel (the `channel` field,
else the title's), its date (`published-date`, else the title's), its URL and its watched state. The state is the
row's stage in the page's words (`READ`, `WATCHED`); a stage the map does not name is shown as it is. A URL that is not
`http` or `https` is no link: `url` is empty and `url_text` keeps what the row holds, so the page shows it as text.

**Categories (pack sd:7669).** Each view has a small fixed set, `CATEGORIES`, chosen from the vault's rows; each
document says its own in the kind's declared `category` field, which `sd store set sdw.learning TITLE category X`
writes. The document sends the set and each row's `category` as text, one off the set too, for Details and search.
Each row's `filed` (pack sd:7695) is the tile it counts under: the first of its values the set names, compared
case-insensitively and spelled as the set spells it, else "", which the page files under Other. A list such as
`["Computers", "Communication"]` files under Communication; as joined text it named no category.

A store that does not read raises, so the route answers 500 with the cause and the page says the rows were not read.
"""

from __future__ import annotations

import urllib.parse

from sd_db.reads import document_view
from sd_db.records import VIDEO_TITLE

__all__ = ["BOOK_KINDS", "CATEGORIES", "READ", "WATCHED", "books", "videos"]

#: The learning stages in a book's words: to read, reading, done.
READ = {"queued": "to read", "in-progress": "reading", "done": "done", "dropped": "dropped"}
#: The learning stages in a video's words.
WATCHED = {"queued": "to watch", "in-progress": "watching", "done": "watched", "dropped": "dropped"}

#: The content types the Books view lists; the page offers each as a Kind tile, so a course is one press away.
BOOK_KINDS = ("book", "article", "course")

#: Each view's categories, in the order its tiles show (pack sd:7669).
CATEGORIES = {
    "books": ("Coaching & Consulting", "Communication", "Teams & Facilitation", "Learning"),
    "videos": ("Coding", "AI Agents & Tools", "AI & Society", "Philosophy & Religion", "Personal Growth"),
}


def _rows(connection):
    from sd_db import records

    return records.list_rows(connection, "learning")


def _in_view(row, view: str, matched: bool) -> bool:
    """A moved row is in its own view only; any other row is where `matched`, its content type or title, puts it."""
    held = document_view(row["fields"])
    return held == view if held else matched


def _text(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def _filed(value, names) -> str:
    """The first of `value`'s values that `names` holds, as `names` spells it, else "": a field may hold any JSON."""
    named = {name.casefold(): name for name in names}
    for one in value if isinstance(value, list) else [value]:
        if isinstance(one, str) and one.strip().casefold() in named:
            return named[one.strip().casefold()]
    return ""


def _url(value) -> tuple[str, str]:
    """The link and its host for an http(s) URL; else no link, so the page draws the text and never a javascript: href.
    A URL urlsplit refuses (`https://[broken`, a full-width colon) is no link either (sd:6280): one row never fails the read."""
    text = _text(value)
    try:
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname or ""
    except ValueError:
        return "", ""
    return (text, host) if parts.scheme in ("http", "https") and host else ("", "")


def _base(row, words, names) -> dict:
    fields = row["fields"]
    url, host = _url(fields.get("url"))
    stage = row.get("stage") or ""
    return {"id": row["id"], "title": row["title"], "stage": stage, "state": words.get(stage, stage),
            "url": url, "host": host, "url_text": "" if url else _text(fields.get("url")),
            "description": _text(fields.get("description")), "added": _text(fields.get("dateCreated"))[:10],
            "category": _text(fields.get("category")), "filed": _filed(fields.get("category"), names)}


def books(connection, *, now: str) -> dict:
    """`/api/documents/books`: the book, article and course rows, in the store's order; the page sorts them."""
    out = []
    for row in _rows(connection):
        fields = row["fields"]
        kind = _text(fields.get("content-type")).lower()
        if not _in_view(row, "books", kind in BOOK_KINDS):
            continue
        item = _base(row, READ, CATEGORIES["books"])
        item.update(kind=kind, by=_text(fields.get("author")) or _text(fields.get("publisher")) or item["host"],
                    subtitle=_text(fields.get("subtitle")), published=_text(fields.get("published-date")),
                    pages=_text(fields.get("total-pages")), isbn=_text(fields.get("isbn")))
        out.append(item)
    return {"read": now, "categories": list(CATEGORIES["books"]), "kinds": list(BOOK_KINDS), "rows": out}


def videos(connection, *, now: str) -> dict:
    """`/api/documents/videos`: the video rows, in the store's order; the page sorts them."""
    out = []
    for row in _rows(connection):
        fields = row["fields"]
        named = VIDEO_TITLE.match(row["title"] or "")
        if not _in_view(row, "videos", _text(fields.get("content-type")).lower() in ("youtube", "video") or bool(named)):
            continue
        item = _base(row, WATCHED, CATEGORIES["videos"])
        item.update(title=named.group(3) if named else row["title"],
                    channel=_text(fields.get("channel")) or (named.group(2) if named else ""),
                    date=_text(fields.get("published-date"))[:10] or (named.group(1) if named else ""),
                    duration=_text(fields.get("duration")))
        out.append(item)
    return {"read": now, "categories": list(CATEGORIES["videos"]), "rows": out}
