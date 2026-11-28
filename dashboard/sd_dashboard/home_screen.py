"""Home: the tiles behind the default UI's Home page (sd:2117).

The page is `v2/home.html`; it holds no tiles. It reads one JSON document,
`/api/home`, built here by `document`.

**Which tiles.** The design names the critical Home Assistant entities one
home has: alarm panels, locks, leak and smoke sensors. Those names describe
the operator's house, and this repository is public, so they live outside the
checkout (`home-tiles.conf.example` is the template), one line per tile:

    headline|<entity_id>|<name>[|<type>]          the lamp above the groups (the problems sensor)
    tile|<group>|<entity_id>|<name>[|<type>]      a tile, in its group, in file order

`<type>` is optional: `alarm`, `lock`, `toggle` or `sensor`, how the tile
reads its state. Without it the page types the tile by its domain; a virtual
binary sensor that works as a switch needs `toggle` said. Its commands follow
the domain alone (`SERVICES`).

A line this module cannot read is named in `config.problems` with its line
number and skipped; the other lines still count.

**Where the list lives (sd:3987).** The workflow database's setting row
`dashboard.home_tiles` (`KEY`) holds the text. Row wins: once the row exists,
`<config>/project-dashboard/home-tiles.conf` is not read; with no row it
answers as before. A row read that fails is unknown: the page says "tile list
not read", reads no file in its place and calls Home Assistant for nothing.
Home's "Edit tiles" opens on the row, else the file, else the example, and
`save` writes the row: it parses first (400, each problem with its line) and
refuses a `before` that is not the digest of the text the page showed (409).
The rows keep history, so Undo posts the previous text.

**Live state (sd:3427).** Each document makes one `GET /api/states` call to
Home Assistant, bounded by `TIMEOUT`, with the URL and token local-ha-mcp
uses: `HA_MCP_URL` and `HA_TOKEN` from the environment, else from
`<config>/ha-mcp/.env`, the environment winning as in `ha-mcp.sh`. The REST
base is that URL's scheme and host. Each tile gets `state` (its value, last
change, device class and unit) or `error`, a short name for why it has none.
A script or automation tile also gets `ran`, its last run: Home Assistant's
`last_triggered`, or None when the answer holds none (sd:5727).
A failed read leaves `reader.available` false with `reader.reason`, and every
tile carries the short error; an entity the answer does not list is
`NOT_LISTED` on its tile alone. The token goes only into the request header:
no document, error or log line holds it, nor the host. No proxy is used and
no redirect is followed, so the token goes to that host alone.

**Service calls (sd:4966).** `POST /api/home/service {entity_id, service, before}` (`service`) calls one
Home Assistant service on one tile, `POST /api/services/<domain>/<service>` with the read's base and token, bounded
by `SERVICE_TIMEOUT`. It refuses (400, nothing sent) an entity the tile list does not name, a service `SERVICES` does
not allow for the entity's domain; a lock, alarm or cover service runs with no typed confirm (sd:5479); a `before`
that is not the digest of the tile text the page showed is a 409. Each call is one record in the command journal
(`runner_exec.execution_journal`, Activity's "Command journal"), on the Home page's item `JOURNAL_ITEM`: the output
file is made, with the dispatch line, before the request leaves, and the note is written with the outcome after it,
so a stop between the two leaves the file, never a note that waits for a reconcile no palette request can finish.
Home Assistant's own reason for a refusal (401, 403, 400) reaches the page. Then the tile's state is read again with
`GET /api/states/<entity_id>`. `writes.available` is true once the read worked; `writes.services` is `SERVICES`.

**Web UI (sd:6163).** The page's first tile links to `WEB`, `/home/assistant`, on the dashboard itself; the
server answers it with a 302 to the REST base above (`web`), or a 503 naming why there is none. `document`
carries `web` as `{path, available, reason}`, so the page never holds the host either.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import sqlite3
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .documents import _config_dir, pipe_lines

__all__ = ["CONFIG", "JOURNAL_ITEM", "KEY", "NOT_LISTED", "NO_TILES", "ROW_SOURCE", "SERVICES", "SERVICE_TIMEOUT", "TIMEOUT", "TYPES",
           "WEB", "document", "parse", "save", "service", "web"]

#: In this machine's config directory, never supplied by a request. The checkout ships `home-tiles.conf.example` only.
CONFIG = _config_dir() / "home-tiles.conf"

#: The tracked template the editor opens on when neither the row nor the file holds a list.
EXAMPLE = Path(__file__).resolve().parents[1] / "home-tiles.conf.example"

#: The setting row that holds the tile list (sd:3987); once written it wins over `CONFIG`.
KEY = "dashboard.home_tiles"

#: How the page names the row as its source.
ROW_SOURCE = f"workflow database setting {KEY}"

#: The dashboard path that opens Home Assistant's web UI (sd:6163): a 302 to the REST base, so no page holds the host.
WEB = "/home/assistant"

#: Seconds for each socket step of the one Home Assistant read; the page waits on it.
TIMEOUT = 3.0

#: The most bytes of `/api/states` read; a larger answer is a named error, not a partial list.
LIMIT = 16 * 1024 * 1024

#: The Home Assistant services a tile may call, by the entity's domain, each with its risk (sd:4966). `undo` and `safe`
#: run on one click; the security domains are `safe` too, as the operator ruled out every Home confirm (sd:5479).
#: Every other domain and service is refused.
_SWITCH = {"turn_on": "undo", "turn_off": "undo", "toggle": "undo"}
SERVICES = {
    "light": _SWITCH, "switch": _SWITCH, "fan": _SWITCH, "input_boolean": _SWITCH,
    "scene": {"turn_on": "safe"}, "script": {"turn_on": "safe"}, "button": {"press": "safe"},
    "lock": {"lock": "safe", "unlock": "safe"},
    "alarm_control_panel": {"alarm_arm_home": "safe", "alarm_arm_away": "safe", "alarm_arm_night": "safe", "alarm_disarm": "safe"},
    "cover": {"open_cover": "safe", "close_cover": "safe"},
}

#: Seconds for each socket step of one service call: Home Assistant answers once the service has run, and a lock takes a while.
SERVICE_TIMEOUT = 10.0

#: The item each service call's journal note goes on: the Home page's (sd:2117), which `v2/pages/home.py` names too.
JOURNAL_ITEM = 2117

#: The most bytes of a refusal's body read for Home Assistant's reason, and the most characters of it shown.
REASON_BYTES, REASON_CHARS = 2048, 200

#: The domains whose tile shows its last run, `last_triggered` (sd:5727).
RUNS = ("script", "automation")

#: A tile whose entity the read answer does not list.
NOT_LISTED = "not in Home Assistant"

#: The reader's reason when there is no tile list: nothing to read, so nothing is called.
NO_TILES = "no tile list, so Home Assistant was not read"

#: An HA entity id: `<domain>.<object_id>`, lower case, as HA writes them.
ENTITY = re.compile(r"[a-z_]+\.[a-z0-9_]+")

#: The ways a tile can read its state (the design's object types on Home).
TYPES = ("alarm", "lock", "toggle", "sensor")


def parse(text: str) -> tuple[dict | None, list[dict], list[str]]:
    """The headline, the groups in file order, and one problem per line that was not read."""
    headline, groups, problems, seen = None, {}, [], set()
    for number, _, fields in pipe_lines(text):
        kind, kind_type = fields[0], None
        if kind == "headline" and len(fields) in (3, 4):
            group, entity, name = None, fields[1], fields[2]
            kind_type = fields[3] if len(fields) == 4 else None
        elif kind == "tile" and len(fields) in (4, 5):
            group, entity, name = fields[1], fields[2], fields[3]
            kind_type = fields[4] if len(fields) == 5 else None
        else:
            problems.append(f"line {number}: expected headline|<entity_id>|<name>[|<type>] or tile|<group>|<entity_id>|<name>[|<type>]")
            continue
        if kind_type is not None and kind_type not in TYPES:
            problems.append(f"line {number}: type {kind_type!r} is not one of {', '.join(TYPES)}")
            continue
        if not ENTITY.fullmatch(entity):
            problems.append(f"line {number}: {entity!r} is not an entity id (domain.object_id)")
            continue
        if not name or (kind == "tile" and not group):
            problems.append(f"line {number}: a tile needs a group and a name")
            continue
        if entity in seen:
            problems.append(f"line {number}: {entity} is listed twice")
            continue
        if kind == "headline" and headline is not None:
            problems.append(f"line {number}: a second headline")
            continue
        seen.add(entity)
        tile = {"id": entity, "name": name, "domain": entity.split(".", 1)[0], "type": kind_type}
        if kind == "headline":
            headline = tile
        else:
            groups.setdefault(group, []).append(tile)
    return headline, [{"name": name, "tiles": tiles} for name, tiles in groups.items()], problems


class _Failed(Exception):
    """A read that gave no states: `short` goes on each tile, `reason` on the page."""

    def __init__(self, short: str, reason: str):
        super().__init__(reason)
        self.short, self.reason = short, reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: following it would send the token to wherever it points."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _settings(environ) -> tuple[str, str]:
    """local-ha-mcp's REST base and token: the environment, an empty value too, wins over `<config>/ha-mcp/.env`; a `change-me` value is unset."""
    from sd_db import config as system_tools_config

    try:
        saved = system_tools_config.read_env("ha-mcp", environ)
    except (OSError, ValueError) as error:  # ValueError: a bad byte, or a line the reader refuses (sd:10424)
        raise _Failed("HA not configured", f"<config>/ha-mcp/.env was not read: {error.__class__.__name__}") from None
    values = {}
    for name in ("HA_TOKEN", "HA_MCP_URL"):
        value = system_tools_config.setting(name, saved, environ) or ""
        values[name] = "" if "change-me" in value else value
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise _Failed("HA not configured", f"{' and '.join(missing)} not set: export {'it' if len(missing) == 1 else 'them'}, or copy "
                      "local-ha-mcp/.env.example to <config>/ha-mcp/.env and fill it in")
    try:
        url = urlsplit(values["HA_MCP_URL"])
        _ = url.port  # a port that is not a number raises here
    except ValueError:
        url = None
    if url is None or url.scheme not in ("http", "https") or not url.hostname:
        raise _Failed("HA not configured", "HA_MCP_URL is not an http or https URL")
    return f"{url.scheme}://{url.netloc}", values["HA_TOKEN"]


def web(environ=None) -> tuple[str | None, str]:
    """Where `WEB` sends the browser: (the REST base with a trailing slash, "") or (None, why there is none)."""
    try:
        return _settings(os.environ if environ is None else environ)[0] + "/", ""
    except _Failed as failed:
        return None, failed.reason


def _read_states(environ=None, timeout: float = TIMEOUT) -> dict[str, dict]:
    """Every entity's state from one `GET /api/states`, by entity id. Raises `_Failed` naming why there is none."""
    base, token = _settings(os.environ if environ is None else environ)
    request = urllib.request.Request(f"{base}/api/states", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read(LIMIT + 1)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise _Failed("HA refused the token", f"Home Assistant refused the token (HTTP {error.code})") from None
        if 300 <= error.code < 400:
            raise _Failed("HA answer not read", f"Home Assistant answered with a redirect (HTTP {error.code}); it is not followed") from None
        raise _Failed(f"HA error {error.code}", f"Home Assistant answered HTTP {error.code}") from None
    except (TimeoutError, socket.timeout):
        raise _Failed("HA timed out", f"Home Assistant gave no answer within {timeout:g} s") from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise _Failed("HA timed out", f"Home Assistant gave no answer within {timeout:g} s") from None
        raise _Failed("HA did not answer", f"Home Assistant did not answer: {error.reason.__class__.__name__}") from None
    except (OSError, http.client.HTTPException) as error:
        raise _Failed("HA did not answer", f"Home Assistant did not answer: {error.__class__.__name__}") from None
    if len(body) > LIMIT:
        raise _Failed("HA answer not read", f"the /api/states answer is over {LIMIT // (1024 * 1024)} MiB")
    try:
        rows = json.loads(body)
    except ValueError:
        rows = None
    if not isinstance(rows, list) or not all(map(_row, rows)):
        raise _Failed("HA answer not read", "the /api/states answer is not a list of states")
    return {row["entity_id"]: row for row in rows}


def _row(row) -> bool:
    """A state row as Home Assistant writes them; one of any other shape fails the whole read."""
    return (isinstance(row, dict) and isinstance(row.get("entity_id"), str) and isinstance(row.get("state"), str)
            and isinstance(row.get("last_changed"), str) and isinstance(row.get("attributes", {}), dict))


def _state(row: dict) -> dict:
    """What a tile shows of one state row: no other attribute leaves this module (an `entity_picture` carries a token)."""
    attributes = row.get("attributes") or {}
    device_class, unit = (attributes.get(name) for name in ("device_class", "unit_of_measurement"))
    state = {"value": row["state"], "changed": row["last_changed"],
             "device_class": device_class if isinstance(device_class, str) else None, "unit": unit if isinstance(unit, str) else None}
    if row["entity_id"].split(".", 1)[0] in RUNS:
        ran = attributes.get("last_triggered")
        state["ran"] = ran if isinstance(ran, str) else None
    return state


class _Unread(Exception):
    """The setting row was not read: its answer is unknown, never "no row"."""


def _current(connection, config: Path) -> tuple[str, str | None]:
    """The tile text the page shows and where it came from: `database` (row wins; a cleared row holds None), else `file`,
    else `missing`. Raises `_Unread` on a failed row read, and `OSError` or `UnicodeDecodeError` on an unreadable file."""
    from sd_db import writes

    if connection is None:
        raise _Unread("no workflow database connection")
    if not hasattr(writes, "read_settings"):
        raise _Unread("the command pack predates setting rows (sd:3989); update it")
    try:
        held = writes.read_settings(connection, [KEY])
    except Exception as error:  # any failed read is unknown, named; the file is not read in its place
        raise _Unread(f"{error.__class__.__name__}: {error}") from None
    if KEY in held:
        return "database", held[KEY]
    try:
        return "file", config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None


def _digest(origin: str, text: str | None) -> str:
    """What a save's `before` must equal: the text the page showed, and where it came from."""
    return hashlib.sha256(f"{origin}\n{text or ''}".encode("utf-8")).hexdigest()


def _editor(origin: str, text: str | None) -> dict:
    """What "Edit tiles" opens on: the row, else the file, else the example."""
    if text is None:
        try:
            shown = EXAMPLE.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            shown = ""
        return {"from": "example", "text": shown, "digest": _digest(origin, text)}
    return {"from": origin, "text": text, "digest": _digest(origin, text)}


def document(connection, *, now: str, config: Path | None = None, environ=None, timeout: float = TIMEOUT) -> dict:
    """The Home page's document: the configured tiles, each with its live state or a named error."""
    config = CONFIG if config is None else config
    source, editor, headline, groups, empty = f"<config>/project-dashboard/{config.name}", None, None, [], NO_TILES
    try:
        origin, text = _current(connection, config)
    except _Unread as unread:
        state, source, problems = "error", ROW_SOURCE, [f"tile list not read: {unread}"]
        empty = problems[0]
    except (OSError, UnicodeDecodeError) as error:
        state, problems = "error", [f"not read: {error.__class__.__name__}"]
    else:
        source = ROW_SOURCE if origin == "database" else source
        editor = _editor(origin, text)
        if text is None:
            state, problems = "missing", []
        else:
            headline, groups, problems = parse(text)
            state = "partial" if problems else "read"
    tiles = ([headline] if headline else []) + [tile for group in groups for tile in group["tiles"]]
    reader, states, short = {"available": False, "reason": empty}, {}, empty
    if tiles:
        try:
            states, reader = _read_states(environ, timeout), {"available": True, "reason": ""}
        except _Failed as failed:
            reader, short = {"available": False, "reason": failed.reason}, failed.short
    opens, why = web(environ)
    for tile in tiles:
        row = states.get(tile["id"])
        tile["state"] = _state(row) if row else None
        tile["error"] = None if row else NOT_LISTED if reader["available"] else short
    return {
        "read": now,
        "config": {"state": state, "source": source, "problems": problems, "editor": editor},
        "reader": reader,
        "writes": {"available": reader["available"], "reason": "" if reader["available"] else reader["reason"], "services": {domain: dict(names) for domain, names in SERVICES.items()}},
        "headline": headline,
        "groups": groups,
        "web": {"path": WEB, "available": opens is not None, "reason": why},
    }


def _refused(text: str) -> str:
    """Why Save refuses `text`, or "" when it saves: a line `parse` cannot read, no tile, or over the row's limit."""
    from sd_db import writes

    headline, groups, problems = parse(text)
    if problems:
        return "; ".join(problems)
    if headline is None and not groups:
        return "it names no tile"
    if len(text) > writes.SETTING_VALUE_LENGTH:
        return f"it is over {writes.SETTING_VALUE_LENGTH} characters"
    return ""


def save(payload) -> object:
    """`POST /api/home/tiles {text, before}` (sd:3987): the write that sets the tile row, or `ValueError` (400).

    `before` is the editor's digest of the text the page showed. The write
    reads that text again and answers 409 when it differs; `set_setting` then
    checks the row in its own transaction, so a save between the two is a 409 too.
    """
    from sd_db import writes

    if set(payload) != {"text", "before"} or not isinstance(payload["text"], str) or not isinstance(payload["before"], str):
        raise ValueError("Expected {text, before}: the tile list and the digest of the text the page showed.")
    text = payload["text"]
    refused = _refused(text)
    if refused:
        raise ValueError(f"The tile list was not saved: {refused}.")
    stale = {"error": "The tile list changed since this page read it; nothing was saved. Read it again.", "reload": True}

    def write(connection):
        try:
            origin, shown = _current(connection, CONFIG)
        except _Unread as unread:
            return 503, {"error": f"tile list not read: {unread}; nothing was saved"}
        if _digest(origin, shown) != payload["before"]:
            return 409, stale
        try:
            writes.set_setting(connection, KEY, text, before=shown if origin == "database" else None)
        except writes.SettingStale:
            return 409, stale
        # Undo posts `previous`, so a text Save would refuse (a file with a skipped line) offers none.
        previous = shown if shown and not _refused(shown) else ""
        return {"saved": KEY, "text": text, "digest": _digest("database", text), "previous": previous}

    return write


def _reason(error: urllib.error.HTTPError) -> str:
    """Home Assistant's own words for a refusal: the `message` of a JSON body, else its first line, short and printable."""
    try:
        body = error.read(REASON_BYTES).decode("utf-8", "replace")
    except (OSError, http.client.HTTPException):
        body = ""
    try:
        value = json.loads(body)
    except ValueError:
        value = None
    text = value["message"] if isinstance(value, dict) and isinstance(value.get("message"), str) else (body.strip().splitlines() or [""])[0]
    text = "".join(ch if ch.isprintable() else " " for ch in text).strip()[:REASON_CHARS]
    return text or "no reason given"


def _call(base: str, token: str, domain: str, name: str, entity: str, timeout: float) -> dict:
    """One `POST /api/services/<domain>/<name>`: `exit` 0 when Home Assistant ran it, 1 when it refused or was not
    reached, None when the request left and no answer came, so whether it ran is unknown."""
    request = urllib.request.Request(f"{base}/api/services/{domain}/{name}", data=json.dumps({"entity_id": entity}).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return {"exit": 0, "status": response.status, "reason": ""}
    except urllib.error.HTTPError as error:
        try:
            reason = _reason(error)
        finally:
            error.close()
        verb = "refused" if error.code < 500 else "failed while it ran"
        return {"exit": 1, "status": error.code, "reason": f"Home Assistant {verb} {domain}.{name} (HTTP {error.code}): {reason}"}
    except (TimeoutError, socket.timeout):
        pass
    except urllib.error.URLError as error:
        # A refused connection or an unknown host sent nothing; any other failure may have come after the request left.
        if isinstance(error.reason, (ConnectionRefusedError, socket.gaierror)):
            return {"exit": 1, "status": None, "reason": f"Home Assistant did not answer: {error.reason.__class__.__name__}; nothing was sent"}
        if not isinstance(error.reason, (TimeoutError, socket.timeout)):
            return {"exit": None, "status": None, "reason": f"Home Assistant's answer was lost ({error.reason.__class__.__name__}); {domain}.{name} may have run"}
    except (OSError, http.client.HTTPException) as error:
        return {"exit": None, "status": None, "reason": f"Home Assistant's answer was lost ({error.__class__.__name__}); {domain}.{name} may have run"}
    return {"exit": None, "status": None, "reason": f"Home Assistant gave no answer within {timeout:g} s; {domain}.{name} may have run"}


def _reread(base: str, token: str, entity: str, timeout: float) -> tuple[dict | None, str]:
    """The tile's state after the call, from `GET /api/states/<entity_id>`, or None and why it was not read."""
    request = urllib.request.Request(f"{base}/api/states/{entity}", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            row = json.loads(response.read(LIMIT))
    except urllib.error.HTTPError as error:
        error.close()
        return None, f"the state was not read again: HTTP {error.code}"
    except (OSError, http.client.HTTPException, ValueError) as error:
        return None, f"the state was not read again: {error.__class__.__name__}"
    if not _row(row) or row["entity_id"] != entity:
        return None, "the state was not read again: the answer is not this entity's state"
    return _state(row), ""


def _journal(connection, who: str, domain: str, name: str, entity: str):
    """The journal record of one call, opened before it leaves: (output file, finish). `finish(outcome)` writes the
    response line and then the note, ended, with its exit code (None: unknown), and answers the note id."""
    from sd_db import runner_exec, writes

    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    value = {"version": 1, "item": JOURNAL_ITEM, "command": ["home-assistant", f"{domain}.{name}", entity], "scope": "home-assistant",
             "service": f"{domain}.{name}", "entity_id": entity, "output_path": str(database.parent / "executions" / f"{uuid.uuid4().hex}.log")}
    output = runner_exec.open_output(value)  # private, exclusive and fsynced, as a palette run's
    started = writes.now()
    try:
        output.write((json.dumps({"event": "dispatch", "service": value["service"], "entity_id": entity}) + "\n").encode())
        os.fsync(output.fileno())
    except BaseException:
        output.close()
        raise

    def finish(outcome: dict) -> tuple[int | None, str]:
        # The call has happened: a failed journal write is named beside its outcome, never put in the outcome's place.
        lost = ""
        try:
            output.write((json.dumps({"event": "response", "exit_code": outcome["exit"], "status": outcome["status"], "reason": outcome["reason"]}) + "\n").encode())
            os.fsync(output.fileno())
        except OSError as error:
            lost = f"; the output file has no response line ({error.__class__.__name__})"
        finally:
            output.close()
        from sd_db.database import transaction
        from sd_db.errors import SdDbError

        try:
            with transaction(connection):
                note = writes.add_note(connection, JOURNAL_ITEM, "exec", "{}", session=who, started=started, ended=writes.now(),
                                       exit_code=outcome["exit"], output_path=value["output_path"])
                # The journal reads a version 1 record whose `note` is its own id, as `runner_exec.prepare` writes it.
                writes.set_note_body(connection, note, json.dumps({**value, "note": note, "status": outcome["status"]}, sort_keys=True))
        except (sqlite3.Error, SdDbError) as error:
            return None, f"the journal note was not written ({error.__class__.__name__}: {error}); its output is {Path(value['output_path']).name}{lost}"
        return note, lost.lstrip("; ")

    finish.abandon = output.close  # a stop before the outcome: the file keeps its dispatch line and no note is written
    return finish


def service(payload, who: str, *, environ=None, timeout: float = SERVICE_TIMEOUT) -> object:
    """`POST /api/home/service {entity_id, service, before}` (sd:4966): the write that calls one Home Assistant
    service on one tile, or `ValueError` (400) before anything is read or sent."""
    keys = set(payload)
    if keys != {"entity_id", "service", "before"} or not all(isinstance(payload[key], str) for key in keys):
        raise ValueError("Expected {entity_id, service, before}: the tile, the service and the digest of the tile list the page showed.")
    entity, name = payload["entity_id"], payload["service"]
    if not ENTITY.fullmatch(entity):
        raise ValueError(f"{entity!r} is not an entity id (domain.object_id); nothing was sent.")
    domain = entity.split(".", 1)[0]
    allowed = SERVICES.get(domain, {})
    if name not in allowed:
        offered = f"it allows {', '.join(allowed)} on {domain}" if allowed else f"it calls no {domain} service"
        raise ValueError(f"{domain}.{name} is not a service the dashboard calls: {offered}. Nothing was sent.")
    stale = {"error": "The tile list changed since this page read it; nothing was sent. Read it again.", "reload": True}

    def write(connection):
        from sd_db import workflow
        from sd_db.database import refuse_hub_only

        try:
            origin, text = _current(connection, CONFIG)
        except _Unread as unread:
            return 503, {"error": f"tile list not read: {unread}; nothing was sent"}
        if _digest(origin, text) != payload["before"]:
            return 409, stale
        headline, groups, _ = parse(text or "")
        if entity not in {tile["id"] for tile in ([headline] if headline else []) + [t for g in groups for t in g["tiles"]]}:
            return 400, {"error": f"{entity} is not a tile in the tile list; nothing was sent."}
        try:
            base, token = _settings(os.environ if environ is None else environ)
        except _Failed as failed:
            return 503, {"error": f"{failed.reason}; nothing was sent"}
        try:
            workflow.item_state(connection, JOURNAL_ITEM)
        except workflow.MissingItem:
            return 503, {"error": f"the command journal's item sd:{JOURNAL_ITEM} is missing; nothing was sent"}
        refuse_hub_only(connection, "the runner's executions directory")
        finish = _journal(connection, who, domain, name, entity)
        try:
            outcome = _call(base, token, domain, name, entity, timeout)
        except BaseException:
            finish.abandon()
            raise
        note, unjournalled = finish(outcome)
        journal = {"note": note, "journal": unjournalled}
        if outcome["exit"] is None:
            return 504, {"error": f"{outcome['reason']}. Read the tiles again before you retry.", "unknown": True, **journal}
        if outcome["exit"]:
            # A 4xx is a refusal; a 5xx is Home Assistant failing while it ran the service, which may have done part of it.
            after = "Nothing changed." if outcome["status"] is None or outcome["status"] < 500 else "It may have partly run; read the tiles again."
            return 502, {"error": f"{outcome['reason']}. {after}", "status": outcome["status"], **journal}
        state, unread = _reread(base, token, entity, min(timeout, TIMEOUT))
        return {"sent": f"{domain}.{name}", "entity_id": entity, "state": state, "reread": unread, **journal}

    return write
