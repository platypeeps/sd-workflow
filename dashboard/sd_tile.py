"""The system dashboard's legacy views, one tab per invocation.

The framework dashboard replaced this one, and six of these views did not go
with it: Toolbox, Vault, Research and Ports read the vault, the
launchd job table and this machine's service map, and all of them stay
system-owned. So they reached that dashboard through the plugin contract
instead of being ported into it -- the collectors below are `collectors.py`'s
own, imported rather than copied, and this file is only the part that turns
them into a tab. The pack dashboard is no longer served and its plugin loader
is gone (sd:719). The workflow dashboard in `sd_dashboard/` renders the views
now: `reports_screen.collect` runs this file as a child for Toolbox
(Management's Machine view), Research (its Repos filter) and Vault (Notes),
pack sd:7527, and Health calls `collect_ports` in-process (`ports_screen.py`).
Queues left at sd:8013: Today's Needs you counts the decision queues.

Five when this file was written; Queues joined at 6b-6, and the count is now
`TABS` rather than a number in a sentence. A new view is a builder and its
line in that dict, plus its name and ceiling in `reports_screen.py` (`VIEWS`,
`VIEW_SECONDS`). Briefs left at sd:3459: the Briefs page reads the workflow
database, not the vault.

**One invocation per tab.** A Resources view runs `sd_tile.py <name> <since>` once per
view (`<since>` is the view's start on `CLOCK_MONOTONIC`; `main` takes the time
this interpreter spent starting from the deadline, sd:2501), under five seconds and 64KB, as the pack's loader ran
`dashboard.sh tile <name>` for each name the manifest declared. That is not a style
choice: run behind one command these collectors took 6.66s together and
would be killed on every load, permanently. When this was written, each of them
alone fitted the budget several times over.

Ports is the tight one. `collect_ports` declares 5 seconds, and here it is read
within `TILE_SECONDS - TILE_MARGIN`, four seconds; the whole tile measured
1.72-1.96s on 2026-09-13 (sd:756). The margin is there for a caller that holds
the tile to five seconds and kills it there: the collector stops its scan and
the tile exits 1 with the reason on stderr before those five seconds run out
(sd:722). The pack's loader was that caller, and a Resources view is that
caller now -- `collectors.Budget` kills the tile's process group at
`VIEW_SECONDS`. The kill is not hypothetical: on 2026-09-14, under load, the
Vault and Research views both returned "python ran past its budget of 5
seconds" in place of the view.

What no longer reaches this tab is `within`. `reports_screen.VIEWS` has no
ports entry, so no Resources view runs the ports tile, and the Ports area calls
`collect_ports` in-process instead (`ports_screen.py`); the only caller that
still passes `within` is `dashboard.sh tile ports` from a shell, which applies
no outer limit of its own. The margin itself bounds every tab on every path
regardless -- `main` sets it as the deadline for the whole tab, not just for
Ports. The other five tabs were not re-measured then.

**Every command a tab runs stops at the same four seconds.** `main` sets that
deadline on the collectors it loads, and each nested command's own timeout is
capped at the time left (sd:760). Those commands run in sessions of their own,
which a kill of the tile's group does not reach: before this, a tab killed at
five seconds left its vault probe or `launchctl list` running with nobody to
stop it, and a TCC-held vault showed "ran past its budget" instead of the Full
Disk Access reason. Now the tile kills them itself and exits 1 with its reason.

**Data, not markup** (pack sd:7527). A tab prints `{title, sections, rows}`:
each section is one of `figures`, `heading`, `table` or `listing`, and a cell
is text with an optional title, http(s) link or numeric flag. The page that
shows the view draws it with `/ui/tile.js`, which escapes every value, so a
tile cannot send markup at all; `reports_screen.collect` refuses output of
any other shape.

**No shebang, on purpose, and two interpreters rather than one.** Two callers
run this file and they do not agree on the interpreter. A Resources view runs
it as `[sys.executable, "-I", <this file>, tab]` (`reports_screen.collect`), so
it inherits the server's own interpreter -- `SD_DASHBOARD_PYTHON`, which the
LaunchAgent sets to the command pack's `.venv/bin/python`, today Homebrew
python@3.13, execing that framework's `Python.app/Contents/MacOS/Python`.
`dashboard.sh tile <tab>` runs it under `DASHBOARD_PYTHON` from `.env`, today
`/opt/homebrew/bin/python3`, which is Homebrew python@3.15 and execs a
different `Python.app` binary. A shebang would add a third, whatever `python3`
resolves to on the caller's PATH, so this file names none and lets each caller
say.

Which binary runs is not an incidental detail: the vault lives under
`~/Documents`, behind macOS Full Disk Access, and FDA is granted to a binary,
so each of those two paths needs its own grant. An ungranted interpreter does
not read an empty vault and report it as one -- `collectors.vault_blocked`
probes the read in a child first, and a refusal becomes the tile's exit 1 with
the reason, naming the interpreter to grant. The Resources path is the one this
machine exercises: on 2026-09-14 its Briefs view rendered from the launchd
server with no refusal, so the python@3.13 binary holds the grant. Whether the
python@3.15 binary does was not measured under launchd; a shell has its own
Documents access and cannot answer it. That is also why nothing reported a
grant one path lost: `vault_blocked` names the interpreter it runs under, on
the tile that was asked, and a `brew upgrade python` on the other Cellar
drops that path's grant with no tile to say so. `dashboard.sh grants` asks
the same probe of both paths and prints one line per path (`vault_grants`,
sd:831); a control listing by `/bin/ls`, which holds no grant of its own,
says whether the answers are the binaries' own or the caller's reaching its
children. A second wrapper script beside
`dashboard.sh` was the first shape and was wrong twice over -- it broke the
folder's one-entrypoint convention and it copied the resolution that script
already does.

**Silence is not an outcome.** A collector that fails must not leave a calm
tab: exceptions become the tile's own exit-1 with the reason on stderr, which
a Resources view shows in place of the view, naming this tab. The pack's loader
turned it into a rank-0 row.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent


def load_collectors():
    """`collectors.py` as a module, imported for the collectors it is now.

    Imported rather than copied, because a second copy of a collector is a
    second thing to keep true; imported by path rather than by name, because
    `import collectors` works only while this file is the one being *run* --
    a script's own directory is `sys.path[0]`, and nothing else's is. Loading
    by path costs four lines and holds however this module is reached. Safe to
    do: the file is a library with no entry point at all since the server was
    deleted at 6b-8 -- its top level is constants, and importing it starts
    nothing because there is now nothing left to start.
    """
    spec = importlib.util.spec_from_file_location("collectors", HERE / "collectors.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {HERE / 'collectors.py'}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- markup ---------------------------------------------------------------


def cell(value: object) -> dict:
    """One table cell as data: its text, and a title, an http(s) link or a numeric alignment when it has them."""
    if not isinstance(value, dict):
        return {"text": "" if value is None else str(value)}
    out = {"text": "" if value.get("text") is None else str(value["text"])}
    if value.get("title"):
        out["title"] = str(value["title"])
    href = value.get("href")
    if isinstance(href, str) and href.startswith(("https://", "http://")):
        out["href"] = href
    if value.get("class") == "n":
        out["num"] = True
    return out


def table(columns: list[tuple[str, str]], rows: list[list[object]], search: str = "") -> dict | None:
    """A table section: `columns` is (label, how), `how` being `text`, `num`, or `none` for a column that does not
    sort; `search` names the filter box. An empty table is no section."""
    if not rows:
        return None
    return {"table": {"columns": [[label, how] for label, how in columns],
                      "rows": [[cell(value) for value in row] for row in rows], "search": search}}


def figures(pairs: list[tuple[str, object]]) -> dict | None:
    """The counts a tab leads with, label and value."""
    if not pairs:
        return None
    return {"figures": [[label, "" if value is None else str(value)] for label, value in pairs]}


def drift_when(got) -> str:
    """How old the drift figure beside this label is.

    It reads as a figure because it qualifies one: the number is the nightly
    job's, not this call's, and a reader who assumes otherwise is reading a
    stale count as a live one.
    """
    age = got.get("drift_age_h")
    if age is None:
        # Only an absent log means never. A log that exists but holds no
        # stamped run, or a run whose stamp will not parse, is a measurement
        # nobody can date -- reporting either as "never" claims more than is
        # known, and claims the reassuring thing.
        return "never" if got.get("drift_logged") is False else "unknown"
    if age < 1:
        return "just now"
    return f"{round(age)}h ago"


def heading(text: str) -> dict:
    return {"heading": text}


def listing(lines: list[str]) -> dict | None:
    """Lines that are text, not a table -- log tails and drift complaints."""
    if not lines:
        return None
    return {"listing": [str(line) for line in lines]}


# A cap for the 64KB tile budget rather than for taste. Whatever it hides, the
# markup says it is hiding.
LIST_ROWS = 25

# The per-tile time ceiling: the plugin contract's (the pack's `TILE_SECONDS`)
# when this was written, and each Resources view's `VIEW_SECONDS` now. The pack
# enforced it on the whole tile process: its clock started as soon as `Popen`
# returned for `dashboard.sh tile` (`bounded_run` in the pack's
# `dashboard/plugins.py`, retired at sd:719 step 3), and at five seconds it
# killed the process group and reported "no stdout within 5s" with only what
# stderr held by then. `collectors.Budget` also kills the tile's process group
# at a Resources view's ceiling.
TILE_SECONDS = 5.0
# How much sooner than that a collector here must give up, so that its refusal
# and its reason reach the caller before the kill does. A collector's budget
# starts only after /bin/sh, the interpreter and the imports, and ends with a
# kill, a wait and an exit. Read within the full five seconds it lost that race
# every time (sd:722 review): the pack killed the tile first, and the scan, in
# a session of its own, outlived the kill.
#
# Measured on 2026-09-13 through `dashboard.sh tile ports` at load average ~11,
# 25 runs: spawn to the collector's `Budget` 35ms median, 234ms worst; the
# collector's deadline to the tile's exit 80ms median, 155ms worst. One second
# is about 2.5 times the worst of the two together, so a collector here reads
# within four seconds.
TILE_MARGIN = 1.0
# The most a tab's one error line may be, newline included. A Resources view
# reads a tile through `collectors.Budget.run`, which keeps the tail of its
# stderr, `BUDGET_STDERR` bytes of it, and the tab name is at the FRONT of
# that line. A cap sized for the longest line the tile was known to write
# protected the name only while the line fitted it (sd:830); an error one
# byte longer pushed `vault: ` out of what the reader kept, and the reader's
# cut marker, naming the interpreter, was the first thing it saw (sd:834).
# So the line is bounded where it is written: `main` cuts the END of the
# reason, where the paths are, and says so there, so the name survives
# whatever the error's length. Copied rather than imported, as the reader's
# `BUDGET_BYTES` is: the line is written when loading `collectors.py` may be
# what failed. `tests/test_stderr_cap.py` pins the two to each other.
ERROR_BYTES = 4096


def error_line(name, error):
    """`<name>: <error!r>`, within `ERROR_BYTES` once `print` adds its newline.

    Cut from the end, never from the front: the front is the tab name and
    the error's class, and the end is where a path is. A cut says so where
    the bytes went missing, and how many, so a reader can tell a reason that
    was cut from one that ended there. The cut falls between characters, so
    what is shown decodes as written.
    """
    line = f"{name}: {error!r}"
    body = line.encode()
    room = ERROR_BYTES - 1
    if len(body) <= room:
        return line
    # The marker's own length depends on the count it carries, and the count
    # on the room the marker leaves. A pass changes the count only when the
    # one before it lengthened the marker, which a digit does at most ten
    # times, so a dozen passes always settle. The bound is there so that a
    # mistake here fails a test on the count rather than hangs a tile.
    cut = 0
    for _ in range(12):
        marker = f" [{name}: the last {cut} bytes of its reason were cut]"
        kept = body[:room - len(marker.encode())].decode("utf-8", "ignore")
        missing = len(body) - len(kept.encode())
        if missing == cut:
            break
        cut = missing
    return kept + marker


# -- tabs -----------------------------------------------------------------
#
# Each builder takes the imported `dashboard` module and returns the tab: a
# title, the markup, and the rows that need attention (the pack showed them in
# Now; a Resources view lists them under "Needs attention"). Rows are emitted
# only for a condition that is actually wrong -- a healthy machine contributes
# none, and the tile's exit 1, whose reason the view shows, is what covers the
# case where a builder dies.


def num(value: object) -> dict:
    """A right-aligned cell. `None` prints as an em dash, not as "None"."""
    return {"text": "\u2014" if value is None else value, "class": "n"}


def cadence_max_h(schedule: str) -> float:
    """How long a job may stay silent before silence is the finding.

    Ported from `cadenceMaxH` in the dashboard's own JS, which is where it has
    always lived -- so the rows it feeds are the ones a headless tile silently
    loses. A malformed expression gets 48h rather than an opinion; a job with a
    day-of-week gets a week and a day; a day-of-month gets a month and a day;
    everything else is daily, one slot plus two hours of slack.
    """
    fields = schedule.split()
    if len(fields) != 5:
        return 48.0
    if fields[4] != "*":
        return 8 * 24.0
    if fields[2] != "*":
        return 32 * 24.0
    return 26.0


def tab_toolbox(collectors) -> dict:
    """launchd's own record of the local-* fleet, plus docker and drift."""
    got = collectors.collect_toolbox()
    jobs = got["jobs"]
    broken = [job for job in jobs if job["last_exit"] not in (None, 0)]
    absent = [job for job in jobs if not job["installed"]]
    # The signal a headless port loses without `cadence_max_h`: a job that is
    # loaded, has never failed, and has simply stopped firing. Nothing else in
    # this tab reports it, and launchd will not either.
    silent = [
        job
        for job in jobs
        if job["installed"]
        and job["log_age_h"] is not None
        and job["next_in_h"] is not None
        and job["log_age_h"] > cadence_max_h(job["schedule"])
    ]

    parts = [
        figures(
            [
                ("jobs", len(jobs)),
                ("failing", len(broken)),
                ("silent", len(silent)),
                ("not loaded", len(absent)),
                ("agents", len(got["agents"])),
                ("containers", len(got["containers"])),
                ("drift", "\u2014" if got["drift"] is None else got["drift"]),
                ("drift checked", drift_when(got)),
            ]
        ),
        heading("Cron jobs"),
        table(
            [
                ("Job", "text"),
                ("Schedule", "none"),
                ("Last run", "text"),
                ("Next", "text"),
                ("Exit", "num"),
                ("Log age h", "num"),
            ],
            [
                [
                    # The description rides as a title rather than a column:
                    # 220 characters times thirty jobs is most of the tab's
                    # budget spent on prose nobody scans.
                    {"text": job["name"], "title": job["desc"]},
                    job["schedule"],
                    job["last_run"],
                    job["next_run"],
                    num(job["last_exit"]),
                    num(job["log_age_h"]),
                ]
                for job in jobs
            ],
            search="filter jobs",
        ),
    ]
    if got["agents"]:
        parts += [
            heading("Other agents"),
            table(
                [("Label", "text"), ("PID", "text"), ("Exit", "num")],
                [[one["label"], one["pid"], num(one["rc"])] for one in got["agents"]],
            ),
        ]
    if got["containers"]:
        parts += [
            heading("Containers"),
            table(
                [("Name", "text"), ("Status", "text"), ("Ports", "none")],
                [[one["name"], one["status"], one["ports"]] for one in got["containers"]],
            ),
        ]
    if got["drift_items"] or got.get("drift_unknown"):
        parts += [heading("Drift")]
        if got.get("drift_unknown"):
            parts += [listing([got["drift_unknown"]])]
        parts += [listing(got["drift_items"])]
    if got["failures"]:
        parts += [heading("Recent failures"), listing(got["failures"])]

    # Ranks and conditions are the dashboard's own `attentionItems`, not a
    # fresh opinion: this tab is being ported, and re-ranking it here would
    # quietly change which line is at the top of Now. The one deliberate
    # subtraction is the launch-agent table, which never produced rows there
    # and should not start now -- on this machine both non-zero codes are 143
    # and -15, a stop and a signal, so the rows it would add are false alarms.
    rows = [
        {
            "rank": 0,
            "kind": "cron",
            "id": f"cron-exit:{job['name']}:{job['last_exit']}",
            "what": f"cron {job['name']} exited {job['last_exit']}",
            "detail": job["schedule"] + (f" \u00b7 last log {job['last_run']}" if job["last_run"] else ""),
        }
        for job in broken
    ]
    rows += [
        {
            "rank": 1,
            "kind": "cron",
            "id": f"cron-silent:{job['name']}",
            "what": f"cron {job['name']} has not logged in {round(job['log_age_h'])}h",
            "detail": f"schedule {job['schedule']} \u00b7 next {job['next_run'] or '?'}",
        }
        for job in silent
    ]
    rows += [
        {
            "rank": 1,
            "kind": "cron",
            "id": f"cron-fail:{line}",
            "what": "cron failure log",
            "detail": line,
        }
        for line in got["failures"][-5:]
    ]
    rows += [
        {
            "rank": 2,
            "kind": "cron",
            "id": f"cron-missing:{job['name']}",
            "what": f"cron {job['name']} is not installed",
            "detail": job["schedule"],
        }
        for job in absent
    ]
    rows += [
        {"rank": 2, "kind": "drift", "id": f"drift:{item}",
         "what": "machine-setup drift", "detail": item}
        for item in got["drift_items"]
    ]
    # The drift figure is now the nightly job's last run rather than a fresh
    # measurement, so a number nobody can trust has to say so itself. This
    # overlaps the `cron-silent` row above when the cause is simply that the
    # job stopped running, and deliberately: that row tells the cron reader a
    # job went quiet, this one tells the drift reader the number above it is
    # not current. Suppressing either would leave one of them lying by
    # omission. The cases it catches alone are a log that never existed and a
    # run that died before it reported.
    if got.get("drift_unknown"):
        rows.append({
            "rank": 1,
            "kind": "drift",
            "id": "drift-unknown",
            # Not "unmeasured": the stale case was measured, just too long ago
            # to describe the machine now, and a row whose text contradicts
            # its own detail teaches the reader to skip both. What every one
            # of the cases has in common is that current drift is not known.
            "what": "current machine drift is not known",
            "detail": got["drift_unknown"],
        })
    return {"title": "Toolbox", "sections": [part for part in parts if part], "rows": rows}


def tab_vault(collectors) -> dict:
    """Vault areas, and the inbox pressure behind them."""
    got = collectors.collect_areas()
    if got.get("error"):
        # The collector reports a blocked vault as a field rather than an
        # exception. Turning it back into one is what makes the tile exit 1
        # with the reason, which a Resources view shows in place of the view
        # (the pack's loader put it in Now as a rank-0 row): a tab that
        # renders zero areas because the disk is unreachable must not look
        # like a vault with zero areas.
        raise RuntimeError(got["error"])
    parts = [
        figures(
            [
                ("inbox", got["inbox"]),
                ("stale inbox", len(got["stale_inbox"])),
            ]
        ),
        table(
            [
                ("Area", "text"),
                ("Notes", "num"),
                ("Latest note", "text"),
                ("When", "text"),
            ],
            [
                [
                    {"text": area["name"], "title": ", ".join(area["subfolders"])},
                    num(area["notes"]),
                    area["latest"],
                    area["latest_when"],
                ]
                for area in got["areas"]
            ],
            search="filter areas",
        ),
    ]
    if got["followups"]:
        parts += [
            heading("Followups"),
            table(
                [("Followup", "text"), ("Notes", "num")],
                [[label, num(count)] for label, count in sorted(got["followups"].items())],
            ),
        ]
    if got["stale_inbox"]:
        parts += [
            heading("Inbox, untouched for a week"),
            listing([note["name"] for note in got["stale_inbox"][:LIST_ROWS]]),
        ]

    rows = [
        {"rank": 3, "kind": "inbox", "id": f"inbox:{note['name']}",
         "what": note["name"], "detail": "inbox note untouched for a week"}
        for note in got["stale_inbox"]
    ]
    return {"title": "Vault", "sections": [part for part in parts if part], "rows": rows}


def tab_research(collectors) -> dict:
    """The sd-research-kit checkouts, and whether their documents are built."""
    items = collectors.collect_research()
    docs = [(item, doc) for item in items for doc in item["docs"]]
    stale = [pair for pair in docs if pair[1]["state"] != "fresh"]
    broken = [item for item in items if item["error"]]
    parts = [
        figures(
            [
                ("repos", len(items)),
                ("documents", len(docs)),
                ("not fresh", len(stale)),
                ("broken conf", len(broken)),
            ]
        ),
        heading("Checkouts"),
        table(
            [
                ("Repo", "text"),
                ("Project", "text"),
                ("Docs", "num"),
                ("Branch", "text"),
                ("Dirty", "num"),
                ("Last commit", "text"),
            ],
            [
                [
                    {"text": item["name"], "title": item["path"]},
                    item.get("project", ""),
                    num(len(item["docs"])),
                    (item["git"] or {}).get("branch", ""),
                    num((item["git"] or {}).get("dirty")),
                    (item["git"] or {}).get("last", ""),
                ]
                for item in items
            ],
            search="filter repos",
        ),
        heading("Documents"),
        table(
            [
                ("Repo", "text"),
                ("Document", "text"),
                ("State", "text"),
                ("Words", "num"),
                ("Updated", "text"),
            ],
            [
                [
                    item["name"],
                    {"text": doc["title"], "title": doc["src"]},
                    doc["state"],
                    num(doc["words"]),
                    doc["updated"],
                ]
                for item, doc in docs
            ],
            search="filter documents",
        ),
    ]
    # The one row this tab adds that the dashboard's own attention view never
    # had, and the only one: a `research.conf.py` that does not run makes the
    # repo render as a repo with no documents, which is the shape this whole
    # module refuses to accept. Stale documents are deliberately *not* a row --
    # they are already a column, an operator can see them, and a rank for every
    # unbuilt draft is how an attention view stops being read.
    rows = [
        {
            "rank": 0,
            "kind": "research",
            "id": f"research:{item['name']}",
            "what": f"{item['name']} could not be fully observed",
            "detail": item["error"],
        }
        for item in broken
    ]
    # The checkouts' paths, for the Repos filter that shows this tab (pack sd:7527).
    return {"title": "Research", "sections": [part for part in parts if part], "rows": rows,
            "paths": [item["path"] for item in items]}


def conflict_ports(service) -> list[str]:
    """`CLASH:8002` for every conflict on one service, for the row id.

    The id is what an acknowledgement is keyed on, so it has to change when
    the situation does: two services fighting over one port and that port
    being held by something else are different problems, and clearing one
    must not silence the other.
    """
    return [f"{c['flag']}:{c['port']}" for c in service.get("conflicts", [])]


def tab_ports(collectors) -> dict:
    """The port map, read out of the service scripts rather than from prose."""
    got = collectors.collect_ports(within=TILE_SECONDS - TILE_MARGIN)
    services = got["services"]
    parts = [
        figures(
            [("services", len(services)), ("clashing", got["clash"]), ("busy", got["busy"])]
        ),
        table(
            [
                ("Service", "text"),
                ("", "none"),
                ("Section", "text"),
                ("Ports", "none"),
                ("State", "text"),
                ("Flags", "text"),
            ],
            [
                [
                    one["name"],
                    # machine-setup's own single-character status marker, kept
                    # verbatim rather than translated: this table reports what
                    # that script said, and a guess at its vocabulary here
                    # would be a second thing to keep true.
                    one["mark"],
                    one["section"],
                    " ".join(one["ports"]),
                    one["state"],
                    " ".join(one["flags"]),
                ]
                for one in services
            ],
            search="filter services",
        ),
    ]
    # Rank 2, matching the dashboard's own attention view. Carried across at
    # 6b-4 knowing it could not fire: `machine-setup.sh candidates service`
    # prints CLASH and BUSY as their own summary lines, whose second token is
    # `port` rather than a service name, so they failed the collector's row
    # regex and no service row ever carried a flag. Ported rather than dropped
    # because the fix belonged upstream in the collector and a tab that quietly
    # lost the alert would hide that. `collect_ports` reads those lines as of
    # 6b-9, so this fires now, and the id carries the port -- two services
    # fighting over 8002 and one held by something else are different
    # situations to be told about.
    rows = [
        {
            "rank": 2,
            "kind": "port",
            "id": f"port:{one['name']}:{'+'.join(sorted(conflict_ports(one)))}",
            "what": f"port {' + '.join(one['flags'])} on {one['name']}",
            "detail": " ".join(
                f"{c['flag']} {c['port']}" for c in one.get("conflicts", [])
            ) or " ".join(one["ports"]),
        }
        for one in services
        if one["flags"]
    ]
    return {"title": "Ports", "sections": [part for part in parts if part], "rows": rows}


TABS = {
    "toolbox": tab_toolbox,
    "vault": tab_vault,
    "research": tab_research,
    "ports": tab_ports,
}


# `--grants`' exit when nothing was refused but the answers were not measured
# as the binaries' own: the caller's own Documents access reached every path,
# or the control that would have told them apart settled nothing -- it never
# started, the call on it failed, or it came back nonzero saying something
# that is not an access refusal, which are the three arms
# `collectors.control_listing` returns '' from. Not 0, which
# says both binaries hold the grant, not 1, which is a path that cannot read,
# and not 2, which is usage: a script gating on this verb must not read an
# unproven pass as a pass.
GRANTS_INCONCLUSIVE = 3

# What each answer from `collectors.control_listing` means for the lines above
# it, and whether those lines stand as the binaries' own. `refused` and
# `waited` are the two ways a binary holding no grant is kept out, so there
# the answers stand; `waited` still says so out loud, because a control that
# only ever waits is also what a wedged `ls` would look like. `listed` and ''
# leave the answers unmeasured, which is not the same as measuring that they
# hold, and each carries `GRANTS_INCONCLUSIVE` (review-376 B1).
#
# `waited` stays a pass on purpose, and sd:845 is where that was weighed
# rather than left to be rediscovered. Its whole surface is a control wedged
# for a reason that is not TCC while both probes answer `ok` promptly, which
# takes a wedge specific to `/bin/ls` and not to the vault: a wedge wide
# enough to reach the probes silences them too, and a silent probe is a
# refusal, so that lands on 1. Against that, the hang is the refusal shape
# this repository has measured under launchd -- an unanswered prompt held a
# collector for 1605 seconds (`collectors.VAULT_PROBE_SECONDS`) -- so if that
# is also the shape the control takes there, reading `waited` as unmeasured
# would leave the verb no route to 0 in the one place it is meant to be run.
# Which shape launchd gives is itself unmeasured (sd:845 R3, an owner action:
# a transient job raises a TCC prompt). Measure that first; it decides this.
CONTROL_LINE = {
    "listed": ("inconclusive: {control}, which holds no grant of its own, lists the vault too, so this "
               "process's own Documents access reached every path above and an ok there is not that "
               "binary's grant. Run this under launchd, where a child's access is its own, to measure "
               "the binaries"),
    "": ("inconclusive: the control settled nothing -- {control} neither listed the vault, nor was "
         "refused, nor ran out its wait -- so nothing here says whether the answers above are the "
         "binaries' own grants or this process's access reaching its children"),
    "waited": ("the control, {control}, was neither answered nor refused in {seconds} seconds, which is "
               "what a read nobody can grant looks like under launchd; the answers above are taken as "
               "the binaries' own"),
}
CONTROL_UNMEASURED = ("listed", "")


def vault_grants(collectors, named: list[str]) -> tuple[list[str], int]:
    """One line per interpreter path, each probed as the binary whose grant is in question.

    `named` is `ROLE=path` per interpreter, the two `dashboard.sh grants`
    passes: `DASHBOARD_PYTHON` for `tile` from a shell and
    `SD_DASHBOARD_PYTHON` for the server and the tiles a Resources view runs
    as its children. macOS grants Full Disk Access per binary, and a
    `brew upgrade python` moves one Cellar path and drops that path's grant
    silently. `collectors.vault_blocked` reports that only for the interpreter
    it runs under, on the tile that was asked, so the other path's loss had
    nowhere to appear but as an empty tab (sd:831). This asks the same probe,
    `collectors.probe_vault`, of each path, and a dropped grant is a line
    naming the path and what to grant, from `collectors.vault_refusal`.

    Two paths that resolve to one binary are probed once and said to be one:
    TCC attributes the read to the binary, so one grant covers both.

    The answers are the binaries' own only when the caller's are not
    reaching them. From a Terminal, or any process with Documents access of
    its own, every child reads the vault: on 2026-09-14 the probe returned
    `ok` under every candidate including `/bin/ls`. So `/bin/ls`, which holds
    no grant of its own, is the control (`collectors.control_listing`), and it
    runs the listing the probe runs rather than a stat. Only a control that
    was kept out -- refused, or left waiting as an unanswerable prompt leaves
    a read under launchd -- lets an `ok` above it stand as that binary's own
    grant. A control that listed the vault, or that measured nothing at all,
    is a caveat line and `GRANTS_INCONCLUSIVE`: not measuring is not the same
    as measuring that the answers hold (review-376 B1). A path that cannot
    read is a refusal whatever the control says: exit 1.
    """
    lines: list[str] = []
    refused = False
    seen: dict[str, str] = {}
    for entry in named:
        role, _, path = entry.partition("=")
        found = shutil.which(path) if path else None
        if found is None:
            lines.append(f"{role} {path}: not an executable path -- no file here runs, so nothing "
                         "on it reads the vault")
            refused = True
            continue
        real = os.path.realpath(found)
        if real in seen:
            lines.append(f"{role} {path}: the same binary as {seen[real]}, {real}; one grant covers "
                         "both, and its answer is that path's line above")
            continue
        seen[real] = role
        reason = collectors.vault_refusal(found, collectors.probe_vault(found))
        refused = refused or bool(reason)
        lines.append(f"{role} {path}: {reason or 'ok, lists the vault'}")
    control = collectors.control_listing()
    said = CONTROL_LINE.get(control)
    if said is not None:
        lines.append(said.format(control=collectors.VAULT_CONTROL,
                                 seconds=collectors.VAULT_PROBE_SECONDS))
    if refused:
        return lines, 1
    return lines, GRANTS_INCONCLUSIVE if control in CONTROL_UNMEASURED else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--grants"]:
        # The one mode that is not a tab: the probe and the refusal are the
        # collectors', so the report is built where they are loaded. `dashboard.sh grants` names the paths.
        if len(argv) < 2 or any("=" not in entry or not entry.partition("=")[0] for entry in argv[1:]):
            print("usage: dashboard.sh grants (runs sd_tile.py --grants ROLE=<interpreter>...)", file=sys.stderr)
            return 2
        lines, code = vault_grants(load_collectors(), argv[1:])
        print("\n".join(lines))
        return code
    if len(argv) not in (1, 2) or (len(argv) == 2 and not re.fullmatch(r"\d+(\.\d+)?", argv[1])):
        print(f"usage: dashboard.sh tile <{'|'.join(sorted(TABS))}> | "
              f"dashboard.sh grants", file=sys.stderr)
        return 2
    started = time.monotonic()
    name = argv[0]
    if len(argv) == 2:
        # A view's start on the shared clock (`fleet.main`, sd:2501): what this interpreter spent starting is taken
        # from the deadline, never added to it. A reading from the future counts as now.
        spent = time.clock_gettime(time.CLOCK_MONOTONIC) - float(argv[1])
        started -= min(max(spent, 0.0), TILE_SECONDS - TILE_MARGIN)
    build = TABS.get(name)
    if build is None:
        print(f"no such tab: {name}", file=sys.stderr)
        return 2
    try:
        collectors = load_collectors()
        with collectors.set_deadline(TILE_SECONDS - TILE_MARGIN, started=started):
            payload = build(collectors)
    except Exception as error:  # noqa: BLE001 - a Resources view shows this reason
        # Deliberately not an empty tab: a collector that failed and a machine
        # with nothing to report must not look the same, and the caller is
        # what makes the difference visible: a Resources view shows the reason.
        # Bounded here, so the tab name at its front survives the reader's
        # tail whatever the reason's length (`ERROR_BYTES`).
        print(error_line(name, error), file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
