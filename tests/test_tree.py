from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support
from dochub.paths import inbox_dir, markdown_dir
from test_api import _file_body


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.app, _, _ = support.make_app(self.data)

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _headers(self, user="alice"):
        return {"X-User-Id": user, "Content-Type": "application/json"}

    def test_create_folder_in_inbox_and_markdown(self):
        resp = self.app.handle(
            "POST",
            "/v1/folders",
            self._headers(),
            json.dumps({"path": "contracts/2024"}).encode(),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body)["path"], "contracts/2024")
        root = self.data / "users" / "alice"
        self.assertTrue((inbox_dir(root) / "contracts" / "2024").is_dir())
        self.assertTrue((markdown_dir(root) / "contracts" / "2024").is_dir())

    def test_create_folder_is_idempotent(self):
        body = json.dumps({"path": "notes"}).encode()
        self.assertEqual(self.app.handle("POST", "/v1/folders", self._headers(), body).status, 200)
        self.assertEqual(self.app.handle("POST", "/v1/folders", self._headers(), body).status, 200)

    def test_folder_path_traversal_rejected(self):
        resp = self.app.handle(
            "POST",
            "/v1/folders",
            self._headers(),
            json.dumps({"path": "../bob"}).encode(),
        )
        self.assertEqual(resp.status, 400)

    def test_tree_lists_folders_and_files(self):
        self.app.handle("POST", "/v1/folders", self._headers(), json.dumps({"path": "contracts/2024"}).encode())
        up = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(filename="report.pdf", content=b"%PDF", markdown="# report", relative="contracts/2024/report.pdf"),
        )
        self.assertEqual(up.status, 202)
        file_id = json.loads(up.body)["file"]["id"]
        tree = self.app.handle("GET", "/v1/tree", self._headers(), b"")
        self.assertEqual(tree.status, 200)
        payload = json.loads(tree.body)["tree"]
        contracts = next(c for c in payload["children"] if c["name"] == "contracts")
        year = next(c for c in contracts["children"] if c["name"] == "2024")
        self.assertEqual(year["type"], "dir")
        report = next(c for c in year["children"] if c["name"] == "report.pdf")
        self.assertEqual(report["type"], "file")
        self.assertEqual(report["file"]["id"], file_id)
        self.assertEqual(report["file"]["size_bytes"], 4)
        self.assertEqual(report["file"]["inbox_size_bytes"], 4)
        self.assertGreaterEqual(report["file"]["markdown_size_bytes"], len("# report"))

    def test_tree_prefix(self):
        self.app.handle("POST", "/v1/folders", self._headers(), json.dumps({"path": "a/b"}).encode())
        tree = self.app.handle("GET", "/v1/tree?path=a", self._headers(), b"")
        self.assertEqual(tree.status, 200)
        payload = json.loads(tree.body)["tree"]
        self.assertEqual(payload["path"], "a")
        self.assertEqual(payload["children"][0]["path"], "a/b")
        missing = self.app.handle("GET", "/v1/tree?path=nope", self._headers(), b"")
        self.assertEqual(missing.status, 404)

    def test_upload_into_existing_folder(self):
        self.app.handle("POST", "/v1/folders", self._headers(), json.dumps({"path": "docs"}).encode())
        resp = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(relative="docs/note.pdf", markdown="# n"),
        )
        self.assertEqual(resp.status, 202)
        self.assertEqual(json.loads(resp.body)["file"]["inbox_relpath"], "docs/note.pdf")

    def test_upload_cannot_overwrite_folder(self):
        self.app.handle("POST", "/v1/folders", self._headers(), json.dumps({"path": "docs"}).encode())
        resp = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(relative="docs", markdown="# n"),
        )
        self.assertEqual(resp.status, 409)
