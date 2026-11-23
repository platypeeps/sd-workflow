"""GitHub transport and fresh merge guards, with no progress tracking writes."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import sd_lib
import sd_protection
from sd_ship_workflow import blocked

COPILOT_REVIEWER = "copilot-pull-request-reviewer[bot]"
COPILOT_LOGINS = frozenset({COPILOT_REVIEWER, "copilot-pull-request-reviewer", "copilot"})

#: One page of a pull request's review threads, each with its first comment (sd:3098).
REVIEW_THREADS = """query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { isResolved path line originalLine comments(first: 1) { nodes { author { login } body } } }
    } } } }"""


def gate_creator(status: dict) -> str:
    """The lower-cased login that posted a commit status, or "" when GitHub named none."""
    creator = status.get("creator")
    login = creator.get("login") if isinstance(creator, dict) else None
    return login.lower() if isinstance(login, str) else ""


class Refusal(Exception):
    """A failed or uncertain prerequisite; never authority to merge."""

    def __init__(self, message: str, *, code: str = "prerequisite_failed", boundary: str = "policy",
                 next_action: str = "Inspect the error and resolve the failed prerequisite.",
                 state: str = "policy_block", approval_required: bool = False, details: dict | None = None):
        self.workflow = blocked(code, boundary, next_action, state=state, approval_required=approval_required)
        # Structured fields the JSON result carries beside `error`, such as `findings` (sd:2102).
        self.details = details or {}
        super().__init__(message)


def completed_process(root: Path, argv: list[str], *, input: str | None = None, timeout: int = 60,
                      answers: frozenset[int] = frozenset({0})) -> subprocess.CompletedProcess:
    """`argv` in `root`; a retryable runtime refusal for any exit outside `answers`.

    `answers` lets a command that replies by exit status (`--is-ancestor`)
    keep its "no" apart from a failure, under the same refusals `run` raises.
    """
    try:
        result = subprocess.run(argv, cwd=root, input=input, text=True,
                                capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise Refusal(f"{argv[0]} could not finish: {error}", code="command_unavailable", boundary="runtime",
                      state="retryable_failure", next_action="Restore command access, then retry this command.") from None
    if result.returncode not in answers:
        raise Refusal((result.stderr or result.stdout or f"{argv[0]} failed").strip()[:2000],
                      code="command_failed", boundary="runtime", state="retryable_failure",
                      next_action="Inspect the command error, resolve its cause, then retry.")
    return result


def unread_threads(number: int, reason: str) -> Refusal:
    return Refusal(f"the review threads on #{number} could not be read: {reason}", code="review_threads_unread",
                   boundary="runtime", state="retryable_failure",
                   next_action="Restore GitHub access, then retry merge; it does not go out with its threads unread.")


def run(root: Path, argv: list[str], *, input: str | None = None, timeout: int = 60) -> str:
    return completed_process(root, argv, input=input, timeout=timeout).stdout.strip()


def git(root: Path, *args: str) -> str:
    return run(root, ["git", *args])


def refuse_behind(pull: dict, base: str) -> None:
    """Name GitHub's BEHIND answer, which strict protection gives a branch missing `base` (sd:2023)."""
    if pull.get("mergeable_state") == "behind":
        raise Refusal(f"the pull request is BEHIND {base}: strict branch protection requires the branch "
                      f"to contain the current {base}", code="base_moved", boundary="ci", state="retryable_failure",
                      next_action=catch_up_action(base))


def catch_up_action(base: str) -> str:
    """What a branch behind `base` does next, on the machine that prepared it."""
    return (f"Run sd-ship prepare --catch-up, which merges origin/{base} into the branch and "
            "reviews the new head, then merge with the new --expected-head.")


#: The `mergeable_state` answers under which GitHub allows the merge (sd:2075).
#: `unstable` is a required-checks pass with some other check pending or
#: failed; the required checks are read below, and under a declared gap
#: `every_check` still refuses any check that is not passing.
MERGEABLE_STATES = ("clean", "unstable")

PASSING = ("success", "neutral", "skipped")
CI_NEXT_ACTION = "Wait for or fix exact-head CI, then retry merge."


def ci_next_action(runs: list[dict], head: str) -> str:
    """What to do about a failed check whose `runs` at `head` share its name (sd:1779).

    A body edit or a close/reopen starts a second check suite on the head,
    and its pass does not clear the first suite's failure: every run is
    judged. Re-running the failure replays its original event, stale body
    included, so when one suite fails and another passes, the remedy is a
    fresh commit. The hint changes the words only, never the decision.
    """
    def suites(passing: bool) -> set:
        return {(entry.get("check_suite") or {}).get("id") for entry in runs
                if entry.get("head_sha") == head and entry.get("status") == "completed"
                and (entry.get("conclusion") in PASSING) == passing
                and type((entry.get("check_suite") or {}).get("id")) is int}

    failed, passed = suites(False), suites(True)
    if failed and passed - failed:
        return ("A run of this check passed in another check suite on this head, which does not clear the failed one, "
                "and re-running the failure replays its stale event; push a fresh commit so every check runs again, "
                "then retry merge.")
    return CI_NEXT_ACTION


def slug(remote: str) -> str:
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", remote)
    if not match:
        raise Refusal("origin must identify one github.com repository")
    return match[1].lower()


class GitHub:
    def __init__(self, root: Path, repository: str):
        self.root, self.repository = root, repository
        self.prefix = f"repos/{repository}"
        #: What the remote last said to `owned`, so the caller that catches
        #: its refusal can write the demotion note with the same reason.
        self.answer: sd_lib.RemoteAnswer | None = None
        #: The repository object `owned` last read, kept only while it names
        #: this repository, is not a fork and grants admin. `None` otherwise,
        #: including before the first call. `sd-ship`'s merge gate merges
        #: against it when the repository row overrides the co-ownership
        #: answer, so it must never stand for a repository those three
        #: clauses did not clear (sd:1347).
        self.metadata: dict | None = None
        #: Why the last `declared_gap` read found no declaration, for the refusal.
        self.declaration_faults: list[str] = []
        #: `repo.ci` for this repository, set by the caller that read the row.
        #: `local` swaps the `pull_request` runs for the `sd/local-gate` status.
        self.ci = "github"
        #: The head the policy's Jenkins job built green (sd:6880): it
        #: validates the head as a check run would, under a declared gap.
        self.jenkins_green: str | None = None
        #: The authenticated account, read once by `viewer_login`.
        self.viewer: str | None = None

    def viewer_login(self) -> str:
        """The login this token authenticates as, the only author a local gate status may have."""
        if self.viewer is None:
            user = self.api("user")
            login = user.get("login") if isinstance(user, dict) else None
            if not isinstance(login, str) or not login:
                raise Refusal("GitHub did not name the authenticated account", code="command_failed",
                              boundary="runtime", state="retryable_failure",
                              next_action="Check `gh auth status`, then retry.")
            self.viewer = login
        return self.viewer

    def api(self, path: str, *, method: str = "GET", body: dict | None = None) -> Any:
        argv = ["gh", "api", path, "--method", method]
        if body is not None:
            argv += ["--input", "-"]
        try:
            return json.loads(run(self.root, argv, input=json.dumps(body) if body is not None else None))
        except ValueError:
            raise Refusal("GitHub returned unreadable JSON") from None

    def api_status(self, path: str, *, method: str = "GET", body: dict | None = None) -> tuple[int, Any]:
        """`(HTTP status, body)` for a call whose non-2xx answer is still an answer.

        A train's merge PUT reads it too: a 4xx there is GitHub's word that
        it merged nothing, where a 5xx or no answer may hide a merge (sd:9923).

        `api` folds every failure into one refusal, which is right everywhere
        but the protection endpoints: there a 404 means "no classic protection
        object", a fact the ruleset read and then the declared-gap rule act
        on, while a 401, 403 or 5xx means "could not observe", which must
        stay a refusal. That reading of a 404 holds for the token `owned()`
        has already shown to administer the repository, and for no other:
        GitHub answers 404 to a non-admin for protection it may not show,
        which is why `merge_ownership` runs before `gate`. The status comes from
        the response line `gh api --include` prints, not from the wording of
        gh's stderr, so a reworded message cannot turn a refusal into a fact.
        """
        argv = ["gh", "api", "--include", path, "--method", method, *(["--input", "-"] if body is not None else [])]
        try:
            result = subprocess.run(argv, cwd=self.root, text=True, capture_output=True, timeout=60, check=False,
                                    input=json.dumps(body) if body is not None else None)
        except (OSError, subprocess.SubprocessError) as error:
            raise Refusal(f"gh could not finish: {error}", code="command_unavailable", boundary="runtime",
                          state="retryable_failure", next_action="Restore command access, then retry this command.") from None
        head, _, body = result.stdout.partition("\n\n")
        match = re.match(r"HTTP/[\d.]+ (\d{3})", head)
        if not match:
            raise Refusal((result.stderr or "gh api returned no HTTP status line").strip()[:2000],
                          code="command_failed", boundary="runtime", state="retryable_failure",
                          next_action="Inspect the command error, resolve its cause, then retry.")
        try:
            return int(match[1]), json.loads(body)
        except ValueError:
            raise Refusal("GitHub returned unreadable JSON") from None

    def pages(self, path: str, field: str | None = None) -> list:
        result = []
        for page in range(1, 101):
            value = self.api(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
            rows = value.get(field) if field and isinstance(value, dict) else value
            if not isinstance(rows, list):
                raise Refusal(f"GitHub did not enumerate {path}")
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise Refusal(f"GitHub inventory exceeded the bounded pagination limit: {path}")

    def owned(self) -> dict:
        """Reuse WORKFLOW's three-question predicate with complete pagination."""
        metadata: dict = {}

        def ask(path: str, _root: Path):
            nonlocal metadata
            try:
                path = path.replace("{owner}/{repo}", self.repository)
                if path.endswith("/collaborators"):
                    people = self.pages(path)
                    if any(not isinstance(person, dict) or not isinstance(person.get("permissions"), dict)
                           or type(person["permissions"].get("push")) is not bool for person in people):
                        raise Refusal("GitHub collaborator permissions are incomplete")
                    return people, ""
                value = self.api(path)
                if path == self.prefix:
                    metadata = value
                return value, ""
            except Refusal as error:
                return None, str(error)

        answer = self.answer = sd_lib.remote_permits_full(self.root, ask=ask)
        # Identity, fork and admin are separated from the answer itself so the
        # caller that catches this refusal can still see a repository these
        # three clauses cleared. The disjunction below is unchanged: the same
        # calls refuse, with the same reason (sd:1347).
        self.metadata = metadata if (str(metadata.get("full_name", "")).lower() == self.repository
                                     and metadata.get("fork") is False
                                     and metadata.get("permissions", {}).get("admin") is True) else None
        if not answer.full or self.metadata is None:
            raise Refusal(answer.reason or "GitHub ownership did not match origin", code="ownership_refused",
                          next_action="Resolve repository ownership or use the existing authorized manual workflow.")
        return metadata

    def pull(self, number: int) -> dict:
        value = self.api(f"{self.prefix}/pulls/{number}")
        if not isinstance(value, dict) or value.get("number") != number:
            raise Refusal("GitHub did not return the requested pull request")
        return value

    def unresolved_threads(self, number: int) -> list[dict]:
        """Every unresolved review thread on pull request `number`, outdated ones too (sd:3098).

        A read that fails or answers in a shape this does not know refuses:
        a thread list that was not read is never an empty one.
        """
        owner, name = self.repository.split("/", 1)
        threads: list[dict] = []
        after = None
        for _ in range(100):
            try:
                value = self.api("graphql", method="POST", body={"query": REVIEW_THREADS, "variables": {
                    "owner": owner, "name": name, "number": number, "after": after}})
            except Refusal as error:
                raise unread_threads(number, str(error)) from None
            try:
                page = value["data"]["repository"]["pullRequest"]["reviewThreads"]
                nodes, more, after = page["nodes"], page["pageInfo"]["hasNextPage"], page["pageInfo"]["endCursor"]
            except (KeyError, TypeError):
                raise unread_threads(number, "GitHub answered without them") from None
            if value.get("errors") or not isinstance(nodes, list) or type(more) is not bool \
                    or any(not isinstance(node, dict) or type(node.get("isResolved")) is not bool for node in nodes):
                raise unread_threads(number, "GitHub answered with errors or an unknown shape")
            threads.extend(node for node in nodes if not node["isResolved"])
            if not more:
                return threads
            if not isinstance(after, str) or not after:
                raise unread_threads(number, "GitHub named no cursor for the next page")
        raise unread_threads(number, "they exceeded the bounded pagination limit")

    @staticmethod
    def is_copilot(record: object) -> bool:
        if not isinstance(record, dict):
            return False
        user = record.get("user")
        login = str(user.get("login") or "").lower() if isinstance(user, dict) else ""
        return login in COPILOT_LOGINS

    @classmethod
    def copilot_reviews(cls, reviews: list) -> list[dict]:
        return [review for review in reviews if cls.is_copilot(review)]

    @classmethod
    def copilot_comments(cls, comments: list) -> list[dict]:
        return [comment for comment in comments if cls.is_copilot(comment)]

    @staticmethod
    def copilot_requested(pull: dict) -> bool:
        requested = pull.get("requested_reviewers") or []
        return any(isinstance(reviewer, dict)
                   and str(reviewer.get("login") or "").lower() in COPILOT_LOGINS
                   for reviewer in requested)

    @classmethod
    def copilot_review_status(cls, pull: dict, reviews: list, head: str) -> str:
        """Return the exact-head Copilot review state without another request."""
        for review in cls.copilot_reviews(reviews):
            state = str(review.get("state") or "").upper()
            if (review.get("commit_id") == head
                    and bool(review.get("submitted_at")) and state not in ("", "PENDING")):
                return "completed"
        if cls.copilot_requested(pull):
            return "pending"
        return "absent"

    def request_copilot_review(self, number: int) -> None:
        """Request one Copilot review through GitHub's reviewer endpoint."""
        self.api(f"{self.prefix}/pulls/{number}/requested_reviewers", method="POST",
                 body={"reviewers": [COPILOT_REVIEWER]})

    def protection_observed(self, base: str) -> tuple[int, Any]:
        return self.api_status(f"{self.prefix}/branches/{quote(base, safe='')}/protection")

    def protection(self, base: str) -> dict:
        status, value = self.protection_observed(base)
        if status != 200:
            message = value.get("message") if isinstance(value, dict) else None
            raise Refusal(f"{message or 'branch protection could not be observed'} (HTTP {status})")
        return self.validate_protection(value)

    @staticmethod
    def validate_protection(value: Any, acceptances: list[dict] | None = None) -> dict:
        """The guards, over a classic object or the one `sd_protection.synthesize`
        shapes from a ruleset. The ruleset spelling of the administrator guard
        is `bypass_actors`: a ruleset that names anyone exempts them, and the
        refusal names the ruleset rather than the derived `enforce_admins`.

        `acceptances` are the reviewed head's `accepted_gaps` (`acceptances`).
        Two refusals can be declared instead of removed, `bypass` and
        `strict`, and each only when its entry pins that fact and every fact
        it pins equals the live state (`sd_protection.matching_acceptance`,
        `sd-status`'s own facts). The entries honoured travel on the object as
        `accepted_gaps`, so the receipt records what the merge accepted
        (sd:1451). Nothing else a declaration says widens this gate."""
        if not isinstance(value, dict):
            raise Refusal("branch protection could not be observed")
        observed = sd_protection.observed_state(value)
        honoured: list[dict] = []

        def honours(gap: str) -> bool:
            entry = sd_protection.matching_acceptance(acceptances or [], gap, observed)
            if entry is not None:
                honoured.append({key: entry[key] for key in ("id", "state", "since", "until")})
            return entry is not None

        checks = value.get("required_status_checks") or {}
        noun = "ruleset protection" if value.get("source") == sd_protection.RULESET_SOURCE else "branch protection"
        if value.get("source") in (sd_protection.RULESET_SOURCE, sd_protection.COMBINED_SOURCE):
            GitHub.validate_ruleset_bypass(value, honours)
        if value.get("enforce_admins", {}).get("enabled") is not True:
            raise Refusal("branch protection does not enforce administrators", code="protection_required",
                          next_action="Restore required branch protection; this command cannot bypass it.")
        if not isinstance(value.get("required_pull_request_reviews"), dict):
            raise Refusal(f"{noun} does not require pull requests")
        if not (checks.get("contexts") or checks.get("checks")) or (
                checks.get("strict") is not True and not honours("strict")):
            raise Refusal(f"{noun} requires strict, named CI checks")
        allowances = value["required_pull_request_reviews"].get("bypass_pull_request_allowances") or {}
        if any(allowances.get(name) for name in ("users", "teams", "apps")):
            raise Refusal("pull-request protection has bypass allowances")
        return {**value, "accepted_gaps": honoured} if honoured else value

    @staticmethod
    def validate_ruleset_bypass(value: dict, honours: Callable[[str], bool] = lambda gap: False) -> None:
        """A named bypass actor refuses, naming it: an app or a team that can
        walk past the ruleset is no more authority than an admin who can,
        and the refusal says which entry to remove. A bypass list GitHub did
        not show refuses too, as unknown rather than as none. GitHub returns
        `bypass_actors` only to a caller who can edit the ruleset, so its
        absence says what this token may see, not who can bypass, and a gate
        that read absence as nobody would grant on a permission it lacks.

        Beside classic protection (`source: combined`) only a decisive bypass
        refuses: one that removes the last firm source of a rule the ruleset
        carries. A ruleset an app can bypass while classic still enforces the
        same rule on everyone is information, not a refusal (sd:1419, Q2).

        The bypass may be declared rather than removed when every decisive
        actor is a `DeployKey` and `honours("bypass")` finds an entry whose
        list equals the live one exactly -- ruleset, rules, mode. Only that
        actor type: a deploy key pushes and cannot merge a pull request,
        while an app, a team or a role can, so any other actor refuses
        whatever the declaration says."""
        decisive = [entry for entry in value.get("rulesets") or []
                    if isinstance(entry, dict) and entry.get("bypass_actors") and sd_protection.decisive(value, entry)]
        keys_only = all(isinstance(actor, dict) and actor.get("actor_type") == "DeployKey"
                        for entry in decisive for actor in entry["bypass_actors"])
        if decisive and not (keys_only and honours("bypass")):
            entry = decisive[0]
            actors = ", ".join(sd_protection.actor_words(actor) for actor in entry["bypass_actors"])
            raise Refusal(f"ruleset {entry.get('name')} (#{entry.get('id')}) can be bypassed by {actors}",
                          code="protection_required",
                          next_action="Remove the bypass actors from the ruleset; this command cannot bypass it.")
        for entry in sd_protection.hidden_bypass(value):
            raise Refusal(f"ruleset {entry.get('name')} (#{entry.get('id')}) did not show its bypass actors, "
                          "so whether anyone can bypass it is unknown",
                          next_action="Read the ruleset with a token that can edit it, so GitHub returns "
                                      "bypass_actors; this command cannot treat a bypass list it was not shown as empty.")

    def declared_gap(self, head: str) -> dict | None:
        """The `unprotected` acceptance on the repository row, or `None` with the reasons.

        Read from the row's `status` declaration at merge time (sd:5109): the
        operator dropped the audit trail that read it at the reviewed commit.
        One parser, `sd_lib.parse_acknowledgements`, is what `sd-status` reads
        too. The entry must name the id *and* pin exactly the fact this branch
        state has, `{"branch_protection": false}`; an entry for another gap
        that pins the same fact accepts that gap, not this one.
        """
        where = sd_lib.ACKNOWLEDGEMENT_SOURCE
        entries, problems = self.acceptances(head)
        if problems:
            self.declaration_faults = problems
            return None
        for entry in entries:
            if entry["id"] == "unprotected" and entry["state"] == {"branch_protection": False}:
                self.declaration_faults = []
                return {"declared_gap": entry["id"], "until": entry["until"]}
        self.declaration_faults = [f"{where} carries no `unprotected` entry pinning branch_protection: false"]
        return None

    def acceptances(self, head: str) -> tuple[list[dict], list[str]]:
        """The repository row's `status` declaration, parsed: `(entries, problems)`.

        `head` names nothing read any more; the row answers for every head. An
        unread row is a problem, so it accepts nothing.
        """
        return sd_lib.load_acknowledgements(self.root)

    def accepted_gaps(self, head: str) -> list[dict]:
        """The row's `accepted_gaps`; a declaration with any fault accepts nothing."""
        entries, problems = self.acceptances(head)
        return [] if problems else entries

    def rulesets_observed(self, base: str) -> dict:
        """The rules every ruleset evaluates for `base`, through the status-bearing read."""
        return sd_protection.read_rulesets(self.api_status, self.prefix, base)

    def gate(self, base: str, head: str) -> dict:
        """What stands between this merge and `main`: the object, or the gap.

        Classic protection is read first, by a token `owned()` has shown to
        administer the repository -- to any other GitHub answers 404 for
        protection it may not show, so the order is load-bearing. A 200 is
        the object (Path A), layered with the branch's rulesets by
        `sd_protection.combine` (sd:1419). A 404
        is "no classic object", which since sd:1327 is not yet "unprotected":
        the branch's rulesets are read next, and an active ruleset that gates
        the merge -- a `pull_request` or `required_status_checks` rule -- is
        Path A too, validated by the same guards over the classic-shaped
        object `sd_protection.synthesize` builds. A 404 with no such rule is
        the declared gap (Path B) or today's refusal. A declaration beside
        either kind of object is the same contradiction.

        A 403 saying the plan lacks the feature (`Upgrade to GitHub Pro`) is
        feature-absent, the fact a declaration pins, so it takes Path B or
        refuses as `plan_limited`; any other non-200, non-404 answer, and any
        rules read that is not a 200 list, is "could not observe" and refuses
        as `protection` always has (sd:1323).
        """
        status, value = self.protection_observed(base)
        declaration = self.declared_gap(head)
        message = value.get("message") if isinstance(value, dict) else None
        where = sd_lib.ACKNOWLEDGEMENT_SOURCE
        if status == 200:
            if declaration is not None:
                raise Refusal(f"{where} declares main unprotected, "
                              "but GitHub returns a protection object; the declaration does not match the observed state")
            # Rulesets layer onto classic protection, strictest per rule
            # (sd:1419). A rules read that fails leaves the classic object as
            # it was: combining can only tighten what classic requires, or
            # bind administrators classic exempts, so classic alone is never
            # a weaker gate than the combined one and a transient fault here
            # does not refuse a merge that passed before rulesets were read.
            if isinstance(value, dict):
                read = self.rulesets_observed(base)
                if not read["error"]:
                    value = sd_protection.combine(value, read["rules"], read["rulesets"])
            return self.validate_protection(value, self.accepted_gaps(head))
        if status == 403 and sd_protection.plan_limited(message):
            if declaration is not None:
                return declaration
            raise Refusal(f"{message} (HTTP 403)", code="plan_limited",
                          next_action="This repository's plan does not offer branch protection. Move the repository "
                                      "to an organization whose plan has it, or declare the accepted gap in "
                                      f"{where}.")
        if status == 404:
            read = self.rulesets_observed(base)
            if read["error"]:
                raise Refusal(read["error"],
                              next_action="Restore read access to the repository's rulesets, then retry merge.")
            synthesized = sd_protection.synthesize(read["rules"], read["rulesets"])
            if synthesized is not None:
                if declaration is not None:
                    names = ", ".join(f"{entry['name']} (#{entry['id']})" for entry in synthesized["rulesets"])
                    raise Refusal(f"{where} declares main unprotected, but a ruleset gates it "
                                  f"({names}); the declaration does not match the observed state")
                return self.validate_protection(synthesized, self.accepted_gaps(head))
            if declaration is not None:
                return declaration
        detail = "; ".join(self.declaration_faults) if status == 404 else ""
        raise Refusal(f"{message or 'branch protection could not be observed'} (HTTP {status})"
                      + (f"; {detail}" if detail else ""),
                      code="protection_required" if status == 404 else "prerequisite_failed",
                      next_action="Restore branch protection, or declare the accepted gap in "
                                  f"{where}.")

    def expected_workflows(self, head: str) -> list[tuple[str, str]]:
        """`(path, name)` for every workflow at `head` whose `on` includes `pull_request`.

        Enumerated from the tree at merge time, `AGENTS.md`'s doctrine: a
        workflow added at `head` is expected at `head`, one deleted there is
        not, and the working tree has no say. `pull_request_target` is left
        out on purpose: its runs carry that event name and never match the
        `event=pull_request` query, so expecting one would refuse every merge.
        A `branches`, `paths` or `types` filter under the trigger is not
        read: the item's requirement 2 makes a workflow that did not run for
        the event a refusal naming it, never a pass, and reconstructing
        GitHub's scheduling here would be a second matcher whose every
        divergence from the real one is a merge without the run (the
        verification pass found two in one attempt). A trigger that is not
        spelled like an event name is a construct this reader could not
        resolve, and that refuses rather than drops the file.
        """
        directory = ".github/workflows"
        try:
            # `-z`: a newline listing quotes a non-ASCII name, and the quoted
            # form fails the suffix test below, so its run was never expected
            # (sd:1440).
            listing = git(self.root, "ls-tree", "--name-only", "-z", head, f"{directory}/")
        except Refusal:
            return []
        expected = []
        for path in sorted(name for name in listing.split("\0") if name):
            if not path.endswith((".yml", ".yaml")):
                continue
            lines = sd_lib.yaml_lines(git(self.root, "show", f"{head}:{path}"))
            triggers = sd_lib.workflow_triggers(lines)
            block = sd_lib.workflow_trigger_block(lines)
            unresolved = sorted(name for name in triggers if not sd_lib.EVENT_NAME_RE.match(name))
            unresolved += sorted(line.strip() for line in sd_lib.yaml_unreadable(block or []))
            if not triggers or unresolved:
                raise Refusal(f"{path} at {head[:12]}: could not read its triggers"
                              + (f" ({', '.join(repr(name) for name in unresolved)})" if unresolved else "")
                              + ", so whether it validates a pull request is unknown", code="ci_missing", boundary="ci")
            if "pull_request" in triggers:
                expected.append((path, sd_lib.yaml_field(lines, "name") or path.rsplit("/", 1)[-1]))
        return expected

    def every_check(self, head: str) -> None:
        """The substitute for required checks under a declared gap: all of them.

        Every check run `filter=latest` reports at `head` must have completed
        `success`, `neutral` or `skipped`; the newest status per context must
        be `success`; the set must not be empty unless Jenkins built `head`
        green; and every workflow at `head` that runs on `pull_request` must
        have a completed, successful run for that event at this exact SHA, so
        a commit the Tests workflow never ran on cannot pass on an advisory
        check alone.

        Under `ci = local` (sd:1843) the `pull_request` runs are not expected:
        the `sd/local-gate` status at this exact SHA is, and a missing one
        refuses as a missing run does. A failed one already refused above.
        """
        runs, statuses = self.check_records(head)
        if self.ci == "local":
            self.local_gate_passed(head, statuses)
        if not runs and not statuses and self.jenkins_green != head:
            raise Refusal(f"nothing validated {head}: no check run and no status exists for it", code="ci_missing",
                          next_action="Run the repository's checks for this exact head, then retry merge.", boundary="ci", state="retryable_failure")
        self.records_passing(head, runs, statuses)
        if self.ci != "local":
            self.pull_request_runs_passed(head)

    def member_checks(self, head: str) -> None:
        """A train member's own checks at `head`: every one recorded passes (sd:9923).

        `sd/local-gate` may be missing, as the train's gate on its tip
        stands in for it; one posted at `head` must still be `success`.
        GitHub enforces required checks only on the train PR's head, so
        this runs whatever the protection says.
        """
        runs, statuses = self.check_records(head)
        self.records_passing(head, runs, statuses)
        if self.ci != "local":
            self.pull_request_runs_passed(head)

    def check_records(self, head: str) -> tuple[list, list]:
        """The latest check runs and the statuses GitHub holds at `head`."""
        return (self.pages(f"{self.prefix}/commits/{head}/check-runs?filter=latest", "check_runs"),
                self.pages(f"{self.prefix}/commits/{head}/statuses"))

    def records_passing(self, head: str, runs: list, statuses: list) -> None:
        """Refuse a check run or a newest status per context at `head` that did not pass."""
        for entry in runs:
            if (entry.get("head_sha") != head or entry.get("status") != "completed"
                    or entry.get("conclusion") not in PASSING):
                same = [other for other in runs if other.get("name") == entry.get("name")]
                raise Refusal(f"CI is not passing on {head}: {entry.get('name')}", code="ci_not_passing",
                              next_action=ci_next_action(same, head), boundary="ci", state="retryable_failure")
        newest: dict[str, dict] = {}
        for entry in statuses:  # newest first, so the first record per context is the current one
            newest.setdefault(str(entry.get("context")), entry)
        for context, entry in newest.items():
            if entry.get("sha", head) != head or entry.get("state") != "success":
                raise Refusal(f"status is not passing on {head}: {context}", code="ci_not_passing",
                              next_action="Wait for or fix exact-head CI, then retry merge.", boundary="ci", state="retryable_failure")

    def pull_request_runs_passed(self, head: str) -> None:
        """Every workflow at `head` that runs on `pull_request` has a successful run there."""
        pull_request_runs = self.pages(f"{self.prefix}/actions/runs?head_sha={head}&event=pull_request", "workflow_runs")
        for path, name in self.expected_workflows(head):
            if not any(run.get("path") == path and run.get("head_sha") == head and run.get("event") == "pull_request"
                       and run.get("status") == "completed" and run.get("conclusion") == "success"
                       for run in pull_request_runs):
                raise Refusal(f"workflow {name} ({path}) has no successful pull_request run on {head}", code="ci_missing",
                              next_action="Run the workflow for this exact head, then retry merge.", boundary="ci", state="retryable_failure")

    def local_gate_passed(self, head: str, statuses: list | None = None) -> None:
        """Refuse unless the newest `sd/local-gate` status at `head` is `success`.

        Missing, failed, pending, naming another SHA, or posted by an account
        other than this token's: each refuses, because under `ci = local` that
        status is the only thing that ran the checks, and anyone with write
        access can post one through the statuses API.
        """
        context = sd_lib.LOCAL_GATE_CONTEXT
        if statuses is None:
            statuses = self.pages(f"{self.prefix}/commits/{head}/statuses")
        current = next((entry for entry in statuses if isinstance(entry, dict) and entry.get("context") == context), None)
        if current is None or current.get("sha", head) != head:
            raise Refusal(f"repo.ci is local and {context} has no status on {head}", code="ci_missing",
                          boundary="ci", state="retryable_failure",
                          next_action="Retry sd-ship merge, which runs sd-check at this head and posts the status.")
        creator = gate_creator(current)
        if creator != self.viewer_login().lower():
            raise Refusal(f"repo.ci is local and {context} on {head} was posted by {creator or 'an unknown account'}, "
                          f"not {self.viewer_login()}; only a status this account posted counts",
                          code="local_gate_foreign", boundary="ci", state="retryable_failure",
                          next_action="Retry sd-ship merge, which runs sd-check at this head and posts its own status.")
        if current.get("state") != "success":
            raise Refusal(f"repo.ci is local and {context} is {current.get('state')} on {head}: "
                          f"{current.get('description') or 'no description'}", code="ci_not_passing",
                          boundary="ci", state="retryable_failure",
                          next_action="Fix the failing check, push, then retry merge.")

    def commits_behind(self, base: str, head: str) -> int | None:
        """How many commits of `base` are missing from `head`, GitHub's count."""
        return self.api(f"{self.prefix}/compare/{quote(base, safe='')}...{head}").get("behind_by")

    def ready(self, pull: dict, head: str, base: str, protection: dict) -> None:
        if pull.get("merged") is True or pull.get("state") != "open" or pull.get("draft") is not False:
            raise Refusal("pull request is not open and ready")
        if pull.get("head", {}).get("sha") != head or pull.get("base", {}).get("ref") != base:
            raise Refusal("pull-request head or default base moved after local review")
        if str(pull.get("head", {}).get("repo", {}).get("full_name", "")).lower() != self.repository:
            raise Refusal("pull request comes from a different repository")
        if pull.get("mergeable_state") == "dirty":
            # No merge ref, so no pull_request check will ever run (sd:1403).
            raise Refusal("the pull request conflicts with its base, so GitHub builds no merge ref and runs no check",
                          code="merge_conflict", boundary="ci",
                          next_action="Merge the default branch into this branch, resolve the conflict, push, then retry.")
        if pull.get("mergeable_state") == "unknown":
            raise Refusal("GitHub has not finished computing mergeability", code="mergeability_pending",
                          boundary="ci", state="retryable_failure", next_action="Wait a minute, then retry merge once.")
        refuse_behind(pull, base)
        if pull.get("mergeable") is not True or pull.get("mergeable_state") not in MERGEABLE_STATES:
            # Retryable: GitHub recomputes the rules after a new status, and
            # `sd-ship merge` reads again for a bounded window (sd:2050).
            raise Refusal("GitHub has not confirmed all required merge rules are satisfied "
                          f"(mergeable_state {pull.get('mergeable_state')!r}, mergeable {pull.get('mergeable')!r})",
                          code="merge_rules_unconfirmed", boundary="ci", state="retryable_failure",
                          next_action="Read the pull request's merge box for the unmet rule, resolve it, then retry merge.")
        if self.commits_behind(base, head) != 0:
            # `base_moved`, as `refuse_behind` names it: a lane hands a satellite's entry back on it (sd:2704).
            raise Refusal("the reviewed branch is behind the current default branch", code="base_moved", boundary="ci",
                          state="retryable_failure", next_action=catch_up_action(base))
        if "declared_gap" in protection:
            self.every_check(head)
            return
        if self.ci == "local":
            # Required contexts are the repository's word; the gate is ours,
            # and is required whether or not protection names it.
            self.local_gate_passed(head)
        runs = self.pages(f"{self.prefix}/commits/{head}/check-runs?filter=latest", "check_runs")
        statuses = self.pages(f"{self.prefix}/commits/{head}/statuses")
        required = protection["required_status_checks"]
        bindings = {entry["context"]: entry.get("app_id", -1) for entry in required.get("checks", [])}
        for context in required.get("contexts", []):
            bindings.setdefault(context, -1)
        for context, app in bindings.items():
            matching = [entry for entry in runs if entry.get("name") == context
                        and (app in (-1, None) or entry.get("app", {}).get("id") == app)]
            legacy = [entry for entry in statuses if entry.get("context") == context] if app in (-1, None) else []
            # The statuses endpoint is newest first. A successful old rerun
            # cannot mask a failed or pending current run of the same check.
            if any(entry.get("head_sha") != head or entry.get("status") != "completed"
                   or entry.get("conclusion") not in PASSING for entry in matching):
                raise Refusal(f"required CI is not passing on {head}: {context}", code="ci_not_passing",
                              boundary="ci", state="retryable_failure", next_action=ci_next_action(matching, head))
            if legacy and (legacy[0].get("sha", head) != head or legacy[0].get("state") != "success"):
                raise Refusal(f"required status is not passing on {head}: {context}")
            if not matching and not legacy:
                raise Refusal(f"required CI has no current result on {head}: {context}", code="ci_missing",
                              boundary="ci", state="retryable_failure", next_action="Run the required check for this exact head, then retry merge.")
