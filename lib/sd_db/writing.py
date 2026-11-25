"""The shared writing lifecycle, its evidence, and explicit file cutover.

The library reads registered piece paths. UI and CLI pass stable item IDs and
intent; neither reimplements the ladder or decides whether a gate is current.
Reports remain files, while structured gate records bind their verdicts to
the exact draft and research they reviewed. No verdict is guessed from prose.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import paths as sdpaths
from .database import refuse_hub_only, transaction
from .progress import _fields, _git, small_int
from .sources.frontmatter import split as split_frontmatter
from .workflow import StaleItem, WorkflowError, _checked_state, _text, item_state
from .writes import (
    PROMOTED_COLUMNS,
    _transition,
    add_note,
    create_item,
    now,
    record_state,
    resolve_state,
    set_item_fields,
    upsert_repo,
)
from .yaml_lite import _Flow

STAGES = ("inbox", "accepted", "researching", "drafting", "review", "ready", "published", "declined")
STAGE_STATUS = {"inbox": "planning", "accepted": "ready", "researching": "in_progress",
                "drafting": "in_progress", "review": "in_progress", "ready": "ready_to_send",
                "published": "done", "declined": "done"}
FILE_STAGES = {"idea": "accepted", **{key: key for key in STAGES[2:7]}}
MARKER = "content/.status-source"
REPORTS = {"research": "research.md", "fact-check": "fact-check.md", "adversarial": "adversarial.md"}
STAMP = re.compile(r"<!-- reconciled-with-draft: ([0-9a-f]{12})\b(.*?)-->")
SOURCE = "writing-piece"


def _manual_destinations():
    """Publication targets whose URL the operator records by hand.

    `SD_WRITING_DESTINATIONS` adds names, comma-separated, such as an
    employer's blog; it is read at call time so a test can set it.
    """
    extra = os.environ.get("SD_WRITING_DESTINATIONS", "")
    return {"blog", "substack"} | {name.strip() for name in extra.split(",") if name.strip()}


def _canonical(connection: sqlite3.Connection, repo: str) -> str:
    """The form the `repo` row holds for this path, else its key (sd:1439).

    Identities, receipt keys and journals are built from it, so a caller
    that passes the disk path and one that passes the key land on one row.
    """
    from .repos import row_for
    row = row_for(connection, repo)
    return str(row["path"]) if row is not None else (sdpaths.key(repo) or repo)


def pieces_owner(connection: sqlite3.Connection, repo: str) -> str:
    from .repos import row_for
    if not repo:
        raise WorkflowError("this piece names no repository")
    row = row_for(connection, repo)
    if row is None:
        raise WorkflowError(f"repository {repo!r} is not registered")
    return row["pieces_source"]


def _key(piece: str) -> str:
    if not isinstance(piece, str) or re.fullmatch(r"[0-9]{4}/[A-Za-z0-9][A-Za-z0-9._-]*", piece) is None:
        raise WorkflowError("piece must be YEAR/SLUG without path traversal")
    return piece


# (registered root, worktree root), set by `checkout` (sd:2024). Only the
# repository it was validated for is redirected; every other one keeps its path.
_CHECKOUT: ContextVar[tuple[Path, Path] | None] = ContextVar("writing_checkout", default=None)


def _common_dir(root: Path) -> Path:
    return (root / _git(root, "rev-parse", "--git-common-dir")).resolve()


@contextmanager
def checkout(repo: str, root: Path | str):
    """Read and write piece files in `root`, a worktree of `repo` (sd:2024).

    Rows stay keyed to the registered repository, so a writer in its own
    worktree records gates against the prose it changed there. `root` must
    share the registered checkout's Git directory; any other path is refused.
    Another repository's rows keep reading their own registered checkout.
    Registration, cutover and recovery journal the registered checkout, so
    they refuse to run inside it.
    """
    registered = sdpaths.expand(repo).resolve()
    root = Path(root).resolve()
    if root != registered:
        try:
            same = _common_dir(root) == _common_dir(registered)
        except WorkflowError:
            same = False
        if not same:
            raise WorkflowError(f"{root} is not a worktree of the registered repository {repo}")
    token = _CHECKOUT.set((registered, root))
    try:
        yield root
    finally:
        _CHECKOUT.reset(token)


def _disk(repo: str) -> Path:
    """Where `repo`'s piece files live: its active `checkout`, else its registered path."""
    registered = sdpaths.expand(repo).resolve()
    active = _CHECKOUT.get()
    return active[1] if active is not None and active[0] == registered else registered


def _registered_only(repo: str) -> None:
    """Journals name the registered checkout, so their writers run only there."""
    if _disk(repo) != sdpaths.expand(repo).resolve():
        raise WorkflowError("writing registration, cutover and recovery run only in the registered checkout, not a worktree")


def _path(repo: str, piece: str, relative: str | None = None) -> Path:
    piece = _key(piece)
    if not repo:
        raise WorkflowError("this piece names no repository")
    root = _disk(repo)
    relative = relative or f"content/{piece}/index.md"
    wanted = {f"content/{piece}/index.md", f"content-parked/{piece}/index.md"}
    if relative not in wanted:
        raise WorkflowError("piece path must name its index.md under content or content-parked")
    target = root / relative
    if not target.resolve().is_relative_to(root) or not target.is_file():
        raise WorkflowError(f"piece file is unavailable inside its registered repository: {relative}")
    return target


def _scalar(value: str):
    value = _uncomment(value).strip()
    if value in ("", "null", "~"):
        return None
    if value[:1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            pass
    if len(value) > 1 and value[0] == value[-1] == "'":
        return value[1:-1]
    if value.startswith(("[", "{")):
        flow = _Flow(value, 1)
        parsed = flow.value()
        flow.skip()
        if flow.at != len(value):
            raise WorkflowError("unparsed text after frontmatter collection")
        return parsed
    return value


def _uncomment(value: str) -> str:
    quote, escaped = None, False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def _metadata(text: str) -> dict:
    block, _ = split_frontmatter(text)
    if not block:
        raise WorkflowError("piece has no frontmatter block")
    result = {}
    parent = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and parent:
            if result.get(parent) is None:
                result[parent] = []
            if not isinstance(result[parent], list):
                raise WorkflowError(f"unsupported frontmatter sequence for {parent}")
            result[parent].append(_scalar(line[4:]))
        elif line.startswith("  ") and parent and ":" in line:
            key, _, value = line.strip().partition(":")
            if result.get(parent) is None:
                result[parent] = {}
            if not isinstance(result[parent], dict):
                raise WorkflowError(f"unsupported frontmatter mapping for {parent}")
            result[parent][key] = _scalar(value)
        elif line[:1].isspace() or ":" not in line:
            raise WorkflowError(f"unsupported frontmatter line: {line}")
        else:
            parent, _, value = line.partition(":")
            if parent in result:
                raise WorkflowError(f"duplicate frontmatter field {parent}")
            result[parent] = _scalar(value)
    return result


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _carry_index_hash(fields: dict, before: bytes, after: bytes) -> None:
    """The pack's own frontmatter rewrite moves index.md bytes: a gate bound to the old bytes moves with them (sd:10719).

    A gate bound to other bytes, a hand edit after it, stays stale, so a render still refuses it.
    """
    gates = fields.get("writing_gates")
    if not isinstance(gates, dict):
        return
    old, new = _hash(before), _hash(after)
    for gate in gates.values():
        if isinstance(gate, dict) and gate.get("index_sha256") == old:
            gate["index_sha256"] = new


def _attested(report: str) -> str | None:
    """The index.md bytes a report's stamps attest, as one SHA-256; None when no stamp names any (sd:10719).

    The one reader of `index=`, for the gate and the reconcile alike: a value it cannot read, or a second one on
    any stamp, is refused, never dropped, since a dropped attestation reads as nothing to check.
    """
    found = [value for stamp in STAMP.finditer(report) for value in re.findall(r"\bindex=(\S*)", stamp.group(2))]
    if not found:
        return None
    if len(found) > 1 or re.fullmatch(r"[0-9a-f]{64}", found[0]) is None:
        raise WorkflowError("report's index stamp is not one SHA-256; run the review again")
    return found[0]


def _layout(report: str, artifact: str, index: str) -> str | None:
    """The index.md hash `report` attests, once it holds for the bytes on disk, whose hash is `index` (sd:10719).

    The one rule for every reader that asks whether a report answers this layout, the gate and the companion
    state alike: a reader with a rule of its own passed what the gate refuses. None is a report that never
    attested, which certifies no bytes.
    """
    attested = _attested(report)
    if attested and attested != index:
        raise WorkflowError("index.md bytes changed after the review; review the current layout before recording it")
    # The pack's own run always attests, and marks its report with the run record: one without `index=` lost
    # it, or predates it, and would pass as nothing to check.
    if attested is None and artifact == "adversarial" and _REVIEWED.search(report):
        raise WorkflowError("adversarial report names no index.md bytes; run the review again")
    return attested


def _draft(text: str) -> str | None:
    """The prose after `## Draft`, the part every gate reviews; None without that heading."""
    return text.split("## Draft", 1)[1] if "## Draft" in text else None


def _prose_digest(prose: str) -> str:
    """The draft digest: whitespace-normalized, so only a prose change moves it."""
    return hashlib.sha1(" ".join(prose.split()).encode("utf-8")).hexdigest()[:12]


def _snapshot(row) -> dict:
    path = _path(row["repo"], row["piece"], row["path"])
    data = path.read_bytes()
    text = data.decode("utf-8")
    prose = _draft(text) or ""
    digest = _prose_digest(prose) if prose.strip() else ""
    files = {"index": data}
    for name, filename in REPORTS.items():
        candidate = path.with_name(filename)
        if candidate.exists():
            if not candidate.resolve().is_relative_to(_disk(row["repo"])):
                raise WorkflowError(f"{filename} escapes the registered repository")
            files[name] = candidate.read_bytes()
    return {"path": path, "text": text, "digest": digest, "files": files,
            "hashes": {key: _hash(value) for key, value in files.items()}}


def piece_files(row) -> dict[str, dict | None]:
    """Each piece file's size and change time, keyed by file name; None for one absent or outside the repository.

    A page shows these beside the gate lamps, so it can tell a missing report from a stale one; it reads no file.
    """
    index = _path(row["repo"], row["piece"], row["path"])
    found: dict[str, dict | None] = {}
    for name in ("index.md", *REPORTS.values()):
        candidate = index.with_name(name)
        try:
            inside = candidate.resolve(strict=True).is_relative_to(_disk(row["repo"]))
            stat = candidate.stat()
        except OSError:
            inside = False
        found[name] = {"bytes": stat.st_size, "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")} if inside else None
    return found


#: The files a piece mirrors to Drive (pack sd:7243), by the stem its request is named with: the draft, then each report.
MIRRORED = {"draft": "index.md", **REPORTS}
#: The longest name a piece document takes, in bytes. Its queue file is `<repo key>.<name>.<destination>.json`, and a
#: file name holds 255 bytes, but a slug may run to 240 (`_key`): a longer name keeps its head and a digest of the
#: whole, so the pack's queue and the dashboard's lookup name one file (pack sd:7243 review).
OUT_BYTES = 160
#: The most a file name holds, and the end of a piece's queue file name: the key and the name share the rest, so a
#: 100-byte key and a 240-byte slug no longer make a 272-byte name (pack sd:7565).
NAME_BYTES = 255
QUEUE_SUFFIX = ".drive.json"


def piece_out(piece: str, stem: str, *, key: str) -> str:
    """A piece file's document name, `<year>-<slug>-<stem>`, bounded to OUT_BYTES and so that its queue file,
    `<key>.<name>.drive.json`, fits NAME_BYTES. A longer name keeps its head and a digest of the whole name.

    `key` is the repository's queue key; the pack's `piece_docs` and the dashboard's `_piece_outs` both pass it.
    A key too long to leave room for the digest and the stem gets just those two.
    """
    out = f"{piece.replace('/', '-')}-{stem}"
    room = min(OUT_BYTES, NAME_BYTES - len(key.encode()) - 1 - len(QUEUE_SUFFIX))
    if len(out.encode()) <= room:
        return out
    digest = hashlib.sha256(out.encode()).hexdigest()[:16]
    head = out.encode()[:max(room - len(digest) - len(stem) - 2, 0)].decode(errors="ignore")
    return f"{head}-{digest}-{stem}"


def mirror_texts(row) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Each present piece file's `(name, text)` by `MIRRORED` stem, from the registered checkout, and what was not read.

    Never a linked worktree's (pack sd:7243): a mirror is the main checkout's copy, as a render's is. An absent file is
    left out; one outside the repository or one that cannot be read is named, never read as absent. A missing index.md
    raises, as `piece_files` does.
    """
    token = _CHECKOUT.set(None)
    try:
        index = _path(row["repo"], row["piece"], row["path"])
        root = _disk(row["repo"])
    finally:
        _CHECKOUT.reset(token)
    texts: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    for stem, name in MIRRORED.items():
        candidate = index.with_name(name)
        try:
            if not candidate.resolve(strict=True).is_relative_to(root):
                problems.append(f"{name} is outside the registered repository")
                continue
            texts[stem] = (name, candidate.read_text(encoding="utf-8", errors="replace"))
        except FileNotFoundError:
            continue
        except OSError as error:
            problems.append(f"{name} not read: {error.strerror or error}")
    return texts, problems


def read_report(connection: sqlite3.Connection, item: int, gate: str, limit: int) -> str | None:
    """Up to `limit` + 1 characters of piece `item`'s report for `gate`, a REPORTS key, so a caller can tell a cut; None
    for one absent or outside the repository. A non-piece raises WorkflowError; any other read failure raises OSError:
    neither is a missing file."""
    row = connection.execute("SELECT * FROM item WHERE id = ?", (item,)).fetchone()
    if row is None or row["kind"] != "idea" or row["piece"] is None:
        raise WorkflowError(f"item {item} is not a writing piece")
    candidate = _path(row["repo"], row["piece"], row["path"]).with_name(REPORTS[gate])
    try:
        if not candidate.resolve(strict=True).is_relative_to(_disk(row["repo"])):
            return None
    except FileNotFoundError:
        return None
    with candidate.open(encoding="utf-8", errors="replace") as handle:
        return handle.read(limit + 1)


def piece_for_key(connection: sqlite3.Connection, repo: str, piece: str) -> sqlite3.Row | None:
    probe = sdpaths.keys(repo)
    return connection.execute(
        f"SELECT * FROM item WHERE repo IN ({sdpaths.placeholders(probe)}) AND piece = ?",
        (*probe, _key(piece))).fetchone()


def list_pieces(connection: sqlite3.Connection, repo: str | None = None, *, include_parked: bool = False) -> list[dict]:
    where, values = ["kind = 'idea'", "piece IS NOT NULL"], []
    if repo is not None:
        probe = sdpaths.keys(repo)
        where.append(f"repo IN ({sdpaths.placeholders(probe)})")
        values.extend(probe)
    if not include_parked:
        where.append("parked_at IS NULL")
    return [dict(row) for row in connection.execute(
        f"SELECT * FROM item WHERE {' AND '.join(where)} ORDER BY priority IS NULL, priority, updated_at DESC, id", values,
    )]


def _piece(connection: sqlite3.Connection, item: int, expected: str | None = None) -> dict:
    state = _checked_state(connection, item, expected)
    if state["item"]["kind"] != "idea" or state["item"]["piece"] is None:
        raise WorkflowError(f"item {item} is not a writing piece")
    return state


def _problems(row, snapshot: dict) -> list[str]:
    problems = []
    digest, generation = snapshot["digest"], row["gate_generation"]
    if not digest:
        problems.append("draft is missing or empty")
    research = snapshot["files"].get("research", b"").decode("utf-8")
    matched = list(STAMP.finditer(research))
    if len(matched) != 1 or matched[0].group(1) != digest:
        problems.append("research.md is missing, unstamped or stale against this draft")
    else:
        # int() refuses a string past 4,300 digits with ValueError, and piece_state runs this for every piece on
        # /api/writing: a stamp past 18 digits is no current generation, unconverted (pack sd:7242).
        stamped_generation = re.search(r"\bgen=(\d+)\b", matched[0].group(2))
        digits = stamped_generation.group(1) if stamped_generation else "0"
        if len(digits) > 18 or int(digits) != generation:
            problems.append("research.md belongs to an earlier gate generation")
    gates = _fields(row["fields"], readable=True).get("writing_gates") or {}
    for name in ("fact-check", "adversarial"):
        gate = gates.get(name) if isinstance(gates, dict) else None
        if name not in snapshot["files"]:
            problems.append(f"{name}.md is missing")
        if not isinstance(gate, dict):
            problems.append(f"{name} has no explicit current gate record; record its verdict and findings")
            continue
        if gate.get("draft_digest") != digest or gate.get("generation") != generation:
            problems.append(f"{name} gate is stale against this draft or generation")
        if gate.get("evidence_sha256") != snapshot["hashes"].get(name) or gate.get("research_sha256") != snapshot["hashes"].get("research"):
            problems.append(f"{name} evidence or research changed after its gate record")
        # The digest ignores whitespace; a gate bound to index.md bytes is stale once they move (sd:10719).
        if "index_sha256" in gate and gate["index_sha256"] != snapshot["hashes"].get("index"):
            problems.append(f"{name} index.md changed after its gate record; review the current layout")
        if gate.get("verdict") != "pass":
            problems.append(f"{name} verdict is not pass")
        for finding in gate.get("findings", []):
            if finding.get("disposition") == "open" and (name == "fact-check" or finding.get("confidence") == "CERTAIN"):
                problems.append(f"{name} finding {finding['id']} is unresolved")
    return problems


def _normal(stage: str) -> list[str]:
    return {"inbox": ["accepted"], "accepted": ["researching"], "researching": ["drafting"],
            "drafting": ["review"], "review": ["ready"], "ready": ["review"], "published": [], "declined": []}.get(stage, [])


def piece_state(connection: sqlite3.Connection, item: int) -> dict:
    state = _piece(connection, item)
    row = state["item"]
    # A piece whose repository is gone or never named reads as its problem: the item page must still open.
    try:
        owner, unowned = pieces_owner(connection, row["repo"]), ""
    except WorkflowError as error:
        owner, unowned = "", str(error)
    try:
        snapshot = _snapshot(row)
        problems = _problems(row, snapshot)
        digest = snapshot["digest"]
        document = snapshot["text"]
        companions = {name: _companion(snapshot, name) for name in COMPANIONS}
    except (OSError, UnicodeError, WorkflowError) as error:
        problems, digest = [str(error)], ""
        document = None
        companions = {}
    if unowned:
        problems = [unowned, *problems]
    stages = _normal(row["stage"])
    corrections = [target for target in ("researching", "drafting", "review")
                   if row["stage"] in STAGES and STAGES.index(target) < STAGES.index(row["stage"])]
    if owner in ("retiring", "") or row["parked_at"]:
        stages, corrections = [], []
    state["writing"] = {"stage": row["stage"], "available_stages": stages, "correction_stages": corrections,
                        "owner": owner, "parked": row["parked_at"] is not None,
                        "metadata": _fields(row["fields"], readable=True).get("writing") or {},
                        "document": document,
                        "gates": {"ok": not problems, "digest": digest,
                                  "generation": row["gate_generation"], "problems": problems,
                                  "companions": companions}}
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_claim'").fetchone():
        claims = []
        for claim in connection.execute("SELECT id,active_item,state FROM publication_claim WHERE item=? ORDER BY created_at", (item,)):
            progress = json.loads(claim["state"], parse_int=small_int)
            claims.append({"id": claim["id"], "active": claim["active_item"] is not None,
                           "phase": progress["phase"], "document_id": progress.get("document_id"),
                           "url": progress.get("url"), "current_draft_published": progress.get("current_draft_published"),
                           "reconcile_required": progress.get("reconcile_required", False)})
        state["writing"]["publications"] = claims
    return state


def _rewrite(text: str, changes: dict, *, remove: tuple[str, ...] = ()) -> str:
    head, separator, body = text.partition("\n---\n")
    if not separator or not head.startswith("---\n"):
        raise WorkflowError("piece frontmatter must be fenced")
    for name in remove:
        head = re.sub(rf"(?m)^{re.escape(name)}:.*(?:\n|$)", "", head)
    for name, value in changes.items():
        encoded = "null" if value is None else json.dumps(value, ensure_ascii=False) if isinstance(value, list) or name in ("tip", "title") else str(value)
        replacement = f"{name}: {encoded}"
        if isinstance(value, dict):
            replacement = f"{name}:" + "".join(f"\n  {key}: {json.dumps(held, ensure_ascii=False)}" for key, held in value.items())
        found = re.search(rf"(?m)^{re.escape(name)}:.*(?:\n[ \t]+[^\n]*)*", head)
        if found:
            head = head[:found.start()] + replacement + head[found.end():]
        else:
            head += "\n" + replacement
    return head + separator + body


def _replace(path: Path, data: bytes, *, mode: int | None = None) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.sd-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode if mode is not None else path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _import_piece(connection: sqlite3.Connection, repo: str, piece: str, *, path: str | None = None, who: str) -> dict:
    piece = _key(piece)
    repo = _canonical(connection, repo)
    with transaction(connection):
        owner = pieces_owner(connection, repo)
        if owner == "retiring":
            raise WorkflowError("writing cutover is in progress")
        existing = piece_for_key(connection, repo, piece)
        if existing is not None and owner == "row":
            raise WorkflowError("database owns this piece; importing its former status would overwrite progress")
        source = _path(repo, piece, path)
        text = source.read_text(encoding="utf-8")
        metadata = _metadata(text)
        stage = FILE_STAGES.get(metadata.get("status"))
        if stage is None:
            raise WorkflowError(f"piece {piece} has an unknown file status {metadata.get('status')!r}")
        title = _text(metadata.get("title"), "title")
        fields = _fields(existing["fields"]) if existing else {}
        fields["writing"] = metadata
        disk = _disk(repo)
        relative = source.relative_to(disk).as_posix()
        base_commit, source_commit = None, None
        if (disk / ".git").exists():
            try:
                base_commit = _git(disk, "rev-parse", "HEAD")
                # git show text strips terminal newlines in the shared helper;
                # exact blob identity comes from Git's own hash-object command.
                captured_oid = _git(disk, "hash-object", "--stdin", input=text)
                tree_oid = _git(disk, "rev-parse", f"HEAD:{relative}")
                source_commit = base_commit if captured_oid == tree_oid else None
            except WorkflowError:
                pass
        fields["writing_source"] = {"sha256": _hash(text.encode("utf-8")), "path": relative, "base_commit": base_commit}
        if existing is None:
            item = create_item(connection, kind="idea", title=title, status=STAGE_STATUS[stage], stage=stage,
                               repo=repo, path=relative, source=SOURCE, external_id=f"{repo}::{piece}",
                               fields=fields, body={"source": text}, session=who, source_commit=source_commit)
            set_item_fields(connection, item, piece=piece, parked_at=now() if relative.startswith("content-parked/") else None)
        else:
            item = existing["id"]
            values = {"title": title, "stage": stage, "path": relative, "fields": fields, "body": {"source": text}, "source_commit": source_commit}
            changed = {key: value for key, value in values.items()
                       if existing[key] != (json.dumps(value, sort_keys=True) if key in ("fields", "body") else value)}
            if changed:
                set_item_fields(connection, item, **changed)
            _transition(connection, item, STAGE_STATUS[stage], who=who, reason="historical writing source")
        return piece_state(connection, item)


def import_piece(connection: sqlite3.Connection, repo: str, piece: str, *, path: str | None = None, who: str) -> dict:
    """Import historical input, or explicitly register a newly created piece.

    Registration after cutover retires its template bookkeeping once. The
    journal distinguishes this from repository cutover because repository
    ownership is already row while the new item may still roll back.
    """
    repo = _canonical(connection, repo)
    _registered_only(repo)
    if pieces_owner(connection, repo) != "row":
        return _import_piece(connection, repo, piece, path=path, who=who)
    journal_path = _journal_path(connection, repo)
    if journal_path.exists() and json.loads(journal_path.read_text()).get("phase") not in ("complete", "rolled_back"):
        raise WorkflowError("unfinished writing operation; recover_cutover before registering a piece")
    journal = None
    touched = []
    try:
        with transaction(connection):
            state = _import_piece(connection, repo, piece, path=path, who=who)
            row = state["item"]
            source = _path(repo, piece, row["path"])
            original = _fields(row["body"])["source"].encode("utf-8")
            retired = _rewrite(original.decode("utf-8"), {}, remove=("status", "published")).encode("utf-8")
            backup = Path(tempfile.mkdtemp(prefix="writing-register-", dir=journal_path.parent))
            saved = backup / row["path"]
            saved.parent.mkdir(parents=True)
            saved.write_bytes(original)
            entry = {"path": row["path"], "before_sha256": _hash(original), "after_sha256": _hash(retired)}
            journal = {"repo": repo, "backup_path": str(backup), "phase": "prepared", "operation": "registration",
                       "piece": piece, "files": [entry], "at": now(), "who": who}
            _save_journal(journal_path, journal)
            if _file_hash(source) != entry["before_sha256"]:
                raise StaleItem("new piece changed before registration retirement")
            touched.append(entry)
            _replace(source, retired)
            result = piece_state(connection, row["id"])
        journal["phase"] = "complete"
        _save_journal(journal_path, journal)
        return result
    except BaseException as error:
        if journal is not None:
            if piece_for_key(connection, repo, piece) is not None:
                raise WorkflowError(f"registration committed; finalize journal with recover_cutover; backup {journal['backup_path']}") from error
            conflicts = _restore_journal_files(journal, touched)
            journal["phase"] = "conflict" if conflicts else "rolled_back"
            _save_journal(journal_path, journal)
            if conflicts:
                raise WorkflowError(f"registration preserved concurrent edits; backup {journal['backup_path']}") from error
        raise


def _inventory(repo: str) -> list[dict]:
    root = _disk(repo)
    entries = {}
    for tree in ("content", "content-parked"):
        for path in sorted((root / tree).glob("*/*/index.md")):
            piece = f"{path.parent.parent.name}/{path.parent.name}"
            _key(piece)
            if piece in entries:
                raise WorkflowError(f"piece {piece} exists in both active and parked trees")
            _path(repo, piece, path.relative_to(root).as_posix())
            data = path.read_bytes()
            entries[piece] = {"piece": piece, "path": path.relative_to(root).as_posix(), "sha256": _hash(data)}
    return list(entries.values())


def import_pieces(connection: sqlite3.Connection, repo: str, *, who: str) -> dict:
    repo = _canonical(connection, repo)
    with transaction(connection):
        items = [import_piece(connection, repo, entry["piece"], path=entry["path"], who=who) for entry in _inventory(repo)]
        return {"items": items, "warnings": [f"{state['item']['piece']}: historical readiness needs fresh gate evidence"
                                               for state in items if state["item"]["stage"] in ("ready", "published")
                                               and not state["writing"]["gates"]["ok"]]}


TEMPLATE = "templates/piece-template.md"


def _pieces_repo(connection: sqlite3.Connection, repo: str | None) -> str:
    """`repo`, else the one repository that registers pieces (sd:1994)."""
    if repo is not None:
        repo = _canonical(connection, repo)
        pieces_owner(connection, repo)
        return repo
    found = [row[0] for row in connection.execute(
        "SELECT path FROM repo WHERE pieces_source != 'file' "
        "UNION SELECT repo FROM item WHERE piece IS NOT NULL AND repo IS NOT NULL ORDER BY 1")]
    if len(found) != 1:
        named = f"several repositories register pieces ({', '.join(found)})" if found else "no repository registers pieces"
        raise WorkflowError(f"{named}; name the target repository")
    return found[0]


def _slug(title: str) -> str:
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")


def promote(connection: sqlite3.Connection, item: int, *, slug: str | None = None, repo: str | None = None,
            who: str, expected_revision: str | None = None) -> dict:
    """Make an idea row a registered piece, scaffolded from the repository's own template (sd:1994).

    The row keeps its id, history and body; it takes the piece identity a
    registration would give it, so verification and recovery read it as one.
    The piece is `<current year>/<slug>`, the slug from the title unless given.
    The file lands in the active `checkout`, and a failure removes it again;
    an untouched scaffold left by a stopped promote is adopted on retry.
    """
    who = _text(who, "who")
    if connection.in_transaction:
        # An outer rollback would undo the row and leave the scaffold behind.
        raise WorkflowError("promote commits its own transaction; call it with none open")
    created = None
    try:
        with transaction(connection):
            row = _checked_state(connection, item, expected_revision)["item"]
            if row["kind"] != "idea":
                raise WorkflowError(f"item {item} is not an idea")
            if row["piece"] is not None:
                raise WorkflowError(f"item {item} is already piece {row['piece']}")
            if row["status"] == "done":
                raise WorkflowError(f"idea {item} is done; reopen it before promoting it")
            title = _text(row["title"], "title")
            if any(ord(char) < 32 or ord(char) == 127 for char in title):
                raise WorkflowError("title must be one line of text without control characters")
            target = _pieces_repo(connection, repo)
            owner = pieces_owner(connection, target)
            if owner == "retiring":
                raise WorkflowError("writing cutover is in progress")
            slug = _slug(title) if slug is None else slug
            if not slug:
                raise WorkflowError(f"title {title!r} gives no slug; pass one")
            today = now()[:10]
            piece = _key(f"{today[:4]}/{slug}")
            disk = _disk(target)
            folder = disk / "content" / piece
            if not folder.resolve().is_relative_to(disk):
                raise WorkflowError(f"content/{piece} escapes the repository")
            template = disk / TEMPLATE
            if not template.resolve().is_relative_to(disk) or not template.is_file():
                raise WorkflowError(f"{target} has no {TEMPLATE} to scaffold from")

            held = _fields(row["body"])
            prose = next((held[key].strip() for key in ("text", "markdown")
                          if isinstance(held.get(key), str) and held[key].strip()), "")

            def scaffold(day: str) -> tuple[str, str]:
                """The registered source and the file it writes, dated `day`.

                The idea's prose opens the body, so the file owns it from here on.
                """
                source = _rewrite(template.read_text(encoding="utf-8"), {"title": title, "created": day, "updated": day})
                if prose:
                    head, fence, rest = source.partition("\n---\n")
                    source = f"{head}{fence}\n{prose}\n{rest}"
                return source, _rewrite(source, {}, remove=("status", "published")) if owner == "row" else source

            text, written = scaffold(today)
            orphan = None
            if folder.exists() and piece_for_key(connection, target, piece) is None:
                # A promote stopped between its file write and its commit leaves
                # an untouched scaffold; adopt it, since it holds no prose.
                try:
                    if not (folder / "index.md").resolve().is_relative_to(disk):
                        raise WorkflowError("scaffold escapes the repository")
                    found = (folder / "index.md").read_text(encoding="utf-8")
                    day = str(_metadata(found).get("created"))
                    if [entry.name for entry in folder.iterdir()] == ["index.md"] and scaffold(day)[1] == found:
                        orphan, (text, written) = found, scaffold(day)
                except (OSError, UnicodeError, WorkflowError):
                    pass
            if (disk / "content-parked" / piece).exists() or orphan is None and (
                    piece_for_key(connection, target, piece) is not None or folder.exists()):
                raise WorkflowError(f"piece {piece} already exists in {target}; pass another slug")
            metadata = _metadata(text)
            if FILE_STAGES.get(metadata.get("status")) != "accepted":
                raise WorkflowError(f"{TEMPLATE} must start a piece at status idea")
            relative = f"content/{piece}/index.md"
            fields = _fields(row["fields"])
            fields["writing"] = metadata
            fields["writing_source"] = {"sha256": _hash(text.encode("utf-8")), "path": relative, "base_commit": None}
            fields["promoted_from"] = {"source": row["source"], "external_id": row["external_id"],
                                       "row": {key: row[key] for key in PROMOTED_COLUMNS}}
            set_item_fields(connection, item, repo=target, piece=piece, path=relative, stage="accepted",
                            source=SOURCE, external_id=f"{target}::{piece}", fields=fields,
                            body={"source": text}, source_commit=None)
            _transition(connection, item, STAGE_STATUS["accepted"], who=who, reason=f"promoted to piece {piece}")
            add_note(connection, item, "comment", f"Promoted to piece {piece} in {target} by {who}", session=who)
            if orphan is None:
                folder.mkdir(parents=True)
                created = folder / "index.md"
                # Whole or absent: a write that fails leaves no partial index.md.
                # A new piece is content other tools read, so it takes the umask mode.
                umask = os.umask(0)
                os.umask(umask)
                _replace(created, written.encode("utf-8"), mode=0o666 & ~umask)
            return piece_state(connection, item)
    except BaseException as error:
        if created is not None and _file_hash(created) not in (None, _hash(written.encode("utf-8"))):
            # Another writer changed the scaffold after it was written; keep the edit.
            error.add_note(f"promote kept {created}: it changed after the scaffold was written")
        elif created is not None:
            created.unlink(missing_ok=True)
            for folder in (created.parent, created.parent.parent):
                try:
                    folder.rmdir()
                except OSError:
                    break
        raise


def record_gate(connection: sqlite3.Connection, item: int, artifact: str, *, verdict: str,
                findings: list[dict], reason: str, who: str, expected_revision: str | None = None,
                reviewed_digest: str | None = None) -> dict:
    if artifact not in ("fact-check", "adversarial") or verdict not in ("pass", "fail"):
        raise WorkflowError("gate needs fact-check or adversarial and an explicit pass or fail verdict")
    reason, who = _text(reason, "reason"), _text(who, "who")
    if not isinstance(findings, list):
        raise WorkflowError("findings must be an explicit list, empty when the review found none")
    held, ids = [], set()
    for finding in findings:
        if not isinstance(finding, dict) or not isinstance(finding.get("id"), str) or not finding["id"].strip():
            raise WorkflowError("each finding needs an explicit id")
        if finding["id"] in ids:
            raise WorkflowError("finding IDs must be unique")
        ids.add(finding["id"])
        if finding.get("disposition") not in ("open", "resolved", "rebutted"):
            raise WorkflowError("finding disposition must be open, resolved or rebutted")
        if finding["disposition"] != "open":
            _text(finding.get("evidence"), "resolution evidence")
        if artifact == "adversarial" and finding.get("confidence") not in ("CERTAIN", "LIKELY", "SPECULATIVE"):
            raise WorkflowError("adversarial finding needs CERTAIN, LIKELY or SPECULATIVE confidence")
        held.append(dict(finding))
    with transaction(connection):
        from .publication import assert_mutable
        assert_mutable(connection, item)
        state = _piece(connection, item, expected_revision)
        row = state["item"]
        if pieces_owner(connection, row["repo"]) == "retiring":
            raise WorkflowError("writing cutover is in progress")
        snapshot = _snapshot(row)
        if not snapshot["digest"] or artifact not in snapshot["files"] or "research" not in snapshot["files"]:
            raise WorkflowError("gate recording requires a draft, research and the full report file")
        evidence_stamps = list(STAMP.finditer(snapshot["files"][artifact].decode("utf-8")))
        if len(evidence_stamps) > 1:
            raise WorkflowError("gate report has ambiguous draft stamps")
        stamped_digest = evidence_stamps[0].group(1) if evidence_stamps else None
        if stamped_digest is not None and stamped_digest != snapshot["digest"]:
            raise WorkflowError("gate report's draft stamp is stale; review the current draft before recording it")
        reviewed = reviewed_digest or stamped_digest
        if reviewed != snapshot["digest"]:
            raise WorkflowError("gate needs the explicitly reviewed draft digest; it must match the current draft")
        # The draft digest ignores whitespace, so it says nothing of layout. Only a stamp's `index=` names the
        # index.md bytes the reviewer read; a gate without one certifies no bytes, and a render refuses it (sd:10719).
        attested = _layout(snapshot["files"][artifact].decode("utf-8"), artifact, snapshot["hashes"]["index"])
        fields = _fields(row["fields"])
        gates = fields.setdefault("writing_gates", {})
        if not isinstance(gates, dict):
            raise WorkflowError("stored writing gate metadata is malformed")
        record = {"verdict": verdict, "findings": held, "reason": reason, "who": who,
                  "draft_digest": snapshot["digest"], "generation": row["gate_generation"],
                  "evidence_sha256": snapshot["hashes"][artifact], "research_sha256": snapshot["hashes"]["research"]}
        if attested:
            record["index_sha256"] = attested
        existing = gates.get(artifact)
        if isinstance(existing, dict) and {key: value for key, value in existing.items() if key != "at"} == record:
            return piece_state(connection, item)
        gates[artifact] = {**record, "at": now()}
        set_item_fields(connection, item, fields=fields)
        add_note(connection, item, "comment", f"{artifact} gate {verdict}: {reason}", session=who)
        return piece_state(connection, item)


def preflight(connection: sqlite3.Connection, item: int) -> dict:
    state = piece_state(connection, item)
    gates = state["writing"]["gates"]
    if state["item"]["stage"] != "ready":
        raise WorkflowError("publication requires a ready piece")
    if not gates["ok"] or state["item"]["ready_digest"] != gates["digest"]:
        raise WorkflowError("publication gate refused: " + "; ".join(gates["problems"] or ["draft differs from the one granted readiness"]))
    return state


def readiness(connection: sqlite3.Connection, item: int) -> dict:
    """Read-only gate diagnosis at any stage, including before readiness."""
    return piece_state(connection, item)["writing"]["gates"]


# Review companions (sd:3301). `adversarial.md` and `research.md` each answer
# to one draft, and each goes silently stale when the draft is revised, so
# each carries the digest of the draft it was last reconciled with, on a line
# of its own. A missing stamp reads as stale, never as fine. These mechanics
# lived in the writing pack's `scripts/pack.py`; they live here so every
# content repository runs the same gates.
COMPANIONS = {"adversarial": REPORTS["adversarial"], "research": REPORTS["research"]}
_STAMP_LINE = re.compile(r"(?m)^<!-- reconciled-with-draft: ([0-9a-f]{12}) .*-->$")
#: The sentences an adversarial run read, so a later reconcile can tell how much is new.
#: A run record. Its word count takes at most 18 digits, so int() never meets one past its 4,300-digit limit: a
#: longer count is no record, and a reconcile then refuses (pack sd:7242).
_REVIEWED = re.compile(r"(?m)^<!-- adversarial-reviewed: words=(\d{1,18}) sentences=([0-9a-f,]*) -->$")
#: A reconcile stamps prose the run never read; past this share of the draft it refuses (sd:2025).
REWRITE_LIMIT = 0.25
#: A shorter draft is not ready for a hostile reader.
REVIEW_MIN_WORDS = 100


def _companion(snapshot: dict, artifact: str) -> dict:
    """One companion's currency against the draft: missing, unstamped, current or stale."""
    data = snapshot["files"].get(artifact)
    if data is None:
        return {"state": "missing", "detail": "never written"}
    stamped = _STAMP_LINE.search(data.decode("utf-8"))
    if not stamped:
        return {"state": "unstamped", "detail": "predates reconcile tracking; treat as stale"}
    if stamped.group(1) == snapshot["digest"]:
        # The digest ignores whitespace; a report the gate would refuse for its layout is not current (sd:10719).
        try:
            _layout(data.decode("utf-8"), artifact, snapshot["hashes"]["index"])
        except WorkflowError as error:
            return {"state": "stale", "detail": str(error)}
        return {"state": "current", "detail": f"draft {snapshot['digest']}"}
    return {"state": "stale", "detail": f"reconciled with {stamped.group(1)}, draft is now {snapshot['digest']}"}


def _sentences(prose: str) -> list[str]:
    return [sentence for sentence in re.split(r"(?<=[.!?])\s+", " ".join(prose.split())) if sentence]


def _sentence_hash(sentence: str) -> str:
    return hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:8]


def reviewed_record(prose: str) -> str:
    """The line a run writes: how many words it read, and a hash per sentence."""
    sentences = _sentences(prose)
    return (f"<!-- adversarial-reviewed: words={sum(len(sentence.split()) for sentence in sentences)} "
            f"sentences={','.join(_sentence_hash(sentence) for sentence in sentences)} -->")


def _unread_share(words: int, hashes: list[str], prose: str) -> float:
    """Share of the larger of the two drafts that the run did not read verbatim, in order.

    Matching is order-sensitive: a moved sentence counts as unread, because
    reordering can change what the prose says.
    """
    sentences = _sentences(prose)
    counts = [len(sentence.split()) for sentence in sentences]
    matcher = difflib.SequenceMatcher(None, hashes, [_sentence_hash(sentence) for sentence in sentences], autojunk=False)
    kept = sum(sum(counts[block.b:block.b + block.size]) for block in matcher.get_matching_blocks())
    return 1 - kept / max(words, sum(counts), 1)


def _committed_draft(root: Path, relative: str, digest: str) -> str | None:
    """The `## Draft` prose of the last commit whose draft has `digest`; None when Git cannot say."""
    try:
        commits = _git(root, "log", "--format=%H", "-n", "200", "--", relative).split()
    except WorkflowError:
        return None
    for commit in commits:
        try:
            prose = _draft(_git(root, "show", f"{commit}:{relative}"))
        except WorkflowError:
            continue
        if prose is not None and _prose_digest(prose) == digest:
            return prose
    return None


def _adversarial_rewrite(row, snapshot: dict, report: str) -> tuple[float | None, str]:
    """How much of the current draft the last run did not read; None when unknowable.

    A report written before runs recorded their sentences falls back to the
    committed draft its stamp names. The second value is the run record
    recovered that way, for the reconcile to keep: the stamp moves on every
    reconcile, the record does not.
    """
    prose = _draft(snapshot["text"]) or ""
    recorded = _REVIEWED.search(report)
    if recorded:
        return _unread_share(int(recorded.group(1)), [h for h in recorded.group(2).split(",") if h], prose), ""
    stamped = _STAMP_LINE.search(report)
    reviewed = _committed_draft(_disk(row["repo"]), row["path"], stamped.group(1)) if stamped else None
    if reviewed is None:
        return None, ""
    record = reviewed_record(reviewed)
    found = _REVIEWED.search(record)
    return _unread_share(int(found.group(1)), [h for h in found.group(2).split(",") if h], prose), record


def adversarial_brief(connection: sqlite3.Connection, item: int) -> dict:
    """What one adversarial run reviews: paths in the checkout, the draft digest, generation and run record.

    The caller renders the prompt from `draft` and `research`, runs the
    reviewer read-only in `root`, and hands its report to `record_adversarial`
    with this digest, record and generation, so the report names the prose
    the run read even if the draft changes while it runs.
    """
    row = _piece(connection, item)["item"]
    snapshot = _snapshot(row)
    prose = _draft(snapshot["text"])
    if prose is None:
        raise WorkflowError(f"{row['piece']} has no '## Draft' section; nothing to review")
    words = len(prose.split())
    if words < REVIEW_MIN_WORDS:
        raise WorkflowError(f"{row['piece']} has only {words} words under '## Draft'; "
                            "draft it before running an adversarial pass")
    disk = _disk(row["repo"])
    draft = snapshot["path"]
    return {"piece": row["piece"], "root": str(disk), "draft": draft.relative_to(disk).as_posix(),
            "research": draft.with_name(REPORTS["research"]).relative_to(disk).as_posix(),
            "research_exists": "research" in snapshot["files"], "digest": snapshot["digest"],
            "generation": row["gate_generation"], "record": reviewed_record(prose), "words": words,
            "index_sha256": snapshot["hashes"]["index"]}


def _write_report(path: Path, text: str) -> None:
    """Replace a report whole; a new one takes the umask mode, as an editor would give it."""
    umask = os.umask(0)
    os.umask(umask)
    _replace(path, text.encode("utf-8"), mode=None if path.exists() else 0o666 & ~umask)


def record_adversarial(connection: sqlite3.Connection, item: int, report: str, *, digest: str, record: str,
                       generation: int, index_sha256: str) -> dict:
    """Write `adversarial.md` for one run: draft stamp, run record, provenance, then the report.

    `digest`, `record`, `generation` and `index_sha256` come from the `adversarial_brief`
    the run reviewed. The result says whether the draft is still that one,
    counts the confidence tags, and quotes the reviewer's verdict line.
    The report is a set of hypotheses; this records which prose it answers
    and decides nothing about it.
    """
    if not isinstance(report, str) or not report.strip():
        raise WorkflowError("the adversarial run produced no report; nothing written")
    if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{12}", digest) is None
            or not isinstance(record, str) or _REVIEWED.fullmatch(record) is None
            or type(generation) is not int
            or not isinstance(index_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", index_sha256) is None):
        raise WorkflowError("adversarial run needs the digest, run record, generation and index hash from its brief")
    row = _piece(connection, item)["item"]
    snapshot = _snapshot(row)
    day = now()[:10]
    target = snapshot["path"].with_name(REPORTS["adversarial"])
    # `index_sha256` names the frozen copy the reviewer read, not the live file: what lands on disk meanwhile, a
    # restored backup too, changes nothing here, and `record_gate` refuses bytes other than these (sd:10719).
    _write_report(target, f"<!-- reconciled-with-draft: {digest} on {day} gen={generation} index={index_sha256} -->\n\n{record}\n\n"
                          f"<!-- codex adversarial review of {row['piece']}, {day}. Findings are HYPOTHESES: "
                          "verify each against the source before acting. Nothing here edited the draft. -->\n\n"
                          + (report if report.endswith("\n") else report + "\n"))
    verdict = re.findall(r"(?im)^.*survive[s]? hostile reading.*$", report)
    return {"piece": row["piece"], "path": target.relative_to(_disk(row["repo"])).as_posix(), "digest": digest,
            "current": snapshot["digest"] == digest and snapshot["hashes"]["index"] == index_sha256,
            "index_sha256": index_sha256,
            "confidence": {tag: len(re.findall(rf"\b{tag}\b", report)) for tag in ("CERTAIN", "LIKELY", "SPECULATIVE")},
            "verdict": verdict[-1].strip() if verdict else None}


def reconcile_companion(connection: sqlite3.Connection, item: int, artifact: str, *, note: str) -> dict:
    """Record that a companion answers the current draft, and append what was reconciled.

    For `adversarial` the note says what became of each finding; it refuses
    when more than `REWRITE_LIMIT` of the draft is prose the last run did not
    read, or when nothing can say how much that is. For `research` the note
    names the claims the revision added and where each is sourced.
    """
    if artifact not in COMPANIONS:
        raise WorkflowError(f"reconcile takes one of {', '.join(sorted(COMPANIONS))}")
    note = _text(note, "reconcile note")
    row = _piece(connection, item)["item"]
    snapshot = _snapshot(row)
    piece, name = row["piece"], COMPANIONS[artifact]
    if artifact not in snapshot["files"]:
        raise WorkflowError(f"no {name} for {piece}; there is nothing to reconcile. "
                            "Run the gate, or write the research, first.")
    digest = snapshot["digest"]
    if not digest:
        raise WorkflowError(f"{piece} has no '## Draft' section to reconcile against")
    before = _companion(snapshot, artifact)
    text = snapshot["files"][artifact].decode("utf-8")
    if before["state"] != "current" and artifact == "adversarial":
        share, record = _adversarial_rewrite(row, snapshot, text)
        rerun = f"Run the adversarial gate on {piece} again; a fresh run is current by construction."
        if share is None:
            raise WorkflowError(f"{piece} adversarial: cannot tell how much of the draft the last run read "
                                f"({before['detail']}), so a reconcile would stamp prose nobody reviewed. {rerun}")
        if share > REWRITE_LIMIT:
            raise WorkflowError(f"{piece} adversarial: {share:.0%} of the draft is prose the last run did not read "
                                f"(the limit is {REWRITE_LIMIT:.0%}), so a reconcile would stamp a rewrite nobody "
                                f"reviewed. {rerun}")
        if record:
            text = f"{record}\n\n{text}"
    day = now()[:10]
    heading = "## Resolution ledger" if artifact == "adversarial" else "## Reconciled with the draft"
    body = _STAMP_LINE.sub("", text).strip()
    # A reconcile answers for the prose, never the layout: it keeps the last run's attestation as it was, so a
    # gate on this report still refuses bytes no reviewer read; only a fresh run moves it (sd:10719).
    kept = _attested(text)
    layout = f" index={kept}" if kept else ""
    _write_report(snapshot["path"].with_name(name),
                  f"<!-- reconciled-with-draft: {digest} on {day} gen={row['gate_generation']}{layout} -->\n\n"
                  f"{body}\n\n{heading} — {day}\n\n{note}\n")
    return {"piece": piece, "artifact": artifact, "digest": digest, "was": before}


def change_stage(connection: sqlite3.Connection, item: int, target: str, *, actor: str = "human", who: str,
                 correct: bool = False, reason: str | None = None, expected_revision: str | None = None,
                 confirmed: bool = False, require_row: bool = False) -> dict:
    if target not in STAGES or actor not in ("human", "session"):
        raise WorkflowError("unknown writing stage or actor")
    who = _text(who, "who")
    original = None
    source = None
    try:
        with transaction(connection):
            from .publication import assert_mutable
            assert_mutable(connection, item)
            state = _piece(connection, item, expected_revision)
            row = state["item"]
            owner = pieces_owner(connection, row["repo"])
            if require_row and owner != "row":
                raise WorkflowError("dashboard stage controls require database ownership after writing cutover")
            if owner == "retiring" or row["parked_at"]:
                raise WorkflowError("piece is parked or its source is retiring")
            current = row["stage"]
            if current == target:
                return piece_state(connection, item)
            if target in ("inbox", "declined"):
                raise WorkflowError("an existing piece is parked instead of declined or returned to inbox")
            correction = correct and target in ("researching", "drafting", "review") and STAGES.index(target) < STAGES.index(current)
            if correct and (not correction or actor != "human"):
                raise WorkflowError("only the human operator can correct a piece to an earlier research or draft stage")
            if correction:
                reason = _text(reason, "correction reason")
            elif target == "published":
                if current != "ready" or not confirmed:
                    raise WorkflowError("published requires ready plus explicit confirmation of the publication")
            elif target not in _normal(current):
                raise WorkflowError(f"writing stage cannot move from {current} to {target} without an explicit correction")
            snapshot = _snapshot(row)
            if target in ("ready", "published"):
                problems = _problems(row, snapshot)
                if problems:
                    raise WorkflowError("writing gate refused: " + "; ".join(problems))
                if target == "published" and row["ready_digest"] != snapshot["digest"]:
                    raise WorkflowError("draft differs from the one granted readiness")
            fields = _fields(row["fields"])
            metadata = fields.setdefault("writing", {})
            if target == "published" and not any((metadata.get("published_urls") or {}).values()):
                raise WorkflowError("published requires an actual recorded publication URL")
            manual_targets = {key: value for key, value in (metadata.get("published_urls") or {}).items()
                              if key in _manual_destinations() and value}
            if target == "published" and not manual_targets:
                raise WorkflowError("Drive publication requires a verified claim and native destination readback; a manually attached Drive URL is unverified")
            changed = {"stage": target, "ready_digest": snapshot["digest"] if target == "ready" else None}
            if correction:
                changed["gate_generation"] = row["gate_generation"] + 1
            if target == "published":
                changed["shipped_at"] = now()
                metadata["published"] = changed["shipped_at"][:10]
                metadata["publication_confirmation"] = {"kind": "manual-acknowledgement", "who": who,
                                                         "at": changed["shipped_at"], "targets": manual_targets}
            elif current == "published":
                changed["shipped_at"] = None
                metadata["published"] = None
            metadata["status"] = "idea" if target == "accepted" else target
            metadata["updated"] = now()[:10]
            changed["fields"] = fields
            if owner == "file":
                source, original = snapshot["path"], snapshot["files"]["index"]
                text = _rewrite(snapshot["text"], {key: metadata.get(key) for key in ("status", "updated", "published")})
                _replace(source, text.encode("utf-8"))
                _carry_index_hash(fields, original, text.encode("utf-8"))
                changed["body"] = {"source": text}
            set_item_fields(connection, item, **changed)
            _transition(connection, item, STAGE_STATUS[target], who=who, reason=reason or f"writing {current} -> {target}")
            if STAGE_STATUS[current] == STAGE_STATUS[target]:
                add_note(connection, item, "comment", f"Writing stage {current} -> {target} by {who}" + (f": {reason}" if reason else ""), session=who)
            return piece_state(connection, item)
    except BaseException:
        if original is not None:
            _replace(source, original)
        raise


def park_piece(connection: sqlite3.Connection, item: int, *, parked: bool = True, actor: str = "human",
               who: str, expected_revision: str | None = None, require_row: bool = False) -> dict:
    if actor != "human" or type(parked) is not bool:
        raise WorkflowError("parking is the human operator's decision")
    with transaction(connection):
        from .publication import assert_mutable
        assert_mutable(connection, item)
        state = _piece(connection, item, expected_revision)
        owner = pieces_owner(connection, state["item"]["repo"])
        if require_row and owner != "row":
            raise WorkflowError("writing controls require row ownership; complete the writing cutover first")
        if owner == "retiring":
            raise WorkflowError("writing cutover is in progress")
        if bool(state["item"]["parked_at"]) == parked:
            return piece_state(connection, item)
        set_item_fields(connection, item, parked_at=now() if parked else None)
        add_note(connection, item, "comment", "Piece parked" if parked else "Piece revived", session=who)
        return piece_state(connection, item)


def update_piece_metadata(connection: sqlite3.Connection, item: int, changes: dict, *,
                          expected_revision: str | None = None, who: str) -> dict:
    who = _text(who, "who")
    allowed = {"published_urls", "review_urls", "tip", "title"}
    if not isinstance(changes, dict) or set(changes) - allowed:
        raise WorkflowError("only published_urls, review_urls, tip and title are editable writing metadata")
    if "title" in changes:
        # One frontmatter line, so no control character; stripped as import strips it.
        title = changes["title"]
        if not isinstance(title, str) or any(ord(char) < 32 or ord(char) == 127 for char in title):
            raise WorkflowError("title must be one line of text without control characters")
        changes = {**changes, "title": _text(title, "title")}
    for name, value in changes.items():
        if name.endswith("_urls"):
            if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
                raise WorkflowError(f"{name} must be a destination mapping")
            for target, url in value.items():
                if target.endswith("_digest"):
                    if url is not None and (not isinstance(url, str) or not re.fullmatch(r"[0-9a-f]{12}", url)):
                        raise WorkflowError("review digest must be a draft digest")
                elif url is not None and (not isinstance(url, str) or any(ord(char) < 32 for char in url) or urlsplit(url).scheme != "https" or not urlsplit(url).netloc):
                    raise WorkflowError("publication and review URLs must be actual https URLs or null")
        elif name == "tip" and value is not None:
            _text(value, "tip")
    original = source = None
    try:
        with transaction(connection):
            from .publication import assert_mutable
            assert_mutable(connection, item)
            state = _piece(connection, item, expected_revision)
            row = state["item"]
            owner = pieces_owner(connection, row["repo"])
            if owner == "retiring":
                raise WorkflowError("writing source is retiring")
            fields = _fields(row["fields"])
            previous_fields = json.dumps(fields, sort_keys=True)
            metadata = fields.setdefault("writing", {})
            for key, value in changes.items():
                if isinstance(value, dict):
                    held = metadata.setdefault(key, {})
                    if held is None:
                        held = metadata[key] = {}
                    allowed_targets = ({"gdrive"} | _manual_destinations()) if key == "published_urls" else {"gdocs", "gdocs_digest"}
                    for target, url in value.items():
                        if target not in allowed_targets and (target not in held or held[target] != url):
                            raise WorkflowError(f"unknown {key} destination {target}")
                        if key == "published_urls" and row["stage"] not in ("ready", "published") and url is not None and held.get(target) != url:
                            raise WorkflowError("published URLs belong only to ready or published pieces")
                    changed_urls = {target: url for target, url in value.items() if held.get(target) != url}
                    held.update(value)
                    if key == "published_urls":
                        provenance = metadata.setdefault("publication_url_provenance", {})
                        for target, url in changed_urls.items():
                            if url is not None:
                                provenance[target] = {"kind": "manual-unverified", "who": who, "url": url}
                else:
                    metadata[key] = value
            retitled = "title" in changes and changes["title"] != row["title"]
            if json.dumps(fields, sort_keys=True) == previous_fields and not retitled:
                return piece_state(connection, item)
            values = {"fields": fields}
            if retitled:
                values["title"] = changes["title"]
            if owner == "file":
                snapshot = _snapshot(row)
                source, original = snapshot["path"], snapshot["files"]["index"]
                text = _rewrite(snapshot["text"], {key: metadata[key] for key in changes})
                _replace(source, text.encode("utf-8"))
                _carry_index_hash(fields, original, text.encode("utf-8"))
                values["body"] = {"source": text}
            set_item_fields(connection, item, **values)
            add_note(connection, item, "comment", f"Updated writing metadata: {', '.join(sorted(changes))}", session=who)
            if retitled:
                add_note(connection, item, "comment",
                         f"Title changed from {row['title']!r} to {changes['title']!r} by {who}", session=who)
            return piece_state(connection, item)
    except BaseException:
        if original is not None:
            _replace(source, original)
        raise


def cutover_preview(connection: sqlite3.Connection, repo: str) -> dict:
    repo = _canonical(connection, repo)
    owner = pieces_owner(connection, repo)
    pending = _journal_path(connection, repo)
    if pending.exists():
        journal = json.loads(pending.read_text(encoding="utf-8"))
        if journal.get("phase") not in ("complete", "rolled_back"):
            raise WorkflowError(f"unfinished writing cutover; run recover_cutover before retrying; backup {journal.get('backup_path')}")
    files = _inventory(repo)
    rows = [{"id": row["id"], "piece": row["piece"], "revision": item_state(connection, row["id"])["revision"]}
            for row in list_pieces(connection, repo, include_parked=True)]
    payload = {"repo": repo, "owner": owner, "files": files, "rows": rows}
    payload["fingerprint"] = _hash(json.dumps(payload, sort_keys=True).encode())
    payload["warnings"] = []
    return payload


def verify_pieces(connection: sqlite3.Connection, repo: str) -> dict:
    repo = _canonical(connection, repo)
    owner = pieces_owner(connection, repo)
    files = _inventory(repo)
    rows = {row["piece"]: row for row in list_pieces(connection, repo, include_parked=True)}
    row_count = len(rows)
    differences = []
    for entry in files:
        row = rows.pop(entry["piece"], None)
        if row is None:
            differences.append(f"{entry['piece']}: missing database row")
            continue
        text = _path(repo, entry["piece"], entry["path"]).read_text(encoding="utf-8")
        metadata = _metadata(text)
        if row["path"] != entry["path"]:
            differences.append(f"{entry['piece']}: artifact path differs")
        if owner == "row":
            if "status" in metadata or "published" in metadata:
                differences.append(f"{entry['piece']}: retired status or published line remains")
        else:
            if FILE_STAGES.get(metadata.get("status")) != row["stage"]:
                differences.append(f"{entry['piece']}: file stage differs from imported row")
            if _fields(row["fields"]).get("writing") != metadata:
                differences.append(f"{entry['piece']}: imported metadata differs from source")
            if _fields(row["body"]).get("source") != text:
                differences.append(f"{entry['piece']}: imported source body differs")
            if bool(row["parked_at"]) != entry["path"].startswith("content-parked/"):
                differences.append(f"{entry['piece']}: parked identity differs from source")
    differences.extend(f"{piece}: database row has no artifact" for piece in rows)
    if owner == "row":
        marker = _disk(repo) / MARKER
        if not marker.exists() or marker.read_text(encoding="utf-8") != "row\n":
            differences.append("writing status marker is missing or incorrect")
    return {"ok": not differences, "owner": owner, "files": len(files), "rows": row_count, "differences": differences}


def _write_marker(repo: str) -> None:
    _replace(sdpaths.expand(repo) / MARKER, b"row\n")


def _journal_path(connection: sqlite3.Connection, repo: str) -> Path:
    """The journal is named by a hash of the repository, so it probes the hash
    of each stored form: a journal written before migration 014 is named by
    the absolute path (sd:1439). A new journal takes the key's name."""
    refuse_hub_only(connection, "the writing cutover journal")
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    names = [database.parent / f"writing-cutover-{_hash(form.encode())[:16]}.json"
             for form in sdpaths.keys(repo)]
    return next((name for name in names if name.exists()), names[0])


def _save_journal(path: Path, journal: dict) -> None:
    _replace(path, (json.dumps(journal, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _file_hash(path: Path) -> str | None:
    return _hash(path.read_bytes()) if path.exists() else None


def _restore_journal_files(journal: dict, entries: list[dict]) -> list[str]:
    conflicts = []
    backup = Path(journal["backup_path"])
    root = sdpaths.expand(journal["repo"]).resolve()
    for entry in reversed(entries):
        target = root / entry["path"]
        if not target.resolve().is_relative_to(root):
            conflicts.append(entry["path"] + ": path escaped repository")
            continue
        current = _file_hash(target)
        if current == entry["before_sha256"]:
            continue
        if current != entry["after_sha256"]:
            conflicts.append(entry["path"] + ": changed by another writer; preserved")
            continue
        if entry["before_sha256"] is None:
            target.unlink()
        else:
            saved = backup / entry["path"]
            data = saved.read_bytes()
            if _hash(data) != entry["before_sha256"]:
                conflicts.append(entry["path"] + ": recovery backup hash differs")
                continue
            _replace(target, data)
    return conflicts


def recover_cutover(connection: sqlite3.Connection, repo: str, *, who: str) -> dict:
    """Recover a killed cutover without overwriting subsequent user edits."""
    repo = _canonical(connection, repo)
    _registered_only(repo)
    journal_path = _journal_path(connection, repo)
    if not journal_path.exists():
        raise WorkflowError("no writing cutover journal exists for this repository")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if not sdpaths.same(journal.get("repo"), repo):
        raise WorkflowError("cutover journal belongs to another repository")
    with transaction(connection):
        owner = pieces_owner(connection, repo)
        registration = journal.get("operation") == "registration"
        registered = not registration or piece_for_key(connection, repo, journal["piece"]) is not None
        if owner == "row" and registered:
            result = verify_pieces(connection, repo)
            if not result["ok"]:
                raise WorkflowError(f"cutover committed but files now differ; preserve edits and inspect {journal['backup_path']}")
            journal["phase"] = "complete"
            _save_journal(journal_path, journal)
            return {**result, "recovered": "completed", "backup_path": journal["backup_path"]}
        conflicts = _restore_journal_files(journal, journal["files"])
        journal["phase"] = "conflict" if conflicts else "rolled_back"
        journal["conflicts"] = conflicts
        _save_journal(journal_path, journal)
        if conflicts:
            raise WorkflowError(f"cutover recovery preserved concurrent edits: {'; '.join(conflicts)}; backup {journal['backup_path']}")
        if owner == "retiring":
            upsert_repo(connection, repo, pieces_source="file")
        return {"ok": True, "recovered": "rolled_back", "owner": owner, "backup_path": journal["backup_path"]}


def cutover_pieces(connection: sqlite3.Connection, repo: str, *, expected_fingerprint: str, who: str) -> dict:
    """Retire only status bookkeeping, retaining every source artifact.

    A dry-run fingerprint binds the source files and rows. Backups are retained
    outside the repository and returned to the caller. An ordinary exception
    restores exact files while SQLite rolls back the ownership and row writes.
    """
    refuse_hub_only(connection, "the writing cutover")
    original, rewritten, touched = {}, {}, []
    journal = None
    repo = _canonical(connection, repo)
    _registered_only(repo)
    disk = sdpaths.expand(repo)
    journal_path = _journal_path(connection, repo)
    try:
        with transaction(connection):
            preview = cutover_preview(connection, repo)
            if preview["fingerprint"] != expected_fingerprint:
                raise StaleItem("writing files or rows changed since cutover preview; review a fresh preview")
            if preview["owner"] == "row":
                return verify_pieces(connection, repo)
            if preview["owner"] != "file":
                raise WorkflowError("writing source is already retiring")
            import_pieces(connection, repo, who=who)
            verified = verify_pieces(connection, repo)
            if not verified["ok"]:
                raise WorkflowError("writing import verification failed: " + "; ".join(verified["differences"]))
            database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
            backup = Path(tempfile.mkdtemp(prefix="writing-cutover-", dir=database.parent))
            for entry in preview["files"]:
                path = disk / entry["path"]
                data = path.read_bytes()
                if _hash(data) != entry["sha256"]:
                    raise StaleItem(f"{entry['piece']} changed during cutover")
                original[path] = data
                rewritten[path] = _rewrite(data.decode("utf-8"), {}, remove=("status", "published")).encode("utf-8")
                target = backup / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            marker = disk / MARKER
            original[marker] = marker.read_bytes() if marker.exists() else None
            rewritten[marker] = b"row\n"
            if original[marker] is not None:
                saved_marker = backup / MARKER
                saved_marker.parent.mkdir(parents=True, exist_ok=True)
                saved_marker.write_bytes(original[marker])
            (backup / "manifest.json").write_text(json.dumps(preview, indent=2) + "\n", encoding="utf-8")
            entries = [{"path": path.relative_to(disk).as_posix(),
                        "before_sha256": _hash(data) if data is not None else None,
                        "after_sha256": _hash(rewritten[path])} for path, data in original.items()]
            journal = {"repo": repo, "backup_path": str(backup), "phase": "prepared", "files": entries,
                       "fingerprint": expected_fingerprint, "who": who, "at": now()}
            _save_journal(journal_path, journal)
            upsert_repo(connection, repo, pieces_source="retiring")
            for entry in entries:
                path = disk / entry["path"]
                if _file_hash(path) != entry["before_sha256"]:
                    raise StaleItem(f"{entry['path']} changed immediately before cutover write")
                touched.append(entry)
                if path == marker:
                    _write_marker(repo)
                else:
                    _replace(path, rewritten[path])
            upsert_repo(connection, repo, pieces_source="row")
            result = verify_pieces(connection, repo)
            if not result["ok"]:
                raise WorkflowError("writing cutover verification failed: " + "; ".join(result["differences"]))
            result["backup_path"] = str(backup)
            receipt = record_state(connection, "verified", key=f"{repo}:pieces_source",
                                   body={"fingerprint": expected_fingerprint, "rows": result["rows"], "files": preview["files"]})
            resolve_state(connection, receipt)
        journal["phase"] = "complete"
        _save_journal(journal_path, journal)
        return result
    except BaseException as error:
        if journal is not None:
            # After SQLite commits, retain the durable journal for explicit
            # recovery. Reverting files then would conflict with row authority.
            if pieces_owner(connection, repo) == "row":
                raise WorkflowError(f"cutover committed; finalize its journal with recover_cutover; backup {journal['backup_path']}") from error
            conflicts = _restore_journal_files(journal, touched)
            journal["phase"] = "conflict" if conflicts else "rolled_back"
            journal["conflicts"] = conflicts
            _save_journal(journal_path, journal)
            if conflicts:
                raise WorkflowError(f"cutover stopped and preserved concurrent edits: {'; '.join(conflicts)}; backup {journal['backup_path']}") from error
        raise
