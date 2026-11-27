"""A repository's policy: how agents act in it (sd:6715, docs/work/2026-10-29-repo-policy/design.md).

The `policy` declaration on the repo row (sd:5109) says whose rules a repository follows:

    sd-db repo declare PATH policy "$(cat <config>/policy/<org>.json)"

No declaration reads as `mode: managed` with today's defaults (`DEFAULTS`), so a repository without one changes
nothing. An org or external repository never takes `auto` for `commit`, `push`, `pr`, `merge` or `issues` (note #34749):
`problems` refuses it, so no reader has to. `commit_convention` is checked by `check_message` where sd moves a commit
out: `sd-ship lane enqueue` checks each branch commit and the title before the push, and `sd-ship merge` rewrites
an alias in the squash subject; never by a hook installed into the repository: an org repository owns its hooks.

The `review` declaration stays the `sd-review` routing; `policy.review` is people and bots, not lenses.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MODES = ("managed", "org", "external")
ACTS = ("commit", "push", "pr")
ACT_VALUES = ("ask", "auto")
#: `merge`: whether the runner merges without asking, the row setting `sd-db repo runner-merge` writes (pack sd:7366);
#: a fourth act, `ask` when absent in every mode.
MERGE = "merge"
MERGE_METHODS = ("rebase", "squash", "merge")
ISSUES = ("off", "read", "ask")
#: `issues_scope`: which intake issues the repository lists (pack sd:7672); `all` when absent. It governs no write.
ISSUES_SCOPES = ("all", "mine")
REVIEW_FIELDS = {"human": None, "reviewers": None, "request_when": ("after-green", "on-open"),
                 "bots": ("ignore", "triage", "blocking-only"), "copilot": ("never", "on-request"),
                 "after_approval": ("add-commits-only", "rebase-ok")}
CONVENTION_FIELDS = ("types", "aliases", "body", "footer", "title_max", "line_max", "release_types")
#: `ci`: the provider `sd-ship merge` waits on, its base URL and the job path; `{pr}` in the job is the PR number.
#: `github` (absent too) and `local` are the row setting `sd-db repo ci` writes (pack sd:7366); `jenkins` needs both.
CI_FIELDS = ("provider", "url", "job")
CI_PROVIDERS = ("github", "local", "jenkins")
#: `test`: run `setup` before the checks and `teardown` after them; each name in `env` must be set.
TEST_FIELDS = ("setup", "env", "teardown")
#: `release`: the chain after a merge (sd:6881, sd:7014, `sd_release`); the deploy three are declared together.
RELEASE_FIELDS = ("deploy_repo", "file", "key", "verify")
RELEASE_DEPLOY = ("deploy_repo", "file", "key")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
#: `runtime_data`: globs of the files code reads as data, by no name, whatever their kind (sd:10906): a build's
#: ruling round that changes one applies no reject or file ruling (`build_run.unwalked`).
RUNTIME_DATA = "runtime_data"
#: Slice 2's keys (design § Slices): accepted as objects now, so a preset written for both slices declares once.
LATER = ("ci", "release", "test", "links")

#: What a repository without a policy reads as: the operator's own rules.
DEFAULTS: dict[str, Any] = {"mode": "managed", "commit": "auto", "push": "auto", "pr": "auto", MERGE: "ask",
                            "merge_method": "squash", "issues": "off", "review": {"human": 0},
                            "commit_convention": {"title_max": 63, "line_max": 72, "body": "optional"}}

#: A Conventional Commit subject: `type(scope)!: text`.
SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?P<rest>(?:\([^)]*\))?!?: \S.*)$")


def _choice(value: dict, key: str, choices: tuple, where: str) -> list[str]:
    if key in value and value[key] not in choices:
        return [f"{where}: {key} is one of {', '.join(choices)}, not {value[key]!r}"]
    return []


def _words(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(word, str) and word.strip() for word in value)


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 1000


def _review_problems(review: Any, where: str) -> list[str]:
    if not isinstance(review, dict):
        return [f"{where} is a JSON object"]
    extra = sorted(set(review) - set(REVIEW_FIELDS))
    problems = [f"{where}: unknown key(s) {', '.join(extra)}"] if extra else []
    if "human" in review and not (_count(review["human"]) and review["human"] <= 10):
        problems.append(f"{where}: human is the number of approvals required, 0 to 10")
    if "reviewers" in review and not _words(review["reviewers"]):
        problems.append(f"{where}: reviewers is a list of names")
    for key, choices in REVIEW_FIELDS.items():
        if choices:
            problems += _choice(review, key, choices, where)
    return problems


def _convention_problems(convention: Any, where: str) -> list[str]:
    if not isinstance(convention, dict):
        return [f"{where} is a JSON object"]
    extra = sorted(set(convention) - set(CONVENTION_FIELDS))
    problems = [f"{where}: unknown key(s) {', '.join(extra)}"] if extra else []
    for key in ("types", "release_types"):
        if key in convention and not _words(convention[key]):
            problems.append(f"{where}: {key} is a list of commit types")
    aliases = convention.get("aliases", {})
    if not (isinstance(aliases, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in aliases.items())):
        problems.append(f"{where}: aliases maps a type to the type it is rewritten to")
    elif "types" in convention and _words(convention["types"]):
        problems += [f"{where}: alias {key} -> {to} names no declared type" for key, to in sorted(aliases.items())
                     if to not in convention["types"]]
    return problems + _choice(convention, "body", ("required", "optional"), where) + _shape_problems(convention, where)


def _shape_problems(convention: dict, where: str) -> list[str]:
    """The footer pattern and the two lengths."""
    problems = []
    if "footer" in convention:
        if not isinstance(convention["footer"], str):
            problems.append(f"{where}: footer is a regular expression")
        else:
            try:
                re.compile(convention["footer"])
            except re.error as error:
                problems.append(f"{where}: footer is not a regular expression: {error}")
    for key in ("title_max", "line_max"):
        if key in convention and not (_count(convention[key]) and convention[key] >= 20):
            problems.append(f"{where}: {key} is a length of at least 20")
    return problems


def _ci_problems(ci: Any, where: str) -> list[str]:
    if not isinstance(ci, dict):
        return [f"{where} is a JSON object"]
    extra = sorted(set(ci) - set(CI_FIELDS))
    found = [f"{where}: unknown key(s) {', '.join(extra)}"] if extra else []
    found += _choice(ci, "provider", CI_PROVIDERS, where)
    if ci.get("provider") == "jenkins":
        found += [f"{where}: {key} is a non-empty string" for key in ("url", "job")
                  if not (isinstance(ci.get(key), str) and ci[key].strip())]
        if isinstance(ci.get("url"), str) and not ci["url"].startswith(("http://", "https://")):
            found.append(f"{where}: url is an http or https address")
    return found


def _test_problems(test: Any, where: str) -> list[str]:
    """`test`: the recipe `sd-check` runs around the checks (sd:6882)."""
    if not isinstance(test, dict):
        return [f"{where} is a JSON object"]
    extra = sorted(set(test) - set(TEST_FIELDS))
    found = [f"{where}: unknown key(s) {', '.join(extra)}"] if extra else []
    found += [f"{where}: {key} is a shell command" for key in ("setup", "teardown")
              if key in test and not (isinstance(test[key], str) and test[key].strip())]
    if "env" in test and not (isinstance(test["env"], list)
                              and all(isinstance(name, str) and ENV_NAME.match(name) for name in test["env"])):
        found.append(f"{where}: env is a list of environment variable names")
    return found


def _release_problems(release: Any, where: str) -> list[str]:
    """`release`: the chain `sd_release` runs after a merge (sd:6881)."""
    if not isinstance(release, dict):
        return [f"{where} is a JSON object"]
    extra = sorted(set(release) - set(RELEASE_FIELDS))
    found = [f"{where}: unknown key(s) {', '.join(extra)}"] if extra else []
    found += [f"{where}: {key} is a non-empty string" for key in RELEASE_FIELDS
              if key in release and not (isinstance(release[key], str) and release[key].strip())]
    if any(key in release for key in RELEASE_DEPLOY) and not all(key in release for key in RELEASE_DEPLOY):
        found.append(f"{where}: deploy_repo, file and key are declared together")
    return found


def recipe_digest(policy: dict | None, env: Any) -> str:
    """The `test` recipe of `policy` and which of its `env` names `env` sets, as one digest (sd:6882).

    Its setup, teardown and env names change what `sd-check` runs, so a check receipt binds them.
    """
    recipe = (policy or {}).get("test") or {}
    names = recipe.get("env")
    held = {"recipe": recipe, "set": sorted(name for name in names if env.get(name)) if isinstance(names, list) else []}
    return hashlib.sha256(json.dumps(held, sort_keys=True).encode()).hexdigest()


def problems(value: Any) -> list[str]:
    """Why `value` is not a `policy` declaration; empty when it is one."""
    where = "the policy declaration"
    if not isinstance(value, dict):
        return [f"{where} is a JSON object, not {type(value).__name__}"]
    known = {"mode", *ACTS, MERGE, "merge_method", "issues", "issues_scope", "review", "commit_convention",
             RUNTIME_DATA, *LATER}
    extra = sorted(set(value) - known)
    found = [f"{where}: unknown key(s) {', '.join(extra)}"] if extra else []
    found += _choice(value, "mode", MODES, where)
    for act in (*ACTS, MERGE):
        found += _choice(value, act, ACT_VALUES, where)
    found += _choice(value, "merge_method", MERGE_METHODS, where)
    if value.get("issues") == "auto":
        found.append(f"{where}: issues takes off, read or ask; an agent never writes an issue unasked (note #34749)")
    else:
        found += _choice(value, "issues", ISSUES, where)
    found += _choice(value, "issues_scope", ISSUES_SCOPES, where)
    mode = value.get("mode", "managed")
    found += [f"{where}: {act} is ask in a {mode} repository; auto is for managed ones only (note #34749)"
              for act in (*ACTS, MERGE) if mode != "managed" and value.get(act) == "auto"]
    if "review" in value:
        found += _review_problems(value["review"], f"{where}: review")
    if "commit_convention" in value:
        found += _convention_problems(value["commit_convention"], f"{where}: commit_convention")
    if "ci" in value:
        found += _ci_problems(value["ci"], f"{where}: ci")
    if "test" in value:
        found += _test_problems(value["test"], f"{where}: test")
    if "release" in value:
        found += _release_problems(value["release"], f"{where}: release")
    if RUNTIME_DATA in value and not _words(value[RUNTIME_DATA]):
        found.append(f"{where}: {RUNTIME_DATA} is a list of path globs")
    if "links" in value and not _words(value["links"]):
        found.append(f"{where}: links is a list of registered paths")
    return found


def effective(value: dict | None) -> dict[str, Any]:
    """The policy a reader acts on: `value` over `DEFAULTS`; a non-managed repository asks for what it leaves out."""
    value = value or {}
    mode = value.get("mode", "managed")
    held = {**DEFAULTS, **({act: "ask" for act in ACTS} if mode != "managed" else {}), **value}
    held["review"] = {**DEFAULTS["review"], **value.get("review", {})}
    held["commit_convention"] = {**DEFAULTS["commit_convention"], **value.get("commit_convention", {})}
    return held


def summary(value: dict | None) -> str:
    """The session-start line: what an agent may do here, before it acts. Empty for no declaration.

    `merge` and a `github` or `local` provider are row settings (pack sd:7366), not rules an agent acts on, so a
    policy of those alone prints nothing, as it did while they were columns.
    """
    value = {key: held for key, held in (value or {}).items() if key != MERGE}
    if isinstance(value.get("ci"), dict) and value["ci"].get("provider") in ("github", "local"):
        value.pop("ci")
    if not value:
        return ""
    held = effective(value)
    human = held["review"].get("human") or 0
    parts = [f"mode={held['mode']}", *(f"{act}={held[act]}" for act in ACTS), f"merge={held['merge_method']}",
             f"issues={held['issues']}", f"review=human({human})" if human else "review=none"]
    if isinstance(value.get("ci"), dict) and value["ci"].get("provider"):
        parts.append(f"ci={value['ci']['provider']}")
    if "commit_convention" in value:
        convention = held["commit_convention"]
        if convention.get("types"):
            parts.append(f"types={'|'.join(convention['types'])}")
        if convention.get("aliases"):
            parts.append("aliases=" + ",".join(f"{key}->{to}" for key, to in sorted(convention["aliases"].items())))
        parts.append(f"body={convention['body']}")
        if convention.get("footer"):
            parts.append(f"footer={convention['footer']}")
        parts += [f"title<={convention['title_max']}", f"lines<={convention['line_max']}"]
    if value.get("links"):  # the repositories work here may also change (sd:6883)
        parts.append("links=" + "|".join(value["links"]))
    return "Repository policy (sd-db repo declare . policy): " + ", ".join(parts)


#: The review states that settle a reviewer's say; a COMMENTED review leaves an earlier one standing.
DECISIVE = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


def approvals(reviews: list, head: str) -> list[str]:
    """The human logins whose latest decisive review approves `head`, sorted (design § Risks).

    An approval binds to the commit it was given on (`commit_id`), so a push past it, or a dismissal, no longer
    counts. A bot is not a human reviewer. `reviews` is GitHub's list, oldest first.
    """
    latest: dict[str, dict] = {}
    for review in reviews:
        user = review.get("user") if isinstance(review, dict) else None
        if not isinstance(user, dict):
            continue
        login = user.get("login")
        if (isinstance(login, str) and login and user.get("type") != "Bot" and not login.endswith("[bot]")
                and review.get("state") in DECISIVE):
            latest[login] = review
    return sorted(login for login, review in latest.items()
                  if review["state"] == "APPROVED" and review.get("commit_id") == head)


class ConventionRefused(ValueError):
    """A commit message the repository's convention refuses; nothing was committed."""


def _body_problems(lines: list[str], convention: dict) -> list[str]:
    """Each body line over `line_max` outside a fence or a bare URL, the footer and the required body."""
    broke, fenced = [], False
    for number, line in enumerate(lines[1:], 2):
        fenced ^= line.startswith("```")
        if not fenced and len(line) > convention["line_max"] and not re.fullmatch(r"\S+://\S+", line.strip()):
            broke.append(f"line {number} is {len(line)} characters, over {convention['line_max']}")
    paragraphs = [part for part in "\n".join(lines[1:]).strip().split("\n\n") if part.strip()]
    footer = convention.get("footer") or ""
    trailers = _unfenced(paragraphs)[-1] if paragraphs else []
    signed = bool(footer) and any(line is not None and re.search(footer, line) for line in trailers)
    if footer and not signed:
        broke.append(f"no footer line matches {footer}")
    if convention["body"] == "required" and not (paragraphs[:-1] if signed else paragraphs):
        broke.append("the convention requires a body")
    return broke


def _unfenced(paragraphs: list[str]) -> list[list[str | None]]:
    """Each paragraph's lines, None for a line in or on a code fence, so a fenced example is never a footer."""
    fenced, out = False, []
    for part in paragraphs:
        row = []
        for line in part.split("\n"):
            marker = line.startswith("```")
            row.append(None if fenced or marker else line)
            fenced ^= marker
        out.append(row)
    return out


def footer_last(body: str, footer: str | None) -> str:
    """`body` with each line matching `footer` moved into its last paragraph, the trailer block git reads.

    `sd-ship merge` appends its own trailer paragraph after the author's body, which would leave an author's
    `Ref: LOG-1` one paragraph short of the footer (sd:6715). A paragraph left empty is dropped.
    """
    paragraphs = [part for part in body.rstrip("\n").split("\n\n") if part.strip()]
    if not footer or len(paragraphs) < 2:
        return body
    moved, kept = [], []
    for part, outside in zip(paragraphs[:-1], _unfenced(paragraphs)[:-1], strict=True):
        lines = part.split("\n")
        hits = [line is not None and bool(re.search(footer, line)) for line in outside]
        moved += [line for line, hit in zip(lines, hits, strict=True) if hit]
        rest = "\n".join(line for line, hit in zip(lines, hits, strict=True) if not hit)
        kept += [rest] if rest.strip() else []
    if not moved:
        return body
    return "\n\n".join([*kept, paragraphs[-1] + "\n" + "\n".join(moved)]) + "\n"


def check_message(message: str, convention: dict | None) -> str:
    """`message` as the commit step commits it, or `ConventionRefused` naming every rule it broke.

    An alias type is rewritten first (`docs:` to `doc:`), then the whole message is checked, so a rewrite that
    still breaks a rule refuses before `git commit` runs and nothing is left to undo (design § Failure table).
    """
    convention = {**DEFAULTS["commit_convention"], **(convention or {})}
    lines = message.rstrip("\n").split("\n")
    subject = SUBJECT.match(lines[0]) if lines else None
    broke = []
    if subject is None:
        broke.append(f"the subject {lines[0]!r} is not a Conventional Commit, type: text")
    else:
        kind = convention.get("aliases", {}).get(subject["type"], subject["type"])
        lines[0] = kind + subject["rest"]
        if convention.get("types") and kind not in convention["types"]:
            broke.append(f"type {kind!r} is not one of {', '.join(convention['types'])}")
    if len(lines[0]) > convention["title_max"]:
        broke.append(f"the subject is {len(lines[0])} characters, over {convention['title_max']}")
    broke += _body_problems(lines, convention)
    if broke:
        raise ConventionRefused("; ".join(broke))
    return "\n".join(lines) + "\n"
