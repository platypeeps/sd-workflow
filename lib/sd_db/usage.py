"""`sd usage`: the month, printed from the read the Usage screen shows.

The verb is the one in `sd-db` (the pack's `usage` group, when it lands,
calls `report` and prints the same text). It reads `read` -- `reads.usage_month`
under the registry's merged caps, one read, shared with the Usage screen and,
per bill, with Today's cost tile -- and writes nothing but the sweep (the
registry is consulted through `caps`, which parses the file and merges the
rows without `registry.read`'s first-open seeding, so no row is written on
the way): requirement 6 has a `reserved` row
whose owner is dead released, and a `sending` row whose owner is dead
settled to `bound`, "by the next reservation or by `sd usage`", so the verb
runs `ledger.release_orphans` before it reads, in the sweep's own committed
transaction, the one a reservation runs it in. `--json` prints the read and
nothing else, so a test can hold it to the screen's bytes.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import reads, registry
from .errors import SdDbError
from .ledger import Released, alive, release_orphans

__all__ = ["bills", "caps", "json_text", "read", "report", "text"]


def caps(connection: sqlite3.Connection) -> dict[str, float | None] | None:
    """Each bill's cap as the registry merges it, for `reads.cost_by_bill`
    and `reads.usage_month`.

    `registry.merge` leaves a legacy row cap on a bill a `start` entry is
    billed to out of the merged view and reports it in `warnings` (#433),
    and `provider_controls.snapshot` shows that view. The cost tile and the
    month card take their caps from the same view, so a cap the reader
    refuses is printed nowhere but the panel's clear form, which asks the
    row directly (sd:234 slice 12h). The registry is the one beside the
    connection's database (`registry.beside`), parsed and merged here rather
    than through `registry.read`, whose first-open seeding is a write this
    module's verb does not make. None when the file is absent, cannot be
    opened or cannot be parsed -- the set the Metrics page's `_guard` catches --
    or parses with a cap that is not a number or too large for a float
    (`parse` does not type one, and a bill with no row shows the file's), so
    the tile and the card fall back to the row's cap and the panel is where
    the registry's trouble is reported.
    """
    try:
        path = registry.beside(connection)
        current = registry.merge(registry.parse(path.read_text(encoding="utf-8"), path), connection)
        return {name: None if bill.cap_usd_month is None else float(bill.cap_usd_month)
                for name, bill in current.bills.items()}
    except (SdDbError, OSError, ValueError, TypeError, OverflowError):
        return None


@contextmanager
def _snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    """One deferred read transaction around the registry's rows and the cost
    rows, as `reads.usage_month` holds one around its own SELECTs: the
    connection is `isolation_level=None`, so without it a cap written between
    `caps` and the read would be under one bill's spend and not its cap. A
    caller's own transaction already does this."""
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN")
    try:
        yield
    finally:
        connection.execute("COMMIT")


def read(
    connection: sqlite3.Connection, *, month: str | None = None, now: str | None = None
) -> reads.Usage:
    """The month under the merged caps: `reads.usage_month` with `caps`, the
    two in one snapshot. The verb, `/api/usage` and the Usage panel all read
    through here, so the three cannot disagree on a cap."""
    with _snapshot(connection):
        return reads.usage_month(connection, month=month, now=now, caps=caps(connection))


def bills(connection: sqlite3.Connection, *, now: str | None = None) -> list[sqlite3.Row]:
    """The cost tile's rows under the merged caps: `reads.cost_by_bill` with
    `caps`, the two in one snapshot."""
    with _snapshot(connection):
        return reads.cost_by_bill(connection, now=now, caps=caps(connection))


def report(
    connection: sqlite3.Connection,
    *,
    month: str | None = None,
    now: str | None = None,
    sweep: bool = True,
    is_alive=alive,
) -> tuple[reads.Usage, Released]:
    """The month checked, the sweep, then the read. A month that is not one
    is refused before the sweep, so a refused call writes nothing.
    `sweep=False` is a read-only connection's."""
    month = reads.month_of(month, now=now)
    released = release_orphans(connection, is_alive=is_alive) if sweep else Released()
    return read(connection, month=month, now=now), released


def json_text(usage: reads.Usage) -> str:
    """The one serialisation: `sd usage --json` and `/api/usage` send these bytes."""
    return json.dumps(usage.document(), indent=2, sort_keys=True) + "\n"


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def text(usage: reads.Usage) -> str:
    """The month as a person reads it: the four numbers per bill, the roles,
    their areas, the `bound` rows, and the total against the invoice line."""
    lines = [f"usage {usage.month}: {_money(usage.spent)} spent, {_money(usage.held)} held"]
    if not usage.bills:
        lines.append("  no bills")
    for bill in usage.bills:
        lines.append(
            f"  {bill.name} ({bill.cost_basis}): spent {_money(bill.spent)}, "
            f"estimated {_money(bill.estimated)}, held {_money(bill.held)}, cap {_money(bill.cap)}"
            + (f", room {_money(bill.room)}" if bill.room is not None else "")
        )
    if usage.unbilled is not None:
        lines.append(f"  no bill: spent {_money(usage.unbilled)}")
    if usage.roles:
        lines.append("roles:")
    for row in usage.roles:
        lines.append(
            f"  {row['bill'] or '-'} / {row['provider'] or '-'} / {row['role'] or '-'}: "
            f"{row['calls']} call(s), spent {_money(row['spent'])}, held {_money(row['held'])}, "
            f"tokens {row['tokens_in']} in / {row['tokens_out']} out"
        )
    if usage.areas:
        lines.append("areas:")
    for area in usage.areas:
        lines.append(f"  {area['area']}: {area['calls']} call(s), spent {_money(area['spent'])}, "
                     f"tokens {area['tokens_in']} in / {area['tokens_out']} out")
    lines.append(f"bound rows ({len(usage.bound)}): billed at the bound, estimated until corrected")
    for row in usage.bound:
        lines.append(
            f"  {row['timestamp']} {row['call_id'] or '-'} {row['bill'] or '-'} "
            f"{row['provider'] or '-'} {row['role'] or '-'} "
            f"assignment {row['assignment'] if row['assignment'] is not None else '-'}: {_money(row['usd'])}"
        )
    return "\n".join(lines) + "\n"


def _prefix_end(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def ever_read(connection: sqlite3.Connection, prefix: str) -> bool:
    """Whether any heartbeat row under `prefix` exists, removed ones too: with none answering, all were removed."""
    return connection.execute("SELECT 1 FROM state WHERE kind = 'heartbeat' AND key >= ? AND key < ? LIMIT 1",
                              (prefix, _prefix_end(prefix))).fetchone() is not None


def newest_heartbeats(connection: sqlite3.Connection, prefix: str) -> dict[str, tuple[str, object]]:
    """The newest heartbeat row of each key under `prefix`, by name: `(timestamp, body)`. A removed entry is left out;
    a body that does not parse is None, which reads unknown."""
    end = _prefix_end(prefix)
    found = {}
    for key, stamp, body in connection.execute(
            "SELECT key, timestamp, body FROM state WHERE id IN (SELECT MAX(id) FROM state"
            " WHERE kind = 'heartbeat' AND key >= ? AND key < ? GROUP BY key) ORDER BY key", (prefix, end)):
        try:
            body = json.loads(body)
        except (TypeError, ValueError, RecursionError):
            body = None
        if isinstance(body, dict) and body.get("status") == "removed":
            continue
        found[key[len(prefix):]] = (stamp, body)
    return found
