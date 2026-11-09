"""`sd shadow sync` — the verb over the collector the library already ships.

`sd_db.shadow_sync`'s own docstring settles the split in as many words: "This
module is the collector, not the command. `sd shadow sync` is a verb and verbs
live in the pack." So nothing here collects. It opens the connection, calls
`sd_db.sync_shadow`, and turns the `Synced` it gets back into a line an
operator can act on.

The one thing worth stating twice is the order the collector guarantees and
this verb must not obscure: rows are written whether or not the collect
succeeded, because a partial answer is still true, and the watermark moves only
on the collect's own success. A run that reports "wrote 4, cursor held" is
therefore not a contradiction -- it is the design -- and the report says both
numbers rather than one summary word that would have to pick.

The verb runs once per tracker the library names in `sd_db.TRACKERS`, in the
library's order, and prefixes every line with `shadow sync[<tracker>]:` so a
log with two blocks reads as two. A tracker whose variables are unset is one
`not collected (<reason>)` line and exit 0, `--strict` or not: an absent
configuration is not a failed collect. `--since` and `--until` bound GitHub
alone -- Jira's window is relative minutes and cannot express them -- so a
recovery run prints `skipped (recovery window is GitHub's)` for every other
tracker (sd:361, the 2026-09-12 decision in its design.md).

Nothing here closes anything. `sync` calls `collect`, `store` and
`write_watermark` and no fourth thing, which is why criterion 28's "the fixture
saw no close call" is a fact about the library rather than a rule this verb has
to keep.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import math
import re
import sys
from datetime import datetime, timezone

#: The two refusals `sync_shadow` raises before it collects (sd:2898), each one
#: stderr line and its own code. 3 is the pack's `EXIT_REFUSED`: a satellite
#: cannot run the verb, so a retry there cannot help. 4 is the pack's
#: retry-later code, `sd-review`'s `EXIT_RATE_LIMITED`: the lock frees itself.
EXIT_HUB_ONLY = 3
EXIT_BUSY = 4

#: `(module, class, code)`; looked up rather than imported, so a library older
#: than system #199, which has no `SyncBusy`, still runs the verb.
REFUSALS = (("sd_db.remote", "HubOnly", EXIT_HUB_ONLY), ("sd_db.shadow_sync", "SyncBusy", EXIT_BUSY))


def refusals() -> dict[type[BaseException], int]:
    """Each refusal class the installed library defines, mapped to its exit code."""
    found: dict[type[BaseException], int] = {}
    for module, name, code in REFUSALS:
        try:
            kind = getattr(importlib.import_module(module), name, None)
        except ImportError:
            continue
        if isinstance(kind, type) and issubclass(kind, BaseException):
            found[kind] = code
    return found


def timestamp(value: str) -> datetime:
    """Require a timezone and normalize conservative whole-second UTC bounds."""
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
        r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)", value,
    ):
        raise argparse.ArgumentTypeError("use an ISO timestamp with Z or an explicit timezone offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(microsecond=0)
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError("use a valid timezone-aware ISO timestamp") from None


def positive_requests(value: str) -> int:
    """A request budget counts whole requests, with at least one permitted."""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def positive_seconds(value: str) -> float:
    """Reject NaN and infinity, which do not impose a useful time budget."""
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive finite number") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def _rows():
    """`bin/sd_handoff_rows.py`, which owns the library frame and its refusal."""
    import sd_handoff_rows  # noqa: PLC0415 - deferred, as the module docstring says

    return sd_handoff_rows


#: The head `Synced.report` writes on each of its lines. The verb strips it
#: before adding its own, so a forwarded line never reads
#: `shadow sync[jira]: shadow sync: ...`.
LIBRARY_HEAD = "shadow sync: "


def report_sync(result, *, name: str, strict: bool) -> int:
    """Turn one tracker's `Synced` into the operator's lines, and the exit code.

    Split out of `shadow_sync` because the verb crossed the branch ceiling
    when this grew, and because the reporting is worth driving directly: it
    is the part that was wrong, and it needs no CLI, no database and no
    network to exercise.

    Every line carries `shadow sync[<name>]:`, so two trackers' blocks in one
    log do not read as one. An unconfigured tracker is one line and exit 0
    whatever `strict` says: `configured` is read with a default of `True`
    because a pin that predates the field has only ever had configured
    trackers.
    """
    def say(line: str) -> None:
        print(f"shadow sync[{name}]: {line}")

    if not getattr(result, "configured", True):
        say(f"not collected ({result.reason or 'not configured'})")
        return 0
    say(f"wrote {result.written} shadow row(s)")
    if result.truncated:
        # Named, because a truncated page means the next run starts from the
        # same watermark and there is more behind it than this run saw.
        say("coverage is incomplete; retry a smaller window or increase the request/time limits")
    # `ok` is the SEARCH's success; `watermark_moved` is the CURSOR's. Reading
    # the cursor line off `ok` said "cursor held" through two nights in which
    # the watermark had in fact moved: one incomplete contribution observation
    # made `ok` false while the search itself had advanced. So the cursor line
    # is read off the cursor, and the reason off the collect.
    if result.watermark_moved:
        say(f"cursor moved to cover from {result.window_start}")
    elif result.ok and not result.window_start:
        # A custom JIRA_JQL has no window, so it covers no span (pack sd:5376).
        say("custom JIRA_JQL ran unwindowed; cursor held")
    elif result.ok:
        say(f"coverage completed from {result.window_start}; existing cursor retained")
    else:
        # The rows above are still written and still true. Only the cursor
        # held, so the next run re-reads the same window rather than skipping.
        say(f"cursor held: {result.reason or 'the collect did not succeed'}")
    if not result.ok and result.watermark_moved:
        say(f"the collect did not succeed: {result.reason or 'no reason given'}")
    for line in _library_lines(result):
        # A line without the head is forwarded as it is, rather than dropped:
        # the library's wording is not this verb's to police.
        say(line[len(LIBRARY_HEAD):] if line.startswith(LIBRARY_HEAD) else line)
    return 0 if result.ok else (1 if strict else 0)


def _library_lines(result) -> list[str]:
    """The library's report, minus the lines this verb already said.

    Forwarded rather than re-worded, so a line the library learns to emit
    reaches the log without this verb being edited -- sd:602's lesson.

    Two guards drop the two lines the verb has already put in its own words,
    and they are not symmetric. The reason line is matched by the FIELD that
    produced it: `result.reason` is the same string the library interpolated,
    so the guard cannot drift from it. The truncation line has no field to
    match -- `result.truncated` is the bucket list, not the sentence -- so
    that guard names a phrase the LIBRARY owns, `truncated buckets`.

    Each guard is also conditioned on the field, so a line is only ever
    considered for dropping when the verb in fact said it above.

    Matching a phrase is the deliberate choice here, because of which way it
    fails. If the library rewords its truncation line, the guard stops
    matching and the line is forwarded and printed TWICE. Dropping by
    position instead -- second line when truncated, third when not ok --
    would fail the other way, and silently eat a real line the moment the
    library stops emitting one. A duplicate is visible; a loss is not.

    The installed library decides how many lines there are. A pin that
    predates `queued` and `incomplete` simply yields fewer, which is why this
    reads `report()` rather than naming the fields it hopes to find.
    """
    lines = result.report()[1:]
    if result.truncated and lines and "truncated buckets" in lines[0]:
        lines = lines[1:]
    if not result.ok and result.reason and lines and result.reason in lines[0]:
        lines = lines[1:]
    return lines


def mention_days() -> int | None:
    """`sd.jira_recent_days`, the days a Jira mention stays a live wait (sd:8200); `None` reads the library's 30."""
    import sd_lib

    try:
        value = sd_lib.core_setting("jira_recent_days")
    except sd_lib.ConfigError as error:
        print(f"sd: {error}; Jira mentions read the last 30 days", file=sys.stderr)
        return None
    return int(value) if value else None


def shadow_sync(args) -> int:
    """Run one sync per tracker and report what moved. Writes rows; never closes anything.

    The exit code is the maximum over trackers, so one held GitHub cursor
    under `--strict` still fails the night when Jira was fine. A refusal from
    the library ends the run with one stderr line and its code from `REFUSALS`.
    """
    rows = _rows()
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    current = datetime.now(timezone.utc).replace(microsecond=0)
    if until is not None and until > current:
        raise rows.RowsRefusal("--until must not be in the future")
    if since is not None and since > (until if until is not None else current):
        raise rows.RowsRefusal("--since must not be after --until (or the current time)")
    options = {
        name: value for name, value in (
            ("since", since), ("now", until),
            ("max_requests", getattr(args, "max_requests", None)),
            ("max_seconds", getattr(args, "max_seconds", None)),
        ) if value is not None
    }
    sd_db = rows.library()
    # A pin that predates the export has one tracker, and the verb's tests
    # keep passing at either pin (design.md, "The library exports the order").
    names = getattr(sd_db, "TRACKERS", ("github",))
    # The recovery window is GitHub's: the library refuses `since` for any
    # other tracker, and a relative-minutes Jira window could not honour
    # `--until` anyway. So a bounded run is GitHub's alone, and the others
    # say so in their place in the order rather than vanishing from the log.
    recovery = "since" in options or "now" in options
    strict = bool(getattr(args, "strict", False))
    code = 0
    refused = refusals()
    connection = rows.connect(sd_db, write=True)
    try:
        for name in names:
            if recovery and name != "github":
                print(f"shadow sync[{name}]: skipped (recovery window is GitHub's)")
                continue
            # sd:6712: a scheduled GitHub run files the pull request alerts; a library without them is not asked.
            # sd:8142: a Jira run files the issue alerts it read.
            alerts: dict[str, bool | int] = {"alerts": True} if not recovery and importlib.util.find_spec("sd_db.pr_attention") else {}
            days = mention_days() if alerts and name == "jira" else None
            if days:
                alerts["mention_days"] = days
            result = sd_db.sync_shadow(connection, tracker=name, **options, **alerts)
            code = max(code, report_sync(result, name=name, strict=strict))
    except tuple(refused) as error:
        print(f"sd: {error}", file=sys.stderr)
        return next(value for kind, value in refused.items() if isinstance(error, kind))
    finally:
        connection.close()
    return code
