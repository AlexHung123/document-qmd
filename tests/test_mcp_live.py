from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import support
from dochub.httpapi import serve_http
from dochub.mcp import DochubClient, McpSession
from test_api import _file_body


class LiveMcpHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, _, self.qmd = support.make_app(Path(self.tmp.name), port=0)
        self.qmd.search_results = [
            {"file": "qmd://markdown/contracts/a.md", "title": "a", "snippet": "alpha", "score": 1},
            {"file": "qmd://markdown/invoices/b.md", "title": "b", "snippet": "beta", "score": 0.5},
        ]
        self.server = serve_http(self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        headers = {"X-User-Id": "alice", "Content-Type": "application/json"}
        self.app.handle(
            "POST",
            "/v1/files",
            headers,
            _file_body(markdown="# pinned", relative="contracts/a.pdf"),
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.store.close()
        self.tmp.cleanup()

    def test_client_and_session_over_http(self):
        client = DochubClient(url=f"http://127.0.0.1:{self.port}", user_id="alice", timeout=5)
        session = McpSession(client, folder="contracts")
        listed = session.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("query", {tool["name"] for tool in listed["result"]["tools"]})
        search = session.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "query", "arguments": {"query": "alpha"}},
            }
        )
        paths = [item["dochub_path"] for item in search["result"]["structuredContent"]["results"]]
        self.assertEqual(paths, ["contracts/a.md"])
        got = session.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get", "arguments": {"file": "contracts/a.md"}},
            }
        )
        self.assertFalse(got["result"]["isError"])
        self.assertIn("pinned", got["result"]["content"][0]["text"])
        denied = session.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "get", "arguments": {"file": "invoices/b.md"}},
            }
        )
        self.assertTrue(denied["result"]["isError"])
        other = DochubClient(url=f"http://127.0.0.1:{self.port}", user_id="bob", timeout=5)
        bob = McpSession(other, folder=None)
        missing = bob.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "get", "arguments": {"file": "contracts/a.md"}},
            }
        )
        self.assertTrue(missing["result"]["isError"])
