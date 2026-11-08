"""Home map attachments (pack sd:8112): an asset's Drive folder, `<kind group>/<asset name>` under `home.drive_folder`,
through the one `hoa_attach` module. workspace-mcp is the HOA tests' `Drive` stand-in behind `hoa_attach._call`."""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import hoa_attach, source_intake
from sd_db import assets, writes
from support import ScreenCase
from test_hoa_attach import FOLDER, Drive

HOME = hoa_attach.HOME


class TheHomeFolders(unittest.TestCase):
    def test_each_kind_files_under_its_group_and_koi_under_the_pond(self):
        key = "home.drive_folder"
        self.assertEqual(HOME.place({"kind": "gas-shutoff", "name": "Meter valve"}), (key, [], ["Gas", "Meter valve"]))
        self.assertEqual(HOME.place({"kind": "backflow", "name": "BF 1"}), (key, [], ["Irrigation", "BF 1"]))
        self.assertEqual(HOME.place({"kind": "tree", "name": "Oak"}), (key, [], ["Plants", "Oak"]))
        self.assertEqual(HOME.place({"kind": "pond", "name": "Pond"}), (key, [], ["Other", "Pond"]))
        self.assertEqual(HOME.place({"kind": "koi", "name": "Kohaku", "pond": "Pond"}),
                         (key, [], ["Other", "Pond", "Kohaku"]))
        self.assertEqual(HOME.account, "home.google_account")


class Case(ScreenCase):
    def setUp(self):
        super().setUp()
        self.drive = Drive()
        self.drive.items = {"HROOT": []}
        for target, name, value in ((hoa_attach, "_call", self.drive),
                                    (hoa_attach.notes_screen, "workspace", lambda: ("http://localhost:8083/mcp", "", "")),
                                    (source_intake, "_state", lambda: Path(self.tmp.name) / "intake")):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        writes.set_setting(self.connection, "home.drive_folder", "HROOT")
        writes.set_setting(self.connection, "home.google_account", "x@example.invalid")
        self.oak = assets.save_asset(self.connection, kind="tree", name="Oak")

    def upload(self, asset):
        data = b"\xff\xd8 a picture"
        status, staged = source_intake.stage("Oak.jpg", str(len(data)), io.BytesIO(data), suffixes=hoa_attach.SUFFIXES,
                                             limit=hoa_attach.UPLOAD_BYTES)
        self.assertEqual(status, 200, staged)
        return hoa_attach.request({"asset": asset, "sha256": staged["sha256"], "name": "Oak.jpg"}, site=HOME)(self.connection)


class TheHomeAttachments(Case):
    def test_a_first_upload_makes_the_group_and_asset_folders_and_writes_into_them(self):
        listed = hoa_attach.listing(self.connection, {"asset": [str(self.oak["id"])]}, site=HOME)
        self.assertEqual((listed["folder"], listed["files"]), ("", []))
        self.assertEqual(self.drive.writes(), [], "a read makes no folder")
        status, body = self.upload(self.oak["id"])
        self.assertEqual(status, 200, body)
        made = [(tool, args.get("folder_name") or args.get("file_name"), args.get("parent_folder_id") or args.get("folder_id"))
                for tool, args in self.drive.writes()]
        self.assertEqual(made, [("create_drive_folder", "Plants", "HROOT"), ("create_drive_folder", "Oak", "NEW1"),
                                ("create_drive_file", "Oak.jpg", "NEW2")])
        listed = hoa_attach.listing(self.connection, {"asset": [str(self.oak["id"])]}, site=HOME)
        self.assertEqual(listed["folder"], "https://drive.google.com/drive/folders/NEW2")
        self.assertEqual([(f["name"], f["kind"]) for f in listed["files"]], [("Oak.jpg", "picture")])
        # A second upload finds both folders: none is made twice.
        self.upload(self.oak["id"])
        self.assertEqual([tool for tool, _ in self.drive.writes()].count("create_drive_folder"), 2)

    def test_a_koi_files_under_the_ponds_folder(self):
        assets.save_asset(self.connection, kind="pond", name="Pond")
        fish = assets.save_asset(self.connection, kind="koi", name="Kohaku")
        self.drive.items["HROOT"] = [("Other", "OTH", FOLDER)]
        self.drive.items["OTH"] = [("Pond", "PND", FOLDER)]
        self.drive.items["PND"] = [("Kohaku", "KOH", FOLDER)]
        self.drive.items["KOH"] = []
        self.assertEqual(hoa_attach.listing(self.connection, {"asset": [str(fish["id"])]}, site=HOME)["folder"],
                         "https://drive.google.com/drive/folders/KOH")

    def test_an_unset_root_lists_no_folder_and_an_upload_names_the_setting(self):
        writes.set_setting(self.connection, "home.drive_folder", None)
        listed = hoa_attach.listing(self.connection, {"asset": [str(self.oak["id"])]}, site=HOME)
        self.assertEqual((listed["folder"], listed["files"]), ("", []))
        status, body = self.upload(self.oak["id"])
        self.assertEqual(status, 503)
        self.assertIn("Nothing was written: home.drive_folder is not set", body["error"])
        self.assertEqual(self.drive.calls, [], "Drive is not asked")

    def test_an_unknown_asset_is_named(self):
        with self.assertRaisesRegex(ValueError, "Name the asset"):
            hoa_attach.request({"asset": "Oak", "link": "https://drive.google.com/x"}, site=HOME)
        got = hoa_attach.listing(self.connection, {"asset": ["999"]}, site=HOME)
        self.assertEqual(got[0], 400)


if __name__ == "__main__":
    unittest.main()
