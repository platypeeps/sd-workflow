"""The dashboard's default UI: the design source's shell, and the pages built on it so far (sd:2163).

The design source is ui-design `products/system/` (`design.md`, `designs/pages/`
and `foundation/`); `static/` holds its shell, tokens and fonts as the files the
dashboard policy allows (`default-src 'self'`: no inline style or script, no
font from another origin). `static/shell.js` marks each change from the
reference with `build:`.

A page is one HTML file beside this module, registered by its own module in
`pages/` (sd:2418): `PAGES`, `SECTIONS`, the classic screens it keeps, its API
routes (`api`, `action`) and the old screens it took the path of (`old`) are
built from that enumeration, so a port edits no line here. `dashboard.sh pages`
prints the map. The assets are what `static/` holds, enumerated from the
directory when the module loads: a file added there is served, and nothing
outside it can be.

Each asset is linked as `/ui/<name>?v=<digest>`, from `VERSIONS`, the map of
digests computed when this module loads (sd:2141): `page` adds the query to the
page's links, and `asset` adds it to a stylesheet's `url(...)` references, so a
font change changes the stylesheet's digest too. `sd_dashboard.caching` says
when the server may cache an answer for good. `/ui/sections.js` is the one
exception: the server writes it on each request (`sections`), unversioned and
`no-store`, because it carries every page's rail badge (sd:3446, `Badges`).

Since sd:2163 the pages are served at the root (`/today`) and the assets under
`/ui/`, a prefix no v1 route uses; v1 keeps `/static/`. A `/v2/` address that
names a page or an asset redirects to its new one (`moved`).
"""

from __future__ import annotations

import itertools
import json
import os
import re
import sys
import threading
from dataclasses import replace
from pathlib import Path

from .. import caching
from . import pages as registry

__all__ = ["ASSETS", "Badges", "CLASSIC", "PAGES", "SCREENS", "SECTIONS", "VERSIONS", "action", "api", "asset", "digests",
           "listing", "moved", "old", "page", "sections"]

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

#: Route path -> page file, from every registered page's routes. Today's include `/`: the new design is the default.
PAGES = {route: entry.html for entry in registry.PAGES for route in entry.routes}

#: The shell's map: a registered section opens its page's first route. A section in neither map has no old screen and
#: still says it is not built. CLASSIC holds the sections that open a server-drawn screen: none since Operations' areas
#: moved to the pages that hold their subjects (pack sd:7517). The rail draws one as any other section, with no tag.
#: The shell reads both beside the others as /ui/sections.js.
SECTIONS = {entry.section: entry.routes[0] for entry in registry.PAGES}
CLASSIC: dict[str, str] = {}
#: Old screens with no rail section of their own; the palette offers them under "Classic screens". Protection belongs to
#: no page (Management links it as the gap matrix). The Jobs, Ports, Repos and Progress areas were retired in sd:3402,
#: and Operations' other areas moved (pack sd:7517). An old screen a page keeps (its `classic`, with the reason in its
#: module) follows, by label.
SCREENS = {
    "Protection": "/protection",
} | dict(sorted((label, href) for entry in registry.PAGES for label, href in entry.classic.items()))

TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


def _enumerate() -> dict[str, str]:
    found = {}
    for directory, _, files in os.walk(STATIC):
        for name in files:
            path = Path(directory) / name
            kind = TYPES.get(path.suffix)
            if kind:
                found[path.relative_to(STATIC).as_posix()] = kind
    return found


#: Published name (relative to /ui/) -> content type, for the files in `static/`; `sections.js` is the server's.
ASSETS = _enumerate()


def sections(badges: dict | None = None, realms: list | None = None) -> bytes:
    """`/ui/sections.js`: the rail's section map, as `SHELL_COUNTS` each page's kept badge from `Badges.read`, and as
    `SHELL_REALMS` the realms the dashboard.realms row names (sd:5064), each `{id, label, place}`, which Tasks and Writing
    draw as views; `place` is the icon of a realm with a report family of its id, which the rail lists as a place (sd:5663)."""
    lines = ["// The rail's section map, written by sd_dashboard/v2/__init__.py from the page registry in sd_dashboard/v2/pages/ (sd:2418),",
             "// and each page's rail badge, written on this request (sd:3446)."]
    for key, value in (("SHELL_PAGES", SECTIONS), ("SHELL_CLASSIC", CLASSIC), ("SHELL_SCREENS", SCREENS),
                       ("SHELL_COUNTS", {"pages": badges or {}}),
                       ("SHELL_REALMS", [{"id": r["id"], "label": r["label"], "place": r.get("place")} for r in realms or []])):
        lines.append(f"window.{key} = {json.dumps(value)};")
    return ("\n".join(lines) + "\n").encode("utf-8")


STATES = ("ok", "caution", "warning", "unknown")


def _attention(document) -> dict | None:
    """A read document's `attention`, `{state, n, what}`, or None when it carries none of that shape."""
    found = document.get("attention") if isinstance(document, dict) else None
    if (not isinstance(found, dict) or set(found) != {"state", "n", "what"} or found["state"] not in STATES
            or type(found["n"]) is not int or found["n"] < 0 or not isinstance(found["what"], str)):
        return None
    return found


class Badges:
    """Each page's rail badge (sd:3446), one per server.

    A page names the read whose document carries its `attention` (`Page.count`). Every page shows what its last read
    carried, kept here with that read's time, so the rail never waits on the network, launchd, a slow file walk or a
    live read (sd:9310): a `live` page's read runs again after each `/ui/sections.js` request, on a thread of its own
    (`refresh_later`), and the next request shows it; every other page's read is the page's own. A failed or shapeless read leaves
    `unknown`, never the value before it, and a read that started earlier never replaces one that started later: each
    read takes a ticket as it starts, since two reads can start in one second. The values live in memory: a restarted
    server shows no such badge until the page is read again.
    """

    def __init__(self):
        self._last: dict[str, tuple[int, dict]] = {}
        self._lock = threading.Lock()
        self._tickets = itertools.count(1)
        self._worker: threading.Thread | None = None

    def ticket(self) -> int:
        """The order of a read that starts now."""
        with self._lock:
            return next(self._tickets)

    def remember(self, path: str, document, now: str, ticket: int | None = None) -> None:
        """Keep the badge in `document`, read at `now` from `path`, for the page that names `path` as its count.
        `ticket` is the read's, taken as it started; none means a read that starts and ends now."""
        page = next((entry for entry in registry.PAGES if entry.count == path and not entry.live), None)
        if page is None:
            return
        found = _attention(document) or {"state": "unknown", "n": 0, "what": f"{page.section} read failed"}
        with self._lock:
            ticket = next(self._tickets) if ticket is None else ticket
            if self._last.get(page.section, (0, None))[0] < ticket:
                self._last[page.section] = (ticket, {**found, "at": now})

    def read(self) -> dict:
        """Section -> badge: each page's last one, the live pages' from the last `refresh`. Runs no read."""
        with self._lock:
            return {section: dict(value) for section, (_, value) in self._last.items()}

    def refresh(self, read, connected: bool = True) -> None:
        """Run each live page's read now and keep its badge with `read.now`. With no database (`connected` false) a
        live page is unknown and its read does not run."""
        ticket = self.ticket()
        for entry in registry.PAGES:
            if not (entry.count and entry.live):
                continue
            badge = None
            if connected:
                try:
                    route = api(entry.count)[1]
                    answer = route.read(replace(read, path=entry.count))
                    status, document = answer if isinstance(answer, tuple) else (200, answer)
                    badge = _attention(document) if status == 200 else None
                except Exception as problem:  # noqa: BLE001 - one page's failed count must not take the others with it
                    print(f"rail badge for {entry.section} failed: {problem!r}", file=sys.stderr)
            found = badge or {"state": "unknown", "n": 0, "what": f"{entry.section} not read"}
            with self._lock:
                if self._last.get(entry.section, (0, None))[0] < ticket:
                    self._last[entry.section] = (ticket, {**found, "at": read.now})

    def refresh_later(self, run) -> None:
        """Start `run`, which calls `refresh`, on a thread, unless the one started before is still running."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=run, name="rail-badges", daemon=True)
            self._worker.start()

    def settle(self, timeout: float = 30.0) -> None:
        """Wait for the running refresh, if any (tests)."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)


def _stylesheet(name: str, body: bytes, versions: dict[str, str]) -> bytes:
    """A stylesheet's relative `url(...)` references, each with `?v=` when `versions` names its file."""
    folder = Path(name).parent

    def versioned(match: re.Match) -> str:
        target = (folder / match.group(2)).as_posix()
        version = versions.get(target)
        return match.group(0) if version is None else f"url({match.group(1)}{match.group(2)}?v={version}{match.group(1)})"

    text = body.decode("utf-8")
    return re.sub(r"url\((['\"]?)([A-Za-z0-9_][A-Za-z0-9_./-]*)\1\)", versioned, text).encode("utf-8")


def _served(name: str, root: Path, versions: dict[str, str]) -> bytes:
    """The bytes `/ui/<name>` answers with: the file, or a stylesheet with its references versioned."""
    body = (root / name).read_bytes()
    return _stylesheet(name, body, versions) if name.endswith(".css") else body


def digests(root: Path = STATIC) -> dict[str, str]:
    """Published name -> the digest of the bytes it is served as, for the assets under `root`.

    Stylesheets last: their bytes carry the digests of the files they name.
    """
    names = {name for name in ASSETS if (root / name).is_file()}
    versions = {name: caching.digest(_served(name, root, {})) for name in sorted(names) if not name.endswith(".css")}
    for name in sorted(names):
        if name.endswith(".css"):
            versions[name] = caching.digest(_served(name, root, versions))
    return versions


#: Every asset's digest when the server started; the pages link these (sd:2141).
VERSIONS = digests()


def page(path: str) -> str | None:
    """The page at a route, its asset links carrying `?v=`, or None when the path is not one."""
    name = PAGES.get(path)
    return None if name is None else caching.with_icons(
        caching.link((HERE / name).read_text(encoding="utf-8"), "/ui/", VERSIONS))


def api(path: str):
    """The registered page and API route that answer a GET at `path`, or None."""
    for entry in registry.PAGES:
        for route in entry.api:
            if route.read and route.matches(path):
                return entry, route
    return None


def action(path: str):
    """The registered write that answers a POST at `path`, or None; a write v1 posts too stays in `action_route`."""
    for entry in registry.PAGES:
        for route in entry.api:
            if route.write and route.matches(path):
                return route
    return None


def old(path: str):
    """The old screen a registered page took the path of, served at `path`, or None."""
    for entry in registry.PAGES:
        for screen in entry.takes:
            if screen.path == path:
                return screen
    return None


def listing() -> list[str]:
    """The rail and palette map, one tab-separated line per target, as `dashboard.sh pages` prints it.

    A registered page is `section, address, new, item`; an unported section is `section, address, classic`; an old
    screen the palette offers is `label, address, palette`.
    """
    return ([f"{entry.section}\t{entry.routes[0]}\tnew\t{entry.item}" for entry in registry.PAGES]
            + [f"{section}\t{href}\tclassic" for section, href in CLASSIC.items()]
            + [f"{label}\t{href}\tpalette" for label, href in SCREENS.items()])


def asset(name: str) -> tuple[bytes, str] | None:
    """One file from `static/`, by its exact published name, or None."""
    kind = ASSETS.get(name)
    if kind is None:
        return None
    return _served(name, STATIC, VERSIONS), kind


def moved(path: str) -> str | None:
    """The new address of a `/v2/` page or asset, or None when the path names neither.

    Only a page or asset that exists redirects, so a crafted path such as
    `/v2//example.test` can never become a Location to another host.
    """
    if path.startswith("/v2/static/"):
        name = path[len("/v2/static/") :]
        return f"/ui/{name}" if name in ASSETS or name == "sections.js" else None
    if path.startswith("/v2/"):
        target = "/" + path[len("/v2/") :]
        return target if target != "/" and target in PAGES else None
    return None
