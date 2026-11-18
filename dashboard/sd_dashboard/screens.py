"""Item, with all reads and writes owned by sd_db.

Every row comes from `sd_db.reads`. Controls render the shared workflow's
allowed transitions and current revision; they contain no independent status
rules or SQL. Tasks took the Backlog, and its run selection, in sd:2622; the
old Today was retired in sd:3402, once the v2 Today carried its capture and
its rows.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from sd_db import reads, workflow

from . import controls
from .listing import Column, Listing
from .markup import Markup, join, markdown, tag
from .pages import command_reference, shell_page

__all__ = ["item"]

sdpaths = None  # bound at the end of this module (sd:1439)


def _repo_labels(paths):
    """Shortest distinct suffix; full paths remain in filters and item details."""
    parts = {path: Path(path).parts for path in set(paths) if path}
    labels = {}
    for path, components in parts.items():
        for depth in range(1, len(components) + 1):
            suffix = components[-depth:]
            if sum(other[-depth:] == suffix for other in parts.values()) == 1:
                labels[path] = path if suffix[0] == "/" else "/".join(suffix)
                break
        else:
            labels[path] = path
    return lambda path: labels.get(path, "—")


def readable_time(stamp):
    if not stamp:
        return Markup("—")
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        fallback = moment.strftime("%b %-d, %Y · %-I:%M %p %Z")
    except (ValueError, AttributeError):
        return tag("span", stamp)
    return tag("time", fallback, datetime=stamp, data_local_time=True)


def _history_text(body):
    return re.sub(r"\b(in_progress|ready_to_send)\b", lambda match: match[0].replace("_", " "),
                  body).replace(" -> ", " → ")


# --------------------------------------------------------------------------
# Item
# --------------------------------------------------------------------------


def _without_frontmatter(text: str) -> str:
    return re.sub(r"\A---\r?\n.*?\r?\n---(?:\r?\n|$)", "", text, count=1, flags=re.S)


def artifact(row, *, document=None) -> Markup:
    """The item's artifact, read from git and rendered through the subset.

    Read at `source_commit` where the row has one, because that is the commit
    the row was landed from and the working copy may have moved since. What
    comes back is never markup: it goes through `markdown()`, which escapes
    first and adds only the tags it wrote.
    """
    repo, path = row["repo"], row["path"]
    if row["kind"] == "idea" and row["piece"] is not None:
        if document is not None:
            document = _without_frontmatter(document)
        return (markdown(document) if document is not None else
                tag("p", "The current draft could not be read. See the review checks above.", class_="artifact-missing"))
    if row["kind"] in (*workflow.HAND_KINDS, "report"):
        # `followup` is here since sd:816. `workflow._fields` stores a followup's
        # body as `{"text": ...}` exactly as it stores a task's, and every
        # followup in the store carries that shape, but this decode named three
        # kinds and not four -- so a followup showed its stored JSON, braces and
        # escaped quotes and all, where its text belongs.
        #
        # The other hand kinds are here for the same reason (PR #332 review).
        # `work-idea` and `personal-idea` are created through
        # `capture_task` and reclassified from a task by the item screen's
        # kind select, and both leave the `{"text": ...}` body in place; a
        # task that became one of them showed its braces the moment it did.
        # The set is `HAND_KINDS` rather than four names so a hand kind added
        # later decodes here without a second edit.
        try:
            body = json.loads(row["body"] or "{}")
        except ValueError:
            # sd:870, the twin of sd:775's hole. `item.body` is plain `TEXT`
            # with no `json_valid` check on it, so a row this cannot parse is a
            # row the store permits. Unguarded, `json.JSONDecodeError` -- a
            # `ValueError` -- raised through `screens.item` and past `do_GET`,
            # which catches `MissingItem`, `NotFound` and the database errors
            # and not this, so the reader got no response at all. Fail closed
            # the way sd:775 settled: render the page, say plainly why the text
            # is missing, and name the command that shows the stored bytes.
            return tag("p", "This item's stored body is not valid JSON, so its text cannot be "
                            "shown here. Read what is stored with ",
                       tag("code", f"sd store item {row['id']}"), ".", class_="artifact-missing")
        text = body.get("text", "") if isinstance(body, dict) else ""
        # A routine brief (sd:3459) is a note with frontmatter: its text renders as a piece's draft does.
        return markdown(_without_frontmatter(text) if row["source"] == "brief" and isinstance(text, str) else text)
    if not repo or not path:
        return markdown(row["body"])
    commit = row["source_commit"] or "HEAD"
    try:
        done = subprocess.run(
            ["git", "-C", str(sdpaths.disk(repo)), "show", f"{commit}:{path}"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return markdown(row["body"])
    if done.returncode != 0:
        return tag(
            "p",
            f"The artifact at {path} could not be read from {commit}.",
            class_="artifact-missing",
        )
    return markdown(done.stdout)


def item(connection, item_id: int, *, now: str, parameters) -> str | None:
    row = reads.item_by_id(connection, item_id)
    if row is None:
        return None
    writing_state = None
    if row["kind"] == "idea" and row["piece"] is not None:
        from sd_db import writing

        writing_state = writing.piece_state(connection, item_id)
    notes = reads.item_notes(connection, item_id)
    changes = reads.status_changes(connection, item_id)
    assignments = reads.item_assignments(connection, item_id)
    shadow = reads.item_shadow(connection, item_id)
    revision = workflow.item_state(connection, item_id)["revision"]
    query, page_number, selected = Listing.read_query(parameters)
    from .palette import note_summary
    execution_notes = {note["id"]: note_summary(note) for note in notes if note["kind"] == "exec"}

    def note_body(note, *, text=False):
        if note["kind"] == "exec":
            return execution_notes[note["id"]][1 if text else 0]
        return _history_text(note["body"]) if note["kind"] == "status_change" else note["body"]

    note_list = Listing(
        name="notes",
        path=f"/item/{item_id}",
        query=query,
        page_number=page_number,
        selected=selected,
        rows=notes,
        columns=[
            Column("when", "When", lambda note: readable_time(note["timestamp"])),
            Column("kind", "Kind", lambda note: note["kind"].replace("_", " ")),
            Column("body", "Note", note_body, text=lambda note: note_body(note, text=True)),
            Column("state", "State",
                   lambda note: controls.note_resolution(note, revision),
                   text=lambda note: "resolved" if note["resolved_at"] else "open"),
            Column("session", "Session", lambda note: (note["session"] or "—"), hidden=True),
        ],
        empty="No notes on this item.",
    )

    # Criterion 7's clause at `prd.md:1303-1307`: the item screen lists the
    # status changes, in order, each with old, new and time. They are notes,
    # so they are in the list above too; this is the reading of them, because
    # "the history of this item's status" is a question with its own answer.
    history = tag(
        "ol",
        join(
            tag(
                "li",
                readable_time(note["timestamp"]),
                " ",
                tag("span", _history_text(note["body"]), class_="status-change"),
                class_="history-entry",
                data_status_change=note["id"],
            )
            for note in changes
        ),
        class_="history",
    ) if changes else tag("p", "This item has never changed status.", class_="history")

    assignment_rows = tag(
        "table",
        tag(
            "thead",
            tag("tr", join(tag("th", label, scope="col") for label in
                           ("Assignment", "Role", "Provider", "Status", "Started",
                            "Ended", "Cost"))),
        ),
        tag(
            "tbody",
            join(
                tag(
                    "tr",
                    tag("td", assignment["id"]),
                    tag("td", assignment["role"]),
                    tag("td", assignment["provider"] or "—"),
                    tag("td", assignment["status"].replace("_", " ")),
                    tag("td", readable_time(assignment["started"])),
                    tag("td", readable_time(assignment["ended"])),
                    tag("td", f"${assignment['usd']:,.2f}"
                        + (" (estimated)" if assignment["estimated"] else "")),
                )
                for assignment in assignments
            ),
        ),
        class_="listing-table",
    ) if assignments else tag("p", "No assignments.", class_="empty")

    shadow_block = external_context(connection, row, shadow, now=now)
    kind_label = "writing" if writing_state else row["kind"]
    status_label = row["stage"] if writing_state else row["status"]

    from .operations_screen import assignment_cancel
    from .reports_screen import report_controls
    from .skills_screen import review_controls

    return shell_page(
        row["title"],
        "Tasks",
        *[
            command_reference(f"sd store item {item_id}"),
            completion_summary(row),
            controls.writing_controls(connection, row, state=writing_state),
            controls.item_controls(connection, item_id),
            controls.progress_controls(connection, item_id),
            join(assignment_cancel(connection, one["id"]) for one in assignments),
            review_controls(connection, row, revision),
            report_controls(connection, row, revision),
            tag("h2", "Status history"),
            history,
            tag("h2", "Artifact"),
            tag("article", artifact(row, document=writing_state["writing"]["document"] if writing_state else None), class_="artifact"),
            tag("h2", "Notes"),
            controls.note_capture(connection, item_id),
            note_list.render(),
            tag("h2", "Assignments"),
            assignment_rows,
            shadow_block,
        ],
        subtitle=f"#{item_id} · {kind_label} · {status_label.replace('_', ' ')} · {row['repo'] or 'no repository'}",
        note=f"#{item_id} on its own page: Tasks lists open items and those done this week, and an item it does not list "
             "opens here. Every control, record and note for the item is on the left.",
        screen="item",
    )


def completion_summary(row):
    from sd_db import progress

    if row["kind"] != "work" or row["status"] != "done":
        return Markup("")
    receipt = progress.completion_record(row)
    if not receipt:
        return tag("p", "Completed; no delivery or cancellation receipt is recorded here.", class_="hint")
    if receipt["outcome"] == "cancelled":
        return tag("section", tag("h2", "Cancelled"), tag("p", receipt["reason"]),
                   readable_time(receipt["at"]), class_="control-panel")
    return tag("section", tag("h2", "Delivered"),
               tag("p", "Delivery is recorded against a verified commit."),
               readable_time(receipt["at"]), class_="control-panel")


def external_context(connection, row, shadow, *, now):
    """External state is dated context beside the local item's own status."""
    from sd_db import progress

    if shadow is None and row["source"] not in ("github", "jira"):
        return Markup("")
    url = shadow["url"] if shadow else row["external_id"]
    if not url:
        return Markup("")
    tracker = shadow["tracker"] if shadow else row["source"]
    freshness = progress.tracker_freshness(connection, tracker=tracker,
                                          now=datetime.fromisoformat(now.replace("Z", "+00:00")))
    try:
        parsed = urlsplit(url)
        safe = parsed.scheme in ("https", "http") and bool(parsed.netloc)
    except ValueError:
        safe = False
    link = (tag("a", url, href=url, rel="noopener noreferrer") if safe else tag("span", url))
    return tag("section", tag("h2", "External context"),
        tag("p", "Local progress is tracked here. External tracker state is context and does not hold this task open.", class_="hint"),
        tag("dl",
            tag("div", tag("dt", "Tracker"), tag("dd", tracker)),
            tag("div", tag("dt", "External state"), tag("dd", shadow["state"] or "Unknown" if shadow else "Not in the local snapshot")),
            tag("div", tag("dt", "Reference"), tag("dd", link)),
            tag("div", tag("dt", "Last seen"), tag("dd", readable_time(shadow["last_seen"]) if shadow else "Never")),
            tag("div", tag("dt", "Sync health"), tag("dd", freshness["state"])),
            tag("div", tag("dt", "Last successful sync"), tag("dd", readable_time(freshness["last_success_at"]) if freshness["last_success_at"] else "Never")),
            class_="shadow"),
        tag("p", freshness["reason"], class_="hint") if freshness["reason"] else Markup(""),
        class_="secondary-panel")


# A library before schema 14 (sd:1439) has no `sd_db.paths` and stores every
# repository path absolute. The services run from this checkout, so a pull can
# reach them before the library moves; against that library a key is the path.
try:
    from sd_db import paths as sdpaths
except ImportError:
    from types import SimpleNamespace as _Namespace
    sdpaths = _Namespace(
        key=lambda value: value,
        disk=lambda value: Path(value).expanduser(),
        same=lambda left, right: left is not None and right is not None and (
            Path(left).expanduser().resolve() == Path(right).expanduser().resolve()),
    )
