"""Help (sd:10917): what to say to a session and how the fleet works, as one read-only document.

Two sources, both read on each request, so nothing here goes stale:

* the phrase table, from the "Operator runs" list in `skills/sd-next/SKILL.md`, each run's budget read from the
  reference file the list names: a new run shows with no page edit;
* the prose, from `v2/help.md` beside the page: `## ` sections of paragraphs and `- ` bullets, with `code`,
  `[text](/path)` links to dashboard views, and two tokens. `{live:<name>}` is a value read now from its source
  (`LIVE`); `{item:<n>}` is that item's status and title from the database. A value that does not read says
  why and the document lists it under `problems`; it is never left out or shown as a value.

The document writes nothing.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
from pathlib import Path

from . import pack_library, pack_module

__all__ = ["HELP", "LIVE", "SKILL", "document", "phrases", "sections"]

#: The pack checkout this dashboard runs from.
ROOT = pack_library().parent
SKILL = ROOT / "skills" / "sd-next" / "SKILL.md"
HELP = Path(__file__).resolve().parent / "v2" / "help.md"
#: A link in help.md opens a dashboard view: a path on this server, nothing else.
LINK = re.compile(r"/[A-Za-z0-9/_?=&#.%-]*")
INLINE = re.compile(r"`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|\{(live|item):([a-z_0-9]+)\}")
RUN = re.compile(r"- `([^`]+)`[^:]*: (.*)")
PHRASE = re.compile(r'"([^"]+)"(?: \(([^)]*)\))?')
GIT_SECONDS = 10


def _runs_block(text: str) -> list[str]:
    """The lines of the SKILL.md section `## Operator runs`; ValueError when it is missing."""
    found = re.search(r"^## Operator runs\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not found:
        raise ValueError(f"{SKILL.name} has no '## Operator runs' section")
    return found.group(1).splitlines()


def _reference(path: str, skill: Path, root: Path) -> Path:
    """A run's reference file: `skills/...` from the pack root, anything else beside SKILL.md; never outside the pack."""
    target = ((root if path.startswith("skills/") else skill.parent) / path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"{path} is outside the pack checkout")
    return target


def phrases(skill: Path = SKILL, root: Path = ROOT) -> list[dict]:
    """One row per phrase the run list names: `{phrase, does, budget, mode, reference}`.

    A phrase with no parenthesis shares the next one's ("open threads" or "collect followups" (...)). The budget is
    the reference's `Budget:` line; `mode` says whether the run queues work. A reference that does not read raises.
    """
    rows = []
    for line in _runs_block(skill.read_text(encoding="utf-8")):
        run = RUN.fullmatch(line.strip())
        if not run:
            continue
        reference = run.group(1)
        text = _reference(reference, skill, root).read_text(encoding="utf-8")
        budget = re.search(r"^Budget: ([^.\n]+)", text, re.M)
        # sd:10917 lane review: a run queues builds only where its reference runs `sd assignments build`.
        mode = ("reports and files; queues nothing" if "queues nothing" in text
                else "acts: fixes, files, rules, queues builds" if "sd assignments build" in text
                else "acts: fixes, files, rules")
        waiting = []
        for phrase, does in PHRASE.findall(run.group(2)):
            waiting.append(phrase)
            if does:
                rows.extend({"phrase": each, "does": does, "budget": budget.group(1) if budget else "not stated",
                             "mode": mode, "reference": reference} for each in waiting)
                waiting = []
        if waiting:
            raise ValueError(f"{skill.name}: {', '.join(waiting)} has no description in parentheses")
    if not rows:
        raise ValueError(f"{skill.name}: the Operator runs list names no phrase")
    return rows


def described(skill: Path = SKILL) -> list[str]:
    """Every quoted phrase in SKILL.md's front-matter description: what a session answers to."""
    head = re.search(r"\A---\n(.*?)\n---", skill.read_text(encoding="utf-8"), re.S)
    line = re.search(r"^description: (.*)$", head.group(1) if head else "", re.M)
    return re.findall(r'"([^"]+)"', line.group(1)) if line else []


# ---------- Live values ----------

def _machine_cap():
    from sd_db import build

    return f"{build.machine_cap()} builders at once on this machine, {build.repo_cap()} per repository"


def _load_cap():
    from sd_db import build

    cap = build.load_cap()
    if not math.isfinite(cap):
        raise ValueError("this machine reports no core count")
    return f"{cap:g} (five-minute load; {build.LOAD_FACTOR:g} × {os.cpu_count()} cores)"


def _load5():
    return f"{os.getloadavg()[1]:.1f}"


def _gate_slots():
    from . import queue_screen

    gates = queue_screen._gates()
    if "error" in gates:
        raise ValueError(gates["error"])
    return f"{gates['running']} of {gates['cap']} held, {gates['waiting']} waiting"


def _threat_model():
    block = pack_module("sd_lib").local_block(ROOT)
    value = block.get("threat-model")
    if not value:
        raise ValueError("CLAUDE.local.md sets no threat-model")
    return value


def _hub_pin():
    from sd_db.config import system_checkout

    checkout = system_checkout()
    done = subprocess.run(["git", "-C", str(checkout), "tag", "-l", "hub-pin",
                           "--format=%(*objectname:short)%00%(taggerdate:iso-strict)%00%(contents)"],
                          capture_output=True, text=True, timeout=GIT_SECONDS, check=False, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise ValueError(f"git tag exit {done.returncode}: {(done.stderr.strip().splitlines() or [''])[-1]}")
    parts = done.stdout.split("\0")
    if len(parts) < 3 or not parts[0].strip():
        raise ValueError(f"the system checkout has no hub-pin tag ({checkout.name})")
    pack = re.search(r"^pack=([0-9a-f]{7,40})$", parts[2], re.M)
    return (f"system {parts[0].strip()}, pack {pack.group(1)[:9] if pack else 'not named'}"
            f", published {parts[1].strip()}")


#: Each `{live:<name>}` help.md may use, and the call that reads it now.
LIVE = {
    "machine_cap": _machine_cap,
    "load_cap": _load_cap,
    "load5": _load5,
    "gate_slots": _gate_slots,
    "threat_model": _threat_model,
    "hub_pin": _hub_pin,
}


def _item(connection, number: str) -> str:
    from sd_db import reads

    row = reads.item_by_id(connection, int(number))
    if row is None:
        raise ValueError(f"no item sd:{number}")
    return f"sd:{number} is {row['status']}: {row['title']}" + (f", due {row['due']}" if row["due"] else "")


# ---------- help.md ----------

def _inline(text: str, value) -> list[dict]:
    parts, at = [], 0
    for found in INLINE.finditer(text):
        if found.start() > at:
            parts.append({"text": text[at:found.start()]})
        code, label, href, token, name = found.groups()
        if code is not None:
            parts.append({"code": code})
        elif label is not None:
            if not LINK.fullmatch(href):
                raise ValueError(f"help.md links {href!r}: a link opens a dashboard view, a path that starts with /")
            from . import v2

            if re.split(r"[?#]", href)[0] not in v2.PAGES:
                raise ValueError(f"help.md links {href!r}: no dashboard page is at that path")
            parts.append({"text": label, "href": href})
        else:
            parts.append(value(token, name))
        at = found.end()
    if at < len(text):
        parts.append({"text": text[at:]})
    return parts


def sections(text: str, value) -> list[dict]:
    """help.md as `[{title, blocks: [{kind: p|li, parts}]}]`; `value(token, name)` answers each token's part."""
    out, lines, kind = [], [], None

    def flush():
        nonlocal lines, kind
        if lines:
            out[-1]["blocks"].append({"kind": kind, "parts": _inline(" ".join(lines), value)})
        lines, kind = [], None

    for raw in text.splitlines():
        line = raw.strip()
        if raw.startswith("## "):
            flush()
            out.append({"title": raw[3:].strip(), "blocks": []})
        elif not line:
            flush()
        elif not out:
            raise ValueError("help.md: text before the first '## ' section")
        elif line.startswith("- "):
            flush()
            lines, kind = [line[2:]], "li"
        else:
            if kind is None:
                kind = "p"
            lines.append(line)
    flush()
    return out


def document(connection, *, now: str) -> dict:
    """The Help page's one read: `{read, phrases, sections, problems}`; a source that fails is a problem, named."""
    problems: list[str] = []
    try:
        rows = phrases()
    except (OSError, UnicodeDecodeError, ValueError) as error:
        rows = []
        problems.append(f"The phrase table did not read: {error}")
    cache: dict[str, dict] = {}

    def value(token: str, name: str) -> dict:
        key = f"{token}:{name}"
        if key not in cache:
            try:
                if token == "item":
                    cache[key] = {"live": key, "value": _item(connection, name)}
                elif name in LIVE:
                    cache[key] = {"live": key, "value": LIVE[name]()}
                else:
                    raise ValueError(f"help.md names {key}, which no source reads")
            except Exception as error:  # noqa: BLE001 - every failure is shown on the page, by name
                cache[key] = {"live": key, "unknown": str(error) or type(error).__name__}
                problems.append(f"{key} did not read: {cache[key]['unknown']}")
        return cache[key]

    try:
        body = sections(HELP.read_text(encoding="utf-8"), value)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        body = []
        problems.append(f"help.md did not read: {error}")
    return {"read": now, "phrases": rows, "sections": body, "problems": problems}
