"""Complete review input accounting and advisory split plans; never dispatch."""

from __future__ import annotations

import base64
import codecs
import difflib
import hashlib
import json
import os
import pathlib
import re
import subprocess
from typing import Any

import sd_lib


def read_git_material(root: pathlib.Path, args: list[str], stdin: str | None = None) -> str:
    """Keep NUL-delimited names and whitespace intact."""
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, input=stdin,
                            check=False, timeout=sd_lib.GIT_TIMEOUT_SECONDS)
    if result.returncode:
        raise ValueError("cannot read the complete review subject")
    return result.stdout


def changed(root: pathlib.Path, args: list[str]) -> tuple[list[str], int]:
    rows = read_git_material(root, ["diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", *args]).split("\0")
    paths, lines = [], 0
    for row in filter(None, rows):
        added, removed, name = row.split("\t", 2)
        paths.append(name)
        lines += sum(int(value) for value in (added, removed) if value.isdigit())
    return paths, lines


def untracked(root: pathlib.Path) -> list[str]:
    return list(filter(None, read_git_material(root, ["ls-files", "--others", "--exclude-standard", "-z"]).split("\0")))


# sd:2181: of what git calls binary, only media is summarized: reviewers cannot read it and a retaken
# screenshot overruns the limit. UTF-8 (a `-diff` file) and BOM-marked UTF-16 go as text; anything else
# stays base64, so the size check refuses honestly. Not media: zip, gzip (can carry source), ICO (weak magic).
BINARY_MARKER = re.compile(r"(?m)^Binary files .* differ\n?")
MEDIA, SUMMARY = re.compile(r"(?m)^\[binary, not sent\] "), re.compile(r"(?m)^\[(?:binary, not sent\] |renamed\] .*; unchanged lines not sent$)")  # a patch prefixes content lines
DELETED = re.compile(r"(?m)^deleted file mode ")  # sd:6088: with -D this header is all a deletion sends
RENAMED = re.compile(r'(?m)^\[renamed\] ("(?:[^"\\]|\\.)*") -> "(?:[^"\\]|\\.)*"; unchanged lines not sent$')
MEDIA_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"wOFF", b"wOF2", b"%PDF-")


def is_media(data: bytes) -> bool:
    return data.startswith(MEDIA_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def as_text(data: bytes) -> str | None:
    try:
        return data.decode("utf-16" if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8")
    except UnicodeError:
        return None


def encoding(data: bytes) -> str:
    marks = (("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE), ("utf-8", codecs.BOM_UTF8))
    return next((f"{name}, BOM" for name, mark in marks if data.startswith(mark)), "utf-8")


def content_summary(data: bytes) -> str:
    return f"{len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}"


def file_material(root: pathlib.Path, name: str, change: str) -> tuple[str, bool]:
    """A whole file and whether it is summarized, read from its bytes: a text file may start with the marker (sd:2431)."""
    path = root / name
    media = False
    if path.is_symlink():
        content = "symlink -> " + os.readlink(path)
    else:
        data = path.read_bytes()
        media = is_media(data)
        text = None if media else as_text(data)
        if text is not None:
            content = ("" if encoding(data) == "utf-8" else f"[encoding] {encoding(data)}\n") + text
        elif media:
            content = f"[binary, not sent] {change}; {content_summary(data)}"
        else:
            content = "[binary, base64]\n" + base64.b64encode(data).decode("ascii")
    return f"\n--- {json.dumps(name)} ---\n{content}", media


def collect_review_material(root: pathlib.Path, subject: Any, limit: int = 2_000_000, reserve: int = 0) -> tuple[str, list[dict[str, Any]]]:
    """One entry per literal path; renames keep both sides. Over `limit` less `reserve` (prompt and overhead, sd:7653), text
    renames go as their delta (sd:7598), then deleted files as their header (sd:6088); each such path is summarized."""
    for whole, drop_deleted in ((True, False), (False, False), (False, True)):
        if len((found := _collect(root, subject, drop_deleted, whole))[0].encode("utf-8")) + reserve <= limit:
            break
    return found


def _collect(root: pathlib.Path, subject: Any, drop_deleted: bool, whole: bool) -> tuple[str, list[dict[str, Any]]]:
    extra = set(untracked(root)) if subject.scope == "worktree" else set()
    patches = {} if subject.scope == "planning" else tracked_material(root, subject, drop_deleted, whole)
    parts: list[str] = []
    inventory: list[dict[str, Any]] = []
    for name in subject.paths:
        if subject.scope == "planning" or name in extra:
            part, summarized = file_material(root, name, "current" if subject.scope == "planning" else "added (untracked)")
        else:
            part, summarized = patches[name], bool(SUMMARY.search(patches[name]) or drop_deleted and DELETED.search(patches[name]))
        parts.append(part := ("\n" if parts else "") + part)
        inventory.append({"path": name, "bytes": len(part.encode("utf-8")),
                          "boundary": name.split("/", 1)[0] if "/" in name else "repository-root",
                          **({"summarized": True} if summarized else {}), **({"media": True} if summarized and MEDIA.search(part) else {})})
    return "".join(parts), inventory


def tracked_material(root: pathlib.Path, subject: Any, drop_deleted: bool = False, whole: bool = False) -> dict[str, str]:
    common = ["--no-ext-diff", "--no-textconv", "--no-color", "--submodule=short", *(["--irreversible-delete"] if drop_deleted else []), subject.base,
              *([] if subject.head == "worktree" else [subject.head]), "--"]
    args = ["--no-renames", *common]
    names = list(filter(None, read_git_material(root, ["diff", "--name-only", "-z", *args]).split("\0")))
    patch = read_git_material(root, ["diff", *args])
    pieces = list(filter(None, re.split(r"(?m)(?=^diff --git )", patch)))
    if len(names) != len(pieces):
        raise ValueError("review patch and path inventory disagree; no partial subject sent")
    material = dict(zip(names, pieces, strict=True))
    if any("\ndeleted file mode " in piece for piece in pieces) and any("\nnew file mode " in piece for piece in pieces):
        material.update(renamed_material(root, common, material, whole))
    binary = {name: piece for name, piece in material.items() if BINARY_MARKER.search(piece)}
    return {**material, **binary_material(root, subject, args, binary)} if binary else material


def renamed_material(root: pathlib.Path, common: list[str], material: dict[str, str], whole: bool = False) -> dict[str, str]:
    """sd:2400: a rename sends git's rename patch under the new path and a record under the old. `whole` adds a text
    rename's new file in full, so both sides are read (sd:7598); a delta alone is partial coverage (sd:2181)."""
    rows = read_git_material(root, ["diff", "--name-status", "-z", "-M", *common]).split("\0")
    entries, index = [], 0
    while index < len(rows) and rows[index]:
        width = 2 if rows[index][:1] in ("R", "C") else 1
        entries.append((rows[index], rows[index + 1:index + 1 + width]))
        index += 1 + width
    if not any(status.startswith("R") for status, _paths in entries):
        return {}
    patch = read_git_material(root, ["diff", "-M", *common])
    pieces = list(filter(None, re.split(r"(?m)(?=^diff --git )", patch)))
    if len(entries) != len(pieces) or any(path not in material for _status, paths in entries for path in paths):
        raise ValueError("review patch and path inventory disagree; no partial subject sent")
    renamed = {}
    for (status, paths), piece in zip(entries, pieces, strict=True):
        if status.startswith("R"):
            old, new = paths
            full = whole and not BINARY_MARKER.search(piece)
            record = f"[renamed] {json.dumps(old)} -> {json.dumps(new)}; {'new file sent whole' if full else 'unchanged lines not sent'}"
            side = [json.dumps(f) if re.search(r'[\x00-\x1f"\\\x7f]', f) else f for f in (f"a/{old}", f"b/{old}")]
            renamed[old], (head, _, rest) = f"diff --git {side[0]} {side[1]}\n{record}\n", piece.partition("\n")
            renamed[new] = f"{head}\n{record}\n{rest}" + (material[new] if full else "")
    return renamed


def read_blobs(root: pathlib.Path, oids: list[str]) -> dict[str, tuple[str, bytes]]:
    """One `cat-file --batch`: abbreviated id -> (full id, bytes); a missing object is left out."""
    result = subprocess.run(["git", "cat-file", "--batch"], cwd=root, capture_output=True, check=False,
                            input="".join(f"{oid}\n" for oid in oids).encode(), timeout=sd_lib.GIT_TIMEOUT_SECONDS)
    if result.returncode:
        raise ValueError("cannot read the complete review subject")
    blobs, out = {}, result.stdout
    for oid in oids:
        header, out = out.split(b"\n", 1)
        full, *rest = header.decode().split(" ")
        if rest[:1] == ["blob"]:
            size = int(rest[1])
            blobs[oid], out = (full, out[:size]), out[size + 1:]
    return blobs


Side = tuple[bytes | None, str]


def binary_sides(root: pathlib.Path, subject: Any, pieces: dict[str, str]) -> dict[str, list[Side]]:
    """Each side's bytes and label: a committed blob, working-tree content, or absent."""
    oids = {name: (re.findall(r"(?m)^index ([0-9a-f]+)\.\.([0-9a-f]+)", piece) or [("", "")])[0] for name, piece in pieces.items()}
    worktree = subject.head == "worktree"
    blobs = read_blobs(root, sorted({oid for pair in oids.values() for oid in pair[:1 if worktree else 2] if oid.strip("0")}))
    sides = {}
    for name, pair in oids.items():
        found: list[Side] = [(blobs[oid][1], f"{len(blobs[oid][1])} bytes, blob {blobs[oid][0]}") if oid in blobs
                             else (None, "unreadable" if oid.strip("0") else "absent") for oid in pair]
        if worktree:
            path = root / name
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
            found[1] = (data, "absent" if data is None else content_summary(data))
        sides[name] = found
    return sides


def binary_piece(name: str, piece: str, found: list[Side]) -> str | None:
    """A media summary, a text diff of decodable sides, or None for `git diff --binary`."""
    header = BINARY_MARKER.split(piece, 1)[0]
    present = [data for data, label in found if label != "absent"]
    if present and all(data is not None and is_media(data) for data in present):
        change = "added" if "\nnew file mode " in piece else "deleted" if "\ndeleted file mode " in piece else "modified"
        return f"{header}[binary, not sent] {change}; old {found[0][1]}; new {found[1][1]}\n"
    texts = [None if data is None or is_media(data) else as_text(data) for data in present]
    if not present or None in texts or len(texts) == 2 and texts[0] == texts[1]:
        return None
    return header + text_diff(name, found)


def text_diff(name: str, found: list[Side]) -> str:
    """The decoded sides as a unified diff, naming any encoding but plain UTF-8."""
    labels = [encoding(data) if data is not None else "absent" for data, _label in found]
    note = "" if set(labels) <= {"utf-8", "absent"} else f"[encoding] old {labels[0]}; new {labels[1]}\n"
    old, new = ((as_text(data) or "", "ab"[side] + "/" + name) if data is not None else ("", "/dev/null")
                for side, (data, _label) in enumerate(found))
    diff = difflib.unified_diff(old[0].splitlines(True), new[0].splitlines(True), old[1], new[1])
    return note + "".join(line if line.endswith("\n") else line + "\n" for line in diff)


def binary_material(root: pathlib.Path, subject: Any, args: list[str], pieces: dict[str, str]) -> dict[str, str]:
    """Summarize media, diff decodable text, and leave anything else to `git diff --binary`."""
    sides = binary_sides(root, subject, pieces)
    material = {name: text for name, piece in pieces.items() if (text := binary_piece(name, piece, sides[name])) is not None}
    raw = [name for name in pieces if name not in material]
    moved = {name: found for name in raw if (found := RENAMED.search(pieces[name]))}
    plain = [name for name in raw if name not in moved]
    if plain:
        patch = read_git_material(root, ["diff", "--binary", *args, *(":(literal)" + name for name in plain)])
        encoded = list(filter(None, re.split(r"(?m)(?=^diff --git )", patch)))
        if len(encoded) != len(plain):
            raise ValueError("review patch and path inventory disagree; no partial subject sent")
        material.update(zip(plain, encoded, strict=True))
    renames = ["-M", *(arg for arg in args if arg != "--no-renames")]
    for new, record in moved.items():  # sd:2432: -M sends a moved, edited blob as its delta, not a full literal
        encoded = list(filter(None, re.split(r"(?m)(?=^diff --git )", read_git_material(
            root, ["diff", "--binary", *renames, ":(literal)" + json.loads(record.group(1)), ":(literal)" + new]))))
        if len(encoded) != 1 or "\nrename from " not in encoded[0]:
            raise ValueError("review patch and path inventory disagree; no partial subject sent")
        head, _, rest = encoded[0].partition("\n")
        material[new] = f"{head}\n{record.group(0)}\n{rest}"
    return material


def coverage(inventory: list[dict[str, Any]], overheads: dict[str, str | None]) -> dict[str, list[str]]:
    """sd:2181: a summarized path is unread by a transport that sees only this material."""
    omitted = [row["path"] for row in inventory if row.get("summarized")]
    return {"omitted_paths": omitted,
            "partial_providers": sorted(name for name, overhead in overheads.items() if overhead is not None) if omitted else []}


def input_manifest(inventory: list[dict[str, Any]], prompt: str, overheads: dict[str, str | None], limit: int,
             context: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    context = context or []
    paths = {row["path"]: dict(row) for row in inventory}
    for row in context:
        current = paths.setdefault(row["path"], dict(row, bytes=0))
        current["bytes"] += row["bytes"]
    inventory = list(paths.values())
    context_bytes = sum(row["bytes"] for row in context)
    prompt_bytes = len(prompt.encode("utf-8")) - sum(row.get("included_bytes", row["bytes"]) for row in context)
    material_bytes = sum(row["bytes"] for row in inventory)
    # None means native repository access: only the complete prompt is transmitted.
    transports = {name: prompt_bytes + (context_bytes if overhead is None else material_bytes + len(overhead.encode()))
                  for name, overhead in overheads.items()}
    measured = max(transports.values(), default=prompt_bytes + material_bytes)
    allowance = max(0, limit - prompt_bytes - max((len((value or "").encode()) for value in overheads.values()), default=0))
    groups: list[dict[str, Any]] = []
    for row in inventory:
        if not groups or groups[-1]["bytes"] + row["bytes"] > allowance or groups[-1]["boundary"] != row["boundary"]:
            groups.append({"paths": [], "bytes": 0, "boundary": row["boundary"]})
        groups[-1]["paths"].append(row["path"])
        groups[-1]["bytes"] += row["bytes"]
    return {"status": "oversized" if measured > limit else "within_limit", "limit_bytes": limit,
            "measured_bytes": measured, "prompt_bytes": prompt_bytes, "material_bytes": material_bytes, "context_bytes": context_bytes,
            "transport_bytes": transports, "paths": inventory, "suggested_groups": groups, **coverage(inventory, overheads),
            "oversized_paths": [row["path"] for row in inventory if row["bytes"] > allowance],
            "next_action": "split_input_for_oversized_providers" if measured > limit else None,
            "advisory_only": False, "split_plan_advisory_only": True,
            "dependency_boundaries": "directory hints; semantic dependencies require operator review",
            "cross_branch_concerns": ["Keep shared interfaces, imports, migrations, and their tests coordinated."],
            "review_complete": False}
