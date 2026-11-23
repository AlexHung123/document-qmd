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


    def test_structured_search_reaches_qmd(self):
        resp = self.app.handle(
            "POST",
            "/v1/search",
            self._headers(),
            json.dumps(
                {
                    "intent": "Find the OKR note",
                    "searches": [
                        {"type": "lex", "query": "OKR"},
                        {"type": "hyde", "query": "A note about quarterly OKRs"},
                    ],
                    "limit": 10,
                    "rerank": False,
                    "candidateLimit": 20,
                    "minScore": 0.2,
                }
            ).encode(),
        )
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["mode"], "query")
        self.assertIn("intent: Find the OKR note", payload["query"])
        self.assertIn("lex: OKR", payload["query"])
        self.assertIn("hyde: A note about quarterly OKRs", payload["query"])
        self.assertEqual(payload["rerank"], False)
        self.assertEqual(payload["candidate_limit"], 20)
        self.assertEqual(payload["min_score"], 0.2)
        kwargs = [c[3] for c in self.qmd.calls if c[0] == "search"][-1]
        self.assertEqual(kwargs["mode"], "query")
        self.assertEqual(kwargs["rerank"], False)
        self.assertEqual(kwargs["candidate_limit"], 20)
        self.assertEqual(kwargs["min_score"], 0.2)
        self.assertTrue(payload["semantic_search_ready"])
        self.assertNotIn("fallback", payload)

    def test_legacy_search_body_still_works(self):
        resp = self.app.handle(
            "POST",
            "/v1/search",
            self._headers(),
            json.dumps({"query": "hello", "mode": "search", "limit": 5}).encode(),
        )
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["mode"], "search")
        self.assertEqual(payload["query"], "hello")
        self.assertNotIn("fallback", payload)
        self.assertFalse(payload.get("rerank", False))

    def test_bare_query_mode_fills_lex_vec_and_defaults_rerank_off(self):
        resp = self.app.handle(
            "POST",
            "/v1/search",
            self._headers(),
            json.dumps({"query": "hello\nworld", "intent": "Find the note", "mode": "query"}).encode(),
        )
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["mode"], "query")
        self.assertEqual(payload["query"], "intent: Find the note\nlex: hello world\nvec: hello world")
        self.assertFalse(payload["rerank"])
        self.assertNotIn("fallback", payload)
        kwargs = [c[3] for c in self.qmd.calls if c[0] == "search"][-1]
        self.assertEqual(kwargs["mode"], "query")
        self.assertFalse(kwargs["rerank"])

    def test_search_falls_back_when_semantic_not_ready(self):
        self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(markdown="# hello", relative="docs/note.md"),
        )
        resp = self.app.handle(
            "POST",
            "/v1/search",
            self._headers(),
            json.dumps({"query": "hello", "mode": "query"}).encode(),
        )
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["fallback"], {"reason": "semantic_not_ready", "mode": "search"})
        self.assertIn("keyword search", payload["hint"].lower())
        calls = [c for c in self.qmd.calls if c[0] == "search"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], "hello")
        self.assertEqual(calls[0][3]["mode"], "search")

    def test_search_falls_back_on_timeout(self):
        from dochub.qmd import QmdError

        self.qmd.search_errors = {"query": QmdError("timed out after 20s", reason="timeout")}
        resp = self.app.handle(
            "POST",
            "/v1/search",
            self._headers(),
            json.dumps({"query": "hello", "mode": "query"}).encode(),
        )
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["fallback"]["reason"], "timeout")
        modes = [c[3]["mode"] for c in self.qmd.calls if c[0] == "search"]
        self.assertEqual(modes, ["query", "search"])

    def test_search_falls_back_when_hybrid_busy(self):
        self.assertTrue(self.app.hybrid_limiter.try_acquire())
        try:
            resp = self.app.handle(
                "POST",
                "/v1/search",
                self._headers(),
                json.dumps({"query": "hello", "mode": "query"}).encode(),
            )
        finally:
            self.app.hybrid_limiter.release()
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["fallback"], {"reason": "busy", "mode": "search"})
        self.assertEqual([c[3]["mode"] for c in self.qmd.calls if c[0] == "search"], ["search"])

    def test_search_fallback_opt_out_returns_502(self):
        from dochub.qmd import QmdError

        self.qmd.search_errors = {"query": QmdError("boom", reason="qmd_exec_failed")}
        resp = self.app.handle(
            "POST",
            "/v1/search",
            self._headers(),
            json.dumps({"query": "hello", "mode": "query", "fallback": False}).encode(),
        )
        self.assertEqual(resp.status, 502)
        self.assertEqual([c[3]["mode"] for c in self.qmd.calls if c[0] == "search"], ["query"])

    def test_get_document_suggests_similar_paths(self):
        self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(filename="note.pdf", markdown="# hello", relative="docs/note.pdf"),
        )
        resp = self.app.handle("GET", "/v1/documents?path=docs/notes.md", self._headers(), b"")
        self.assertEqual(resp.status, 404)
        payload = json.loads(resp.body)
        self.assertIn("docs/note.md", payload["similar"])

    def test_get_document_by_docid(self):
        self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(filename="note.pdf", markdown="# hello\n\nworld\n", relative="docs/note.pdf"),
        )
        self.qmd.docids = {"#abc123": {"file": "qmd://markdown/docs/note.md"}}
        resp = self.app.handle("GET", "/v1/documents?path=%23abc123", self._headers(), b"")
        self.assertEqual(resp.status, 200)
        doc = json.loads(resp.body)["document"]
        self.assertEqual(doc["path"], "docs/note.md")
        self.assertEqual(doc["docid"], "#abc123")
        self.assertIn("hello", doc["text"])

    def test_status_and_context(self):
        self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(markdown="# hello", relative="docs/note.md"),
        )
        status = self.app.handle("GET", "/v1/status", self._headers(), b"")
        self.assertEqual(status.status, 200)
        payload = json.loads(status.body)
        self.assertTrue(payload["ok"])
        self.assertIn("index", payload)
        self.assertFalse(payload["semantic_search_ready"])
        self.assertIn("keyword search", payload["hint"].lower())
        ctx = self.app.handle("GET", "/v1/context", self._headers(), b"")
        self.assertEqual(ctx.status, 200)
        contexts = json.loads(ctx.body)["contexts"]
        self.assertTrue(any("User markdown documents" in item.get("text", "") for item in contexts))

    def test_multi_get_recursive_glob_and_max_bytes(self):
        self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(markdown="# a", relative="docs/a.md"),
        )
        self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(markdown="# b", relative="docs/sub/b.md"),
        )
        resp = self.app.handle(
            "POST",
            "/v1/documents/multi",
            self._headers(),
            json.dumps({"files": ["docs/**/*.md"], "max_bytes": 20480, "line_numbers": False}).encode(),
        )
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        paths = sorted(doc["path"] for doc in payload["documents"])
        self.assertEqual(paths, ["docs/a.md", "docs/sub/b.md"])
