"""The writing pack and dashboard share one database workflow."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import sd_handoff_rows
import sd_lib
from sd_notes import item_id
from sd_work import WorkRefusal

#: Verbs that write journals or publish, so they read only merged prose (sd:2024).
REGISTERED_ONLY = frozenset({"import", "register", "cutover", "recover", "publication-render", "publication-recover",
                             "publication-claim", "publication-status", "publication-dispatch",
                             "publication-receipt", "publication-reconcile", "publication-abandon"})

#: Verbs that read the checkout's pieces, so a checkout with none is refused (sd:1660, sd:1803).
CONTENT_ONLY = frozenset({"list", "import", "verify"})

#: Review verbs that write only the piece's own report files, and the library function each needs (sd:3301).
REVIEWS = {"adversarial": "adversarial_brief", "reconcile": "reconcile_companion"}

#: Verbs that write one piece, so its Drive mirror is queued again after them (pack sd:7243); `publication-reconcile`
#: moves a piece to `published`, and with it the copy to Writing/Published.
MIRRORED = frozenset({"promote", "register", "stage", "metadata", "gate", "park", "adversarial", "reconcile",
                      "publication-reconcile"})


def _mirror(connection: Any, item: int) -> None:
    """Queue the piece's files whose text changed, from review on, as a brief add queues its brief (pack sd:7243).

    Best effort: the verb's write is committed, so a failure here changes neither its exit nor its JSON. It is named
    on stderr, and the next verb, or the Writing page's Stage or Park, queues the piece.
    """
    try:
        import sd_research_publish  # noqa: PLC0415 - the pack's `bin/`, beside this file

        lines = sd_research_publish.enqueue_pieces(connection, {item})
    except Exception as error:  # noqa: BLE001 - reported; the verb's result stands
        lines = [f"mirror: piece not queued: {error or type(error).__name__}"]
    for line in lines:
        print(line, file=sys.stderr)


def gate_bin() -> str:
    """The shared adversarial gate: `$ADVERSARIAL_GATE_BIN`, else the system checkout's copy.

    `isfile` and `X_OK` together: a directory answers True to `X_OK` alone.
    """
    def usable(path: str | None) -> bool:
        return bool(path) and os.path.isfile(str(path)) and os.access(str(path), os.X_OK)

    named = os.environ.get("ADVERSARIAL_GATE_BIN")
    if usable(named):
        return str(named)
    default = str(Path.home() / "repos/system/local-adversarial-gate/adversarial-gate.sh")
    if usable(default):
        return default
    raise WorkRefusal(f"cannot find the shared adversarial gate: $ADVERSARIAL_GATE_BIN is unset or not executable, "
                      f"and {default} is not executable")


def _render(brief: dict) -> str:
    """The `writing-draft` lens with this piece's paths, checked before codex sees it."""
    command = [gate_bin(), "render", "--lens", "writing-draft",
               "--set", f"DRAFT_PATH={brief['draft']}", "--set", f"RESEARCH_PATH={brief['research']}"]
    try:
        rendered = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    except subprocess.TimeoutExpired:
        raise WorkRefusal("adversarial-gate render timed out after 30s") from None
    except OSError as error:
        raise WorkRefusal(f"cannot run {command[0]}: {error}") from None
    if rendered.returncode:
        raise WorkRefusal((rendered.stderr or rendered.stdout).strip()[:500] or "adversarial-gate render failed")
    prompt = rendered.stdout
    # Composed in another repository, so checked rather than assumed: a literal
    # placeholder would send codex to read a file that does not exist.
    left = sorted(set(re.findall(r"\{[A-Z_]+\}", prompt)))
    if left:
        raise WorkRefusal(f"the composed prompt still carries {', '.join(left)}; "
                          "the writing-draft lens and sd writing disagree about which keys it takes")
    if len(prompt.split()) < 100:
        raise WorkRefusal(f"adversarial-gate render returned {len(prompt.split())} words; that is not the prompt")
    return prompt


def _codex(root: str, prompt: str, *, model: str | None, timeout: int, effort: str | None = None) -> str:
    """Run codex read-only in `root` at `model` and `effort`, codex's own when None, with the prompt on stdin;
    return its final message.

    codex runs in a group of its own (`sd_lib.run_group`), so a timeout or a
    SIGTERM, even one during the spawn (sd:5801), stops the sandbox and the
    reviewer under the launcher, not only the launcher.
    """
    with tempfile.TemporaryDirectory(prefix="sd-writing-adversarial-") as scratch:
        out = Path(scratch) / "report.md"
        command = ["codex", "exec", *(["-m", model] if model else []),
                   *(["-c", f"model_reasoning_effort={effort}"] if effort else []), "-s", "read-only", "-C", root,
                   "-o", str(out), "-"]
        try:
            ran = sd_lib.run_group(command, cwd=Path.cwd(), env=dict(os.environ), timeout=timeout,
                                   input_text=prompt, encoding="utf-8")
        except sd_lib.GroupTimeout:
            raise WorkRefusal(f"codex exceeded --timeout {timeout}s; rerun or raise the limit") from None
        except OSError as error:
            raise WorkRefusal(f"cannot run codex ({error}); report the adversarial gate as skipped, never as passed") from None
        if ran.returncode:
            sys.stderr.write(ran.stderr[-2000:])
            raise WorkRefusal(f"codex exited {ran.returncode}")
        report = out.read_text(encoding="utf-8") if out.is_file() else ""
        if not report.strip():
            raise WorkRefusal("codex produced no final message; nothing written")
        return report


def adversarial(writing: Any, connection: Any, item: int, args: argparse.Namespace) -> dict:
    """One hostile read of the current draft by another vendor's model, written to `adversarial.md`.

    Its findings are hypotheses: the report records which prose it read and
    decides nothing. `sd writing gate` records the verdict after a reader checks them.
    """
    brief = writing.adversarial_brief(connection, item)
    if not brief["research_exists"]:
        print(f"# warning: no research.md for {brief['piece']}; the citation-strength axis "
              "has nothing to check against", file=sys.stderr)
    prompt = _render(brief)
    print(f"# codex adversarial review: {brief['piece']} (sandbox=read-only, {brief['root']})", file=sys.stderr)
    report = _codex(brief["root"], prompt, model=args.model, timeout=args.timeout, effort=getattr(args, "effort", None))
    result = writing.record_adversarial(connection, item, report, digest=brief["digest"], record=brief["record"],
                                        generation=brief["generation"], index_sha256=brief["index_sha256"])
    print(f"# reviewed draft {result['digest']}", file=sys.stderr)
    if not result["current"]:
        print("# draft changed during review; this report is stale and cannot clear readiness", file=sys.stderr)
    counts = result["confidence"]
    print(f"# confidence tags: CERTAIN={counts['CERTAIN']} LIKELY={counts['LIKELY']} "
          f"SPECULATIVE={counts['SPECULATIVE']}", file=sys.stderr)
    if result["verdict"]:
        print(f"# {result['verdict']}", file=sys.stderr)
    return result


def run(args: argparse.Namespace) -> int:
    sd_db = sd_handoff_rows.library()
    try:
        import sd_db.writing as writing
    except ImportError:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs for writing controls") from None
    root = sd_lib.repo_root()
    if root is None:
        raise WorkRefusal("writing controls require the writing Git checkout as the current directory")
    root = root.resolve()
    # A linked worktree keys rows to the main checkout and reads its own files (sd:2024).
    registered = sd_lib.main_worktree_root(root)
    repo = sd_lib.stored_repo(registered)
    action = args.writing_action
    linked = registered != root
    if linked and action in REGISTERED_ONLY:
        raise WorkRefusal(f"sd writing {action} runs only in the main checkout {registered}, not a linked worktree")
    # Looked up, not imported: the gate builds an older sd_db that lacks it.
    checkout: Any = getattr(writing, "checkout", None)
    if linked and checkout is None:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs to run writing controls from a worktree")
    promote: Any = getattr(writing, "promote", None)
    if action == "promote" and promote is None:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs to promote an idea")
    # Looked up, as `checkout` is: an older installed sd_db lacks them.
    review: Any = getattr(writing, REVIEWS[action], None) if action in REVIEWS else None
    if action in REVIEWS and review is None:
        raise WorkRefusal(f"install the current library build for sd writing {action}")
    note = None
    if action == "reconcile":
        # From a file, so prose with backticks never passes through a shell.
        try:
            note = (Path(args.note_file).read_text(encoding="utf-8") if args.note_file else args.note).strip()
        except OSError as error:
            raise WorkRefusal(f"cannot read --note-file: {error}") from None
    pieces = any((root / name).is_dir() for name in ("content", "content-parked"))
    if action in CONTENT_ONLY and not pieces:
        # Another checkout has no pieces, so these would answer on zero files and zero rows (sd:1660, sd:1803).
        raise WorkRefusal(f"{root} holds no content/ folder, so there is nothing to {action}; "
                          f"run sd writing {action} from the writing Git checkout")
    write = action in {"cutover", "recover", "register", "promote", "stage", "metadata", "gate", "park",
                       "publication-claim", "publication-dispatch", "publication-receipt", "publication-reconcile", "publication-abandon", "publication-recover"} or (
        action == "import" and args.apply)
    connection = sd_handoff_rows.connect(sd_db, write=write)
    scope = contextlib.ExitStack()
    try:
        if linked:
            scope.enter_context(checkout(repo, root))
        who = getpass.getuser()
        revision = getattr(args, "if_revision", None)
        result: Any
        item: Any = None
        if action == "list":
            result = writing.list_pieces(connection, repo, include_parked=args.all)
            if args.status:
                result = [row for row in result if row["stage"] == args.status]
        elif action == "import":
            result = (writing.import_pieces(connection, repo, who=who) if args.apply
                      else writing.cutover_preview(connection, repo))
        elif action == "verify":
            result = writing.verify_pieces(connection, repo)
        elif action == "cutover":
            result = writing.cutover_pieces(
                connection, repo, expected_fingerprint=args.expected_digest, who=who)
        elif action == "recover":
            result = writing.recover_cutover(connection, repo, who=who)
        elif action == "register":
            result = writing.import_piece(connection, repo, args.piece, path=args.path, who=who)
            registered_row = result.get("item") if isinstance(result, dict) else None
            item = registered_row.get("id") if isinstance(registered_row, dict) else None
        elif action == "promote":
            item = args.item
            # A writing checkout is the target; from any other the library picks the one
            # repository that registers pieces and refuses several (sd:1994, R10-D6).
            try:
                result = promote(connection, args.item, slug=args.slug, repo=repo if pieces else None, who=who,
                                 expected_revision=revision)
            except sd_db.SdDbError as error:
                if pieces:
                    raise
                raise WorkRefusal(f"{error}; run sd writing promote from that repository's checkout") from error
        else:
            row = writing.piece_for_key(connection, repo, args.piece)
            if row is None:
                raise WorkRefusal(f"no database piece {args.piece!r}; register or import it first")
            item = row["id"]
            if action.startswith("publication-"):
                from sd_db import publication

                def read_json(path):
                    source = Path(path)
                    if source.stat().st_size > 32 * 1024 * 1024:
                        raise WorkRefusal("publication input exceeds 32 MiB")
                    return json.loads(source.read_text(encoding="utf-8"))

                context = read_json(args.context_file) if getattr(args, "context_file", None) else None
                if action == "publication-render":
                    result = publication.render_payload(connection, item)
                elif action == "publication-recover":
                    result = publication.recover_journal(connection, item, repair_incomplete=args.repair_incomplete, who=who)
                elif action == "publication-claim":
                    result = publication.create_claim(connection, item, read_json(args.payload_file), context,
                                                      expected_revision=revision, who=who)
                elif action == "publication-status":
                    result = publication.claim_state(connection, item, args.claim, include_payload=args.include_payload)
                elif action == "publication-dispatch":
                    result = publication.dispatch(connection, item, args.claim, context, confirmed=args.confirmed, who=who)
                elif action == "publication-receipt":
                    result = publication.receipt(connection, item, args.claim, args.operation, read_json(args.receipt_file))
                elif action == "publication-reconcile":
                    result = publication.reconcile(connection, item, args.claim, context, read_json(args.evidence_file), who=who)
                else:
                    result = publication.abandon(connection, item, args.claim, reason=args.reason, leave=args.leave, who=who)
            elif action == "adversarial":
                result = adversarial(writing, connection, item, args)
            elif action == "reconcile":
                result = review(connection, item, args.artifact, note=note)
            elif action == "get":
                result = writing.piece_state(connection, item)
            elif action == "readiness":
                result = writing.readiness(connection, item)
            elif action == "stage":
                result = writing.change_stage(
                    connection, item, args.stage, who=who, correct=args.correct,
                    reason=args.reason, expected_revision=revision, confirmed=args.confirmed)
            elif action == "metadata":
                result = writing.update_piece_metadata(
                    connection, item, json.loads(args.changes_json), who=who,
                    expected_revision=revision)
            elif action == "park":
                result = writing.park_piece(connection, item, parked=not args.revive,
                                           who=who, expected_revision=revision)
            elif action == "gate":
                result = writing.record_gate(
                    connection, item, args.artifact, verdict=args.verdict,
                    findings=json.loads(args.findings_json), reason=args.reason,
                    who=who, expected_revision=revision, reviewed_digest=args.reviewed_digest)
            else:
                raise WorkRefusal(f"unknown writing operation: {action}")
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif isinstance(result, list):
            for entry in result:
                print(f"{sd_db.repos.item_ref(entry['id'], entry.get('repo'))}  {entry['stage']}  {entry['title']}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if action in MIRRORED and item is not None:
            _mirror(connection, item)
        return 1 if action == "verify" and not result["ok"] else 0
    except (sd_db.SdDbError, ValueError, OSError) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        scope.close()
        connection.close()


def register(groups: Any) -> None:
    writing = groups.add_parser("writing", help="shared piece stages, evidence, and database cutover")
    verbs = writing.add_subparsers(dest="verb", required=True)
    for action in ("list", "get", "readiness", "import", "verify", "cutover", "recover", "register", "promote",
                   "stage", "metadata", "gate", "park", "adversarial", "reconcile", "publication-render", "publication-recover", "publication-claim", "publication-status",
                   "publication-dispatch", "publication-receipt", "publication-reconcile", "publication-abandon"):
        parser = verbs.add_parser(action)
        parser.set_defaults(handler=run, writing_action=action)
        parser.add_argument("--json", action="store_true")
        if action.startswith("publication-"):
            parser.add_argument("--piece", required=True)
            if action not in {"publication-claim", "publication-render", "publication-recover"}:
                parser.add_argument("--claim", required=True)
            if action in {"publication-claim", "publication-dispatch", "publication-reconcile"}:
                parser.add_argument("--context-file", required=True)
            if action == "publication-claim":
                parser.add_argument("--payload-file", required=True)
                parser.add_argument("--if-revision")
            elif action == "publication-recover":
                parser.add_argument("--repair-incomplete", action="store_true")
            elif action == "publication-status":
                parser.add_argument("--include-payload", action="store_true")
            elif action == "publication-dispatch":
                parser.add_argument("--confirmed", action="store_true")
            elif action == "publication-receipt":
                parser.add_argument("--operation", required=True)
                parser.add_argument("--receipt-file", required=True)
            elif action == "publication-reconcile":
                parser.add_argument("--evidence-file", required=True)
            elif action == "publication-abandon":
                parser.add_argument("--reason", required=True)
                parser.add_argument("--leave", action="store_true")
        elif action in {"get", "readiness", "register", "stage", "metadata", "gate", "park", "adversarial", "reconcile"}:
            parser.add_argument("--piece", required=True, help="YEAR/slug")
        if action in {"promote", "stage", "metadata", "gate", "park"}:
            parser.add_argument("--if-revision")
        if action == "list":
            parser.add_argument("--all", action="store_true", help="include parked pieces")
            parser.add_argument("--status", help="filter by writing stage")
        elif action == "import":
            parser.add_argument("--apply", action="store_true", help="write import rows; default previews only")
        elif action == "cutover":
            parser.add_argument("--expected-digest", required=True, help="fingerprint from the preview")
        elif action == "register":
            parser.add_argument("--path", help="relative index.md path for a parked piece")
        elif action == "promote":
            parser.add_argument("item", type=item_id, help="the idea row to register as a piece, as 234, sd:234 or item:234")
            parser.add_argument("--slug", help="piece slug; default from the idea title")
        elif action == "stage":
            parser.add_argument("--stage", required=True)
            parser.add_argument("--correct", action="store_true")
            parser.add_argument("--reason")
            parser.add_argument("--confirmed", action="store_true",
                                help="confirm recording an existing publication, without publishing")
        elif action == "metadata":
            parser.add_argument("--changes-json", required=True)
        elif action == "park":
            parser.add_argument("--revive", action="store_true", help="return a parked piece to active work")
        elif action == "gate":
            parser.add_argument("--artifact", required=True, choices=("fact-check", "adversarial"))
            parser.add_argument("--verdict", required=True, choices=("pass", "fail"))
            parser.add_argument("--findings-json", default="[]")
            parser.add_argument("--reason", required=True)
            parser.add_argument("--reviewed-digest", help="draft digest actually reviewed, required for an unstamped report")
        elif action == "adversarial":
            parser.add_argument("--model", help="codex model; default is codex's own")
            parser.add_argument("--timeout", type=int, default=1800, help="seconds before codex is stopped (default 1800)")
        elif action == "reconcile":
            parser.add_argument("--artifact", required=True, choices=("adversarial", "research"))
            note = parser.add_mutually_exclusive_group(required=True)
            note.add_argument("--note", help="for adversarial: what became of each finding; "
                                             "for research: which claims the revision added and where each is sourced")
            note.add_argument("--note-file", help="read the note from a file; use it when the note holds backticks")
