"""Help (sd:10917): the phrases a session answers to, from skills/sd-next/SKILL.md, and how the fleet works, from
`v2/help.md` with its live values read now (`help_screen`). Read-only: the page posts nothing."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import help_screen

    return help_screen.document(read.connection, now=read.now)


PAGE = Page(
    section="Help",
    routes=("/help",),
    html="help.html",
    item="sd:10917",
    api=(Api("/api/help", _read),),
)
