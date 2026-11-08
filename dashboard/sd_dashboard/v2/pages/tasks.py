"""Tasks (sd:2124): the page's rows and one item's Details, read as v1 /backlog and /item/<id> read them. The page
takes v1's status, age, active, q and page filters, which Operations > Progress linked until sd:3402 (sd:2589). A /backlog address
opens this page with its query (sd:2356). The Personal and Work views' Run sweep posts /api/tasks/sweep (sd:5096,
sd:5097), which starts that realm's sweep in the background; GET /api/tasks/sweep/status reads both realms' last runs.
Start next (sd:5107) reads its plan at `/api/tasks/start-next` and posts the items it showed to
`/api/tasks/start-next/run` (`start_next`). The palette's why search (pack sd:6019) reads `/api/why?q=`: a path or a
rule's text, to the items whose pages say why (`why_search`). Build (pack sd:6095) reads its dialog's plan at
`/api/tasks/build/plan?items=1,2` and posts the items it showed, with their revisions, to `/api/tasks/build` (`builds`).
The decision queues (pack sd:7527) are one line on Today's Needs you now (sd:8013, `now_screen.triage_rows`)."""

from __future__ import annotations

from . import Api, Page


def _rows(read):
    from ... import tasks_screen

    return tasks_screen.document(read.connection, now=read.now)


def _start_plan(read):
    from ... import start_next

    return start_next.plan(read.connection, now=read.now)


def _start(payload, principal):
    from ... import start_next

    return start_next.request(payload)


def _build_plan(read):
    from ... import builds

    asked = read.parameters.get("items", [])
    if set(read.parameters) - {"items"} or len(asked) != 1:
        return 400, {"error": "Build takes one items: the item ids, comma-separated."}
    try:
        items = builds.items_of([int(part) for part in asked[0].split(",")])
    except ValueError:
        return 400, {"error": "Select one to 50 distinct item ids to build."}
    return builds.plan(read.connection, items, now=read.now)


def _build(payload, principal):
    from ... import builds

    return builds.press(payload)


def _details(read):
    from ... import tasks_screen

    number = int(read.path.rsplit("/", 1)[1])
    if number > 9223372036854775807:
        return 404, {"error": "No such item."}
    return tasks_screen.details(read.connection, number, now=read.now)


def _why(read):
    from ... import why_search

    asked = read.parameters.get("q", [])
    if set(read.parameters) - {"q"} or len(asked) != 1:
        return 400, {"error": "Search takes one q: a file path or a rule's text."}
    return why_search.search(read.connection, asked[0])


def _sweeps(read):
    from ... import sweep

    return {realm: sweep.status(None, realm) for realm in sweep.REALMS[1:]}


def _run_sweep(payload, principal):
    from ... import sweep

    return sweep.request_realm(dict(payload))


PAGE = Page(
    section="Tasks",
    routes=("/tasks",),
    html="tasks.html",
    item="sd:2124",
    api=(Api("/api/tasks", _rows), Api("/api/tasks/start-next", _start_plan), Api("/api/tasks/start-next/run", write=_start),
         Api("/api/tasks/build/plan", _build_plan, query=True), Api("/api/tasks/build", write=_build),
         Api(pattern=r"/api/tasks/[1-9][0-9]{0,18}", example="/api/tasks/1", read=_details),
         Api("/api/tasks/sweep", write=_run_sweep), Api("/api/tasks/sweep/status", _sweeps),
         Api("/api/why", _why, query=True)),
    count="/api/tasks",
    live=True,
)
