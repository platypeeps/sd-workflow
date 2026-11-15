"""The fleet's branch protection, observed per registered repository.

`sd-status` reports protection for the repository enclosing the working
directory and, since R10-D6, for no other. There was therefore no single
answer to "which of my repositories actually enforce merge authority": a
repository with protection off was invisible until someone ran `sd-status`
inside it. This module is the fleet-wide answer, in the shape `sd_db` already
uses for GitHub facts -- a collector that writes rows, and a screen that reads
them. Nothing here is called from a request.

**Same doctrine, same names.** The gaps are the ones `sd-status` names, with
the same ids and, where practical, the same sentences: `enforce_admins`,
`required_checks`, `strict`, `required_not_produced`,
`produced_not_required`, `reviews`, `bypass`, the two merge-settings
flags `squash_message` and `rebase_merge`, and the two baseline flags
`protection_source` and `required_check` (sd:1741, below). `enforce_admins` is the classic
question -- are administrators themselves subject to the rules -- and a
ruleset answers it only through the bypass actors that reach
administrators: `OrganizationAdmin`, or the `RepositoryRole` that is admin.
A bypass for one GitHub App, a team or a deploy key leaves administrators
subject to every rule, so it is its own gap, `bypass`, naming the actor and
when it applies, and never "enforce_admins is off" (sd:1327 review, Codex
finding 2: that sentence on a correctly configured repository is a false
security finding). Either bypass is reported per ruleset with the gating
rules that ruleset carries, since a bypass of one ruleset reaches no other
ruleset's rules (Codex :730). Protection that exempts admins is
prose, not authority; a check the repository runs but does not require can go
red without blocking a merge. A reader who has learned one screen has learned
the other.

**Unknown is not protected, and a classic 404 is an answer only after the
rulesets answered, and only from an admin.** A repository is `protected` on
a 200 from the classic protection endpoint, or on a ruleset whose rules gate
what a merge lands (`pull_request`, `required_status_checks`) -- a branch a
ruleset protects answers 404 on the classic endpoint to its admin too, so
the 404 is never interpreted before the branch's rules are read. Then a 404
from a token with `admin` on the repository, with no gating rule, is "the
branch has no protection" and is `unprotected`. A 404 from any other token
is not: GitHub answers 404 `Not Found`, not 403, on classic protection to a
caller without `admin`, whether or not the branch is protected -- measured
2026-09-22 on home-assistant/core and gohugoio/hugo, both protected, both
404 -- while an admin of a bare branch gets 404 `Branch not protected`. That
404 is `unknown`, with the permission named as its reason; this file said
otherwise until sd:1327's review, and the collector filed every upstream
repository the operator does not administer as `unprotected` on no evidence.
The rules endpoint is readable without `admin` (home-assistant/core answers
its five rules to a token that gets 404 on classic), so a ruleset-protected
upstream repository is `protected` here and never `unknown`. Everything
else (a 403 on a private repository under a free plan, a
timeout, an exhausted budget, a malformed body, a remote that is not GitHub)
is `unknown` too, with the reason recorded, and never presented as safety.
A malformed body is one that is not JSON, whose top level has the wrong
type, or that `classify` cannot read -- an `enforce_admins` that is not a
boolean, a `contexts` that is not a list -- and `observe` files all three
(sd:1359). A nested value of the wrong type that can only drop a
requirement (checks, reviews, a rule) is read as absent, so it reports the
gap rather than safety; `enrich` reads bypass lists the same way.
A registered repository that has not been observed at all reads as
`unknown` as well, with `not yet observed` as its reason.

**Classic and rulesets together (sd:1430).** The rules are read whatever
classic answered. After a 404 they are the whole answer; beside a 200 they
are layered onto the classic object the way GitHub layers them: for each
merge-gating rule, the strictest source wins. The operator's decisions of
2026-09-24, which the pack's `sd_protection.combine` implements (sd:1419),
refine that. A source is *firm* for a rule when nobody can bypass it:
classic with `enforce_admins` on (and, for reviews, no bypass allowances),
or a ruleset whose `bypass_actors` was shown and is empty. The requirement
is the strictest among the firm sources; a stricter source that is not
firm is `advisory` and never tightens it (Q3). A bypass is the `bypass`
gap only when it removes a rule's last firm source; otherwise it is
`bypass_info` (Q2). Administrators are enforced on a rule when any source
imposing it binds them, and `enforce_admins` is true only when that holds
for every required rule (Q1). More than one source reads `source:
combined`, with a `sources` map per rule (Q4). One source keeps the output
it had. A rules read that fails beside a 200 keeps the classic result and
names the fault in `rules_read_error`: layering can only add requirements,
so classic alone is never reported stronger than it is.

**The fleet baseline (sd:1741).** Two more flags ride in the same
`merge_settings` list, for the repositories the operator owns
(`BASELINE_OWNERS`): `protection_source`, raised unless classic protection
alone protects the branch, with no active ruleset beside it (sd:5406), and
`required_check`, raised unless `ci` is among the required contexts --
present, not sole, so `route` or `body-lint` may stand beside it. A repository whose `repo.ci` is `local` runs no Actions, so for
it the check is `sd/local-gate`, and that is also the one context it
produces, as the pack's `sd-status` reads it (sd:1992). Like the merge flags they are not gaps an acknowledgement can
silence. A repository another owner holds carries neither, and the screen
shows the two cells as not applicable. A job that a required job gates
through `needs` in the same workflow file is covered by it, so an
aggregate's inner jobs are not `produced_not_required`. `needs` alone does
not gate: a failed need skips the job, and a skipped required check passes,
so only the aggregate `design.md` prescribes counts, exactly as written
there; it gates every need (`_gated_needs`).

**Alerts ride along (sd:2205, sd:2206).** For a managed repository the
sweep also stores, in the same row's `body.alerts`, its open Dependabot
alerts (one page, by severity, with each alert's summary for the GitHub
page's Dependabot view, pack sd:7515; `more` when GitHub has a next page)
and its secret-scanning facts: visibility, the `security_and_analysis` setting, and,
when scanning is on in a public repository, its open alerts. The setting
comes with the repository read the basic observation already made; the
alert pages are read last, from what the observations and bypass reads
left, so they never take a repository's three. An alert read that fails is
that read's `reason`, never a count of zero. A refused Dependabot read in a
repository the token administers costs one probe of the setting; when GitHub
says the alerts are off, `dependabot` is `{setting: "disabled"}`, a state of
its own rather than a failed read (sd:5594). Health reads the row and calls
nothing.

**A side observation, not the tracker.** `shadow_sync.sync` calls `sync`
here after the contribution refresh and before it writes its own heartbeat.
A failure here becomes `unknown` rows and its own heartbeat; it never fails
the tracker and never holds the watermark.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import paths as sdpaths
from .database import transaction
from .writes import policy_ci, policy_human

GAP_IDS = (
    "enforce_admins",
    "required_checks",
    "strict",
    "required_not_produced",
    "produced_not_required",
    "reviews",
    "bypass",
)
MERGE_FLAG_IDS = ("squash_message", "rebase_merge")
#: The two fleet-baseline flags (sd:1741), reported in `merge_settings`
#: beside the merge flags and, like them, never acknowledgeable.
BASELINE_FLAG_IDS = ("protection_source", "required_check")
#: The owners whose repositories the baseline applies to: the pack stamp's
#: `OWNERS`. Another owner's repository (an employer's, decision D1 of
#: sd:1741) carries neither baseline flag. `SD_BASELINE_OWNERS` replaces the
#: set, space-separated, so a personal account can join it from the config.
BASELINE_OWNERS = frozenset(
    os.environ.get("SD_BASELINE_OWNERS", "platypeeps").lower().split())
#: The one check name every owned repository requires (sd:1741, R3).
BASELINE_CHECK = "ci"
#: The check a `repo.ci = local` repository requires instead (sd:1992):
#: with Actions off nothing posts `ci`, and `sd-ship merge` posts this one
#: after `sd-check` passes. The pack's `sd_lib.LOCAL_GATE_CONTEXT`.
LOCAL_GATE_CHECK = "sd/local-gate"
STATUSES = ("protected", "unprotected", "unknown")

NOT_OBSERVED = "not yet observed"
BUDGET_EXHAUSTED = "budget exhausted"
#: The two ways the shared budget runs out, each a reason that names the
#: limit which stopped the observation. Both start with `BUDGET_EXHAUSTED`,
#: which is what `sync` matches a not-reached row on.
REQUESTS_EXHAUSTED = f"{BUDGET_EXHAUSTED}: request limit reached"
TIME_EXHAUSTED = f"{BUDGET_EXHAUSTED}: time limit reached"
HEARTBEAT_KEY = "protection-sync:github"

#: What `observe` spends per repository: the repository, its default
#: branch's classic protection and, whatever that answers, the first page of
#: the branch's rules (sd:1430: a ruleset beside a classic object gates the
#: merge too). That is the whole basic observation, and `sync` makes
#: it for every repository before it spends anything else, so these three
#: a repository are never taken by another repository's extras. The extras
#: are a rules list past one page (a GET a page; the fleet's longest branch
#: carries eight rules) and, after every repository
#: has its row, one GET per gating ruleset for its bypass list -- spent from
#: what is left, and when nothing is, the bypass stays unknown rather than
#: a later repository going unobserved (sd:1327 review, Codex finding 1).
REQUESTS_PER_REPO = 3

#: Wall-clock seconds `reserve_seconds` holds back per repository. The
#: request reserve alone did not keep the sweep alive: from 2026-09-26 the
#: nightly's detail backlog (271, then 320 queued) ran the shared budget to
#: its 600 s deadline with 237 requests unspent, and the sweep, which reads
#: the clock before each GET, filed all 64 rows `budget exhausted` on zero
#: requests (sd:1663). Measured 2026-09-27: 20 managed repositories, 61
#: requests, 32 s -- about 1.5 s a repository; this is twice that.
SECONDS_PER_REPO = 3.0

#: What `reserve` holds back per repository beyond the basic observation:
#: one bypass read. Measured 2026-09-27 over the 64 registered: 209
#: requests, 17 of them bypass reads past the basic 192. Without it, details
#: that stopped exactly at the reserve left no request for any bypass read.
BYPASS_MARGIN_PER_REPO = 1

#: What `reserve` holds back per managed repository for its alerts: one page
#: of open Dependabot alerts, the setting probe a 403 on it costs, and one
#: page of open secret-scanning alerts.
ALERT_REQUESTS_PER_MANAGED = 3
#: What the client raises when GitHub refuses the Dependabot alert read; a
#: repository with the alerts off answers it, as do others (`_dependabot`).
ALERTS_FORBIDDEN = "API HTTP 403; retry on a later collection"
#: One page of alerts, GitHub's largest; past it the count is "100+".
ALERT_PAGE_SIZE = 100
#: The longest advisory summary a stored alert summary keeps (pack sd:7515):
#: 100 alerts a repository stay a few kilobytes of the row's body.
ALERT_SUMMARY_CHARS = 200

#: The `source` marker on protection synthesized from rulesets; a classic
#: object carries none. The pack's `sd_protection.RULESET_SOURCE`.
RULESET_SOURCE = "ruleset"

#: The `source` marker on protection layered from a classic object and at
#: least one ruleset, each contributing a merge-gating rule (sd:1430). The
#: pack's `sd_protection.COMBINED_SOURCE`.
COMBINED_SOURCE = "combined"

#: How `_combine` names classic protection in `sources`; a ruleset is
#: `ruleset:<id>`. The pack's `sd_protection.CLASSIC_SOURCE`.
CLASSIC_SOURCE = "classic"

#: The `protection_source` value of a classic branch whose rules read failed:
#: no evidence that no ruleset stands beside it, so not the baseline (sd:5406).
UNREAD_RULESETS = "classic, rulesets unread"

#: Rule types that gate what a merge lands. `deletion`, `non_fast_forward`
#: and the rest constrain how the branch moves, not what a pull request must
#: satisfy, so a ruleset carrying only those leaves the branch as ungated as
#: no ruleset at all. The pack's `MERGE_GATING_RULES`.
MERGE_GATING_RULES = frozenset({"pull_request", "required_status_checks"})

#: The rules endpoint pages its answer. The size is sent so a full page means
#: the same to the server and to the loop that reads until a page comes back
#: short; a list still full at `MAX_RULE_PAGES` is a fault, not an answer.
RULES_PAGE_SIZE = 30
MAX_RULE_PAGES = 100


def github_slug(remote: str | None) -> tuple[str, str] | None:
    """`(owner, name)` for a github.com remote, or None for any other.

    `repos.parse_remote` decides which remotes are GitHub (sd:1025). Anything
    else is skipped -- not `unknown`, because there is nothing to be unknown about.
    """
    from .repos import parse_remote

    found = parse_remote(remote)
    return (found.owner, found.name) if found.kind == "github" else None


def protection_path(owner: str, name: str, branch: str) -> str:
    """Classic protection on `branch`, encoded: a raw `/` is another route."""
    return f"repos/{owner}/{name}/branches/{quote(branch, safe='')}/protection"


def rules_path(owner: str, name: str, branch: str, page: int = 1) -> str:
    """One page of the rules GitHub evaluates for `branch`."""
    return (f"repos/{owner}/{name}/rules/branches/{quote(branch, safe='')}"
            f"?per_page={RULES_PAGE_SIZE}&page={page}")


def ruleset_path(owner: str, name: str, ruleset_id: int) -> str:
    return f"repos/{owner}/{name}/rulesets/{ruleset_id}"


def reserve(connection: sqlite3.Connection) -> int:
    """Requests `sync` needs to reach every registered GitHub repository,
    and to read the alerts of every managed one.

    The contribution refresh runs first on the shared budget and stops this
    many short, so a long detail backlog cannot turn every protection row
    `unknown` with `budget exhausted`.
    """
    return ((REQUESTS_PER_REPO + BYPASS_MARGIN_PER_REPO) * _fleet_size(connection)
            + ALERT_REQUESTS_PER_MANAGED * _fleet_size(connection, managed=True))


def reserve_seconds(connection: sqlite3.Connection, max_seconds: float) -> float:
    """Seconds `sync` needs to reach every registered GitHub repository,
    at most half of the run's `max_seconds`.

    The same stop as `reserve`, on the clock: the budget has a deadline as
    well as a request count, and a detail backlog that spends the time
    leaves the sweep nothing whatever it left in requests. The cap keeps a
    large fleet from taking the whole run from the work the tracker exists
    for; a sweep cut short there resumes where it stopped (`sync`).
    """
    return min(SECONDS_PER_REPO * _fleet_size(connection), max_seconds / 2)


def _fleet_size(connection: sqlite3.Connection, *, managed: bool = False) -> int:
    query = "SELECT remote FROM repo" + (" WHERE managed = 1" if managed else "")
    return sum(1 for row in connection.execute(query) if github_slug(row["remote"]) is not None)


# ------------------------------------------------- the checks a repo produces
#
# A port of `workflow_checks` in the pack's `bin/sd-status`: enough YAML to
# read job names out of `.github/workflows/*.yml`, and a stated note for every
# construct it cannot resolve rather than a silent wrong answer. PyYAML is not
# stdlib and this package has no dependencies; `yaml_lite` parses the provider
# registry's two-level shape and refuses block sequences, which every workflow
# file has.

_KEY_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*):\s*(?P<value>.*?)\s*$")
_ITEM_RE = re.compile(r"^(?P<indent>\s*)-\s*(?P<rest>.*?)\s*$")

PR_TRIGGERS = ("pull_request", "pull_request_target")


class Produced(set):
    """The check names a repository produces, with `needed_by`: for each
    name, the names of the jobs in the same workflow file that reach its job
    through `needs` (sd:1741, S2). A required name among them covers it: an
    aggregate such as `ci` gates its inner jobs, so they are not
    `produced_not_required`. A plain set has no such map and covers nothing."""

    def __init__(self, names=(), needed_by: dict[str, set[str]] | None = None):
        super().__init__(names)
        self.needed_by: dict[str, set[str]] = {name: set(by) for name, by in (needed_by or {}).items()}


class Partial(Produced):
    """Produced names from workflows with a job whose names were not derived:
    a reusable workflow, an expression, an approximate matrix, an unreadable
    file. A required context missing from it may still be produced, so
    `required_not_produced` is not judged; the names in it are real."""


def _scalar(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _uncommented(line: str) -> str:
    """`line` without its comment: a `#` that starts the line or follows a
    space, outside quotes. `name: lint # fast` is the context `lint`."""
    quote_char = None
    for index, char in enumerate(line):
        if quote_char:
            quote_char = None if char == quote_char else quote_char
        elif char in "\"'" and (index == 0 or line[index - 1] in " \t:[{,"):
            quote_char = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _significant(text: str) -> list[str]:
    """Non-blank lines with their comments removed, right-stripped."""
    lines = (_uncommented(line).rstrip() for line in text.split("\n"))
    return [line for line in lines if line.strip()]


def _children(lines: list[str], index: int) -> list[str]:
    base = _indent(lines[index])
    out = []
    for line in lines[index + 1:]:
        if _indent(line) <= base:
            break
        out.append(line)
    return out


def _entries(lines: list[str]) -> list[tuple[int, str, str]]:
    """`(offset, key, inline value)` for the mapping keys at the top indent."""
    if not lines:
        return []
    base = _indent(lines[0])
    found = []
    for offset, line in enumerate(lines):
        if _indent(line) != base:
            continue
        match = _KEY_RE.match(line)
        if match:
            found.append((offset, match.group("key"), match.group("value")))
    return found


def _field(lines: list[str], key: str) -> str | None:
    for _, name, value in _entries(lines):
        if name == key:
            return value
    return None


def _sub(lines: list[str], key: str) -> list[str]:
    for offset, name, _ in _entries(lines):
        if name == key:
            return _children(lines, offset)
    return []


def _inline_list(value: str) -> list[str] | None:
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(part) for part in text[1:-1].split(",") if part.strip()]
    return None


def _sequence(lines: list[str], inline: str) -> list[str]:
    parsed = _inline_list(inline)
    if parsed is not None:
        return parsed
    if not lines:
        return []
    base = _indent(lines[0])
    return [
        _scalar(match.group("rest"))
        for line in lines
        if _indent(line) == base and (match := _ITEM_RE.match(line))
    ]


def _mappings(lines: list[str]) -> list[dict[str, str]]:
    """A `- key: value` block list, each entry keeping its key order."""
    if not lines:
        return []
    base = _indent(lines[0])
    found: list[dict[str, str]] = []
    for line in lines:
        item = _ITEM_RE.match(line)
        if item and _indent(line) == base:
            current: dict[str, str] = {}
            found.append(current)
            inner = _KEY_RE.match(item.group("rest"))
            if inner:
                current[inner.group("key")] = _scalar(inner.group("value"))
            continue
        if not found or _indent(line) <= base:
            continue
        match = _KEY_RE.match(line)
        if match:
            found[-1][match.group("key")] = _scalar(match.group("value"))
    return found


def _matrix_names(matrix: list[str]) -> tuple[list[str], bool]:
    """Matrix suffixes as GitHub renders them, and whether that is approximate."""
    axes: dict[str, list[str]] = {}
    includes: list[dict[str, str]] = []
    excludes: list[dict[str, str]] = []
    for offset, key, inline in _entries(matrix):
        if key == "exclude":
            excludes = _mappings(_children(matrix, offset))
            continue
        if key == "include":
            includes = _mappings(_children(matrix, offset))
            continue
        values = _sequence(_children(matrix, offset), inline)
        if values:
            axes[key] = values
    if axes:
        # A combination matching every key of one `exclude:` entry is one
        # GitHub never runs.
        combos = [
            tuple(values)
            for values in itertools.product(*axes.values())
            if not any(
                entry and all(dict(zip(axes, values, strict=True)).get(k) == v for k, v in entry.items())
                for entry in excludes
            )
        ]
        approximate = bool(includes)
    else:
        combos = [tuple(entry.values()) for entry in includes if entry]
        approximate = False
    return [", ".join(combo) for combo in combos if combo], approximate


def _triggers(lines: list[str]) -> set[str]:
    """The workflow's event names; an empty set means "could not tell"."""
    for offset, key, inline in _entries(lines):
        if key not in ("on", "true"):  # a YAML 1.1 loader would fold `on` to true
            continue
        parsed = _sequence(_children(lines, offset), inline)
        if parsed:
            return set(parsed)
        if _scalar(inline) and not inline.lstrip().startswith("{"):
            return {_scalar(inline)}  # `on: schedule`, the one event
        return {name for _, name, _ in _entries(_children(lines, offset))}
    return set()


def _jobs(lines: list[str]) -> list[tuple[str, list[str]]]:
    for offset, key, _ in _entries(lines):
        if key == "jobs":
            block = _children(lines, offset)
            return [
                (name, _children(block, index)) for index, name, _ in _entries(block)
            ]
    return []


def _needs(body: list[str]) -> list[str]:
    """The job ids a job's `needs` names: a scalar, an inline list or a block list."""
    for offset, key, inline in _entries(body):
        if key == "needs":
            listed = _sequence(_children(body, offset), inline)
            if listed:
                return listed
            value = _scalar(inline)
            return [value] if value and _inline_list(inline) is None else []
    return []


#: The whole job conditions that still run the job after a job it needs
#: failed. Exact, not searched: `always() && needs.x.result == 'success'`
#: skips on the failure it names (review round 3), and a condition this
#: cannot evaluate is not taken as proof.
_RUNS_AFTER_FAILURE = frozenset({"!cancelled()", "always()"})
#: A line that is a condition, a job's or a step's.
_CONDITION_LINE = re.compile(r"^\s*(?:-\s*)?if:")
#: A job or step that lets a failure pass; its value, unless `false`.
_TOLERATES = re.compile(r"^\s*(?:-\s*)?continue-on-error:\s*(.*)$")
#: The aggregate step `design.md` ("The `ci` job") prescribes, the only
#: one this reader credits (review round 6): the results of every need,
#: then a loop that fails on the first that is not `success`. Lines are
#: compared with runs of whitespace collapsed; the echo text may vary.
_AGGREGATE_ENV = re.compile(r"""^\s*(?:-\s*)?RESULTS:\s*\$\{\{\s*join\(\s*needs\.\*\.result\s*,\s*'\s'\s*\)\s*\}\}$""")
_AGGREGATE_LOOP = (
    re.compile(r"^for result in \$RESULTS; do$"),
    re.compile(r"""^\[ "\$result" = success \] \|\| \{ echo "[^"`$\\;]*(?:\$result)?"; exit 1; \}$"""),
    re.compile(r"^done$"),
)
_ASSIGNS_RESULTS = re.compile(r"\bRESULTS\s*[:=]")
_RUN_BLOCK = re.compile(r"^\s*(?:-\s*)?run:\s*\|\s*$")
#: The only line shapes the aggregate job may hold outside its loop: a
#: template key in plain form, or an item of a block `needs:` list. A line
#: match cannot follow YAML's other spellings of a key -- quoted
#: (`"continue-on-error": true`, the extra review pass), explicit (`? k`),
#: a flow mapping, a merge key, a value continued on the next line (sd:1820)
#: -- so they are not read, they are refused.
_TEMPLATE_LINE = re.compile(
    r"^\s*(?:(?:-\s+)?(?:name|needs|if|runs-on|timeout-minutes|steps|env|run|continue-on-error|RESULTS):(?:\s.*)?"
    r"|-\s+[A-Za-z0-9_.-]+)$")
#: `${{ }}` spans, whose text GitHub evaluates and YAML does not read.
_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
#: YAML syntax that can add or alias a key once expressions are removed:
#: flow mappings, anchors, aliases, tags.
_YAML_SYNTAX = re.compile(r"[{}&*!]")
#: The workflow keys that leave the aggregate as written, plain or quoted
#: (`"on":` is common), and a document start. Any other top-level line --
#: `defaults:` in any spelling (quoted, the extra pass on dd81109), an
#: explicit or merge key -- can change what every step runs, so it is
#: refused, not read.
_WORKFLOW_LINE = re.compile(
    r"""^(?:---|(?P<q>["']?)(?:name|run-name|on|permissions|concurrency|env|jobs)(?P=q):(?:\s.*)?)$""")
#: Keys that change what a step runs or which steps run.
_STEP_KEYS = re.compile(r"^\s*(?:-\s*)?(run|uses|shell):")


def _condition(value: str | None) -> str:
    """A job's `if:` without quotes, `${{ }}` and whitespace."""
    text = _scalar(value or "")
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return "".join(text.split())


def _gated_needs(body: list[str], needs: list[str]) -> list[str]:
    """The jobs among `needs` whose failure this job makes its own.

    `needs` alone does not: a failed need skips the job, and GitHub counts a
    skipped required check as passing (sd:1741 review). Textual signs of a
    result check -- a result read, an `exit` -- did not hold either: `echo
    "${{ needs.x.result }}; exit 1"` shows both and passes (review rounds
    1 to 6). So only the design's aggregate is credited, and then for every
    need: the whole job `if:` is `!cancelled()` or `always()`, no other
    `if:` and no `continue-on-error` other than `false` in the job, one
    step that is a `run: |` block, no `uses:` or `shell:`, the
    `RESULTS` join of `needs.*.result` in its own `env:` (round 7), every
    other line a template key in plain form (the extra pass), and the block is exactly the loop
    `_AGGREGATE_LOOP` matches. Any other aggregate reads as not gating,
    which reports a gap that is not there rather than hide one that is. A
    workflow-level `defaults:` is refused by the caller.
    """
    if _condition(_field(body, "if")) not in _RUNS_AFTER_FAILURE:
        return []
    # A flow-sequence `name:` is no check name, so it gates nothing (sd:2902).
    if (_field(body, "name") or "").startswith("["):
        return []
    loop = {child for start, line in enumerate(body) if _RUN_BLOCK.match(line)
            for child in range(start + 1, start + 1 + len(_children(body, start)))}
    for index, line in enumerate(body):
        if index in loop:
            continue
        if not _TEMPLATE_LINE.match(line) or _YAML_SYNTAX.search(_EXPRESSION.sub("", line)):
            return []
    if sum(1 for line in body if _CONDITION_LINE.match(line)) != 1:
        return []
    if any((match := _TOLERATES.match(line)) and _scalar(match.group(1)).lower() != "false" for line in body):
        return []
    keys = [index for index, line in enumerate(body) if _STEP_KEYS.match(line)]
    if len(keys) != 1 or not _RUN_BLOCK.match(body[keys[0]]):
        return []
    # One assignment of RESULTS in the job, the join, in the step's own
    # `env:` (deeper than its `run:`), so no job-level value or override
    # replaces it (review round 7). A step's env wins over a workflow's.
    assigns = [line for line in body if _ASSIGNS_RESULTS.search(line)]
    if len(assigns) != 1 or not _AGGREGATE_ENV.match(assigns[0]):
        return []
    if _indent(assigns[0]) <= _indent(body[keys[0]]):
        return []
    block = [" ".join(line.split()) for line in _children(body, keys[0])]
    if len(block) != len(_AGGREGATE_LOOP):
        return []
    if not all(pattern.match(line) for pattern, line in zip(_AGGREGATE_LOOP, block, strict=True)):
        return []
    return list(needs)


def _needed_by(jobs: dict[str, list[str]], names: dict[str, set[str]]) -> dict[str, set[str]]:
    """For each job id, the names of the jobs that gate it through
    `needs`, transitively, within one workflow file. `jobs` maps
    each job to the needs `_gated_needs` accepts, so a walk follows only
    those edges: a job that merely needs another is skipped by its
    failure, and that skip hides it. A cycle, which GitHub rejects anyway,
    ends the walk rather than looping."""
    found: dict[str, set[str]] = {}
    for outer, direct in jobs.items():
        seen: set[str] = set()
        stack = list(direct)
        while stack:
            inner = stack.pop()
            if inner in seen or inner == outer:
                continue
            seen.add(inner)
            stack.extend(jobs.get(inner, []))
        for inner in seen:
            found.setdefault(inner, set()).update(names.get(outer, set()))
    return found


def _job_names(
    filename: str, job_id: str, body: list[str], produced: set[str], notes: list[str]
) -> bool:
    """Add the job's check names; False when they were not all derived."""
    if _field(body, "uses") is not None:
        notes.append(
            f"{filename}: job {job_id} calls a reusable workflow; "
            "its check names live in the called file and are not derived here"
        )
        return False
    display = _scalar(_field(body, "name") or "") or job_id
    matrix = _sub(_sub(body, "strategy"), "matrix")
    suffixes, approximate = _matrix_names(matrix) if matrix else ([], False)
    if approximate:
        notes.append(
            f"{filename}: job {job_id} mixes matrix axes with include:, "
            "so its derived names are approximate"
        )
    names = [f"{display} ({suffix})" for suffix in suffixes] or [display]
    if any("${{" in name for name in names):
        notes.append(
            f"{filename}: job {job_id} names contain a ${{{{ }}}} expression "
            "that only the runner can resolve"
        )
        return False
    if _field(body, "if") is not None:
        notes.append(
            f"{filename}: job {job_id} is conditional (if:); a required context "
            "it does not report stays pending forever"
        )
    produced.update(names)
    return not approximate


def produced_contexts(root: Path | str, *, ci: str | None = None) -> tuple[set[str] | None, list[str]]:
    """The check names `.github/workflows/**` produces on a pull request, or
    the local gate's when `ci` is `local`.

    Read-only: it reads files under the registered checkout and runs nothing,
    because that checkout may be another session's. Returns `(None, [note])`
    when the checkout is absent -- *None*, not an empty set, because an empty
    set is a real answer (a repository with no workflows produces no checks,
    and every context it requires is then one nothing produces) and an absent
    checkout is not one. `classify` skips the two comparison gaps on None.

    A `repo.ci = local` repository runs no Actions; `sd-ship merge` posts
    `LOCAL_GATE_CHECK`, the one context it produces, checkout or not. The
    workflow notes stay, as the pack's `sd-status` keeps them.
    """
    # A stored key is a disk path only after `expand` (sd:1439).
    root = sdpaths.expand(root) if str(root).startswith("~") else Path(root)
    if ci == "local":
        notes = produced_contexts(root)[1] if root.is_dir() else []
        return Produced({LOCAL_GATE_CHECK}), [
            *notes, f"repo.ci is local: {LOCAL_GATE_CHECK} replaces the workflow contexts"]
    if not root.is_dir():
        return None, [
            f"checkout {root} is absent on this machine, so the checks it "
            "produces could not be derived and the two comparison gaps are not reported"
        ]
    directory = root / ".github" / "workflows"
    produced: set[str] = set()
    notes: list[str] = []
    if not directory.is_dir():
        return produced, ["no .github/workflows directory, so this repo produces no checks"]
    try:
        paths = sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix in (".yml", ".yaml")
        )
    except OSError as error:
        return None, [f".github/workflows is unreadable ({error})"]
    complete = True
    # Per name, one set of gating names per job that produces it: a name
    # two jobs produce is covered only by what gates both (review round 5).
    producers: dict[str, list[set[str]]] = {}
    for path in paths:
        try:
            lines = _significant(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as error:
            notes.append(f"{path.name}: unreadable ({error})")
            complete = False
            continue
        triggers = _triggers(lines)
        if triggers and not triggers & set(PR_TRIGGERS):
            continue
        jobs = _jobs(lines)
        names: dict[str, set[str]] = {}
        for job_id, body in jobs:
            names[job_id] = set()
            complete = _job_names(path.name, job_id, body, names[job_id], notes) and complete
            produced |= names[job_id]
        gating: dict[str, list[str]] = {}
        # A workflow-level `defaults:` can change every step's shell, which
        # the aggregate template does not survive (review round 6). Only
        # plain known keys pass, so no other spelling of it gets through.
        # The top level is the smallest indent, not column 0: YAML reads a
        # workflow indented as a whole the same (re-review of 923fdcf).
        base = min((_indent(line) for line in lines), default=0)
        defaults = any(_indent(line) == base and not _WORKFLOW_LINE.match(line[base:])
                       for line in lines)
        for job_id, body in jobs:
            needs = _needs(body)
            gating[job_id] = [] if defaults else _gated_needs(body, needs)
            ungated = [need for need in needs if need not in gating[job_id]]
            if ungated:
                notes.append(
                    f"{path.name}: job {job_id} needs {', '.join(ungated)} but is not the aggregate "
                    "design.md prescribes, so it is not counted as gating them"
                )
        gated_by = _needed_by(gating, names)
        for job_id, job_names in names.items():
            for name in job_names:
                producers.setdefault(name, []).append(gated_by.get(job_id, set()))
    needed_by = {name: set.intersection(*sets) for name, sets in producers.items()}
    needed_by = {name: by for name, by in needed_by.items() if by}
    return (Produced if complete else Partial)(produced, needed_by), notes


# ---------------------------------------------------------- the classification


def _protection_gaps(
    protection: dict[str, Any],
    default_branch: str,
    produced: set[str] | None,
    notes: list[str],
    human: int = 1,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """`_protection_gaps` as `bin/sd-status` has it, plus one case: `produced`
    is None when the checkout could not be read, and then the two gaps that
    compare required against produced are not reported rather than reported
    against an empty set. `human` is the approvals the repository's policy
    asks for; fewer is the `reviews` gap (pack sd:8823)."""
    gaps: list[dict[str, str]] = []
    checks = protection.get("required_status_checks") or {}
    reviews = protection.get("required_pull_request_reviews")
    admins = protection.get("enforce_admins") or {}
    enabled = admins.get("enabled") if isinstance(admins, dict) else admins
    if enabled not in (True, False, None):
        # Read by truthiness, `[1]` or `"no"` would be "enforced" and print
        # no gap: a malformed body presented as safety (sd:1359). `observe`
        # files the error as unknown.
        raise TypeError(f"enforce_admins is {type(enabled).__name__}, not a boolean")
    source = protection.get("source")
    if enabled is None and source not in (RULESET_SOURCE, COMBINED_SOURCE):
        enabled = False

    if enabled is None:
        # Unknown, reported as not enforced and never as enforced, for one of
        # two reasons, each named: a bypass list withheld or not read, or a
        # `RepositoryRole` bypass whose numeric id nothing here resolves to
        # admin or not -- so neither "exempts the admins" nor "administrators
        # stay subject" is said of it (sd:1327 review).
        gaps.append({"id": "enforce_admins", "gap": _unknown_admins_words(protection, default_branch)})
    elif not enabled and source == COMBINED_SOURCE:
        # Per rule: which rules no source binds the administrators on, and
        # which ones a source still does (sd:1430, Q1).
        gaps.append({"id": "enforce_admins", "gap": _combined_admins_words(protection, default_branch)})
    elif not enabled and source == RULESET_SOURCE:
        gaps.append({"id": "enforce_admins", "gap": _admin_bypass_words(protection, default_branch)})
    elif not enabled:
        gaps.append(
            {
                "id": "enforce_admins",
                "gap": (
                    f"enforce_admins is off on {default_branch}: every rule below stops "
                    "collaborators and exempts the admins who do the merging. "
                    "Protection that exempts admins is prose, not authority."
                ),
            }
        )
    others = _bypass_pairs(protection, False)
    if others:
        named = "; ".join(f"{_scope(entry)} by {_actor_words(actor)}" for entry, actor in others)
        subject = [entry for entry in _ruleset_states(protection) if entry["admins"] == "enforced"]
        gaps.append(
            {
                "id": "bypass",
                "gap": (
                    f"a ruleset protecting {default_branch} can be bypassed: {named}. "
                    "That actor is not subject to the rules in brackets, and a merge it "
                    "makes lands past them"
                    + (
                        f"; administrators stay subject to {_scopes(subject)}."
                        if subject else "."
                    )
                ),
            }
        )
    if not isinstance(checks, dict) or not checks:
        gaps.append(
            {
                "id": "required_checks",
                "gap": f"no required status checks on {default_branch}: a red PR still merges",
            }
        )
        contexts: list[str] = []
    else:
        contexts = [str(name) for name in checks.get("contexts") or []]
        if not checks.get("strict"):
            gaps.append(
                {
                    "id": "strict",
                    "gap": (
                        "required checks are not strict: a green check run against a "
                        "stale base still merges, so main can go red on a merge that "
                        "was never tested against it"
                    ),
                }
            )
    if produced is None:
        missing: list[str] = []
        extra: list[str] = []
    else:
        # A partial set cannot say a context is produced nowhere (sd:1204).
        missing = [] if isinstance(produced, Partial) else sorted(set(contexts) - produced)
        # A job a required job needs is gated through it (sd:1741, S2).
        needed_by = getattr(produced, "needed_by", {})
        extra = sorted(name for name in produced - set(contexts)
                       if not needed_by.get(name, set()) & set(contexts))
    if missing:
        gaps.append(
            {
                "id": "required_not_produced",
                "gap": (
                    "required contexts no workflow in this repo produces: "
                    + ", ".join(missing)
                    + " -- each one blocks every PR until an external app reports it"
                ),
            }
        )
    if extra:
        gaps.append(
            {
                "id": "produced_not_required",
                "gap": (
                    "checks this repo runs but does not require: "
                    + ", ".join(extra)
                    + " -- they can go red without blocking a merge"
                ),
            }
        )
    if not isinstance(reviews, dict):
        gaps.append(
            {
                "id": "reviews",
                "gap": f"no pull-request review is required on {default_branch}",
            }
        )
        approvals = 0
    else:
        approvals = int(reviews.get("required_approving_review_count") or 0)
        if 0 < approvals < human:
            gaps.append({"id": "reviews", "gap": (
                f"a pull request is required but {approvals} approval{'s are' if approvals > 1 else ' is'} "
                f"fewer than the {human} the repository policy asks for")})
        elif approvals < human:
            # The `required_pull_request_reviews` object is what requires a
            # pull request at all; what asks for nothing is the approval
            # count. The two wordings are deliberately different.
            gaps.append(
                {
                    "id": "reviews",
                    "gap": (
                        "a pull request is required but no approving review is: "
                        "0 approvals, so one with green CI self-merges"
                    ),
                }
            )
    detail: dict[str, Any] = {
        "enforce_admins": None if enabled is None else bool(enabled),
        "strict": bool(checks.get("strict")) if isinstance(checks, dict) else False,
        "required_contexts": sorted(contexts),
        "produced_contexts": sorted(produced) if produced is not None else None,
        "required_not_produced": missing,
        "produced_not_required": extra,
        "required_approving_review_count": approvals,
        "workflow_notes": list(notes),
    }
    if source in (RULESET_SOURCE, COMBINED_SOURCE):
        detail["source"] = source
        detail["rulesets"] = list(protection.get("rulesets") or [])
        detail["bypass"] = [f"{_scope(entry)}: {_actor_words(actor)}" for entry, actor in others]
        detail["admin_bypass"] = [
            f"{_scope(entry)}: {_actor_words(actor)}"
            for entry, actor in _bypass_pairs(protection, True)
        ]
    if source == COMBINED_SOURCE:
        detail["sources"] = dict(protection.get("sources") or {})
        detail["bypass_info"] = list(protection.get("bypass_info") or [])
        detail["advisory"] = list(protection.get("advisory") or [])
    return gaps, detail


def merge_settings(repo: dict[str, Any]) -> list[dict[str, Any]]:
    """The two merge-settings flags, reported beside the protection gaps.

    They are not branch protection -- they are what the merge button does when
    a human presses it -- but they decide whether `wip:` subjects from a
    carrier branch reach main's history, so they are reported here or nowhere.
    """
    title = str(repo.get("squash_merge_commit_title") or "")
    message = str(repo.get("squash_merge_commit_message") or "")
    return [
        {
            "id": "squash_message",
            "value": f"{title or '?'} / {message or '?'}",
            "flagged": title != "PR_TITLE" or message == "COMMIT_MESSAGES",
            "gap": (
                f"squash commits are built from {title or '?'} / {message or '?'}, so a "
                "carrier branch's `wip:` subjects land in main's history when the merge "
                "runs through the web UI; set the title to PR_TITLE and the message to "
                "PR_BODY or BLANK"
            ),
        },
        {
            "id": "rebase_merge",
            "value": "allowed" if repo.get("allow_rebase_merge") else "disallowed",
            "flagged": bool(repo.get("allow_rebase_merge")),
            "gap": (
                "rebase merging is allowed, which replays every branch commit onto main "
                "verbatim -- `wip:` subjects included; disable it and leave squash"
            ),
        },
    ]


def _owner(repo: dict[str, Any], fallback: str | None) -> str | None:
    """The repository's owner as GitHub names it, else the remote's."""
    owner = repo.get("owner")
    login = owner.get("login") if isinstance(owner, dict) else None
    return str(login) if login else fallback


def baseline_check(ci: str | None) -> str:
    """The check the baseline requires of a repository whose `repo.ci` is `ci`."""
    return LOCAL_GATE_CHECK if ci == "local" else BASELINE_CHECK


def baseline_flags(protection: dict[str, Any] | None, owner: str | None,
                   classic_present: bool | None, ci: str | None = None,
                   ruleset_present: bool | None = None) -> list[dict[str, Any]]:
    """The two fleet-baseline flags (sd:1741, S1), or none for a repository
    outside `BASELINE_OWNERS`: its baseline is not this fleet's to set.

    `protection_source` is raised unless classic protection alone protects
    the branch (sd:5406): a ruleset, combined, none at all, a classic object
    that `_combine` left out because it gates nothing, or classic beside an
    active ruleset that gates nothing, which `_combine` reads as classic.
    `ruleset_present` says whether the branch's rules read found an active
    rule; None is a read that failed, which is no evidence that no ruleset
    stands, so it raises the flag too. `required_check` is raised unless
    `baseline_check(ci)` is among the required contexts; other names may
    stand beside it (decision D5).
    """
    if (owner or "").lower() not in BASELINE_OWNERS:
        return []
    if protection is None:
        source = "none"
    else:
        source = str(protection.get("source") or CLASSIC_SOURCE)
        if source == RULESET_SOURCE and classic_present:
            source = f"{RULESET_SOURCE}, beside a classic object"
        elif source == CLASSIC_SOURCE and ruleset_present is None:
            source = UNREAD_RULESETS
        elif source == CLASSIC_SOURCE and ruleset_present:
            source = f"{CLASSIC_SOURCE}, beside a ruleset"
    standard = "the fleet baseline is classic protection and no repository ruleset"
    gap = (f"whether a ruleset stands beside the default branch's classic protection is unknown: "
           f"the rules read failed; {standard}") if source == UNREAD_RULESETS else (
        f"the default branch is protected by {source}, not by classic protection alone; {standard}")
    checks = (protection or {}).get("required_status_checks")
    contexts = [str(name) for name in checks.get("contexts") or []] if isinstance(checks, dict) else []
    check = baseline_check(ci)
    return [
        {"id": "protection_source", "value": source, "flagged": source != CLASSIC_SOURCE, "gap": gap},
        {
            "id": "required_check",
            "value": ", ".join(contexts) or "none",
            "flagged": check not in contexts,
            "gap": (
                f"`{check}` is not a required check; the fleet baseline requires "
                f"one aggregate check named `{check}`, and other names may stand beside it"
            ),
        },
    ]


def classify(
    protection: dict[str, Any] | None,
    repo: dict[str, Any],
    default_branch: str,
    produced: set[str] | None,
    notes: list[str],
    *,
    owner: str | None = None,
    classic_present: bool | None = None,
    ci: str | None = None,
    ruleset_present: bool | None = None,
    human: int = 1,
) -> dict[str, Any]:
    """Pure. `protection` is the protection object, or None for a 404.

    Returns `{"status", "gaps", "detail", "merge_settings"}`. A 404 is
    `unprotected`, and its gaps say so: the `unprotected` sentence
    `sd-status` prints, then `required_checks` and `reviews`, because those
    are the two things a branch with no protection object does not have and
    the columns a reader compares repositories on. `enforce_admins` and
    `strict` are not gaps there -- there is no rule for admins to be exempt
    from and no check to be strict about -- and the screen shows them as not
    applicable rather than as passing.

    `merge_settings` also carries `baseline_flags`, judged for the owner the
    repository object names, else `owner` (the remote's). `classic_present`
    says a classic object stood even where the layered result reads
    `ruleset`. `ci` is the repository's `repo.ci`, which names the baseline
    check (`baseline_check`). `ruleset_present` says the rules read found an
    active rule on the branch, None when that read failed.
    """
    settings = merge_settings(repo) + baseline_flags(protection, _owner(repo, owner), classic_present, ci,
                                                     ruleset_present)
    if protection is None:
        gaps = [
            {
                "id": "unprotected",
                "gap": (
                    f"{default_branch} has no branch protection at all: nothing "
                    "that can push is stopped by anything, and no local lane can "
                    "supply the authority the config does not"
                ),
            },
            {
                "id": "required_checks",
                "gap": f"no required status checks on {default_branch}: a red PR still merges",
            },
            {
                "id": "reviews",
                "gap": f"no pull-request review is required on {default_branch}",
            },
        ]
        detail = {
            "enforce_admins": False,
            "strict": False,
            "required_contexts": [],
            "produced_contexts": sorted(produced) if produced is not None else None,
            "required_not_produced": [],
            "produced_not_required": [],
            "required_approving_review_count": 0,
            "workflow_notes": list(notes),
        }
        return {"status": "unprotected", "gaps": gaps, "detail": detail, "merge_settings": settings}
    gaps, detail = _protection_gaps(protection, default_branch, produced, notes, human)
    return {"status": "protected", "gaps": gaps, "detail": detail, "merge_settings": settings}


# ----------------------------------------------------------------- the sync


def _unknown(path: str, observed_at: str, reason: str, *, default_branch: str | None = None,
             requests: int = 0) -> dict[str, Any]:
    return {
        "repo": path,
        "observed_at": observed_at,
        "status": "unknown",
        "default_branch": default_branch,
        "reason": reason,
        "body": {"gaps": [], "detail": {}, "merge_settings": [], "requests": requests},
    }


#: The reason a non-admin's 404 is filed under: the classic endpoint is
#: unseen, not empty. Names the permission so the reader knows the remedy.
HIDDEN_BY_PERMISSION = ("token does not administer the repository: GitHub answers 404 on "
                        "classic protection it may not show, and no ruleset gates the branch")


def _administers(repo: dict[str, Any]) -> bool:
    """Whether the token has `admin` on the repository, as the repository
    object says. Absent `permissions` is not admin: the fact is unestablished."""
    permissions = repo.get("permissions")
    return isinstance(permissions, dict) and permissions.get("admin") is True


def _branch_rules(client, owner: str, name: str, branch: str) -> list[dict[str, Any]]:
    """Every page of the rules GitHub evaluates for `branch`.

    Only a 200 with a list is an answer: the endpoint answers `[]` for a
    branch no ruleset touches, so a 404 or 403 from it is a repository this
    token could not see into, never "no rules" -- the client raises
    `Unavailable` on either and the row is `unknown`. Pages are read until
    one comes back shorter than `RULES_PAGE_SIZE`; a reader that took the
    first page for the whole list would see a weaker branch than the real
    one, which is the direction a protection report must not fail in.
    """
    from .contribution_github import Unavailable

    rules: list[dict[str, Any]] = []
    for page in range(1, MAX_RULE_PAGES + 1):
        spent = _exhausted(client)
        if spent:
            raise Unavailable(spent)
        body = client.get(rules_path(owner, name, branch, page))
        if not isinstance(body, list):
            raise Unavailable("branch rules response is not a list")
        rules.extend(rule for rule in body if isinstance(rule, dict))
        if len(body) < RULES_PAGE_SIZE:
            return rules
    raise Unavailable(f"branch rules ran past {MAX_RULE_PAGES} pages of {RULES_PAGE_SIZE}")


def _ruleset_entry(client, owner: str, name: str, ruleset_id: int) -> dict[str, Any]:
    """The ruleset's name and bypass list as GitHub shows them, best-effort.

    GitHub withholds `bypass_actors` from a caller who cannot edit the
    ruleset and may withhold the object itself; either leaves the list
    `None`, which `_protection_gaps` reports as an unknown `enforce_admins`
    rather than as an empty list. A budget with nothing left reads nothing.
    """
    from .contribution_github import Unavailable

    entry: dict[str, Any] = {"id": ruleset_id, "name": str(ruleset_id), "bypass_actors": None}
    if _exhausted(client):
        return entry
    try:
        value = client.get(ruleset_path(owner, name, ruleset_id), missing=True)
    except Unavailable:
        return entry
    if isinstance(value, dict):
        entry["name"] = str(value.get("name") or ruleset_id)
        actors = value.get("bypass_actors")
        entry["bypass_actors"] = list(actors) if isinstance(actors, list) else None
    return entry


def _reaches_admins(actor: Any) -> bool | None:
    """Whether a bypass actor exempts administrators from the rules. `True`
    for an `OrganizationAdmin` entry. `None` for a `RepositoryRole`: it
    carries a numeric role id, which role that id names is confirmed
    nowhere here (no registered repository carries a role bypass this token
    can read), so whether it is the admin role or a lesser one is unknown
    and is reported as unknown rather than as either -- a guessed constant
    would pick which of two sentences an operator reads, and the wrong
    guess reads as reassurance. `False` for an app, a team, a user or a
    deploy key, which leave administrators subject to every rule, whatever
    they let that actor do."""
    if not isinstance(actor, dict):
        return False
    kind = actor.get("actor_type")
    if kind == "OrganizationAdmin":
        return True
    return None if kind == "RepositoryRole" else False


def _actor_words(actor: Any) -> str:
    """One bypass actor for a sentence: its type, its id and when it applies."""
    if not isinstance(actor, dict):
        return "an unnamed actor"
    kind = actor.get("actor_type") or "an unnamed actor"
    ident = actor.get("actor_id")
    mode = actor.get("bypass_mode") or "always"
    return f"{kind} {ident} ({mode})" if ident is not None else f"{kind} ({mode})"


def _admins_subject(lists: list[list | None]) -> bool | None:
    """`enforce_admins` asked the ruleset way round: `False` when a shown
    actor reaches administrators; `None` when a list was withheld, or a
    shown actor is a role nothing here resolves, and none shown reaches
    them; `True` when every list was shown and none does either."""
    verdicts = [_reaches_admins(actor) for actors in lists if actors for actor in actors]
    if any(verdict is True for verdict in verdicts):
        return False
    if not lists or any(actors is None for actors in lists) or any(verdict is None for verdict in verdicts):
        return None
    return True


def _bypass_pairs(protection: dict[str, Any], reaching: bool | None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Each (ruleset entry, actor) pair whose actor's `_reaches_admins` is
    `reaching`: `True` for the admin-reaching bypasses, `False` for the app,
    team, user and deploy-key ones, `None` for the roles nothing resolves.
    In a combined object only the decisive ones (`_decisive`)."""
    return [
        (entry, actor)
        for entry in protection.get("rulesets") or []
        if isinstance(entry, dict)
        for actor in entry.get("bypass_actors") or []
        if _reaches_admins(actor) is reaching and _decisive(protection, entry, reaching)
    ]


def _decisive(protection: dict[str, Any], entry: dict[str, Any], reaching: bool | None = False) -> bool:
    """Whether a bypass of ruleset `entry` removes the last enforcement of a
    rule it carries. Always, outside a combined object. Inside one, a bypass
    by an actor that does not reach administrators (`reaching` `False`) is
    decisive when a rule of `entry` has no firm source; an administrators'
    exemption, or a role nothing resolves, when a rule of `entry` has no
    source binding them. A bypass beside a firm source is `bypass_info`, not
    a gap (sd:1430, Q2). The pack's `sd_protection.decisive`."""
    if protection.get("source") != COMBINED_SOURCE:
        return True
    rules = entry.get("rules") or []
    if reaching is False:
        return any(not (protection.get("firm") or {}).get(rule) for rule in rules)
    return any((protection.get("admins") or {}).get(rule) is not True for rule in rules)


def _scope(entry: dict[str, Any]) -> str:
    """One ruleset for a sentence, with the merge-gating rules it carries:
    `guard (#7) [pull_request]`. A bypass of one ruleset exempts its holder
    from that ruleset's rules and from no other's, so a bypass named without
    the rules it reaches reads as wider than it is (sd:1327 review, Codex
    :730). An entry read without rules -- none is, since
    `_ruleset_protection` records them -- is named without the brackets."""
    named = f"{entry.get('name')} (#{entry.get('id')})"
    rules = entry.get("rules")
    return f"{named} [{', '.join(str(rule) for rule in rules)}]" if rules else named


def _scopes(entries: list[dict[str, Any]]) -> str:
    return "; ".join(_scope(entry) for entry in entries)


def _ruleset_states(protection: dict[str, Any]) -> list[dict[str, Any]]:
    """Each contributing ruleset with `admins`, whether its rules bind
    administrators: `exempt` when a shown actor reaches them, `unknown` when
    its list was withheld or a shown actor is a role nothing resolves and no
    other reaches them, `enforced` when the list was shown and none does.
    One ruleset's verdict says nothing about another's: GitHub layers
    rulesets, and a bypass on the review ruleset leaves the checks ruleset
    binding everyone, so the verdicts are kept apart rather than folded into
    the one `enforce_admins` boolean that `_admins_subject` gives the row."""
    states = []
    for entry in protection.get("rulesets") or []:
        if not isinstance(entry, dict):
            continue
        verdict = _admins_subject([entry.get("bypass_actors")])
        states.append({**entry, "admins": "unknown" if verdict is None else ("enforced" if verdict else "exempt")})
    return states


def _admin_bypass_words(protection: dict[str, Any], default_branch: str) -> str:
    """`enforce_admins` off, the ruleset way round: which ruleset exempts
    administrators, through which actor, from which rules -- and which
    rulesets still bind them, and which are not known either way. "Every
    rule below" is said only when no ruleset is left binding or unknown:
    a review ruleset the admins bypass beside a checks ruleset nobody does
    leaves the CI requirement enforced, and a sentence exempting them from
    everything is a false finding on it (sd:1327 review, Codex :730)."""
    states = _ruleset_states(protection)
    binding = [entry for entry in states if entry["admins"] == "enforced"]
    unknown = [entry for entry in states if entry["admins"] == "unknown"]
    named = "; ".join(
        f"{_scope(entry)} by {_actor_words(actor)}"
        for entry, actor in _bypass_pairs(protection, True)
    )
    words = f"enforce_admins is off on {default_branch}: {named}. "
    if not binding and not unknown:
        words += (
            "Every rule below stops collaborators and exempts the admins who do the "
            "merging. Protection that exempts admins is prose, not authority."
        )
        return words
    words += "The rules in brackets stop collaborators and exempt the admins who do the merging."
    if binding:
        words += f" Still binding them: {_scopes(binding)}."
    if unknown:
        words += (
            f" Not known either way: {_scopes(unknown)}, whose bypass list was withheld "
            "or names a role nothing here resolves."
        )
    return words


def _unknown_admins_words(protection: dict[str, Any], default_branch: str) -> str:
    """Why `enforce_admins` is unknown, each reason named: the rulesets whose
    bypass list was withheld or not read, and the role bypasses nothing here
    resolves. Unknown is a statement about this reader's knowledge; the two
    alternative sentences are claims about the repository, and neither is
    made. The pack's `sd_protection.unknown_admins_words`."""
    reasons: list[str] = []
    hidden = ", ".join(
        _scope(entry)
        for entry in protection.get("rulesets") or []
        if isinstance(entry, dict) and entry.get("bypass_actors") is None
        and _decisive(protection, entry, None)
    )
    if hidden:
        reasons.append(
            f"whether anyone can bypass the ruleset protecting {default_branch} is unknown: GitHub "
            f"did not show bypass_actors for {hidden}, which it withholds from a caller who cannot "
            "edit the ruleset. A bypass list not shown is not an empty one."
        )
    roles = "; ".join(
        f"{_scope(entry)} lets {_actor_words(actor)} bypass it"
        for entry, actor in _bypass_pairs(protection, None)
    )
    if roles:
        reasons.append(
            f"whether administrators can bypass the ruleset protecting {default_branch} is unknown: "
            f"{roles}, and which role that id names is not confirmed here. The admin role would be "
            "their exemption; a lesser role would not; neither is claimed."
        )
    return " ".join(reasons) or f"whether anyone can bypass the ruleset protecting {default_branch} is unknown."


def _parameters(rule: dict[str, Any]) -> dict[str, Any]:
    raw = rule.get("parameters")
    return raw if isinstance(raw, dict) else {}


def _cited(rules: list[dict[str, Any]]) -> list[int]:
    """The ids of the rulesets the merge-gating rules on the branch cite."""
    return sorted({rule["ruleset_id"] for rule in rules
                   if rule.get("type") in MERGE_GATING_RULES and isinstance(rule.get("ruleset_id"), int)})


def _ruleset_protection(rules: list[dict[str, Any]], entries: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    """The classic-shaped object the merge-gating rules amount to, or None
    when no rule on the branch gates a merge.

    A port of the pack's `sd_protection.synthesize`, with the one difference
    a reporter can afford and a gate cannot: `entries` is what
    `_ruleset_entry` read for each cited ruleset -- its name and bypass list
    -- and a ruleset not read yet, or not shown, leaves `enforce_admins`
    unknown rather than failing the row. The rules endpoint answers active
    rules only, so enforcement is not re-read. Where two rulesets both carry
    a rule GitHub applies the stricter, so review counts take the maximum,
    strictness the disjunction, required checks the union.
    """
    gating = [rule for rule in rules if rule.get("type") in MERGE_GATING_RULES]
    if not gating:
        return None
    # Each entry carries the gating rules it contributes: a bypass of one
    # ruleset reaches those rules and no other ruleset's (Codex :730).
    contributing = [
        {
            **(entries.get(ruleset_id) or {"id": ruleset_id, "name": str(ruleset_id), "bypass_actors": None}),
            "rules": sorted({str(rule["type"]) for rule in gating if rule.get("ruleset_id") == ruleset_id}),
        }
        for ruleset_id in _cited(rules)
    ]
    value: dict[str, Any] = {
        "source": RULESET_SOURCE,
        "rulesets": contributing,
        "enforce_admins": {"enabled": _admins_subject([entry["bypass_actors"] for entry in contributing])},
    }
    reviews = [_parameters(rule) for rule in gating if rule.get("type") == "pull_request"]
    if reviews:
        counts = [p.get("required_approving_review_count") for p in reviews]
        value["required_pull_request_reviews"] = {
            "required_approving_review_count": max([c for c in counts if isinstance(c, int)] or [0]),
        }
    checks = [_parameters(rule) for rule in gating if rule.get("type") == "required_status_checks"]
    if checks:
        contexts: list[str] = []
        for parameters in checks:
            for entry in parameters.get("required_status_checks") or []:
                if isinstance(entry, dict) and entry.get("context") and str(entry["context"]) not in contexts:
                    contexts.append(str(entry["context"]))
        value["required_status_checks"] = {
            "strict": any(p.get("strict_required_status_checks_policy") is True for p in checks),
            "contexts": contexts,
        }
    return value


# ------------------------------------- classic and rulesets together (sd:1430)


def _layered(classic: dict[str, Any] | None, rules: list[dict[str, Any]],
             entries: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    """The protection a branch amounts to: the rulesets alone after a classic
    404 (`classic` None), layered onto the classic object after a 200."""
    if classic is None:
        return _ruleset_protection(rules, entries)
    return _combine(classic, rules, entries)


def _combine(classic: dict[str, Any], rules: list[dict[str, Any]],
             entries: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Classic protection and the branch's rulesets, layered the way GitHub
    layers them: for each merge-gating rule, the strictest source wins. A
    port of the pack's `sd_protection.combine` (sd:1419).

    One contributing source keeps the object it had: `classic` itself when
    no rule on the branch gates a merge, and `_ruleset_protection`'s object
    when classic carries neither a review nor a checks requirement.
    Otherwise the object is classic-shaped, marked `source: "combined"`, and
    carries per rule `sources` (`classic` or `ruleset:<id>`), `firm` (the
    ones nobody can bypass) and `admins` (whether a source binds
    administrators: `True`, `False`, or `None` for unknown). The
    requirement is the strictest among the firm sources, or among all of
    them when none is firm -- the rule whose bypass is the `bypass` gap.
    `advisory` names a stricter source that is not firm; `bypass_info` a
    bypass that is not decisive. `enforce_admins` is `True` only when every
    rule has a source binding administrators, `False` when one has none
    that could, and `None` otherwise.
    """
    synthesized = _ruleset_protection(rules, entries)
    if synthesized is None:
        return classic
    classic_source = _classic_source(classic)
    if not classic_source["rules"]:
        return synthesized
    gating = [rule for rule in rules if rule.get("type") in MERGE_GATING_RULES]
    sources = [classic_source] + [_ruleset_source(entry, gating) for entry in synthesized["rulesets"]]
    value: dict[str, Any] = {"source": COMBINED_SOURCE, "rulesets": synthesized["rulesets"],
                             "sources": {}, "firm": {}, "admins": {}, "advisory": []}
    for rule in sorted(MERGE_GATING_RULES):
        _layer_rule(value, rule, [source for source in sources if rule in source["rules"]], classic_source)
    verdicts = list(value["admins"].values())
    value["enforce_admins"] = {"enabled": False if False in verdicts else (None if None in verdicts else True)}
    value["bypass_info"] = _bypass_info(value)
    return value


def _layer_rule(value: dict[str, Any], rule: str, imposing: list[dict[str, Any]],
                classic_source: dict[str, Any]) -> None:
    """One rule of `_combine`: its sources, its firm ones, its administrators,
    the requirement the firm sources set, and the stricter asks they do not."""
    if not imposing:
        return
    firm = [source for source in imposing if _firm(source, rule)]
    effective = _strictest(rule, [source["rules"][rule] for source in firm or imposing])
    value["sources"][rule] = [source["name"] for source in imposing]
    value["firm"][rule] = [source["name"] for source in firm]
    value["admins"][rule] = _rule_admins([source["admins"] for source in imposing])
    if firm:
        value["advisory"].extend(
            f"{source['label']} [{rule}] asks for more than the firm sources, but {source['why']}, "
            "so it is advisory and the requirement is theirs"
            for source in imposing
            if source not in firm and _strictest(rule, [effective, source["rules"][rule]]) != effective)
    if rule != "pull_request":
        value["required_status_checks"] = effective
        return
    allowances = classic_source["allowances"]
    if not firm and classic_source in imposing and allowances:
        effective = dict(effective, bypass_pull_request_allowances=allowances)
    value["required_pull_request_reviews"] = effective


def _classic_source(classic: dict[str, Any]) -> dict[str, Any]:
    """Classic protection as one source: its two merge-gating rules in the
    normalized shape `_reviews` and `_checks` give a ruleset's. A classic
    object without a `checks` list has its `contexts` stand in, bound to no
    app, so the same named check in both sources compares equal."""
    admins = classic.get("enforce_admins")
    enforced = (admins.get("enabled") if isinstance(admins, dict) else admins) is True
    found: dict[str, Any] = {}
    reviews = classic.get("required_pull_request_reviews")
    allowances: dict[str, Any] = {}
    if isinstance(reviews, dict):
        count = reviews.get("required_approving_review_count")
        found["pull_request"] = {
            "required_approving_review_count": count if isinstance(count, int) else 0,
            "dismiss_stale_reviews": reviews.get("dismiss_stale_reviews") is True,
            "require_code_owner_reviews": reviews.get("require_code_owner_reviews") is True,
            "require_last_push_approval": reviews.get("require_last_push_approval") is True,
        }
        raw = reviews.get("bypass_pull_request_allowances")
        if isinstance(raw, dict) and any(raw.get(name) for name in ("users", "teams", "apps")):
            allowances = raw
    checks = classic.get("required_status_checks")
    if isinstance(checks, dict) and checks:
        contexts = [str(name) for name in checks.get("contexts") or []]
        listed = checks.get("checks")
        bound = ([{"context": str(entry["context"]), "app_id": entry.get("app_id")}
                  for entry in listed if isinstance(entry, dict) and entry.get("context")]
                 if isinstance(listed, list) else [{"context": context, "app_id": None} for context in contexts])
        found["required_status_checks"] = {"strict": checks.get("strict") is True,
                                           "contexts": contexts, "checks": bound}
    why = "enforce_admins is off" if not enforced else "it has pull-request bypass allowances"
    return {"name": CLASSIC_SOURCE, "label": "classic protection", "rules": found, "admins": enforced,
            "allowances": allowances, "why": why}


def _ruleset_source(entry: dict[str, Any], gating: list[dict[str, Any]]) -> dict[str, Any]:
    """One contributing ruleset as a source, its rules reduced on their own."""
    mine = [rule for rule in gating if rule.get("ruleset_id") == entry["id"]]
    found: dict[str, Any] = {}
    reviews = _reviews([_parameters(rule) for rule in mine if rule.get("type") == "pull_request"])
    if reviews is not None:
        found["pull_request"] = reviews
    checks = _checks([_parameters(rule) for rule in mine if rule.get("type") == "required_status_checks"])
    if checks is not None:
        found["required_status_checks"] = checks
    actors = entry.get("bypass_actors")
    why = ("its bypass list was not shown" if actors is None
           else "it can be bypassed by " + ", ".join(_actor_words(actor) for actor in actors))
    return {"name": f"ruleset:{entry['id']}", "label": f"{entry.get('name')} (#{entry.get('id')})",
            "rules": found, "admins": _admins_subject([actors]), "actors": actors, "why": why}


def _reviews(parameter_sets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The `pull_request` rules of one ruleset, normalized, stricter wins."""
    if not parameter_sets:
        return None
    counts = [parameters.get("required_approving_review_count") for parameters in parameter_sets]
    return {
        "required_approving_review_count": max([count for count in counts if isinstance(count, int)] or [0]),
        "dismiss_stale_reviews": any(p.get("dismiss_stale_reviews_on_push") is True for p in parameter_sets),
        "require_code_owner_reviews": any(p.get("require_code_owner_review") is True for p in parameter_sets),
        "require_last_push_approval": any(p.get("require_last_push_approval") is True for p in parameter_sets),
    }


def _checks(parameter_sets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The `required_status_checks` rules of one ruleset, normalized: the
    union of the named checks, `integration_id` carried as `app_id`."""
    if not parameter_sets:
        return None
    contexts: list[str] = []
    checks: list[dict[str, Any]] = []
    for parameters in parameter_sets:
        for entry in parameters.get("required_status_checks") or []:
            if not isinstance(entry, dict) or not entry.get("context") or str(entry["context"]) in contexts:
                continue
            contexts.append(str(entry["context"]))
            checks.append({"context": str(entry["context"]), "app_id": entry.get("integration_id")})
    return {"strict": any(p.get("strict_required_status_checks_policy") is True for p in parameter_sets),
            "contexts": contexts, "checks": checks}


def _firm(source: dict[str, Any], rule: str) -> bool:
    """Whether nobody can bypass `source` for `rule`."""
    if source["name"] == CLASSIC_SOURCE:
        return source["admins"] is True and (rule != "pull_request" or not source["allowances"])
    return source.get("actors") == []


def _rule_admins(verdicts: list[bool | None]) -> bool | None:
    """Administrators on one rule: bound when any source imposing it binds
    them, exempt when every source lets them past, else unknown (Q1)."""
    if True in verdicts:
        return True
    return None if None in verdicts else False


def _strictest(rule: str, parameter_sets: list[dict[str, Any]]) -> dict[str, Any]:
    """The normalized requirement for `rule` that satisfies every set given."""
    if rule == "pull_request":
        return {
            "required_approving_review_count": max(p["required_approving_review_count"] for p in parameter_sets),
            "dismiss_stale_reviews": any(p["dismiss_stale_reviews"] for p in parameter_sets),
            "require_code_owner_reviews": any(p["require_code_owner_reviews"] for p in parameter_sets),
            "require_last_push_approval": any(p["require_last_push_approval"] for p in parameter_sets),
        }
    contexts: list[str] = []
    checks: list[dict[str, Any]] = []
    for parameters in parameter_sets:
        for context in parameters["contexts"]:
            if context not in contexts:
                contexts.append(context)
        for entry in parameters["checks"]:
            if entry["context"] not in [known["context"] for known in checks]:
                checks.append(dict(entry))
    return {"strict": any(p["strict"] for p in parameter_sets), "contexts": contexts, "checks": checks}


def _bypass_info(value: dict[str, Any]) -> list[str]:
    """The bypasses of a combined object that remove no last enforcement:
    `main (#42) [pull_request]: Integration 77 (pull_request)`, and a
    withheld list as `... : bypass_actors not shown`. Information, sorted."""
    words = []
    for entry in value.get("rulesets") or []:
        actors = entry.get("bypass_actors")
        if actors is None:
            if not _decisive(value, entry, False):
                words.append(f"{_scope(entry)}: bypass_actors not shown")
            continue
        words.extend(f"{_scope(entry)}: {_actor_words(actor)}" for actor in actors
                     if not _decisive(value, entry, _reaches_admins(actor)))
    return sorted(words)


def _combined_admins_words(protection: dict[str, Any], default_branch: str) -> str:
    """`enforce_admins` off on a combined object: the rules no source binds
    administrators on, each with why every source lets them past, then the
    rules a source still binds them on. The pack's
    `sd_protection.combined_admins_words`."""
    names = {CLASSIC_SOURCE: "classic protection (enforce_admins off)"}
    for entry in protection.get("rulesets") or []:
        actors = [_actor_words(actor) for actor in entry.get("bypass_actors") or [] if _reaches_admins(actor)]
        names[f"ruleset:{entry.get('id')}"] = f"{_scope(entry)} by {', '.join(actors) or 'an actor'}"
    verdicts = sorted((protection.get("admins") or {}).items())
    exempt = [rule for rule, verdict in verdicts if verdict is False]
    binding = [rule for rule, verdict in verdicts if verdict is True]
    detail = "; ".join(f"{rule}: " + ", ".join(names.get(name, name) for name in protection["sources"][rule])
                       for rule in exempt)
    words = (f"enforce_admins is off on {default_branch} for {', '.join(exempt)}: every source of "
             f"{'that rule' if len(exempt) == 1 else 'those rules'} exempts the admins who do the merging "
             f"({detail}). Protection that exempts admins is prose, not authority.")
    if binding:
        words += f" Still binding them: {', '.join(binding)}."
    return words


def _exhausted(client) -> str | None:
    """The `BUDGET_EXHAUSTED` reason naming the limit that ran out, or None."""
    budget = client.budget
    if budget.remaining <= 0:
        return REQUESTS_EXHAUSTED
    if budget.deadline - time.monotonic() <= 0:
        return TIME_EXHAUSTED
    return None


def _spent_reason(client, error: Exception) -> str:
    """The reason for an `Unavailable`: the limit that ran out when the
    client refused on the budget, the client's own words otherwise."""
    reason = str(error)
    if BUDGET_EXHAUSTED in reason:
        return _exhausted(client) or BUDGET_EXHAUSTED
    return reason


#: What reading a body of the wrong shape raises. A malformed body is
#: `unknown` with `_malformed`'s reason, wherever in the reading it surfaces.
MALFORMED = (KeyError, TypeError, ValueError, AttributeError)


def _malformed(error: Exception) -> str:
    return f"malformed API response ({type(error).__name__})"


def observe(client, path: str, owner: str, name: str, *, observed_at: str,
            ci: str | None = None, seen: dict[str, Any] | None = None, human: int = 1) -> dict[str, Any]:
    """One repository's basic observation: the repository, its classic
    protection and the branch's rules, and a row whatever they answer.

    A row protected by a ruleset, alone or beside classic protection,
    carries `_enrich`, what `enrich` needs to read the bypass lists later:
    `sync` makes every repository's basic observation first and spends what
    is left on those, so a bypass lookup never takes the requests reserved
    for a repository after it.

    `Unavailable` from the client -- a 403, a timeout, a body that is not
    JSON, the shared budget running out -- is `unknown` with the client's
    reason, and so is a JSON body whose shape the reading or `classify`
    cannot use (`MALFORMED`), with the error's type as the reason. The
    budget is checked before each call so a repository the budget never
    reached says `budget exhausted` rather than a request it did not make.
    The one exception is the rules read beside a classic 200: it fails into
    `rules_read_error` in the detail and the classic result stands (sd:1430).

    `seen`, when given, receives the repository object as `repo` once it is
    read, whatever the row says after it: `alerts` reads it later.
    """
    from .contribution_github import Unavailable

    before = client.budget.requests
    default_branch: str | None = None
    try:
        spent = _exhausted(client)
        if spent:
            return _unknown(path, observed_at, spent)
        repo = client.get(f"repos/{owner}/{name}")
        if not isinstance(repo, dict):
            raise Unavailable("repository response is not an object")
        if seen is not None:
            seen["repo"] = repo
        if not isinstance(repo.get("default_branch"), str) or not repo["default_branch"]:
            # Read as `main`, a 200 there was `protected` for a branch the
            # repository never named (sd:1204).
            raise Unavailable("repository response has no default branch")
        default_branch = repo["default_branch"]
        spent = _exhausted(client)
        if spent:
            return _unknown(path, observed_at, spent, default_branch=default_branch,
                            requests=client.budget.requests - before)
        protection = client.get(protection_path(owner, name, default_branch), missing=True)
        if protection is not None and not isinstance(protection, dict):
            raise Unavailable("protection response is not an object")
        if protection == {}:
            # A 200 always carries at least its `url`; an empty object is
            # not protection nobody configured (sd:1204).
            raise Unavailable("protection response is an empty object")
        rules: list[dict[str, Any]] = []
        classic = protection
        rules_error: str | None = None
        if classic is None:
            # The 404 is judged only after the rules answer: a branch a
            # ruleset gates answers 404 here to its admin too.
            rules = _branch_rules(client, owner, name, default_branch)
        else:
            # Beside a 200 the rulesets layer onto classic (sd:1430). A read
            # that fails keeps the classic result: layering only adds
            # requirements, so classic alone is never reported stronger.
            try:
                rules = _branch_rules(client, owner, name, default_branch)
            except Unavailable as error:
                rules_error = _spent_reason(client, error)
        protection = _layered(classic, rules, {})
        if protection is None and not _administers(repo):
            # The 404 of a token that may not look, not of a bare branch.
            return _unknown(path, observed_at, HIDDEN_BY_PERMISSION, default_branch=default_branch,
                            requests=client.budget.requests - before)
    except Unavailable as error:
        return _unknown(path, observed_at, _spent_reason(client, error), default_branch=default_branch,
                        requests=client.budget.requests - before)
    except MALFORMED as error:
        return _unknown(path, observed_at, _malformed(error),
                        default_branch=default_branch, requests=client.budget.requests - before)
    produced, notes = produced_contexts(path, ci=ci)
    try:
        # Inside the guard, not after it: a body whose top level has the
        # right type can still carry a nested value classify cannot read,
        # and that is the malformed body the module docstring files as
        # unknown (sd:1359). Outside, it raised through `sync`.
        result = classify(protection, repo, default_branch, produced, notes,
                          owner=owner, classic_present=classic is not None, ci=ci,
                          ruleset_present=None if rules_error is not None else bool(rules), human=human)
    except MALFORMED as error:
        return _unknown(path, observed_at, _malformed(error),
                        default_branch=default_branch, requests=client.budget.requests - before)
    if rules_error is not None:
        result["detail"]["rules_read_error"] = rules_error
    row: dict[str, Any] = {
        "repo": path,
        "observed_at": observed_at,
        "status": result["status"],
        "default_branch": default_branch,
        "reason": None,
        "body": {
            "gaps": result["gaps"],
            "detail": result["detail"],
            "merge_settings": result["merge_settings"],
            "requests": client.budget.requests - before,
        },
    }
    if isinstance(protection, dict) and protection.get("source") in (RULESET_SOURCE, COMBINED_SOURCE):
        row["_enrich"] = {"owner": owner, "name": name, "rules": rules, "repo": repo, "classic": classic,
                          "default_branch": default_branch, "produced": produced, "notes": notes,
                          "ci": ci, "human": human}
    return row


def enrich(client, row: dict[str, Any]) -> None:
    """Read the bypass lists a ruleset-protected row is still missing, and
    re-classify it with them, layered onto its classic object when it has
    one. Spends only what the budget has left: a ruleset it cannot afford
    stays unread, its bypass unknown, and the row keeps its basic
    observation. Removes `_enrich` either way.
    """
    pending = row.pop("_enrich", None)
    if not pending:
        return
    before = client.budget.requests
    entries = {ruleset_id: _ruleset_entry(client, pending["owner"], pending["name"], ruleset_id)
               for ruleset_id in _cited(pending["rules"])}
    protection = _layered(pending["classic"], pending["rules"], entries)
    result = classify(protection, pending["repo"], pending["default_branch"], pending["produced"], pending["notes"],
                      owner=pending["owner"], classic_present=pending["classic"] is not None,
                      ci=pending["ci"], ruleset_present=bool(pending["rules"]), human=pending["human"])
    row["status"] = result["status"]
    row["body"]["gaps"] = result["gaps"]
    row["body"]["detail"] = result["detail"]
    row["body"]["merge_settings"] = result["merge_settings"]
    row["body"]["requests"] += client.budget.requests - before


def alert_path(owner: str, name: str, kind: str) -> str:
    """One page of open alerts; `kind` is `dependabot` or `secret-scanning`."""
    return f"repos/{owner}/{name}/{kind}/alerts?state=open&per_page={ALERT_PAGE_SIZE}"


def _open_alerts(client, path: str, *, severity: bool = False) -> dict[str, Any]:
    """`{open, more}` from one page, with `severity` counts and each alert's summary (`_alert_summary`) when asked,
    or `{reason}`."""
    from .contribution_github import Unavailable

    spent = _exhausted(client)
    if spent:
        return {"reason": spent}
    try:
        page, headers = client.request(path)
        if not isinstance(page, list):
            raise Unavailable("alert list is not a list")
        if any(not isinstance(alert, dict) or type(alert.get("number")) is not int for alert in page):
            # Counted, an entry that is not an alert would read as an open finding.
            raise Unavailable("alert list holds an entry that is not an alert")
        found: dict[str, Any] = {"open": len(page), "more": 'rel="next"' in headers.get("link", "")}
        if severity:
            counts: dict[str, int] = {}
            for alert in page:
                level = alert["security_advisory"]["severity"]
                level = level if isinstance(level, str) else "unknown"
                counts[level] = counts.get(level, 0) + 1
            found["severity"] = counts
            found["summaries"] = [_alert_summary(alert) for alert in page]
    except Unavailable as error:
        return {"reason": _spent_reason(client, error)}
    except MALFORMED as error:
        return {"reason": _malformed(error)}
    return found


def _alert_text(value: Any, *keys: str) -> str | None:
    """The non-empty string at `keys` under `value`, or None for any other shape."""
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, str) and value else None


def _alert_summary(alert: dict[str, Any]) -> dict[str, Any]:
    """What the GitHub page's Dependabot view shows of one alert (pack sd:7515), from the page already read.

    A field the alert lacks or holds in another shape is None, so one odd alert never fails the read; the severity is
    the count's. Only a github.com link is kept: the page opens it in a new tab.
    """
    level = _alert_text(alert, "security_advisory", "severity") or "unknown"
    summary = _alert_text(alert, "security_advisory", "summary")
    if summary is not None and len(summary) > ALERT_SUMMARY_CHARS:
        summary = summary[:ALERT_SUMMARY_CHARS - 1] + "…"
    url = _alert_text(alert, "html_url")
    return {"number": alert["number"], "severity": level,
            "package": _alert_text(alert, "dependency", "package", "name"),
            "ecosystem": _alert_text(alert, "dependency", "package", "ecosystem"),
            "manifest": _alert_text(alert, "dependency", "manifest_path"), "summary": summary,
            "ghsa": _alert_text(alert, "security_advisory", "ghsa_id"), "cve": _alert_text(alert, "security_advisory", "cve_id"),
            "patched": _alert_text(alert, "security_vulnerability", "first_patched_version", "identifier"),
            "url": url if url is not None and url.startswith("https://github.com/") else None,
            "created_at": _alert_text(alert, "created_at")}


def _dependabot(client, owner: str, name: str, repo: dict[str, Any]) -> dict[str, Any]:
    """`_open_alerts` for Dependabot, or `{setting: "disabled"}` when the read
    was refused and GitHub says the alerts are off.

    GitHub refuses the read with a 403 for a repository whose alerts are off,
    but a 403 alone does not say why. `vulnerability-alerts` answers 404 when
    they are off and 204 when they are on, and only to an admin, so without
    `admin` it is not asked. A 204, a failure or a spent budget keeps the 403.
    """
    from .contribution_github import Unavailable

    found = _open_alerts(client, alert_path(owner, name, "dependabot"), severity=True)
    if found != {"reason": ALERTS_FORBIDDEN} or not _administers(repo) or _exhausted(client):
        return found
    try:
        probed, _ = client.request(f"repos/{owner}/{name}/vulnerability-alerts", missing=True)
    except Unavailable:
        return found
    return {"setting": "disabled"} if probed is None else found


def alerts(client, owner: str, name: str, repo: dict[str, Any]) -> dict[str, Any]:
    """A managed repository's `body.alerts`: `dependabot` and `secret_scanning`.

    `dependabot` is `{archived: true}` for an archived repository, which
    Health leaves out, else `_dependabot`: `{setting: "disabled"}` when its
    alerts are off, else `_open_alerts`. `secret_scanning` carries the
    `visibility`; for a public repository also the `setting` from
    `security_and_analysis` -- None when the object does not show it, which
    it does only to an admin -- and, when the setting is `enabled`, the open
    alerts. A private repository is not scanned, by policy, and reads nothing.
    """
    dependabot = ({"archived": True} if repo.get("archived") is True else
                  _dependabot(client, owner, name, repo))
    private = repo.get("private")
    if private is True:
        return {"dependabot": dependabot, "secret_scanning": {"visibility": "private"}}
    if private is not False:
        return {"dependabot": dependabot, "secret_scanning": {"reason": "repository response has no visibility"}}
    analysis = repo.get("security_and_analysis")
    scanning = analysis.get("secret_scanning") if isinstance(analysis, dict) else None
    setting = scanning.get("status") if isinstance(scanning, dict) else None
    secret: dict[str, Any] = {"visibility": "public", "setting": setting if isinstance(setting, str) else None}
    if secret["setting"] is None:
        secret["reason"] = "security_and_analysis not shown: the token does not administer the repository"
    elif secret["setting"] == "enabled":
        secret.update(_open_alerts(client, alert_path(owner, name, "secret-scanning")))
    return {"dependabot": dependabot, "secret_scanning": secret}


def sync(connection: sqlite3.Connection, *, client, observed_at: str) -> dict[str, Any]:
    """Every registered GitHub repository, observed and written in one transaction.

    A `repo` row whose remote is not github.com is skipped and nothing is
    written for it. Returns `{attempted, protected, unprotected, unknown,
    requests, errors}`; `errors` names each `unknown` with its reason.

    **Resumable (sd:1663).** Repositories are observed in `_sweep_order`:
    managed ones first, and within each group the ones the last sweep did
    not reach -- no row, or a `budget exhausted` one -- before the ones it
    did, oldest observation first. A repository this sweep does not reach
    keeps the row an earlier sweep observed; only a repository with no row
    gets the `budget exhausted` one, so the screen says why it has no
    answer. Nothing observed is overwritten by "not reached", and the next
    sweep starts where this one stopped.
    """
    summary: dict[str, Any] = {
        "attempted": 0, "protected": 0, "unprotected": 0, "unknown": 0,
        "requests": 0, "errors": [],
    }
    before = client.budget.requests
    rows = []
    for registered in _sweep_order(connection):
        slug = github_slug(registered["remote"])
        if slug is None:
            continue
        summary["attempted"] += 1
        seen: dict[str, Any] = {}
        row = observe(client, str(registered["path"]), *slug, observed_at=observed_at, ci=policy_ci(registered["declarations"]),
                      seen=seen, human=policy_human(registered["declarations"]))
        rows.append((slug, row, registered, seen.get("repo")))
    # Every repository has its basic observation before any bypass list is
    # read: the basic observation is three a repository, a gating ruleset
    # costs a fourth, and a first pass that spent it would take it from the
    # last repository's three and file that one `budget exhausted` on nothing.
    for _slug, row, _registered, _repo in rows:
        enrich(client, row)
    # Alerts last, for managed repositories whose repository object was read
    # (sd:2205, sd:2206): from what is left, as the bypass reads are.
    for slug, row, registered, repo in rows:
        if registered["managed"] and repo is not None:
            row["body"]["alerts"] = alerts(client, *slug, repo)
    written = []
    for slug, row, registered, _repo in rows:
        kept = _not_reached(row) and registered["kept_at"] is not None and not _not_reached(registered)
        if kept:
            # The earlier observation stands, and is counted as what it says.
            status = registered["kept_status"] if registered["kept_status"] in STATUSES else "unknown"
            summary[status] += 1
            summary["errors"].append(f"{slug[0]}/{slug[1]}: {row['reason']}; "
                                     f"kept the observation of {registered['kept_at']}")
            continue
        written.append(row)
        summary[row["status"]] += 1
        if row["status"] == "unknown":
            summary["errors"].append(f"{slug[0]}/{slug[1]}: {row['reason']}")
    summary["requests"] = client.budget.requests - before
    with transaction(connection):
        for row in written:
            # An upsert spelled so `tests/test_schema.py`'s grep for the
            # tables the library writes reads this one too.
            connection.execute(
                "INSERT INTO repo_protection "
                "(repo, observed_at, status, default_branch, reason, body) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(repo) DO UPDATE SET "
                "observed_at = excluded.observed_at, status = excluded.status, "
                "default_branch = excluded.default_branch, reason = excluded.reason, "
                "body = excluded.body",
                (row["repo"], row["observed_at"], row["status"], row["default_branch"],
                 row["reason"], json.dumps(row["body"], sort_keys=True)),
            )
    return summary


def _not_reached(row) -> bool:
    """Whether `row` records a sweep that ran out before it had an answer."""
    reason = row["reason"]
    return isinstance(reason, str) and reason.startswith(BUDGET_EXHAUSTED)


def _sweep_order(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Registered repositories in the order `sync` observes them, each with
    its `ci` and its stored row's `kept_at`, `kept_status` and `reason` (all
    None when there is none): managed first, then not reached before reached, then
    oldest observation first, then path."""
    return list(connection.execute(
        "SELECT repo.path, repo.remote, repo.declarations, repo.managed, p.observed_at AS kept_at, p.status AS kept_status, p.reason "
        "FROM repo LEFT JOIN repo_protection p ON p.repo = repo.path "
        "ORDER BY repo.managed DESC, "
        "(p.repo IS NULL OR COALESCE(p.reason, '') LIKE ? || '%') DESC, "
        "p.observed_at ASC, repo.path",
        (BUDGET_EXHAUSTED,),
    ))


# ----------------------------------------------------------------- the read


def rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every registered repository with its last observation, for the screen.

    A left join: a repository with no `repo_protection` row yet is `unknown`
    with `not yet observed`, so the table never has fewer rows than the
    registry and never lets an unobserved repository pass for a safe one.

    A checkout with no row borrows the row of a sibling checkout of the same
    GitHub repository, the latest observed, and names it in `borrowed_from`
    (sd:1607): the table is keyed by path, and protection belongs to the
    remote. The borrowed row brings the sibling's `ci` with it, since its
    flags were judged against that. A checkout with a row of its own keeps it.
    """
    found = list(connection.execute(
        "SELECT repo.path, repo.remote, repo.declarations, repo.managed, p.repo AS observed, p.observed_at, p.status, "
        "p.default_branch, p.reason, p.body FROM repo LEFT JOIN repo_protection p ON p.repo = repo.path "
        "ORDER BY repo.path"
    ))
    lenders: dict[tuple[str, str], sqlite3.Row] = {}
    for row in found:
        slug = github_slug(row["remote"])
        if slug is None or row["observed"] is None:
            continue
        key = (slug[0].lower(), slug[1].lower())
        if key not in lenders or row["observed_at"] > lenders[key]["observed_at"]:
            lenders[key] = row
    out = []
    for own in found:
        slug = github_slug(own["remote"])
        lender = lenders.get((slug[0].lower(), slug[1].lower())) if slug and own["observed"] is None else None
        row = lender or own
        try:
            body = json.loads(row["body"]) if row["body"] else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        status = row["status"] if row["status"] in STATUSES else "unknown"
        out.append({
            "repo": own["path"],
            "remote": own["remote"],
            # The screen names the baseline check from it: `baseline_check(ci)` (sd:2509).
            "ci": policy_ci(row["declarations"]),
            "slug": f"{slug[0]}/{slug[1]}" if slug else None,
            "borrowed_from": lender["path"] if lender else None,
            "status": status,
            "observed_at": row["observed_at"],
            "default_branch": row["default_branch"],
            "reason": row["reason"] if row["status"] is not None else NOT_OBSERVED,
            "gaps": [gap for gap in body.get("gaps") or [] if isinstance(gap, dict)],
            "detail": body.get("detail") if isinstance(body.get("detail"), dict) else {},
            "merge_settings": [flag for flag in body.get("merge_settings") or []
                               if isinstance(flag, dict)],
            "requests": body.get("requests"),
            # Health's Dependencies and Security areas (sd:2205, sd:2206).
            "managed": bool(own["managed"]),
            "alerts": body.get("alerts") if isinstance(body.get("alerts"), dict) else None,
        })
    return out
