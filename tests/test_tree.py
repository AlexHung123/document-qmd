from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support
from dochub.paths import inbox_dir, markdown_dir
from dochub.tree import _file_brief
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

    def test_tree_uses_stored_sizes_not_disk(self):
        self.app.handle("POST", "/v1/folders", self._headers(), json.dumps({"path": "contracts/2024"}).encode())
        up = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(filename="report.pdf", content=b"%PDF", markdown="# report", relative="contracts/2024/report.pdf"),
        )
        self.assertEqual(up.status, 202)
        file_id = json.loads(up.body)["file"]["id"]
        stored = self.app.store.get_file(file_id)
        self.assertIsNotNone(stored.markdown_size_bytes)
        root = self.data / "users" / "alice"
        (inbox_dir(root) / "contracts" / "2024" / "report.pdf").write_bytes(b"x" * 100)
        (markdown_dir(root) / "contracts" / "2024" / "report.md").write_text("# much longer markdown\n" * 40, encoding="utf-8")
        tree = self.app.handle("GET", "/v1/tree", self._headers(), b"")
        self.assertEqual(tree.status, 200)
        payload = json.loads(tree.body)["tree"]
        contracts = next(c for c in payload["children"] if c["name"] == "contracts")
        year = next(c for c in contracts["children"] if c["name"] == "2024")
        report = next(c for c in year["children"] if c["name"] == "report.pdf")
        self.assertEqual(report["file"]["inbox_size_bytes"], 4)
        self.assertEqual(report["file"]["markdown_size_bytes"], stored.markdown_size_bytes)
        self.assertNotEqual(report["file"]["markdown_size_bytes"], (markdown_dir(root) / "contracts" / "2024" / "report.md").stat().st_size)

    def test_tree_omits_unknown_markdown_size(self):
        self.app.handle("POST", "/v1/folders", self._headers(), json.dumps({"path": "legacy"}).encode())
        inbox = inbox_dir(self.data / "users" / "alice") / "legacy" / "old.pdf"
        inbox.write_bytes(b"%PDF")
        self.app.store.insert_registered_file(
            user_id="alice",
            original_filename="old.pdf",
            content_type="application/pdf",
            sha256="abc",
            size_bytes=4,
            inbox_relpath="legacy/old.pdf",
            markdown_relpath="legacy/old.md",
        )
        tree = self.app.handle("GET", "/v1/tree", self._headers(), b"")
        self.assertEqual(tree.status, 200)
        payload = json.loads(tree.body)["tree"]
        folder = next(c for c in payload["children"] if c["name"] == "legacy")
        report = next(c for c in folder["children"] if c["name"] == "old.pdf")
        self.assertEqual(report["file"]["inbox_size_bytes"], 4)
        self.assertNotIn("markdown_size_bytes", report["file"])

    def test_file_brief_does_not_stat(self):
        row = self.app.store.insert_registered_file(
            user_id="alice",
            original_filename="note.md",
            content_type="text/markdown",
            sha256="abc",
            size_bytes=9,
            inbox_relpath="note.md",
            markdown_relpath="note.md",
            markdown_size_bytes=12,
        )
        brief = _file_brief(row)
        self.assertEqual(brief["inbox_size_bytes"], 9)
        self.assertEqual(brief["markdown_size_bytes"], 12)
        row_unknown = self.app.store.insert_registered_file(
            user_id="alice",
            original_filename="other.md",
            content_type="text/markdown",
            sha256="def",
            size_bytes=3,
            inbox_relpath="other.md",
            markdown_relpath="other.md",
        )
        unknown = _file_brief(row_unknown)
        self.assertEqual(unknown["inbox_size_bytes"], 3)
        self.assertNotIn("markdown_size_bytes", unknown)
