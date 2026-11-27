"""Routine briefs: the Markdown briefs scheduled vault routines write, as `item` rows (sd:3459).

One brief is one row of kind `report`, `source` `brief`, `external_id` and
`title` its stem, `<YYYY-MM-DD> - <Series>`. It is filed `done` and stays
`done`: the quiet-report rule keeps it off work views, and the Reports
list and its verbs leave it alone (`reporting`). `body` is `{"text": <the
whole file>}`; `fields.brief` holds what a page shows without reading the
text: series, day, when it was first filed, words, a lead, the text's
sha256, and `signal_topics`, the frontmatter's `signal-topics` slugs
(sd:5798). A brief with no such key, or one filed before, has no
`signal_topics`: not recorded, which is no empty list. `sd_db.brief` is the session brief, a different thing.

**The row is the source of truth.** No file replaces a row that exists and
differs. `add` takes one only with a flag: `extend` when the row's text is a
strict prefix of the file (a routine appending its findings), `replace`
whatever the row holds (the operator's flag). `import_folder` adds missing
stems, lists every other difference as a conflict, and takes a file only for
a stem the operator names. No counter or time decides it: a restore rolls
those back with the rows.

**A stem is a file name.** macOS ignores case and Unicode form in one, so
stems, folders and `--replace` names are compared by `records._file_key`.
`add` and `import_folder` refuse a stem another row spells the same way,
naming both; `render` writes neither of two such rows.

`hashed` adds the `mirror-hash` line a vault copy carries, and `unhashed` and
`hash_holds` read it. `render` writes the copies under that rule (sd:4041),
into the folder each owning repository's `channels` setting names.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import channels, records, repos, runner_journal
from .database import refuse_hub_only, transaction
from .records import RecordRefused, _file_key
from .reporting import MAX_REPORT
from .workflow import MissingItem, WorkflowError, _text, item_state
from .writes import (
    add_note,
    create_item,
    item_by_external,
    now,
    record_state,
    set_item_fields,
    unresolved_state,
)

SOURCE = "brief"

#: `<YYYY-MM-DD> - <Series>`: the stem a wikilink names a brief by.
STEM = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}) - (\S(?:.*\S)?)")

#: How much of the body, whitespace folded, a row keeps to show without its text.
LEAD = 200

#: What a conflict says about the vault copy, by `hash_holds`.
COPY = {True: "its mirror-hash matches: a copy the render wrote",
        False: "its mirror-hash does not match: a hand edit",
        None: "no mirror-hash: a hand edit or an unrendered note"}


class BriefRefused(WorkflowError):
    """A brief verb refused; nothing was written."""


class BriefUnusable(BriefRefused):
    """The file or stem is not a brief: refused before any write (`sd` exits 2)."""


class BriefConflict(BriefRefused):
    """The stem's row holds other text, and nothing allowed the file to replace it."""


class BriefCollision(BriefRefused):
    """Another row's stem names the same file on macOS (`records._file_key`); nothing was written."""


#: What starts the line a render adds as the last frontmatter line.
HASH_LINE = "mirror-hash:"

#: Room for the `mirror-hash` line a render adds: a copy of a row at `MAX_REPORT` still imports.
HASH_ROOM = len(f"{HASH_LINE} {'0' * 64}\n")


def hashed(content: str) -> str:
    """`content` with `mirror-hash: <sha256 of content>` as its last frontmatter line: the copy's mark.

    `unhashed` of the result is `content` again.
    """
    lines = content.split("\n")
    end = _closing(lines)
    if end is None:
        raise BriefRefused("it opens no frontmatter, so there is no place for its mirror-hash")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return "\n".join([*lines[:end], f"{HASH_LINE} {digest}", *lines[end:]])


def unhashed(text: str) -> tuple[str, str | None]:
    """`text` without its frontmatter's `mirror-hash` line, and that line's value; `(text, None)` with none."""
    lines = text.split("\n")
    for index in range(1, _closing(lines) or 1):
        if lines[index].startswith(HASH_LINE):
            return "\n".join(lines[:index] + lines[index + 1:]), lines[index][len(HASH_LINE):].strip()
    return text, None


def hash_holds(text: str) -> bool | None:
    """Whether a file's `mirror-hash` is its own content's: True for a copy the render wrote, None with no hash.

    False is a file changed since it was written: a hand edit, which no render overwrites.
    """
    rest, value = unhashed(text)
    return None if value is None else hashlib.sha256(rest.encode("utf-8")).hexdigest() == value


def _closing(lines: list[str]) -> int | None:
    """The index of the line that closes the frontmatter, or None when the text opens none."""
    if not lines or lines[0] != "---":
        return None
    return next((index for index in range(1, len(lines)) if lines[index] == "---"), None)


def read_draft(path: Path, *, room: int = 0) -> tuple[str, str]:
    """`(stem, text)` of a draft or note, or `BriefUnusable` naming the rule it breaks.

    `room` is the bytes allowed over `MAX_REPORT`: an import reads a copy
    with its hash line, and `counts` holds the text without it to the limit.
    """
    path = Path(path)
    if path.suffix != ".md":
        raise BriefUnusable(f"{path.name}: the file name must be `<YYYY-MM-DD> - <Series>.md`")
    with path.open("rb") as stream:
        data = stream.read(MAX_REPORT + room + 1)
    if len(data) > MAX_REPORT + room:
        raise BriefUnusable(f"{path.name}: over {MAX_REPORT} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BriefUnusable(f"{path.name}: not UTF-8 ({error.reason} at byte {error.start})") from None
    counts(path.stem, unhashed(text)[0] if room else text)
    return path.stem, text


def counts(stem: str, text: str) -> dict[str, Any]:
    """The row's `fields.brief` but `at`, or `BriefUnusable`: a stem, a day that exists, frontmatter, a size."""
    found = STEM.fullmatch(stem) if isinstance(stem, str) else None
    try:
        day = datetime.date.fromisoformat(found[1]).isoformat() if found else None
    except ValueError:
        day = None
    if found is None or day is None or "/" in stem or "\\" in stem:
        raise BriefUnusable(f"{stem}: the file name must be `<YYYY-MM-DD> - <Series>.md`, with a day that exists "
                            f"and no / or \\ in the series")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_REPORT:
        raise BriefUnusable(f"{stem}: the text must be at most {MAX_REPORT} bytes")
    lines = text.split("\n")
    end = _closing(lines)
    if end is None:
        raise BriefUnusable(f"{stem}: the text must open with frontmatter between two `---` lines")
    body = " ".join("\n".join(lines[end + 1:]).split())
    topics = signal_topics(lines[1:end])
    return {"series": found[2], "day": day, "words": len(body.split()), "lead": body[:LEAD],
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            **({} if topics is None else {"signal_topics": topics})}


#: A frontmatter `signal-topics:` line, and the value after it.
SIGNAL_TOPICS = re.compile(r"signal-topics:(.*)")


def signal_topics(frontmatter: list[str]) -> list[str] | None:
    """The `signal-topics` slugs of `frontmatter`'s lines, in order, once each; None with no such key.

    The routines write a block list (`  - <slug>` lines). A flow list
    (`[a, b]`), one bare slug and an empty value are read too; a YAML
    comment, a blank line and quotes are dropped.
    """
    for index, line in enumerate(frontmatter):
        found = SIGNAL_TOPICS.fullmatch(line)
        if found is None:
            continue
        value = _uncommented(found[1])
        if value.startswith("[") and value.endswith("]"):
            items = value[1:-1].split(",")
        elif value:
            items = [value]
        else:
            items = []
            for item in frontmatter[index + 1:]:
                if not _uncommented(item):
                    continue  # a blank or comment line sits inside a YAML list
                listed = re.fullmatch(r"\s+- (.*)|- (.*)", item)
                if listed is None:
                    break
                items.append(listed[1] if listed[1] is not None else listed[2])
        slugs = (_uncommented(item).strip("'\"") for item in items)
        return list(dict.fromkeys(slug for slug in slugs if slug))
    return None


def _uncommented(value: str) -> str:
    """`value` without a YAML comment (` #` on) and surrounding spaces."""
    return re.sub(r"(^|\s)#.*", "", value).strip()


def add(connection: sqlite3.Connection, stem: str, text: str, *, repo: str, who: str,
        mode: str | None = None, at: str | None = None) -> dict[str, Any]:
    """File one brief: `filed`, `unchanged`, `extended` or `replaced`, else `BriefConflict`.

    `repo` is the registered checkout that owns it, for a new row. `mode` is
    None, `extend` or `replace`. `at` is when it was first filed, now unless
    an import gives the note's time. A row's `at` never changes. Either
    replacing mode writes `body`, `fields` and one `comment` note in one
    transaction.
    """
    who = _text(who, "who")
    if mode not in (None, "extend", "replace"):
        raise BriefRefused(f"mode must be extend, replace or nothing, not {mode!r}")
    brief = counts(stem, text)
    with transaction(connection):
        row = item_by_external(connection, SOURCE, stem)
        if row is None:
            _refuse_collision(connection, stem)
            owner = repos.row_for(connection, repo)
            if owner is None:
                raise BriefRefused(f"{repo} is not a registered repository; register it with `sd-db repo add`")
            stamp = at or now()
            item = create_item(connection, kind="report", title=stem, status="done", repo=owner["path"],
                               source=SOURCE, external_id=stem, created_at=stamp, session=who,
                               fields={"attention": False, "brief": {**brief, "at": stamp}}, body={"text": text})
            result = "filed"
        else:
            item = row["id"]
            held = json.loads(row["body"] or "{}").get("text")
            fields = json.loads(row["fields"] or "{}")
            old = fields.get("brief") or {}
            if held == text:
                result = "unchanged"
            elif mode is None:
                raise BriefConflict(f"{stem}: the row holds other text; show the brief to read it, and only a "
                                    f"replace takes the file over it")
            elif mode == "extend" and not (isinstance(held, str) and text.startswith(held)):
                raise BriefConflict(f"{stem}: the draft is not the row's text with more after it, so --extend "
                                    f"leaves the row; show the brief to read it")
            else:
                result = "extended" if mode == "extend" else "replaced"
                fields["brief"] = {**brief, "at": old.get("at") or now()}
                set_item_fields(connection, item, fields=fields, body={"text": text})
                add_note(connection, item, "comment",
                         f"Brief replaced by {who}: {old.get('words', '?')} -> {brief['words']} words", session=who)
    return _answer(connection, item, result)


def _refuse_collision(connection: sqlite3.Connection, stem: str) -> None:
    """A stem is one vault file name: macOS ignores case and Unicode form, so a second spelling is refused."""
    key = _file_key(stem)
    for row in connection.execute("SELECT id, external_id FROM item WHERE source = ?", (SOURCE,)):
        if _file_key(row["external_id"] or "") == key:
            raise BriefCollision(f"{stem}: brief {row['external_id']} (item {row['id']}) has the same file name "
                                 f"on macOS, which ignores case and Unicode form; file it under that stem")


def _answer(connection: sqlite3.Connection, item: int, result: str) -> dict[str, Any]:
    """What `add` reports, read after its commit."""
    row = item_state(connection, item)["item"]
    return {"result": result, "item": item, "stem": row["title"], **json.loads(row["fields"])["brief"]}


#: How many briefs `sd reports brief list` prints; past it the verb says more exist (sd:10638).
LIST_LIMIT = 200


def rows(connection: sqlite3.Connection, *, series: str | None = None, since: str | None = None,
         limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    """Briefs newest first, by day then first filing, without their text: what a list or a page shows."""
    query = ("SELECT id, title, repo, created_at, updated_at, fields FROM item WHERE source = ? AND kind = 'report'")
    values: list[Any] = [SOURCE]
    if series is not None:
        query += " AND json_extract(fields, '$.brief.series') = ?"
        values.append(series)
    if since is not None:
        query += " AND json_extract(fields, '$.brief.day') >= ?"
        values.append(since)
    query += (" ORDER BY json_extract(fields, '$.brief.day') DESC, json_extract(fields, '$.brief.at') DESC, id DESC"
              " LIMIT ?")
    values.append(max(1, int(limit)))
    found = []
    for row in connection.execute(query, values):
        out = dict(row)
        out["brief"] = json.loads(out.pop("fields") or "{}").get("brief") or {}
        found.append(out)
    return found


def _held(connection: sqlite3.Connection, stem: str) -> str | None:
    """The sha256 the stem's row holds, read back; None when it has no row."""
    row = item_by_external(connection, SOURCE, stem)
    return None if row is None else json.loads(row["fields"] or "{}").get("brief", {}).get("sha256")


def show(connection: sqlite3.Connection, stem: str) -> str:
    """The stored Markdown of one brief."""
    row = item_by_external(connection, SOURCE, stem)
    if row is None:
        raise MissingItem(f"no brief has the stem {stem!r}")
    return str(json.loads(row["body"] or "{}").get("text", ""))


def import_folder(connection: sqlite3.Connection, folder: Path, *, repo: str, who: str,
                  replace: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    """File each `*.md` in `folder`, each in its own transaction, and read them back.

    The `mirror-hash` line a render added is stripped first, so a copy that
    equals its row is `unchanged`. A missing stem is filed, with the note's
    modification time as `at`. A stem whose row differs is a conflict, kept
    as the row holds it and listed with what its copy is, unless it is named
    in `replace`. A file that is not a brief is refused by name and left.
    `verified` is the count read back with each row's sha256 the note's, or
    None when any conflict or mismatch is left. Renders nothing.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise BriefUnusable(f"{folder} is not a folder")
    notes = sorted(folder.glob("*.md"))
    stems = {_file_key(path.stem) for path in notes}
    wanted = {_file_key(stem) for stem in replace}
    unknown = sorted(stem for stem in replace if _file_key(stem) not in stems)
    if unknown:
        raise BriefUnusable(f"--replace names {', '.join(unknown)}, which has no note in {folder}; "
                            f"name each stem as its note is called")
    result: dict[str, Any] = {"filed": [], "unchanged": 0, "replaced": [], "conflicts": [], "refused": [],
                              "verified": None}
    taken: dict[str, str] = {}
    for path in notes:
        try:
            stem, held = read_draft(path, room=HASH_ROOM)
            text, _ = unhashed(held)
        except BriefUnusable as refusal:
            result["refused"].append({"name": path.name, "reason": str(refusal)})
            continue
        stamp = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.UTC).isoformat(timespec="seconds")
        try:
            done = add(connection, stem, text, repo=repo, who=who, at=stamp,
                       mode="replace" if _file_key(stem) in wanted else None)
        except BriefCollision as refusal:
            result["refused"].append({"name": path.name, "reason": str(refusal)})
            continue
        except BriefConflict:
            result["conflicts"].append({"stem": stem, "copy": COPY[hash_holds(held)]})
            continue
        if done["result"] == "filed":
            result["filed"].append(stem)
        elif done["result"] == "replaced":
            result["replaced"].append(stem)
        else:
            result["unchanged"] += 1
        taken[stem] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    matched = sum(1 for stem, digest in taken.items() if _held(connection, stem) == digest)
    if not result["conflicts"] and matched == len(taken):
        result["verified"] = matched
    return result


# ---------------------------------------------------------------- vault copies

#: The `heartbeat` state row each render records:
#: `{at, ok, state, folder, previous_folder, written, refused, reason}`.
COPIES_KEY = "brief-copies"

#: The lock file beside the database that serializes renders.
LOCK_NAME = "brief-copies.lock"

#: Seconds a render waits for another to finish before it fails.
LOCK_WAIT = 60.0

#: The most refused stems a heartbeat lists.
REFUSED_SHOWN = 50

#: The obsidian channel's default folder under the vault, then the repository's name: as for a research document.
DEFAULT_FOLDER = "Briefs"


def render(connection: sqlite3.Connection, vault: Path | str | None) -> dict[str, Any]:
    """Write each brief row's vault copy, `<vault>/<folder>/<stem>.md`, and record a `brief-copies` heartbeat.

    The copy is `body.text` with `hashed`'s `mirror-hash` line. A file
    is written only when its bytes differ, by temporary file and rename, and
    overwritten only when its own `mirror-hash` holds, or its text without
    that line is the row's: else it is a hand edit, kept and refused. A
    stem-shaped `.md` no row holds is refused and kept too. `folder` maps
    each owning repository to the folder its `channels` setting names, None
    when its obsidian channel is off. A folder that moved is kept as
    `previous_folder` while copies of rows are left there; none is deleted.

    Refuses with `HubOnly` off the hub, before it reads or records anything.
    Every other failure ends in the returned body and its heartbeat. Rows are
    read only once the lock beside the database is held, so a render that
    read older rows never writes after one that read newer rows.
    """
    copies: dict[str, Any] = {"state": "failed", "folder": {}, "previous_folder": {}, "written": [],
                              "refused": [], "left": [], "reason": None}
    try:
        with runner_journal.lock(_lock_path(connection), blocking=False, wait=LOCK_WAIT, error=BriefRefused,
                                 noun="brief render", held="another brief render held the lock for "
                                                           f"{LOCK_WAIT:.0f} seconds"):
            try:
                _copy(connection, vault, copies)
            except Exception as error:  # the heartbeat names it; an unknown is not a pass
                copies["reason"] = str(error) or type(error).__name__
            return _beat(connection, copies)
    except BriefRefused as error:
        copies["reason"] = str(error)
    return _beat(connection, copies)


def copies_state(connection: sqlite3.Connection) -> tuple[str, dict[str, Any]] | None:
    """The last render's heartbeat as `(timestamp, body)`, or None when none ran."""
    row = connection.execute("SELECT timestamp, body FROM state WHERE kind = 'heartbeat' AND key = ? "
                             "ORDER BY id DESC LIMIT 1", (COPIES_KEY,)).fetchone()
    return None if row is None else (row["timestamp"], json.loads(row["body"] or "{}"))


def _lock_path(connection: sqlite3.Connection) -> Path:
    """The lock beside the database: a hub-only path, so a satellite meets `HubOnly` here, before any read."""
    refuse_hub_only(connection, "brief render")
    found = connection.execute("PRAGMA database_list").fetchone()
    if found is None or not found[2]:
        raise BriefRefused("the database has no file, so there is no place for the render's lock")
    return Path(found[2]).parent / LOCK_NAME


def _beat(connection: sqlite3.Connection, copies: dict[str, Any]) -> dict[str, Any]:
    if copies["reason"] is None and not copies["refused"]:
        copies["state"] = "ok" if any(copies["folder"].values()) or not copies["folder"] else "off"
    else:
        copies["state"] = "failed"
    copies["ok"] = copies["state"] != "failed"
    record_state(connection, "heartbeat", key=COPIES_KEY, body={
        "at": now(), "ok": copies["ok"], "state": copies["state"], "folder": copies["folder"],
        "previous_folder": copies["previous_folder"], "written": len(copies["written"]),
        "refused": [stem for stem, _ in copies["refused"]][:REFUSED_SHOWN], "reason": copies["reason"]})
    return copies


def _copy(connection: sqlite3.Connection, vault: Path | str | None, copies: dict[str, Any]) -> None:
    """Every refusal of the whole render first: a restore, no vault, a setting it cannot read. Then each file."""
    if unresolved_state(connection, "restore"):
        raise BriefRefused("a restore is unresolved, so the rows may be older than the copies; "
                           "import the folder, then resume the restore")
    briefs = [dict(row) for row in connection.execute(
        "SELECT repo, title, body FROM item WHERE source = ? AND kind = 'report' ORDER BY id", (SOURCE,))]
    if not briefs:
        return
    if not vault:
        raise BriefRefused("OBSIDIAN_VAULT is not set, so there is no vault to write copies into")
    root = Path(vault).expanduser()
    if not root.is_dir():
        raise BriefRefused(f"{root} is not a directory; the render creates no vault")
    last = copies_state(connection)
    before = last[1] if last is not None else {}
    folders = {repo: _folder(connection, repo) for repo in sorted({row["repo"] for row in briefs})}
    copies["folder"] = folders
    for named in filter(None, folders.values()):
        if not (root / named).resolve().is_relative_to(root.resolve()):
            raise BriefRefused(f"{root / named} leads outside the vault, through a link; the render writes "
                               f"copies inside {root} only")
    targets = [(row, folder, f"{_place(root, folder)}/{_file_key(row['title'])}") for row in briefs
               if (folder := folders[row["repo"]]) is not None]
    places: dict[str, list[str]] = {}  # one vault file, as macOS names it: the stems that would write it
    for row, _, key in targets:
        places.setdefault(key, []).append(row["title"])
    for row, folder, key in targets:
        others = [stem for stem in places[key] if stem != row["title"]]
        if others:
            copies["refused"].append((row["title"], f"its file is also the file of {', '.join(others)} on macOS, "
                                                    f"which ignores case and Unicode form; none of them is written"))
        else:
            _write(root / folder, row, copies)
    held: dict[str, tuple[Path, set[str]]] = {}  # one folder, as macOS names it: the stems its rows hold
    for repo, folder in folders.items():
        if folder is not None:
            stems = {row["title"] for row in briefs if row["repo"] == repo}
            held.setdefault(_place(root, folder), (root / folder, set()))[1].update(map(_file_key, stems))
            _moved(root, repo, folder, stems, before, copies)
    for path, keys in held.values():
        _strays(path, keys, copies)


def _place(root: Path, folder: str) -> str:
    """A folder under the vault as macOS names it: two spellings of one directory are one place."""
    return _file_key((root / folder).resolve().as_posix())


def _folder(connection: sqlite3.Connection, repo: str) -> str | None:
    """The folder under the vault the repository's obsidian channel names, None when that channel is off.

    A setting it cannot read raises with the cause: unknown is not "no setting".
    """
    setting = channels.outbound(repos.repo_channels(connection, repo))
    if setting is None:
        return f"{DEFAULT_FOLDER}/{Path(repo).name}"
    if "obsidian" not in setting:
        return None
    return setting["obsidian"].get("folder") or f"{DEFAULT_FOLDER}/{Path(repo).name}"


def _write(folder: Path, row: dict[str, Any], copies: dict[str, Any]) -> None:
    """Write one copy, unless the file there is a hand edit."""
    stem = row["title"]
    try:
        text = json.loads(row["body"] or "{}").get("text")
        counts(stem, text)
        target = folder / f"{stem}.md"
        held: bytes | None = None
        with contextlib.suppress(FileNotFoundError):
            held = target.read_bytes()
        if held is not None:
            _refuse_hand_edit(held, text)
        if records._replace(target, hashed(text).encode("utf-8"), held=held):
            copies["written"].append(stem)
    except (BriefRefused, RecordRefused) as refusal:
        copies["refused"].append((stem, str(refusal)))


def _refuse_hand_edit(held: bytes, text: str) -> None:
    """Refuse a file an overwrite would lose text from: one whose hash fails and whose text is not the row's.

    A file whose text without its `mirror-hash` line is the row's loses
    nothing, whatever its hash says: an import with `--replace` took a hand
    edit into the row and left the file's stale hash (sd:7488).
    """
    try:
        found = held.decode("utf-8")
    except UnicodeDecodeError:
        raise BriefRefused("it is not UTF-8: a hand edit, kept") from None
    if unhashed(found)[0] == text:
        return
    holds = hash_holds(found)
    if holds is False:
        raise BriefRefused("it differs from its mirror-hash: a hand edit, kept")
    if holds is None:
        raise BriefRefused("it has no mirror-hash and differs from its row: a hand edit or a note changed "
                           "since the import, kept; an import of the folder lists it")


def _strays(folder: Path, keys: set[str], copies: dict[str, Any]) -> None:
    """Report every brief-shaped note in the folder no row holds, by `_file_key`. Other notes there are left alone."""
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.md")):
        if _file_key(path.stem) not in keys and STEM.fullmatch(path.stem):
            copies["refused"].append((path.stem, "no row holds this file; kept: file it with "
                                                 "`sd reports brief add` or delete it"))


def _moved(root: Path, repo: str, folder: str, stems: set[str], before: dict[str, Any],
           copies: dict[str, Any]) -> None:
    """Name the copies left in the folder the last render wrote, when the folder moved; delete none."""
    last = (before.get("folder") or {}).get(repo) if isinstance(before.get("folder"), dict) else None
    previous = last if isinstance(last, str) and last and _place(root, last) != _place(root, folder) else \
        ((before.get("previous_folder") or {}) if isinstance(before.get("previous_folder"), dict) else {}).get(repo)
    if not isinstance(previous, str) or not previous or _place(root, previous) == _place(root, folder):
        return
    left = sorted(stem for stem in stems if (root / previous / f"{stem}.md").is_file())
    if left:
        copies["previous_folder"][repo] = previous
        copies["left"].extend(f"{previous}/{stem}.md" for stem in left)
