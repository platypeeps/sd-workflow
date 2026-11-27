"""The satellite stage's half in the library (step 9).

Step 9 of `docs/work/2026-09-22-run-the-framework-from-a-second-machine/`.
`local-machine-setup/machine-setup.sh` runs it with the pack's interpreter,
`-I`, so the installed copy answers and not a checkout:

    python -I -m sd_db.satellite --hub HOST [--port N] [--address IP] [--apply]

It prints the stage's lines and exits 0 when it could report. The lines
carry the drift words the stage's counter greps for:

* `EXTRA` for a local database. A machine is a hub or a satellite, not
  both, so nothing else is written while one is there.
* `MISSING` or `DIFFERS` for `~/.config/sd/hub.json`; `--apply` writes it,
  with `--address`, the hub's tailnet IP, beside the name (sd:10979).
  Without `--apply` the hub is asked nothing until the file names it, so
  a missing file is one line of drift, not three.
* `DIFFERS` for a build the hub refuses, with both sides' values and the
  `repo-sync.sh` verb that moves the pack. Nothing is installed: the pack's
  `lib/` is the one copy, and `follow` moves it to the hub's (sd:3280).
* `MISSING` or `DIFFERS` for `providers.yaml`; `--apply` installs the
  hub's bytes, which the protocol serves (seam 7).

A hub that does not answer is `SKIP`, not drift: a laptop away from the
tailnet has not drifted, and the next run checks again.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from . import database, hub, registry, remote
from .errors import RegistryError


def _line(out, word: str, text: str) -> None:
    # The stage's own layout: two spaces, the word padded to eight.
    out.write(f"  {word:<7} {text}\n")


def _plan(out, apply: bool, text: str) -> None:
    out.write(f"  {'' if apply else '[dry-run] '}{text}\n")


def _write(path: Path, data: bytes) -> None:
    """Replace `path` whole, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(staged, 0o644)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def _named(config: Path) -> tuple | None:
    """The hub, port and address `hub.json` names, or `None` when it names none readably."""
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("hub"), data.get("port", hub.DEFAULT_PORT), data.get("address")


def _shown(host, port, address) -> str:
    return f"{host}:{port}" + (f" at {address}" if address is not None else "")


def run(host: str, port: int, *, apply: bool = False, home: Path | str | None = None,
        out=sys.stdout, address: str | None = None) -> int:
    """The stage; 2 for an `address` that is not an IP, with nothing written."""
    if address is not None and not hub.ip_literal(address):
        out.write(f"  ERROR   --address {address!r} is not an IP address; nothing written\n")
        return 2
    config = hub.config_path(home)
    local = database.local_path(home)
    if local.exists():
        _line(out, "EXTRA", f"database {local} — a satellite holds no database; move it away, "
                            f"then rerun. {config} was not written")
        return 0
    wanted = (host, port, address)
    named = _named(config) if config.exists() else None
    if not config.exists():
        _line(out, "MISSING", f"{config} — this machine names no sd hub")
    elif named != wanted:
        shown = "nothing readable" if named is None else _shown(*named)
        _line(out, "DIFFERS", f"{config} names {shown}, not {_shown(*wanted)}")
    else:
        _line(out, "ok", f"{config} names {_shown(*wanted)}")
    if named != wanted:
        _plan(out, apply, f"write {config}")
        if not apply:
            # One gap, one line: the hub is asked once this machine names it.
            _line(out, "SKIP", "the build and providers.yaml are checked once hub.json names the hub")
            return 0
        written = {"hub": host, "port": port} | ({"address": address} if address is not None else {})
        _write(config, (json.dumps(written) + "\n").encode("utf-8"))
    # The loopback token, only from a file that names this same hub. On the
    # tailnet the peer is the credential and no token is sent (step 7).
    try:
        current = hub.read(home) if named == wanted else None
        token = current.token if current is not None and current.token_file is not None else ""
        session = remote.connect(host, port, None, token=token, write=False,
                                 timeout=hub.CONNECT_TIMEOUT, answer_timeout=hub.ANSWER_TIMEOUT,
                                 address=address)
    except remote.BuildMismatch as mismatch:
        _line(out, "DIFFERS", f"{remote.DESCRIBED[mismatch.field]}: satellite {mismatch.satellite}, "
                              f"hub {mismatch.hub} — {_remedy(mismatch)}")
        return 0
    except remote.HubUnreachable as error:
        _line(out, "SKIP", f"sd hub {host}:{port} did not answer ({error.reason}); "
                           f"the build and providers.yaml were not checked")
        return 0
    except remote.RemoteError as error:
        _line(out, "DIFFERS", f"sd hub {host}:{port} refused this machine: {error}")
        return 0
    try:
        built = remote.handshake()
        _line(out, "ok", f"sd_db {built['package']} build {built['build']} matches the hub's")
        try:
            served = session.registry_bytes()
        except RegistryError as error:
            _line(out, "DIFFERS", f"providers.yaml: {error}")
            return 0
    finally:
        session.close()
    target = registry.registry_path(home)
    try:
        registry.parse(served.decode("utf-8"), f"{host}:{target.name}")
    except (RegistryError, UnicodeDecodeError) as error:
        _line(out, "DIFFERS", f"providers.yaml: the hub's file does not read as a registry: {error}; "
                              f"nothing installed")
        return 0
    if not target.exists():
        _line(out, "MISSING", f"{target} — the hub's providers.yaml is not here")
    elif target.read_bytes() != served:
        _line(out, "DIFFERS", f"{target} differs from the hub's providers.yaml")
    else:
        _line(out, "ok", f"{target} matches the hub's")
        return 0
    _plan(out, apply, f"install the hub's providers.yaml at {target}")
    if apply:
        _write(target, served)
    return 0


def _remedy(mismatch: remote.BuildMismatch) -> str:
    if mismatch.upgrade == "hub":
        return "run `repo-sync.sh refresh` on the hub, then rerun"
    if mismatch.upgrade == "satellite":
        return "run `repo-sync.sh follow` here, then rerun"
    return "run `repo-sync.sh refresh` on the hub, then `repo-sync.sh follow` here, then rerun"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sd_db.satellite")
    parser.add_argument("--hub", required=True, help="the hub's host name on the tailnet")
    parser.add_argument("--port", type=int, default=hub.DEFAULT_PORT)
    parser.add_argument("--address", help="the hub's tailnet IP, which the dial uses in place of the name")
    parser.add_argument("--apply", action="store_true", help="write hub.json and providers.yaml")
    arguments = parser.parse_args(argv)
    return run(arguments.hub, arguments.port, apply=arguments.apply, address=arguments.address)


if __name__ == "__main__":
    raise SystemExit(main())
