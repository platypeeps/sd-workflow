"""`sd writing import` names the operator to the library that records it."""

import argparse
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import sd_db
import sd_db.publication
import sd_db.writing as writing

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "path", [str(ROOT / "bin"), *sys.path]):
    cli = importlib.import_module("sd_writing")


class WritingImport(unittest.TestCase):
    """`writing.import_pieces` takes `who` with no default (sd:749).

    The library call is an autospec mock, so leaving `who` out is the
    library's own TypeError rather than a mock that accepts anything.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name).resolve()
        (self.repo / "content").mkdir()
        self.parser = argparse.ArgumentParser()
        cli.register(self.parser.add_subparsers(required=True))
        self.connection = Mock()
        self.connect = Mock(return_value=self.connection)
        for patcher in (
            patch.object(cli.sd_handoff_rows, "library", return_value=sd_db),
            patch.object(cli.sd_handoff_rows, "connect", self.connect),
            patch.object(cli.sd_lib, "repo_root", return_value=self.repo),
            patch.object(cli.getpass, "getuser", return_value="operator"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.imported = self.autospec("import_pieces", {"items": [], "warnings": []})
        self.preview = self.autospec("cutover_preview", {"pieces": []})

    def autospec(self, name, result):
        patcher = patch.object(writing, name, autospec=True, return_value=result)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def call(self, *argv):
        arguments = self.parser.parse_args(["writing", "import", "--json", *argv])
        with redirect_stdout(io.StringIO()) as output:
            code = arguments.handler(arguments)
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_applied_import_names_the_operator(self):
        self.assertEqual(self.call("--apply"), {"items": [], "warnings": []})
        self.imported.assert_called_once_with(self.connection, str(self.repo), who="operator")
        self.connect.assert_called_once_with(sd_db, write=True)
        self.preview.assert_not_called()
        self.connection.close.assert_called_once()

    def test_preview_writes_nothing_and_names_no_operator(self):
        self.assertEqual(self.call(), {"pieces": []})
        self.preview.assert_called_once_with(self.connection, str(self.repo))
        self.connect.assert_called_once_with(sd_db, write=False)
        self.imported.assert_not_called()


class WritingPromote(unittest.TestCase):
    """`sd writing promote` hands an idea row to the library that registers it (sd:1994).

    The library call is an autospec mock, so a keyword the library lacks is
    its own TypeError rather than a mock that accepts anything.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name).resolve()
        self.parser = argparse.ArgumentParser()
        cli.register(self.parser.add_subparsers(required=True))
        self.connection = Mock()
        self.connect = Mock(return_value=self.connection)
        for patcher in (
            patch.object(cli.sd_handoff_rows, "library", return_value=sd_db),
            patch.object(cli.sd_handoff_rows, "connect", self.connect),
            patch.object(cli.sd_lib, "repo_root", return_value=self.repo),
            patch.object(cli.sd_lib, "stored_repo", side_effect=str),
            patch.object(cli.getpass, "getuser", return_value="operator"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(writing, "promote", autospec=True, return_value={"item": {"piece": "2026/a"}})
        self.promoted = patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, *argv):
        arguments = self.parser.parse_args(["writing", "promote", *argv, "--json"])
        with redirect_stdout(io.StringIO()) as output:
            code = arguments.handler(arguments)
        return code, json.loads(output.getvalue())

    def test_a_writing_checkout_is_the_target(self):
        """The current checkout names the repository, never a flag (R10-D6)."""
        (self.repo / "content").mkdir()
        self.assertEqual((0, {"item": {"piece": "2026/a"}}), self.call("12"))
        self.promoted.assert_called_once_with(self.connection, 12, slug=None, repo=str(self.repo),
                                              who="operator", expected_revision=None)
        self.connect.assert_called_once_with(sd_db, write=True)
        self.connection.close.assert_called_once()

    def test_another_checkout_lets_the_library_pick_the_target(self):
        """With no pieces here, the library picks the one repository that registers them."""
        self.assertEqual(0, self.call("12")[0])
        self.promoted.assert_called_once_with(self.connection, 12, slug=None, repo=None, who="operator",
                                              expected_revision=None)

    def test_promote_passes_slug_and_revision(self):
        (self.repo / "content-parked").mkdir()
        self.call("12", "--slug", "a", "--if-revision", "r1")
        self.promoted.assert_called_once_with(self.connection, 12, slug="a", repo=str(self.repo),
                                              who="operator", expected_revision="r1")

    def test_promote_takes_no_repository_flag(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parser.parse_args(["writing", "promote", "12", "--repo", "~/repos/writing"])

    def test_a_library_refusal_is_a_work_refusal(self):
        self.promoted.side_effect = sd_db.SdDbError("several repositories register pieces; name the target repository")
        with self.assertRaisesRegex(cli.WorkRefusal, "name the target repository; run sd writing promote "
                                                     "from that repository's checkout"):
            self.call("12")
        self.connection.close.assert_called_once()

    def test_a_refusal_in_a_writing_checkout_names_no_other_checkout(self):
        (self.repo / "content").mkdir()
        self.promoted.side_effect = sd_db.SdDbError("item 12 is not an idea")
        with self.assertRaisesRegex(cli.WorkRefusal, r"^item 12 is not an idea$"):
            self.call("12")

    def test_an_older_library_refuses_by_name(self):
        with patch.object(writing, "promote", None):
            with self.assertRaisesRegex(cli.WorkRefusal, "update this pack checkout.* to promote"):
                self.call("12")
        self.connect.assert_not_called()


class WritingVerifyCheckout(unittest.TestCase):
    """`sd writing verify` refuses where there is nothing to verify (sd:1660)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name).resolve()
        self.parser = argparse.ArgumentParser()
        cli.register(self.parser.add_subparsers(required=True))
        self.connection = Mock()
        self.connect = Mock(return_value=self.connection)
        self.root = patch.object(cli.sd_lib, "repo_root", return_value=self.repo)
        for patcher in (
            patch.object(cli.sd_handoff_rows, "library", return_value=sd_db),
            patch.object(cli.sd_handoff_rows, "connect", self.connect),
            self.root,
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(writing, "verify_pieces", autospec=True, return_value={
            "ok": True, "owner": "row", "files": 1, "rows": 1, "differences": []})
        self.verified = patcher.start()
        self.addCleanup(patcher.stop)

    def run_verify(self):
        arguments = self.parser.parse_args(["writing", "verify", "--json"])
        with redirect_stdout(io.StringIO()) as output:
            code = arguments.handler(arguments)
        return code, output.getvalue()

    def test_outside_any_checkout_refuses(self):
        self.root.stop()
        with patch.object(cli.sd_lib, "repo_root", return_value=None):
            with self.assertRaisesRegex(cli.WorkRefusal, "writing Git checkout"):
                self.run_verify()
        self.root.start()
        self.connect.assert_not_called()

    def test_checkout_without_content_refuses(self):
        with self.assertRaisesRegex(cli.WorkRefusal, "no content/ folder.*writing Git checkout"):
            self.run_verify()
        self.connect.assert_not_called()
        self.verified.assert_not_called()

    def test_writing_checkout_verifies(self):
        (self.repo / "content").mkdir()
        code, output = self.run_verify()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["files"], 1)
        self.verified.assert_called_once_with(self.connection, str(self.repo))


class WritingContentCheckout(unittest.TestCase):
    """`list` and `import` refuse where there are no pieces, as `verify` does (sd:1803).

    In a checkout with no content/ folder they printed empty results and
    exited 0, which reads as "this repository has no pieces" rather than
    "you are in the wrong checkout".
    """

    VERBS = (("list", "list_pieces", []), ("import", "cutover_preview", {"pieces": []}),
             ("import --apply", "import_pieces", {"items": [], "warnings": []}))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name).resolve()
        self.parser = argparse.ArgumentParser()
        cli.register(self.parser.add_subparsers(required=True))
        self.connection = Mock()
        self.connect = Mock(return_value=self.connection)
        for patcher in (
            patch.object(cli.sd_handoff_rows, "library", return_value=sd_db),
            patch.object(cli.sd_handoff_rows, "connect", self.connect),
            patch.object(cli.sd_lib, "repo_root", return_value=self.repo),
            *(patch.object(writing, name, autospec=True, return_value=result)
              for _verb, name, result in self.VERBS),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_verb(self, verb):
        arguments = self.parser.parse_args(["writing", *verb.split(), "--json"])
        with redirect_stdout(io.StringIO()) as output:
            code = arguments.handler(arguments)
        return code, output.getvalue()

    def test_checkout_without_content_refuses_each_verb(self):
        for verb, name, _result in self.VERBS:
            with self.subTest(verb=verb):
                action = verb.split()[0]
                with self.assertRaisesRegex(
                        cli.WorkRefusal, f"no content/ folder, so there is nothing to {action}; "
                                         f"run sd writing {action} from the writing Git checkout"):
                    self.run_verb(verb)
                getattr(writing, name).assert_not_called()
        self.connect.assert_not_called()

    def test_writing_checkout_answers_each_verb(self):
        (self.repo / "content").mkdir()
        for verb, name, result in self.VERBS:
            with self.subTest(verb=verb):
                code, output = self.run_verb(verb)
                self.assertEqual((0, result), (code, json.loads(output)))
                getattr(writing, name).assert_called_once()

    def test_a_parked_only_checkout_still_answers(self):
        """`content-parked` alone counts, as it does for `verify`."""
        (self.repo / "content-parked").mkdir()
        self.assertEqual(0, self.run_verb("list")[0])


BRIEF = {"piece": "2026/a", "draft": "content/2026/a/index.md", "research": "content/2026/a/research.md",
         "research_exists": True, "digest": "0123456789ab", "generation": 2, "index_sha256": "e" * 64,
         "record": "<!-- adversarial-reviewed: words=150 sentences=0123abcd -->", "words": 150}


class WritingReview(unittest.TestCase):
    """`adversarial` runs the shared gate prompt through codex; `reconcile` restamps a companion (sd:3301).

    The gate binary and codex are stubs on a private PATH; the library is an
    autospec mock, so a keyword it lacks is its own TypeError.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "repo"
        (self.repo / "content").mkdir(parents=True)
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        self.parser = argparse.ArgumentParser()
        cli.register(self.parser.add_subparsers(required=True))
        self.connection = Mock()
        self.connect = Mock(return_value=self.connection)
        self.gate = self.stub("adversarial-gate", 'printf "%s\\n" "$@" > "$STUBS/gate.argv"\n'
                                                  'for n in $(seq 120); do printf "word "; done\necho\n')
        self.stub("codex", 'printf "%s\\n" "$@" > "$STUBS/codex.argv"\ncat > "$STUBS/codex.stdin"\n'
                           'while [ $# -gt 0 ]; do\n  if [ "$1" = -o ]; then printf "# Review\\n" > "$2"; fi\n'
                           '  shift\ndone\n')
        environment = {"PATH": f"{self.stubs}{os.pathsep}/usr/bin:/bin", "ADVERSARIAL_GATE_BIN": str(self.gate),
                       "STUBS": str(self.stubs)}
        for patcher in (
            patch.object(cli.sd_handoff_rows, "library", return_value=sd_db),
            patch.object(cli.sd_handoff_rows, "connect", self.connect),
            patch.object(cli.sd_lib, "repo_root", return_value=self.repo),
            patch.object(cli.sd_lib, "stored_repo", side_effect=str),
            patch.object(cli.getpass, "getuser", return_value="operator"),
            patch.object(writing, "piece_for_key", return_value={"id": 7}),
            patch.dict(os.environ, environment),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.brief = self.autospec("adversarial_brief", {**BRIEF, "root": str(self.repo)})
        self.recorded = self.autospec("record_adversarial", {"path": "content/2026/a/adversarial.md", "current": True,
                                                             "digest": "0123456789ab", "verdict": None,
                                                             "confidence": {"CERTAIN": 0, "LIKELY": 0, "SPECULATIVE": 0}})
        self.reconciled = self.autospec("reconcile_companion", {"piece": "2026/a", "artifact": "research",
                                                                "digest": "0123456789ab", "was": {"state": "stale"}})

    def stub(self, name, body):
        path = self.stubs / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return path

    def autospec(self, name, result):
        patcher = patch.object(writing, name, autospec=True, return_value=result)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def call(self, *argv):
        arguments = self.parser.parse_args(["writing", *argv, "--json"])
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            code = arguments.handler(arguments)
        return code, json.loads(output.getvalue())

    def test_adversarial_renders_the_lens_and_runs_codex_read_only_on_the_checkout(self):
        code, result = self.call("adversarial", "--piece", "2026/a")
        self.assertEqual((code, result["path"]), (0, "content/2026/a/adversarial.md"))
        self.assertEqual((self.stubs / "gate.argv").read_text().splitlines(),
                         ["render", "--lens", "writing-draft", "--set", "DRAFT_PATH=content/2026/a/index.md",
                          "--set", "RESEARCH_PATH=content/2026/a/research.md"])
        argv = (self.stubs / "codex.argv").read_text().splitlines()
        self.assertEqual(argv[:6] + argv[7:], ["exec", "-s", "read-only", "-C", str(self.repo), "-o", "-"])
        self.assertTrue(argv[6].endswith("report.md"))
        self.assertTrue((self.stubs / "codex.stdin").read_text().startswith("word word"))
        self.recorded.assert_called_once_with(self.connection, 7, "# Review\n", digest="0123456789ab",
                                              record=BRIEF["record"], generation=2, index_sha256="e" * 64)
        self.connect.assert_called_once_with(sd_db, write=False)
        self.connection.close.assert_called_once()

    def test_adversarial_passes_a_model(self):
        self.call("adversarial", "--piece", "2026/a", "--model", "m1")
        self.assertEqual((self.stubs / "codex.argv").read_text().splitlines()[:3], ["exec", "-m", "m1"])

    def test_an_unsubstituted_placeholder_or_short_prompt_refuses_before_codex(self):
        for body, message in (("echo '{GUARDRAILS_PATH}'; for n in $(seq 120); do printf 'word '; done\n",
                               r"still carries \{GUARDRAILS_PATH\}"),
                              ("echo too short\n", "returned 2 words")):
            with self.subTest(message=message):
                self.stub("adversarial-gate", body)
                with self.assertRaisesRegex(cli.WorkRefusal, message):
                    self.call("adversarial", "--piece", "2026/a")
                self.assertFalse((self.stubs / "codex.argv").exists())
        self.recorded.assert_not_called()

    def test_a_missing_gate_names_both_places_it_looked(self):
        with patch.dict(os.environ, {"ADVERSARIAL_GATE_BIN": str(self.stubs)}), \
                patch.object(cli.Path, "home", return_value=self.tmp):
            with self.assertRaisesRegex(cli.WorkRefusal, "ADVERSARIAL_GATE_BIN.*local-adversarial-gate"):
                self.call("adversarial", "--piece", "2026/a")
        self.brief.assert_called_once()
        self.recorded.assert_not_called()

    def test_a_failed_or_silent_codex_writes_no_report(self):
        for body, message in (("exit 3\n", "codex exited 3"), ("cat >/dev/null\n", "no final message")):
            with self.subTest(message=message):
                self.stub("codex", body)
                with self.assertRaisesRegex(cli.WorkRefusal, message):
                    self.call("adversarial", "--piece", "2026/a")
        self.recorded.assert_not_called()

    def test_a_prompt_and_codex_errors_go_as_utf_8_under_any_locale(self):
        """sd:5801: on `run_group` the prompt went in the locale's encoding, and a C locale refused it."""
        self.stub("codex", 'cat > "$STUBS/codex.stdin"\nprintf "r\\303\\251sum\\303\\251\\n" >&2\nexit 3\n')
        script = ("import sys\n"
                  f"sys.path.insert(0, {str(ROOT / 'bin')!r})\n"
                  "import sd_writing\n"
                  "try:\n"
                  "    sd_writing._codex('.', 'caf\\u00e9', model=None, timeout=30)\n"
                  "except sd_writing.WorkRefusal as refusal:\n"
                  "    print(refusal)\n")
        ran = subprocess.run([sys.executable, "-c", script], capture_output=True, cwd=self.tmp,
                             env={**os.environ, "PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C"})
        # Decoded as UTF-8, then escaped by an ASCII stderr: one `\xe9` per character, not two bytes.
        self.assertEqual(ran.stderr.decode().splitlines()[-1:], ["r\\xe9sum\\xe9"])
        self.assertEqual((ran.returncode, ran.stdout), (0, b"codex exited 3\n"))
        self.assertEqual((self.stubs / "codex.stdin").read_bytes(), "caf\u00e9".encode())

    def test_a_codex_past_its_timeout_is_stopped(self):
        self.stub("codex", "cat >/dev/null\nsleep 30\n")
        with self.assertRaisesRegex(cli.WorkRefusal, "--timeout 1"):
            self.call("adversarial", "--piece", "2026/a", "--timeout", "1")
        self.recorded.assert_not_called()

    def test_reconcile_reads_its_note_from_a_file(self):
        note = self.tmp / "note.md"
        note.write_text("Added `sd writing` claims; each sourced in research.md.\n")
        code, result = self.call("reconcile", "--piece", "2026/a", "--artifact", "research", "--note-file", str(note))
        self.assertEqual((code, result["artifact"]), (0, "research"))
        self.reconciled.assert_called_once_with(self.connection, 7, "research",
                                                note="Added `sd writing` claims; each sourced in research.md.")
        self.connect.assert_called_once_with(sd_db, write=False)

    def test_reconcile_takes_an_inline_note_and_one_note_only(self):
        self.call("reconcile", "--piece", "2026/a", "--artifact", "adversarial", "--note", "A1 rebutted.")
        self.reconciled.assert_called_once_with(self.connection, 7, "adversarial", note="A1 rebutted.")
        for argv in (["--note", "a", "--note-file", "b"], [], ["--artifact", "fact-check", "--note", "a"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.parser.parse_args(["writing", "reconcile", "--piece", "2026/a",
                                        *(["--artifact", "research"] if "--artifact" not in argv else []), *argv])

    def test_an_older_library_refuses_by_name(self):
        with patch.object(writing, "adversarial_brief", None), patch.object(writing, "reconcile_companion", None):
            for argv in (["adversarial", "--piece", "2026/a"],
                         ["reconcile", "--piece", "2026/a", "--artifact", "research", "--note", "n"]):
                with self.subTest(verb=argv[0]):
                    with self.assertRaisesRegex(cli.WorkRefusal, "install the current library build"):
                        self.call(*argv)
        self.connect.assert_not_called()

    def test_review_verbs_run_from_a_linked_worktree(self):
        self.assertNotIn("adversarial", cli.REGISTERED_ONLY)
        self.assertNotIn("reconcile", cli.REGISTERED_ONLY)


class WritingFromWorktree(unittest.TestCase):
    """A linked worktree keys rows to the main checkout and reads its own files (sd:2024)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.main = Path(tmp.name).resolve() / "main"
        self.main.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        for command in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "seed"],
                        ["worktree", "add", "-q", "-b", "gate", "../linked"]):
            subprocess.run(["git", *command], cwd=self.main, env=env, check=True, capture_output=True)
        self.linked = self.main.parent / "linked"
        self.parser = argparse.ArgumentParser()
        cli.register(self.parser.add_subparsers(required=True))
        self.connection = Mock()
        self.connect = Mock(return_value=self.connection)
        self.checkout = Mock(return_value=nullcontext())
        for patcher in (
            patch.object(cli.sd_handoff_rows, "library", return_value=sd_db),
            patch.object(cli.sd_handoff_rows, "connect", self.connect),
            patch.object(cli.sd_lib, "stored_repo", side_effect=str),
            patch.object(writing, "checkout", self.checkout, create=True),
            patch.object(writing, "piece_for_key", return_value={"id": 7}),
            patch.object(writing, "readiness", return_value={"ok": True}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_in(self, root, *argv):
        arguments = self.parser.parse_args(["writing", *argv, "--json"])
        with patch.object(cli.sd_lib, "repo_root", return_value=root), redirect_stdout(io.StringIO()):
            return arguments.handler(arguments)

    def test_worktree_reads_under_the_main_checkout_row(self):
        self.assertEqual(self.run_in(self.linked, "readiness", "--piece", "2026/a"), 0)
        writing.piece_for_key.assert_called_once_with(self.connection, str(self.main), "2026/a")
        self.checkout.assert_called_once_with(str(self.main), self.linked)

    def test_main_checkout_enters_no_override(self):
        self.assertEqual(self.run_in(self.main, "readiness", "--piece", "2026/a"), 0)
        writing.piece_for_key.assert_called_once_with(self.connection, str(self.main), "2026/a")
        self.checkout.assert_not_called()

    def test_publication_and_cutover_refuse_a_worktree(self):
        for argv in (["publication-render", "--piece", "2026/a"], ["import"], ["recover"],
                     ["register", "--piece", "2026/a"]):
            with self.subTest(argv=argv[0]):
                with self.assertRaisesRegex(cli.WorkRefusal, "only in the main checkout"):
                    self.run_in(self.linked, *argv)
        self.connect.assert_not_called()

    def test_promote_scaffolds_in_the_worktree(self):
        """Promote writes the new piece file, so a worktree's own copy takes it (sd:1994, sd:2024)."""
        (self.linked / "content").mkdir()
        with patch.object(writing, "promote", return_value={"ok": True}) as promoted:
            self.assertEqual(self.run_in(self.linked, "promote", "12"), 0)
        self.assertEqual(promoted.call_args.kwargs["repo"], str(self.main))
        self.checkout.assert_called_once_with(str(self.main), self.linked)

    def test_an_older_library_refuses_a_worktree_by_name(self):
        with patch.object(writing, "checkout", None, create=True):
            with self.assertRaisesRegex(cli.WorkRefusal, "update this pack checkout"):
                self.run_in(self.linked, "readiness", "--piece", "2026/a")


class WritingMirror(unittest.TestCase):
    """pack sd:7243: a verb that writes a piece queues its Drive mirror again, best effort.

    `enqueue_pieces` queues only the files whose text changed, so every write verb may call it. It never changes the
    verb's exit status or its JSON: the verb's write is committed, and the next verb or page write queues.
    """

    GATE = {"artifact": "fact-check", "verdict": "pass"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name).resolve()
        (self.repo / "content").mkdir()
        self.parser = argparse.ArgumentParser()
        cli.register(self.parser.add_subparsers(required=True))
        self.connection = Mock()
        with patch.object(sys, "path", [str(ROOT / "bin"), *sys.path]):
            self.publish = importlib.import_module("sd_research_publish")
        self.enqueue = Mock(return_value=["mirror: queued 2026-a-draft -> Briefs/repo"])
        for patcher in (
            patch.object(cli.sd_handoff_rows, "library", return_value=sd_db),
            patch.object(cli.sd_handoff_rows, "connect", Mock(return_value=self.connection)),
            patch.object(cli.sd_lib, "repo_root", return_value=self.repo),
            patch.object(cli.sd_lib, "stored_repo", side_effect=str),
            patch.object(cli.getpass, "getuser", return_value="operator"),
            patch.object(writing, "piece_for_key", return_value={"id": 7}),
            patch.object(writing, "record_gate", autospec=True, return_value=self.GATE),
            patch.object(writing, "piece_state", autospec=True, return_value={"item": {"id": 7}}),
            patch.object(writing, "promote", autospec=True, return_value={"item": {"piece": "2026/a"}}),
            patch.object(self.publish, "enqueue_pieces", self.enqueue),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, *argv):
        arguments = self.parser.parse_args(["writing", *argv, "--json"])
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = arguments.handler(arguments)
        return code, output.getvalue(), errors.getvalue()

    def gate(self):
        return self.call("gate", "--piece", "2026/a", "--artifact", "fact-check", "--verdict", "pass", "--reason", "ok")

    def test_a_write_verb_queues_the_piece_and_says_so_on_stderr(self):
        code, output, errors = self.gate()
        self.assertEqual((code, json.loads(output)), (0, self.GATE))
        self.enqueue.assert_called_once_with(self.connection, {7})
        self.assertIn("mirror: queued 2026-a-draft", errors)

    def test_a_publication_reconcile_queues_the_piece_it_published(self):
        # pack sd:7243: reconcile moves a piece to `published`, so its Drive copy moves to Writing/Published.
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("context.json", "evidence.json"):
            (folder / name).write_text("{}", encoding="utf-8")
        reconciled = {"phase": "published"}
        self.enterContext(patch.object(sd_db.publication, "reconcile", autospec=True, return_value=reconciled))
        code, output, _ = self.call("publication-reconcile", "--piece", "2026/a", "--claim", "c1",
                                    "--context-file", str(folder / "context.json"),
                                    "--evidence-file", str(folder / "evidence.json"))
        self.assertEqual((code, json.loads(output)), (0, reconciled))
        self.enqueue.assert_called_once_with(self.connection, {7})

    def test_promote_queues_the_item_it_names(self):
        self.assertEqual(self.call("promote", "12")[0], 0)
        self.enqueue.assert_called_once_with(self.connection, {12})

    def test_a_failed_enqueue_changes_neither_exit_nor_json(self):
        # Failure table, "Verb, then enqueue": the verb's write is committed; the mirror is named on stderr.
        expected = self.gate()[:2]
        for failure in (RuntimeError("queue unwritable"), OSError("disk full")):
            with self.subTest(failure=failure):
                self.enqueue.side_effect = failure
                code, output, errors = self.gate()
                self.assertEqual((code, output), expected)
                self.assertIn("mirror: piece not queued: %s" % failure, errors)

    def test_a_pack_without_the_producer_changes_neither_exit_nor_json(self):
        expected = self.gate()[:2]
        with patch.object(self.publish, "enqueue_pieces", None):
            code, output, errors = self.gate()
        self.assertEqual((code, output), expected)
        self.assertIn("mirror: piece not queued", errors)

    def test_a_read_verb_queues_nothing(self):
        self.assertEqual(self.call("get", "--piece", "2026/a")[0], 0)
        self.enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
