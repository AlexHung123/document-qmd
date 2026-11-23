from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

import support


def _file_body(filename="a.md", content=b"# a", markdown="# a", relative=None):
    body = {
        "filename": filename,
        "content_base64": base64.b64encode(content if isinstance(content, bytes) else content.encode()).decode(),
        "markdown": markdown,
    }
    if relative is not None:
        body["relative_path"] = relative
    return json.dumps(body).encode()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.app, self.worker, self.qmd = support.make_app(self.data)

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _headers(self, user="alice", content_type="application/json"):
        return {"X-User-Id": user, "Content-Type": content_type}

    def test_health_no_user(self):
        resp = self.app.handle("GET", "/health", {}, b"")
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.body)["ok"])

    def test_upload_requires_user_header(self):
        resp = self.app.handle("POST", "/v1/files", {"Content-Type": "application/json"}, _file_body())
        self.assertEqual(resp.status, 400)
        self.assertIn("X-User-Id", json.loads(resp.body)["message"])

    def test_upload_requires_markdown(self):
        body = json.dumps(
            {"filename": "a.pdf", "content_base64": base64.b64encode(b"%PDF").decode()}
        ).encode()
        resp = self.app.handle("POST", "/v1/files", self._headers(), body)
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(resp.body)["error"], "markdown_required")

    def test_upload_writes_inbox_markdown_and_task(self):
        resp = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(filename="note.pdf", content=b"%PDF", markdown="# hello", relative="docs/note.pdf"),
        )
        self.assertEqual(resp.status, 202)
        payload = json.loads(resp.body)
        self.assertEqual(payload["task"]["status"], "pending")
        self.assertEqual(payload["task"]["stage"], "qmd_update")
        self.assertEqual(payload["file"]["markdown_relpath"], "docs/note.md")
        inbox = self.data / "users" / "alice" / "inbox" / "docs" / "note.pdf"
        md = self.data / "users" / "alice" / "markdown" / "docs" / "note.md"
        self.assertEqual(inbox.read_bytes(), b"%PDF")
        self.assertIn("# hello", md.read_text(encoding="utf-8"))
        before = self.app.store.counts("alice")
        search = self.app.handle(
            "POST", "/v1/search", self._headers(), json.dumps({"query": "hello", "mode": "search"}).encode()
        )
        self.assertEqual(search.status, 200)
        self.assertEqual(before, self.app.store.counts("alice"))
        result = json.loads(search.body)
        self.assertEqual(result["results"][0]["cwd"], "alice")
        self.assertEqual(len([c for c in self.qmd.calls if c[0] == "search"]), 1)

    def test_user_cannot_read_other_files(self):
        resp = self.app.handle("POST", "/v1/files", self._headers("alice"), _file_body())
        file_id = json.loads(resp.body)["file"]["id"]
        got = self.app.handle("GET", f"/v1/files/{file_id}", self._headers("bob"), b"")
        self.assertEqual(got.status, 404)

    def test_path_traversal_rejected(self):
        resp = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(relative="../bob/inbox/x.md"),
        )
        self.assertEqual(resp.status, 400)

    def test_multipart_upload(self):
        boundary = "----TestBoundary"
        raw = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="x.pdf"\r\n'
            "Content-Type: application/pdf\r\n"
            "\r\n"
            "%PDF\n"
            "\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="markdown"\r\n'
            "\r\n"
            "# mp"
            "\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        headers = self._headers(content_type=f"multipart/form-data; boundary={boundary}")
        resp = self.app.handle("POST", "/v1/files", headers, raw)
        self.assertEqual(resp.status, 202)
        payload = json.loads(resp.body)
        content = self.app.handle(
            "GET",
            f"/v1/files/{payload['file']['id']}/content?tree=markdown",
            self._headers(),
            b"",
        )
        self.assertEqual(content.status, 200)
        self.assertIn(b"# mp", content.body)

    def test_unknown_route(self):
        resp = self.app.handle("GET", "/v1/mcp", self._headers(), b"")
        self.assertEqual(resp.status, 404)

    def test_invalid_user_id(self):
        resp = self.app.handle("GET", "/v1/me", {"X-User-Id": "../alice"}, b"")
        self.assertEqual(resp.status, 400)

    def test_search_path_filter(self):
        self.qmd.search_results = [
            {"file": "qmd://markdown/contracts/a.md", "title": "a", "score": 1},
            {"file": "qmd://markdown/invoices/b.md", "title": "b", "score": 0.5},
        ]
        resp = self.app.handle(
            "POST",
            "/v1/search",
            self._headers(),
            json.dumps({"query": "hello", "path": "contracts", "limit": 10}).encode(),
        )
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["path"], "contracts")
        self.assertEqual([item["dochub_path"] for item in payload["results"]], ["contracts/a.md"])
        kwargs = [c[3] for c in self.qmd.calls if c[0] == "search"][-1]
        self.assertGreater(kwargs["limit"], 10)

    def test_search_path_traversal_rejected(self):
        resp = self.app.handle(
            "POST",
            "/v1/search",
            self._headers(),
            json.dumps({"query": "hello", "path": "../bob"}).encode(),
        )
        self.assertEqual(resp.status, 400)

    def test_get_document_by_path(self):
        self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(filename="note.pdf", markdown="# hello\n\nworld\n", relative="docs/note.pdf"),
        )
        resp = self.app.handle("GET", "/v1/documents?path=docs/note.md", self._headers(), b"")
        self.assertEqual(resp.status, 200)
        doc = json.loads(resp.body)["document"]
        self.assertEqual(doc["path"], "docs/note.md")
        self.assertIn("1:", doc["text"])
        sliced = self.app.handle(
            "GET",
            "/v1/documents?path=docs/note.md:1:1&line_numbers=0",
            self._headers(),
            b"",
        )
        self.assertEqual(sliced.status, 200)
        text = json.loads(sliced.body)["document"]["text"]
        self.assertTrue(text.startswith("---") or "hello" in text or text.startswith("#"))

    def test_get_document_rejects_escape_and_other_user(self):
        self.app.handle("POST", "/v1/files", self._headers("alice"), _file_body(relative="a.md", markdown="# a"))
        missing = self.app.handle("GET", "/v1/documents?path=a.md", self._headers("bob"), b"")
        self.assertEqual(missing.status, 404)
        escape = self.app.handle("GET", "/v1/documents?path=../bob/a.md", self._headers("alice"), b"")
        self.assertEqual(escape.status, 400)
