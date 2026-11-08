"""HOA map attachments (sd:6690): an asset's documents and pictures, held in the HOA's Drive folders.

No new store: an asset's attachments are its Drive folder's listing. The design is
`docs/work/2026-10-30-hoa-attachments/design.md`; its failure table names each step's recovery.

- **Where.** The `hoa.water_system_folder` setting holds the Water System folder id, `hoa.light_system_folder` the
  Light System folder id, and `hoa.google_account` the account workspace-mcp reads them as. Subfolders are found by
  name (`place`): a valve or hydrant is `Main/<asset id>`, made on its first attachment; `WELL-A1` and `WELL-A2` are
  `Well A1` and `Well A2`; the tank is `Cistern`; the booster station is `Submarine`; the irrigation system, point or
  line, is `Irrigation` (sd:7667). A street light `LGT-n` is `LGT-n` under Light System, a folder the operator made;
  while `hoa.light_system_folder` is unset, a light lists no folder and an upload names the setting.
- **List**, `GET /api/hoa/attachments?asset=<id>`: the folder's items. A `.url` file is a Reference; its target is read
  from its text. A missing asset folder is an empty list, and nothing is made.
- **Picture**, `GET /api/hoa/attachments/picture?id=<file id>`: one picture's bytes, through workspace-mcp's download
  URL on its own host, so the page's thumbnails need no third-party image source.
- **Upload**, `POST /api/hoa/attach {"asset", "sha256", "name"}`: a file the page first staged at
  `POST /api/hoa/attach/file` (`source_intake.stage`), written into the folder. Only on the operator's press (hoa sd:6692).
- **Reference**, `POST /api/hoa/attach {"asset", "link", "name"?}`: a small `<name>.url` file naming a Drive link
  (operator ruling, note #36048): workspace-mcp cannot make a Drive shortcut.

Every Drive call goes through workspace-mcp (`research_slides._call`), so the dashboard holds no Google token.

**Home** (pack sd:8112) runs the same code as a second `Site`, `HOME`: `home.drive_folder` holds the root folder id
and `home.google_account` the account; an asset's folder is `<kind group>/<asset name>` (a koi's is under its pond's,
`Other/<pond>/<koi>`), each folder made on the first attachment. Unset, an asset lists no folder and an upload names
the setting. Its routes are `/api/home/attachments`, `/api/home/attachments/picture` and `/api/home/attach`.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import hoa_screen, notes_screen, research_slides, source_intake

__all__ = ["ACCOUNT_KEY", "FOLDER_KEY", "HOA", "HOME", "LIGHT_KEY", "PICTURE_BYTES", "SUFFIXES", "UPLOAD_BYTES", "Site",
           "listing", "picture", "place", "request"]

FOLDER_KEY = "hoa.water_system_folder"
#: sd:7667: the street lights' folders sit under their own Light System folder, not under Water System.
LIGHT_KEY = "hoa.light_system_folder"
ROOTS = {FOLDER_KEY: "Water System", LIGHT_KEY: "Light System"}
ACCOUNT_KEY = "hoa.google_account"
#: What Upload takes: pictures and documents. The bytes ride base64 in one workspace-mcp call, so the cap is modest.
SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".pdf", ".txt", ".md", ".doc", ".docx", ".xls", ".xlsx")
UPLOAD_BYTES = 20 * 1024 * 1024
#: A picture the page draws as a thumbnail; a larger one is a link only.
PICTURE_BYTES = 10 * 1024 * 1024
FOLDER_MIME = "application/vnd.google-apps.folder"
#: Drive and Docs links only: a Reference names a file in Drive.
LINK = re.compile(r"https://(drive|docs)\.google\.com/[^\s\"'<>]+")
ID = re.compile(r"[\w-]{10,200}")
ITEM = re.compile(r'^- Name: "(?P<name>.*)" \(ID: (?P<id>[\w-]+), Type: (?P<mime>[^,)]+).*?\) Link: (?P<link>\S+)$',
                  re.MULTILINE)
class Unknown(Exception):
    """A write workspace-mcp gave no clear answer to: whether Drive holds the file is not known."""


#: One attachment write at a time, so two first uploads to one valve make one folder, not two.
ATTACHING = threading.Lock()


def place(asset: dict) -> tuple[str, str, str | None]:
    """`(root setting, subfolder, asset folder or None)`; `ValueError` for an asset with no place."""
    kind = asset.get("type")
    if kind in ("valve", "hydrant"):
        return FOLDER_KEY, "Main", asset["id"]
    if asset.get("id") in ("WELL-A1", "WELL-A2"):
        return FOLDER_KEY, f"Well {asset['id'][-2:]}", None
    if kind == "tank":
        return FOLDER_KEY, "Cistern", None
    if kind == "booster":
        return FOLDER_KEY, "Submarine", None
    if kind in ("irrigation", "irrline"):
        return FOLDER_KEY, "Irrigation", None
    if kind == "light":
        return LIGHT_KEY, asset["id"], None
    raise ValueError(f"{asset.get('id')} ({kind}) has no attachment folder.")


def _call(tool: str, arguments: dict, what: str) -> str:
    """One workspace-mcp call: `ConnectionError` when it did nothing, `LookupError` when that is not known."""
    url, _, _ = notes_screen.workspace()
    return research_slides._call(url, tool, arguments, what)


def _first(parameters: dict, name: str):
    value = parameters.get(name)
    return value[0] if isinstance(value, list) and value else value


def _write(tool: str, arguments: dict, what: str) -> str:
    """The id of the file or folder the one write made: an answer naming no such one is `Unknown`, as is no answer."""
    try:
        said = _call(tool, arguments, what)
    except LookupError as error:
        raise Unknown(str(error)) from None
    # Only workspace-mcp's success line, for this name, confirms the write; any other text may hide an error.
    kind = "folder" if "folder_name" in arguments else "file"
    name = re.escape(arguments[f"{kind}_name"])
    made = re.search(rf"Successfully created {kind} '{name}' \(ID: ([\w-]+)\)", said)
    if not made:
        raise Unknown(f"workspace-mcp answered without a created {kind}: {said[:200]}")
    return made[1]


@dataclass(frozen=True)
class Site:
    """One map's attachments: its root settings by label, its account setting, where an asset files and how one is found.

    `place(asset)` is `(root setting, folders that must exist, folders made on the first attachment)`. `optional` are
    the settings whose absence lists an asset with no folder, Drive not asked; an upload still names them."""

    roots: dict
    account: str
    place: Callable[[dict], tuple[str, list, list]]
    asset: Callable[[object, object], dict]
    ident: type
    optional: frozenset = frozenset()


def _account(connection, site: Site) -> str:
    """The account alone (sd:7680): a picture is named by its file id, so no folder setting gates it; `LookupError`
    when unset."""
    from sd_db import writes

    account = writes.read_settings(connection, [site.account]).get(site.account)
    if not account:
        raise LookupError(f"{site.account} is not set: run sd config set {site.account} <value>.")
    return account


def _settings(connection, root: str, account_key: str) -> tuple[str, str]:
    """`(the root folder id named by setting `root`, the account)`; `LookupError` naming each one unset."""
    from sd_db import writes

    held = writes.read_settings(connection, [root, account_key])
    folder, account = held.get(root), held.get(account_key)
    if not folder or not account:
        missing = " and ".join(key for key, value in ((root, folder), (account_key, account)) if not value)
        raise LookupError(f"{missing} is not set: run sd config set {missing.split(' and ')[0]} <value>.")
    return folder, account


def _asset(connection, ident: object) -> dict:
    if not isinstance(ident, str) or not ident:
        raise ValueError("Name the asset by its id.")
    found = hoa_screen._geojson(hoa_screen._paths(hoa_screen.checkout(connection)[1])["assets"], hoa_screen.ASSET_KEYS,
                                "assets")
    if found.get("error"):
        raise LookupError(f"The asset map was not read: {found['error']}")
    for asset in found["features"]:
        if asset["id"] == ident:
            return asset
    raise ValueError(f"No asset {ident} on the map.")


def _children(account: str, folder: str) -> list[dict]:
    """The folder's whole listing; `LookupError` for any answer that is not one, so none reads as empty."""
    said = _call("list_drive_items", {"user_google_email": account, "folder_id": folder, "page_size": 200}, "the listing")
    if said.strip() == f"No items found in folder '{folder}'.":
        return []
    header = re.match(rf"Found (\d+) items in folder '{re.escape(folder)}' for ", said)
    rows = [match.groupdict() for match in ITEM.finditer(said)]
    # A nextPageToken is one page of a longer listing: a folder past it could be missed and made twice.
    if not header or int(header[1]) != len(rows) or "nextPageToken:" in said:
        raise LookupError(f"workspace-mcp's listing of {folder} is unreadable: {said[:200]}")
    return rows


def _one(account: str, parent: str, name: str) -> str | None:
    """The id of the one folder named `name` in `parent`; None for none, `LookupError` for two."""
    found = [row["id"] for row in _children(account, parent) if row["mime"] == FOLDER_MIME and row["name"] == name]
    if len(found) > 1:
        raise LookupError(f"Drive holds {len(found)} folders named {name}: {', '.join(found)}. Merge them in Drive.")
    return found[0] if found else None


def _folder(connection, ident: object, site: Site, *, make: bool) -> tuple[dict, str, str | None]:
    """`(asset, account, folder id)`: the asset's folder, made when `make` and missing; None when missing."""
    from sd_db import writes

    asset = site.asset(connection, ident)
    key, existing, made = site.place(asset)
    quiet = site.optional & {key, site.account}
    # sd:7667, sd:8112: an unset optional setting is no folder yet: an empty listing, Drive not asked; an upload
    # still refuses below, naming the setting.
    if quiet and not make and not all(writes.read_settings(connection, sorted(quiet)).values()):
        return asset, "", None
    folder, account = _settings(connection, key, site.account)
    for sub in existing:
        parent, folder = folder, _one(account, folder, sub)
        if folder is None:
            raise LookupError(f"The {site.roots[key]} folder has no folder named {sub}.")
    for own in made:
        parent, folder = folder, _one(account, folder, own)
        if folder is None:
            if not make:
                return asset, account, None
            folder = _write("create_drive_folder", {"user_google_email": account, "folder_name": own,
                                                    "parent_folder_id": parent}, "the folder")
    return asset, account, folder


def _kind(row: dict) -> str:
    if row["mime"] == FOLDER_MIME:
        return "folder"
    if row["name"].lower().endswith(".url"):
        return "link"
    return "picture" if row["mime"].startswith("image/") else "document"


def listing(connection, parameters: dict, site: Site | None = None):
    """`GET /api/hoa/attachments?asset=<id>`: `{"asset", "folder", "files"}`, or `(status, {"error"})`."""
    try:
        asset, account, folder = _folder(connection, _first(parameters, "asset"), site or HOA, make=False)
        files = []
        for row in _children(account, folder) if folder else []:
            entry = {**row, "kind": _kind(row)}
            if entry["kind"] == "link":
                text = _call("get_drive_file_content", {"user_google_email": account, "file_id": row["id"]}, "the link")
                target = re.search(r"^URL=(\S+)", text, re.MULTILINE)
                entry["target"] = target[1] if target and LINK.fullmatch(target[1]) else ""
            files.append(entry)
    except ValueError as error:
        return 400, {"error": str(error)}
    except (LookupError, ConnectionError) as error:
        return 503, {"error": f"The attachments were not read: {error}"}
    return {"asset": asset["id"], "folder": f"https://drive.google.com/drive/folders/{folder}" if folder else "",
            "files": files}


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=research_slides.SLIDES_TIMEOUT) as answer:  # noqa: S310 - checked host
        data = answer.read(PICTURE_BYTES + 1)
    if len(data) > PICTURE_BYTES:
        raise LookupError(f"the picture is over {PICTURE_BYTES // (1024 * 1024)} MB")
    return data


def picture(connection, parameters: dict, site: Site | None = None):
    """`GET /api/hoa/attachments/picture?id=<file id>`: `(200, bytes, mime)` for a picture, else `(status, {"error"})`."""
    ident = _first(parameters, "id")
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        return 400, {"error": "Name the picture by its Drive file id."}
    try:
        account = _account(connection, site or HOA)
        said = _call("get_drive_file_download_url", {"user_google_email": account, "file_id": ident}, "the download")
        found, mime = re.search(r"Download URL:\s*(http\S+)", said), re.search(r"MIME Type:\s*(image/[\w.+-]+)", said)
        if not mime:
            return 415, {"error": "That file is not a picture."}
        server = urllib.parse.urlsplit(notes_screen.workspace()[0])
        # Only workspace-mcp's own host: the URL comes back in a tool's text, and the server fetches it.
        if not found or urllib.parse.urlsplit(found[1]).netloc != server.netloc:
            return 502, {"error": "workspace-mcp gave no download URL on its own host."}
        return 200, _fetch(found[1]), mime[1]
    except (LookupError, ConnectionError, OSError) as error:
        return 503, {"error": f"The picture was not read: {error}"}


def _upload(connection, values: dict, site: Site) -> dict:
    sha, name = values.get("sha256"), values.get("name")
    if not isinstance(sha, str) or not source_intake.SHA.fullmatch(sha) or not isinstance(name, str):
        raise ValueError("Upload names a staged file by its sha256 and its name.")
    staged = source_intake._state() / "staged" / sha
    try:
        data = staged.read_bytes()
    except FileNotFoundError:
        raise ValueError(f"{name} is no longer staged: choose it again.") from None
    if len(data) > UPLOAD_BYTES or hashlib.sha256(data).hexdigest() != sha:
        raise ValueError(f"{name} is over {UPLOAD_BYTES // (1024 * 1024)} MB or changed since it was staged.")
    stored = source_intake._name(name, SUFFIXES)
    mime = mimetypes.guess_type(stored)[0] or "application/octet-stream"
    with ATTACHING:
        asset, account, folder = _folder(connection, values.get("asset"), site, make=True)
        made = _write("create_drive_file", {"user_google_email": account, "file_name": stored, "folder_id": folder,
                                           "base64_content": base64.b64encode(data).decode("ascii"), "base64_sha256": sha,
                                           "content_mime_type": mime, "mime_type": mime}, "the upload")
    staged.unlink(missing_ok=True)
    return {"asset": asset["id"], "name": stored, "id": made}


def _reference(connection, values: dict, site: Site) -> dict:
    link, name = values.get("link"), values.get("name") or "Drive link"
    if not isinstance(link, str) or not LINK.fullmatch(link.strip()):
        raise ValueError("Reference takes a drive.google.com or docs.google.com link.")
    if not isinstance(name, str):
        raise ValueError("A Reference's name is text.")
    stored = re.sub(r"[^A-Za-z0-9._ -]+", "-", name).strip(" -._")[:100] or "Drive link"
    with ATTACHING:
        asset, account, folder = _folder(connection, values.get("asset"), site, make=True)
        made = _write("create_drive_file", {"user_google_email": account, "file_name": f"{stored}.url",
                                           "folder_id": folder, "content": f"[InternetShortcut]\nURL={link.strip()}\n",
                                           "mime_type": "text/plain"}, "the reference")
    return {"asset": asset["id"], "name": f"{stored}.url", "id": made}


def request(values: dict, site: Site | None = None):
    """`POST /api/hoa/attach`'s checks; the write runs on the operator's press only."""
    site = site or HOA
    asset = values.get("asset")
    if set(values) - {"asset", "sha256", "name", "link"} or not isinstance(asset, site.ident) or isinstance(asset, bool):
        raise ValueError('Name the asset by its id. Send {"asset", "sha256", "name"} to upload or {"asset", "link", "name"} to reference.')
    if ("sha256" in values) == ("link" in values):
        raise ValueError("Attach either a staged file (sha256) or a link, not both.")
    act = _upload if "sha256" in values else _reference

    def write(connection):
        try:
            return 200, act(connection, values, site)
        except Unknown as error:
            return 504, {"error": f"Whether Drive holds the file is not known ({error}). Look in the asset's folder "
                                  "before you press again."}
        except (LookupError, ConnectionError) as error:
            return 503, {"error": f"Nothing was written: {error}"}

    return write


def _hoa_place(asset: dict) -> tuple[str, list, list]:
    key, sub, own = place(asset)
    return key, [sub], [own] if own else []


HOA = Site(roots=ROOTS, account=ACCOUNT_KEY, place=_hoa_place, asset=_asset, ident=str, optional=frozenset({LIGHT_KEY}))

HOME_FOLDER_KEY = "home.drive_folder"
HOME_ACCOUNT_KEY = "home.google_account"
#: The Map's kind groups (sd:8085, home-map.js GROUPS); a kind in none is Other's.
HOME_GROUPS = {"Gas": ("gas-line", "gas-shutoff"), "Water": ("water-line", "water-shutoff"),
               "Sewer": ("sewer-line", "sewer-shutoff"), "Electric": ("electric-line", "electric-shutoff"),
               "Comms": ("comms-line", "comms-shutoff"),
               "Irrigation": ("irrigation-line", "irrigation-valve", "irrigation-zone", "backflow"),
               "Energy": ("solar", "powerwall", "inverter", "ev-charger", "hvac"), "Plants": ("tree", "plant")}


def _home_place(asset: dict) -> tuple[str, list, list]:
    """`Other/<pond>/<koi>` for a fish, else `<kind group>/<name>`; every folder is made on the first attachment."""
    name = asset["name"].strip()
    if asset["kind"] == "koi":
        return HOME_FOLDER_KEY, [], ["Other", asset["pond"], name]
    group = next((word for word, kinds in HOME_GROUPS.items() if asset["kind"] in kinds), "Other")
    return HOME_FOLDER_KEY, [], [group, name]


def _home_asset(connection, ident: object) -> dict:
    from sd_db import assets

    if isinstance(ident, str) and ident.isdigit():
        ident = int(ident)
    if type(ident) is not int:
        raise ValueError("Name the asset by its id.")
    try:
        asset = assets.get_asset(connection, ident)
    except assets.AssetRefused as refused:
        raise ValueError(f"No asset {ident} on the map: {refused}.") from None
    if asset["kind"] == "koi":
        # ponytail: the Koi view's one pond (home_koi._pond); a fish names no pond of its own.
        ponds = assets.list_assets(connection, ("pond",))
        if not ponds:
            raise ValueError("A koi files under its pond's folder: add the pond first.")
        asset = {**asset, "pond": ponds[0]["name"].strip()}
    return asset


HOME = Site(roots={HOME_FOLDER_KEY: "Home Map"}, account=HOME_ACCOUNT_KEY, place=_home_place, asset=_home_asset, ident=int,
            optional=frozenset({HOME_FOLDER_KEY, HOME_ACCOUNT_KEY}))
