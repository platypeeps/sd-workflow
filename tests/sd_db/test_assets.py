"""Home assets (pack sd:8053): the `home_asset` rows the property map draws, and the care items that tend them."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sd_db import assets, connect
from sd_db.migrate import initialise
from sd_db.workflow import capture_task, change_status
from sd_db.writes import set_item_fields


class Assets(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "sd.db"
        initialise(path)
        self.db = connect(path, write=True)
        self.addCleanup(self.db.close)

    def test_a_point_and_a_line_round_trip_and_an_edit_replaces_them(self):
        shutoff = assets.save_asset(self.db, kind="gas-shutoff", name="Gas meter",
                                    geometry={"type": "Point", "coordinates": [3200000.5, 1650000]})
        line = assets.save_asset(self.db, kind="water-line", name="Main",
                                 geometry={"type": "LineString", "coordinates": [[1, 2], [3, 4], [5, 6]]},
                                 fields={"depth": "4 ft"})
        self.assertEqual(shutoff["geometry"], {"type": "Point", "coordinates": [3200000.5, 1650000.0]})
        self.assertEqual(line["fields"], {"depth": "4 ft"})
        moved = assets.save_asset(self.db, asset=shutoff["id"], kind="gas-shutoff", name="Gas meter",
                                  geometry={"type": "Point", "coordinates": [10, 20]}, photo="https://example.test/m.jpg")
        self.assertEqual((moved["geometry"]["coordinates"], moved["photo"]), ([10.0, 20.0], "https://example.test/m.jpg"))
        self.assertEqual([a["id"] for a in assets.list_assets(self.db)], [shutoff["id"], line["id"]])
        self.assertEqual(assets.delete_asset(self.db, line["id"])["name"], "Main")
        self.assertEqual([a["name"] for a in assets.list_assets(self.db)], ["Gas meter"])

    def test_a_bad_value_is_refused_by_name_and_nothing_is_written(self):
        for values, words in ((dict(kind="moat", name="x"), "kind is one of"),
                              (dict(kind="tree", name=" "), "name is text"),
                              (dict(kind="tree", name="Oak", geometry={"type": "Point", "coordinates": [1, float("inf")]}),
                               "two finite numbers"),
                              (dict(kind="tree", name="Oak", geometry={"type": "LineString", "coordinates": [[1, 2]]}),
                               "two points or more"),
                              (dict(kind="tree", name="Oak", geometry={"type": "Point", "coordinates": [True, 2]}),
                               "two finite numbers"),
                              (dict(kind="tree", name="Oak", fields=["x"]), "fields is a JSON object"),
                              (dict(kind="tree", name="Oak", fields={"x": float("nan")}), "JSON cannot keep"),
                              (dict(kind="tree", name="Oak", asset=99), "no asset 99")):
            with self.subTest(words=words), self.assertRaisesRegex(assets.AssetRefused, words):
                assets.save_asset(self.db, **values)
        self.assertEqual(assets.list_assets(self.db), [])

    def test_care_lists_each_assets_open_items_and_marks_an_overdue_one(self):
        oak = assets.save_asset(self.db, kind="tree", name="Oak", fields={"species": "Bur oak"})
        rose = assets.save_asset(self.db, kind="plant", name="Rose")
        due = {}
        for title, when, asset in (("Water the oak", "2026-11-01", oak["id"]), ("Prune the rose", "2026-12-01", rose["id"]),
                                   ("Mulch the oak", "2026-11-20", oak["id"]), ("Unlinked", "2026-01-01", None)):
            item = capture_task(self.db, title=title, due=when, who="operator")["item"]["id"]
            if asset is not None:
                set_item_fields(self.db, item, fields={"area": "home", "asset": asset})
            due[title] = item
        change_status(self.db, due["Mulch the oak"], "done", who="operator")
        found = assets.care(self.db, today="2026-11-07")
        self.assertEqual(found[oak["id"]], {"items": [{"id": due["Water the oak"], "title": "Water the oak",
                                                        "due": "2026-11-01"}], "overdue": True})
        self.assertFalse(found[rose["id"]]["overdue"])
        self.assertEqual(set(found), {oak["id"], rose["id"]})

    def test_item_asset_reads_only_a_whole_number(self):
        for text, want in (('{"asset": 3}', 3), ('{"asset": "3"}', None), ('{"asset": true}', None), ("not json", None),
                           (None, None), ("[1]", None)):
            self.assertEqual(assets.item_asset(text), want, text)


if __name__ == "__main__":
    unittest.main()
