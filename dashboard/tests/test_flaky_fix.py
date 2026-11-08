"""Today > Needs you: Fix on a flaky head and Fix all on the flaky group (pack sd:8113), and Fix on a broken row.

Fix posts `/api/items {"fix": [<flaky row id>, ...]}`: the server reads its own flaky heads, files one task per head under
`--ref flaky:<repo>:<head>` in the head's item's repository, with a brief naming each lane run and its prepare log, then
queues its build (`build.queue`, the Build button's). An open item under that ref is shown and nothing is filed. A broken
row's Fix posts `/api/items` with `ref: broken:<row id>` and `build: true`. Over real HTTP; `builds.kickstart` is replaced.
"""

import json
from datetime import datetime, timedelta, timezone

from sd_db import workflow, writes
from test_build_panel import REPO, Fixture

H1, H2 = "1" * 40, "2" * 40


def stamp(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TheFlakyFix(Fixture):
    def setUp(self):
        super().setUp()
        self.owner = self.ready(status="done")

    def flaky(self, head, *, repository="example/alpha", entry="e"):
        for name, status, hours, extra in ((f"{entry}f", "failed", 3, {"expected_head": head, "head": None,
                                                                        "reason": "the repository gate failed"}),
                                           (f"{entry}p", "merged", 1, {"head": head})):
            body = {"id": name, "item": self.owner, "title": "Item", "repository": repository, "status": status,
                    "step": "prepare", "expected_head": head, "finished_at": stamp(hours),
                    "prepare_log": f"/logs/prepare-{name}.log", "log_host": "hub", **extra}
            self.connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                    (f"lane:v1:{repository}:{name}", stamp(hours), json.dumps(body)))
        self.connection.commit()
        return f"flaky:{repository}:{head}"

    def filed(self, ref):
        return writes.item_by_external(self.connection, "task-ref", ref)

    def test_fix_files_one_task_in_the_heads_repo_and_queues_its_build_once(self):
        row = self.flaky(H1)
        status, _, out = self.post("/api/items", {"fix": [row]})
        self.assertEqual(status, 201, out)
        self.assertEqual((out["filed"], out["existing"], out["failed"]), (1, 0, []))
        item = self.filed(row)
        self.assertEqual((item["repo"], item["status"], item["title"]),
                         (REPO, "ready", f"Flaky test in example/alpha at {H1[:12]}: investigate and fix"))
        body = workflow.item_state(self.connection, item["id"])["item"]["body"]
        for words in ("ef", "/logs/prepare-ef.log", "ep", "make test SUITE=", "never a retry loop"):
            self.assertIn(words, body)
        self.assertEqual(self.builds_of(item["id"]), 1)
        self.assertEqual(out["results"][0]["item"], item["id"])
        # A second press shows the open item and files nothing, queues nothing.
        status, _, again = self.post("/api/items", {"fix": [row]})
        self.assertEqual((status, again["filed"], again["existing"]), (201, 0, 1))
        self.assertEqual(again["results"][0], {"row": row, "item": item["id"], "existing": True})
        self.assertEqual(self.builds_of(item["id"]), 1)

    def test_fix_all_files_one_task_per_head_and_names_a_failed_one(self):
        rows = [self.flaky(H1), self.flaky(H2, entry="g"), "flaky:example/alpha:" + "9" * 40]
        status, _, out = self.post("/api/items", {"fix": rows})
        self.assertEqual(status, 201, out)
        self.assertEqual((out["filed"], out["existing"]), (2, 0))
        self.assertEqual([f["row"] for f in out["failed"]], [rows[2]])
        self.assertIn("no longer a flaky head", out["failed"][0]["error"])
        self.assertNotEqual(self.filed(rows[0])["id"], self.filed(rows[1])["id"])

    def test_a_refused_build_is_named_and_the_others_still_file(self):
        rows = [self.flaky(H1), self.flaky(H2, entry="g")]
        self.connection.execute("UPDATE item SET repo = NULL WHERE id = ?", (self.owner,))
        self.connection.commit()
        status, _, out = self.post("/api/items", {"fix": rows})
        self.assertEqual((status, out["filed"]), (201, 0))
        self.assertEqual(len(out["failed"]), 2)
        self.assertIn("names no repository", out["failed"][0]["error"])

    def test_a_fix_list_takes_only_flaky_row_ids(self):
        status, _, out = self.post("/api/items", {"fix": ["broken:job:x"]})
        self.assertEqual(status, 400)
        self.assertIn("flaky:<repo>:<head>", out["error"])


class TheBrokenFix(Fixture):
    def test_a_broken_row_files_once_and_queues_its_build(self):
        body = {"title": "Review and fix: lane not read", "body": "What: lane not read", "repo": REPO,
                "ref": "broken:dark:lane", "build": True}
        status, _, out = self.post("/api/items", body)
        self.assertEqual(status, 201, out)
        self.assertFalse(out["existing"])
        self.assertEqual(self.builds_of(out["item"]["id"]), 1)
        status, _, again = self.post("/api/items", body)
        self.assertEqual((status, again["existing"], again["item"]["id"]), (201, True, out["item"]["id"]))
        self.assertEqual(self.builds_of(out["item"]["id"]), 1)

    def test_a_broken_row_with_no_repo_files_and_names_the_refused_build(self):
        status, _, out = self.post("/api/items", {"title": "Review and fix: job x", "ref": "broken:job:x", "build": True})
        self.assertEqual(status, 201, out)
        self.assertEqual(out["item"]["status"], "ready")
        self.assertIn("needs a registered repository", out["build_error"])


if __name__ == "__main__":
    import unittest

    unittest.main()
