"""Home assets (pack sd:8053, sd:8054): what is on the home lot, one `home_asset` row each.

The Home page's property map draws them over the county aerial, and the koi
dashboard (pack sd:8054) reads its pond and fish from the same rows. A row is
a `kind` from `KINDS`, a name, an optional GeoJSON `Point` or `LineString` in
EPSG:2232 feet, the kind's `fields` as a JSON object, and an optional photo
address. A care task is an sd item naming the row in `item.fields.asset`
(`care`); it recurs with the item, so the link outlives each occurrence.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from .database import transaction
from .errors import SdDbError
from .writes import now

#: The kinds, in the order the Add form lists them (operator 2026-11-07).
KINDS = (
    "gas-line", "gas-shutoff", "water-line", "water-shutoff", "sewer-line", "sewer-shutoff",
    "electric-line", "electric-shutoff", "comms-line", "comms-shutoff",
    "irrigation-line", "irrigation-valve", "irrigation-zone", "backflow",
    "solar", "powerwall", "inverter", "ev-charger", "hvac",
    "tree", "plant",
    "light", "drainage", "fence", "easement", "pond",
)
#: The kinds with no place on the map: the pond's fish (pack sd:8054, the Koi view).
FISH = ("koi",)
ALL_KINDS = KINDS + FISH
#: The kinds drawn as a line by default; any kind may hold either shape.
LINES = frozenset({"gas-line", "water-line", "sewer-line", "electric-line", "comms-line", "irrigation-line",
                   "irrigation-zone", "drainage", "fence", "easement"})
#: The fields a plant or a tree carries (operator 2026-11-07); the form offers them, the store keeps any.
PLANT_FIELDS = ("species", "planted", "sun", "water", "zone", "notes")
NAME_MAX, PHOTO_MAX, FIELDS_MAX = 200, 2000, 64 * 1024


class AssetRefused(SdDbError, ValueError):
    """An asset write the store will not make, with the reason."""


def _geometry(value: Any) -> str | None:
    """A Point or a LineString of finite numbers, as JSON text, or None."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"type", "coordinates"}:
        raise AssetRefused("geometry is null or {type, coordinates}")

    def point(p) -> list[float]:
        if (not isinstance(p, list) or len(p) != 2
                or not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in p)):
            raise AssetRefused("a coordinate is [x, y], two finite numbers")
        return [float(p[0]), float(p[1])]

    kind, coordinates = value["type"], value["coordinates"]
    if kind == "Point":
        shaped: Any = point(coordinates)
    elif kind == "LineString" and isinstance(coordinates, list) and len(coordinates) >= 2:
        shaped = [point(p) for p in coordinates]
    else:
        raise AssetRefused("geometry is a Point, or a LineString of two points or more")
    return json.dumps({"type": kind, "coordinates": shaped}, separators=(",", ":"))


def _fields(value: Any) -> str:
    if not isinstance(value, dict):
        raise AssetRefused("fields is a JSON object")
    try:
        text = json.dumps(value, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise AssetRefused("fields holds a value JSON cannot keep") from None
    if len(text) > FIELDS_MAX:
        raise AssetRefused(f"fields is over {FIELDS_MAX // 1024} KiB")
    return text


def _text(value: Any, what: str, limit: int, *, optional: bool = False) -> str | None:
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AssetRefused(f"{what} is text of 1 to {limit} characters")
    return value.strip()


def _row(row: sqlite3.Row | tuple) -> dict:
    id_, kind, name, geometry, fields, photo, created, updated = tuple(row)
    return {"id": id_, "kind": kind, "name": name, "geometry": json.loads(geometry) if geometry else None,
            "fields": json.loads(fields or "{}"), "photo": photo, "created_at": created, "updated_at": updated}


COLUMNS = "id, kind, name, geometry, fields, photo, created_at, updated_at"


def list_assets(connection: sqlite3.Connection, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Every asset, or those of `kinds`, by id."""
    rows = connection.execute(f"SELECT {COLUMNS} FROM home_asset ORDER BY id").fetchall()
    return [asset for asset in map(_row, rows) if kinds is None or asset["kind"] in kinds]


def get_asset(connection: sqlite3.Connection, asset: int) -> dict:
    row = connection.execute(f"SELECT {COLUMNS} FROM home_asset WHERE id = ?", (asset,)).fetchone()
    if row is None:
        raise AssetRefused(f"there is no asset {asset}")
    return _row(row)


def save_asset(connection: sqlite3.Connection, *, kind: str, name: str, geometry: Any = None,
               fields: Any = None, photo: Any = None, asset: int | None = None,
               kinds: tuple[str, ...] = ALL_KINDS) -> dict:
    """Add an asset, or replace asset `asset`'s values; the row as stored."""
    if kind not in kinds:
        raise AssetRefused(f"kind is one of {', '.join(kinds)}")
    values = (kind, _text(name, "name", NAME_MAX), _geometry(geometry), _fields({} if fields is None else fields),
              _text(photo, "photo", PHOTO_MAX, optional=True))
    at = now()
    with transaction(connection):
        if asset is None:
            asset = int(connection.execute(
                "INSERT INTO home_asset (kind, name, geometry, fields, photo, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (*values, at, at)).lastrowid or 0)
        else:
            get_asset(connection, asset)
            connection.execute("UPDATE home_asset SET kind = ?, name = ?, geometry = ?, fields = ?, photo = ?, "
                               "updated_at = ? WHERE id = ?", (*values, at, asset))
        return get_asset(connection, asset)


def delete_asset(connection: sqlite3.Connection, asset: int) -> dict:
    """Remove one asset; the row it was. Its care items keep their link and read as an asset gone."""
    with transaction(connection):
        found = get_asset(connection, asset)
        connection.execute("DELETE FROM home_asset WHERE id = ?", (asset,))
        return found


def care(connection: sqlite3.Connection, *, today: str) -> dict[int, dict]:
    """Each asset's open care items, by asset id: `{items: [{id, title, due}], overdue}`; overdue is a due before `today`."""
    out: dict[int, dict] = {}
    rows = connection.execute("SELECT id, title, due, fields FROM item WHERE status != 'done' AND fields LIKE '%\"asset\"%' "
                              "ORDER BY due IS NULL, due, id").fetchall()
    for item, title, due, fields in rows:
        if (asset := item_asset(fields)) is None:
            continue
        entry = out.setdefault(asset, {"items": [], "overdue": False})
        entry["items"].append({"id": item, "title": title, "due": due})
        entry["overdue"] = entry["overdue"] or (due is not None and str(due)[:10] < today)
    return out


def item_asset(fields: Any) -> int | None:
    """The asset an item's `fields` JSON names, or None: no fields, unreadable fields, or no whole-number `asset`."""
    try:
        value = json.loads(fields or "{}")
    except (TypeError, ValueError, RecursionError):
        return None
    asset = value.get("asset") if isinstance(value, dict) else None
    return asset if type(asset) is int else None
