"""The schema: its version, its tables, and the migration files.

Migrations are numbered files applied by one explicit command. Opening the
database applies nothing -- an upgrade that happens because a process started
is an upgrade nobody chose, under processes still running against the old
shape.
"""

from __future__ import annotations

import re
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

#: The version this build of the library was written against. Bumped in the
#: same commit that adds a migration file, and nowhere else.
SCHEMA_VERSION = 48

#: The storage tables. `report` is an `item.kind`
#: value. The tuple is the document requirement 1 points at: a record kind
#: with no home is added here, in review, before anything writes it.
TABLES = (
    "repo",
    "item",
    "note",
    "shadow",
    "assignment",
    "skill_use",
    "trial",
    "cost",
    "provider",
    "bill",
    "state",
    "publication_claim",
    "runner_run",
    "runner_lease",
    "repo_protection",
    "judgment",
    "request_outcome",
    "home_asset",
)

#: The version is `PRAGMA user_version`, not a bookkeeping table.
VERSION_PRAGMA = "user_version"

MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def migrations() -> list[tuple[int, Path]]:
    """Every migration file, as `(version, path)`, in order.

    A gap or a duplicate is a refusal and not a warning: applying 1 and 3
    leaves a database whose `user_version` claims a shape it does not have.
    """
    found: dict[int, Path] = {}
    for path in sorted(SCHEMA_DIR.glob("*.sql")):
        match = MIGRATION_NAME.match(path.name)
        if match is None:
            raise ValueError(
                f"{path.name} is not a migration file name; expected "
                f"NNN_lower_snake.sql"
            )
        version = int(match.group(1))
        if version in found:
            raise ValueError(f"two migrations numbered {version:03d}")
        found[version] = path
    ordered = sorted(found.items())
    for expected, (version, path) in enumerate(ordered, start=1):
        if version != expected:
            raise ValueError(
                f"migration {version:03d} ({path.name}) is out of sequence; "
                f"expected {expected:03d}"
            )
    if ordered and ordered[-1][0] != SCHEMA_VERSION:
        raise ValueError(
            f"the last migration is {ordered[-1][0]:03d} but SCHEMA_VERSION "
            f"is {SCHEMA_VERSION}; they are bumped together"
        )
    return ordered
