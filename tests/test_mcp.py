from __future__ import annotations

import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import support
from dochub.mcp import (
    DochubClient,
    McpSession,
    compose_query,
    read_message,
    write_message,
)
from test_api import _file_body


def app_transport(app, user_id: str):
    def _call(method, path, body, query):
        url = path
        if query:
            filtered = {key: value for key, value in query.items() if value not in (None, "")}
            if filtered:
                url += "?" + urlencode(filtered)
        headers = {"X-User-Id": user_id}
        raw = b""
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(body).encode()
        resp = app.handle(method, url, headers, raw)
        payload = json.loads(resp.body) if resp.body else {}
        return resp.status, payload

    return _call


def _call_tool(session: McpSession, name: str, arguments: dict, msg_id=1):
    return session.handle(
        {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.app, _, self.qmd = support.make_app(self.data)
        self.qmd.search_results = [
            {"file": "qmd://markdown/contracts/a.md", "title": "contract a", "snippet": "alpha", "score": 0.9},
            {"file": "qmd://markdown/invoices/b.md", "title": "invoice b", "snippet": "beta", "score": 0.8},
            {"file": "qmd://markdown/contracts/2024/c.md", "title": "year c", "snippet": "gamma", "score": 0.7},
        ]
        self.client = DochubClient(
            url="http://dochub.test",
            user_id="alice",
            transport=app_transport(self.app, "alice"),
        )
        self.session = McpSession(self.client, folder=None)
        headers = {"X-User-Id": "alice", "Content-Type": "application/json"}
        self.app.handle(
            "POST",
            "/v1/files",
            headers,
            _file_body(filename="a.pdf", markdown="# contract a", relative="contracts/a.pdf"),
        )
        self.app.handle(
            "POST",
            "/v1/files",
            headers,
            _file_body(filename="b.pdf", markdown="# invoice b", relative="invoices/b.pdf"),
        )

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def test_initialize_and_tool_list(self):
        reply = self.session.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        self.assertEqual(reply["result"]["serverInfo"]["name"], "dochub")
        self.assertIn("never pass user_id", reply["result"]["instructions"])
        listed = self.session.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(names, {"query", "get", "multi_get", "tree", "status"})
        self.assertIsNone(
            self.session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_query_filters_to_path(self):
        reply = _call_tool(self.session, "query", {"query": "alpha", "path": "contracts", "limit": 10})
        payload = reply["result"]["structuredContent"]
        self.assertEqual(payload["path"], "contracts")
        paths = [item["dochub_path"] for item in payload["results"]]
        self.assertEqual(paths, ["contracts/a.md", "contracts/2024/c.md"])
        self.assertNotIn("invoices/b.md", paths)

    def test_folder_pin_blocks_sibling_get(self):
        pinned = McpSession(self.client, folder="contracts")
        ok = _call_tool(pinned, "get", {"file": "qmd://markdown/contracts/a.md"})
        self.assertFalse(ok["result"]["isError"])
        self.assertIn("contract a", ok["result"]["content"][0]["text"])
        denied = _call_tool(pinned, "get", {"file": "invoices/b.md"})
        self.assertTrue(denied["result"]["isError"])
        search = _call_tool(pinned, "query", {"query": "alpha"})
        paths = [item["dochub_path"] for item in search["result"]["structuredContent"]["results"]]
        self.assertTrue(all(p.startswith("contracts") for p in paths))
        self.assertNotIn("invoices/b.md", paths)

    def test_get_line_range_suffix(self):
        reply = _call_tool(self.session, "get", {"file": "contracts/a.md:1:2"})
        text = reply["result"]["content"][0]["text"]
        self.assertIn("1:", text)
        self.assertFalse(reply["result"]["isError"])

    def test_user_id_argument_rejected(self):
        reply = _call_tool(self.session, "query", {"query": "alpha", "user_id": "bob"})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("user_id", reply["result"]["content"][0]["text"])

    def test_status_uses_bound_user(self):
        reply = _call_tool(self.session, "status", {})
        body = reply["result"]["structuredContent"]
        self.assertEqual(body["user_id"], "alice")
        self.assertIsNone(body["folder"])

    def test_unknown_method(self):
        reply = self.session.handle({"jsonrpc": "2.0", "id": 9, "method": "nope"})
        self.assertEqual(reply["error"]["code"], -32601)

    def test_stdio_framing_roundtrip(self):
        buf = BytesIO()
        write_message(buf, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        buf.seek(0)
        msg = read_message(buf)
        self.assertEqual(msg["method"], "ping")
        ndjson = BytesIO(b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        self.assertEqual(read_message(ndjson)["id"], 2)


class ComposeQueryTests(unittest.TestCase):
    def test_lex_only_uses_search(self):
        query, mode = compose_query({"searches": [{"type": "lex", "query": "cockpit OKR"}]})
        self.assertEqual(mode, "search")
        self.assertEqual(query, "cockpit OKR")

    def test_vec_uses_query_mode(self):
        query, mode = compose_query(
            {
                "intent": "metrics as instruments",
                "searches": [
                    {"type": "lex", "query": "OKR"},
                    {"type": "vec", "query": "data informed not metric driven"},
                ],
            }
        )
        self.assertEqual(mode, "query")
        self.assertIn("intent: metrics as instruments", query)
        self.assertIn("lex: OKR", query)
        self.assertIn("vec: data informed not metric driven", query)

    def test_plain_query_defaults_to_search(self):
        query, mode = compose_query({"query": "hello"})
        self.assertEqual((query, mode), ("hello", "search"))

    def test_plain_query_mode_query_fills_lex_and_vec(self):
        query, mode = compose_query({"query": "hello", "intent": "Find the note", "mode": "query"})
        self.assertEqual(mode, "query")
        self.assertEqual(query, "intent: Find the note\nlex: hello\nvec: hello")


class McpConfigTests(unittest.TestCase):
    def test_user_id_required(self):
        from dochub.mcp import config_from_env

        old = {key: os.environ.pop(key, None) for key in ("DOCHUB_USER_ID", "DOCHUB_FOLDER", "DOCHUB_URL")}
        try:
            with self.assertRaises(ValueError):
                config_from_env()
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_cli_user_and_folder(self):
        from dochub.mcp import config_from_env

        url, user, folder, timeout = config_from_env(url="http://mac:8787", user_id="alice", folder="contracts/2024")
        self.assertEqual(url, "http://mac:8787")
        self.assertEqual(user, "alice")
        self.assertEqual(folder, "contracts/2024")
        self.assertEqual(timeout, 180)
