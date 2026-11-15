"""The one text layout of `sd today` and `sd-status` (sd:5098).

Both commands render `sd_db.today.sections` here, so a section reads the same
in each. Every section prints in `sd_db.today.ORDER`, empty or not: a heading
`<Section> (<count>)`, then a plain ASCII table (a header row, a rule of
dashes, aligned columns) of at most `ROWS` rows, then `… N more`. Work is the
exception to the cap: it is the list, and it prints whole. `sd today --all`
lifts the cap for every section. An empty section
is its heading alone. The one-line summary, the gate line, Delivered's
`+N closed by hand` line and Decisions' `+N review questions wait` line are
plain lines.

Each table is ID, Repo, State, the section's own columns, Title and When,
except Operations, this machine: ID, Job and Finding, then the gate line.
Title takes the width the other columns leave, out of `width` (the terminal's,
else `DEFAULT_WIDTH`), and is cut to fit. When is a relative time ("3 d ago",
"in 2 h") or, where the moment matters (a landing, a lane start), a local time
with its zone label. Nothing reads a clock here: `now` comes from the caller, so
two runs on the same data and the same `now` print the same bytes.
"""

from __future__ import annotations

import datetime
from typing import Any, Callable, Iterable

#: Rows a capped section shows before its `… N more` line.
ROWS = 5
#: The width when the caller has no terminal to measure.
DEFAULT_WIDTH = 100
#: The narrowest Title column; a narrow terminal wraps rather than lose the title.
TITLE_MIN = 16
#: The widest any other column grows; longer text is cut, so Title keeps its room.
CELL_MAX = 32
#: Each section's heading.
HEADINGS = {"attention": "Attention", "operations": "Operations, this machine", "decisions": "Decisions", "landing": "Landing now", "queues": "Queues", "delivered": "Delivered",
            "planned": "Planned", "blocked": "Blocked", "unscheduled": "Unscheduled", "followups": "Followups",
            "work": "Work"}
#: The sections only `sd today --here` adds, after the rest (sd:8844).
HERE = {"issues": "Issues"}
#: The sections that print whole.
UNCAPPED = frozenset({"work", "queues"})

Cell = Callable[[dict[str, Any]], str]


def _moment(stamp: Any) -> datetime.datetime | None:
    if not stamp:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def local(stamp: Any, now: datetime.datetime) -> str:
    """`HH:MM MDT` on `now`'s local day, the date before it otherwise; `-` for no time."""
    moment = _moment(stamp)
    if moment is None:
        return "-"
    moment = moment.astimezone()
    same = moment.date() == now.astimezone().date()
    return moment.strftime("%H:%M %Z" if same else "%Y-%m-%d %H:%M %Z")


def relative(stamp: Any, now: datetime.datetime) -> str:
    """`5 m ago`, `3 h ago`, `2 d ago`, or `in ...` for a later moment; `-` for no time."""
    moment = _moment(stamp)
    if moment is None:
        return "-"
    seconds = (moment - now).total_seconds()
    size = abs(seconds)
    span = (f"{int(size // 60)} m" if size < 3600 else f"{int(size // 3600)} h" if size < 86400
            else f"{int(size // 86400)} d")
    return "now" if size < 60 else (f"in {span}" if seconds > 0 else f"{span} ago")


def day(due: Any, now: datetime.datetime, *, zone: bool = True) -> str:
    """A due date, with the zone label this machine has on that day unless `zone` is off, and `late` before
    `now`'s day."""
    try:
        noon = datetime.datetime.combine(datetime.date.fromisoformat(str(due)), datetime.time(12)).astimezone()
    except ValueError:
        return str(due or "-")
    late = " late" if noon.date() < now.astimezone().date() else ""
    return f"{due}{f' {noon.tzname()}' if zone else ''}{late}"


def days(due: Any, now: datetime.datetime) -> str:
    """A due date as days from `now`'s local day: `today`, `in 3 d`, `2 d ago`."""
    try:
        count = (datetime.date.fromisoformat(str(due)) - now.astimezone().date()).days
    except ValueError:
        return "-"
    return "today" if count == 0 else f"in {count} d" if count > 0 else f"{-count} d ago"


def repo_short(repo: Any) -> str:
    """A Repo cell: `~/repos/system` is `system`, `owner/name` is `name`, and none is `fleet`, as each item
    reference reads (`sd_db.repos.item_ref`, sd:5124)."""
    import sd_lib

    sd_lib.import_sd_db()  # the gateway first, as for every sd_db import here
    from sd_db.repos import NO_REPO, short

    return short(repo) or NO_REPO


def _state(row: dict[str, Any]) -> str:
    return str(row.get("status") or "-")


def columns(section: str, now: datetime.datetime) -> list[tuple[str, Cell]]:
    """The section's columns, in order: ID, Repo, State, its own, Title, When."""
    def when(key: str, how: Callable[[Any, datetime.datetime], str] = relative) -> Cell:
        return lambda row: how(row.get(key), now)

    own: dict[str, list[tuple[str, Cell]]] = {
        "attention": [("Why", lambda row: row.get("why") or "-")],
        "decisions": [("Note", lambda row: f"#{row['note']}")],
        "landing": [("Position", lambda row: str(row["place"]) if row.get("place") else "-")],
        "queues": [("Queue", lambda row: row["queue"]), ("Detail", lambda row: str(row.get("detail") or "-"))],
        "delivered": [("PR", lambda row: f"#{row['pr']}" if row.get("pr") else "-"),
                      ("Commit", lambda row: (row.get("commit") or "-")[:9])],
        "planned": [("Due", lambda row: day(row["due"], now))],
        "blocked": [("Waiting on", lambda row: row.get("why") or "-")],
        "unscheduled": [("Priority", lambda row: f"P{row['priority']}" if row.get("priority") is not None else "-")],
        "followups": [("Note", lambda row: f"#{row['note']}")],
        "issues": [("Why", lambda row: row.get("why") or "-")],
        "work": [("Due", lambda row: day(row["due"], now, zone=False) if row.get("due") else "-"),
                 ("Priority", lambda row: f"P{row['priority']}" if row.get("priority") is not None else "-")],
    }
    moment = {"attention": when("at"), "decisions": when("at"), "landing": when("since", local), "queues": when("since"),
              "delivered": when("at", local), "planned": when("due", days), "blocked": when("since"),
              "unscheduled": when("created_at"), "followups": when("at"), "work": when("status_since"),
              "issues": when("at")}[section]
    return [("ID", _id), ("Repo", _repo), ("State", STATES.get(section, _state)), *own[section],
            ("Title", _title), ("When", moment)]


def _id(row: dict[str, Any]) -> str:
    return f"#{row['id']}"


def _repo(row: dict[str, Any]) -> str:
    return repo_short(row.get("repo") or row.get("repository"))


def _title(row: dict[str, Any]) -> str:
    return str(row.get("title") or "")


def _lane_state(row: dict[str, Any]) -> str:
    """A queued entry's state; a stopped one names the step it stopped at."""
    return _state(row) + (f" ({row['step']})" if row.get("step") and not row.get("place") else "")


def _delivered_state(row: dict[str, Any]) -> str:
    return "merged" if row.get("pr") else "delivered"


#: The State cell where it is not the row's `status`.
STATES: dict[str, Cell] = {"landing": _lane_state, "delivered": _delivered_state}


def _cut(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[:max(1, width - 1)] + "…"


def widths(heads: list[str], rows: list[list[str]], width: int, flex: str = "Title") -> list[int]:
    """The column sizes `table` prints at; a cell longer than its column's size is cut.

    `flex` names the column that takes the width the others leave (`Title`
    unless a caller names another); every other column grows to `CELL_MAX`.
    """
    title = heads.index(flex) if flex in heads else None
    sizes = [max([len(head)] + [len(" ".join(row[index].split())) for row in rows]) for index, head in enumerate(heads)]
    sizes = [size if index == title else min(size, max(len(heads[index]), CELL_MAX)) for index, size in enumerate(sizes)]
    if title is not None:
        rest = sum(size for index, size in enumerate(sizes) if index != title) + 2 * (len(heads) - 1)
        sizes[title] = max(TITLE_MIN, min(sizes[title], width - rest))
    return sizes


def table(heads: list[str], rows: list[list[str]], width: int, flex: str = "Title") -> list[str]:
    """Aligned columns under a header row and a rule; the `flex` column takes the width the others leave."""
    sizes = widths(heads, rows, width, flex)

    def line(cells: Iterable[str]) -> str:
        return "  ".join(cell.ljust(size) for cell, size in zip(cells, sizes, strict=True)).rstrip()

    cut = [[_cut(cell, size) for cell, size in zip(row, sizes, strict=True)] for row in rows]
    return [line(heads), line("-" * size for size in sizes), *(line(row) for row in cut)]


def section_table(section: str, rows: list[dict[str, Any]], now: datetime.datetime, width: int, *,
                  show_all: bool = False) -> list[str]:
    """One section: its heading, its table capped at `ROWS` unless `UNCAPPED` or `show_all`, and the `… N more` line."""
    lines = [f"{({**HEADINGS, **HERE})[section]} ({len(rows)})"]
    if not rows:
        return lines
    shown = rows if show_all or section in UNCAPPED else rows[:ROWS]
    cells = columns(section, now)
    lines += table([head for head, _ in cells], [[cell(row) for _, cell in cells] for row in shown], width)
    if len(rows) > len(shown):
        lines.append(f"… {len(rows) - len(shown)} more")
    return lines


def gate_line(gate: dict[str, Any] | None, now: datetime.datetime) -> list[str]:
    """The machine's gate slots on one plain line; a failed read says unknown, never an idle gate.

    The load and its limit follow when the snapshot carries them, in `sd gate status`'s words.
    """
    if gate is None:
        return []
    if gate.get("error"):
        return [f"Gate slots: unknown ({gate['error']})"]
    holders, waiters = gate["holders"], gate["waiters"]
    held = ", ".join(f"{entry['label'] or 'unlabelled'} since {local(entry['since'], now)}" for entry in holders)
    queued = ", ".join(entry["label"] or "unlabelled" for entry in waiters[:3])
    load = ""
    if gate.get("load_max") is not None:
        limit = f"{gate['load_max']:g}" if gate["load_max"] > 0 else "off"
        load = f" · load {'/'.join(f'{value:.1f}' for value in gate.get('load') or []) or 'unknown'}, limit {limit}"
    return [f"Gate slots: {len(holders)} holding" + (f" ({held})" if held else "")
            + f" · {len(waiters)} waiting" + (f" ({queued}{', …' if len(waiters) > 3 else ''})" if queued else "")
            + load]


def operations_table(found: dict[str, Any], now: datetime.datetime, width: int, *,
                     rows: int | None = ROWS) -> list[str]:
    """This machine's findings (`sd_db.today.operations`): heading, ID, Job and Finding, capped at `rows`, the gate line."""
    findings = found["findings"]
    lines = [f"{HEADINGS['operations']} ({len(findings)})"]
    shown = findings if rows is None else findings[:rows]
    if shown:
        lines += table(["ID", "Job", "Finding"], [[f"#{row['id']}", row["job"], row["finding"]] for row in shown],
                       width, "Finding")
    if len(findings) > len(shown):
        lines.append(f"… {len(findings) - len(shown)} more")
    return lines + gate_line(found["gate"], now)


def summary_line(summary: dict[str, int], keys: Iterable[str]) -> str:
    """`2 attention · 71 decisions · 3 landing · …`, over the sections `keys` names."""
    words = {"planned": ("planned_this_week", "planned this week"), "operations": ("operations", "on this machine")}
    parts = []
    for key in keys:
        count_key, word = words.get(key, (key, key))
        parts.append(f"{summary[count_key]} {word}")
    return " · ".join(parts)


def render_sections(found: dict[str, Any], *, now: datetime.datetime, width: int = DEFAULT_WIDTH,
           keys: Iterable[str] | None = None, show_all: bool = False) -> str:
    """`found` (from `sd_db.today.sections`) as text: the summary line, then each section in `keys`' order.

    `show_all` prints every row of every section, with no `… N more` line.
    """
    order = list(keys if keys is not None else [*HEADINGS, *(key for key in HERE if key in found)])
    lines = [summary_line(found["summary"], order)]
    for key in order:
        if key == "operations":
            lines += ["", *operations_table(found[key], now, width, rows=None if show_all else ROWS)]
            continue
        rows = found[key]["entries"] if key == "landing" else found[key]
        lines += ["", *section_table(key, rows, now, width, show_all=show_all)]
        if key == "landing" and (unread := found[key].get("unread")):
            lines.append(f"{unread} lane {'checkpoint' if unread == 1 else 'checkpoints'} unreadable")
        if key == "delivered" and found.get("closed_by_hand"):
            lines.append(f"+{len(found['closed_by_hand'])} closed by hand")
        if key == "decisions" and (batched := found["summary"].get("review_questions")):
            lines.append(f"+{batched} review {'question waits' if batched == 1 else 'questions wait'}"
                         " on the dashboard's Review page")
        if key == "decisions" and found["summary"].get("grill"):
            count, items = found["summary"]["grill"], ", ".join(f"sd:{row['id']}" for row in found["grill"])
            lines.append(f"Grill ready: {count} {'question' if count == 1 else 'questions'} on {items}"
                         "; say 'grill' in a fresh session")
    return "\n".join(lines) + "\n"
