from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

import support
from dochub.httpapi import serve_http


class LiveHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, _, _ = support.make_app(Path(self.tmp.name), port=0)
        self.server = serve_http(self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.store.close()
        self.tmp.cleanup()

    def _conn(self) -> HTTPConnection:
        return HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_health_and_search_over_http(self):
        conn = self._conn()
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertTrue(body["ok"])

        conn = self._conn()
        payload = json.dumps({"query": "hello", "mode": "search"}).encode()
        conn.request(
            "POST",
            "/v1/search",
            body=payload,
            headers={"X-User-Id": "alice", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["results"][0]["cwd"], "alice")
        self.assertEqual(len(self.app.store.list_tasks("alice")), 0)
