"""`sd-db backup`. Prints what it did; raises what it could not do."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from ..backup import prune_older, require_mount, run
from ..database import connect
from ..errors import BackupError, SdDbError
from ..retention import prune, vacuum

#: The job's exit code when the BACKUP PASSED but the SOURCE is referentially
#: broken. It is not 1, because 1 means the documented failure -- the library
#: raised, nothing was written, no prune ran -- and `sd-db` mails that as
#: "sd-db backup FAILED". This run wrote a snapshot and verified it; calling it
#: a failed backup sends the operator looking for a snapshot that is on disk.
#: It is not 0 either: 0 is silent, and three silent nights over six orphan
#: rows is exactly what sd:744 was.
#:
#: It claims nothing about the checkpoint or the prune. BOTH returns below can
#: reach it, and the read-only-before-`migrate` path took neither -- it says
#: "prune skipped" on the same line. The summary says which ran; the exit code
#: carries the finding and only the finding.
#:
#: It is not 2, and that is the reason for the gap in the numbering.
#: `argparse` exits 2 for a usage error, so `--keep nope` leaves `main`
#: unreached, and `sd-db` reads a status and not a traceback. On 2 the two
#: are indistinguishable and a typo mails "the source is referentially
#: broken".
BROKEN_SOURCE = 3


def bulk_root() -> tuple[Path | None, str]:
    """`sd.bulk_storage_root` and, when it is set and unread, why: None and "" when it is unset.

    Read by `bin/sd_lib.core_setting`, the resolver the build log archive
    uses (`sd_operations.log_root`), from this checkout's own `bin/`: the
    job runs as `-m sd_db.jobs.backup` with only `lib/` on its path.
    """
    path = Path(__file__).resolve().parents[3] / "bin" / "sd_lib.py"
    try:
        spec = importlib.util.spec_from_file_location("sd_lib", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"{path} cannot be loaded")
        module = sys.modules.get("sd_lib")
        if module is None:
            module = sys.modules["sd_lib"] = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except BaseException:
                del sys.modules["sd_lib"]
                raise
        value = module.core_setting("bulk_storage_root")
    except Exception as error:  # noqa: BLE001 - an unread setting is unknown, never unset
        return None, f"sd.bulk_storage_root was not read: {type(error).__name__}: {error}"
    return (Path(os.path.expanduser(value)), "") if value else (None, "")


def retention(value: str) -> int | None:
    if value == "all":
        return None
    # Zero is refused: the count includes the backup this run takes.
    if value.isascii() and value.isdecimal() and int(value) > 0:
        return int(value)
    raise argparse.ArgumentTypeError("keep must be 'all' or a positive integer")


def days(value: str) -> int:
    if value.isascii() and value.isdecimal() and int(value) > 0:
        return int(value)
    raise argparse.ArgumentTypeError("keep-days must be a positive integer")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a verified backup; preserve every backup by default.")
    parser.add_argument("--destination", type=Path, default=None, metavar="PATH",
                        help="write backups under PATH instead of /Volumes/local/Backup/sd-backups, "
                             "which is refused while /Volumes/local is not mounted")
    kept = parser.add_mutually_exclusive_group()
    kept.add_argument("--keep", type=retention, default=None, metavar="all|N",
                      help="all disables deletion; N (at least 1) retains that many verified owned backups, this one included")
    kept.add_argument("--keep-days", type=days, default=None, metavar="N",
                      help="delete verified owned backups taken more than N days ago")
    parser.add_argument("--require-mount", type=Path, default=None, metavar="PATH",
                        help="refuse, writing nothing, unless PATH is a mounted volume "
                             "and the destination is on it")
    parser.add_argument("--no-row-prune", action="store_true",
                        help="skip the nightly row prune and its report item; for a "
                             "backup taken more often than nightly")
    parser.add_argument("--prune-only", action="store_true",
                        help="take no snapshot; delete copies under --destination, such as a mirror "
                             "on a share, whose checkpoint is more than --keep-days days old")
    parser.add_argument("--prune-unmanifested", action="store_true",
                        help="with --prune-only: also delete a dated folder with no manifest, such as a copy "
                             "stopped partway, named for a day more than twice --keep-days back; refused unless "
                             "the newest manifest shows the root holds copies written elsewhere")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --prune-only: list what would be deleted and delete nothing")
    arguments = parser.parse_args(argv)
    home = Path(os.environ.get("HOME", "~")).expanduser()
    if arguments.dry_run and not arguments.prune_only:
        print("sd-db backup: --dry-run needs --prune-only", file=sys.stderr)
        return 1
    if arguments.prune_unmanifested and not arguments.prune_only:
        print("sd-db backup: --prune-unmanifested needs --prune-only", file=sys.stderr)
        return 1
    if arguments.prune_only:
        # The hourly job's NAS prune (sd:4011). The copies were written in
        # another directory, so their manifests name it; `moved` verifies
        # them by their hashes and checkpoint instead.
        if arguments.destination is None or arguments.keep_days is None:
            print("sd-db backup: --prune-only needs --destination and --keep-days", file=sys.stderr)
            return 1
        try:
            if arguments.require_mount is not None:
                require_mount(arguments.require_mount, arguments.destination)
            # Flushed: the job's time bound TERMs this process, and the log
            # must still name every copy removed before it (fleet sd:5543).
            removed = prune_older(arguments.destination, arguments.keep_days, now=datetime.now(UTC), moved=True,
                                  dry_run=arguments.dry_run, unmanifested=arguments.prune_unmanifested,
                                  report=lambda line: print(f"sd-db backup: {line}", flush=True))
        except (BackupError, OSError) as failure:
            print(f"sd-db backup: {failure}", file=sys.stderr)
            return 1
        copies = f"{len(removed)} {'copy' if len(removed) == 1 else 'copies'} older than {arguments.keep_days} days"
        if arguments.prune_unmanifested:
            copies += f", or with no manifest older than {2 * arguments.keep_days} days,"
        if arguments.dry_run:
            for path in removed:
                print(f"sd-db backup: would remove {path}")
            print(f"sd-db backup: dry run: {copies} would be removed under {arguments.destination}; nothing was deleted")
        else:
            print(f"sd-db backup: {copies} removed under {arguments.destination}")
        return 0
    try:
        snapshot = run(home=home, destination=arguments.destination, keep=arguments.keep,
                       keep_days=arguments.keep_days, mount=arguments.require_mount)
    except BackupError as failure:
        print(f"sd-db backup: {failure}", file=sys.stderr)
        return 1
    rows = sum(snapshot.counts.values())
    beside = ", ".join(snapshot.configuration) or "no configuration files"
    # A referentially broken source is a finding about the store, not a failed
    # backup: the snapshot is on disk and is verified. It is NOT the thing to
    # recover from -- `_check_restore` refuses a candidate carrying orphans,
    # so the snapshot is a forensic copy until the source is repaired and a
    # later night copies a clean store. The finding therefore rides on the end
    # of the normal summary rather than replacing it, and goes to stderr under
    # `BROKEN_SOURCE`, which is neither the prune failure's 1 nor `argparse`'s
    # 2. Exit 0 was the actual defect in sd:744: three nightly runs said
    # "restored and compared" and nobody had a reason to read the log for
    # three days.
    broken = ""
    if snapshot.violations:
        where = ", ".join(sorted({str(row[0]) for row in snapshot.violations}))
        broken = (f"; BROKEN: {len(snapshot.violations)} foreign key "
                  f"violation(s) in the source, in {where}")
    stream = sys.stderr if snapshot.violations else sys.stdout
    if not snapshot.checkpointed:
        # The source could not be written: it waits for `migrate`. The copy
        # is on disk and compared by counts; the prune is a write and the
        # retention table would refuse a snapshot no checkpoint row proves,
        # so neither runs, and `--keep` never counts this directory. File
        # retention of the owned backups did run in `run`, so say what it removed.
        print(
            f"sd-db backup: {snapshot.directory} — {rows} row(s) across "
            f"{len(snapshot.counts)} table(s), compared by counts; {beside}; taken read-only "
            f"before `migrate`, so no checkpoint row proves it and retention will not own it; "
            f"{len(snapshot.removed)} old snapshot(s) removed; row prune skipped{broken}",
            file=stream,
        )
        return BROKEN_SOURCE if snapshot.violations else 0
    if arguments.no_row_prune:
        # An hourly backup is not a night. The row prune files one `report`
        # item per run, so running it every hour would put 24 of them a day
        # in `planning` -- the accumulation `retention` exists to settle. The
        # nightly job still runs it; this run only takes and keeps snapshots.
        print(
            f"sd-db backup: {snapshot.directory} — {rows} row(s) across "
            f"{len(snapshot.counts)} table(s), restored and compared; {beside}; "
            f"{len(snapshot.removed)} old snapshot(s) removed; row prune skipped "
            f"(--no-row-prune){broken}",
            file=stream,
        )
        return BROKEN_SOURCE if snapshot.violations else 0
    # The nightly row prune runs here and nowhere earlier: `run` raised on
    # anything short of a restored-and-compared snapshot, so reaching this
    # line is the backup having passed. A prune that fails is the job
    # failing -- the backup is on disk either way, and the mail says which.
    root, unresolved = bulk_root()
    try:
        connection = connect(home=home)
        try:
            pruned = prune(connection, snapshot, bulk_root=root, unresolved=unresolved)
            try:
                vacuumed = vacuum(connection)
            except (sqlite3.Error, SdDbError) as failure:
                # VACUUM is all or nothing: it changed nothing, and the free
                # pages it reads make the next night try again.
                print(f"sd-db backup: the backup passed at {snapshot.directory} and the prune ran: {pruned} "
                      f"(report #{pruned.report}); the VACUUM did not run: {failure}{broken}", file=sys.stderr)
                return 1
        finally:
            connection.close()
    except (OSError, SdDbError) as failure:
        # `{broken}` belongs here as much as on the success line. Without it a
        # night that both failed to prune AND found orphans reports only the
        # prune, and the finding this whole change exists to surface is the
        # one thing the mail does not say.
        print(f"sd-db backup: the backup passed at {snapshot.directory}; "
              f"the prune did not run: {failure}{broken}",
              file=sys.stderr)
        return 1
    print(
        f"sd-db backup: {snapshot.directory} — {rows} row(s) across "
        f"{len(snapshot.counts)} table(s), restored and compared; {beside}; "
        f"{len(snapshot.removed)} old snapshot(s) removed; pruned: {pruned} "
        f"(report #{pruned.report}); {_archived(pruned)}{vacuumed}{broken}",
        file=stream,
    )
    return BROKEN_SOURCE if snapshot.violations else 0


def _archived(pruned) -> str:
    """What the checkpoint step archived, or why it kept rows, as a clause ending in "; "; empty when neither."""
    compacted = pruned.compacted
    if compacted is None:
        return ""
    if compacted.path is not None:
        return f"{compacted.archived} checkpoint row(s) archived to {compacted.path}; "
    return f"{compacted.why}; " if compacted.why else ""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
