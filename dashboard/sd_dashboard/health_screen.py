"""Health: the rows behind the default UI's Health page (sd:2115).

The page is `v2/health.html`; it holds no rows. It reads `summary`, every
area's lamp and the rail badge without waiting on a walker, and `part`, the
areas of one of `VIEWS`, each its own request, so a slow or failing view holds
up and takes down no other. `document` is every area at once, `/api/health`.
Each is built here from the readers the dashboard already has, so the port
adds no collector:

- Worktrees: `fleet.collect("sessions")`, the child Operations > Sessions and
  Today's Now read. A registration whose directory is gone is a row per
  checkout, carrying the registrations behind it; one whose files could not
  be read is a row of its own, in the unknown state.
- Ports: `ports_screen.port_rows` over `collect_ports`, Operations > Ports'
  reader, with its counts and warnings; collected for the request.
- Containers (sd:5840): the running containers in that same collection,
  from the one `docker ps` `machine-setup.sh candidates service` runs; Ports
  and Containers share one `collect_ports` call (`_Once`). A Docker that
  was not read is the area's error. No control: the rows only show.
- Protection: `protection.rows`, what the nightly `sd shadow sync` left in
  `repo_protection`, with `protection_screen`'s rule: a repository whose
  protection was not read is unknown and shows no cell.
- Disk and Branches: `health_collectors.disk_scan` and `branch_scan`
  (sd:2202, sd:2204), each inside its own budget; that module says what
  each reads and leaves out.
- Dependencies and Security (sd:2205, sd:2206): the same `protection.rows`,
  each managed repository's `alerts`, which the nightly sync stores beside
  its protection. Health makes no GitHub call. A repository with Dependabot
  alerts off is allowed: an ok row lists it (sd:5595).
- Credentials (sd:2203): the latest `credentials:nightly` heartbeat that
  `sd-db credentials` writes each night: presence, validity and expiry,
  never a value. Expiry is judged against the request's time.
- Services (sd:3707): `sd_db.services.inventory`, what `sd services list`
  and Operations > Services read, cut to the long-running user LaunchAgents.
  Restart and Start post `/api/services/<label>/<action>`, the route
  Operations posts, into `sd services restart|start`'s code with the row's
  revision. The dashboard's own row adds `runtime.served_behind`, the check
  `dashboard.sh health` makes (sd:3528); its service is protected, so the row
  offers the kickstart line to copy.
- AWS (sd:6713): one row, the operator's running AWS resources by region
  with age and a console link, from the `aws` part of the same nightly
  credentials heartbeat. No action: the console does the rest.
- Doctor (sd:3393): `health_collectors.doctor_scan`, `machine-setup.sh
  doctor` in a child under its own deadline. Its answer is kept for
  `DOCTOR_FRESH_SECONDS`, since the doctor runs `brew doctor`; the page waits
  at most `DOCTOR_WAIT_SECONDS` for a run, and a run still going, stopped or
  failed is the area's error, never a clean lamp.

The design source shows nine areas; the page leaves out its Attribution
(sd:3011). The parts no reader covers are in the
document as `missing`, by name, so
the page says what it does not read instead of showing a clean lamp. A
reader that fails is its area's `error`, never an empty area: a fleet
nobody could read must not look like a fleet with nothing wrong.

Nothing here writes. Prune, Delete merged and Remove build output post
`/api/health/cleanup` (`health_cleanup`) with the row's `revision`, which
`health_collectors` computes the same way the write does.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from sd_db import protection as protection_module
from sd_db import reads, repos, usage
from sd_db.errors import SdDbError

from . import fleet as fleet_module
from . import health_collectors, ports_screen, runtime, snooze, usage_screen
from .protection_screen import APPLICABLE, FLAGS, GAPS, ORDER

__all__ = ["AREAS", "VIEWS", "document", "part", "summary"]

#: The design's areas, in its order: id, name, the reader's source line, and what no reader covers yet.
AREAS = (
    ("disk", "Disk", "df -kPl · du -k -d 1 per dashboard.disk_storage folder · git worktree list per registered repo",
     ("build output sizes (its presence is read, not its size)", "build output in worktrees not yet merged")),
    ("cred", "Credentials", "sd-db credentials (nightly) → state heartbeat credentials:nightly", ()),
    ("wt", "Worktrees", "the fleet's .git/worktrees registrations", ()),
    ("br", "Branches", "git for-each-ref --merged=origin/HEAD refs/heads per registered repo · "
     "git worktree list against the lane entries that merged", ()),
    ("dep", "Dependencies", "sd shadow sync → repo_protection alerts · dependabot/alerts?state=open per managed repo", ()),
    ("sec", "Security", "sd shadow sync → repo_protection alerts · security_and_analysis · secret-scanning/alerts per public managed repo", ()),
    ("ports", "Ports", "collect_ports · lsof -nP -iTCP -sTCP:LISTEN · docker ps", ()),
    ("prot", "Protection", "sd shadow sync → repo_protection", ()),
    ("svc", "Services", "sd services list · launchctl print per user LaunchAgent · the dashboard's /health commit", ()),
    ("ctr", "Containers", "collect_ports · machine-setup.sh candidates service · docker ps", ()),
    ("mir", "Vault mirror", "sd-db records render → state heartbeat records-mirror · "
     "sd reports brief render → state heartbeat brief-copies", ()),
    ("doc", "Doctor", "machine-setup.sh doctor · its ok, WARN and FAIL lines", ()),
    ("aws", "AWS", "sd-db credentials (nightly) → heartbeat credentials:nightly · aws ec2 describe-instances and "
     "cloudformation describe-stacks per SD_AWS_READ profile and region", ()),
    ("stat", "Service status", "agent-meter.py services (every 5 min) → state heartbeat service-status:<name> per "
     "dashboard.status_services line", ()),
)
#: The page's views, by the operator's task (sd:2115): id, name and the areas each reads, in AREAS' order within it.
#: Machine is this Mac (its disks, services, ports and doctor); Checkouts the local git cleanups; GitHub what the nightly
#: sync stored; Jobs the heartbeats unattended jobs leave; Status the services the operator depends on (pack sd:9260).
#: Each is one request, `/api/health/<view>`.
VIEWS = (
    ("checkouts", "Checkouts", ("wt", "br")),
    ("machine", "Machine", ("disk", "svc", "ports", "ctr", "doc", "aws")),
    ("github", "GitHub", ("prot", "dep", "sec")),
    ("jobs", "Jobs", ("cred", "mir")),
    ("status", "Status", ("stat",)),
)
VIEW_AREAS = {view: keys for view, _, keys in VIEWS}
VIEW_NAMES = {view: name for view, name, _ in VIEWS}
VIEW_OF = {key: view for view, _, keys in VIEWS for key in keys}
#: The matrix lines, in the design's order: v1's gap columns, then its merge-setting flags.
CHECKS = GAPS + FLAGS
LISTENER = {"listening": "Listening", "not_listening": "No listener observed",
            "unknown": "Unknown", "unconfigured": "No port configured"}


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _worktree_rows(document) -> list[dict]:
    """One row per checkout with registrations whose directory is gone, and per checkout with unreadable ones."""
    trees = document.get("worktrees") if isinstance(document, dict) else None
    root = document.get("root") if isinstance(document, dict) else None
    if not isinstance(trees, list) or not isinstance(root, str) or any(
            not isinstance(tree, dict) or not isinstance(tree.get("state"), str) or not isinstance(tree.get("repo"), str)
            for tree in trees):
        raise ValueError("fleet collector returned an incomplete sessions document")
    # A missing root comes back as no worktrees at all, which must not read as "checked 0 registrations".
    if document.get("rootExists") is not True:
        raise ValueError(f"the checkout root {root} does not exist; REPO_ROOT names it")
    by_repo: dict[tuple[str, str], list[dict]] = {}
    for tree in trees:
        if tree["state"] in ("abandoned", "unknown"):
            by_repo.setdefault((tree["state"], tree["repo"]), []).append(tree)
    # What Prune acts on: every registration of the checkout that is not live, as the write reads it again.
    revisions = {repo: health_collectors.registrations_revision(repo, [tree for tree in trees if tree["repo"] == repo])
                 for _, repo in by_repo}
    rows = []
    for (state, repo), found in sorted(by_repo.items()):
        path = str(Path(root) / repo)
        count = len(found)
        listed = [f"{tree.get('path') or '(unnamed)'} ({tree.get('branch') or '?'})" for tree in found]
        if state == "abandoned":
            rows.append({
                "id": f"gone:{repo}", "state": "caution", "type": "worktree registrations", "repo_path": path,
                "what": f"{repo}: {count} {_plural(count, 'worktree', 'worktrees')} registered, directory gone",
                "detail": " · ".join(tree.get("branch") or "?" for tree in found),
                "kind": "Worktrees · directory gone", "facts": {"Repo": path, "Registered": str(count)},
                "list": listed, "revision": revisions[repo],
                "unreadable": len(by_repo.get(("unknown", repo), [])),
            })
        else:
            rows.append({
                "id": f"unread:{repo}", "state": "unknown", "type": "unread registrations", "repo_path": path,
                "what": f"{repo}: {count} {_plural(count, 'registration', 'registrations')} could not be read",
                "detail": "the worktree records could not be read, so whether the folder exists is unknown",
                "kind": "Worktrees · unreadable", "facts": {"Repo": path, "Unreadable": str(count)},
                "list": listed,
            })
    if not rows:
        repos = len({tree["repo"] for tree in trees})
        rows.append({
            "id": "wt:ok", "state": "ok", "type": "check",
            "what": "No registration without a directory",
            "detail": f"checked {len(trees)} {_plural(len(trees), 'registration', 'registrations')} "
                      f"in {repos} {_plural(repos, 'checkout', 'checkouts')} under {root}",
            "kind": "Worktrees", "facts": {"Registrations": str(len(trees)), "Root": root},
            "cli": "git worktree list --porcelain  # per repo",
        })
    return rows


def _fleet_rows(document) -> tuple[list[dict], dict]:
    """The Worktrees rows, dated by the fleet snapshot's `observed`, and `stale` with its reason after a failed refresh."""
    rows = _worktree_rows(document)
    extra = {"at": document["observed"]} if document.get("observed") else {}
    return rows, {**extra, "stale": document["stale"]} if document.get("stale") else extra


def _port_rows(snapshot) -> tuple[list[dict], dict]:
    """Operations > Ports' rows as ledger rows, with its counts line and warnings."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("services"), list):
        raise ValueError("invalid port collector output")
    found = ports_screen.port_rows(snapshot)
    if not found and not snapshot.get("complete"):
        raise ValueError("port inventory is unavailable")
    unique = {row["port"]: row for row in found if row["port"]}
    counts = {"configured": sum(row["configured"] for row in unique.values()),
              "listening": sum(row["listener"] == "listening" for row in unique.values()),
              "unknown": sum(row["listener"] == "unknown" for row in unique.values())}
    observed = snapshot.get("observed")
    warnings = [text for flagged, text in (
        (not snapshot.get("complete"), "Inventory collection was incomplete; listed observations may be partial."),
        (counts["unknown"], f"Listener state is unknown for {counts['unknown']} configured ports."),
        (not (isinstance(observed, dict) and observed.get("complete")),
         "The wider TCP listener inventory is incomplete or unavailable; positive observations remain visible."),
    ) if flagged]
    rows = []
    for port in found:
        listener = LISTENER[port["listener"]]
        state = ("caution" if port["clash"] or port["busy"] else
                 ("ok" if port["configured"] else "queued") if port["listener"] == "listening" else
                 "unknown" if port["listener"] == "unknown" else "queued")
        holder = port["process"] or ("Listener inspection unavailable" if port["listener"] == "unknown" else listener)
        configured = port["configured"]
        rows.append({
            "id": f"port:{port['service']}:{port['port'] or 'none'}" if configured else f"port:observed:{port['port']}",
            "state": state, "type": "port", "port": port["port"],
            "what": f"{port['service']} {':' + port['port'] if port['port'] else '· no port configured'}" if configured
                    else f":{port['port']} observed only",
            "detail": f"{listener} · {holder} · container {port['container_display'].lower()} · {port['profile']}",
            "kind": "Port · configured service" if configured else "Port · observed only",
            "facts": {"Service": port["service"], "Port": port["port"] or "—", "Listener": listener, "Process": holder,
                      "Container": port["container_display"], "Profile": port["profile"]},
            "note": "Unknown: the listener was not inspected, so nothing says whether the port is open or free."
                    if port["listener"] == "unknown" else
                    "No configured service claims this port; the row shows what the Mac reported and nothing else."
                    if not configured else "",
        })
    return rows, {"counts": counts, "warnings": warnings}


def _container_rows(snapshot) -> tuple[list[dict], dict]:
    """One row per running container, with the bind mounts it holds under the checkout root (sd:5840)."""
    found = snapshot.get("containers") if isinstance(snapshot, dict) else None
    if not isinstance(found, dict) or not isinstance(found.get("rows"), list):
        raise ValueError("the port collection carried no container read")
    if not found.get("read"):
        raise ValueError(found.get("reason") or "docker ps was not read")
    # The checkout root the collectors read (`collectors.REPO_ROOT`).
    root = os.environ.get("REPO_ROOT", os.path.expanduser("~/repos")).rstrip("/") or "/"
    rows, holding = [], 0
    for one in found["rows"]:
        # Docker Desktop names a bind mount's source by its path in its VM, under /host_mnt.
        sources = [mount.removeprefix("/host_mnt") for mount in one["mounts"] if mount.startswith("/")]
        held = [source for source in sources if source == root or source.startswith(root + "/")]
        holding += bool(held)
        project = one["project"]
        rows.append({
            "id": f"ctr:{one['name']}", "state": "ok", "type": "container", "what": one["name"],
            "detail": " · ".join(part for part in (
                one["image"], one["status"],
                f"compose {project} in {one['workdir'] or 'no working dir'}" if project else "no compose project",
                f"mounts {', '.join(held)}" if held else "") if part),
            "kind": "Container · running",
            "facts": {"Image": one["image"], "Status": one["status"], "Compose project": project or "none",
                      "Working dir": one["workdir"] or "none", "Checkouts mounted": str(len(held))},
            "list": held, "list_head": f"Bind mounts under {root}",
            "cli": f"docker inspect {shlex.quote(one['name'])}",
        })
    if not rows:
        rows.append({"id": "ctr:none", "state": "ok", "type": "check", "what": "No container running",
                     "detail": "no container is running", "kind": "Containers", "facts": {},
                     "cli": "docker ps"})
    return rows, {"running": len(found["rows"]), "holding": holding, "root": root}


def _cell(row: dict, check: str) -> list[str]:
    """One matrix cell: ['gap', sentence], ['ok'], ['na'] (does not apply) or ['unknown'] (not read)."""
    if row["status"] == "unknown":
        return ["unknown"]
    if check in dict(FLAGS):
        flag = next((flag for flag in row["merge_settings"] if flag.get("id") == check), None)
        if flag is None:
            return ["na"]
        return ["gap", f"{flag.get('value') or ''} · {flag.get('gap') or ''}"] if flag.get("flagged") else ["ok"]
    if check not in APPLICABLE.get(row["status"], frozenset()):
        return ["na"]
    gap = next((gap for gap in row["gaps"] if gap.get("id") == check), None)
    return ["gap", gap.get("gap") or ""] if gap else ["ok"]


def _protection_rows(found: list[dict]) -> tuple[list[dict], dict]:
    """One ledger row per registered repository, and the matrix's column order: unprotected, unknown, protected."""
    rows = []
    for repo in found:
        status = repo["status"] if repo["status"] in ORDER else "unknown"
        repo = {**repo, "status": status}
        cells = [[label, *_cell(repo, check)] for check, label in CHECKS]
        gaps = [cell for cell in cells if cell[1] == "gap"]
        name = repo.get("slug") or repo["repo"]
        branch = repo.get("default_branch") or "—"
        unprotected = next((gap.get("gap") for gap in repo["gaps"] if gap.get("id") == "unprotected"), None)
        rows.append({
            "id": f"prot:{repo['repo']}", "type": "branch protection", "slug": repo.get("slug"),
            "state": "warning" if status == "unprotected" else "unknown" if status == "unknown" else "caution" if gaps else "ok",
            "what": f"{name}: protection not read" if status == "unknown" else
                    f"{name}: {branch} is unprotected" if status == "unprotected" else
                    f"{name}: {len(gaps)} {_plural(len(gaps), 'gap', 'gaps')}" if gaps else f"{name}: protected, no gap",
            "detail": repo.get("reason") or "unknown" if status == "unknown" else
                      f"{branch} · {', '.join(cell[0] for cell in gaps) if gaps else 'every check passes'}",
            "kind": "Branch protection · " + status,
            "facts": {"Repository": repo["repo"], "Slug": repo.get("slug") or "no github.com remote", "Status": status,
                      "Branch": branch, "Observed": repo.get("observed_at") or "never",
                      # A sibling checkout's row, read for this one (sd:1607).
                      **({"Borrowed from": repo["borrowed_from"]} if repo.get("borrowed_from") else {})},
            "status": status, "name": name, "branch": branch, "reason": repo.get("reason") or "unknown" if status == "unknown" else "",
            "sentence": unprotected or "", "cells": [] if status == "unknown" else cells, "gaps": len(gaps),
        })
    if not rows:
        # An empty registry is not a protected fleet.
        rows.append({"id": "prot:none", "state": "unknown", "type": "check",
                     "what": "No repository registered: protection has nothing to read",
                     "detail": "register one with sd-db repo add PATH, then run sd shadow sync",
                     "kind": "Branch protection", "facts": {"Registered": "0"},
                     "cli": "sd-db repo add PATH && sd shadow sync"})
        return rows, {"counts": dict.fromkeys(ORDER, 0), "checks": [label for _, label in CHECKS], "columns": [],
                      "reasons": [], "at": None}
    order = sorted(rows, key=lambda row: (ORDER[row["status"]], -row["gaps"], row["name"]))
    counts = {status: sum(row["status"] == status for row in rows) for status in ORDER}
    observed = [repo["observed_at"] for repo in found if repo.get("observed_at")]
    return rows, {"counts": counts, "checks": [label for _, label in CHECKS], "columns": [row["id"] for row in order],
                  "reasons": sorted({row["reason"] for row in rows if row["status"] == "unknown"}),
                  "at": max(observed) if observed else None}


def _size(kb: int) -> str:
    """The design's size wording: MiB, GiB or TiB from a count of KiB."""
    if kb >= 1024 ** 3:
        return f"{kb / 1024 ** 3:.2f} TiB"
    return f"{kb / 1024 ** 2:.1f} GiB" if kb >= 1024 ** 2 else f"{round(kb / 1024)} MiB"


def _unread_repos(found: list[str], area: str, reason: str, fix: str, key: str) -> dict:
    count = len(found)
    return {"id": f"{area}:{key}", "state": "unknown", "type": "check",
            "what": f"{count} {_plural(count, 'repo has', 'repos have')} {reason.split(',')[0]}: not read",
            "detail": reason, "kind": f"{'Branches' if area in ('br', 'wt') else 'Disk'} · not read",
            "facts": {"Repos": str(count)}, "list": list(found), "cli": fix}


def _branch_rows(scan: dict) -> list[dict]:
    """Per repository, its merged branches `git branch -d` can delete, and those a worktree has checked out."""
    rows = []
    for repo in scan["merged"]:
        name, path = Path(repo["repo"]).name, repo["path"]
        if repo["deletable"]:
            found = repo["deletable"]
            names = [branch for branch, _ in found]
            dates = sorted(date for _, date in found if date)
            rows.append({
                "id": f"br:{repo['repo']}", "state": "caution", "type": "merged branches", "repo_path": path,
                "what": f"{name}: {len(found)} {_plural(len(found), 'merged branch', 'merged branches')} not deleted",
                "detail": " · ".join(names[:4]) + (f" · +{len(names) - 4}" if len(names) > 4 else ""),
                "kind": "Branches · merged",
                "facts": {"Repo": path, "Merged": str(len(found)), "Oldest": dates[0] if dates else "—",
                          "Newest": dates[-1] if dates else "—"},
                "list": [f"{branch} · {date or '?'}" for branch, date in found],
                "cli": f"git -C {shlex.quote(path)} branch -d {' '.join(shlex.quote(branch) for branch in names)}",
                "branches": names, "revision": repo.get("revision"),
            })
        if repo["checked_out"]:
            found = repo["checked_out"]
            rows.append({
                "id": f"brs:{repo['repo']}", "state": "queued", "type": "merged branches", "repo_path": path,
                "what": f"{name}: {len(found)} merged {_plural(len(found), 'branch is', 'branches are')} checked out in a worktree",
                "detail": " · ".join(branch for branch, _ in found[:4]) + (f" · +{len(found) - 4}" if len(found) > 4 else ""),
                "kind": "Branches · checked out", "facts": {"Repo": path, "Count": str(len(found))},
                "list": [f"{branch} · {tree}" for branch, tree in found],
                "disabled": "checked out in a worktree: git branch -d refuses it until the worktree is removed",
                "cli": f"git -C {shlex.quote(path)} worktree list",
            })
    if not rows and scan["repos"]:
        repos = scan["repos"]
        rows.append({"id": "br:ok", "state": "ok", "type": "check", "what": "No merged branch left undeleted",
                     "detail": f"checked the local branches of {repos} {_plural(repos, 'repo', 'repos')} against origin/HEAD",
                     "kind": "Branches", "facts": {"Repos": str(repos)},
                     "cli": "git -C <repo> branch --merged origin/HEAD  # per registered repo"})
    rows.extend(_merged_worktree_rows(scan.get("worktrees")))
    if scan["no_default"]:
        rows.append(_unread_repos(scan["no_default"], "br", "no origin/HEAD, so nothing says which branch is the default",
                                  "git -C <repo> remote set-head origin --auto", "no_default"))
    if scan["unread"]:
        rows.append(_unread_repos(scan["unread"], "br", "a git error, so its branches were not listed",
                                  "git -C <repo> for-each-ref --merged=refs/remotes/origin/HEAD refs/heads/", "unread"))
    if not rows:
        # An empty registry is not a fleet with nothing to delete.
        rows.append({"id": "br:none", "state": "unknown", "type": "check",
                     "what": "No repository registered: Branches has nothing to read",
                     "detail": "register one with sd-db repo add PATH", "kind": "Branches",
                     "facts": {"Registered": "0"}, "cli": "sd-db repo add PATH"})
    return rows


def _merged_worktree_rows(found: dict | None) -> list[dict]:
    """The worktrees the lane merged that are still on disk (sd:5105); `found` is None from a seam that predates them."""
    trees = (found or {}).get("trees") or []
    unread = (found or {}).get("unread") or []
    # A repository whose worktree list failed is named, never read as having no merged worktree.
    rows = [_unread_repos(unread, "wt", "a git error, so its merged worktrees were not listed",
                          "git -C <repo> worktree list --porcelain", "unread")] if unread else []
    if not trees:
        return rows
    go = [tree for tree in trees if not tree["kept"]]

    def remove(tree: dict) -> str:
        repo, path, branch = shlex.quote(tree["repo_path"]), shlex.quote(tree["path"]), tree["lane_branch"]
        return (f"git -C {repo} worktree remove {path}"
                + (f" && git -C {repo} update-ref -d {shlex.quote('refs/heads/' + branch)} {tree['head']}" if branch else ""))
    return rows + [{
        "id": "wt:merged", "state": "caution" if go else "queued", "type": "merged worktrees",
        "what": f"{len(trees)} merged {_plural(len(trees), 'worktree is', 'worktrees are')} still on disk",
        "detail": " · ".join(tree["path"] for tree in trees[:4]) + (f" · +{len(trees) - 4}" if len(trees) > 4 else ""),
        "kind": "Branches · merged worktrees",
        "facts": {"Found": str(len(trees)), "Removable": str(len(go)), "Kept": str(len(trees) - len(go))},
        "list": [f"{tree['path']} · sd:{tree['item']} · {tree['lane_branch'] or 'detached'}"
                 + (f" · kept: {tree['kept']}" if tree["kept"] else "") for tree in trees],
        "cli": "\n".join(remove(tree) for tree in go) or "no merged worktree can go: each says why it stays",
        "removable": [tree["path"] for tree in go],
        "kept": [{"name": tree["path"], "why": tree["kept"]} for tree in trees if tree["kept"]],
        "revision": found.get("revision"),
    }]


#: The design's volume thresholds, in percent used.
VOLUME_WARNING, VOLUME_CAUTION = 90, 80


def _volume_name(mount: str) -> str:
    return "Mac data" if mount in ("/System/Volumes/Data", "/") else mount.removeprefix("/Volumes/")


def _disk_rows(scan: dict) -> tuple[list[dict], dict]:
    """A row per volume past 80%, the three biggest folders per storage folder, and merged worktrees with build output."""
    rows = []
    volumes = [{**volume, "name": _volume_name(volume["mount"])} for volume in scan["volumes"]]
    for volume in volumes:
        capacity = volume["capacity"]
        if capacity >= VOLUME_CAUTION:
            rows.append({
                "id": f"vol:{volume['mount']}", "state": "warning" if capacity >= VOLUME_WARNING else "caution",
                "type": "volume", "what": f"{volume['name']} is {capacity}% full",
                "detail": f"{volume['mount']} · {_size(volume['avail_kb'])} free of {_size(volume['size_kb'])}",
                "kind": "Disk · volume",
                "facts": {"Mount": volume["mount"], "Used": f"{capacity}%", "Free": _size(volume["avail_kb"]),
                          "Size": _size(volume["size_kb"]), "File system": volume["filesystem"]},
                "cli": f"df -h {shlex.quote(volume['mount'])}",
                "note": f"Health lights caution at {VOLUME_CAUTION}% and warning at {VOLUME_WARNING}%.",
                # A snooze's problem (sd:1896): the band, not the percent, which drifts with every write.
                "problem": [volume["mount"]],
            })
    for entry in scan["storage"]:
        root = entry["root"]
        share = entry.get("network") or entry.get("drive")
        if share:
            # sd:5726: a folder on a network mount is not walked; its share's use stands in, at the volume thresholds.
            # sd:5879: so does a local drive's when du did not finish on its folder.
            used, size, free = share["used_kb"], share["size_kb"], share["avail_kb"]
            capacity = round(100 * used / (used + free)) if used + free else 0
            use = f"{_size(used)} used of {_size(size)}, {_size(free)} free"
            rows.append({
                "id": f"rs:{root}", "state": "warning" if capacity >= VOLUME_WARNING else "caution" if capacity >= VOLUME_CAUTION else "queued",
                "type": "volume",
                "what": f"{Path(root).name} on a network mount: {use}" if "type" in share else f"{Path(root).name}: drive {use}",
                "detail": f"{root} · {share['type']} share at {share['mount']} · folders not measured" if "type" in share else
                          f"{root} · local drive at {share['mount']} · "
                          f"{entry.get('du') or 'du did not finish inside the Disk budget'}; folders not measured",
                "kind": "Disk · storage folder",
                "facts": {"Path": root, "Mount": share["mount"], "File system": share.get("type", "local"), "Used": f"{capacity}%",
                          "Free": _size(free), "Size": _size(size)},
                "cli": f"df -h {shlex.quote(root)}",
                "note": "Health does not measure folders on a network drive: that took over 120 s, and Disk has 8 s."
                        if "type" in share else
                        "Measuring the folders took longer than Disk's 8 s, so Health shows the drive's total, not its folders.",
                "problem": [root],
            })
            continue
        if entry["error"]:
            rows.append({"id": f"rs:{root}", "state": "unknown", "type": "check", "what": f"{root}: sizes not read",
                         "detail": entry["error"], "kind": "Disk · storage folder", "facts": {"Path": root},
                         "cli": f"du -sh {shlex.quote(root)}/* | sort -h | tail -5"})
            continue
        for folder in entry["folders"][:3]:
            path = folder["path"]
            rows.append({
                "id": f"rs:{path}", "state": "queued", "type": "storage folder",
                "what": f"{Path(root).name}/{Path(path).name} holds {_size(folder['kb'])}",
                "detail": f"{path} · largest of {len(entry['folders'])} {_plural(len(entry['folders']), 'folder', 'folders')} in {root}",
                "kind": "Disk · storage folder", "facts": {"Path": path, "Size": _size(folder["kb"]), "Storage folder": root},
                "cli": f"du -sh {shlex.quote(path)}/* | sort -h | tail -5",
                "note": "Not a fault: a storage folder is where large uncommitted data belongs. Review it when its volume passes 80%.",
                "problem": [path],  # its size drifts with every write (sd:1896)
            })
    if scan.get("problem"):
        rows.append({"id": "rs:unread", "state": "unknown", "type": "check", "what": "Storage folders not read: sizes not read",
                     "detail": f"{scan['config']} {scan['problem']}", "kind": "Disk · storage folder",
                     "facts": {"Config": scan["config"]}})
    elif not scan["storage"]:
        rows.append({"id": "rs:none", "state": "unknown", "type": "check", "what": "No storage folder configured: sizes not read",
                     "detail": f"name each with a storage|<path> line in Settings, Dashboard ({scan['config']})"
                               + (f" ({len(scan['refused'])} {_plural(len(scan['refused']), 'line', 'lines')} not understood)" if scan["refused"] else ""),
                     "kind": "Disk · storage folder", "facts": {"Config": scan["config"]}, "list": list(scan["refused"])})
    elif scan["refused"]:
        rows.append({"id": "rs:refused", "state": "unknown", "type": "check",
                     "what": f"{len(scan['refused'])} storage {_plural(len(scan['refused']), 'line', 'lines')} not understood",
                     "detail": f"{scan['config']} · each line is storage|<path>", "kind": "Disk · storage folder",
                     "facts": {"Config": scan["config"]}, "list": list(scan["refused"])})
    build = scan["build"]
    if build["merged"]:
        merged = build["merged"]
        # Only a clean worktree's folders go; one with uncommitted changes is listed and kept.
        clean = [tree for tree in merged if not tree.get("status")]
        remove = [f"{tree['path']}/{name}" for tree in clean for name in tree["dirs"]]
        rows.append({
            "id": "build:merged", "state": "caution", "type": "build output",
            "what": f"{len(merged)} merged {_plural(len(merged), 'worktree keeps', 'worktrees keep')} build output",
            "detail": " · ".join(tree["path"] for tree in merged), "kind": "Disk · worktree build output",
            "facts": {"Rule": "delete build output once the PR merges (2026-09-25)", "Checked": f"{build['checked']} worktrees",
                      "Found": str(len(merged)), "Clean": str(len(clean))},
            "list": [f"{tree['path']} · {', '.join(name + '/' for name in tree['dirs'])} · {tree['branch'] or 'detached'}"
                     + ("" if tree in clean else " · uncommitted changes: kept") for tree in merged],
            "cli": "\n".join(f"rm -rf {shlex.quote(path)}" for path in remove) or "no clean worktree: nothing to remove",
            "remove": remove, "kept": [tree["path"] for tree in merged if tree not in clean],
            "revision": build.get("revision"),
        })
    else:
        rows.append({
            "id": "build:merged", "state": "ok", "type": "check", "what": "No merged worktree keeps build output",
            "detail": f"checked {', '.join(name + '/' for name in health_collectors.BUILD_DIRS)} in {build['checked']} registered "
                      f"{_plural(build['checked'], 'worktree', 'worktrees')}",
            "kind": "Disk · worktree build output",
            "facts": {"Rule": "delete build output once the PR merges (2026-09-25)", "Checked": f"{build['checked']} worktrees",
                      "Found": "0 merged with build output"},
            "cli": "git worktree list --porcelain  # per repo, then ls -d <path>/{target,node_modules,.venv}",
        })
    if build["unread"]:
        rows.append({"id": "build:unread", "state": "unknown", "type": "check",
                     "what": f"{len(build['unread'])} {_plural(len(build['unread']), 'worktree', 'worktrees')} with build output: merge not read",
                     "detail": "git merge-base --is-ancestor or git status failed, so nobody can say whether it may go",
                     "kind": "Disk · worktree build output", "facts": {"Worktrees": str(len(build["unread"]))},
                     "list": list(build["unread"])})
    return rows, {"volumes": volumes}


def _managed(found: list[dict]) -> list[dict]:
    """The managed repositories, one per GitHub repository: of sibling checkouts, the newest observation."""
    if found and not any("managed" in repo for repo in found):
        # An installed sd_db older than sd:2205 reports neither flag nor alerts: not "no managed repository".
        raise ValueError("the installed sd_db reports no managed flag; install the build that stores alerts (sd:2205)")
    newest: dict[str, dict] = {}
    for repo in found:
        key = repo.get("slug") or repo["repo"]
        if repo.get("managed") and (key not in newest or (repo.get("observed_at") or "") > (newest[key].get("observed_at") or "")):
            newest[key] = repo
    return list(newest.values())


def _alert_extra(managed: list[dict]) -> dict:
    observed = [repo["observed_at"] for repo in managed if repo.get("observed_at")]
    return {"at": max(observed) if observed else None}


#: An alert observation older than this means the nightly sync has stopped reading that repository.
ALERTS_STALE_HOURS = 48


def _stale(area: str, name: str, managed: list[dict], unread: set[str], now: str) -> list[dict]:
    """A caution row naming each read repository whose observation is stale: a stopped sync is not a clean fleet."""
    moment = _when(now)
    stale = [f"{repo.get('slug') or repo['repo']}: observed {repo['observed_at']}" for repo in managed
             if repo["repo"] not in unread and repo.get("observed_at")
             and (moment - _when(repo["observed_at"])).total_seconds() / 3600 > ALERTS_STALE_HOURS]
    if not stale:
        return []
    return [{"id": f"{area}:stale", "state": "caution", "type": "check",
             "what": f"{len(stale)} managed {_plural(len(stale), 'repo', 'repos')}: {name.lower()} not re-read in {ALERTS_STALE_HOURS} hours",
             "detail": "the nightly sd shadow sync has not observed them since", "kind": f"{name} · stale",
             "facts": {"Repos": str(len(stale))}, "list": stale, "cli": "sd shadow sync"}]


def _no_managed(area: str, name: str) -> dict:
    # An empty list is not a clean fleet.
    return {"id": f"{area}:none", "state": "unknown", "type": "check",
            "what": f"No managed repository: {name} has nothing to read",
            "detail": "the nightly sync reads alerts for managed repositories only",
            "kind": name, "facts": {"Managed": "0"}, "cli": "cd PATH && sd fleet standard --apply && sd shadow sync"}


def _not_read(area: str, name: str, unread: list[str]) -> dict:
    count = len(unread)
    return {"id": f"{area}:unread", "state": "unknown", "type": "check",
            "what": f"{count} managed {_plural(count, 'repo', 'repos')}: {name.lower()} not read",
            "detail": "a read that failed is not a count of zero", "kind": f"{name} · not read",
            "facts": {"Repos": str(count)}, "list": unread, "cli": "sd shadow sync"}


def _unread_reason(repo: dict, part: dict | None) -> str:
    name = repo.get("slug") or repo["repo"]
    if repo.get("alerts") is None:
        return f"{name}: {repo.get('reason') or 'not read yet; the next sd shadow sync reads it'}"
    return f"{name}: {part.get('reason') or 'not read'}" if isinstance(part, dict) else f"{name}: stored alerts are malformed"


def _dependency_rows(found: list[dict], *, now: str) -> tuple[list[dict], dict]:
    """A row per managed repository with open Dependabot alerts, one for those not read, one for those with alerts
    off, and the clean count. Alerts off is allowed (sd:5595): an ok row names those repos, so it stays visible."""
    managed = _managed(found)
    if not managed:
        return [_no_managed("dep", "Dependencies")], {}
    rows, unread, off, clean, archived, skipped = [], [], [], 0, 0, set()
    for repo in managed:
        part = (repo.get("alerts") or {}).get("dependabot")
        name = repo.get("slug") or repo["repo"]
        if isinstance(part, dict) and part.get("archived"):
            archived += 1
        elif isinstance(part, dict) and part.get("setting") == "disabled":
            off.append(name)
        elif not isinstance(part, dict) or not isinstance(part.get("open"), int):
            unread.append(_unread_reason(repo, part))
            skipped.add(repo["repo"])
        elif not part["open"]:
            clean += 1
        else:
            count = f"{part['open']}{'+' if part.get('more') else ''}"
            severity = part.get("severity") if isinstance(part.get("severity"), dict) else {}
            # The severity of alerts past the first page is unread, so a page cut short counts as grave.
            grave = sum(severity.get(level, 0) for level in ("critical", "high")) or part.get("more")
            rows.append({
                "id": f"dep:{repo['repo']}", "state": "warning" if grave else "caution", "type": "dependabot alerts",
                "slug": repo.get("slug"), "repo_path": repo["repo"],
                "grave": sum(severity.get(level, 0) for level in ("critical", "high")),
                "what": f"{name}: {count} open Dependabot {_plural(part['open'], 'alert', 'alerts')}",
                "detail": " · ".join([f"{severity[level]} {level}" for level in ("critical", "high", "medium", "low", "unknown")
                                      if severity.get(level)] + ["severity past the first page not read"] * bool(part.get("more")))
                          or "severity not read",
                "kind": "Dependencies · open alerts",
                "facts": {"Repository": repo["repo"], "Open": str(part["open"]), "Shown": count,
                          "Observed": repo.get("observed_at") or "never"},
                "cli": f"gh api --paginate {shlex.quote(f'repos/{name}/dependabot/alerts?state=open')} --jq '.[].html_url'",
            })
    if unread:
        rows.append(_not_read("dep", "Dependencies", unread))
    rows += _stale("dep", "Dependencies", managed, skipped, now)
    if clean and not any(row["type"] == "dependabot alerts" for row in rows):
        rows.append({"id": "dep:ok", "state": "ok", "type": "check",
                     "what": f"No open Dependabot alert in {clean} managed {_plural(clean, 'repo', 'repos')}",
                     "detail": f"archived repos left out: {archived}", "kind": "Dependencies",
                     "facts": {"Clean": str(clean), "Archived": str(archived)}})
    if off:
        rows.append({"id": "dep:off", "state": "ok", "type": "check",
                     "what": f"Dependabot alerts off in {len(off)} managed {_plural(len(off), 'repo', 'repos')}",
                     "detail": "allowed; the nightly sync reads no alerts there", "kind": "Dependencies · alerts off",
                     "facts": {"Alerts off": str(len(off))}, "list": off})
    return rows, _alert_extra(managed)


#: Dependabot's severities, worst first; a level GitHub adds later ranks after `unknown`.
SEVERITIES = ("critical", "high", "medium", "low", "unknown")


def _rank(level: object) -> int:
    return SEVERITIES.index(level) if level in SEVERITIES else len(SEVERITIES)


def dependabot_document(found: list[dict]) -> dict:
    """GitHub > Dependabot (pack sd:7515): a group per managed repository with open alerts, from the same stored rows
    `_dependency_rows` reads, so it makes no GitHub call.

    A group carries what Health's Fix reads (`slug`, `repo_path`, `grave`, `facts`), so the one shared Fix serves both
    pages, and `alerts`, the stored summaries critical first, newest first within a severity; None when the row predates
    them (stored before pack sd:7515), so the page says the next sync stores them. Groups sort worst severity first,
    then most alerts. Repositories with alerts off, not read or archived are names and counts, not groups.
    """
    try:
        managed = _managed(found)
    except ValueError as error:
        return {"error": str(error), "groups": [], "off": [], "unread": [], "clean": 0, "archived": 0, "managed": 0,
                "at": None}
    groups, off, unread, clean, archived = [], [], [], 0, 0
    for repo in managed:
        part = (repo.get("alerts") or {}).get("dependabot")
        name = repo.get("slug") or repo["repo"]
        if isinstance(part, dict) and part.get("archived"):
            archived += 1
        elif isinstance(part, dict) and part.get("setting") == "disabled":
            off.append(name)
        elif not isinstance(part, dict) or not isinstance(part.get("open"), int):
            unread.append(_unread_reason(repo, part))
        elif not part["open"]:
            clean += 1
        else:
            counted = part.get("severity")
            severity: dict = counted if isinstance(counted, dict) else {}
            stored = part.get("summaries")
            alerts = None if not isinstance(stored, list) else sorted(
                (alert for alert in stored if isinstance(alert, dict) and type(alert.get("number")) is int),
                key=lambda alert: (_rank(alert.get("severity")), -alert["number"]))
            levels = [level for level in severity if severity.get(level)] or [alert.get("severity") for alert in alerts or []]
            shown = f"{part['open']}{'+' if part.get('more') else ''}"
            groups.append({
                "id": f"dep:{repo['repo']}", "type": "dependabot alerts", "name": name, "slug": repo.get("slug"),
                "repo_path": repo["repo"], "open": part["open"], "more": bool(part.get("more")), "shown": shown,
                "severity": {level: severity[level] for level in sorted(severity, key=_rank) if severity.get(level)},
                "worst": min(levels, key=_rank) if levels else "unknown",
                "grave": sum(severity.get(level, 0) for level in ("critical", "high")),
                "observed_at": repo.get("observed_at"), "alerts": alerts,
                "facts": {"Repository": repo["repo"], "Shown": shown}})
    groups.sort(key=lambda group: (_rank(group["worst"]), -group["open"], group["name"]))
    return {"groups": groups, "off": sorted(off), "unread": unread, "clean": clean, "archived": archived,
            "managed": len(managed), "at": _alert_extra(managed)["at"]}


def _security_rows(found: list[dict], *, now: str) -> tuple[list[dict], dict]:
    """Per public managed repository: open secret-scanning alerts, or scanning off; private ones are not scanned."""
    managed = _managed(found)
    if not managed:
        return [_no_managed("sec", "Security")], {}
    rows, unread, clean, private, skipped = [], [], 0, 0, set()
    for repo in managed:
        part = (repo.get("alerts") or {}).get("secret_scanning")
        name = repo.get("slug") or repo["repo"]
        settings = f"https://github.com/{name}/settings/security_analysis"
        if isinstance(part, dict) and part.get("visibility") == "private":
            private += 1
        elif isinstance(part, dict) and part.get("setting") == "disabled":
            rows.append({"id": f"sec:off:{repo['repo']}", "state": "caution", "type": "secret scanning",
                         "slug": repo.get("slug"), "what": f"{name}: public, secret scanning off",
                         "detail": "a public repo with scanning off is a finding", "kind": "Security · scanning off",
                         "facts": {"Repository": repo["repo"], "Setting": "disabled",
                                   "Observed": repo.get("observed_at") or "never"},
                         "cli": f"open {shlex.quote(settings)}"})
        elif not isinstance(part, dict) or not isinstance(part.get("open"), int):
            unread.append(_unread_reason(repo, part))
            skipped.add(repo["repo"])
        elif not part["open"]:
            clean += 1
        else:
            count = f"{part['open']}{'+' if part.get('more') else ''}"
            rows.append({"id": f"sec:{repo['repo']}", "state": "warning", "type": "secret scanning",
                         "slug": repo.get("slug"),
                         "what": f"{name}: {count} open secret-scanning {_plural(part['open'], 'alert', 'alerts')}",
                         "detail": "rotate the secret first, then close the alert", "kind": "Security · open alerts",
                         "facts": {"Repository": repo["repo"], "Open": str(part["open"]), "Shown": count,
                                   "Observed": repo.get("observed_at") or "never"},
                         "cli": f"gh api --paginate {shlex.quote(f'repos/{name}/secret-scanning/alerts?state=open')} --jq '.[].html_url'"})
    if unread:
        rows.append(_not_read("sec", "Security", unread))
    rows += _stale("sec", "Security", managed, skipped, now)
    if clean and not any(row["type"] == "secret scanning" for row in rows):
        rows.append({"id": "sec:ok", "state": "ok", "type": "check",
                     "what": f"Scanning on, no open alert, in {clean} public managed {_plural(clean, 'repo', 'repos')}",
                     "detail": f"private repos are not scanned, by policy: {private}", "kind": "Security",
                     "facts": {"Clean": str(clean), "Private": str(private)}})
    return rows, _alert_extra(managed)


#: Days before a credential's expiry that Health lights caution, then warning.
EXPIRY_CAUTION, EXPIRY_WARNING = 30, 7
#: A heartbeat older than this means the nightly job has stopped.
CREDENTIALS_STALE_HOURS = 48
CREDENTIALS_CLI = "sd-db credentials"


def read_credentials(connection: sqlite3.Connection) -> tuple[str, dict] | None:
    """`sd_db.credentials.read`, imported here so an installed library older than sd:2203 is the area's error."""
    try:
        credentials = importlib.import_module("sd_db.credentials")
    except ImportError as missing:
        raise ValueError("the installed sd_db has no credentials module; install the build with sd:2203") from missing
    return credentials.read(connection)


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def _credential_row(probe: dict, now: datetime) -> dict:
    """One probe as a ledger row: absent or rejected is a warning, an expiry near or past lights by `EXPIRY_*`."""
    name, pid = probe.get("name") or probe.get("id") or "?", probe.get("id") or "?"
    row = {"id": f"cred:{pid}", "type": "credential", "kind": "Credentials · " + name.split(" (")[0],
           "facts": {"Credential": name}, "cli": CREDENTIALS_CLI}
    if isinstance(probe.get("signin"), str):
        # sd:6722: Sign in starts the login in herdr; the server reads the line again from disk, never from this row.
        row["signin"] = probe["signin"]
    facts = row["facts"]
    for key, label in (("present", "Present"), ("valid", "Accepted"), ("signed_in", "Signed in"), ("expires", "Expires")):
        if key in probe:
            facts[label] = "—" if probe[key] is None else str(probe[key]).lower() if isinstance(probe[key], bool) else probe[key]
    servers = probe.get("servers")
    if isinstance(servers, list):
        # An entry that is not a server object is not a connected server.
        down = [server if isinstance(server, dict) else {"name": "?", "status": "unreadable"}
                for server in servers if not isinstance(server, dict) or server.get("status") != "connected"]
        facts["Servers"] = str(len(servers))
        if down:
            return {**row, "state": "caution", "what": f"{len(down)} of {len(servers)} MCP servers not connected",
                    "detail": " · ".join(f"{s.get('name')} {s.get('status')}" for s in down[:4]),
                    "list": [f"{s.get('name')} · {s.get('status')}" for s in down], "cli": "claude mcp list"}
        return {**row, "state": "ok", "what": f"All {len(servers)} MCP servers connected", "detail": name}
    if probe.get("present") is False:
        return {**row, "state": "warning", "what": f"{name}: absent", "detail": "not set in the job's environment (env.sh)"}
    if probe.get("valid") is False or probe.get("signed_in") is False:
        return {**row, "state": "warning", "what": f"{name}: rejected",
                "detail": "the service refused it" if "valid" in probe else
                "signed out: Sign in starts the login" if "signin" in row else "gh auth status failed"}
    if probe.get("reason"):
        return {**row, "state": "unknown", "what": f"{name}: not read", "detail": probe["reason"]}
    if probe.get("expires"):
        days = (_when(probe["expires"]) - now).days
        state = "warning" if days <= EXPIRY_WARNING else "caution" if days <= EXPIRY_CAUTION else "ok"
        return {**row, "state": state, "what": f"{name}: expired" if days < 0 else f"{name}: expires in {days} days",
                "detail": f"expires {probe['expires']}",
                "note": f"Health lights caution {EXPIRY_CAUTION} days before expiry and warning at {EXPIRY_WARNING}.",
                "problem": [name, probe["expires"]]}  # the days left drift each day; the state carries the band (sd:1896)
    if probe.get("valid") or probe.get("signed_in"):
        return {**row, "state": "ok", "what": f"{name}: accepted",
                "detail": "no expiry reported" if "expires" in probe else name}
    return {**row, "state": "unknown", "what": f"{name}: not read", "detail": "the probe recorded no answer"}


def _credential_rows(found: tuple[str, dict] | None, *, now: str) -> tuple[list[dict], dict]:
    """A row per probe of the last nightly run, and a caution row when that run is stale."""
    if found is None:
        return [{"id": "cred:none", "state": "unknown", "type": "check", "what": "Credentials not yet observed",
                 "detail": "the nightly job writes presence and expiry: local-cron-jobs/examples/credentials-nightly.job",
                 "kind": "Credentials", "facts": {"Heartbeat": "credentials:nightly"}, "cli": CREDENTIALS_CLI}], {"at": None}
    stamp, body = found
    moment = _when(now)
    rows = [_credential_row(probe, moment) if isinstance(probe, dict) else
            {"id": f"cred:bad{index}", "state": "unknown", "type": "check", "what": "A credential probe is unreadable",
             "detail": "the heartbeat holds an entry that is not a probe", "kind": "Credentials", "facts": {},
             "cli": CREDENTIALS_CLI}
            for index, probe in enumerate(body["probes"])]
    if not rows:
        # No probe is not a clean bill.
        rows.append({"id": "cred:empty", "state": "unknown", "type": "check", "what": "The credentials heartbeat names no probe",
                     "detail": f"recorded {stamp}", "kind": "Credentials", "facts": {"Observed": stamp}, "cli": CREDENTIALS_CLI,
                     "problem": ["no probe"]})  # each night's run records a new time for the same answer (sd:1896)
    age = (moment - _when(stamp)).total_seconds() / 3600
    if age > CREDENTIALS_STALE_HOURS:
        rows.append({"id": "cred:stale", "state": "caution", "type": "check",
                     "what": f"Credentials last observed {round(age / 24)} days ago",
                     "detail": f"the nightly job has not recorded since {stamp}", "kind": "Credentials",
                     "facts": {"Observed": stamp}, "cli": CREDENTIALS_CLI,
                     "problem": [stamp]})  # the age drifts each day; a later stamp is a new stall (sd:1896)
    return rows, {"at": stamp}


_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


def _console(resource: dict) -> str:
    """The resource's AWS console page; '' for a region that is not one."""
    region, rid = str(resource.get("region") or ""), quote(str(resource.get("id") or ""), safe="")
    if not _REGION.match(region) or not rid:
        return ""
    base = f"https://{region}.console.aws.amazon.com"
    if resource.get("type") == "EC2 instance":
        return f"{base}/ec2/home?region={region}#InstanceDetails:instanceId={rid}"
    return f"{base}/cloudformation/home?region={region}#/stacks/stackinfo?stackId={rid}"


def _age(since, now: datetime) -> str:
    try:
        days = (now - _when(str(since))).days
    except ValueError:
        return "age unknown"
    return "under a day" if days < 1 else f"{days} {_plural(days, 'day', 'days')}"


def _aws_rows(found: tuple[str, dict] | None, *, now: str) -> tuple[list[dict], dict]:
    """One row per running AWS resource, with its console link; a failed read is an unknown row, never clean."""
    row = {"id": "aws", "type": "check", "kind": "AWS", "cli": CREDENTIALS_CLI, "facts": {"Setting": "SD_AWS_READ"}}
    if found is None:
        return [{**row, "state": "unknown", "what": "AWS resources not yet observed",
                 "detail": "the nightly credentials job reads them"}], {"at": None}
    stamp, body = found
    aws = body.get("aws")
    if not isinstance(aws, dict):
        return [{**row, "state": "ok", "what": "AWS read not configured",
                 "detail": "set SD_AWS_READ='profile:region,region' in env.sh"}], {"at": stamp}
    moment = _when(now)
    found_rows = sorted((one for one in aws.get("resources") or [] if isinstance(one, dict)),
                        key=lambda one: (str(one.get("region")), str(one.get("since")), str(one.get("profile"))))
    facts = {**row["facts"], "Observed": stamp}
    rows = [{**row, "id": f"aws:{one.get('profile')}:{one.get('region')}:{one.get('id')}", "state": "ok",
             "what": f"{one.get('type')} {one.get('name')}",
             "detail": f"{one.get('region')} · running {_age(one.get('since'), moment)}", "kind": "AWS · running",
             "facts": {"Region": str(one.get("region")), "Type": str(one.get("type")), "Profile": str(one.get("profile")),
                       "Since": str(one.get("since")), **facts},
             "list_head": "Console", "list": [{"text": "AWS console", "href": _console(one)}] if _console(one) else []}
            for one in found_rows]
    errors = [str(error) for error in aws.get("errors") or []]
    if errors:
        rows.append({**row, "id": "aws:failed", "state": "unknown",
                     "what": f"{len(errors)} AWS {_plural(len(errors), 'read', 'reads')} failed",
                     "detail": "the resources it would list are not shown", "facts": facts, "list_head": "Failed reads",
                     "list": errors, "problem": errors})
    return rows or [{**row, "state": "ok", "what": "No running AWS resources", "detail": "none running",
                     "facts": facts}], {"at": stamp}


MIRROR_CLI = "sd-db records render"


def read_mirror(connection: sqlite3.Connection) -> tuple[str, dict] | None:
    """`sd_db.records.mirror_state` (sd:3977), looked up here so a library without it is the area's error, never clean."""
    try:
        return importlib.import_module("sd_db.records").mirror_state(connection)
    except (ImportError, AttributeError) as missing:
        raise ValueError("the installed sd_db has no records.mirror_state; install a pack with it") from missing


def _with_row(found: tuple[list[dict], dict], row: dict) -> tuple[list[dict], dict]:
    rows, extra = found
    return [*rows, row], extra


def _mirror_rows(found: tuple[str, dict] | None) -> tuple[list[dict], dict]:
    """One row: the last render of the vault mirror, ok only when its heartbeat says so. Read-only: the page writes no vault note (sd:5134)."""
    row = {"id": "mir", "type": "vault mirror", "kind": "Vault mirror", "cli": MIRROR_CLI}
    if found is None:
        return [{**row, "state": "unknown", "what": "Vault mirror not yet rendered",
                 "detail": "a base row write records it", "facts": {"Heartbeat": "records-mirror"}}], {"at": None}
    stamp, body = found
    refused = [f"{one.get('path')}: {one.get('reason')}" for one in body.get("refused") or [] if isinstance(one, dict)]
    facts = {"Observed": stamp, "Kinds": ", ".join(body.get("kinds") or []) or "—", "Written": str(body.get("written", "—"))}
    if body.get("ok") is True:
        return [{**row, "state": "ok", "what": "Vault mirror rendered", "detail": f"recorded {stamp}", "facts": facts}], {"at": stamp}
    why = body.get("reason") or (f"{len(refused)} file(s) refused" if refused else "the heartbeat says not ok")
    return [{**row, "state": "warning", "what": "Vault mirror not updated", "detail": why, "facts": facts,
             "list": refused, "problem": [why, *refused]}], {"at": stamp}


BRIEF_COPIES_CLI = "sd reports brief render"
#: A brief render older than this is caution: the routines file briefs daily, and each add renders (sd:3459).
BRIEF_COPIES_STALE_HOURS = 48


def read_brief_copies(connection: sqlite3.Connection) -> tuple[str, dict] | None:
    """`sd_db.routine_briefs.copies_state` (sd:3459), looked up here so a library without it is the row's unknown."""
    try:
        return importlib.import_module("sd_db.routine_briefs").copies_state(connection)
    except (ImportError, AttributeError) as missing:
        raise ValueError("the installed sd_db has no routine_briefs.copies_state; install a pack with it") from missing


def _brief_copies_row(read, *, now: str) -> dict:
    """One row: the last brief render's vault copies. Failed, unread or stale is a finding; off is quiet."""
    row = {"id": "mir:briefs", "type": "check", "kind": "Brief copies", "cli": BRIEF_COPIES_CLI,
           "facts": {"Heartbeat": "brief-copies"}}
    try:
        found = read()
    except Exception as error:  # noqa: BLE001 - a failed read is unknown, never clean
        return {**row, "state": "unknown", "what": "Brief copies not read", "detail": str(error) or type(error).__name__}
    if found is None:
        return {**row, "state": "unknown", "what": "Brief copies not yet rendered",
                "detail": "sd reports brief add or render records the heartbeat"}
    stamp, body = found
    body = body if isinstance(body, dict) else {}
    facts = {**row["facts"], "Observed": stamp, "Written": str(body.get("written", "—"))}
    state = body.get("state")
    if state == "failed" or state not in ("ok", "off"):
        refused = [str(stem) for stem in body.get("refused") or []]
        why = body.get("reason") or (f"{len(refused)} copies refused" if refused else f"the heartbeat says {state!r}")
        return {**row, "state": "warning", "what": "Brief copies not written", "detail": why, "facts": facts,
                "list": refused, "problem": [why, *refused]}
    age = (_when(now) - _when(stamp)).total_seconds() / 3600
    if age > BRIEF_COPIES_STALE_HOURS:
        return {**row, "state": "caution", "what": f"Brief copies last rendered {round(age / 24)} days ago",
                "detail": f"no brief render has recorded since {stamp}", "facts": facts,
                "problem": [stamp]}  # the age drifts each day; a later stamp is a new stall (sd:1896)
    if state == "off":
        return {**row, "state": "ok", "what": "Brief copies off", "detail": "no repository's obsidian channel is on",
                "facts": facts}
    return {**row, "state": "ok", "what": "Brief copies rendered", "detail": f"recorded {stamp}", "facts": facts}


STATUS_CLI = "local-agent-meter/agent-meter.py services"


def _status_rows(found: list[dict], *, now: str, removed: bool = False) -> tuple[list[dict], dict]:
    """One row per watched service (pack sd:9260): its status, its open incidents with their links, how long the oldest
    has lasted and its read time. Degraded is caution and outage a warning; a read that failed or is older than
    `usage_screen.STATUS_STALE_MINUTES` is unknown with why. The list's add and remove are Settings' (`extra.settings`)."""
    extra = {"settings": usage_screen.SERVICES_LIST, "counts": {status: sum(one["status"] == status for one in found)
                                                                for status in usage_screen.SERVICE_STATE}}
    if not found and removed:  # rows exist and none answers: each was removed, so none is watched
        return [], extra
    if not found:
        return [{"id": "stat:none", "state": "unknown", "type": "check", "what": "No service status read yet",
                 "detail": "agent-meter.py services writes one row per dashboard.status_services line every 5 minutes",
                 "kind": "Service status", "facts": {"Heartbeat": "service-status:<name>"}, "cli": STATUS_CLI}], extra
    rows = []
    for one in found:
        incidents, status = one["incidents"], one["status"]
        since = usage_screen.lasted(incidents, now)
        first = incidents[0]["title"] if incidents else ""
        detail = (one["why"] if status == "unknown" else
                  f"{first or 'an incident with no title'}{f' · for {since}' if since else ''}"
                  + (f" · +{len(incidents) - 1} more" if len(incidents) > 1 else "") if incidents else
                  ", ".join(f"{part['name']} {part['status'].replace('_', ' ')}" for part in one["components"][:3])
                  or (one["indicator"] if status != "ok" else "") or "no open incident")
        rows.append({
            "id": f"stat:{one['name']}", "state": usage_screen.SERVICE_STATE[status], "type": "service status",
            "what": f"{one['name']}: {status}", "detail": detail, "kind": f"Service status · {one['kind'] or 'unknown kind'}",
            "facts": {"Service": one["name"], "Status": status, "Kind": one["kind"] or "—",
                      "Providers": ", ".join(one["providers"]) or "none", "Indicator": one["indicator"] or "—",
                      **({"Lasted": since} if since else {})},
            "read_at": one["read_at"], "url": one["url"], "cli": STATUS_CLI,
            "list_head": "Open incidents", "list": [{"text": incident["title"] or "untitled incident", "href": incident["url"]}
                                                    if incident["url"] else incident["title"] or "untitled incident"
                                                    for incident in incidents],
            # Ignore holds until the status, an incident or a component changes (sd:5470, pack sd:9380); a new read time
            # is not a new problem. An incident is its title, link and start, so a later outage with the same title is a
            # new problem; a component is its name and status, so a later component failure is one too.
            "problem": [status, *(field for incident in incidents
                                  for field in (incident["title"], incident["url"], incident["started_at"] or "")),
                        *(field for part in one["components"] for field in (part["name"], part["status"]))],
        })
    return rows, extra


DOCTOR_CLI = "local-machine-setup/machine-setup.sh doctor"
DOCTOR_STATE = {"FAIL": "warning", "WARN": "caution"}


def _doctor_rows(found: dict) -> tuple[list[dict], dict]:
    """A row per WARN and FAIL line, then one row with the lines that pass, are skipped or are context."""
    rows = []
    for check in found["checks"]:
        if check["mark"] in DOCTOR_STATE:
            text = check["text"]
            rows.append({"id": "doc:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
                         "state": DOCTOR_STATE[check["mark"]], "type": "doctor check", "what": text.split(" — ")[0],
                         "detail": " ".join([text, *check["more"]]), "kind": f"Doctor · {check['mark']}",
                         "facts": {"Line": check["mark"]}, "cli": DOCTOR_CLI})
    passed = [check for check in found["checks"] if check["mark"] == "ok"]
    rest = [check for check in found["checks"] if check["mark"] in ("--", "SKIP")]
    rows.append({"id": "doc:ok", "state": "ok", "type": "doctor check",
                 "what": f"{len(passed)} doctor {_plural(len(passed), 'check passes', 'checks pass')}",
                 "detail": f"{len(rest)} not checked here · {found['fails']} hard {_plural(found['fails'], 'failure', 'failures')}",
                 "kind": "Doctor", "facts": {"Passed": str(len(passed)), "Not checked": str(len(rest))},
                 "list": [f"{check['mark']} · {check['text']}" for check in passed + rest] + found["other"],
                 "cli": DOCTOR_CLI})
    return rows, {}


#: launchd's states as Health's: down is caution, a non-zero exit or a signal for cause is warning (sd:3707).
SERVICE_STATE = {"running": "ok", "idle": "caution", "unloaded": "caution", "failed": "warning", "unknown": "unknown"}


def read_services(connection: sqlite3.Connection, backend=None) -> dict:
    """`sd services list`'s inventory; the seam a test fills, since a real read runs launchctl."""
    from sd_db import services

    try:
        return services.inventory(connection, backend=backend)
    except subprocess.SubprocessError as failure:
        raise RuntimeError(f"launchctl gave no answer: {failure}") from failure


#: The user LaunchAgents Services lists only when their last run failed, by `sd_db.services` category.
RUN_ONCE = {"startup": "Run-once", "scheduled": "Scheduled"}


def _service_rows(inventory, *, served_commit=None, here: bool = True) -> list[dict]:
    """One row per long-running user LaunchAgent, and per run-once or scheduled one whose last run failed.

    The dashboard's row also says whether it serves the checkout. A row of another machine (`here` false, sd:9757)
    has no control and no dashboard check: Restart and Start act on this machine's launchctl.
    """
    if not isinstance(inventory, dict) or not isinstance(inventory.get("services"), list):
        raise ValueError("the service inventory is incomplete")
    rows = []
    for entry in inventory["services"]:
        if entry.get("scope") != "user":
            continue
        label, state = entry["label"], entry["state"]
        facts = {"Label": label, "Domain": entry["domain"], "State": state,
                 "PID": "—" if entry["pid"] is None else str(entry["pid"]),
                 "Last exit": "—" if entry["last_exit"] is None else str(entry["last_exit"])}
        if entry.get("category") in RUN_ONCE and state == "failed":
            # sd:5882: a run-once or scheduled agent shows only when its last run failed, and has no
            # controls: `sd services` starts and restarts long-running services only.
            rows.append({"id": f"svc:{label}", "state": "warning", "type": "launch agent",
                         "revision": entry["revision"], "what": f"{entry['name']} · failed",
                         "detail": " · ".join(part for part in (
                             f"last exit {entry['last_exit']}" if entry["last_exit"] is not None else "",
                             entry.get("reason") or "") if part) or "last run failed",
                         "kind": f"{RUN_ONCE[entry['category']]} user LaunchAgent", "facts": facts})
            continue
        if entry.get("category") != "service":
            continue
        row = {"id": f"svc:{label}", "state": SERVICE_STATE.get(state, "unknown"), "type": "service",
               "revision": entry["revision"], "what": f"{entry['name']} · {state}",
               "detail": " · ".join(part for part in (
                   f"PID {entry['pid']}" if entry["pid"] is not None else "",
                   f"last exit {entry['last_exit']}" if entry["last_exit"] is not None else "",
                   entry.get("reason") or "") if part) or "user LaunchAgent",
               "kind": "Service · user LaunchAgent", "facts": facts,
               "can": {action: entry["capabilities"][action] for action in ("start", "restart")},
               "note": (entry.get("last_request") or {}).get("message") or ""}
        if not here:
            del row["can"]
            row["note"] = "Read on another machine: start or restart it there with sd services."
        elif label == runtime.LABEL:
            row["type"], row["kind"] = "dashboard service", "Service · this dashboard"
            row["kickstart"] = f"launchctl kickstart -k gui/{os.getuid()}/{label}"
            facts["Served commit"] = served_commit or "unknown"
            try:
                behind = runtime.served_behind(served_commit)
            except runtime.RuntimeRefused as failure:
                row.update(state="unknown", detail=f"the checkout's commit was not read: {failure}")
            else:
                if behind and state == "running":
                    row.update(state="caution", what=f"{entry['name']} · stale", detail=behind)
            row["note"] = ("The dashboard's own service is protected: restarting it ends the request that asks. "
                           "Run the kickstart line in a terminal.")
        rows.append(row)
    if not rows:
        rows.append({"id": "svc:none", "state": "unknown", "type": "check", "what": "No long-running user service found",
                     "detail": "no user LaunchAgent declares KeepAlive, Sockets or MachServices", "kind": "Services",
                     "facts": {}, "cli": "sd services list"})
    return rows


#: The whole document's budget. The readers run at once, each inside its own budget, the fleet's 12 seconds the
#: longest, so this is that figure and a margin; a reader still running at it is its area's error.
PAGE_SECONDS = 13.0
#: The areas whose reader walks subprocesses, each on its own scan thread; Protection, Dependencies, Security and
#: Credentials read only the database, on this thread.
POOLED = ("disk", "wt", "br", "ports", "ctr", "doc")
#: How long a doctor answer stands before a page read starts another run, and how long a page waits on a run in flight
#: when no answer is kept.
DOCTOR_FRESH_SECONDS = 600
DOCTOR_WAIT_SECONDS = 2.0


class _Scans:
    """At most one scan per pooled area at a time, and each area's last answer.

    The page made a thread pool per request and left a reader past its budget
    running: Disk's `isdir()` and config reads sit outside any subprocess
    timeout, so a stalled mount held its thread for good, each refresh added
    one, and the interpreter waited for all of them at exit (sd:2520 review).
    Now a request joins an area's scan that is still running instead of
    starting a second, so a blocked reader holds one thread however often the
    page is read. The thread is a daemon, so exit does not wait on it.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.running: dict[str, tuple[str, Future]] = {}
        self.last: dict[str, tuple[str, object]] = {}
        #: Each area as a read last settled it, before the snooze split, for `summary`.
        self.areas: dict[str, dict] = {}
        #: Each area's newest failed scan, `(started, reason)`, until a scan succeeds: `kept` marks what it serves.
        self.failed: dict[str, tuple[str, str]] = {}

    def start(self, key: str, read, *, now: str, fresh: float | None = None) -> tuple[str, Future, bool]:
        """The area's scan, the time it started and whether it was already running.

        With `fresh`, an answer younger than that many seconds is handed back as a finished scan and nothing runs; a
        failed run drops the kept answer, so the next read runs again rather than show the answer before the failure.
        """
        with self.lock:
            running, last, failed = self.running, self.last, self.failed
            held = running.get(key)
            if held and not held[1].done():
                return held[0], held[1], True
            kept = last.get(key)
            if fresh is not None and kept and 0 <= (_when(now) - _when(kept[0])).total_seconds() < fresh:
                future = Future()
                future.set_result(kept[1])
                return kept[0], future, False
            future: Future = Future()
            running[key] = (now, future)

        def scan():
            try:
                found = read()
            except BaseException as failure:  # noqa: BLE001 - the request that reads the future settles it
                with self.lock:
                    failed[key] = (now, str(failure) or repr(failure))
                    if fresh is not None:
                        last.pop(key, None)
                future.set_exception(failure)
            else:
                with self.lock:
                    failed.pop(key, None)
                    last[key] = (now, found)
                future.set_result(found)
        threading.Thread(target=scan, name=f"health-{key}", daemon=True).start()
        return now, future, False

    def keep(self, area: dict) -> None:
        """Keep `area` as settled; an area that failed or was not re-read keeps it too, so its lamp says so."""
        with self.lock:
            self.areas[area["id"]] = {**area, "rows": list(area["rows"]), "extra": dict(area["extra"])}

    def kept(self, key: str) -> dict | None:
        """The area's newest answer: what a read settled, or a scan that finished after it, settled now."""
        with self.lock:
            area, last, failed = self.areas.get(key), self.last.get(key), self.failed.get(key)
        # A scan that started after the kept read, or as it did when that read was cut short, is newer; a failed scan
        # keeps no answer, so a failure is never replaced by the success before it.
        if last and (area is None or last[0] > (area["at"] or "")
                     or ((area["error"] or area["stale"]) and last[0] >= (area["at"] or ""))):
            name, source, missing = next((name, source, missing) for one, name, source, missing in AREAS if one == key)
            area = _area(key, name, source, missing, at=last[0])
            _settle(area, name, lambda: last[1])
        # A scan that failed after this answer was read: the rows stay, marked with the failure and their age, so the
        # lamp is not green; with no answer the failure is the area's error.
        if failed and (area is None or (not area["error"] and failed[0] > (area["at"] or ""))):
            why = f"the refresh at {failed[0]} failed: {failed[1]}"
            if area is None:
                name, source, missing = next((name, source, missing) for one, name, source, missing in AREAS if one == key)
                return {**_area(key, name, source, missing, at=failed[0]), "error": why}
            return {**area, "stale": "; ".join(filter(None, (area["stale"], f"{why}; these rows are the read of {area['at']}")))}
        return area

    def clear(self) -> None:
        """Forget every scan and answer; a scan still running writes only into what it started with."""
        with self.lock:
            self.running, self.last, self.areas, self.failed = {}, {}, {}, {}


_SCANS = _Scans()


class _Once:
    """`read` run once for one request's readers: every later call shares its answer or its failure.

    Ports and Containers read one `collect_ports` (sd:5840), so one `docker ps` answers both areas whichever scan
    calls first. A scan a later request starts gets that request's own read.
    """

    def __init__(self, read):
        self.read, self.lock, self.answer = read, threading.Lock(), None

    def __call__(self):
        with self.lock:
            if self.answer is None:
                self.answer = Future()
                mine = True
            else:
                mine = False
        if mine:
            try:
                self.answer.set_result(self.read())
            except BaseException as failure:  # noqa: BLE001 - each caller gets the failure from the future
                self.answer.set_exception(failure)
        return self.answer.result()


def _settle(area: dict, name: str, read) -> None:
    """One reader's answer into its area: rows and extra, or the reason it has none."""
    try:
        found = read()
        area["rows"], extra = found if isinstance(found, tuple) else (found, {})
        area["extra"] = dict(extra)  # a kept answer is settled again; popping `at` must not change it
        if "at" in area["extra"]:
            area["at"] = area["extra"].pop("at")
        if area["extra"].get("stale"):
            area["stale"] = "; ".join(filter(None, (area["stale"], area["extra"].pop("stale"))))
    except reads.OverBudget as refused:
        area["error"] = f"{refused} and was stopped rather than waited on"
    except ports_screen.OverBudget as refused:
        area["error"] = f"the port inventory exceeded its collection budget and was stopped rather than waited on: {refused}"
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, SdDbError, sqlite3.Error) as failure:
        area["error"] = str(failure) or f"the {name} reader failed without a reason"


def _readers(connection: sqlite3.Connection, *, now: str, fleet=None, ports=None, protection=None, disk=None,
             branches=None, credentials=None, doctor=None, services=None, served_commit=None) -> dict:
    """Each area's reader, by id; `document` says what each seam is."""
    try:
        known: list[str] | Exception = [row["path"] for row in repos.registered(connection)]
    except (SdDbError, sqlite3.Error) as failure:
        known = failure
    try:
        # The lane entries the merged worktrees are read against, on this thread for the same reason (sd:5105).
        heads: dict | Exception = health_collectors.merged_heads(connection) if branches is None else {}
    except (SdDbError, sqlite3.Error) as failure:
        heads = failure
    # The storage folders' row, on this thread too (sd:5109); a failed read is the answer's `problem`, never a raise.
    storage = health_collectors.storage_list(connection) if disk is None else None

    def registered() -> list[str]:
        if isinstance(known, Exception):
            raise known
        return known

    def merged() -> dict:
        if isinstance(heads, Exception):
            raise heads
        return heads

    read_fleet = fleet or fleet_module.collect
    collect_ports = _Once(ports or ports_screen._collect)
    read_protection = protection or protection_module.rows
    read_creds = credentials or read_credentials
    scan_disk = disk or (lambda connection: health_collectors.disk_scan(connection, listed=storage, repo_paths=registered()))
    scan_branches = branches or (lambda connection: health_collectors.branch_scan(
        connection, repo_paths=registered(), heads=merged()))
    read_doctor = doctor or (lambda: health_collectors.doctor_scan())
    return {
        "disk": lambda: _disk_rows(scan_disk(connection)),
        "wt": lambda: _fleet_rows(read_fleet("sessions")),
        "br": lambda: _branch_rows(scan_branches(connection)),
        "ports": lambda: _port_rows(collect_ports()),
        "ctr": lambda: _container_rows(collect_ports()),
        "prot": lambda: _protection_rows(read_protection(connection)),
        "dep": lambda: _dependency_rows(read_protection(connection), now=now),
        "sec": lambda: _security_rows(read_protection(connection), now=now),
        "cred": lambda: _credential_rows(read_creds(connection), now=now),
        "svc": lambda: _service_rows(read_services(connection, services), served_commit=served_commit),
        "mir": lambda: _with_row(_mirror_rows(read_mirror(connection)),
                                 _brief_copies_row(lambda: read_brief_copies(connection), now=now)),
        "doc": lambda: _doctor_rows(read_doctor()),
        "aws": lambda: _aws_rows(read_creds(connection), now=now),
        "stat": lambda: _status_rows(usage_screen.services(connection, now=now), now=now,
                                     removed=usage_screen.ever_read(connection, usage_screen.STATUS_PREFIX)),
    }


def _area(key: str, name: str, source: str, missing, *, at) -> dict:
    return {"id": key, "name": name, "view": VIEW_OF[key], "source": source, "read": True, "error": "", "stale": "",
            "rows": [], "missing": list(missing), "at": at, "extra": {}}


def _tag_managed(rows: list[dict], flags: dict[str, bool]) -> None:
    """Mark each row of a registered repository with its `managed` flag, for the page's Repos choice; other rows stay bare."""
    for row in rows:
        path = row.get("repo_path") or (row.get("facts") or {}).get("Repository")
        if path and os.path.expanduser(path) in flags:
            row["managed"] = flags[os.path.expanduser(path)]


def document(connection: sqlite3.Connection, *, now: str, fleet=None, ports=None, protection=None,
             disk=None, branches=None, credentials=None, doctor=None, services=None, served_commit=None,
             view: str | None = None, release=None) -> dict:
    """Every area of the design, each with its rows, the reason it was not read, and what no reader covers.

    `fleet` is `fleet.collect`'s shape, `area -> document`, `ports`
    `collect_ports`' (no argument) and
    `protection` `protection.rows`', and `disk` and `branches`
    `health_collectors.disk_scan`'s and `branch_scan`'s, and `credentials`
    `read_credentials`', and `services` the service backend `read_services` hands
    `sd_db.services` (`served_commit` is the commit this process started on); each is a seam a test fills. Each reader
    is guarded on its own, so one failure is its area's `error` and the
    others still answer. A reader returns its rows, or its rows and what the
    page draws above them (`extra`). `view`, one of `VIEWS`, reads only that
    view's areas (`part`); none reads them all, for `/api/health`.

    **The walkers run at once.** Each waits on subprocesses inside its own
    budget, so in series the budgets add up (Disk's and Branches' 8, the
    fleet's 12) and at once the slowest decides.
    A sqlite connection belongs to the thread that opened it, so the
    registry the default walkers need is read here first and handed to them
    as paths; Protection, which reads only the database, stays on this
    thread. `PAGE_SECONDS` bounds the whole document: a reader still running
    at it is left to stop at its own budget, not waited on. While it runs, a
    later request starts no second scan of that area (`_Scans`); the area
    shows its last answer marked `stale`, or is its error if it has none.
    A row the operator snoozed (sd:1896) is in its area's `snoozed`, with its
    `until`, and not in `rows`; `snooze_error` says why none could be read.
    Each area as settled, before the snooze split, is kept for `summary`.

    `release`, the server's, closes `connection` and gives its read slot back. The store areas are read first, then
    it runs, then the walkers are waited on: a view waiting on Disk's 8 s held a slot all that time, and Health's reads
    filled the slots under load (sd:7898).
    """
    keys = set(VIEW_AREAS[view]) if view else {key for key, *_ in AREAS}
    readers = _readers(connection, now=now, fleet=fleet, ports=ports, protection=protection, disk=disk,
                       branches=branches, credentials=credentials, doctor=doctor, services=services,
                       served_commit=served_commit)
    snoozes, snooze_error = snooze.held(connection, now=now)
    try:
        flags = {os.path.expanduser(str(row["path"])): bool(row["managed"]) for row in repos.registered(connection)}
    except (SdDbError, sqlite3.Error):
        flags = {}  # no flag: every row shows, as under All
    running = {key: _SCANS.start(key, readers[key], now=now, fresh=DOCTOR_FRESH_SECONDS if key == "doc" else None)
               for key in POOLED if key in keys}
    settled = {}
    for key, name, source, missing in AREAS:
        if key in keys and key not in running:
            settled[key] = area = _area(key, name, source, missing, at=now)
            _settle(area, name, readers[key])
    if release is not None:
        release()
    stop = time.monotonic() + PAGE_SECONDS
    for key, name, source, missing in AREAS:
        if key not in running:
            continue
        settled[key] = area = _area(key, name, source, missing, at=now)
        since, scan, joined = running[key]
        # The doctor is last in AREAS: the page has waited on the others, and gives it only a short wait more. A run
        # takes about 10 s, so with an answer kept the page shows that at once, marked stale (sd:5064).
        wait = max(0.0, stop - time.monotonic())
        if key == "doc":
            wait = 0.0 if _SCANS.last.get(key) else min(wait, DOCTOR_WAIT_SECONDS)
        try:
            scan.result(timeout=wait)
        except FutureTimeout:
            why = (f"machine-setup.sh doctor started at {since} and is still running (its deadline is "
                   f"{health_collectors.DOCTOR_SECONDS:g} seconds); read Health again for its answer" if key == "doc" else
                   f"the {name} reader was still running at the page's budget of {PAGE_SECONDS:g} "
                   "seconds and was left rather than waited on")
            if joined:
                why += f"; that scan started at {since}, and no second one starts while it runs"
            kept = _SCANS.last.get(key)
            if kept:
                area["stale"], area["at"] = f"{why}; these rows are the read of {kept[0]}", kept[0]
                _settle(area, name, lambda *, kept=kept: kept[1])
            else:
                area["error"], area["at"] = why, since
        except BaseException:  # noqa: BLE001 - the reader's own error is settled below
            pass
        if not area["error"] and not area["stale"]:
            area["at"] = since
            _settle(area, name, scan.result)
    areas = []
    for key, *_ in AREAS:
        if key not in settled:
            continue
        area = settled[key]
        _SCANS.keep(area)
        _tag_managed(area["rows"], flags)
        area["rows"], area["snoozed"] = snooze.split("health", area["rows"], snoozes)
        areas.append(area)
    found = {"read": now, "areas": areas, "snooze_error": snooze_error, "attention": _attention(areas)}
    return {**found, "view": view} if view else found


MACHINE_PREFIX = health_collectors.MACHINE_PREFIX
#: The areas a stored machine reading carries (sd:9757); the Machine view's others read only this machine.
MACHINE_AREAS = ("disk", "svc")


def this_host() -> str:
    return health_collectors.this_host()


def machines(connection: sqlite3.Connection) -> dict:
    """The Machine view's chooser: this machine, the hub, first, then every other host with a stored reading."""
    here = this_host()
    try:
        found = usage.newest_heartbeats(connection, MACHINE_PREFIX)
    except sqlite3.Error as failure:  # the hub's own rows still read; the chooser says why it lists no other
        return {"hub": here, "hosts": [here], "error": f"the other machines' readings were not read: {failure}"}
    return {"hub": here, "hosts": [here, *sorted(set(found) - {here})]}


def read_machine(connection: sqlite3.Connection, host: str) -> tuple[str, dict] | None:
    """`host`'s newest reading as `(timestamp, body)`, or None; a body that does not parse is a ValueError."""
    found = usage.newest_heartbeats(connection, MACHINE_PREFIX).get(host)
    if found is None:
        return None
    if not isinstance(found[1], dict) or not isinstance(found[1].get("disk"), dict):
        raise ValueError(f"{host}'s reading has no Disk part")
    return found


def _machine_part(connection: sqlite3.Connection, host: str, *, now: str) -> dict:
    """The Machine view for another host (sd:9757): its stored Disk and Services, read at the reading's time.

    A row's id carries the host, so a snooze of a hub row never hides a satellite's. A reading older than its interval
    is `stale`, so its lamp is never green; a host with no reading is each area's error.
    """
    snoozes, snooze_error = snooze.held(connection, now=now)
    try:
        found, failed = read_machine(connection, host), ""
    except (ValueError, sqlite3.Error) as failure:
        found, failed = None, f"{host}'s reading was not read: {failure}"
    if found is None and not failed:
        failed = (f"no reading from {host}: its `sd services list --record` job has not written one "
                  f"(heartbeat {MACHINE_PREFIX}{host})")
    at, body = found if found else (None, {})
    stale = ""
    if found:
        interval = int(body.get("interval_seconds") or health_collectors.MACHINE_INTERVAL)
        if (_when(now) - _when(at)).total_seconds() > interval:
            stale = f"{host}'s reading of {at} is older than its interval of {interval // 60} minutes"
    readers = {"disk": lambda: _disk_rows(body["disk"]),
               "svc": lambda: _service_rows({"services": body.get("services")}, here=False)}
    areas = []
    for key, name, _source, missing in AREAS:
        if key not in MACHINE_AREAS:
            continue
        area = {**_area(key, name, f"{host} · sd services list --record → state heartbeat {MACHINE_PREFIX}{host}",
                        missing, at=at), "error": failed, "stale": stale}
        if not failed:
            _settle(area, name, readers[key])
        rows = [row for row in area["rows"] if not row["id"].startswith("build:")]
        for row in rows:
            row["id"] = f"{host}:{row['id']}"
            row["host"] = host
        area["rows"], area["snoozed"] = snooze.split("health", rows, snoozes)
        areas.append(area)
    # Its own lamps (review round 6): the page shows them, and counts the Machine tab, in place of the hub's.
    lamps = [{**lamp, "value": [*lamp["value"], {"b": "", "t": f"on {host}"}]} for lamp in map(_lamp, areas)]
    return {"read": now, "view": "machine", "host": host, "machines": machines(connection), "areas": areas,
            "lamps": lamps, "snooze_error": snooze_error, "attention": _attention(areas)}


def part(connection: sqlite3.Connection, view: str, *, now: str, host: str | None = None, **seams) -> dict:
    """One view's areas (`VIEWS`), read alone, for `/api/health/<view>`.

    A failure the areas' own guards do not catch stays in this view: each of its areas is unknown with the reason,
    and no other view's read is touched, since each view is its own request. `host`, for the Machine view, names
    another machine whose stored reading answers instead (sd:9757); the Machine view lists the hosts to choose.
    """
    try:
        if view == "machine" and host and host != this_host():
            return _machine_part(connection, host, now=now)
        if view == "machine":
            found = machines(connection)  # before `document`, whose `release` closes the connection
            return {**document(connection, now=now, view=view, **seams), "machines": found}
        return document(connection, now=now, view=view, **seams)
    except Exception as failure:  # noqa: BLE001 - one view's failure must not take the page with it
        why = f"the {VIEW_NAMES[view]} read failed: {failure!r}"
        areas = [{**_area(key, name, source, missing, at=None), "error": why, "snoozed": []}
                 for key, name, source, missing in AREAS if key in VIEW_AREAS[view]]
        return {"read": now, "view": view, "areas": areas, "snooze_error": "", "attention": _attention(areas)}


#: How long a walker's answer stands for `summary`, which starts a scan in the background past it.
SUMMARY_FRESH_SECONDS = 60.0
#: Areas `summary` reads afresh: they read only the database. Every other area shows its last answer.
CHEAP = ("prot", "dep", "sec", "cred", "mir", "aws", "stat")
RANK = {"warning": 0, "caution": 1, "unknown": 2, "queued": 3, "ok": 4}


def _lamp(area: dict) -> dict:
    """One area's lamp: its state, and the short value the strip shows under its name, as phrases (`b` is bold)."""
    rows = area["rows"]
    if not area["read"] or area["error"]:
        return {"id": area["id"], "name": area["name"], "view": area["view"], "state": "unknown", "at": area["at"],
                "error": area["error"], "stale": area["stale"], "n": None, "value": [{"b": "", "t": "not read"}]}
    loudest = min((row["state"] for row in rows), key=lambda state: RANK.get(state, 2), default="ok")
    state = "ok" if loudest == "queued" else loudest if loudest in RANK else "unknown"
    # Rows not re-read cannot say all is well: a stale area's quiet lamp is unknown, never green.
    if area["stale"] and state == "ok":
        state = "unknown"

    def total(key: str) -> int:
        return sum(int(row.get("facts", {}).get(key) or 0) for row in rows
                   if str(row.get("facts", {}).get(key) or "0").isdigit())
    key, extra = area["id"], area["extra"]
    if key == "wt":
        value = [{"b": str(total("Registered")), "t": "dir gone"}]
    elif key == "disk":
        fullest = max(extra.get("volumes") or [], key=lambda volume: volume["capacity"], default=None)
        value = ([{"b": f"{fullest['capacity']}%", "t": "fullest"}, {"b": "", "t": fullest["name"]}] if fullest
                 else [{"b": "", "t": "no volume"}])
    elif key == "br":
        value = [{"b": str(total("Merged")), "t": "merged, not deleted"}]
    elif key in ("dep", "sec"):
        unread = [row for row in rows if row["state"] == "unknown"]
        if rows and len(unread) == len(rows):
            value = [{"b": "", "t": "not read"}]
        else:
            more = "+" if any(str(row.get("facts", {}).get("Shown") or "").endswith("+") for row in rows) else ""
            value = [{"b": f"{total('Open')}{more}", "t": "open alerts"}] + [{"b": "", "t": "not all read"}] * bool(unread)
    elif key == "ports":
        counts = extra.get("counts") or {}
        value = [{"b": str(counts.get("unknown", 0)), "t": "unknown"}, {"b": "", "t": f"{counts.get('listening', 0)} listening"}]
    elif key == "ctr":
        value = [{"b": str(extra.get("running", 0)), "t": "running"}, {"b": "", "t": f"{extra.get('holding', 0)} mount a checkout"}]
    elif key == "prot":
        counts = extra.get("counts") or {}
        value = [{"b": str(counts.get("unprotected", 0)), "t": "unprotected"}, {"b": "", "t": f"{counts.get('unknown', 0)} unknown"}]
    else:
        value = [{"b": str(sum(row["state"] in ("warning", "caution") for row in rows)), "t": "need you"}]
    return {"id": key, "name": area["name"], "view": area["view"], "state": state, "at": area["at"], "error": "",
            "stale": area["stale"], "n": sum(row["state"] in ("warning", "caution", "unknown") for row in rows), "value": value}


def summary(connection: sqlite3.Connection, *, now: str, **seams) -> dict:
    """Every area's lamp and the rail badge, without waiting on a walker: `/api/health/summary`, Health's count.

    The database-only areas (`CHEAP`) are read now, each under its own guard. Every other area shows the last answer
    a view read, or a scan, left (`_Scans.areas`, `_Scans.last`), with its time; an area no read has answered since the
    server started is unknown and says which view reads it. A walker with no answer younger than
    `SUMMARY_FRESH_SECONDS` (the doctor's ten minutes) gets a scan started in the background, joined by the view that
    reads it next, so the next summary has it; this read never waits on one.
    """
    readers = _readers(connection, now=now, **seams)
    for key in POOLED:
        try:
            _SCANS.start(key, readers[key], now=now, fresh=DOCTOR_FRESH_SECONDS if key == "doc" else SUMMARY_FRESH_SECONDS)
        except Exception as failure:  # noqa: BLE001 - a scan that could not start leaves the area's last answer
            print(f"health summary: the {key} scan did not start: {failure!r}", file=sys.stderr)
    snoozes, snooze_error = snooze.held(connection, now=now)
    areas = []
    for key, name, source, missing in AREAS:
        area = _area(key, name, source, missing, at=now)
        if key in CHEAP:
            try:
                _settle(area, name, readers[key])
            except Exception as failure:  # noqa: BLE001 - one area's failure stays in its lamp
                area["error"] = str(failure) or f"the {name} reader failed without a reason"
            _SCANS.keep(area)
        else:
            kept = _SCANS.kept(key)
            if kept is None:
                running = _SCANS.running.get(key)
                area["at"] = None
                area["error"] = (f"being read since {running[0]}; read Health again for its answer"
                                 if running and not running[1].done() else
                                 f"not read since the dashboard started; the {VIEW_NAMES[VIEW_OF[key]]} view reads it")
            else:
                area = {**kept, "rows": list(kept["rows"]), "extra": dict(kept["extra"])}
        area["rows"], area["snoozed"] = snooze.split("health", area["rows"], snoozes)
        areas.append(area)
    return {"read": now, "views": [{"id": view, "name": name, "areas": list(keys)} for view, name, keys in VIEWS],
            "lamps": [_lamp(area) for area in areas], "snooze_error": snooze_error, "attention": _attention(areas)}


def _attention(areas) -> dict:
    """The rail badge (sd:3446): the loudest state among the rows every area raised, less the snoozed ones."""
    from .v2.pages import attention

    return attention((row["state"] for area in areas for row in area["rows"]),
                     {"warning": "findings want you", "caution": "findings to watch"})
