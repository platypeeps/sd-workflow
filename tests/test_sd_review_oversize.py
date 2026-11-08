"""Input limits yield complete, nonexecuting split-branch advice."""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
from unittest import mock

from tests.clean_env import clean_environment
from tests.test_sd_review import (
    FakeClient,
    FakeRunner,
    ReviewFixture,
    codex_sessions,
    namespace,
    sd_review,
)
from tests.test_sd_review_readiness import read_only_connections


class OversizeTests(ReviewFixture):
    reviewing_depth = 1  # fallthrough past an oversized reader, one per tier

    def test_gate_mutation_cannot_dispatch_an_unmeasured_subject(self):
        for target in ("introduced.py", "change.py"):
            with self.subTest(target=target):
                root = self.make_repo(target)
                (root / "change.py").write_text("small change")

                def change_during_gate(*args, root=root, target=target):
                    (root / target).write_text("x" * sd_review.MAX_OUTPUT_BYTES)
                    return sd_review.Completed(0, "{}", "")

                runner = FakeRunner({"sd-check": change_during_gate})
                result = sd_review.review(root, namespace(), runner, self.environment(), self.chatgpt_home())
                # sd:2605: the reviewers run before the gate, so a change made during the gate
                # lands after dispatch; it still refuses the result.
                self.assertTrue(runner.calls[-1]["argv"][1].endswith("sd-check"), runner.calls[-1]["argv"])
                self.assertEqual(result["status"], "refused")
                self.assertEqual(result["input_manifest"]["status"], "within_limit")
                self.assertIn("input_changed", [row["code"] for row in result["readiness"]["blockers"]])
                self.assertIn(target, [row["path"] for row in result["input_manifest"]["paths"]])

    def test_same_size_material_and_prompt_changes_also_stop_dispatch(self):
        for change in ("material", "prompt"):
            with self.subTest(change=change):
                root = self.make_repo(change)
                (root / "change.py").write_text("before")

                def change_during_gate(*args, root=root, change=change):
                    if change == "material":
                        (root / "change.py").write_text("after!")
                    else:
                        self.local_block(root, "convention: changed")
                    return sd_review.Completed(0, "{}", "")

                runner = FakeRunner({"sd-check": change_during_gate})
                result = sd_review.review(root, namespace(), runner, self.environment(), self.chatgpt_home())
                self.assertTrue(runner.calls[-1]["argv"][1].endswith("sd-check"), runner.calls[-1]["argv"])
                self.assertEqual(result["status"], "refused")
                self.assertIn("input_changed", [row["code"] for row in result["readiness"]["blockers"]])

    def test_unchanged_gate_inputs_still_dispatch_normally(self):
        root = self.make_repo()
        (root / "change.py").write_text("small change")
        runner = FakeRunner()
        result = sd_review.review(root, namespace(), runner, self.environment(), self.chatgpt_home())
        # The gate, the skill probe, its `--version` read (sd:3499), the review.
        # The probe is a codex call and is counted here so a fifth call cannot arrive unnoticed.
        self.assertEqual(len(runner.calls), 4, [call["argv"][:3] for call in runner.calls])
        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["input_manifest"]["status"], "within_limit")

    def test_oversize_is_advisory_without_gate_provider_or_meter(self):
        root = self.make_repo()
        registry = self.registry_home / ".local/share/sd/providers.yaml"
        registry.write_text(registry.read_text().replace("reader: codex-json", "reader: claude-json"))
        (root / "large.py").write_text("x" * sd_review.MAX_OUTPUT_BYTES)
        for explain in (False, True):
            runner, client = FakeRunner(), FakeClient()
            with read_only_connections():
                result = sd_review.review(root, namespace(explain=explain), runner, self.environment(),
                                          self.chatgpt_home(), client=client, meter=lambda *args: self.fail("meter"))
            inventory = result["input_manifest"]
            self.assertEqual(inventory["status"], "oversized")
            self.assertEqual(inventory["limit_bytes"], 2_000_000)
            self.assertEqual(inventory["next_action"], "split_input_for_oversized_providers")
            self.assertFalse(inventory["review_complete"])
            self.assertFalse(inventory["advisory_only"])
            self.assertTrue(inventory["split_plan_advisory_only"])
            self.assertEqual(inventory["oversized_paths"], ["large.py"])
            self.assertEqual(runner.calls, [])
            self.assertEqual(client.sent, [])
            self.assertEqual(result["completed_reviews"], 0)

    def test_native_codex_counts_only_actual_prompt_with_oversized_fallback(self):
        root = self.make_repo()
        (root / "large.py").write_text("x" * (sd_review.MAX_OUTPUT_BYTES + 100))
        registry = self.registry_home / ".local/share/sd/providers.yaml"
        registry.write_text(registry.read_text().replace("roles: [author, reviewer], reader: codex-json",
                                                       "roles: [author, reviewer], reader: claude-json"))
        for provider in ("codex", None):
            for failed in (False, True):
                runner = FakeRunner({"codex": sd_review.Completed(1, "", "synthetic failure")} if failed else {})
                with self.subTest(provider=provider, failed=failed):
                    result = sd_review.review(root, namespace(provider=provider), runner, self.environment(), self.chatgpt_home())
                    calls = codex_sessions(runner)
                    self.assertEqual(len(calls), 1)
                    # The gate runs only after a review that cleared (sd:2605); the
                    # skill probe and its `--version` read (sd:3499) run first.
                    self.assertEqual(len(runner.calls), 3 if failed else 4, "oversized fallback must never dispatch")
                    self.assertEqual(result["readiness"]["status"], "ready")
                    self.assertEqual(result["input_manifest"]["transport_bytes"]["codex"], len(calls[0]["stdin"].encode()))
                    self.assertLess(len(calls[0]["stdin"].encode()), sd_review.MAX_OUTPUT_BYTES)
                    self.assertEqual(result["status"], ("unavailable" if provider else "refused") if failed else "clean")
                    if provider is None:
                        self.assertEqual([row["provider"] for row in result["readiness"]["warnings"]], ["second"])

    def test_each_dispatch_enforces_complete_transmitted_prompt(self):
        root = self.make_repo()
        subject = sd_review.resolve_subject(root, "worktree")
        for transport in ({"reader": "codex-json", "start": "codex exec"},
                          {"reader": "claude-json", "start": "claude"},
                          {"url": "https://api.example.invalid/v1", "model": "fixture"}):
            runner, client = FakeRunner(), FakeClient()
            provider = sd_review.sd_registry.Provider(name="fixture", vendor="fixture", bill="fixture", **transport)
            with self.subTest(transport=transport):
                outcome = sd_review.run_provider(provider, root, subject, "x" * (sd_review.MAX_OUTPUT_BYTES + 1),
                    runner, self.environment(), 10, self.chatgpt_home(), client)
                self.assertEqual(outcome.status, sd_review.REFUSED)
                self.assertEqual(runner.calls, [])
                self.assertEqual(client.sent, [])

    def test_attached_transports_measure_and_bound_the_complete_payload(self):
        root = self.make_repo()
        (root / "src.py").write_text("attached material " * 100)
        subject = sd_review.resolve_subject(root, "worktree")
        material, inventory = sd_review.sd_review_material.collect_review_material(root, subject)
        prompt = "fixture prompt " * 100
        for remote in (False, True):
            transport = {"url": "https://api.example.invalid/v1"} if remote else {"reader": "claude-json", "start": "claude"}
            provider = sd_review.sd_registry.Provider(name="fixture", vendor="fixture", bill="fixture", **transport)
            captured = []

            def reply(argv, env, cwd, timeout, captured=captured):
                captured.append((sd_review.pathlib.Path(argv[argv.index("--add-dir") + 1]) / "review-subject.md").read_text())
                return sd_review.Completed(0, json.dumps({"type": "result", "subtype": "success", "structured_output": {"findings": []}}), "")

            client = FakeClient()
            with self.subTest(remote=remote):
                outcome = sd_review.run_provider(provider, root, subject, prompt, reply, self.environment(), 10, client=client)
                self.assertEqual(outcome.status, sd_review.CLEAN)
                sent = client.sent[0]["prompt"] if remote else captured[0]
                overhead = (f"\nSchema: {json.dumps(sd_review.CODEX_OUTPUT_SCHEMA)}\n\nReview input:\n{sd_review.URL_OUTPUT_CONTRACT}"
                            if remote else sd_review.sd_lib.READER_PROMPTS["claude-json"] + "\n\nReview input:\n")  # sd:7519
                manifest = sd_review.sd_review_material.input_manifest(inventory, prompt, {"fixture": overhead}, sd_review.MAX_OUTPUT_BYTES)
                self.assertEqual(manifest["transport_bytes"]["fixture"], len(sent.encode()))
                limit = len(sent.encode()) - 1
                self.assertLess(len(prompt.encode()), limit)
                self.assertLess(len(material.encode()), limit)
                runner, client = FakeRunner(), FakeClient()
                with mock.patch.object(sd_review, "MAX_OUTPUT_BYTES", limit):
                    refused = sd_review.run_provider(provider, root, subject, prompt, runner, self.environment(), 10, client=client)
                self.assertEqual(refused.status, sd_review.REFUSED)
                self.assertEqual(runner.calls, [])
                self.assertEqual(client.sent, [])

    def test_inventory_preserves_rename_binary_symlink_and_unusual_paths(self):
        root = self.make_repo()
        subprocess.run(["git", "mv", "README.md", "renamed.md"], cwd=root, check=True, capture_output=True)
        names = [" leading.py", "tabs\tand\nlines.py", "binary.bin", "link"]
        for name in names[:2]:
            (root / name).write_text("some text\n")
        (root / "binary.bin").write_bytes(b"\xff\x00\x01")
        os.symlink("../outside-secret", root / "link")
        subject = sd_review.resolve_subject(root, "worktree")
        material, entries = sd_review.sd_review_material.collect_review_material(root, subject)
        expected = {"README.md", "renamed.md", *names}
        self.assertEqual(set(subject.paths), expected)
        self.assertEqual({entry["path"] for entry in entries}, expected)
        self.assertEqual(sum(entry["bytes"] for entry in entries), len(material.encode()))
        self.assertIn("[binary, base64]", material)
        self.assertIn("symlink -> ../outside-secret", material)
        plan = sd_review.sd_review_material.input_manifest(entries, "prompt", {"test": "overhead"}, 200)
        grouped = [path for group in plan["suggested_groups"] for path in group["paths"]]
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(set(grouped), expected)
        self.assertEqual(plan["measured_bytes"], len(("prompt" + material + "overhead").encode()))

    def test_folder_rename_is_sent_as_rename_records_with_both_paths(self):
        """sd:2400: a moved file costs a rename record, not a full delete and a full add."""
        for scope in ("worktree", "branch"):
            with self.subTest(scope=scope):
                root = self.make_repo(scope)
                (root / "old").mkdir()
                moved = [f"file-{index}.html" for index in range(40)]
                for index, name in enumerate(moved):
                    (root / "old" / name).write_text(f"<p>page {index}</p>\n" * 5000)
                subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "commit", "--quiet", "-m", "pages"], cwd=root, check=True, capture_output=True)
                if scope == "branch":
                    subprocess.run(["git", "checkout", "--quiet", "-b", "move"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "mv", "old", "new"], cwd=root, check=True, capture_output=True)
                (root / "new" / "file-0.html").write_text("<p>edited</p>\n" + "<p>page 0</p>\n" * 4999)
                if scope == "branch":
                    subprocess.run(["git", "commit", "--quiet", "-am", "move"], cwd=root, check=True, capture_output=True)
                subject = sd_review.resolve_subject(root, scope)
                material, entries = sd_review.sd_review_material.collect_review_material(root, subject)
                expected = {f"{side}/{name}" for side in ("old", "new") for name in moved}
                self.assertEqual(set(subject.paths), expected)
                self.assertEqual({entry["path"] for entry in entries}, expected)
                self.assertEqual(sum(entry["bytes"] for entry in entries), len(material.encode()))
                moved_bytes = sum(len(f"<p>page {index}</p>\n".encode()) * 5000 for index in range(40))
                self.assertLess(len(material.encode()), moved_bytes // 20)
                for name in moved:
                    self.assertIn(f"rename from old/{name}\n", material)
                    self.assertIn(f"rename to new/{name}\n", material)
                    self.assertIn(f'[renamed] "old/{name}" -> "new/{name}"', material)
                self.assertIn("+<p>edited</p>\n", material)
                # The unchanged lines are not sent, so both sides are summarized and coverage is partial (sd:2181).
                self.assertEqual({entry["path"] for entry in entries if entry.get("summarized")}, expected)
                self.assertEqual(set(sd_review.sd_review_material.coverage(entries, {"r": "x"})["omitted_paths"]), expected)

    def test_a_rename_that_fits_is_sent_whole_and_counts_as_read(self):
        """sd:7598: a renamed, edited file within the limit goes with every line, so no reader is partial."""
        for scope in ("worktree", "branch"):
            with self.subTest(scope=scope):
                root = self.make_repo(scope)

                def git(*args: str, root=root) -> None:
                    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=clean_environment())

                (root / "up_wall.rs").write_text("".join(f"fn step_{index}() {{}}\n" for index in range(200)))
                git("add", "-A")
                git("commit", "--quiet", "-m", "wall")
                if scope == "branch":
                    git("checkout", "--quiet", "-b", "move")
                git("mv", "up_wall.rs", "up_no_accounts.rs")
                with (root / "up_no_accounts.rs").open("a") as stream:
                    stream.write("fn edited() {}\n")
                if scope == "branch":
                    git("commit", "--quiet", "-am", "move")
                material, entries = sd_review.sd_review_material.collect_review_material(root, sd_review.resolve_subject(root, scope))
                self.assertIn("rename to up_no_accounts.rs\n", material)
                self.assertIn("+fn step_3() {}\n", material)  # an unchanged line, sent with the new file
                self.assertEqual(sd_review.sd_review_material.coverage(entries, {"r": "x"})["omitted_paths"], [])

    def test_a_renamed_and_modified_binary_is_sent_as_its_rename_delta(self):
        """sd:2432: the `--binary` fallback detects the rename, so a moved, edited blob is not sent as full base64."""
        data = b"\x00\x01" + random.Random(2432).randbytes(200_000)  # incompressible, so a literal is large
        for scope in ("worktree", "branch"):
            with self.subTest(scope=scope):
                root = self.make_repo(scope)
                (root / "old.dat").write_bytes(data)
                subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "commit", "--quiet", "-m", "blob"], cwd=root, check=True, capture_output=True)
                if scope == "branch":
                    subprocess.run(["git", "checkout", "--quiet", "-b", "move"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "mv", "old.dat", "new.dat"], cwd=root, check=True, capture_output=True)
                (root / "new.dat").write_bytes(data[:1000] + b"edited" + data[1006:])
                if scope == "branch":
                    subprocess.run(["git", "commit", "--quiet", "-am", "move"], cwd=root, check=True, capture_output=True)
                material, entries = sd_review.sd_review_material.collect_review_material(
                    root, sd_review.resolve_subject(root, scope))
                self.assertEqual({entry["path"] for entry in entries}, {"old.dat", "new.dat"})
                self.assertEqual(sum(entry["bytes"] for entry in entries), len(material.encode()))
                self.assertLess(len(material.encode()), len(data) // 10)
                piece = material[material.index("diff --git a/old.dat b/new.dat"):]
                self.assertIn("rename from old.dat\n", piece)
                self.assertIn("GIT binary patch\ndelta ", piece)
                # A delta leaves the unchanged bytes unsent, so both sides are summarized, as a text rename is.
                self.assertEqual({entry["path"] for entry in entries if entry.get("summarized")}, {"old.dat", "new.dat"})

    def test_rename_record_quotes_an_unusual_old_path(self):
        """sd:2400: the old path's header is quoted as git quotes it, so a name cannot forge a file boundary."""
        root = self.make_repo()
        old = "x\ndiff --git forged forged"
        (root / old).write_text("<p>page</p>\n" * 50)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "page"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "mv", old, "moved.html"], cwd=root, check=True, capture_output=True)
        material, entries = sd_review.sd_review_material.collect_review_material(root, sd_review.resolve_subject(root, "worktree"))
        self.assertEqual({entry["path"] for entry in entries}, {old, "moved.html"})
        self.assertNotIn("\ndiff --git forged", material)
        self.assertIn('diff --git "a/x\\ndiff --git forged forged" "b/x\\ndiff --git forged forged"\n', material)

    def test_a_marker_line_in_raw_content_is_not_a_summary(self):
        """sd:2400: only a marker the material writes summarizes a path; file text that looks like one does not."""
        root = self.make_repo()
        (root / "notes.md").write_text('# Notes\n[renamed] "a" -> "b"; unchanged lines not sent\n[binary, not sent] x\n')
        (root / "z.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
        material, entries = sd_review.sd_review_material.collect_review_material(root, sd_review.resolve_subject(root, "worktree"))
        self.assertIn('[renamed] "a" -> "b"', material)
        self.assertEqual([entry["path"] for entry in entries if entry.get("summarized")], ["z.png"])

    def test_prompt_overhead_is_counted_even_when_material_fits(self):
        root = self.make_repo()
        (root / "small.py").write_text("x = 1")
        with mock.patch.object(sd_review, "MAX_OUTPUT_BYTES", 1000):
            result = sd_review.review(root, namespace(explain=True), FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertLess(result["input_manifest"]["material_bytes"], 1000)
        self.assertGreater(result["input_manifest"]["measured_bytes"], 1000)
        self.assertEqual(result["readiness"]["status"], "blocked")

    def test_material_collection_uses_constant_git_calls(self):
        root = self.make_repo()
        for index in range(20):
            (root / f"file-{index}.py").write_text("initial\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "tracked files"], cwd=root, check=True, capture_output=True)
        for index in range(20):
            (root / f"file-{index}.py").write_text("changed\n")
        subject = sd_review.resolve_subject(root, "worktree")
        helper = sd_review.sd_review_material
        with mock.patch.object(helper, "read_git_material", wraps=helper.read_git_material) as git_calls:
            material, inventory = helper.collect_review_material(root, subject)
        self.assertEqual(git_calls.call_count, 3)
        self.assertEqual(len(inventory), 20)
        self.assertEqual(sum(row["bytes"] for row in inventory), len(material.encode()))


class BinaryMaterialTests(ReviewFixture):
    """sd:2181: a binary path contributes a short summary, never its bytes."""
    reviewing_depth = 1  # one reader per tier

    SHOT = b"\x89PNG\r\n\x1a\n" + os.urandom(2_100_000)

    def git(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout

    def screenshot_branch(self):
        root = self.make_repo()
        (root / "src.py").write_text("".join(f"line = {index}\n" for index in range(200)))
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "text")
        base = self.git(root, "rev-parse", "HEAD").strip()
        self.git(root, "checkout", "--quiet", "-b", "retake")
        (root / "shot.png").write_bytes(self.SHOT)
        (root / "src.py").write_text("".join(f"value = {index}\n" for index in range(200)))
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "retake")
        return root, base

    def test_branch_adding_a_large_png_fits_the_review_limit(self):
        root, base = self.screenshot_branch()
        subject = sd_review.resolve_subject(root, "branch", base=base)
        material, inventory = sd_review.sd_review_material.collect_review_material(root, subject)
        manifest = sd_review.sd_review_material.input_manifest(inventory, "prompt", {"fixture": "overhead"},
                                                               sd_review.MAX_OUTPUT_BYTES)
        self.assertEqual(manifest["status"], "within_limit", manifest["measured_bytes"])
        self.assertEqual([row["path"] for row in inventory], ["shot.png", "src.py"])
        self.assertEqual(sum(row["bytes"] for row in inventory), len(material.encode()))
        blob = self.git(root, "rev-parse", "HEAD:shot.png").strip()
        shot = material[material.index('diff --git a/shot.png'):material.index('diff --git a/src.py')]
        self.assertNotIn("GIT binary patch", shot)
        self.assertIn("[binary, not sent] added; old absent; new 2100008 bytes, blob " + blob, shot)
        self.assertLess(len(shot.encode()), 400)

    def test_text_change_beside_a_binary_is_sent_in_full(self):
        root, base = self.screenshot_branch()
        subject = sd_review.resolve_subject(root, "branch", base=base)
        material, _inventory = sd_review.sd_review_material.collect_review_material(root, subject)
        expected = self.git(root, "diff", "--no-color", base, "HEAD", "--", "src.py")
        self.assertTrue(material.endswith(expected))
        self.assertEqual(expected.count("\n+value = "), 200)

    def test_worktree_binaries_report_old_blob_and_new_content_hash(self):
        root, _base = self.screenshot_branch()
        changed = b"\x89PNG\r\n\x1a\n\x00changed"
        old_blob = self.git(root, "rev-parse", "HEAD:shot.png").strip()
        (root / "shot.png").write_bytes(changed)
        (root / "new.png").write_bytes(self.SHOT)
        subject = sd_review.resolve_subject(root, "worktree")
        material, inventory = sd_review.sd_review_material.collect_review_material(root, subject)
        self.assertEqual({row["path"] for row in inventory}, {"shot.png", "new.png"})
        self.assertEqual(sum(row["bytes"] for row in inventory), len(material.encode()))
        self.assertIn(f"[binary, not sent] modified; old 2100008 bytes, blob {old_blob}; "
                      f"new {len(changed)} bytes, sha256 {hashlib.sha256(changed).hexdigest()}", material)
        self.assertIn(f"[binary, not sent] added (untracked); 2100008 bytes, sha256 {hashlib.sha256(self.SHOT).hexdigest()}",
                      material)
        self.assertLess(len(material.encode()), 1000)

    def test_git_binary_text_is_sent_readable_and_unknown_bytes_are_not_summarized(self):
        # sd:2181 review: git also calls UTF-16 and `-diff` files binary; only media is summarized.
        root, base = self.screenshot_branch()
        self.git(root, "checkout", "--quiet", "-b", "configs")
        (root / ".gitattributes").write_text("notes.txt -diff\n")
        (root / "notes.txt").write_text("readable note behind -diff\n")
        (root / "config.ini").write_bytes("[core]\nsetting = utf16-value\n".encode("utf-16"))
        (root / "blob.dat").write_bytes(b"\x00\x01" + os.urandom(300))
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "configs")
        subject = sd_review.resolve_subject(root, "branch", base=base)
        material, inventory = sd_review.sd_review_material.collect_review_material(root, subject)
        self.assertEqual(sum(row["bytes"] for row in inventory), len(material.encode()))
        pieces = {name: material[material.index(f"diff --git a/{name}"):] for name in ("notes.txt", "config.ini", "blob.dat")}
        for name, expected in (("notes.txt", "+readable note behind -diff\n"), ("config.ini", "+setting = utf16-value\n"),
                               ("blob.dat", "GIT binary patch")):
            with self.subTest(name=name):
                self.assertIn(expected, pieces[name][:400])
        self.assertNotIn("[binary, not sent]", pieces["blob.dat"].split("diff --git a/config.ini")[0])
        self.assertIn("[binary, not sent] added; old absent; new 2100008 bytes, blob", material)

    def test_whole_file_material_follows_the_same_rule(self):
        root = self.make_repo()
        (root / "wide.ini").write_bytes("setting = utf16-untracked\n".encode("utf-16"))
        (root / "raw.dat").write_bytes(b"\x00\xff" + os.urandom(64))
        (root / "new.png").write_bytes(self.SHOT)
        subject = sd_review.resolve_subject(root, "worktree")
        material, _inventory = sd_review.sd_review_material.collect_review_material(root, subject)
        self.assertIn("setting = utf16-untracked", material)
        self.assertIn('--- "raw.dat" ---\n[binary, base64]\n', material)
        self.assertIn("[binary, not sent] added (untracked); 2100008 bytes", material)

    def test_a_text_file_that_starts_with_the_media_marker_is_not_summarized(self):
        # sd:2431: summarized is decided from the bytes, not from the rendered text.
        root = self.make_repo()
        (root / "notes.txt").write_text("[binary, not sent] a note that only looks like a summary\n")
        (root / "new.png").write_bytes(self.SHOT)
        for scope in ("worktree", "planning"):
            with self.subTest(scope=scope):
                subject = (sd_review.resolve_subject(root, "worktree") if scope == "worktree"
                           else mock.Mock(scope="planning", paths=["notes.txt", "new.png"]))
                _material, inventory = sd_review.sd_review_material.collect_review_material(root, subject)
                summarized = {row["path"]: row.get("summarized", False) for row in inventory}
                self.assertEqual((summarized["notes.txt"], summarized["new.png"]), (False, True), inventory)

    def test_ico_magic_is_not_a_media_pass(self):
        # sd:2181 review: 00 00 01 00 is too weak a signature to let a file escape review.
        root, base = self.screenshot_branch()
        disguised = b"\x00\x00\x01\x00" + os.urandom(64)
        (root / "favicon.ico").write_bytes(disguised)
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "icon")
        (root / "loose.ico").write_bytes(disguised)
        subject = sd_review.resolve_subject(root, "branch", base=base)
        material, _inventory = sd_review.sd_review_material.collect_review_material(root, subject)
        piece = material[material.index("diff --git a/favicon.ico"):material.index("diff --git a/shot.png")]
        self.assertIn("GIT binary patch", piece)
        self.assertNotIn("[binary, not sent]", piece)
        self.assertIn('--- "loose.ico" ---\n[binary, base64]\n',
                      sd_review.sd_review_material.file_material(root, "loose.ico", "added (untracked)")[0])

    def test_a_summarized_path_leaves_a_material_only_review_partial(self):
        # sd:2181 review pass 2: a hash line is not a review. A `-diff` script that starts
        # like a GIF is summarized, so a reviewer that reads only material has partial coverage.
        root = self.make_repo()
        (root / ".gitattributes").write_text("build.sh -diff\n")
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "attributes")
        (root / "build.sh").write_text("GIF89a\nrm -rf \"$HOME\"\n")
        registry = self.registry_home / ".local/share/sd/providers.yaml"
        registry.write_text(registry.read_text().replace("reader: codex-json", "reader: claude-json"))
        answer = sd_review.Completed(0, json.dumps({"type": "result", "subtype": "success",
                                                   "structured_output": {"findings": []}}), "")
        result = sd_review.review(root, namespace(), FakeRunner({"codex": answer, "second": answer}),
                                  self.environment(), self.chatgpt_home())
        self.assertEqual(result["completed_reviews"], 0, [(row["backend"], row["status"]) for row in result["outcomes"]])
        self.assertEqual(result["input_manifest"]["omitted_paths"], ["build.sh"])
        self.assertNotEqual(result["status"], "clean")
        partial = [row for row in result["outcomes"] if (row["diagnostic"] or {}).get("coverage") == "partial"]
        self.assertTrue(partial, result["outcomes"])
        self.assertTrue(all(row["diagnostic"]["omitted_paths"] == ["build.sh"] and "build.sh" in row["detail"]
                            for row in partial))

    def test_an_encoding_change_is_visible(self):
        root = self.make_repo()
        (root / "same.txt").write_text("unchanged words\n")
        (root / "moved.txt").write_text("old words\n")
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "utf-8")
        (root / "same.txt").write_bytes("unchanged words\n".encode("utf-16"))
        (root / "moved.txt").write_bytes("new words\n".encode("utf-16"))
        subject = sd_review.resolve_subject(root, "worktree")
        material, _inventory = sd_review.sd_review_material.collect_review_material(root, subject)
        same = material[material.index("diff --git a/same.txt"):]
        moved = material[material.index("diff --git a/moved.txt"):material.index("diff --git a/same.txt")]
        self.assertIn("GIT binary patch", same)
        self.assertIn("[encoding] old utf-8; new utf-16-le, BOM\n", moved)
        self.assertIn("+new words\n", moved)

    def test_a_native_reader_covers_a_material_readers_screenshot(self):
        # sd:8060: a lane prepare stopped at 1/2 on every branch with a PNG. codex
        # read the repository; claude saw the PNG only as a hash and was voided.
        self.one_reader_per_tier(2)
        root = self.make_repo()
        (root / "shot.png").write_bytes(self.SHOT)
        registry = self.registry_home / ".local/share/sd/providers.yaml"
        registry.write_text(registry.read_text().replace("roles: [author, reviewer], reader: codex-json",
                                                         "roles: [author, reviewer], reader: claude-json"))
        answer = sd_review.Completed(0, json.dumps({"type": "result", "subtype": "success",
                                                   "structured_output": {"findings": []}}), "")
        result = sd_review.review(root, namespace(), FakeRunner({"second": answer}),
                                  self.environment(), self.chatgpt_home())
        outcomes = {row["backend"]: row for row in result["outcomes"]}
        self.assertEqual((result["status"], result["completed_reviews"]), ("clean", 2), outcomes)
        self.assertEqual(outcomes["second"]["diagnostic"]["omitted_paths"], ["shot.png"])
        self.assertEqual(outcomes["second"]["status"], "clean")
        self.assertIn("coverage is partial", outcomes["second"]["detail"])

    def test_a_native_reader_completes_a_screenshot_review(self):
        root = self.make_repo()
        (root / "shot.png").write_bytes(self.SHOT)
        result = sd_review.review(root, namespace(), FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertEqual(result["input_manifest"]["omitted_paths"], ["shot.png"])
        self.assertEqual(result["input_manifest"]["partial_providers"], [])
        self.assertEqual((result["status"], result["completed_reviews"]), ("clean", 1))


class DeletedMaterialTests(ReviewFixture):
    """sd:6088: a deleted file shows as its header and name, never its text."""

    git = BinaryMaterialTests.git

    def deleting_branch(self):
        root = self.make_repo()
        (root / "big").mkdir()
        (root / "big" / "notes.txt").write_text("".join(f"row {index:07d}\n" for index in range(300_000)))
        (root / "big" / "blob.bin").write_bytes(b"\x00\x01" * 400_000)
        (root / "keep.py").write_text("value = 1\n")
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "big")
        base = self.git(root, "rev-parse", "HEAD").strip()
        self.git(root, "checkout", "--quiet", "-b", "prune")
        self.git(root, "rm", "--quiet", "-r", "big")
        (root / "keep.py").write_text("value = 2\n")
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "prune")
        return root, base

    def test_branch_deleting_large_files_fits_the_review_limit(self):
        root, base = self.deleting_branch()
        subject = sd_review.resolve_subject(root, "branch", base=base)
        material, inventory = sd_review.sd_review_material.collect_review_material(root, subject)
        manifest = sd_review.sd_review_material.input_manifest(inventory, "prompt", {"fixture": "overhead"},
                                                               sd_review.MAX_OUTPUT_BYTES)
        self.assertEqual(manifest["status"], "within_limit", manifest["measured_bytes"])
        self.assertEqual([row["path"] for row in inventory], ["big/blob.bin", "big/notes.txt", "keep.py"])
        self.assertIn("diff --git a/big/notes.txt b/big/notes.txt", material)
        self.assertIn("deleted file mode", material)
        self.assertNotIn("row 0000001", material)
        self.assertIn("+value = 2", material)
        self.assertEqual(sum(row["bytes"] for row in inventory), len(material.encode()))

    def test_deleted_paths_are_summarized_so_material_readers_report_partial(self):
        root, base = self.deleting_branch()
        subject = sd_review.resolve_subject(root, "branch", base=base)
        _material, inventory = sd_review.sd_review_material.collect_review_material(root, subject)
        manifest = sd_review.sd_review_material.input_manifest(inventory, "prompt", {"fixture": "overhead"},
                                                               sd_review.MAX_OUTPUT_BYTES)
        self.assertEqual(manifest["omitted_paths"], ["big/blob.bin", "big/notes.txt"])
        self.assertEqual(manifest["partial_providers"], ["fixture"])
        native = sd_review.sd_review_material.input_manifest(inventory, "prompt", {"codex": None},
                                                             sd_review.MAX_OUTPUT_BYTES)
        self.assertEqual(native["partial_providers"], [])
        self.assertEqual(native["status"], "within_limit")

    def test_deletion_within_the_limit_is_sent_whole_and_not_omitted(self):
        root = self.make_repo()
        (root / "old.py").write_text("gone = 1\n")
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "old")
        base = self.git(root, "rev-parse", "HEAD").strip()
        self.git(root, "rm", "--quiet", "old.py")
        self.git(root, "commit", "--quiet", "-m", "drop")
        subject = sd_review.resolve_subject(root, "branch", base=base)
        material, inventory = sd_review.sd_review_material.collect_review_material(root, subject)
        self.assertIn("-gone = 1", material)
        manifest = sd_review.sd_review_material.input_manifest(inventory, "prompt", {"fixture": "overhead"},
                                                               sd_review.MAX_OUTPUT_BYTES)
        self.assertEqual(manifest["omitted_paths"], [])

    def test_material_limit_default_matches_the_review_limit(self):
        default = sd_review.sd_review_material.collect_review_material.__defaults__
        self.assertEqual(default[0], sd_review.MAX_OUTPUT_BYTES)

    def test_fallbacks_fit_prompt_material_and_overhead_not_material_alone(self):
        """sd:7653: material that fits alone but not with the prompt goes as its rename delta or deletion header."""
        body = "".join(f"line {index:07d} of the fixture\n" for index in range(60_000))  # ~1.6 MB
        prompt = "fixture prompt " * 30_000  # ~450 kB: whole material plus this passes the limit
        for change in ("rename", "delete"):
            with self.subTest(change=change):
                root = self.make_repo(change)
                (root / "big.txt").write_text(body)
                subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True, env=clean_environment())
                subprocess.run(["git", "commit", "--quiet", "-m", "big"], cwd=root, check=True, capture_output=True, env=clean_environment())
                if change == "rename":
                    subprocess.run(["git", "mv", "big.txt", "moved.txt"], cwd=root, check=True, capture_output=True, env=clean_environment())
                    with (root / "moved.txt").open("a") as stream:
                        stream.write("edited\n")
                else:
                    (root / "big.txt").unlink()
                subject = sd_review.resolve_subject(root, "worktree")
                whole, _entries = sd_review.sd_review_material.collect_review_material(root, subject)
                self.assertLess(len(whole.encode()), sd_review.MAX_OUTPUT_BYTES)
                self.assertGreater(len((prompt + whole).encode()), sd_review.MAX_OUTPUT_BYTES)
                provider = sd_review.sd_registry.Provider(name="fixture", vendor="fixture", bill="fixture",
                                                          reader="claude-json", start="claude")
                captured = []

                def reply(argv, env, cwd, timeout, captured=captured):
                    captured.append((sd_review.pathlib.Path(argv[argv.index("--add-dir") + 1]) / "review-subject.md").read_text())
                    return sd_review.Completed(0, json.dumps({"type": "result", "subtype": "success",
                                                              "structured_output": {"findings": []}}), "")

                outcome = sd_review.run_provider(provider, root, subject, prompt, reply, self.environment(), 10)
                self.assertEqual(outcome.status, sd_review.CLEAN, outcome)
                self.assertLessEqual(len(captured[0].encode()), sd_review.MAX_OUTPUT_BYTES)
                self.assertIn("unchanged lines not sent" if change == "rename" else "deleted file mode ", captured[0])
                self.assertNotIn("line 0001234 of the fixture", captured[0])

