"""Today (sd:2110, the default since sd:2163): /api/now as one page, served from server.py's chain. The old Today, kept
under /classic/ when this page took `/`, was retired in sd:3402. Decisions moved to Decide and GitHub with Your issues to
GitHub (sd:5781). The Status view and its /api/today/status went with the tabs (sd:5921): `sd today` prints the sections.

The Queue view (pack sd:7234; the Queue page until then, sd:2585) reads each repository's ship lane from /api/queue
(`queue_screen`). Up, down, top, hold, release and cancel, a blocked row's retry and approve, a cancelled row's Re-queue
and a running row's Release post /api/queue/move, which runs `sd-ship lane move|hold|release|cancel|retry` and nothing
else. The build runs read /api/queue/builds (`builds.panel`), with ?show=all the ended runs too (pack sd:6834); a blocked
or cancelled run's Retry posts /api/queue/builds/retry, and its Send to builder posts the same route with a `finding`
(pack sd:6020); a running build's Cancel posts the assignment cancel route that exists. Auto-pickup's On and Off post
/api/queue/builds/pickup (`builds.pickup`, pack sd:6097). The runner assignments (pack
sd:7115, sd:7217) read /api/queue/runner (`queue_screen.runner`). Read again on the Queue view posts
/api/queue/prs/check first (`queue_screen.check_prs`, sd:8375): a merged pull request notes its item."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import queue_screen

    return queue_screen.document(read.connection, now=read.now)


def _builds(read):
    from ... import builds

    shown = (read.parameters.get("show") or ["active"])[0]
    return builds.panel(read.connection, now=read.now, show="all" if shown == "all" else "active")


def _runner(read):
    from ... import queue_screen

    return queue_screen.runner(read.connection)


def _retry(payload, principal):
    from ... import builds

    finding = payload.get("finding", "")
    if (set(payload) - {"finding"} != {"assignment", "revision"} or type(payload["assignment"]) is not int
            or not 1 <= payload["assignment"] <= 9223372036854775807 or not isinstance(payload["revision"], str)):
        raise ValueError("Retry takes the lead assignment and the revision the panel read.")
    if "finding" in payload and (not isinstance(finding, str) or not finding.strip()
                                 or len(finding) > builds.FINDING_MAX):
        raise ValueError(f"Send to builder takes a finding of 1 to {builds.FINDING_MAX} characters.")

    def write(connection):
        out = builds.retry(connection, payload["assignment"], expected_revision=payload["revision"], who="dashboard",
                           finding=finding.strip())
        return {**out, "kickstart": builds.kickstart()}

    return write


def _pickup(payload, principal):
    from ... import builds

    return builds.pickup(payload)


def _check_prs(payload, principal):
    from ... import queue_screen

    return queue_screen.check_prs(payload)


def _move(payload, principal):
    from ... import queue_screen

    return queue_screen.move(payload)


PAGE = Page(
    section="Today",
    routes=("/today", "/"),
    html="today.html",
    item="sd:2110",
    api=(Api("/api/queue", _read), Api("/api/queue/move", write=_move), Api("/api/queue/builds", _builds, query=True),
         Api("/api/queue/builds/retry", write=_retry), Api("/api/queue/builds/pickup", write=_pickup),
         Api("/api/queue/runner", _runner), Api("/api/queue/prs/check", write=_check_prs)),
    # server.py serves /api/now and keeps each read's badge for the rail (sd:3446).
    count="/api/now",
)
