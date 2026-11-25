from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

import support
from dochub.httpapi import content_disposition, latin1_header_value


def _file_body(filename="a.md", content=b"# a", markdown="# a", relative=None):
    body = {
        "filename": filename,
        "content_base64": base64.b64encode(content if isinstance(content, bytes) else content.encode()).decode(),
        "markdown": markdown,
    }
    if relative is not None:
        body["relative_path"] = relative
    return json.dumps(body).encode()


def _send_header_line(keyword: str, value: str) -> bytes:
    return ("%s: %s\r\n" % (keyword, value)).encode("latin-1", "strict")


def _filename_from_star(header: str) -> str:
    marker = "filename*=UTF-8''"
    start = header.lower().find(marker.lower())
    if start < 0:
        raise AssertionError(f"filename* missing from {header!r}")
    token = header[start + len(marker) :].split(";")[0].strip()
    return unquote(token)


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

    def test_upload_conflict_when_inbox_path_already_registered(self):
        first = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(filename="note.pdf", content=b"AAAA", markdown="# a", relative="docs/note.pdf"),
        )
        self.assertEqual(first.status, 202)
        first_id = json.loads(first.body)["file"]["id"]
        inbox = self.data / "users" / "alice" / "inbox" / "docs" / "note.pdf"
        markdown = self.data / "users" / "alice" / "markdown" / "docs" / "note.md"
        inbox.unlink()
        markdown.unlink()
        second = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(filename="note.pdf", content=b"BBBB", markdown="# b", relative="docs/note.pdf"),
        )
        self.assertEqual(second.status, 409)
        self.assertEqual(json.loads(second.body)["error"], "conflict")
        self.assertFalse(inbox.exists())
        row = self.app.store.get_file(first_id)
        self.assertIsNotNone(row)
        self.assertEqual(row.sha256, hashlib.sha256(b"AAAA").hexdigest())
        self.assertEqual(len(self.app.store.list_files("alice")), 1)

    def test_second_upload_same_path_is_renamed(self):
        first = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(filename="note.pdf", content=b"AAAA", markdown="# a", relative="docs/note.pdf"),
        )
        second = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(filename="note.pdf", content=b"BBBB", markdown="# b", relative="docs/note.pdf"),
        )
        self.assertEqual(first.status, 202)
        self.assertEqual(second.status, 202)
        first_rel = json.loads(first.body)["file"]["inbox_relpath"]
        second_rel = json.loads(second.body)["file"]["inbox_relpath"]
        self.assertEqual(first_rel, "docs/note.pdf")
        self.assertNotEqual(second_rel, first_rel)
        inbox = self.data / "users" / "alice" / "inbox"
        self.assertEqual((inbox / first_rel).read_bytes(), b"AAAA")
        self.assertEqual((inbox / second_rel).read_bytes(), b"BBBB")

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

    def test_dot_and_dotdot_user_id_do_not_escape_users_dir(self):
        for user in (".", ".."):
            resp = self.app.handle("GET", "/v1/tree", {"X-User-Id": user}, b"")
            self.assertEqual(resp.status, 400, user)
            self.assertEqual(json.loads(resp.body)["error"], "bad_path")
        self.assertFalse((self.data / "inbox").exists())
        self.assertFalse((self.data / "markdown").exists())
        self.assertFalse((self.data / ".qmd").exists())
        self.assertFalse((self.data / "users" / "inbox").exists())

    def test_reserved_public_owner_is_not_a_user_id(self):
        resp = self.app.handle("GET", "/v1/tree", {"X-User-Id": "__public__"}, b"")
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(resp.body)["error"], "bad_path")
        self.assertFalse((self.data / "users" / "__public__").exists())

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

    def test_search_does_not_fallback_after_historical_failed_ingest(self):
        first = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(markdown="# old", relative="docs/old.md"),
        )
        self.assertEqual(first.status, 202)
        failed_id = json.loads(first.body)["task"]["id"]
        claimed = self.app.store.claim()
        self.assertEqual(claimed.id, failed_id)
        self.app.store.fail(failed_id, "embed boom", permanent=True)

        second = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(markdown="# hello", relative="docs/note.md"),
        )
        self.assertEqual(second.status, 202)
        later_id = json.loads(second.body)["task"]["id"]
        claimed = self.app.store.claim()
        self.assertEqual(claimed.id, later_id)
        self.app.store.set_stage(later_id, "qmd_embed")
        self.app.store.succeed(later_id)

        self.qmd.status_payload = {
            "totalDocuments": 2,
            "needsEmbedding": 0,
            "hasVectorIndex": True,
            "collections": [{"name": "markdown", "documents": 2}],
        }
        resp = self.app.handle(
            "POST",
            "/v1/search",
            self._headers(),
            json.dumps({"query": "hello", "mode": "query"}).encode(),
        )
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertTrue(payload["semantic_search_ready"])
        self.assertNotIn("fallback", payload)
        modes = [c[3]["mode"] for c in self.qmd.calls if c[0] == "search"]
        self.assertEqual(modes, ["query"])

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

    def test_multi_get_single_star_does_not_recurse(self):
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
            json.dumps({"files": ["docs/*.md"], "line_numbers": False}).encode(),
        )
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        paths = sorted(doc["path"] for doc in payload["documents"])
        self.assertEqual(paths, ["docs/a.md"])

    def test_multi_get_similar_stays_under_path_prefix(self):
        self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(markdown="# contract", relative="contracts/keep.md"),
        )
        self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(markdown="# invoice", relative="invoices/note.md"),
        )
        resp = self.app.handle(
            "POST",
            "/v1/documents/multi",
            self._headers(),
            json.dumps({"files": ["contracts/note.md"], "path": "contracts"}).encode(),
        )
        self.assertEqual(resp.status, 200)
        errors = json.loads(resp.body)["errors"]
        self.assertTrue(errors)
        similar = errors[0].get("similar") or []
        self.assertNotIn("invoices/note.md", similar)

    def test_content_download_non_ascii_filename(self):
        filename = "季度報告.pdf"
        resp = self.app.handle(
            "POST",
            "/v1/files",
            self._headers(),
            _file_body(
                filename=filename,
                content=b"%PDF",
                markdown="# 報告",
                relative="docs/季度報告.pdf",
            ),
        )
        self.assertEqual(resp.status, 202)
        file_id = json.loads(resp.body)["file"]["id"]

        inbox = self.app.handle("GET", f"/v1/files/{file_id}/content", self._headers(), b"")
        self.assertEqual(inbox.status, 200)
        self.assertEqual(inbox.body, b"%PDF")
        inbox_disp = inbox.headers["Content-Disposition"]
        _send_header_line("Content-Disposition", inbox_disp)
        self.assertEqual(_filename_from_star(inbox_disp), filename)

        markdown = self.app.handle(
            "GET",
            f"/v1/files/{file_id}/content?tree=markdown",
            self._headers(),
            b"",
        )
        self.assertEqual(markdown.status, 200)
        self.assertIn("# 報告", markdown.body.decode("utf-8"))
        md_disp = markdown.headers["Content-Disposition"]
        _send_header_line("Content-Disposition", md_disp)
        self.assertEqual(_filename_from_star(md_disp), "季度報告.md")


class ContentDispositionTests(unittest.TestCase):
    def test_ascii_filename_needs_no_star(self):
        value = content_disposition("notes.pdf")
        _send_header_line("Content-Disposition", value)
        self.assertEqual(value, 'attachment; filename="notes.pdf"')
        self.assertNotIn("filename*", value)

    def test_non_ascii_filename_uses_rfc5987(self):
        name = "季度報告.pdf"
        value = content_disposition(name)
        _send_header_line("Content-Disposition", value)
        self.assertIn('filename="download.pdf"', value)
        self.assertEqual(_filename_from_star(value), name)

    def test_strips_crlf_path_components_and_quotes(self):
        value = content_disposition('dir/evil\r\nX-Injected: 1".md')
        _send_header_line("Content-Disposition", value)
        self.assertNotIn("\r", value)
        self.assertNotIn("\n", value)
        line = _send_header_line("Content-Disposition", value).decode("latin-1")
        self.assertEqual(line.count("\r\n"), 1)
        self.assertEqual(_filename_from_star(value), 'evilX-Injected: 1".md')

    def test_latin1_header_value_rewrites_unsafe_content_disposition(self):
        naive = 'attachment; filename="季度報告.pdf"'
        with self.assertRaises(UnicodeEncodeError):
            _send_header_line("Content-Disposition", naive)
        fixed = latin1_header_value("Content-Disposition", naive)
        _send_header_line("Content-Disposition", fixed)
        self.assertEqual(_filename_from_star(fixed), "季度報告.pdf")

    def test_latin1_header_value_replaces_other_unsafe_headers(self):
        value = latin1_header_value("X-Name", "季度報告")
        _send_header_line("X-Name", value)
        self.assertNotEqual(value, "季度報告")
