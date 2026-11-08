"""Operations moved (pack sd:7517): where each of its areas went, and the helpers other screens import.

Every area moved to the page that holds its subject. The Jobs, Ports, Progress, Reports, Repos and Commands areas were
retired in sd:3402, once Management, Health, Metrics, Reports and Activity carried their controls; an address naming
one is a 404. Services and Sessions moved to Management in pack sd:7517, Usage to Metrics in pack sd:7524, Resources'
four views to Management, Notes and Tasks in pack sd:7527, and Trackers to GitHub's Tracked view in pack sd:7525
(`MOVED`, `MOVED_RESOURCES`): `server` answers an address naming one with a 301 to the view that holds it now
(`moved_to`). The bare address draws `moved_page`, a list of those homes, since only the browser sees an old
`#view=<area>`: `/ui/operations.js` follows the link that names it, and Trackers' with none, the area it opened on.
"""

import contextlib
import re
import signal

from sd_db import operations

from .controls import form
from .markup import join, tag
from .pages import shell_page

#: Each retired area, the view that holds it now and what the moved page says of it (pack sd:7517).
MOVED = {"services": ("/management#view=deploys", "Services: Management, Deploys"),
         "sessions": ("/management#view=sessions", "Sessions: Management, Sessions"),
         "usage": ("/metrics#view=spend", "Usage: Metrics, Spend"),
         "resources": ("/management#view=machine", "Resources: Management, Machine; research, vault and queues on "
                                                   "Repos, Notes and Today"),
         "trackers": ("/github#view=tracked", "Trackers: GitHub, Tracked")}
#: Resources' views and the page that holds each now (pack sd:7527): `?area=resources&resource=<view>` answers 301
#: there, and a bare or unknown one to Toolbox's, the view Resources opened on.
MOVED_RESOURCES = {"toolbox": "/management#view=machine", "research": "/management?research=yes#view=repos",
                   "vault": "/notes", "queues": "/today"}


def assignment_cancel(connection, identity: int):
    """`sd assignments cancel` as a form, where `operations.cancel_assignment` takes it; else nothing.

    The runner's cancel went with the runner (sd:3041); this one ends a queued row with no process to stop.
    """
    state = operations.assignment_state(connection, identity)
    if not state["capabilities"]["cancel"]["allowed"]:
        return ""
    return form(f"/api/assignments/{identity}/cancel", label="Cancel assignment",
                command=f"sd assignments cancel {identity}", revision=state["revision"], compact=True)


def _signal_name(number):
    """`SIGTERM (15)` when Python knows the number, the bare number otherwise."""
    try:
        return f"{signal.Signals(number).name} ({number})"
    except ValueError:
        return str(number)


@contextlib.contextmanager
def _one_snapshot(connection):
    """One deferred read transaction around a page's reads, so they compare one snapshot of the store.

    Tasks, Places, HOA and Reports read through it. Deferred: the connection is read-only and `isolation_level=None`,
    and WAL keeps a reader's snapshot from its first read to its COMMIT. Inside a caller's transaction it adds none.
    """
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN")
    try:
        yield
    finally:
        connection.execute("COMMIT")


def moved_to(parameters):
    """The address a retired area's bookmark answers 301 to, or None for no area or one that never moved.

    Usage's month rides along when it is one (pack sd:7524): Metrics reads `?month=YYYY-MM`.
    """
    area = (parameters.get("area") or [""])[0]
    if area == "resources":
        return MOVED_RESOURCES.get((parameters.get("resource") or [""])[0], MOVED["resources"][0])
    target = MOVED[area][0] if area in MOVED else None
    month = (parameters.get("month") or [""])[0]
    if target and area == "usage" and re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        return target.replace("#", f"?month={month}#")
    return target


def moved_page():
    """`/operations` with no area: a link to each area's new home, which `/ui/operations.js` follows for an old
    `#view=<area>` (Trackers' for none), so a bookmark the server cannot read still lands where its area went."""
    return shell_page("Operations moved", "Today", tag("ul", join(
        tag("li", tag("a", words, href=href, data_view=area)) for area, (href, words) in MOVED.items())),
        subtitle="Each area moved to the page that holds its subject.", screen="today", cli_equivalents=False,
        note="Operations' areas moved to Management, Metrics, Notes, Tasks and GitHub.", script="operations.js")
