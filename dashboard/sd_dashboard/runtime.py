"""Explicit, reversible installation of the database dashboard and private Serve route."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from sd_db.config import system_checkout

from . import PACK_MODULES, pack_bin, pack_library

#: The launchd label prefix is shared by every system service, so a
#: second install under another prefix keeps its own labels. The default is
#: spelled out here, not read from `sd_db.config`: `dashboard.sh` exports the
#: prefix it read, and an installed copy has no shell to read it
#: (tests/test_installed.py).
LABEL_PREFIX = os.environ.get("SYSTEM_TOOLS_LABEL_PREFIX", "local.system-tools")
LABEL = f"{LABEL_PREFIX}.sd-dashboard"
LAUNCH_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
HERE = Path(__file__).resolve().parents[1]
#: `/bin/bash -c` runs `dashboard.sh serve` and stays its parent: TCC charges
#: disk access to Apple's signed bash, not to a Python whose ad-hoc grant a
#: brew upgrade drops (sd:6444). The trailing `exit` stops bash from exec'ing
#: its last command; launchd stops the whole process group.
LAUNCH_SCRIPT = '"$0" "$@"; exit $?'
#: Apple's bash passes its grant only to Apple programs, so Homebrew Python
#: under it is still charged to itself (system sd:6466). Where the system
#: tools built their launcher, it starts `dashboard.sh` instead: it forks,
#: waits and stays the parent, and its one grant survives every upgrade.
JOB_LAUNCH = ".local/libexec/system-tools/job-launch"


class RuntimeRefused(Exception):
    pass


class NoDashboard(RuntimeRefused):
    """No dashboard.json: this machine runs no dashboard (sd:9920); `main` exits 3."""


def jev_command():
    """The system checkout's `local-jev/jev.sh`, else `jev` on PATH, else None.

    The checkout's copy first: launchd's PATH (`LAUNCH_PATH`) holds no
    `local-bin-links` folder. None means the failed-job shadow asks nothing.
    """
    script = system_checkout() / "local-jev" / "jev.sh"
    if script.is_file():
        return ["sh", str(script)]
    found = shutil.which("jev")
    return [found] if found else None


def _run(arguments, *, check=True, timeout=20):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeRefused(f"cannot run {arguments[0]}: {error}") from error
    if check and result.returncode:
        raise RuntimeRefused(f"{arguments[0]} failed: {(result.stderr or result.stdout).strip()[:1000]}")
    return result


def _serve_status():
    try:
        value = json.loads(_run(["tailscale", "serve", "status", "--json"]).stdout)
    except ValueError as error:
        raise RuntimeRefused("tailscale serve status did not return JSON") from error
    if not isinstance(value, dict):
        raise RuntimeRefused("tailscale serve status is not an object")
    return value


def read_config(path):
    path = Path(path).expanduser().resolve()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise NoDashboard(f"no dashboard on this machine: {path} does not exist") from error
    except (OSError, ValueError) as error:
        raise RuntimeRefused(f"cannot read dashboard config {path}: {error}") from error
    if not isinstance(config, dict):
        raise RuntimeRefused("dashboard configuration must be a JSON object")
    unknown = set(config) - {"origin", "operator_login", "ip_origin", "port", "database"}
    if unknown:
        raise RuntimeRefused("unknown dashboard configuration: " + ", ".join(sorted(unknown)))
    port = config.get("port", 8767)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise RuntimeRefused("dashboard port must be an integer from 1024 to 65535")
    config["port"] = port
    from .auth import parse_direct_origin
    parse_direct_origin(config)
    database = config.get("database")
    if database is not None and (not isinstance(database, str) or not Path(database).expanduser().is_absolute()):
        raise RuntimeRefused("configured database must be an absolute path")
    return config


def _check_node(config):
    from .auth import parse_direct_origin

    try:
        if not isinstance(config.get("origin"), str) or not isinstance(config.get("operator_login"), str):
            raise ValueError("origin and operator_login must be strings")
        requested_host = urlsplit(config["origin"]).hostname
        state = json.loads(_run(["tailscale", "status", "--json"]).stdout)
        own = state["Self"]
        hostname = own["DNSName"].rstrip(".")
        user = state["User"][str(own["UserID"])]
        if not isinstance(user, dict):
            raise ValueError("Tailscale owner is unavailable")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise RuntimeRefused("cannot identify this Tailscale node and its operator") from error
    if state.get("BackendState") != "Running":
        raise RuntimeRefused("Tailscale is not running")
    if requested_host != hostname:
        raise RuntimeRefused("configured dashboard hostname is not this Tailscale node")
    if config.get("operator_login") != user.get("LoginName"):
        raise RuntimeRefused("configured operator_login is not this node's Tailscale owner")
    direct = parse_direct_origin(config)
    if direct and direct.address not in own.get("TailscaleIPs", []):
        raise RuntimeRefused("configured dashboard IP is not this Tailscale node")


def _check_direct_serve(config, status):
    """A direct listener must not overlap a Serve or Funnel listener."""
    from .auth import parse_direct_origin

    direct = parse_direct_origin(config)
    if direct is None:
        return
    for key in ("TCP", "Web", "AllowFunnel"):
        section = status.get(key, {})
        if not isinstance(section, dict):
            raise RuntimeRefused("invalid Tailscale Serve configuration")
        if ((key == "TCP" and str(direct.port) in section) or
                (key != "TCP" and any(str(name).endswith(f":{direct.port}") for name in section))):
            raise RuntimeRefused("direct dashboard IP port is already configured in Tailscale Serve")


def peer_login(peer):
    """Identify the TCP peer through the local Tailscale daemon, never headers.

    The rules are `sd_db.tailnet`'s, shared with `sd-db serve`: a tagged,
    expired or mismatched node has no login."""
    from sd_db import tailnet

    try:
        return tailnet.login(peer, lambda arguments: _run(arguments).stdout)
    except tailnet.TailnetError as error:
        raise RuntimeRefused("cannot identify the dashboard's Tailscale peer") from error


def load_frontdoor(config_path: Path, port: int):
    from .auth import validate_frontdoor

    config = read_config(config_path)
    if config["port"] != port:
        raise RuntimeRefused("configured backend port differs from --port; use the configured port explicitly")
    if not config.get("origin") and not config.get("operator_login"):
        return None
    _check_node(config)
    status = _serve_status()
    _check_direct_serve(config, status)
    return validate_frontdoor(config, status, port)


def _file_digest(path):
    if path.is_symlink() or not path.is_file():
        raise RuntimeRefused(f"runtime build requires a regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_manifest(root):
    """Enumerate package data as well as code; generated bytecode is excluded."""
    if root.is_symlink() or not root.is_dir():
        raise RuntimeRefused(f"runtime build directory is unavailable: {root}")
    entries = []
    def unreadable(error):
        raise error
    for directory, names, files in os.walk(root, onerror=unreadable, followlinks=False):
        names[:] = sorted(name for name in names if name != "__pycache__")
        for name in names:
            if (Path(directory) / name).is_symlink():
                raise RuntimeRefused(f"runtime build contains a linked directory: {Path(directory) / name}")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(directory) / name
            entries.append((path.relative_to(root).as_posix(), _file_digest(path)))
    if not entries:
        raise RuntimeRefused(f"runtime build directory is empty: {root}")
    return sorted(entries)


def build_digests(*, library_root=None, dashboard_root=None, system_root=None):
    """Hash installed package bytes and dashboard code with fixed collectors.

    Optional roots are Python-only test seams, never CLI or HTTP parameters.
    Names are part of each digest, so file additions and removals count too.
    """
    import sd_db

    library_root = Path(library_root) if library_root is not None else Path(sd_db.__file__).resolve().parent
    dashboard_root = Path(dashboard_root) if dashboard_root is not None else HERE
    system_root = Path(system_root) if system_root is not None else system_checkout()
    try:
        library = _build_manifest(library_root)
        # The pack `bin/` modules the Queue imports in-process (sd:TBD). An installed copy has no `bin/`: absent is
        # recorded as None, as for the launcher below.
        for name in PACK_MODULES:
            path = pack_bin(library_root) / f"{name}.py"
            library.append((f"bin/{name}.py", _file_digest(path) if path.exists() or path.is_symlink() else None))
        dashboard = [("sd_dashboard/" + name, digest)
                     for name, digest in _build_manifest(dashboard_root / "sd_dashboard")]
        # Ports uses these fixed repository dependencies outside the package;
        # Notes loads local-mail-intake's workspace-mcp client (sd:3425).
        # Their absence is valid in isolated fixtures, but adding or removing
        # either must still invalidate the previously captured build identity.
        #
        # `dashboard.sh` joined them when this package became installable. It
        # sits beside the package in a checkout and nowhere at all in
        # site-packages, so requiring it meant `server.build` raised
        # RuntimeRefused from an installed copy -- the dashboard could be
        # installed but not started. Absent is now recorded as None, which
        # still distinguishes a launcher that was removed from one that never
        # shipped.
        # The two system tools are the system checkout's since the dashboard moved into the pack (sd:5987).
        for root, name in ((dashboard_root, "dashboard.sh"), (dashboard_root, "collectors.py"), (dashboard_root, "sd_tile.py"),
                           (system_root, "local-machine-setup/machine-setup.sh"),
                           (system_root, "local-mail-intake/mail_intake.py")):
            path = root / name
            dashboard.append((name, _file_digest(path) if path.exists() or path.is_symlink() else None))
    except OSError as error:
        raise RuntimeRefused(f"cannot fingerprint runtime build: {error}") from error
    return {name: hashlib.sha256(json.dumps(sorted(entries), separators=(",", ":")).encode()).hexdigest()
            for name, entries in (("library_digest", library), ("dashboard_digest", dashboard))}


def _pack_commit(pack):
    """The pack checkout's HEAD, or None when git is absent or the pack is not a repository."""
    if shutil.which("git") is None:
        return None
    result = _run(["git", "-C", str(pack), "rev-parse", "HEAD"], check=False)
    if result.returncode:
        if "not a git repository" in result.stderr:
            return None
        raise RuntimeRefused(f"git failed: {(result.stderr or result.stdout).strip()[:1000]}")
    return result.stdout.strip() or None


def checkout_commit():
    """This dashboard checkout's HEAD; None for an installed copy outside any checkout (sd:3528)."""
    return _pack_commit(HERE)


def served_behind(served):
    """None when a process that started on commit `served` serves this checkout's HEAD; else why it does not.

    `dashboard.sh health` and the Health page's dashboard row both ask this (sd:3528, sd:3707). A HEAD that git
    cannot read raises RuntimeRefused: an unknown checkout is not a match.
    """
    checkout = checkout_commit()
    if served == checkout:
        return None
    return (f"served {served or 'unknown'}, checkout {checkout or 'unknown'}: "
            f"launchctl kickstart -k gui/{os.getuid()}/{LABEL}")


def installed_library(database):
    """Prove the runtime uses the pack checkout's `lib/sd_db` and can read this database.

    The pack owns `sd_db` and step 3 of its move uninstalls the venv copy
    (sd:3280), so any other copy is refused here, a pack-built one in
    site-packages included: a service on another copy than the pack's made
    the hub refuse satellite writes for two hours (sd:3279). With no copy,
    nothing can lag the checkout, so the commit check of sd:962 has gone.
    """
    import sd_db
    import sd_db.writing  # every production dashboard must support the writing stage
    from sd_db.schema import SCHEMA_VERSION

    library = Path(sd_db.__file__).resolve()
    expected = pack_library()
    if library.parent != expected / "sd_db":
        raise RuntimeRefused(f"sd_db must be the command pack's lib/ at {expected}, not {library}; "
                             "start the dashboard through dashboard.sh, from the pack checkout it sits in")
    try:
        connection = sd_db.connect(database, write=False)
        try:
            version = sd_db.database.schema_version(connection)
        finally:
            connection.close()
    except (OSError, sd_db.SdDbError) as error:
        raise RuntimeRefused(str(error)) from error
    if version != SCHEMA_VERSION:
        raise RuntimeRefused(f"database schema {version} differs from the pack's schema {SCHEMA_VERSION}; migrate explicitly with services stopped")
    library_commit = _pack_commit(expected.parent)
    return {"python": sys.executable, "library": str(library), "schema": version, "library_commit": library_commit, **build_digests()}


def _configured_route(config, status):
    """Return whether this exact route exists; never overwrite a different service."""
    from .auth import validate_frontdoor

    _check_direct_serve(config, status)

    origin = config.get("origin")
    if not isinstance(origin, str):
        raise RuntimeRefused("installation requires an explicit private HTTPS origin and operator_login")
    try:
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or parsed.port != 8443 or not parsed.hostname or not parsed.hostname.endswith(".ts.net"):
            raise RuntimeRefused("installation uses a private *.ts.net HTTPS origin on :8443 only")
    except ValueError as error:
        raise RuntimeRefused("invalid dashboard origin") from error
    authority = parsed.netloc
    if (status.get("AllowFunnel") or {}).get(authority):
        raise RuntimeRefused("the dashboard origin is public through Funnel; refusing installation")
    present = authority in (status.get("Web") or {}) or "8443" in (status.get("TCP") or {})
    expected = json.loads(json.dumps(status))
    if not present:
        expected.setdefault("TCP", {})["8443"] = {"HTTPS": True}
        expected.setdefault("Web", {})[authority] = {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{config['port']}"}}}
    validate_frontdoor(config, expected, config["port"])
    return present


def _without_dashboard(status, authority):
    result = json.loads(json.dumps(status))
    for key, name in (("TCP", "8443"), ("Web", authority), ("AllowFunnel", authority)):
        mapping = result.get(key)
        if isinstance(mapping, dict):
            mapping.pop(name, None)
            if not mapping:
                result.pop(key, None)
    return result


def _dashboard_scope(status, authority):
    return {key: (status.get(key) or {}).get(name)
            for key, name in (("TCP", "8443"), ("Web", authority), ("AllowFunnel", authority))}


def _rollback_route(config, before, owned_route):
    """Reconcile an attempted creation, including an unknown CLI outcome."""
    authority = urlsplit(config["origin"]).netloc
    current = _serve_status()
    scope = _dashboard_scope(current, authority)
    original = _dashboard_scope(before, authority)
    if scope == original:
        return
    expected_web = {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{config['port']}"}}}
    if (scope["TCP"] != {"HTTPS": True} or scope["Web"] != expected_web or
            scope["AllowFunnel"] not in (None, False) or
            (owned_route is not None and scope != owned_route)):
        raise RuntimeRefused("dashboard Serve route changed concurrently; preserving it for manual recovery")
    untouched = _without_dashboard(current, authority)
    command_error = None
    try:
        # Serve requires the original flags when disabling a background mount.
        # Naming / also avoids a port-wide removal of another mounted path.
        _run(["tailscale", "serve", "--bg", "--https=8443", "--set-path=/", "off"])
    except RuntimeRefused as error:
        command_error = error
    after = _serve_status()
    if _dashboard_scope(after, authority) != original:
        raise RuntimeRefused("could not verify removal of the attempted dashboard route" +
                             (f": {command_error}" if command_error else ""))
    if _without_dashboard(after, authority) != untouched:
        raise RuntimeRefused("unrelated Serve configuration changed during rollback; review the saved snapshot")


def _plist(config_path, config, home):
    launcher = home / JOB_LAUNCH
    if os.access(launcher, os.X_OK):
        arguments = [str(launcher), str(HERE / "dashboard.sh")]
    else:
        arguments = ["/bin/bash", "-c", LAUNCH_SCRIPT, str(HERE / "dashboard.sh")]
    arguments += ["serve", "--config", str(config_path), "--port", str(config["port"])]
    if config.get("database"):
        arguments += ["--database", str(Path(config["database"]).expanduser().resolve())]
    # Standard, not Background: this is an interactive HTTP server. Background
    # confines it to efficiency cores and low-priority I/O, which made a Today
    # render 4-5x slower (sd:1433). The runner's plist stays Background.
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": arguments, "RunAtLoad": True,
        "KeepAlive": True, "ThrottleInterval": 30, "ProcessType": "Standard",
        "StandardOutPath": str(home / "Library/Logs" / f"{LABEL}.log"),
        "StandardErrorPath": str(home / "Library/Logs" / f"{LABEL}.err"),
        "EnvironmentVariables": _launch_environment(),
    }, sort_keys=False)


#: Carried into the LaunchAgent when the installing shell sets them, so the
#: server reads the same vault, checkout root, labels, config directory and
#: extra job directories as `dashboard.sh`. `OBSIDIAN_VAULT` is the vault
#: `sd store` writes Notes' quick notes into (sd:2549). `JEV_JOB_TRIAGE`
#: switches the failed-job Jev shadow off (`job_failures`, sd:2095).
#: `SYSTEM_TOOLS_ROOT` names the system checkout whose tools it runs (sd:5987).
PASSED_THROUGH = ("REPO_ROOT", "SYSTEM_TOOLS_LABEL_PREFIX", "SYSTEM_TOOLS_CONFIG",
                  "CRON_JOBS_EXTRA_DIRS", "OBSIDIAN_VAULT", "JEV_JOB_TRIAGE", "SYSTEM_TOOLS_ROOT")


def _launch_environment():
    environment = {"PATH": LAUNCH_PATH, "SD_DASHBOARD_PYTHON": sys.executable}
    # Notes runs `sd store` (sd:2549): the installing shell's `sd` directory joins the agent's PATH.
    sd = shutil.which("sd")
    if sd and str(Path(sd).parent) not in LAUNCH_PATH.split(":"):
        environment["PATH"] = f"{LAUNCH_PATH}:{Path(sd).parent}"
    for name in PASSED_THROUGH:
        if os.environ.get(name):
            environment[name] = os.environ[name]
    return environment


def launch_gaps(environment):
    """What Notes' quick notes need from the agent's environment and the installing shell did not give."""
    return ([] if shutil.which("sd", path=environment["PATH"]) else ["sd is not on the installing shell's PATH"]) + (
        [] if environment.get("OBSIDIAN_VAULT") else ["OBSIDIAN_VAULT is not set"])


def installed_gap(home=None):
    """Why the installed LaunchAgent's PATH no longer finds `sd`, else "" (sd:5288): install froze sd's folder into it,
    and a later move of sd left Notes and Settings without it while health stayed green. No plist is no agent to
    check; a plist that does not read is a gap, since its PATH is unknown."""
    home = Path(home) if home is not None else Path.home()
    target = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    reinstall = "run `dashboard.sh install`, then `dashboard.sh install --apply --expected-fingerprint <its fingerprint>`"
    if not target.exists():
        return ""
    try:
        path = plistlib.loads(target.read_bytes()).get("EnvironmentVariables", {}).get("PATH", "")
    except (OSError, ValueError, AttributeError) as error:
        return f"the installed LaunchAgent {target} does not read ({error}); {reinstall}"
    if shutil.which("sd", path=path):
        return ""
    return f"the installed LaunchAgent's PATH no longer finds sd ({target}, PATH={path}); {reinstall}"


def preflight(config_path, *, home=None):
    home = Path(home) if home is not None else Path.home()
    config_path = Path(config_path).expanduser().resolve()
    config = read_config(config_path)
    database = Path(config.get("database") or home / ".local/share/sd/sd.db").expanduser().resolve()
    library = installed_library(database)
    remote = bool(config.get("origin") or config.get("operator_login"))
    if remote:
        _check_node(config)
    status = _serve_status() if remote else None
    present = _configured_route(config, status) if remote else False
    target = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    old = target.read_bytes() if target.exists() else None
    domain = f"gui/{os.getuid()}"
    loaded = _run(["launchctl", "print", f"{domain}/{LABEL}"], check=False).returncode == 0
    if loaded and old is None:
        raise RuntimeRefused("dashboard is loaded without its plist; restore its original plist before replacement")
    report = {"config_path": str(config_path), "config": config, "runtime": library,
              "database": str(database), "remote": remote, "serve_status": status, "serve_route_present": present,
              "plist": str(target), "old_plist_sha256": hashlib.sha256(old).hexdigest() if old else None,
              "loaded": loaded, "program": plistlib.loads(_plist(config_path, config, home))["ProgramArguments"]}
    report["fingerprint"] = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    report["planned_actions"] = (["add private HTTPS :8443 proxy"] if remote and not present else []) + [
        "back up existing LaunchAgent and Serve snapshot", "replace dashboard LaunchAgent",
        "verify backend health and unchanged unrelated Serve configuration"]
    if config.get("ip_origin"):
        report["planned_actions"].insert(0, "bind authenticated dashboard IP listener at " + config["ip_origin"])
    return report


def health_check(port, *, wait=30.0):
    """Poll /health until it answers healthy or `wait` seconds pass; 0 tries once.

    A clock bounds it, not an attempt count: a refused connection returns at
    once, so 20 attempts gave a slow start about 5 seconds (sd:2811).
    """
    deadline = time.monotonic() + wait
    last = None
    while True:
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            client.request("GET", "/health")
            response = client.getresponse()
            body = json.loads(response.read())
            if response.status == 200 and body.get("service") == "sd-dashboard" and body.get("ok") is True:
                return body
            last = body if isinstance(body, dict) and body.get("service") == "sd-dashboard" else last
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            client.close()
        if time.monotonic() >= deadline:
            # A stale process answers 503; say what it serves (sd:3528).
            serves = "" if last is None else (f"; it serves {last.get('commit') or 'an unknown commit'}"
                                              + (" and its build changed on disk" if last.get("code_changed") else ""))
            raise RuntimeRefused(f"new dashboard did not return its healthy /health response within {wait:g}s{serves}")
        time.sleep(0.25)


def _verify_loaded_process(domain, health):
    """A healthy older listener cannot stand in for the process just loaded.

    launchd's pid is the launcher (job-launch or `/bin/bash`); the server is its child, so its
    parent pid must be the one launchd names (sd:6444)."""
    service = f"{domain}/{LABEL}"
    result = _run(["launchctl", "print", service], check=False)
    lines = result.stdout.splitlines()
    pids = [int(match[1]) for line in lines
            if (match := re.fullmatch(r"\tpid = ([1-9][0-9]*)", line))]
    if (result.returncode or not lines or lines[0] != service + " = {" or
            lines[-1] != "}" or len(pids) != 1 or type(health.get("ppid")) is not int or
            pids[0] != health["ppid"]):
        raise RuntimeRefused("health response is not from the newly loaded dashboard process")


def _replace(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _plist_bytes(target):
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise RuntimeRefused("dashboard plist is no longer a regular file; preserving it")
    return target.read_bytes() if target.exists() else None


def _owned_service(target, expected, domain, *, timeout=20):
    """Preserve a newer operator's plist or loaded command during rollback."""
    if _plist_bytes(target) != expected:
        raise RuntimeRefused("dashboard plist changed concurrently; preserving the newer service")
    service = f"{domain}/{LABEL}"
    result = _run(["launchctl", "print", service], check=False, timeout=timeout)
    if result.returncode == 113:
        return False
    lines = result.stdout.splitlines()
    arguments = plistlib.loads(expected).get("ProgramArguments", []) if expected else []
    block = "\targuments = {\n" + "\n".join("\t\t" + argument for argument in arguments) + "\n\t}"
    if (result.returncode or not arguments or not lines or lines[0] != service + " = {" or
            lines[-1] != "}" or lines.count("\tpath = " + str(target)) != 1 or
            lines.count("\tprogram = " + arguments[0]) != 1 or
            lines.count("\targuments = {") != 1 or block not in result.stdout):
        raise RuntimeRefused("loaded dashboard identity changed or is unknown; preserving it for manual recovery")
    return True


def _wait_unloaded(target, expected, domain, *, timeout=10):
    """bootout can return before launchd has removed the service registration."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeRefused("dashboard service did not unload before the shutdown deadline")
        if not _owned_service(target, expected, domain, timeout=min(2, remaining)):
            # Preserve a plist edit made while launchctl was answering.
            if _plist_bytes(target) != expected:
                raise RuntimeRefused("dashboard plist changed concurrently; preserving the newer service")
            return
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def install(config_path, *, expected_fingerprint, home=None):
    home = Path(home) if home is not None else Path.home()
    report = preflight(config_path, home=home)
    if report["fingerprint"] != expected_fingerprint:
        raise RuntimeRefused("runtime, config, service or Serve state changed; review a new preflight")
    config = report["config"]
    authority = urlsplit(config.get("origin", "")).netloc if report["remote"] else None
    target = Path(report["plist"])
    old = _plist_bytes(target)
    replacement = _plist(Path(report["config_path"]), config, home)
    for gap in launch_gaps(plistlib.loads(replacement)["EnvironmentVariables"]):
        print(f"dashboard: {gap}; Notes cannot list or keep quick notes", file=sys.stderr)
    backup_root = home / ".local/share/sd/runtime-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="dashboard-", dir=backup_root))
    if old is not None:
        (backup / target.name).write_bytes(old)
    (backup / "preflight.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    domain = f"gui/{os.getuid()}"
    added_route = attempted_route = replaced = False
    replacement_written = service_conflict = unloading = False
    owned_route = None
    try:
        if report["remote"] and not report["serve_route_present"]:
            if _serve_status() != report["serve_status"]:
                raise RuntimeRefused("Serve configuration changed before route creation; review a new preflight")
            attempted_route = True
            _run(["tailscale", "serve", "--bg", "--https=8443", "--set-path=/",
                  f"http://127.0.0.1:{config['port']}"])
            added_route = True
        current = _serve_status() if report["remote"] else None
        if added_route:
            owned_route = _dashboard_scope(current, authority)
        if report["remote"]:
            _configured_route(config, current)
            if _without_dashboard(current, authority) != _without_dashboard(report["serve_status"], authority):
                raise RuntimeRefused("unrelated Serve configuration changed; refusing LaunchAgent replacement")
        (home / "Library/Logs").mkdir(parents=True, exist_ok=True)
        try:
            if _plist_bytes(target) != old:
                raise RuntimeRefused("dashboard plist changed before replacement; preserving the newer service")
            if report["loaded"] and not _owned_service(target, old, domain):
                raise RuntimeRefused("dashboard service unloaded before replacement; review a new preflight")
        except RuntimeRefused:
            service_conflict = True
            raise
        replaced = True
        if report["loaded"]:
            unloading = True
            _run(["launchctl", "bootout", f"{domain}/{LABEL}"])
            _wait_unloaded(target, old, domain)
            unloading = False
        _replace(target, replacement)
        replacement_written = True
        _run(["launchctl", "bootstrap", domain, str(target)])
        health = health_check(config["port"])
        if health.get("schema") != report["runtime"]["schema"] or health.get("library") != report["runtime"]["library"]:
            raise RuntimeRefused("new dashboard health reports a different schema or library than preflight")
        if any(health.get(key) != report["runtime"][key] for key in ("library_digest", "dashboard_digest")):
            raise RuntimeRefused("new dashboard health reports different build digests than preflight")
        if config.get("ip_origin") and health.get("ip_origin") != config["ip_origin"].rstrip("/"):
            raise RuntimeRefused("new dashboard health does not confirm the configured IP listener")
        _verify_loaded_process(domain, health)
        if report["remote"]:
            final = _serve_status()
            _configured_route(config, final)
            if _without_dashboard(final, authority) != _without_dashboard(report["serve_status"], authority):
                raise RuntimeRefused("unrelated Serve configuration changed during installation")
        return {"ok": True, "origin": config.get("ip_origin") or config.get("origin") or f"http://127.0.0.1:{config['port']}",
                "backup_path": str(backup), "health": health}
    except BaseException as error:
        failures = []
        if replaced:
            try:
                loaded = _owned_service(target, replacement if replacement_written else old, domain)
                if loaded:
                    if not unloading:
                        _run(["launchctl", "bootout", f"{domain}/{LABEL}"])
                    _wait_unloaded(target, replacement if replacement_written else old, domain)
                if old is not None:
                    _replace(target, old)
                elif target.exists():
                    target.unlink()
                if report["loaded"]:
                    _run(["launchctl", "bootstrap", domain, str(target)])
            except BaseException as failure:
                service_conflict = True
                failures.append(str(failure))
        if attempted_route and service_conflict:
            failures.append("dashboard route preserved because service rollback requires manual recovery")
        elif attempted_route:
            try:
                _rollback_route(config, report["serve_status"], owned_route)
            except BaseException as failure:
                failures.append(str(failure))
        detail = f"; rollback requires attention: {'; '.join(failures)}" if failures else "; previous service restored"
        raise RuntimeRefused(f"installation failed: {error}{detail}; backup: {backup}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "install", "health"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fingerprint")
    # health: seconds to wait for a starting server; 0, the default, tries once.
    parser.add_argument("--wait", type=float, default=0.0)
    args = parser.parse_args()
    if not (math.isfinite(args.wait) and args.wait >= 0):
        parser.error("--wait needs a finite number of seconds, 0 or more")
    named, args.config = args.config is not None, args.config or Path.home() / ".config/sd/dashboard.json"
    try:
        if args.action == "health":
            if not named and not os.path.lexists(args.config):  # a dangling link is a broken config
                # sd:9916: a host with no dashboard (a satellite) has nothing to check; a named config still must read.
                print(json.dumps({"status": "not_configured", "config": str(args.config)}))
                return 0
            config = read_config(args.config)
            load_frontdoor(args.config, config["port"])
            result = health_check(config["port"], wait=args.wait)
            behind = served_behind(result.get("commit")) or installed_gap()
            if behind:
                raise RuntimeRefused(behind)
        elif args.action == "install" and args.apply:
            if not args.expected_fingerprint:
                raise RuntimeRefused("--apply requires --expected-fingerprint from preflight")
            result = install(args.config, expected_fingerprint=args.expected_fingerprint)
        else:
            result = preflight(args.config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except NoDashboard as error:
        print(f"dashboard: {error}", file=sys.stderr)
        return 3
    except (RuntimeRefused, ImportError, ValueError) as error:
        print(f"dashboard: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
