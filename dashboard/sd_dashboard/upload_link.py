"""Shared upload (sd:5405): the shared bucket's console link, the current upload link and Regenerate, on Management.

local-shared-upload (sd:5346) owns the bucket and the signer; this module loads its `shared_upload.py` by path
(`_tool`) and signs nothing itself.

- **Config.** `<config>/shared-upload/.env`, read with `system_tools_config` as `st_source_env` reads it: an exported
  value wins, an empty one too; a `change-me` value is unset, as Home reads ha-mcp's. With the bucket, region or
  profile unset the section says "not set up" and names the file; a file that does not read is the source's reason.
- **The link.** One presigned POST under `PREFIX`, for `LIFETIME` (168 h, the Signature Version 4 limit), each file up
  to `SHARED_UPLOAD_MAX_MB` (500). It is the workflow database setting row `KEY`: Regenerate writes a new row and the
  latest is the current link. A link already handed out keeps working until its expiry; nothing here revokes one.
- **Its subject.** The row names the bucket, region and prefix it was signed for; a row signed for another one is not
  the current link (`other`), and a row that does not read is unknown (`unread`), never "none signed".
- **The secret.** A link grants uploads. It goes into the setting row and the page's document only: no error, toast or
  log line holds it, and `regenerate` answers the expiry, not the link.
"""

from __future__ import annotations

import datetime
import functools
import importlib.util
import json
import sqlite3
from urllib.parse import quote

from . import fix_session

__all__ = ["KEY", "LIFETIME", "PREFIX", "document", "regenerate"]

KEY = "dashboard.shared_upload_link"
PREFIX = "inbox/"
LIFETIME = "168h"
TOOL = "shared-upload"
NEEDED = ("SHARED_UPLOAD_BUCKET", "SHARED_UPLOAD_REGION", "SHARED_UPLOAD_PROFILE")


@functools.cache
def _tool():
    """`local-shared-upload/shared_upload.py`, loaded by path as the satellites' module is (`management_screen`)."""
    spec = importlib.util.spec_from_file_location("sd_dashboard_shared_upload",
                                                  fix_session.CHECKOUT / "local-shared-upload" / "shared_upload.py")
    if spec is None or spec.loader is None:
        raise ValueError("local-shared-upload/shared_upload.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config():
    from sd_db import config as system_tools_config
    return system_tools_config


def settings(environ=None) -> dict:
    """The tool's values and the file they come from; `missing` names the needed ones unset.

    Raises `OSError` or `UnicodeDecodeError` for a file that does not read: unknown, not "not set up".
    """
    stc = _config()
    saved = stc.read_env(TOOL, environ)
    value = {name: stc.setting(name, saved, environ) or "" for name in NEEDED + ("SHARED_UPLOAD_MAX_MB",)}
    value = {name: "" if "change-me" in found else found for name, found in value.items()}
    missing = [name for name in NEEDED if not value[name]]
    path = stc.config_dir(TOOL, environ) / ".env"
    remedy = (f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set. Export "
              f"{'it' if len(missing) == 1 else 'them'}, or copy local-shared-upload/.env.example to {path} and fill it in."
              if missing else "")
    return {"config": str(path), "missing": missing, "remedy": remedy,
            "bucket": value["SHARED_UPLOAD_BUCKET"], "region": value["SHARED_UPLOAD_REGION"],
            "profile": value["SHARED_UPLOAD_PROFILE"], "max_mb": value["SHARED_UPLOAD_MAX_MB"] or "500"}


def console(bucket: str, region: str) -> str:
    """The bucket's page in the S3 console."""
    return f"https://s3.console.aws.amazon.com/s3/buckets/{quote(bucket, safe='')}?region={quote(region, safe='')}&tab=objects"


def _moment(text) -> datetime.datetime:
    moment = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("a time without a zone")
    return moment


def _stored(connection) -> tuple[dict | None, str]:
    """`(body, problem)`: the latest row's body, None when none was ever signed; a problem means the row is unknown."""
    from sd_db import writes
    from sd_db.errors import SdDbError

    try:
        held = writes.read_settings(connection, [KEY])
    except (SdDbError, sqlite3.Error) as error:
        return None, f"the {KEY} row was not read: {error.__class__.__name__}"
    if held.get(KEY) is None:
        return None, ""
    try:
        body = json.loads(held[KEY])
        if not (isinstance(body, dict) and all(isinstance(body.get(name), str) for name in
                                                ("bucket", "region", "prefix", "url", "expiry", "signed"))
                and isinstance(body.get("fields"), dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in body["fields"].items())):
            raise ValueError
        _moment(body["expiry"])
    except (ValueError, TypeError):
        return None, f"the {KEY} row does not read; Regenerate replaces it"
    return body, ""


def document(connection, *, now: str, environ=None) -> dict:
    """The section's reading: the config file, the console link and the current link with its state.

    `link_state` is `current`, `expired`, `none` (never signed), `other` (signed for another bucket, region or
    prefix) or `unread` (with `link_reason`). `link` is set for `current` and `expired`, with its ready curl line.
    """
    found = settings(environ)
    out = {"config": found["config"], "set_up": not found["missing"], "remedy": found["remedy"], "prefix": PREFIX,
           "lifetime": LIFETIME, "link": None, "link_state": "none", "link_reason": ""}
    if found["missing"]:
        return out
    tool = _tool()
    bucket, region = found["bucket"], found["region"]
    out.update(bucket=bucket, region=region, console=console(bucket, region),
               public=tool.public_url(bucket, region, PREFIX) + "<file name>")
    body, problem = _stored(connection)
    if problem:
        out.update(link_state="unread", link_reason=problem)
    elif body is None:
        pass
    elif (body["bucket"], body["region"], body["prefix"]) != (bucket, region, PREFIX):
        out.update(link_state="other", link_reason=f"the stored link was signed for {body['bucket']} in {body['region']} "
                   f"under {body['prefix']}, not this bucket; Regenerate signs one for it")
    else:
        expired = _moment(body["expiry"]) <= _moment(now)
        out["link_state"] = "expired" if expired else "current"
        out["link"] = {"url": body["url"], "fields": body["fields"], "expiry": body["expiry"], "signed": body["signed"],
                       "temporary": body.get("temporary"), "curl": tool.curl_line(body["url"], body["fields"]),
                       "public": tool.public_url(body["bucket"], body["region"], body["prefix"]) + "<file name>"}
    return out


def regenerate(connection, *, environ=None, now: datetime.datetime | None = None, credentials=None):
    """Sign a new link with the tool's signer and store it as the `KEY` row; `(status, answer)`.

    A config that is not set up or a bad size cap is a `ValueError` (400). Keys the profile cannot give are a 502
    naming why, and nothing is written. The answer holds the expiry, never the link. `credentials` is the seam a test
    fills; the tool's `credentials` runs `aws configure export-credentials` (30 s).
    """
    found = settings(environ)
    if found["missing"]:
        raise ValueError(f"Shared upload is not set up: {found['remedy']}")
    tool = _tool()
    try:
        max_bytes = tool.megabytes(found["max_mb"]) * 1024 * 1024
    except ValueError as error:
        raise ValueError(f"SHARED_UPLOAD_MAX_MB in {found['config']}: {error}") from None
    try:
        keys = (credentials or tool.credentials)(found["profile"])
    except RuntimeError as error:
        return 502, {"error": f"No link was signed: {error}"}
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    url, fields, expiry = tool.presigned_post(found["bucket"], found["region"], PREFIX, keys, tool.seconds(LIFETIME),
                                              max_bytes, moment)
    temporary = (keys.get("Expiration") or "unknown") if keys.get("SessionToken") else None
    body = {"bucket": found["bucket"], "region": found["region"], "prefix": PREFIX, "url": url, "fields": fields,
            "expiry": expiry, "signed": moment.isoformat(timespec="seconds"), "temporary": temporary}
    from sd_db import writes

    # Unsorted: the fields keep the order the form sends them in, `key` first, as the tool prints them.
    writes.set_setting(connection, KEY, json.dumps(body))
    return 200, {"prefix": PREFIX, "expiry": expiry, "temporary": temporary}
