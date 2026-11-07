"""Writing (sd:2125): every writing piece by stage, with its four gate lamps and its moves. The old filtered list, kept
under /classic/, was retired in sd:3402; Stage, Correct, Park and Revive post the item routes the item page posts
too. A gate's view icon reads its report at /api/writing/<item>/<gate> (pack sd:7242)."""

from __future__ import annotations

from . import Api, Page

#: writing_screen.REPORT; this module imports no screen while the server loads.
REPORT = r"/api/writing/([1-9][0-9]{0,17})/(research|fact-check|adversarial)"


def _read(read):
    from ... import writing_screen

    return writing_screen.document(read.connection, now=read.now)


def _report(read):
    from ... import writing_screen

    return writing_screen.report(read.connection, read.path)


def _decline(payload, principal):
    from ... import triage

    return triage.decline(payload, principal)


PAGE = Page(
    section="Writing",
    routes=("/writing",),
    html="writing.html",
    item="sd:2125",
    api=(Api("/api/writing", _read),
         Api(pattern=REPORT, example="/api/writing/1/research", read=_report),
         Api("/api/triage/decline", write=_decline)),
    count="/api/writing",
    live=True,
)
