"""File-derived skill catalog and database requests for isolated runner work."""

import hashlib
import json
import re
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import pack, paths, registry, runner, workflow
from .database import transaction
from .sources.frontmatter import split
from .writes import add_note, create_item, resolve_note, set_item_fields, start_trial

TRIAL_DAYS = 30
MAX_TEXT = 2 * 1024 * 1024


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _json_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TEXT:
        raise workflow.WorkflowError(f"missing, linked or oversized catalog file: {path}")
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as error:
        raise workflow.WorkflowError(f"cannot read catalog file {path}: {error}") from error


def location(*, root=None, home=None):
    home = Path(home) if home else Path.home()
    receipt_path = pack.receipt_path(home)
    receipt = _json_file(receipt_path) if receipt_path.exists() else {}
    if not isinstance(receipt, dict):
        raise workflow.WorkflowError("installed skill receipt must be an object")
    named = root or receipt.get("checkout")
    if not isinstance(named, (str, Path)) or not named or not Path(named).is_absolute():
        raise workflow.WorkflowError("the installed pack receipt does not name a checkout")
    return Path(named).resolve(), receipt


def _snapshot(root, directory):
    files, total = {}, 0
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > MAX_TEXT:
            raise workflow.WorkflowError("skill contains a linked or oversized file")
        total += path.stat().st_size
        if len(files) >= 200 or total > 8 * MAX_TEXT:
            raise workflow.WorkflowError("skill exceeds its file count or combined size limit")
        files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _description(frontmatter):
    """Read a display scalar without parsing unrelated, possibly nested metadata.

    Literal blocks retain line breaks; folded blocks join ordinary wrapped lines.
    Display text omits leading/trailing blank lines, regardless of YAML chomping.
    """
    missing = "No description declared."
    match = re.search(r"(?m)^description:[ \t]*(.*)$", frontmatter)
    if not match:
        return missing
    value = match[1].strip()
    header = re.fullmatch(r"([|>])([1-9][+-]?|[+-][1-9]?)?(?:[ \t]+#.*)?", value)
    if not header:
        return value.strip('"\'') or missing
    block = []
    for line in frontmatter[match.end():].splitlines():
        if line.strip() and not line.startswith(" "):
            break
        block.append(line)
    content = "\n".join(block).strip("\n")
    if not content.strip():
        return missing
    indentation = next((int(char) for char in header[2] or "" if char.isdigit()), None)
    if indentation is None:
        first = next(line for line in block if line.strip())
        indentation = len(first) - len(first.lstrip(" "))
    lines = []
    for line in content.splitlines():
        if line.strip() and not line.startswith(" " * indentation):
            raise workflow.WorkflowError("skill description block has inconsistent indentation")
        lines.append(line[indentation:] if line.strip() else "")
    if header[1] == "|":
        return "\n".join(lines).strip("\n")
    folded, previous, blanks = [], None, 0
    for line in lines:
        if not line:
            blanks += 1
            continue
        if previous is not None:
            if previous.startswith(" ") or line.startswith(" "):
                folded.append("\n" * (blanks + 1))
            else:
                folded.append("\n" * blanks if blanks else " ")
        folded.append(line)
        previous, blanks = line, 0
    return "".join(folded)


def _scan(*, root=None, home=None):
    """The catalog's file-system half: paths.json, the installed copies, and
    each skill's files, description and use. It reads no database row, so a
    write runs it before its transaction and holds no lock for the scan
    (pack sd:7853)."""
    root, receipt = location(root=root, home=home)
    definitions = _json_file(root / "skills/paths.json")
    paths = definitions.get("paths") if isinstance(definitions, dict) else None
    if not isinstance(paths, dict) or any(not isinstance(value, dict) or not isinstance(value.get("skills"), list) for value in paths.values()):
        raise workflow.WorkflowError("skills/paths.json has no valid path inventory")
    installed = {}
    for entry in receipt.get("owned", []):
        if not isinstance(entry, dict) or not str(entry.get("kind", "")).startswith("skill:"):
            continue
        target = Path(str(entry.get("path", "")))
        name = target.parent.name if target.name == "SKILL.md" else target.stem
        actual = "missing"
        if target.is_file() and target.stat().st_size <= MAX_TEXT:
            actual = "current" if hashlib.sha256(target.read_bytes()).hexdigest() == entry.get("sha256") else "changed"
        installed.setdefault(name, []).append({"surface": entry["kind"].split(":", 1)[1], "state": actual})
    skills, names = [], set()
    for source in ("skills", "contrib"):
        directory = root / source
        for path in sorted(directory.glob("*/SKILL.md")):
            name = path.parent.name
            if name in names or not re.fullmatch(r"sd-[a-z0-9-]+", name):
                raise workflow.WorkflowError(f"duplicate or invalid skill name: {name}")
            names.add(name)
            files = _snapshot(root, path.parent)
            text = path.read_text()
            frontmatter, body = split(text)
            description = _description(frontmatter)
            when = re.search(r"(?is)## (?:When to use|Use when|When invoked)\s*\n(.+?)(?=\n## |\Z)", body)
            usage = " ".join((when[1] if when else description).split())[:1500]
            skills.append({"name": name, "description": description, "when": usage, "path": str(path.relative_to(root)),
                           "installed": installed.get(name, []), "files": files})
    return {"root": root, "paths": paths, "skills": skills}


def _composed(connection, scanned, now=None):
    """The catalog from a `_scan` and the database's trial and use rows."""
    paths = scanned["paths"]
    trials = {row["skill"]: dict(row) for row in connection.execute("SELECT * FROM trial")}
    use = {}
    for row in connection.execute("SELECT skill,surface,mode,count(*) AS count FROM skill_use GROUP BY skill,surface,mode"):
        use.setdefault(row["skill"], []).append({"surface": row["surface"] or "unknown", "mode": row["mode"] or "unknown", "count": row["count"]})
    # Each skill's newest use, so a reader can list the skills nobody used lately (pack sd:5992).
    last = dict(connection.execute("SELECT skill, max(timestamp) FROM skill_use GROUP BY skill").fetchall())
    moment = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    skills = []
    for found in scanned["skills"]:
        name = found["name"]
        trial = trials.get(name)
        membership = [key for key, value in paths.items() if name in value["skills"]]
        status = "path" if membership else "trial" if trial and trial["expires"] > moment else "contrib"
        value = {"name": name, "description": found["description"], "when": found["when"], "path": found["path"],
                 "paths": membership, "status": status, "trial": trial, "uses": use.get(name, []),
                 "last": last.get(name),
                 "installed": found["installed"], "source_sha256": _hash(found["files"]), "files": dict(found["files"])}
        value["revision"] = _hash(value)
        skills.append(value)
    return {"root": str(scanned["root"]), "paths": paths, "skills": skills, "observed_at": moment}


def catalog(connection, *, root=None, home=None, now=None):
    return _composed(connection, _scan(root=root, home=home), now)


def use_rows(connection):
    """Every `skill_use` row's time, skill and surface, oldest first: what a page counts per week and per surface."""
    return [dict(row) for row in connection.execute("SELECT timestamp, skill, surface FROM skill_use ORDER BY timestamp")]


def _selected(connection, scanned, name, expected_revision=None):
    """`name`'s catalog entry, from a scan taken before the caller's transaction (pack sd:7853)."""
    inventory = _composed(connection, scanned)
    matches = [skill for skill in inventory["skills"] if skill["name"] == name]
    if not matches:
        raise workflow.WorkflowError(f"no catalog skill {name}; available: {', '.join(skill['name'] for skill in inventory['skills'])}")
    skill = matches[0]
    if expected_revision is not None and expected_revision != skill["revision"]:
        raise workflow.StaleItem("skill, trial or usage changed; reload before continuing")
    return Path(inventory["root"]), skill, inventory


def trial(connection, name, *, expected_revision=None, root=None, home=None):
    scanned = _scan(root=root, home=home)
    with transaction(connection):
        _, skill, _ = _selected(connection, scanned, name, expected_revision)
        if skill["paths"]:
            raise workflow.WorkflowError("this skill is already on a path")
        if skill["status"] == "trial":
            return skill
        expires = (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).isoformat(timespec="seconds")
        start_trial(connection, name, expires)
        return _selected(connection, scanned, name)[1]


def _later(read, *args):
    """`read(*args)` now, before a transaction; a refusal it raises waits until the answer is used.

    So a read taken early still refuses only where the code that uses it is reached.
    """
    try:
        value = read(*args)
    except workflow.WorkflowError as error:
        refusal = error

        def refused():
            raise refusal
        return refused
    return lambda: value


def _git(root, *args):
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise workflow.WorkflowError("cannot read the skill source with git") from error
    if result.returncode or len(result.stdout.encode()) > MAX_TEXT:
        raise workflow.WorkflowError("cannot read the skill's committed source and authorship")
    return result.stdout.strip()


def _authorship(root, skill):
    directory = str(Path(skill["path"]).parent)
    if _git(root, "status", "--porcelain", "--", directory):
        raise workflow.WorkflowError("commit this skill's changes before review; unrelated checkout changes can stay")
    latest = _git(root, "log", "-1", "--format=%H", "--", directory)
    message = _git(root, "show", "-s", "--format=%B", latest)
    own = [line[len("Authored-with:"):].strip() for line in message.rsplit("\n\n", 1)[-1].splitlines() if line.startswith("Authored-with:")]
    values = own[:]
    if not own:
        for record in _git(root, "log", "--format=%B%x1e", f"{latest}..HEAD").split("\x1e"):
            found = []
            for line in record.strip().rsplit("\n\n", 1)[-1].splitlines():
                match = re.fullmatch(r"Attributes: ([a-f0-9]{7,40}) (\S+)", line)
                if match and latest.startswith(match[1]) and _git(root, "rev-parse", "--verify", match[1] + "^{commit}") == latest:
                    found.append(match[2])
            if found:
                values = found
                break
    vendors = set()
    for value in values:
        if value == "human":
            vendors.add("human")
            continue
        match = re.fullmatch(r"[^/\s]+\s*/\s*([^/\s]+)", value)
        if not match:
            raise workflow.WorkflowError("the skill's latest change has malformed Authored-with attribution")
        vendors.add(match[1].lower())
    if not vendors:
        raise workflow.WorkflowError("the skill's latest change needs a valid Authored-with trailer or recorded attribution before independent review")
    return latest, sorted(vendors)


def is_review(row):
    """Whether `row` is a skill review: a `task` the catalogue filed with
    `fields.skill_review.action` `review`. The kind `skill-review` held it
    until migration 46 (sd:7004); `runner.delivery_candidates` spells the
    same test in SQL."""
    if row["kind"] != "task" or row["source"] != "skill-request":
        return False
    try:
        fields = json.loads(row["fields"] or "{}")
    except (TypeError, ValueError):
        return False
    review = fields.get("skill_review") if isinstance(fields, dict) else None
    return isinstance(review, dict) and review.get("action") == "review"


def request(connection, name, action, *, expected_revision=None, path_name=None, root=None, home=None, who):
    """File intent only, as one ready item. No branch, skill or installed surface is changed here.

    It queues no runner assignment: the runner is idle, so the item waits in
    Tasks for the operator or a session (system sd:5477).
    """
    if action not in {"review", "promote", "demote"}:
        raise workflow.WorkflowError("skill action must be review, promote or demote")
    # Files and git before the transaction (pack sd:7853): it holds the
    # write lock, and a scan or a git call can take seconds.
    scanned = _scan(root=root, home=home)
    checkout = scanned["root"]
    found = next((skill for skill in scanned["skills"] if skill["name"] == name), None)
    # An unknown name is refused in the transaction (`_selected`) before this answer is used.
    authorship = _later(_authorship, checkout, found) if found else _later(tuple)
    head = _later(_git, checkout, "rev-parse", "HEAD")
    moves = action in {"promote", "demote"}
    paths_changed = _later(_git, checkout, "status", "--porcelain", "--", "skills/paths.json") if moves else _later(str)
    paths_sha = hashlib.sha256((checkout / "skills/paths.json").read_bytes()).hexdigest() if moves else None
    with transaction(connection):
        root, skill, inventory = _selected(connection, scanned, name, expected_revision)
        from .repos import row_for
        registered = row_for(connection, str(root))
        if registered is None:
            raise workflow.WorkflowError("register the pack repository before filing skill work")
        # The key the row holds names the request, so two machines hash one
        # identity (sd:1439).
        key = registered["path"]
        bound_files = dict(skill["files"])
        if moves:
            if paths_changed():
                raise workflow.WorkflowError("commit the workflow path changes before filing a skill move")
            bound_files["skills/paths.json"] = paths_sha
        bound_hash = _hash(bound_files)
        identity = _hash({"repo": key, "skill": name, "action": action, "source": bound_hash, "path": path_name})
        previous = connection.execute("SELECT id FROM item WHERE source='skill-request' AND external_id=?", (identity,)).fetchone()
        if previous:
            return workflow.item_state(connection, previous["id"])
        latest, vendors = authorship()
        if action == "promote" and (skill["paths"] or path_name not in inventory["paths"]):
            raise workflow.WorkflowError("choose a declared path for a contrib skill")
        if action == "demote" and not skill["paths"]:
            raise workflow.WorkflowError("only a skill on a path can be demoted")
        source = {"name": name, "path": skill["path"], "source_sha256": bound_hash,
                  "files": bound_files, "latest_author_vendors": vendors,
                  "latest_author_vendor": vendors[0] if len(vendors) == 1 else None, "latest_commit": latest,
                  "action": action, "path_name": path_name, "accepted": []}
        brief = ("Review the skill for correctness of steps, flags and references, internal consistency, overlap, contradictions, missing handoffs and vocabulary fit. "
                 "Do not edit source or contact external systems. Write .git/sd-skill-review.json with version=1, item=ITEM_ID, source_sha256=" + skill["source_sha256"] +
                 ", proposals=[{path,line_start,line_end,body}]. Each proposal names a file in this skill and exact existing lines. An empty proposals array is a valid review with no recommendations.") if action == "review" else (
                 f"{action.capitalize()} {name} in an isolated checkout. " +
                 (f"Move contrib/{name} to skills/{name} and add it to the {path_name} path." if action == "promote" else f"Move skills/{name} to contrib/{name} and remove it from every path.") +
                 " Preserve unrelated work, run the pack checks and follow its sd-ship skill to prepare one pull request. The operator owns merging.")
        item = create_item(connection, kind="task",
            title=f"{action.capitalize()} {name}", status="ready", repo=key,
            branch=f"skill-{action}/{name}-{uuid.uuid4().hex[:10]}", source_commit=head(),
            source="skill-request", external_id=identity,
            fields={"skill_review": source}, body={"text": brief}, session=who)
        set_item_fields(connection, item, body={"text": brief.replace("ITEM_ID", str(item))})
        return workflow.item_state(connection, item)


def record_review_proposals(connection, item, assignment, provider, document):
    """Validate the whole reviewer result before appending any proposal notes."""
    with transaction(connection):
        state = workflow.item_state(connection, item)
        source = json.loads(state["item"]["fields"] or "{}").get("skill_review", {})
        held = runner.queue_state(connection, assignment)
        configured = registry.read(connection=connection).providers.get(provider)
        if not is_review(state["item"]) or held["item"] != item or held["role"] != "reviewer" or held["status"] != "running" or held["provider"] != provider:
            raise workflow.WorkflowError("proposal result must come from this skill's running reviewer assignment")
        if not configured or configured.vendor.lower() in (source.get("latest_author_vendors") or [source.get("latest_author_vendor")]):
            raise workflow.WorkflowError("skill reviewer must have a different vendor from the latest author")
        if not isinstance(document, dict) or set(document) != {"version", "item", "source_sha256", "proposals"} or document["version"] != 1 or document["item"] != item or document["source_sha256"] != source.get("source_sha256"):
            raise workflow.WorkflowError("review result does not match the bound skill snapshot")
        proposals = document["proposals"]
        if not isinstance(proposals, list) or len(proposals) > 40 or len(json.dumps(document).encode()) > 65536:
            raise workflow.WorkflowError("review result exceeds its proposal or size limit")
        for proposal in proposals:
            if not isinstance(proposal, dict) or set(proposal) != {"path", "line_start", "line_end", "body"} or proposal["path"] not in source["files"]:
                raise workflow.WorkflowError("each proposal must name a file in the reviewed skill")
            start, end = proposal["line_start"], proposal["line_end"]
            if type(start) is not int or type(end) is not int or not 1 <= start <= end or not isinstance(proposal["body"], str) or not proposal["body"].strip():
                raise workflow.WorkflowError("proposal needs existing line numbers and recommendation text")
            original = _git(paths.expand(state["item"]["repo"]), "show", f"{state['item']['source_commit']}:{proposal['path']}")
            if end > len(original.splitlines()):
                raise workflow.WorkflowError("proposal line range is outside the reviewed file")
        if source.get("review_result"):
            if source["review_result"] == _hash(document):
                return workflow.item_state(connection, item)
            raise workflow.WorkflowError("this reviewer result was already recorded with different content")
        source["proposal_notes"] = [add_note(connection, item, "proposal",
            json.dumps({**proposal, "assignment": assignment, "provider": provider}, sort_keys=True),
            session="reviewer") for proposal in proposals]
        source["review_result"] = _hash(document)
        set_item_fields(connection, item, fields={**json.loads(state["item"]["fields"] or "{}"), "skill_review": source})
        add_note(connection, item, "comment", f"Reviewer {provider} recorded {len(proposals)} proposals against the checked skill source.", session="runner")
        return workflow.item_state(connection, item)


def apply_proposals(connection, item, notes, *, expected_revision, who):
    with transaction(connection):
        state = workflow._checked_state(connection, item, expected_revision)
        source = json.loads(state["item"]["fields"] or "{}").get("skill_review", {})
        if not is_review(state["item"]) or not source.get("review_result"):
            raise workflow.WorkflowError("a completed skill review is required")
        if connection.execute(f"SELECT 1 FROM assignment WHERE item=? AND status IN {runner.OWNING}", (item,)).fetchone():
            raise workflow.WorkflowError("wait for the reviewer to finish and release its checkout")
        if connection.execute("SELECT 1 FROM runner_run JOIN assignment ON assignment.id=runner_run.assignment WHERE assignment.item=? AND runner_run.released_at IS NULL", (item,)).fetchone():
            raise workflow.WorkflowError("the review checkout needs reconciliation before applying proposals")
        if not isinstance(notes, list) or not notes or any(type(note) is not int for note in notes) or len(set(notes)) != len(notes):
            raise workflow.WorkflowError("select distinct proposal notes to apply")
        choices = {note["id"]: note for note in state["notes"] if note["kind"] == "proposal" and not note["resolved_at"] and note["id"] in source.get("proposal_notes", [])}
        if set(notes) - set(choices):
            raise workflow.WorkflowError("one selected proposal is missing, already accepted or belongs to another review")
        root = paths.expand(state["item"]["repo"])
        if _snapshot(root, root / Path(source["path"]).parent) != source["files"]:
            raise workflow.WorkflowError("the skill changed since review; refresh its review before applying proposals")
        accepted = [{"note": note, **json.loads(choices[note]["body"])} for note in notes]
        brief = "Apply only these accepted skill proposals in an isolated checkout. Run the pack checks and follow its sd-ship skill to prepare one pull request; the operator owns merging.\n\n" + json.dumps(accepted, indent=2)
        application = create_item(connection, kind="task", title=f"Apply {len(notes)} proposals to {source['name']}", status="ready",
            repo=state["item"]["repo"], branch=f"skill-apply/{source['name']}-{uuid.uuid4().hex[:10]}", source_commit=state["item"]["source_commit"],
            fields={"skill_review": {**source, "action": "apply", "review_item": item, "accepted": notes}}, body={"text": brief}, session=who)
        queued = runner.enqueue(connection, [application], role="author", scope="skill-apply", who=who)
        for note in notes:
            resolve_note(connection, note)
        add_note(connection, item, "comment", f"Accepted proposals {notes}; one isolated apply assignment {queued[0]['id']} on item {application}.", session=who)
        result = workflow.item_state(connection, application)
        result["assignments"] = queued
        return result
