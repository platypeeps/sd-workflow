"""Repository paths: the stored key and the disk path (sd:1439).

The database stores a repository under the current `$HOME` as `~/` plus its
home-relative POSIX path, so a second machine with another login resolves
the same rows. A path outside `$HOME` stays absolute. That stored form is the
**key**. The **disk path** is what `git -C`, `cwd=` and `Path.exists` need.

A path becomes a key when it enters the library, and a key becomes a disk
path at the moment something touches the disk. Nothing else converts:

* `store` -- disk path or key in, key out. Every writer and every probe.
* `expand` -- key in, disk path out. Every disk use.
* `keys` -- the key plus the legacy absolute form, for `IN (...)` probes, so
  a row written before migration 014 still matches.
* `same` -- whether two paths name one repository, for comparisons in Python.
* `same_key` -- whether a repository value stored in JSON (a receipt, a
  proof, a journal) names the same repository as a column 014 rewrote.

`$HOME` is read at call time, as `database.default_path` does: tests set
`HOME` for themselves and their children, and a frozen home would leak the
real one into them. An unset or empty `$HOME` is an error and never a fall
back to the password database, because a wrong home fails open.

The conversion never runs inside SQL at query time. Under sd:1335 the SQL
runs on the hub, under the hub's home, and would answer a satellite wrongly.
The one exception is the migration, which runs only on the hub: `install`
registers two string functions on the connection `migrate` uses.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .errors import SdDbError

#: The prefix of a home-relative key. `~` alone is the home itself.
PREFIX = "~/"


#: What points git at another repository than the one `-C` names: the variables `git rev-parse --local-env-vars`
#: lists. A hook exports `GIT_DIR` and `GIT_WORK_TREE`, and a child that inherits them reads and writes the hook's
#: repository and its origin (pack sd:9709, round 26). The config carriers are here too: a one-off
#: `url.*.insteadOf` would redirect the child's origin (pack sd:9863 review).
GIT_LOCAL = frozenset({
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR"})
#: The command-scope keys a child keeps, renumbered: the suite's guard against git's detached auto-maintenance
#: racing a temporary directory's cleanup (`.github/scripts/run-tests.sh`). None names a location.
GIT_KEPT_CONFIG = frozenset({"maintenance.auto", "gc.auto", "receive.autogc"})
#: More config entries than any caller sets; a larger count is no real one.
GIT_CONFIG_MAX = 64


def git_env(environ=None) -> dict[str, str]:
    """`environ`, this process's when None, less `GIT_LOCAL` and every `GIT_CONFIG_KEY_n`/`VALUE_n`, with the
    `GIT_KEPT_CONFIG` entries put back: the environment a `git -C DIR` child, or a child that starts one, runs in,
    so `DIR` alone names the repository and its remotes."""
    source = os.environ if environ is None else environ
    result = {key: value for key, value in source.items()
              if key not in GIT_LOCAL and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    count = source.get("GIT_CONFIG_COUNT", "")
    total = int(count) if count.isascii() and count.isdigit() and len(count) <= 2 else 0
    kept = [(source[f"GIT_CONFIG_KEY_{n}"], source.get(f"GIT_CONFIG_VALUE_{n}", ""))
            for n in range(min(total, GIT_CONFIG_MAX))
            if source.get(f"GIT_CONFIG_KEY_{n}", "").lower() in GIT_KEPT_CONFIG]
    for index, (key, value) in enumerate(kept):
        result[f"GIT_CONFIG_KEY_{index}"], result[f"GIT_CONFIG_VALUE_{index}"] = key, value
    if kept:
        result["GIT_CONFIG_COUNT"] = str(len(kept))
    return result


class PathRefused(SdDbError):
    """A value that cannot be a repository path, and why."""


class NotAPath(PathRefused):
    """A relative value or `~user`: not a path this module converts.

    Lookups and writers pass such a value through unchanged, because fixtures
    and sentinels use bare names; `store` and `expand` refuse it.
    """


def home() -> Path:
    """`$HOME`, read now. Refused when unset or empty, never guessed."""
    raw = os.environ.get("HOME", "")
    if not raw:
        raise PathRefused(
            "HOME is unset or empty, so a repository key cannot be converted; "
            "set HOME for this process (launchd and cron jobs need it in their "
            "environment)"
        )
    if not os.path.isabs(raw):
        raise PathRefused(f"HOME is {raw!r}, which is not an absolute path")
    return Path(raw)


def _full(path: os.PathLike[str] | str) -> Path:
    """An absolute path from a disk path or a key, before resolving."""
    text = os.fspath(path)
    if text == "~" or text.startswith(PREFIX):
        return home() / text[len(PREFIX):] if text != "~" else home()
    if text.startswith("~"):
        raise NotAPath(
            f"{text!r} names another account's home; only `~/` is a key")
    if not os.path.isabs(text):
        raise NotAPath(
            f"{text!r} is relative; a repository path is absolute or starts `~/`")
    return Path(text)


def store(path: os.PathLike[str] | str) -> str:
    """The key for a disk path or a key.

    Resolved on both sides, so a symlinked home and macOS's `/var` ->
    `/private/var` still match. A path at or under the resolved home becomes
    `~` or `~/<relative>`; any other path is its resolved absolute form.
    """
    resolved = _full(path).resolve()
    base = home().resolve()
    if resolved == base:
        return "~"
    try:
        relative = resolved.relative_to(base)
    except ValueError:
        return str(resolved)
    return PREFIX + relative.as_posix()


def expand(key: os.PathLike[str] | str) -> Path:
    """The disk path for a key. An absolute key is returned as it is."""
    return _full(key)


def disk(value: os.PathLike[str] | str) -> Path:
    """`expand` for a value that may not be a key: a stored `~/` key becomes
    its disk path, and anything else is taken as the path it already is.

    For disk uses whose input also comes from a caller or a fixture -- a
    `repo_paths` list, a journal -- where refusing a relative value would
    change what the caller asked for.
    """
    text = os.fspath(value)
    return expand(text) if text == "~" or text.startswith(PREFIX) else Path(text)


def key(value: str | None) -> str | None:
    """What a writer stores: the `~/` key for a path under the home.

    Any other value is kept exactly as given -- a path outside the home stays
    absolute and unchanged (prd R1), and a bare name or sentinel is not a
    path. `None` stays `None`.
    """
    if value is None:
        return None
    try:
        converted = store(value)
    except NotAPath:
        return value
    return converted if converted.startswith("~") else value


def keys(path: os.PathLike[str] | str) -> tuple[str, ...]:
    """Every stored form a row for this path may hold, the key first.

    The key; the resolved absolute path when it differs, which is what a row
    written before migration 014 holds; and the text as given when it is an
    absolute path that differs from both, which is what a writer that did not
    resolve stored. For `WHERE path IN (...)` probes: the column is never
    wrapped in a function, so the index still serves the lookup. A value that
    is not a path probes as itself.
    """
    text = os.fspath(path)
    try:
        stored = store(text)
    except NotAPath:
        return (text,)
    found = [stored]
    if stored.startswith("~"):
        found.append(str(_full(stored).resolve()))
    if os.path.isabs(text):
        found.append(text)
    return tuple(dict.fromkeys(found))


def same(left: os.PathLike[str] | str | None, right: os.PathLike[str] | str | None) -> bool:
    """Whether two paths name one repository. `None` matches nothing."""
    if left is None or right is None:
        return False
    try:
        return store(left) == store(right)
    except NotAPath:
        return os.fspath(left) == os.fspath(right)


def placeholders(values: tuple[str, ...] | list[str]) -> str:
    """`?, ?` for an `IN (...)` probe of `keys`."""
    return ", ".join("?" for _ in values)


def same_key(stored: str | None, column: str | None) -> bool:
    """Whether a stored repository value and a column value agree after 014.

    Migration 014 rewrote the repository columns to the `~/` key and left
    JSON values -- completion receipts, delivery proofs, publication
    journals -- as written, so a value from before it names the absolute
    path of the same repository (sd:1450). Both sides pass through
    `home_relative`, the function 014 ran, so they agree byte for byte.

    Unlike `same`, nothing touches the disk and nothing raises: a path
    outside the home and a non-string are compared as they are, and without
    `$HOME` both sides are compared as they are, so a legacy value then
    fails to match (closed). Two `None` values agree, as `==` has them.
    """
    def keyed(value):
        if not isinstance(value, str):
            return value
        try:
            return home_relative(value)
        except PathRefused:
            return value

    return keyed(stored) == keyed(column)


# ------------------------------------------------------------ migration only


def _homes() -> tuple[str, ...]:
    """The home as the environment spells it and as it resolves, longest first."""
    raw = str(home()).rstrip("/") or "/"
    real = str(home().resolve()).rstrip("/") or "/"
    return tuple(dict.fromkeys(sorted((real, raw), key=len, reverse=True)))


def home_relative(value: str | None) -> str | None:
    """`store` without the resolve: a pure string function on a stored value.

    Registered as `sd_home_relative` for migration 014 alone. A value already
    `~`-relative, or outside the home, comes back unchanged, which is what
    makes a replay of 014 a no-op.
    """
    if value is None or not value.startswith("/"):
        return value
    for base in _homes():
        if value == base:
            return "~"
        if value.startswith(base + "/"):
            return PREFIX + value[len(base) + 1:]
    return value


def home_absolute(value: str | None) -> str | None:
    """`expand` without the resolve, to the resolved home: 014's reverse.

    The resolved home, because the rows 014 rewrote were written resolved:
    `repos.add` resolved every path it registered, so this returns them
    byte-identical.
    """
    if value is None:
        return None
    base = str(home().resolve()).rstrip("/")
    if value == "~":
        return base or "/"
    if value.startswith(PREFIX):
        return base + "/" + value[len(PREFIX):]
    return value


def install(connection: sqlite3.Connection) -> None:
    """Register `sd_home_relative` and `sd_home_absolute` on a connection.

    `migrate` calls it before any migration runs, and so does the reference
    connection `backup._check_restore` executes every migration on. A
    connection without them fails 014 closed with "no such function".
    """
    connection.create_function("sd_home_relative", 1, home_relative, deterministic=True)
    connection.create_function("sd_home_absolute", 1, home_absolute, deterministic=True)
