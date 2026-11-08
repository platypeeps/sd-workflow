"""HOA map attachments (sd:6690): an asset's Drive folder, read and written through a stubbed workspace-mcp.

`Drive` stands in for workspace-mcp behind `hoa_attach._call`: it answers each tool in the text shape the real server
prints (`list_drive_items`' `- Name: "..." (ID: ..., Type: ...) Link: ...` lines), and records every call, so a test
can say what was sent and that nothing was. No test reaches Google. The failure table is the design's,
`docs/work/2026-10-30-hoa-attachments/design.md`.
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import test_v2_hoa
from sd_dashboard import hoa_attach, source_intake
from sd_db import writes
from test_v2_hoa import Fixture, checkout
from test_workflow_actions import BrowserSession

FOLDER = hoa_attach.FOLDER_MIME


class Drive:
    """workspace-mcp as text: folders by id, each a list of `(name, id, mime)`, and each file's text."""

    def __init__(self):
        self.items = {"ROOT": [("Main", "MAIN", FOLDER), ("Cistern", "CIS", FOLDER), ("Well A2", "WA2", FOLDER)],
                      "MAIN": [], "CIS": [("pump.jpg", "PIC1", "image/jpeg"), ("manual.pdf", "DOC1", "application/pdf"),
                                          ("Layout.url", "LNK1", "text/plain")], "WA2": []}
        self.text = {"LNK1": "[InternetShortcut]\nURL=https://drive.google.com/file/d/TARGET/view\n"}
        self.calls, self.fail, self.say, self.listed, self.made = [], {}, {}, {}, 0

    def __call__(self, tool, arguments, what):
        self.calls.append((tool, arguments))
        if tool in self.fail:
            raise self.fail[tool]
        if tool in self.say:
            return self.say[tool]
        if tool == "list_drive_items":
            if arguments["folder_id"] in self.listed:
                return self.listed[arguments["folder_id"]]
            rows = self.items.get(arguments["folder_id"], [])
            if not rows:
                return f"No items found in folder '{arguments['folder_id']}'."
            return f"Found {len(rows)} items in folder '{arguments['folder_id']}' for x@example.invalid:\n" + "\n".join(
                f'- Name: "{name}" (ID: {ident}, Type: {mime}, Created: 2026-10-30T00:00:00Z, Last Edited By: A '
                f'<a@example.invalid>) Link: https://drive.google.com/open?id={ident}' for name, ident, mime in rows)
        self.made += 1
        ident = f"NEW{self.made}"
        if tool == "create_drive_folder":
            self.items[arguments["parent_folder_id"]].append((arguments["folder_name"], ident, FOLDER))
            self.items[ident] = []
            return f"Successfully created folder '{arguments['folder_name']}' (ID: {ident}) in folder x."
        if tool == "create_drive_file":
            self.items[arguments["folder_id"]].append((arguments["file_name"], ident,
                                                       arguments.get("content_mime_type") or "text/plain"))
            self.text[ident] = arguments.get("content", "")
            return f"Successfully created file '{arguments['file_name']}' (ID: {ident}) in folder x. Link: https://x"
        if tool == "get_drive_file_content":
            return f'File: "x" (ID: {arguments["file_id"]})\n\n--- CONTENT ---\n{self.text[arguments["file_id"]]}'
        if tool == "get_drive_file_download_url":
            mime = "image/jpeg" if arguments["file_id"].startswith("PIC") else "application/pdf"
            return (f"File downloaded successfully!\nFile ID: {arguments['file_id']}\nMIME Type: {mime}\n\n"
                    f"📎 Download URL: http://localhost:8083/attachments/abc\n")
        raise AssertionError(f"unexpected tool {tool}")

    def writes(self):
        return [call for call in self.calls if call[0].startswith("create_")]


class Case(Fixture):
    def setUp(self):
        super().setUp()
        self.drive = Drive()
        for target, name, value in ((hoa_attach, "_call", self.drive),
                              (hoa_attach.notes_screen, "workspace", lambda: ("http://localhost:8083/mcp", "", "")),
                              (source_intake, "_state", lambda: self.root / "intake")):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        writes.set_setting(self.connection, hoa_attach.FOLDER_KEY, "ROOT")
        writes.set_setting(self.connection, hoa_attach.ACCOUNT_KEY, "x@example.invalid")

    def stage(self, data=b"\xff\xd8 a picture", name="Valve photo.JPG"):
        status, staged = source_intake.stage(name, str(len(data)), io.BytesIO(data), suffixes=hoa_attach.SUFFIXES,
                                             limit=hoa_attach.UPLOAD_BYTES)
        self.assertEqual(status, 200, staged)
        return staged

    def attach(self, values):
        return hoa_attach.request(values)(self.connection)


class TheFolders(unittest.TestCase):
    def test_each_asset_type_has_its_folder(self):
        water, light = hoa_attach.FOLDER_KEY, hoa_attach.LIGHT_KEY
        self.assertEqual(hoa_attach.place({"id": "V-01", "type": "valve"}), (water, "Main", "V-01"))
        self.assertEqual(hoa_attach.place({"id": "HYD-03", "type": "hydrant"}), (water, "Main", "HYD-03"))
        self.assertEqual(hoa_attach.place({"id": "WELL-A1", "type": "well"}), (water, "Well A1", None))
        self.assertEqual(hoa_attach.place({"id": "WELL-A2", "type": "well"}), (water, "Well A2", None))
        self.assertEqual(hoa_attach.place({"id": "TANK-01", "type": "tank"}), (water, "Cistern", None))
        # sd:7516: the booster station, the "submarine", files under Submarine.
        self.assertEqual(hoa_attach.place({"id": "BST-1", "type": "booster"}), (water, "Submarine", None))
        # sd:7667: the irrigation system, point or line, is Water System/Irrigation; a street light LGT-n is
        # Light System/LGT-n, a folder the operator made per pole.
        self.assertEqual(hoa_attach.place({"id": "IRR-1", "type": "irrigation"}), (water, "Irrigation", None))
        self.assertEqual(hoa_attach.place({"id": "IRL-1", "type": "irrline"}), (water, "Irrigation", None))
        self.assertEqual(hoa_attach.place({"id": "LGT-3", "type": "light"}), (light, "LGT-3", None))
        with self.assertRaisesRegex(ValueError, "has no attachment folder"):
            hoa_attach.place({"id": "X-1", "type": "meter"})
        self.assertEqual(light, "hoa.light_system_folder")


class TheListing(Case):
    def test_a_folder_lists_pictures_documents_and_references_with_their_targets(self):
        got = hoa_attach.listing(self.connection, {"asset": ["TANK-01"]})
        self.assertEqual(got["folder"], "https://drive.google.com/drive/folders/CIS")
        self.assertEqual([(f["name"], f["kind"]) for f in got["files"]],
                         [("pump.jpg", "picture"), ("manual.pdf", "document"), ("Layout.url", "link")])
        self.assertEqual(got["files"][2]["target"], "https://drive.google.com/file/d/TARGET/view")
        self.assertEqual(self.drive.writes(), [])

    def test_a_valve_with_no_folder_yet_lists_none_and_makes_none(self):
        got = hoa_attach.listing(self.connection, {"asset": ["V-01"]})
        self.assertEqual((got["files"], got["folder"]), ([], ""))
        self.assertEqual(self.drive.writes(), [], "a read makes no folder")

    def test_a_reference_to_a_non_drive_target_shows_no_target(self):
        self.drive.text["LNK1"] = "[InternetShortcut]\nURL=javascript:alert(1)\n"
        got = hoa_attach.listing(self.connection, {"asset": ["TANK-01"]})
        self.assertEqual(got["files"][2]["target"], "")

    def test_failures_are_named_never_an_empty_list(self):
        # Failure table: setting unset; subfolder renamed; two folders of one name; workspace-mcp down.
        writes.set_setting(self.connection, hoa_attach.FOLDER_KEY, None)
        status, body = hoa_attach.listing(self.connection, {"asset": ["TANK-01"]})
        self.assertEqual(status, 503)
        self.assertIn("sd config set hoa.water_system_folder", body["error"])
        writes.set_setting(self.connection, hoa_attach.FOLDER_KEY, "ROOT")
        self.drive.items["ROOT"] = [("Main", "MAIN", FOLDER)]
        self.assertIn("no folder named Cistern", hoa_attach.listing(self.connection, {"asset": ["TANK-01"]})[1]["error"])
        self.drive.items["ROOT"] = [("Cistern", "C1", FOLDER), ("Cistern", "C2", FOLDER)]
        self.assertIn("2 folders named Cistern: C1, C2", hoa_attach.listing(self.connection, {"asset": ["TANK-01"]})[1]["error"])
        self.drive.fail["list_drive_items"] = ConnectionError("workspace-mcp at http://localhost:8083/mcp: refused")
        status, body = hoa_attach.listing(self.connection, {"asset": ["TANK-01"]})
        self.assertEqual(status, 503)
        self.assertIn("refused", body["error"])
        self.assertEqual(hoa_attach.listing(self.connection, {"asset": ["NOPE"]})[0], 400)


    def test_a_listing_drive_did_not_answer_is_no_empty_folder(self):
        # An error text, a page of a longer listing, or a header that does not
        # count its lines: none is a folder's whole listing.
        for said in ("Error: The user has exceeded their Drive API quota",
                     "Found 1 items in folder 'MAIN' for x@example.invalid:\nnextPageToken: abc",
                     "Found 2 items in folder 'MAIN' for x@example.invalid:", ""):
            self.drive.listed["MAIN"] = said
            with self.subTest(said=said):
                staged = self.stage()
                status, body = self.attach({"asset": "V-01", "sha256": staged["sha256"], "name": "a.jpg"})
                self.assertEqual((status, self.drive.writes()), (503, []))
                self.assertIn("Nothing was written", body["error"])
                got = hoa_attach.listing(self.connection, {"asset": ["V-01"]})
                self.assertIsInstance(got, tuple, "an unreadable listing read as a folder")
                self.assertEqual(got[0], 503)
                self.assertIn("unreadable", got[1]["error"])
        self.assertEqual(self.drive.writes(), [], "an unreadable listing makes no folder")


class TheOtherSystems(Case):
    """sd:7667: the irrigation system lists under Water System; a street light under the Light System folder."""

    def setUp(self):
        super().setUp()
        assets = json.loads(json.dumps(test_v2_hoa.LIGHTS))
        assets["features"].append({"type": "Feature", "properties": {**assets["features"][0]["properties"],
                                                                     "id": "IRR-1", "type": "irrigation"},
                                   "geometry": {"type": "Point", "coordinates": [10.0002, 50.0002]}})
        (self.hoa / "water" / "map" / "assets.geojson").write_text(json.dumps(assets), encoding="utf-8")
        self.drive.items["ROOT"].append(("Irrigation", "IRR", FOLDER))
        self.drive.items.update({"IRR": [("zones.pdf", "DOC2", "application/pdf")], "LIGHTS": [
            ("LGT-1", "L1", FOLDER), ("LGT-2", "L2", FOLDER)], "L1": [("pole.jpg", "PIC2", "image/jpeg")], "L2": []})
        writes.set_setting(self.connection, hoa_attach.LIGHT_KEY, "LIGHTS")

    def test_the_irrigation_and_a_light_pole_list_their_own_folders(self):
        got = hoa_attach.listing(self.connection, {"asset": ["IRR-1"]})
        self.assertEqual((got["folder"], [f["name"] for f in got["files"]]),
                         ("https://drive.google.com/drive/folders/IRR", ["zones.pdf"]))
        got = hoa_attach.listing(self.connection, {"asset": ["IRL-1"]})
        self.assertEqual(got["folder"], "https://drive.google.com/drive/folders/IRR")
        before = len(self.drive.calls)
        got = hoa_attach.listing(self.connection, {"asset": ["LGT-1"]})
        self.assertEqual((got["folder"], [f["name"] for f in got["files"]]),
                         ("https://drive.google.com/drive/folders/L1", ["pole.jpg"]))
        self.assertEqual([a["folder_id"] for _, a in self.drive.calls[before:]], ["LIGHTS", "L1"],
                         "a pole is found under the Light System folder, never Water System")
        self.assertEqual(self.drive.writes(), [])

    def test_an_upload_to_a_light_pole_writes_into_its_folder(self):
        staged = self.stage()
        self.assertEqual(self.attach({"asset": "LGT-1", "sha256": staged["sha256"], "name": "a.jpg"})[0], 200)
        self.assertEqual([(t, a["folder_id"]) for t, a in self.drive.writes()], [("create_drive_file", "L1")])

    def test_an_unset_light_setting_is_no_folder_yet_and_an_upload_names_it(self):
        # The pack lands before hoa declares the key and before it is set: a pole lists no folder and no files,
        # nothing is read or made, and an upload is refused naming the setting.
        writes.set_setting(self.connection, hoa_attach.LIGHT_KEY, None)
        got = hoa_attach.listing(self.connection, {"asset": ["LGT-1"]})
        self.assertEqual(got, {"asset": "LGT-1", "folder": "", "files": []})
        self.assertEqual(self.drive.calls, [], "an unset light setting asks Drive nothing")
        status, body = self.attach({"asset": "LGT-1", "link": "https://drive.google.com/file/d/X/view"})
        self.assertEqual(status, 503)
        self.assertIn("Nothing was written: hoa.light_system_folder is not set", body["error"])
        self.assertEqual(self.drive.writes(), [])
        self.assertIsInstance(hoa_attach.listing(self.connection, {"asset": ["TANK-01"]}), dict,
                              "an unset light setting leaves the water assets read")

    def test_a_light_setting_never_held_is_no_folder_yet(self):
        # Review of sd:8112: a key with no row is absent from read_settings, not None; the pack lands before
        # hoa declares the key, so a never-held key is the case the quiet listing exists for.
        self.connection.execute("DELETE FROM state WHERE kind = 'setting' AND key = ?", (hoa_attach.LIGHT_KEY,))
        self.connection.commit()
        self.assertEqual(hoa_attach.listing(self.connection, {"asset": ["LGT-1"]}),
                         {"asset": "LGT-1", "folder": "", "files": []})
        self.assertEqual(self.drive.calls, [], "a never-held light setting asks Drive nothing")

    def test_a_light_pole_with_no_folder_is_named_and_never_made(self):
        writes.set_setting(self.connection, hoa_attach.LIGHT_KEY, "LIGHTS")
        self.drive.items["LIGHTS"] = [("LGT-2", "L2", FOLDER)]
        status, body = hoa_attach.listing(self.connection, {"asset": ["LGT-1"]})
        self.assertEqual(status, 503)
        self.assertIn("The Light System folder has no folder named LGT-1.", body["error"])
        self.assertEqual(self.drive.writes(), [], "a pole's folder is the operator's, never made")


class TheUpload(Case):
    def test_the_first_upload_to_a_valve_makes_its_folder_once_and_writes_the_bytes(self):
        data = b"\xff\xd8 a picture"
        staged = self.stage(data)
        status, out = self.attach({"asset": "V-01", "sha256": staged["sha256"], "name": "Valve photo.JPG"})
        self.assertEqual((status, out["name"]), (200, "Valve-photo.jpg"))
        (folder, made), (upload, sent) = self.drive.writes()
        self.assertEqual((folder, made["folder_name"], made["parent_folder_id"]), ("create_drive_folder", "V-01", "MAIN"))
        self.assertEqual((upload, sent["folder_id"], sent["content_mime_type"]), ("create_drive_file", "NEW1", "image/jpeg"))
        self.assertEqual(sent["base64_sha256"], hashlib.sha256(data).hexdigest())
        self.assertFalse((self.root / "intake" / "staged" / staged["sha256"]).exists(), "a written file leaves staging")
        # The second upload finds the folder the first made.
        staged = self.stage(b"%PDF-1 a document", "Valve test.pdf")
        self.assertEqual(self.attach({"asset": "V-01", "sha256": staged["sha256"], "name": "Valve test.pdf"})[0], 200)
        self.assertEqual([call[0] for call in self.drive.writes()], ["create_drive_folder", "create_drive_file",
                                                                     "create_drive_file"])
        self.assertEqual([f["name"] for f in hoa_attach.listing(self.connection, {"asset": ["V-01"]})["files"]],
                         ["Valve-photo.jpg", "Valve-test.pdf"])

    def test_a_well_and_the_cistern_write_into_their_own_folders(self):
        staged = self.stage()
        self.attach({"asset": "WELL-A2", "sha256": staged["sha256"], "name": "a.jpg"})
        self.assertEqual([(t, a["folder_id"]) for t, a in self.drive.writes()], [("create_drive_file", "WA2")])

    def test_a_staging_refusal_names_the_suffixes_and_the_cap(self):
        status, body = source_intake.stage("run.sh", "4", io.BytesIO(b"echo"), suffixes=hoa_attach.SUFFIXES,
                                           limit=hoa_attach.UPLOAD_BYTES)
        self.assertEqual(status, 400)
        self.assertIn(".jpg", body["error"])
        status, body = source_intake.stage("a.jpg", str(hoa_attach.UPLOAD_BYTES + 1), io.BytesIO(b""),
                                           suffixes=hoa_attach.SUFFIXES, limit=hoa_attach.UPLOAD_BYTES)
        self.assertEqual((status, body["error"]), (400, "A dropped file is 1 byte to 20 MB."))

    def test_each_failure_says_whether_drive_holds_the_file(self):
        staged = self.stage()
        values = {"asset": "TANK-01", "sha256": staged["sha256"], "name": "a.jpg"}
        # Refused: nothing written, and the staged file stays for a second press.
        self.drive.fail["create_drive_file"] = ConnectionError("workspace-mcp refused the upload: token expired")
        status, body = self.attach(values)
        self.assertEqual(status, 503)
        self.assertIn("Nothing was written: workspace-mcp refused the upload: token expired", body["error"])
        # No answer: not known, so the page says to look before pressing again.
        self.drive.fail["create_drive_file"] = LookupError("TimeoutError: timed out")
        status, body = self.attach(values)
        self.assertEqual(status, 504)
        self.assertIn("Whether Drive holds the file is not known", body["error"])
        self.assertTrue((self.root / "intake" / "staged" / staged["sha256"]).exists())
        # Gone from staging: choose it again; nothing sent.
        before = len(self.drive.calls)
        with self.assertRaisesRegex(ValueError, "no longer staged"):
            self.attach({**values, "sha256": "0" * 64})
        self.assertEqual(len(self.drive.calls), before)

    def test_an_answer_naming_no_created_file_is_not_known(self):
        staged = self.stage()
        upload = {"asset": "TANK-01", "sha256": staged["sha256"], "name": "a.jpg"}
        reference = {"asset": "TANK-01", "link": "https://drive.google.com/file/d/X/view"}
        for said in ("Error: The user has exceeded their Drive storage quota", "Successfully created file 'b.jpg' "
                     "(ID: NEW9) in folder x.", "Successfully created file 'a.jpg' in folder x.", ""):
            self.drive.say["create_drive_file"] = said
            for values in (upload, reference):
                with self.subTest(said=said, values=values):
                    status, body = self.attach(values)
                    self.assertEqual(status, 504)
                    self.assertIn("Whether Drive holds the file is not known", body["error"])
        self.assertTrue((self.root / "intake" / "staged" / staged["sha256"]).exists())

    def test_a_folder_answer_naming_no_created_folder_writes_nothing_into_it(self):
        staged = self.stage()
        upload = {"asset": "V-01", "sha256": staged["sha256"], "name": "a.jpg"}
        reference = {"asset": "V-01", "link": "https://drive.google.com/file/d/X/view"}
        for said in ("Error creating folder. Parent folder ID: MAIN", "Successfully created folder 'V-02' (ID: NEW9) "
                     "in folder x.", "Successfully created folder 'V-01' (ID: N/A) in folder x.", ""):
            self.drive.say["create_drive_folder"] = said
            for values in (upload, reference):
                with self.subTest(said=said, values=values):
                    status, body = self.attach(values)
                    self.assertEqual(status, 504)
                    self.assertIn("is not known", body["error"])
        self.assertEqual({call[0] for call in self.drive.writes()}, {"create_drive_folder"}, "no file into a guess")

    def test_the_request_takes_a_file_or_a_link_and_nothing_else(self):
        for bad in ({"asset": "V-01"}, {"asset": "V-01", "sha256": "0" * 64, "link": "https://drive.google.com/x"},
                    {"asset": "V-01", "link": "https://drive.google.com/x", "path": "/etc/passwd"}, {"link": "x"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                hoa_attach.request(bad)


class TheReference(Case):
    def test_a_reference_writes_a_url_file_naming_the_drive_link(self):
        status, out = self.attach({"asset": "V-01", "link": "https://docs.google.com/document/d/ABC/edit",
                                   "name": "Valve manual"})
        self.assertEqual((status, out["name"]), (200, "Valve manual.url"))
        _, sent = self.drive.writes()[-1]
        self.assertEqual(sent["content"], "[InternetShortcut]\nURL=https://docs.google.com/document/d/ABC/edit\n")
        listed = hoa_attach.listing(self.connection, {"asset": ["V-01"]})["files"]
        self.assertEqual([(f["kind"], f["target"]) for f in listed], [("link", "https://docs.google.com/document/d/ABC/edit")])

    def test_only_a_drive_or_docs_link_is_referenced(self):
        for link in ("https://example.com/a", "http://drive.google.com/a", "javascript:alert(1)", "https://drive.google.com.evil/a"):
            with self.subTest(link=link), self.assertRaisesRegex(ValueError, "drive.google.com or docs.google.com"):
                self.attach({"asset": "V-01", "link": link})
        self.assertEqual(self.drive.writes(), [])


class ThePicture(Case):
    def test_a_picture_comes_from_workspace_mcps_own_host_only(self):
        with mock.patch.object(hoa_attach, "_fetch", return_value=b"\xff\xd8jpeg") as fetch:
            self.assertEqual(hoa_attach.picture(self.connection, {"id": ["PIC1234567"]}), (200, b"\xff\xd8jpeg", "image/jpeg"))
            fetch.assert_called_once_with("http://localhost:8083/attachments/abc")
            self.assertEqual(hoa_attach.picture(self.connection, {"id": ["DOC1234567"]})[0], 415)
            with mock.patch.object(hoa_attach.notes_screen, "workspace", lambda: ("http://127.0.0.1:9999/mcp", "", "")):
                self.assertEqual(hoa_attach.picture(self.connection, {"id": ["PIC1234567"]})[0], 502)
            self.assertEqual(hoa_attach.picture(self.connection, {"id": ["../etc"]})[0], 400)
            self.assertEqual(fetch.call_count, 1)

    def test_a_picture_needs_the_account_alone_not_a_folder_setting(self):
        # sd:7680: a light's thumbnail, with only the light folder and the account set, read the water folder setting
        # and answered 503. A picture is named by its file id: the account is all it reads.
        writes.set_setting(self.connection, hoa_attach.FOLDER_KEY, None)
        writes.set_setting(self.connection, hoa_attach.LIGHT_KEY, "LIGHTS")
        with mock.patch.object(hoa_attach, "_fetch", return_value=b"\xff\xd8jpeg"):
            self.assertEqual(hoa_attach.picture(self.connection, {"id": ["PIC1234567"]}), (200, b"\xff\xd8jpeg", "image/jpeg"))
        writes.set_setting(self.connection, hoa_attach.ACCOUNT_KEY, None)
        status, body = hoa_attach.picture(self.connection, {"id": ["PIC1234567"]})
        self.assertEqual(status, 503)
        self.assertIn(f"{hoa_attach.ACCOUNT_KEY} is not set", body["error"])
        self.assertNotIn(hoa_attach.FOLDER_KEY, body["error"])


class TheRoutes(BrowserSession):
    """The server half: staging bytes at /api/hoa/attach/file, and a picture's bytes with an image type only."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo(str(checkout(self.root / "hoa")))
        patch = mock.patch.object(source_intake, "_state", lambda: self.root / "intake")
        patch.start()
        self.addCleanup(patch.stop)

    def raw(self, path, data=None, headers=None):
        request = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                         headers={"Cookie": self.cookie, **(headers or {})})
        try:
            answer = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            answer = error
        with answer:
            return answer.status, answer.headers.get("Content-Type"), answer.read()

    def test_the_page_stages_a_picture_and_reads_one_back_as_an_image(self):
        data = b"\xff\xd8\x00 not utf-8"
        status, _, body = self.raw("/api/hoa/attach/file", data, {
            "Content-Type": "application/octet-stream", "Origin": self.base, "X-SD-CSRF": self.csrf,
            "Sec-Fetch-Site": "same-origin", "X-SD-File-Name": "IMG%201.HEIC"})
        self.assertEqual((status, json.loads(body)["name"]), (200, "IMG-1.heic"))
        self.assertEqual((self.root / "intake" / "staged" / hashlib.sha256(data).hexdigest()).read_bytes(), data)
        with mock.patch.object(hoa_attach, "picture", return_value=(200, data, "image/jpeg")):
            self.assertEqual(self.raw("/api/hoa/attachments/picture?id=PIC1234567"), (200, "image/jpeg", data))
        with mock.patch.object(hoa_attach, "picture", return_value=(200, b"<svg onload=x>", "image/svg+xml")):
            status, kind, _ = self.raw("/api/hoa/attachments/picture?id=PIC1234567")
            self.assertEqual((status, kind.split(";")[0]), (502, "application/json"))


class TheDetails(Fixture):
    """hoa.js: an asset's Details read its attachments. Upload's and Reference's posts are a browser check: the
    stand-in page has no form elements."""

    run_page = test_v2_hoa.TheScript.run_page

    LISTED = {"asset": "TANK-01", "folder": "https://drive.google.com/drive/folders/CIS", "files": [
        {"id": "PIC1234567", "name": "pump.jpg", "mime": "image/jpeg", "link": "https://drive.google.com/open?id=PIC1234567", "kind": "picture"},
        {"id": "LNK1234567", "name": "Layout.url", "mime": "text/plain", "link": "https://drive.google.com/open?id=LNK1234567",
         "kind": "link", "target": "https://drive.google.com/file/d/TARGET/view"}]}

    def test_an_assets_details_show_its_attachments_with_picture_thumbnails(self):
        pre = ("const was0 = ANSWER; ANSWER = (p, b) => p.startsWith('/api/hoa/attachments?') ? [200, "
               f"{json.dumps(self.LISTED)}] : was0(p, b);")
        out = self.run_page("""document.dispatchEvent(new CustomEvent('shell:open', { detail: 'asset:TANK-01' })); await flush();
R.det = ELS.details.html; R.attach = ELS.attach.html;""", pre=pre)
        self.assertIn("/api/hoa/attachments?asset=TANK-01", out["gets"])
        self.assertIn('<h3 class="label">Attachments</h3><div id="attach"', out["R"]["det"])
        self.assertIn('<img class="thumb" src="/api/hoa/attachments/picture?id=PIC1234567"', out["R"]["attach"])
        self.assertIn('href="https://drive.google.com/file/d/TARGET/view" target="_blank" rel="noopener">Layout.url', out["R"]["attach"])
        self.assertIn('data-attach="upload"', out["R"]["attach"])
        self.assertIn('data-attach="reference"', out["R"]["attach"])

    def test_a_listing_that_failed_is_unknown_with_its_reason(self):
        pre = ("const was0 = ANSWER; ANSWER = (p, b) => p.startsWith('/api/hoa/attachments?') ? [503, "
               "{ error: 'The attachments were not read: hoa.water_system_folder is not set' }] : was0(p, b);")
        out = self.run_page("""document.dispatchEvent(new CustomEvent('shell:open', { detail: 'asset:TANK-01' })); await flush();
R.attach = ELS.attach.html;""", pre=pre)
        self.assertIn("<b>▨ Not read.</b> The attachments were not read: hoa.water_system_folder is not set", out["R"]["attach"])
        self.assertNotIn("data-attach=", out["R"]["attach"])


if __name__ == "__main__":
    unittest.main()
