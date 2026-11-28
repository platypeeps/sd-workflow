"""The provider registry: identity from the file, state from the rows.

One file, `~/.local/share/sd/providers.yaml`, says what a provider *is* --
its start line or URL, vendor, bill, reader, price, the environment
variables its process receives, and the two role lists. The `provider` and
`bill` tables say what has *changed* since: enabled, the reason it is not,
each role's automatic order, and a bill's cap. A checkpoint makes NULL ranks
explicit exclusions after the operator configures membership. The file seeds those tables on
the first read through a writable connection (`ensure_seeded`, which `read`
calls); a later writable read seeds only a provider or bill the file has
gained since, and never rewrites a row that exists. On every read the
library merges the two, so the pack's `sd-review` and the dashboard see one registry. A
read-only connection cannot seed and gets the merged view all the same,
which for an unseeded table is the file alone.

The split is the whole design. Identity in a file, because it is edited by
hand, reviewed in a diff and restored from a snapshot. State in rows, because
it is changed from a screen, at a moment, by someone who is not editing a
file.

Four refusals, all at read time, so a registry that cannot mean anything
never reaches a caller:

* A **capped bill with a `start` entry**. The library makes `url` calls
  itself and can refuse one before it is sent; it cannot refuse a spawned
  CLI's calls, so a cap on one is a number nothing enforces.
* **`author` and `reviewer` resolving to the same provider**, which is a
  review by the author.
* A **name that does not resolve**: a role list naming an unknown provider,
  a provider naming an unknown bill.
* **Unreadable explicit membership**: malformed policy or a missing provider
  row cannot silently restore automatic selection.

Nothing here special-cases a provider. An `exo` entry with a URL resolves
because every `url` entry resolves; that is criterion 3's second half, and it
is a property of having no table of known names rather than a feature.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .database import Patient, transaction
from .errors import RegistryError
from .yaml_lite import YamlLiteError, load

#: Beside the database, in `~/.local/share/sd/`.
REGISTRY_NAME = "providers.yaml"

#: The two role lists. A third role is added here and in the file; nothing
#: else in this module names one.
ROLES = ("author", "reviewer")

# A listed provider's row ranks are authoritative, including NULL exclusions.
ORDER_POLICY_KEY = "provider-orders:v1"

#: A bill with one of these bases has a spend limit the library enforces, so
#: every provider on it must be callable by the library itself.
CAPPED_BASES = ("company", "plan", "prepaid")


@dataclass(frozen=True)
class Bill:
    name: str
    cost_basis: str
    cap_usd_month: float | None = None
    meter: str | None = None

    @property
    def capped(self) -> bool:
        return self.cap_usd_month is not None or self.cost_basis in CAPPED_BASES


@dataclass(frozen=True)
class Provider:
    """One entry, as the file and the rows together describe it."""

    name: str
    vendor: str
    bill: str
    start: str | None = None
    url: str | None = None
    model: str | None = None
    reader: str | None = None
    max_tokens: int | None = None
    thinking: str | None = None
    reasoning_effort: str | None = None
    #: `json_schema` when the endpoint holds its answer to a caller's schema
    #: in strict mode (sd:1827); unset sends no response_format at all.
    response_format: str | None = None
    price: dict[str, float] = field(default_factory=dict)
    env: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    enabled: bool = True
    reason: str | None = None
    ranks: dict[str, int] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """`url` when the library makes the call, `start` when it spawns one."""
        return "url" if self.url else "start"


@dataclass(frozen=True)
class Registry:
    path: Path
    bills: dict[str, Bill]
    providers: dict[str, Provider]
    #: What `merge` read past: a row cap on a bill a `start` entry holds is
    #: not applied, and its sentence is here for the screen. Empty for the
    #: file alone, which `parse` refuses outright instead.
    warnings: tuple[str, ...] = ()

    def order(self, role: str) -> list[Provider]:
        """The enabled providers holding `role`, best first.

        Legacy NULL ranks inherit the file. An explicit order policy makes
        each listed provider's row authoritative, including NULL exclusions.
        """
        if role not in ROLES:
            raise RegistryError(f"no role {role!r}; the roles are {', '.join(ROLES)}")
        # Only what the role list ranks resolves. A provider that declares
        # the role but is on no list is capable and not chosen -- which is
        # what a shipped-disabled entry looks like from here.
        holders = [
            provider
            for provider in self.providers.values()
            if role in provider.ranks and provider.enabled
        ]
        return sorted(holders, key=lambda provider: provider.ranks[role])

    def resolve(self, role: str) -> Provider:
        """The provider a role gets. A disabled entry never resolves."""
        order = self.order(role)
        if not order:
            raise RegistryError(
                f"no enabled provider holds the {role!r} role in {self.path}"
            )
        return order[0]


def registry_path(home: Path | str | None = None) -> Path:
    base = Path(home) if home is not None else Path(os.environ.get("HOME", "~")).expanduser()
    return base / ".local/share/sd" / REGISTRY_NAME


def beside(connection: sqlite3.Connection) -> Path:
    """The registry next to the database this connection is open on.

    The file and the database share one directory, `~/.local/share/sd/`, and
    a caller holding a connection has already said which one. Resolving from
    the connection rather than from `$HOME` is what keeps a fixture inside
    its fixture: a test opens a database in a directory it owns, and the
    registry it gets is the one there or a `RegistryError` naming that path
    -- never the operator's file because the process's `HOME` is the real
    one. That is how a test that omitted `path=` passed on a developer's
    machine and failed under CI's isolated home twice on 2026-09-12 (#286,
    #288). The same rule already places `executions/` (`runner_exec`).
    """
    located = connection.execute("PRAGMA database_list").fetchone()[2]
    if not located:
        raise RegistryError("the connection has no database file; pass the registry path")
    return Path(located).parent / REGISTRY_NAME


def _require(mapping: dict[str, Any], key: str, name: str, path: Path) -> Any:
    try:
        return mapping[key]
    except KeyError:
        raise RegistryError(f"{path}: provider {name!r} has no {key!r}") from None


def reasoning_controls(body: dict[str, Any], path: Path, name: str) -> dict[str, Any]:
    """Explicit URL controls; omitted fields leave the endpoint default intact."""
    controls = {}
    for key, values in (("thinking", ("disabled", "adaptive")),
                        ("reasoning_effort", ("none", "low", "high", "max"))):
        value = body.get(key)
        if value is not None:
            if not isinstance(value, str) or value not in values or not body.get("url"):
                raise RegistryError(f"{path}: provider {name!r} needs a URL and {key} in {values}")
            controls[key] = value
    if len(controls) > 1:
        raise RegistryError(f"{path}: provider {name!r} must choose one reasoning control")
    return controls


RESPONSE_FORMATS = ("json_schema",)


def response_format(body: dict[str, Any], path: Path, name: str) -> str | None:
    """The entry's opt-in to a strict response_format, or None (sd:1827).

    Per entry, because an endpoint that accepts the field may ignore it:
    MiniMax-M3 did, and kimi-k3 held its answer to the schema.
    """
    value = body.get("response_format")
    if value is None:
        return None
    if not isinstance(value, str) or value not in RESPONSE_FORMATS or not body.get("url"):
        raise RegistryError(f"{path}: provider {name!r} needs a URL and response_format in {RESPONSE_FORMATS}")
    return value


def parse(text: str, path: Path | str = REGISTRY_NAME) -> Registry:
    """Read the file. Every refusal below happens here, before any caller."""
    path = Path(path)
    try:
        document = load(text)
    except YamlLiteError as error:
        raise RegistryError(f"{path}: {error}") from None

    for section in ("bills", "providers", "roles"):
        if section not in document:
            raise RegistryError(f"{path}: no {section!r} section")
        # A section that is a list or a scalar reached `.items()` below and
        # raised `AttributeError`, which no caller's boundary names (#438).
        if not isinstance(document[section], dict):
            raise RegistryError(f"{path}: {section!r} is not a mapping")

    bills: dict[str, Bill] = {}
    for name, body in document["bills"].items():
        if not isinstance(body, dict) or "cost" not in body:
            raise RegistryError(f"{path}: bill {name!r} has no 'cost'")
        bills[name] = Bill(
            name=name,
            cost_basis=body["cost"],
            cap_usd_month=body.get("cap_usd_month"),
            meter=body.get("meter"),
        )

    role_lists: dict[str, list[str]] = {}
    for role, names in document["roles"].items():
        if role not in ROLES:
            raise RegistryError(f"{path}: no role {role!r}")
        if not isinstance(names, list):
            raise RegistryError(f"{path}: role {role!r} is not a list")
        role_lists[role] = [str(name) for name in names]

    providers: dict[str, Provider] = {}
    for name, body in document["providers"].items():
        if not isinstance(body, dict):
            raise RegistryError(f"{path}: provider {name!r} is not a mapping")
        start, url = body.get("start"), body.get("url")
        if bool(start) == bool(url):
            raise RegistryError(
                f"{path}: provider {name!r} needs exactly one of 'start' or "
                f"'url'; it has "
                + ("both" if start else "neither")
            )
        bill_name = _require(body, "bill", name, path)
        if bill_name not in bills:
            raise RegistryError(
                f"{path}: provider {name!r} is billed to {bill_name!r}, which "
                f"the 'bills' section does not name"
            )
        bill = bills[bill_name]
        if bill.capped and start:
            raise RegistryError(_capped_start(path, name, bill_name))
        # An entry's `roles` says what it *can* be; a role list says what is
        # tried, and in what order. The list may be a subset: `exo` declares
        # both roles and appears only under `reviewer`, because it ships
        # disabled with its model unpinned. The reverse is a refusal, below.
        declared = body.get("roles")
        if declared is None:
            roles = tuple(role for role in ROLES if name in role_lists.get(role, []))
        else:
            declared_set = {str(role) for role in declared}
            if declared_set - set(ROLES):
                raise RegistryError(
                    f"{path}: provider {name!r} claims role(s) "
                    f"{sorted(declared_set - set(ROLES))}"
                )
            roles = tuple(role for role in ROLES if role in declared_set)
        providers[name] = Provider(
            name=name,
            vendor=str(_require(body, "vendor", name, path)),
            bill=bill_name,
            start=start or None,
            url=url or None,
            model=body.get("model"),
            reader=body.get("reader"),
            max_tokens=body.get("max_tokens"),
            **reasoning_controls(body, path, name),
            response_format=response_format(body, path, name),
            price=dict(body.get("price") or {}),
            env=tuple(str(name) for name in (body.get("env") or ())),
            roles=roles,
            enabled=bool(body.get("enabled", True)),
            reason=body.get("reason"),
            ranks={
                role: role_lists[role].index(name)
                for role in roles
                if name in role_lists.get(role, [])
            },
        )

    for role, names in role_lists.items():
        unknown = [name for name in names if name not in providers]
        if unknown:
            raise RegistryError(
                f"{path}: the {role!r} list names {unknown}, which the "
                f"'providers' section does not"
            )
        without = [
            name for name in names
            if name in providers and role not in providers[name].roles
        ]
        if without:
            raise RegistryError(
                f"{path}: the {role!r} list names {without}, whose entries do "
                f"not declare that role. A list may be a subset of the "
                f"providers holding a role; it may not add one."
            )

    registry = Registry(path=path, bills=bills, providers=providers)
    _refuse_one_provider_for_both_roles(registry)
    return registry


def _capped_start(path: Path | str, name: str, bill: str) -> str:
    """The one sentence for a `start` entry on a capped bill, wherever the
    cap came from: `parse` says it of the file, `refuse_capped_start_entries`
    of the merged rows, and `provider_controls.set_cap` of a cap it will not
    write. One text, so the dashboard's refusal is the reader's."""
    return (
        f"{path}: provider {name!r} is a 'start' entry on the capped "
        f"bill {bill!r}. The library makes 'url' calls and can "
        f"refuse one before it is sent; it cannot refuse a spawned "
        f"command's calls, so the cap would be a number nothing "
        f"enforces. Give it a 'url', or move it to an uncapped bill."
    )


def refuse_capped_start_entries(registry: Registry) -> None:
    """The file's first refusal, over a registry a write would produce.

    `parse` refuses a capped bill with a `start` entry in the file;
    `provider_controls.set_cap` runs this over the registry as it would
    read after its write, so a cap the file would be refused for is refused
    at the row too (sd:234, clause 15.15). `merge` does not raise it: a row
    that already holds such a cap (a bare `set_bill_cap`) would then fail
    every read, and the clearing write with it (#433's review); the merge
    reads the bill without that cap and reports it in `warnings`.
    """
    for provider in registry.providers.values():
        if provider.kind == "start" and registry.bills[provider.bill].capped:
            raise RegistryError(_capped_start(registry.path, provider.name, provider.bill))


def _refuse_one_provider_for_both_roles(registry: Registry) -> None:
    """`author` and `reviewer` must not resolve to the same provider.

    A review by the author is not a review. Checked on what *resolves* and
    not on the whole lists, because the lists overlap on purpose: a provider
    is allowed to hold both roles, and does, as long as the one at the top of
    each is not the same one.
    """
    resolved = {}
    for role in ROLES:
        order = registry.order(role)
        if order:
            resolved[role] = order[0].name
    if len(resolved) == len(ROLES) and len(set(resolved.values())) == 1:
        name = next(iter(resolved.values()))
        raise RegistryError(
            f"{registry.path}: {name!r} is first in both the 'author' and the "
            f"'reviewer' list, so a review would be by the author. Reorder one "
            f"list, or disable the entry for one role."
        )


def read(
    path: Path | str | None = None,
    *,
    home: Path | str | None = None,
    connection: sqlite3.Connection | None = None,
) -> Registry:
    """The registry as the system sees it: the file, merged with the rows.

    With no connection this is the file alone, which is what a caller with no
    database -- the installer, a doctor command -- gets, at `registry_path`.
    With a connection and neither `path` nor `home`, the file is the one
    beside that connection's database (`beside`), not the one under `$HOME`;
    on a hub's session, the hub's file, read over that session.
    """
    session = _session(connection) if path is None and home is None else None
    if session is not None:
        # A hub's session answers the hub's path, which names no file on a satellite: read the hub's
        # file over that session instead (seam 7; sd:10400, a satellite build lost its cost attribution).
        registry = parse(session.registry_bytes().decode("utf-8"), f"{session.host}:{REGISTRY_NAME}")
    else:
        if path is not None:
            target = Path(path)
        elif connection is not None and home is None:
            target = beside(connection)
        else:
            target = registry_path(home)
        if not target.exists():
            raise RegistryError(f"no provider registry at {target}")
        registry = parse(target.read_text(encoding="utf-8"), target)
    if connection is None:
        return registry
    ensure_seeded(connection, registry)
    return merge(registry, connection)


def _session(connection: Any):
    """The hub session `connection` reads through, unwrapped from `Patient`, or None for a local file."""
    from . import remote

    inner = connection.connection if isinstance(connection, Patient) else connection
    return inner if isinstance(inner, remote.Connection) else None


def writable(connection: sqlite3.Connection) -> bool:
    """Whether this connection may write, as far as `query_only` says.
    `database.connect(write=False)` sets it and also opens `mode=ro`, which
    no pragma reports; `ensure_seeded` catches that one at the write."""
    return not connection.execute("PRAGMA query_only").fetchone()[0]


#: SQLite's primary result code for a write to a read-only database; the
#: extended codes carry it in their low byte.
SQLITE_READONLY = 8


def order_policy(connection: sqlite3.Connection) -> dict | None:
    """Read explicit membership authority; malformed or lost state refuses."""
    row = connection.execute(
        "SELECT body FROM state WHERE kind='checkpoint' AND key=? ORDER BY id DESC LIMIT 1",
        (ORDER_POLICY_KEY,),
    ).fetchone()
    if row is None:
        return None
    def unique_object(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate policy fields")
        return value
    try:
        value = json.loads(row["body"], object_pairs_hook=unique_object)
        if not isinstance(value, dict) or set(value) != {"schema_version", "explicit_providers"}:
            raise ValueError("expected schema_version and explicit_providers")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("unsupported schema_version")
        names = value["explicit_providers"]
        if (not isinstance(names, list) or not names
                or any(not isinstance(name, str) or not name.strip() for name in names)
                or len(set(names)) != len(names)):
            raise ValueError("explicit_providers must list distinct provider names")
        present = {entry[0] for entry in connection.execute("SELECT name FROM provider")}
        if set(names) - present:
            raise ValueError("explicit provider rows are missing: " + ", ".join(sorted(set(names) - present)))
    except (TypeError, ValueError) as error:
        raise RegistryError(f"unreadable provider order policy: {error}") from None
    return value


def ensure_seeded(connection: sqlite3.Connection, registry: Registry) -> int:
    """Seed `provider` and `bill` from the file if the rows are not there.

    This is what "on first open" means: the first read through a writable
    connection that finds an entry of the file with no row writes the file's
    values for it, in one transaction, and returns how many rows it wrote. A
    read that finds every row already there issues no write and begins no
    transaction, and a read-only connection cannot seed and does not try:
    it returns 0 and its caller gets the merged view, which for an unseeded
    table is the file alone. Nothing a row already holds is touched --
    `seed` inserts and never updates -- so a provider the dashboard has
    switched off stays off across every later read.

    Until this existed, `init` promised the seed "on first read" and no read
    seeded: a machine whose file arrived after `init` kept an empty
    `provider` table until the dashboard's first toggle wrote a row.
    """
    if not writable(connection):
        return 0
    # Check before seeding: a lost explicit row must not regain its YAML rank.
    order_policy(connection)
    present = {
        row[0] for row in connection.execute("SELECT name FROM provider")
    }
    bills = {row[0] for row in connection.execute("SELECT name FROM bill")}
    if registry.providers.keys() <= present and registry.bills.keys() <= bills:
        return 0
    try:
        with transaction(connection):
            return seed(connection, registry)
    except sqlite3.OperationalError as error:
        # A `mode=ro` open whose `query_only` was switched off: read-only all
        # the same, so it seeds nothing, like the connection `writable` sees.
        if (getattr(error, "sqlite_errorcode", 0) or 0) & 0xFF != SQLITE_READONLY:
            raise
        return 0


def merge(registry: Registry, connection: sqlite3.Connection) -> Registry:
    """Read policy, provider rows, and caps from one SQLite snapshot."""
    if connection.in_transaction:
        return _merge(registry, connection)
    # Deferred BEGIN works on read-only connections and does not block writers.
    connection.execute("BEGIN")
    try:
        return _merge(registry, connection)
    finally:
        # Only this function's read transaction ends; caller-owned work stays open.
        connection.execute("ROLLBACK")


def _merge(registry: Registry, connection: sqlite3.Connection) -> Registry:
    """Apply one snapshot over the file. Rows win on state, never identity."""
    policy = order_policy(connection)
    explicit = set(policy["explicit_providers"]) if policy is not None else set()
    rows = {
        row["name"]: row
        for row in connection.execute(
            "SELECT name, enabled, reason, author_rank, reviewer_rank FROM provider"
        )
    }
    caps = {
        row["name"]: row["cap_usd_month"]
        for row in connection.execute("SELECT name, cap_usd_month FROM bill")
    }
    providers = {}
    for name, provider in registry.providers.items():
        row = rows.get(name)
        if row is None:
            providers[name] = provider
            continue
        ranks = {} if name in explicit else dict(provider.ranks)
        for role in ROLES:
            rank = row[f"{role}_rank"]
            if rank is not None and role in provider.roles:
                ranks[role] = rank
        providers[name] = Provider(
            **{
                **provider.__dict__,
                "enabled": bool(row["enabled"]),
                "reason": row["reason"] if row["reason"] is not None else provider.reason,
                "ranks": ranks,
            }
        )
    # A row cap on a bill a `start` entry holds is the file's first refusal
    # arrived at through a row; the merge leaves the file's cap in place
    # for that bill and carries the sentence, so the read succeeds and the
    # screen can show what `set_cap(..., None)` clears.
    spawned = {provider.bill for provider in registry.providers.values() if provider.kind == "start"}
    warnings = []
    bills = {}
    for name, bill in registry.bills.items():
        cap = caps.get(name, bill.cap_usd_month)
        if cap is not None and name in spawned and not bill.capped:
            holders = sorted(p.name for p in registry.providers.values() if p.kind == "start" and p.bill == name)
            warnings.extend(_capped_start(registry.path, holder, name) for holder in holders)
            cap = bill.cap_usd_month
        bills[name] = Bill(**{**bill.__dict__, "cap_usd_month": cap})
    merged = Registry(path=registry.path, bills=bills, providers=providers, warnings=tuple(warnings))
    _refuse_one_provider_for_both_roles(merged)
    return merged


def seed(connection: sqlite3.Connection, registry: Registry) -> int:
    """Write the file's entries into `provider` and `bill`, once.

    A row that already exists is left alone, because it holds what the
    dashboard changed and the file is not where that lives. Returns how many
    rows were written, so `init` can report it and a test can assert the
    second call writes none. `ensure_seeded` is the caller that decides
    whether to call this at all; this is the write, and it commits nothing
    itself.
    """
    written = 0
    for bill in registry.bills.values():
        cursor = connection.execute(
            "INSERT OR IGNORE INTO bill (name, cost_basis, cap_usd_month) "
            "VALUES (?, ?, ?)",
            (bill.name, bill.cost_basis, bill.cap_usd_month),
        )
        written += cursor.rowcount if cursor.rowcount > 0 else 0
    for provider in registry.providers.values():
        cursor = connection.execute(
            "INSERT OR IGNORE INTO provider "
            "(name, enabled, reason, author_rank, reviewer_rank) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                provider.name,
                int(provider.enabled),
                provider.reason,
                provider.ranks.get("author"),
                provider.ranks.get("reviewer"),
            ),
        )
        written += cursor.rowcount if cursor.rowcount > 0 else 0
    return written
