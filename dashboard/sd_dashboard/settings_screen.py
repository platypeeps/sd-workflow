"""Settings' Fleet section (sd:3989): the `sd.*` keys whose one value serves every machine.

The pack keeps them as setting rows in the workflow database (`sd_lib.FLEET_KEYS`). This page reads them with
`sd config list sd --json` and writes one with `sd config set sd.<key> <value>`: the pack holds each key's pattern,
description and the row-wins rule, so none is copied here. Each row shows its value, its source (`database`, `file`
or `default`) and its description; a key whose pattern is a list of words gets a select.

A failed list is named and lists nothing. A refused set answers sd's reason (400). A set that runs past `SD_SECONDS`
may have committed before it was stopped, so its outcome is unknown: it answers 504 with `unknown`, and the page reads
the value again. There is no stale-write check: two concurrent manual writes are outside the threat model, and the
answer carries the value the set replaced, so the page can say when it differs from the one it showed.

The Sweeps section (sd:5109) holds the Work and Personal sweeps' settings, `realm_sweep.SETTINGS`, and the Dashboard
section the adversarial review's model and effort (`adversarial.SETTINGS`), Home's Drive keys (`hoa_attach.HOME_SETTINGS`), the Deep review's model, effort, bound and
sub-agent cap (`adversarial.DEEP`, sd:5784) and the dashboard lists (`setting_rows.LISTS`):
setting rows read and written on the page's own connection (`sd_db.writes`), as no `sd config` prefix declares them.

- A single value posts `/api/settings/row {key, value, before}`: `before` is the row value the page showed, a row that
  changed since refuses with 409, and an empty value unsets.
- A list posts `/api/settings/list {key, text, before}`, as Home's tiles do (sd:3987): `before` is the digest of the
  source and text the page showed (`setting_rows.digest`). The text must parse with the list's own grammar (400, each
  problem named). The write reads the source and text again and refuses a digest that differs (409): a clear or an
  edit of the old file since the read changes it. `set_setting` then checks the row in its own transaction. An empty
  text writes a cleared row, so the old file is not read again.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess

__all__ = ["FLEET", "document", "request", "request_list", "request_row", "rows"]

#: An `sd config` run's ceiling: one pack start and one small read or write.
SD_SECONDS = 5.0

#: The pack's `sd_lib.FLEET_KEYS`, in the order the page lists them; `test_v2_settings` pins the two equal.
FLEET = ("fleet_owners", "copilot_review", "hub_name", "status_companions",
         "notion_private_folder", "notion_team_folder", "notion_experiments", "notion_experiment_tasks")

#: Plain names for the deep review's settings, where the key's last word is not one.
DEEP_NAMES = {"bound_minutes": "time limit", "agents": "most helper agents at once"}

#: A pattern that is a list of words, which the page offers as a select.
CHOICES = re.compile(r"[a-z]+(\|[a-z]+)+")

#: What `sd config set` prints for a set: `sd.<key>: <was> -> <value>`, and ` (workflow database)` when it wrote the row.
SET_LINE = re.compile(r"sd\.(\w+): (.*) -> (.*?)( \(workflow database\))?")


def _sd(*args: str) -> subprocess.CompletedProcess:
    """One `sd` run within `SD_SECONDS`; `ValueError` when sd is not on PATH, `TimeoutExpired` past the bound."""
    try:
        return subprocess.run(["sd", *args], capture_output=True, text=True, timeout=SD_SECONDS, check=False)
    except FileNotFoundError:
        raise ValueError("sd is not on the dashboard's PATH") from None


def _said(done: subprocess.CompletedProcess) -> str:
    return (done.stderr.strip().splitlines() or [""])[0]


def _fleet() -> list[dict]:
    try:
        done = _sd("config", "list", "sd", "--json")
    except subprocess.TimeoutExpired:
        raise ValueError(f"sd config list sd ran past its {SD_SECONDS:g} seconds") from None
    if done.returncode:
        raise ValueError(f"sd config list sd exited {done.returncode}" + (f": {_said(done)}" if _said(done) else ""))
    try:
        listed = json.loads(done.stdout)
    except ValueError:
        listed = None
    if not isinstance(listed, dict):
        raise ValueError("sd config list sd --json printed no JSON object")
    rows = []
    for key in FLEET:
        spec = listed.get(key)
        if not isinstance(spec, dict) or spec.get("source") not in ("database", "file", "default"):
            raise ValueError(f"sd config list sd --json gives no sd.{key} with its source; the pack predates fleet settings")
        pattern = spec.get("pattern") if isinstance(spec.get("pattern"), str) else ""
        rows.append({"key": key, "value": spec.get("value") if isinstance(spec.get("value"), str) else None,
                     "source": spec["source"], "description": str(spec.get("description") or ""),
                     "choices": pattern.split("|") if CHOICES.fullmatch(pattern) else None})
    return rows


def document(*, now: str) -> dict:
    """The Fleet section: each fleet key's row, or None with the reason the list was not read."""
    try:
        return {"read": now, "fleet": _fleet(), "reason": ""}
    except ValueError as failed:
        return {"read": now, "fleet": None, "reason": str(failed)}


def request(payload) -> object:
    """`POST /api/settings/fleet {key, value}`: the write that runs `sd config set`, or `ValueError` (400) before sd runs."""
    key, value = payload.get("key"), payload.get("value")
    if set(payload) != {"key", "value"} or key not in FLEET or not isinstance(value, str):
        raise ValueError(f"Expected {{key, value}}, the key one of {', '.join(FLEET)}.")
    value = value.strip()
    if not value or value.startswith("-"):
        raise ValueError(f"sd.{key} takes a value; unset it with sd config unset sd.{key}.")
    return lambda connection: _set(key, value)


def _set(key: str, value: str):
    try:
        done = _sd("config", "set", f"sd.{key}", value)
    except subprocess.TimeoutExpired:
        return 504, {"error": f"sd config set ran past its {SD_SECONDS:g} seconds, so whether sd.{key} changed is unknown; "
                              "read it again", "unknown": True}
    if done.returncode:
        raise ValueError(f"sd config set sd.{key} refused" + (f": {_said(done)}" if _said(done) else f" (exit {done.returncode})"))
    match = SET_LINE.fullmatch(done.stdout.strip())
    if not match or match[1] != key:
        return 502, {"error": f"sd config set printed {done.stdout.strip()[:200]!r}, so what it set is unknown; read it again",
                     "unknown": True}
    return {"key": key, "value": match[3], "was": None if match[2] == "(unset)" else match[2], "database": bool(match[4])}


def _scalars() -> dict:
    """Every single-value row the page edits, by key: `(description, pattern or None)`."""
    from .adversarial import DEEP, PATTERNS
    from .adversarial import SETTINGS as REVIEW
    from .hoa_attach import HOME_SETTINGS
    from .home_map import EXTENT_KEY
    from .realm_sweep import SETTINGS as SWEEP

    return {**{key: (text, None) for key, text in SWEEP.items()}, **{key: (text, PATTERNS[key]) for key, text in REVIEW.items()},
            EXTENT_KEY: (EXTENT_TEXT, None), **HOME_SETTINGS,
            **{key: (f"The deep review's {DEEP_NAMES.get(key.rsplit('.', 1)[1], key.rsplit('.', 1)[1])}: {wants}. Unset is {default}.", pattern)
               for key, (default, pattern, wants) in DEEP.items()}}


#: What Settings says about Home's map area (sd:8110); the Map view's Area control writes the same row.
EXTENT_TEXT = ("The area Home's Map covers around the address (pack sd:8110): west,east,south,north in feet, each "
               "50 to 2000. Unset is 200,800,200,200. The Map view's Area control edits it too.")


def _list(key: str):
    """A list's old file (read at call time, so a test can point it elsewhere) and its parser: text to problems."""
    from . import (
        health_collectors,
        hoa_screen,
        home_koi,
        home_map,
        places,
        reports_screen,
    )

    return {"dashboard.realms": (places.REALMS_CONFIG, lambda text: places.parse_realms(text)[1]),
            "dashboard.report_families": (reports_screen.FAMILIES_CONFIG, lambda text: reports_screen.parse_families(text)[1]),
            "dashboard.hoa_thresholds": (hoa_screen.THRESHOLDS, hoa_screen.parse_thresholds),
            "dashboard.home_map": (home_map.state_dir() / "home-map.conf", lambda text: home_map.parse(text)[1]),
            "dashboard.koi_water": (home_map.state_dir() / "koi-water.conf", lambda text: home_koi.parse(text)[1]),
            "dashboard.disk_storage": (health_collectors._storage_config(), lambda text: health_collectors.parse_storage(text)[1])}[key]


def rows(connection) -> dict:
    """The Sweeps and Dashboard sections: each setting's row, or None with the reason the database did not answer."""
    from sd_db import writes
    from sd_db.errors import SdDbError

    from . import setting_rows
    from .adversarial import DEEP
    from .adversarial import SETTINGS as REVIEW
    from .hoa_attach import HOME_SETTINGS
    from .home_map import EXTENT_KEY
    from .realm_sweep import SETTINGS as SWEEP

    def scalar(key, text, held, pattern=None):
        value = held.get(key)
        return {"key": key, "value": value, "row": value, "source": "database" if value else "unset", "description": text,
                "choices": pattern.split("|") if pattern and CHOICES.fullmatch(pattern) else None}

    out = {}
    try:
        held = writes.read_settings(connection, list(SWEEP))
    except (SdDbError, sqlite3.Error) as failed:
        out.update(sweeps=None, sweeps_reason=f"the workflow database did not answer: {failed}")
    else:
        out.update(sweeps=[scalar(key, text, held) for key, text in SWEEP.items()], sweeps_reason="")
    try:
        held = writes.read_settings(connection, [*REVIEW, *DEEP, EXTENT_KEY, *HOME_SETTINGS])
        lists = []
        for key, (_, text) in setting_rows.LISTS.items():
            try:
                origin, found = setting_rows.current(connection, key, _list(key)[0])
            except (OSError, UnicodeDecodeError) as error:
                origin, found = "unreadable", f"the old file did not read: {error.__class__.__name__}"
            lists.append({"key": key, "value": found if origin != "unreadable" else None, "lines": True,
                          "source": {"database": "database" if found is not None else "unset", "file": "file"}.get(origin, "unset"),
                          "problem": found if origin == "unreadable" else "", "digest": setting_rows.digest(origin, found),
                          "description": text, "choices": None})
    except (SdDbError, sqlite3.Error, LookupError) as failed:
        out.update(dashboard=None, dashboard_reason=f"the workflow database did not answer: {failed}")
    else:
        known = _scalars()
        out.update(dashboard=lists + [scalar(EXTENT_KEY, EXTENT_TEXT, held)]
                   + [scalar(key, text, held) for key, (text, _) in HOME_SETTINGS.items()] + [scalar(key, text, held) for key, text in REVIEW.items()]
                   + [scalar(key, known[key][0], held, known[key][1]) for key in DEEP], dashboard_reason="")
    return out


def request_row(payload) -> object:
    """`POST /api/settings/row {key, value, before}`: one setting row, `before` the value shown; an empty value unsets."""
    from . import home_map

    known = _scalars()
    key, value, before = payload.get("key"), payload.get("value"), payload.get("before")
    if (set(payload) != {"key", "value", "before"} or key not in known or not isinstance(value, str)
            or not (before is None or isinstance(before, str))):
        raise ValueError(f"Expected {{key, value, before}}, the key one of {', '.join(known)}.")
    value, pattern = value.strip() or None, known[key][1]
    if value is not None and pattern and not re.fullmatch(pattern, value):
        raise ValueError(f"{key} does not take {value!r}: {known[key][0]}")
    if value is not None and key == home_map.EXTENT_KEY and home_map.parse_extent(value)[1]:
        raise ValueError(f"{key} does not take {value!r}: {'; '.join(home_map.parse_extent(value)[1])}.")
    return lambda connection: _set_row(connection, key, value, before)


def _set_row(connection, key: str, value: str | None, before: str | None):
    from sd_db import writes

    try:
        was = writes.set_setting(connection, key, value, before=before)
    except writes.SettingStale as stale:
        return 409, {"error": str(stale), "reload": True}
    return {"key": key, "value": value, "was": was}


def request_list(payload) -> object:
    """`POST /api/settings/list {key, text, before}`: one dashboard list's row, or `ValueError` (400)."""
    from sd_db import writes

    from . import setting_rows

    if (set(payload) != {"key", "text", "before"} or payload["key"] not in setting_rows.LISTS
            or not isinstance(payload["text"], str) or not isinstance(payload["before"], str)):
        raise ValueError(f"Expected {{key, text, before}}, the key one of {', '.join(setting_rows.LISTS)}.")
    key, text = payload["key"], payload["text"].strip()
    legacy, parse = _list(key)
    problems = parse(text) if text else []
    if problems:
        raise ValueError(f"{key} was not saved: {'; '.join(problems)}.")
    if len(text) > writes.SETTING_VALUE_LENGTH:
        raise ValueError(f"{key} was not saved: it is over {writes.SETTING_VALUE_LENGTH} characters.")

    def write(connection):
        try:
            origin, shown = setting_rows.current(connection, key, legacy)
        except LookupError as unread:
            return 503, {"error": f"{key} not read: {unread}; nothing was saved"}
        except (OSError, UnicodeDecodeError) as error:
            # The page showed this file unreadable too; the save replaces it.
            origin, shown = "unreadable", f"the old file did not read: {error.__class__.__name__}"
        if setting_rows.digest(origin, shown) != payload["before"]:
            return 409, {"error": f"{key} changed since this page read it; nothing was saved. Read it again.", "reload": True}
        try:
            was = writes.set_setting(connection, key, text or None, before=shown if origin == "database" else None)
        except writes.SettingStale:
            return 409, {"error": f"{key} changed since this page read it; nothing was saved. Read it again.", "reload": True}
        return {"key": key, "value": text or None, "was": was, "digest": setting_rows.digest("database", text or None)}

    return write
