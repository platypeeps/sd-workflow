"""The v2 pages, one module each (sd:2418).

Each module beside this one defines `PAGE`, a `Page`: the rail section it
fills, its routes and HTML file, its API routes, and any old screen it keeps in
the palette or takes the path of. The modules are enumerated when this package
loads, as `v2` enumerates `static/`, so a port adds a module here and edits no
shared line: `v2.PAGES`, `v2.SECTIONS`, `/ui/sections.js`, the server's API
dispatch and the old-screen routes are all built from `PAGES`.

A module imports its screen on first use, inside the function that reads it,
as `server.py` did: enumerating the pages loads no collector.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable

__all__ = ["Api", "Old", "PAGES", "Page", "Read", "attention", "collect", "place_views"]


@dataclass(frozen=True)
class Read:
    """What one request hands a page's read, write or old-screen render."""

    connection: Any = None
    now: str = ""
    path: str = ""
    parameters: dict = field(default_factory=dict)
    fleet: Any = None
    jobs: Any = None
    services: Any = None
    ports: Any = None
    #: The server's kept Metrics parts (sd:4955), `metrics_screen.SLOW` name -> document; None reads them now.
    metrics: Any = None
    #: The checkout commit the server started on (sd:3528), for Health's dashboard row.
    served_commit: Any = None
    #: Closes `connection` and gives the request's read slot back, once (sd:7898): a read that then waits on no store,
    #: such as Health's walkers, calls it first. None for a read the server did not open.
    release: Any = None


@dataclass(frozen=True)
class Api:
    """One API route. `read(read)` answers a GET with a document (a 200) or `(status, document)`.

    `write(payload, principal)` is a POST, with `action_route`'s contract: it returns a callable that takes a write
    connection, or raises `ValueError`. Only a write the page alone posts registers here; one v1 also posts stays in
    `action_route`. `pattern` is a full-match regex for a path with an id, and `example` a path it matches, which the
    registry test requests. A route refuses a query string unless `query` is true.
    """

    path: str = ""
    read: Callable | None = None
    write: Callable | None = None
    pattern: str = ""
    example: str = ""
    query: bool = False

    def matches(self, path: str) -> bool:
        return path == self.path if self.path else re.fullmatch(self.pattern, path) is not None


@dataclass(frozen=True)
class Old:
    """An old screen whose path the new page took, served at `path` by `render(read)`."""

    path: str
    render: Callable[[Read], str]


@dataclass(frozen=True)
class Page:
    """One v2 page. `routes[0]` is the address the rail opens; `classic` maps a palette label to an old screen."""

    section: str
    routes: tuple[str, ...]
    html: str
    item: str
    api: tuple[Api, ...] = ()
    classic: dict[str, str] = field(default_factory=dict)
    takes: tuple[Old, ...] = ()
    #: The rail badge (sd:3446): the path of the read whose document carries the page's `attention`, `{state, n, what}`,
    #: the one place its rule lives. `live` runs that read after each `/ui/sections.js` request, off the request (sd:9310),
    #: for a page that reads the database alone; any other page shows what its own last read carried (`v2.Badges`). Empty: the page has no badge.
    count: str = ""
    live: bool = False
    name: str = ""


def attention(states, what: dict[str, str]) -> dict:
    """A page's rail badge from its rows' states (sd:3446): the louder of warning and caution, and how many rows carry it.

    `what` names the count for `warning` and `caution`; an `ok` badge counts nothing and says what was looked for.
    """
    states = list(states)
    warning, caution = states.count("warning"), states.count("caution")
    state = "warning" if warning else "caution" if caution else "ok"
    return {"state": state, "n": warning or caution, "what": what.get(state, what["caution"])}


def place_views(area: str, kinds=("tasks", "briefs", "reports")) -> tuple[Api, ...]:
    """A place page's Tasks, Briefs and Reports reads (`places`), at `/api/<area>/<view>`: each view its own document."""

    def view(kind: str) -> Api:
        def read(read: Read):
            from ... import places

            # A Reports row's Details read the jobs as the Reports page does (sd:5636).
            return getattr(places, kind)(read.connection, area, now=read.now, **({"jobs": read.jobs} if kind == "reports" else {}))

        return Api(f"/api/{area}/{kind}", read)

    return tuple(view(kind) for kind in kinds)


def collect(paths, package: str) -> tuple[Page, ...]:
    """Every module's `PAGE` under `paths`, in module-name order; a module without one fails the import."""
    found = []
    for module in sorted(name for _, name, _ in pkgutil.iter_modules(paths)):
        page = getattr(importlib.import_module(f"{package}.{module}"), "PAGE", None)
        if not isinstance(page, Page):
            raise ImportError(f"{package}.{module} defines no PAGE: every module in the page registry is one page")
        found.append(replace(page, name=module))
    return tuple(found)


#: Every registered page.
PAGES = collect(__path__, __name__)
