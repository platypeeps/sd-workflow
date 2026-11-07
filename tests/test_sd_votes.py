"""`sd votes add|edit|list|show|import` (pack sd:8050), run as `bin/sd` in a scratch home.

The library's rules are `tests/sd_db/test_votes.py`'s; this file holds the
command's shape: the checkout it reads from cwd or `--belongs-to`, `--json`
on the reads, `--if-revision`, and exit 1 with the refusal on stderr.
"""

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sd_db
from sd_db import initialise

from tests.clean_env import clean_environment

ROOT = Path(__file__).resolve().parents[1]


class Votes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        initialise(home=self.home)
        self.checkout = self.home / "repos" / "hoa"
        self.checkout.mkdir(parents=True)
        for args in (["init", "-q"], ["-c", "user.email=t@example.test", "-c", "user.name=T",
                                      "commit", "-q", "--allow-empty", "-m", "first"]):
            subprocess.run(["git", "-C", str(self.checkout), *args], check=True, capture_output=True,
                           env=clean_environment(HOME=str(self.home)))
        db = sd_db.connect(sd_db.default_path(self.home), write=True)
        self.addCleanup(db.close)
        sd_db.upsert_repo(db, "~/repos/hoa")
        self.db = db

    def sd(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(ROOT / "bin/sd"), "votes", *args], cwd=cwd or self.checkout,
                              env=clean_environment(HOME=str(self.home)),
                              capture_output=True, text=True, check=False, timeout=30)

    def ok(self, *args, cwd=None):
        done = self.sd(*args, cwd=cwd)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def test_add_then_show_and_list_read_it_back_as_json(self):
        added = self.ok("add", "CI-1", "--date", "2026-10-02", "--topic", "Crime policy",
                        "--motion", "Buy the crime policy.", "--votes", "Ada, Ben Yes; Cy No", "--outcome", "adopted",
                        "--json")
        vote = json.loads(added.stdout)
        self.assertEqual((vote["key"], vote["repo"], vote["revision"], vote["state"]), ("CI-1", "~/repos/hoa", 1, "closed"))
        self.assertEqual(vote["tally"], {"Ada": "yes", "Ben": "yes", "Cy": "no"})
        self.assertEqual(json.loads(self.ok("show", "CI-1", "--json").stdout), vote)
        self.ok("add", "PG-1", "--outcome", "pending: not sent")
        listed = json.loads(self.ok("list", "--json").stdout)
        self.assertEqual([v["key"] for v in listed["votes"]], ["PG-1", "CI-1"])
        self.assertEqual(listed["repo"], "~/repos/hoa")
        self.assertEqual([v["key"] for v in json.loads(self.ok("list", "--open", "--json").stdout)["votes"]], ["PG-1"])
        text = self.ok("show", "CI-1").stdout
        for part in ("CI-1", "Buy the crime policy.", "Ada yes, Ben yes, Cy no", "revision 1"):
            self.assertIn(part, text)
        self.assertIn("PG-1", self.ok("list").stdout.splitlines()[0])

    def test_edit_checks_the_revision_and_links_an_item(self):
        self.ok("add", "CI-1", "--outcome", "pending")
        item = sd_db.create_item(self.db, kind="task", title="send the binder", repo="~/repos/hoa")
        edited = json.loads(self.ok("edit", "CI-1", "--outcome", "adopted", "--item", f"sd:{item}",
                                    "--if-revision", "1", "--json").stdout)
        self.assertEqual((edited["revision"], edited["state"], edited["item"]), (2, "closed", item))
        stale = self.sd("edit", "CI-1", "--notes", "late", "--if-revision", "1")
        self.assertEqual(stale.returncode, 1)
        self.assertIn("revision 2, not 1", stale.stderr)
        cleared = json.loads(self.ok("edit", "CI-1", "--clear-item", "--state", "open", "--json").stdout)
        self.assertEqual((cleared["item"], cleared["state"], cleared["revision"]), (None, "open", 3))
        self.assertIn("unchanged", self.ok("edit", "CI-1", "--state", "open").stdout)

    def test_import_loads_a_votes_csv_once_and_a_second_run_changes_nothing(self):
        path = self.home / "votes.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            out = csv.writer(handle)
            out.writerow(["id", "date", "topic", "motion", "type", "moved", "seconded", "votes", "outcome",
                          "follow_up", "source", "notes"])
            out.writerow(["CI-1", "2026-10-02", "Crime policy", "Buy it, with a comma", "email vote", "Ada", "Ben",
                          "5-0: Ada, Ben, Cy, Dee, Eve", "adopted", "", "mail 1", "Ünïcode note"])
            out.writerow(["PG-1", "", "Hours", "Approve the hours.", "email vote (not sent)", "", "none yet", "none",
                          "pending: not sent", "", "", ""])
        first = json.loads(self.ok("import", str(path), "--json").stdout)
        self.assertEqual(first, {"repo": "~/repos/hoa", "added": 2, "updated": 0, "unchanged": 0})
        self.assertIn("0 added, 0 updated, 2 unchanged", self.ok("import", str(path)).stdout)
        vote = json.loads(self.ok("show", "CI-1", "--json").stdout)
        self.assertEqual((vote["motion"], vote["notes"], len(vote["tally"])), ("Buy it, with a comma", "Ünïcode note", 5))

    def test_belongs_to_names_the_checkout_from_anywhere(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        self.ok("add", "CI-1", "--belongs-to", str(self.checkout), cwd=elsewhere)
        self.assertEqual(json.loads(self.ok("show", "CI-1", "--json").stdout)["key"], "CI-1")
        refused = self.sd("list", cwd=elsewhere)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--belongs-to", refused.stderr)

    def test_refusals_exit_1_and_write_nothing(self):
        for args, word in ((("add", "a key"), "not a vote key"), (("add", "K-1", "--date", "Oct 2"), "YYYY-MM-DD"),
                           (("show", "NO-1"), "no vote NO-1"), (("edit", "NO-1", "--notes", "x"), "no vote NO-1"),
                           (("import", str(self.home / "absent.csv")), "absent.csv")):
            with self.subTest(args=args):
                done = self.sd(*args)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(word, done.stderr)
                self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(self.db.execute("SELECT count(*) FROM vote").fetchone()[0], 0)
        unregistered = self.home / "repos" / "loose"
        unregistered.mkdir()
        subprocess.run(["git", "-C", str(unregistered), "init", "-q"], check=True, capture_output=True,
                       env=clean_environment(HOME=str(self.home)))
        done = self.sd("list", cwd=unregistered)
        self.assertEqual(done.returncode, 1)
        self.assertIn("not a registered", done.stderr)


if __name__ == "__main__":
    unittest.main()
