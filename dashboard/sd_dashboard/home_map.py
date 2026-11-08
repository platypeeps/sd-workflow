"""Home's property map (pack sd:8053): the lot's assets over the county aerial, at `/api/home/map`.

**Where.** The address is the workflow database's setting row `dashboard.home_map` (Settings, a dashboard list): one
line, the street address. Pack code holds no address. The county's address locator (`LOCATOR`) turns it into a point
in EPSG:2232 (Colorado Central, US feet), and the map runs `HALF` feet west, north and south of it and `EAST` feet
east (sd:8085): 1,000 ft by 400 ft.

**The aerial.** Arapahoe County's 2024 aerial (`AERIAL`, an ArcGIS ImageServer, 0.217 ft = 2.6 in per pixel) answers
one `exportImage` for that box at full resolution, cached under `<state>/system/home-map/` by the address and the
box, so the county is asked once per address. `GET /api/home/map/aerial` serves the cached picture.

**The assets.** `sd_db.assets` rows, each a point or a line in the same feet, drawn in an SVG whose viewBox is the
box (y flipped, north up). A plant or a tree whose care is overdue is marked: a care task is an open sd item naming
the asset in `fields.asset` (`assets.care`), filed recurring from the asset's Details (`POST /api/home/care`), so it
recurs with the item. Add, move, edit and delete post `/api/home/asset` and `/api/home/asset/delete`.

A failed address read, geocode or aerial names its reason; the assets still draw on a plain ground.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KEY = "dashboard.home_map"
LOCATOR = "https://gis.arapahoegov.com/arcgis/rest/services/AddressLocator/GeocodeServer/findAddressCandidates"
AERIAL = "https://gis.arapahoegov.com/arcgis/rest/services/Aerials/ImageServer/exportImage"
SR = 2232
#: The aerial's pixel, in feet; the map's reach west, north and south of the address; and its reach east (sd:8085,
#: 600 ft more). 1,000 ft by 400 ft is 4,608 by 1,843 pixels at full resolution, under the service's 15000x4100.
PIXEL_FT, HALF = 0.21701388888888898, 200.0
EAST = HALF + 600.0
TIMEOUT = 20.0
LIMIT = 32 * 1024 * 1024
#: The fields only the server writes, by kind: `home_koi.log_water` and `log_test` append to the current row.
SERVER_KEYS = {"pond": ("water_log", "tests", "water_synced", "water_gaps")}
#: The fields the Add form offers a plant or a tree, and a care task's recurrence choices (`sd_db.recurrence`).
RECUR = ("FREQ=DAILY", "FREQ=WEEKLY", "FREQ=WEEKLY;INTERVAL=2", "FREQ=MONTHLY", "FREQ=YEARLY")


def state_dir() -> Path:
    """`<XDG_STATE_HOME>/system/home-map`: the geocode and aerial caches."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / "home-map"


def parse(text: str) -> tuple[str | None, list[str]]:
    """The setting's address and its problems: one line that is not a comment."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        return None, [f"one line, the street address; found {len(lines)}"]
    if len(lines[0]) > 200:
        return None, ["the address is over 200 characters"]
    return lines[0], []


def _get(url: str, *, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "sd-dashboard"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- the two fixed https hosts above
        body = response.read(LIMIT + 1)
    if len(body) > LIMIT:
        raise ValueError(f"the answer is over {LIMIT // (1024 * 1024)} MiB")
    return body


def _cache(address: str) -> Path:
    return state_dir() / hashlib.sha256(f"{address}|{HALF}|{EAST}".encode()).hexdigest()[:16]


def place(address: str, *, timeout: float = TIMEOUT) -> dict:
    """`{x, y, bbox, match}` for the address, from the cache or the county's locator; raises `LookupError` with why."""
    cached = _cache(address) / "place.json"
    try:
        return json.loads(cached.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    query = urllib.parse.urlencode({"SingleLine": address, "outSR": SR, "maxLocations": 1, "f": "json"})
    try:
        answer = json.loads(_get(f"{LOCATOR}?{query}", timeout=timeout))
    except (OSError, ValueError) as error:
        raise LookupError(f"the county's address locator did not answer: {error.__class__.__name__}") from None
    out = _located(answer)
    _keep(cached, json.dumps(out).encode())
    return out


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _located(answer) -> dict:
    """`{x, y, bbox, match}` from the locator's JSON, or `LookupError` naming what the reply lacks (sd:8076)."""
    if not isinstance(answer, dict):
        raise LookupError("the county's address locator answered with something that is not a JSON object")
    if "error" in answer:
        error = answer["error"] if isinstance(answer["error"], dict) else {}
        raise LookupError(f"the county's address locator answered an error: {error.get('code', '')} "
                          f"{error.get('message', '')}".rstrip())
    found = answer.get("candidates") or []
    if not isinstance(found, list) or not all(isinstance(one, dict) for one in found):
        raise LookupError("the county's address locator answered with something that is not a list of candidates")
    if not found:
        raise LookupError("the county's address locator found no match for the address on Settings")
    location = found[0].get("location")
    x, y = (_number(location.get("x")), _number(location.get("y"))) if isinstance(location, dict) else (None, None)
    if x is None or y is None:
        raise LookupError("the county's address locator gave its match no numeric x and y")
    return {"x": x, "y": y, "bbox": [x - HALF, y - HALF, x + EAST, y + HALF], "match": str(found[0].get("address", ""))}


def _keep(path: Path, data: bytes) -> None:
    """Write the cache file whole or not at all: a temp file beside it, then a rename. A failed write is no cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)


def aerial(address: str, *, timeout: float = TIMEOUT) -> bytes:
    """The aerial for the address's box, from the cache or one `exportImage`; raises `LookupError` with why."""
    where = place(address, timeout=timeout)
    cached = _cache(address) / "aerial.jpg"
    try:
        held = cached.read_bytes()
        if held.startswith(b"\xff\xd8"):
            return held
    except OSError:
        pass
    size = f"{round((HALF + EAST) / PIXEL_FT)},{round(2 * HALF / PIXEL_FT)}"
    query = urllib.parse.urlencode({"bbox": ",".join(f"{n:.3f}" for n in where["bbox"]), "bboxSR": SR, "imageSR": SR,
                                    "size": size, "format": "jpg", "f": "image"})
    try:
        body = _get(f"{AERIAL}?{query}", timeout=timeout)
    except (OSError, ValueError) as error:
        raise LookupError(f"the county's aerial did not answer: {error.__class__.__name__}") from None
    if not body.startswith(b"\xff\xd8"):
        raise LookupError("the county's aerial answered with something that is not a JPEG")
    _keep(cached, body)
    return body


def address(connection) -> tuple[str | None, str]:
    """The setting's address and, when there is none, why."""
    from sd_db import writes

    try:
        held = writes.read_settings(connection, [KEY])
    except Exception as error:  # an unread row is unknown, named
        return None, f"{KEY} not read: {error.__class__.__name__}"
    found, problems = parse(held.get(KEY) or "")
    if found is None:
        return None, f"No address: set {KEY} on Settings ({problems[0]})." if held.get(KEY) else \
            f"No address: set {KEY} on Settings to the street address."
    return found, ""


def document(connection, *, now: str, timeout: float = TIMEOUT) -> dict:
    """The Map view's document: the square, the aerial's address, the assets with their care, the kinds."""
    from sd_db import assets

    shown, reason = address(connection)
    where = None
    if shown is not None:
        try:
            where = place(shown, timeout=timeout)
        except LookupError as error:
            reason = str(error)
    care = assets.care(connection, today=now[:10])
    rows = [{**row, "care": care.get(row["id"], {"items": [], "overdue": False})}
            for row in assets.list_assets(connection, assets.KINDS)]
    return {"state": "read" if where else "partial", "reason": reason, "bbox": where["bbox"] if where else None,
            "aerial": "/api/home/map/aerial" if where else None, "pixel_ft": PIXEL_FT, "assets": rows,
            "kinds": list(assets.KINDS), "lines": sorted(assets.LINES), "plant_fields": list(assets.PLANT_FIELDS),
            "recur": list(RECUR)}


def aerial_route(connection) -> tuple:
    """`GET /api/home/map/aerial`: `(200, bytes, "image/jpeg")`, else `(status, {"error"})`."""
    shown, reason = address(connection)
    if shown is None:
        return 404, {"error": reason}
    try:
        return 200, aerial(shown), "image/jpeg"
    except LookupError as error:
        return 503, {"error": str(error)}


def _id(value, what: str = "asset") -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{what} is a whole number, 1 or more.")
    return value


def save(payload: dict):
    """`POST /api/home/asset {id?, kind, name, geometry, fields, photo}`: add or replace one asset."""
    from sd_db import assets
    from sd_db.database import transaction

    values = dict(payload)
    if not set(values) <= {"id", "kind", "name", "geometry", "fields", "photo"} or not {"kind", "name"} <= set(values):
        raise ValueError("Send kind and name, and any of id, geometry, fields and photo.")
    asset = _id(values.pop("id")) if "id" in values else None

    def write(connection):
        try:
            # One transaction: a log the Koi view appends between the read and the write is not lost.
            with transaction(connection):
                if asset is not None:
                    # The server writes these on the current row (the Koi view); a form's older copy, or no
                    # `fields` at all (omitted or null, which save_asset reads as empty), never replaces them.
                    current = assets.get_asset(connection, asset)
                    kept = {key: current["fields"][key] for key in SERVER_KEYS.get(current["kind"], ())
                            if key in current["fields"]}
                    sent = values.get("fields")
                    if kept and (sent is None or isinstance(sent, dict)):
                        values["fields"] = {**(sent or {}), **kept}
                return assets.save_asset(connection, asset=asset, **values)
        except assets.AssetRefused as refused:
            return 400, {"error": str(refused)}

    return write


def delete(payload: dict):
    """`POST /api/home/asset/delete {id}`: remove one asset; its care items keep their link."""
    from sd_db import assets

    if set(payload) != {"id"}:
        raise ValueError("Send the asset's id.")
    asset = _id(payload["id"])

    def write(connection):
        try:
            return assets.delete_asset(connection, asset)
        except assets.AssetRefused as refused:
            return 404, {"error": str(refused)}

    return write


def care(payload: dict, who: str):
    """`POST /api/home/care {asset, title, due, recurrence}`: one recurring care task, area home, naming the asset."""
    from sd_db import assets, workflow
    from sd_db.database import transaction
    from sd_db.writes import set_item_fields

    if set(payload) != {"asset", "title", "due", "recurrence"} or payload["recurrence"] not in RECUR \
            or not isinstance(payload["title"], str) or not payload["title"].strip() or not isinstance(payload["due"], str):
        raise ValueError(f"Send asset, title, due (YYYY-MM-DD) and recurrence, one of {', '.join(RECUR)}.")
    asset = _id(payload["asset"])

    def write(connection):
        with transaction(connection):
            try:
                assets.get_asset(connection, asset)
            except assets.AssetRefused as refused:
                return 404, {"error": str(refused)}
            try:
                item = workflow.capture_task(connection, title=payload["title"].strip(), due=payload["due"],
                                             recurrence=payload["recurrence"], who=who or "dashboard")["item"]
            except (workflow.WorkflowError, ValueError) as error:
                return 400, {"error": str(error)}
            set_item_fields(connection, item["id"], fields={"area": "home", "asset": asset})
            return {"item": item["id"], "asset": asset, "due": item["due"]}

    return write
