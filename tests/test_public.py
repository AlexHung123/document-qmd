from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support
from dochub.library import PUBLIC_OWNER_ID
from dochub.paths import qmd_dir

from test_api import _file_body


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.app, self.worker, self.qmd = support.make_app(
            self.data, public_curators=("alice",)
        )

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _headers(self, user="alice", content_type="application/json"):
        return {"X-User-Id": user, "Content-Type": content_type}

    def test_me_reports_public_write(self):
        alice = json.loads(self.app.handle("GET", "/v1/me", self._headers("alice"), b"").body)
        bob = json.loads(self.app.handle("GET", "/v1/me", self._headers("bob"), b"").body)
        self.assertTrue(alice["can_write_public"])
        self.assertFalse(bob["can_write_public"])
        self.assertEqual(alice["libraries"], ["private", "public"])

    def test_create_public_folder_rejected_for_non_curator(self):
        resp = self.app.handle(
            "POST",
            "/v1/folders",
            self._headers("bob"),
            json.dumps({"path": "spr", "library": "public"}).encode(),
        )
        self.assertEqual(resp.status, 403)
        self.assertEqual(json.loads(resp.body)["error"], "forbidden")
        self.assertFalse((self.data / "public" / "inbox" / "spr").exists())

    def test_create_public_folder_allowed_for_curator(self):
        resp = self.app.handle(
            "POST",
            "/v1/folders",
            self._headers("alice"),
            json.dumps({"path": "spr", "visibility": "public"}).encode(),
        )
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["library"], "public")
        self.assertTrue((self.data / "public" / "inbox" / "spr").is_dir())
        self.assertTrue((self.data / "public" / "markdown" / "spr").is_dir())

    def test_public_upload_rejected_for_non_curator(self):
        resp = self.app.handle(
            "POST",
            "/v1/files",
            self._headers("bob"),
            _file_body(relative="spr/note.md", markdown="# public"),
        )
        # default library is private, so bob can write his own tree
        self.assertEqual(resp.status, 202)
        public = self.app.handle(
            "POST",
            "/v1/files",
            self._headers("bob"),
            json.dumps(
                {
                    "filename": "note.md",
                    "content_base64": __import__("base64").b64encode(b"# public").decode(),
                    "markdown": "# public",
                    "relative_path": "spr/note.md",
                    "library": "public",
                }
            ).encode(),
        )
        self.assertEqual(public.status, 403)
        self.assertFalse((self.data / "public" / "inbox" / "spr" / "note.md").exists())

    def test_public_and_private_indexes_stay_separate(self):
        priv = self.app.handle(
            "POST",
            "/v1/files",
            self._headers("alice"),
            _file_body(filename="secret.md", markdown="# secret", relative="notes/secret.md"),
        )
        self.assertEqual(priv.status, 202)
        pub = self.app.handle(
            "POST",
            "/v1/files",
            self._headers("alice"),
            json.dumps(
                {
                    "filename": "policy.md",
                    "content_base64": __import__("base64").b64encode(b"# policy").decode(),
                    "markdown": "# policy",
                    "relative_path": "spr/policy.md",
                    "library": "public",
                }
            ).encode(),
        )
        self.assertEqual(pub.status, 202)
        self.assertTrue((self.data / "users" / "alice" / "inbox" / "notes" / "secret.md").is_file())
        self.assertTrue((self.data / "public" / "inbox" / "spr" / "policy.md").is_file())
        self.assertNotEqual(
            (qmd_dir(self.data / "users" / "alice") / "index.sqlite").resolve(),
            (qmd_dir(self.data / "public") / "index.sqlite").resolve(),
        )
        pub_file = json.loads(pub.body)["file"]
        self.assertEqual(pub_file["library"], "public")
        self.assertEqual(pub_file["user_id"], PUBLIC_OWNER_ID)
        # Worker indexes public files against the public root, not alice's private tree.
        self.assertTrue(self.worker.process_one())
        self.assertTrue(self.worker.process_one())
        public_updates = [
            c for c in self.qmd.calls if c[0] == "update" and Path(c[1]) == self.data / "public"
        ]
        private_updates = [
            c
            for c in self.qmd.calls
            if c[0] == "update" and Path(c[1]) == self.data / "users" / "alice"
        ]
        self.assertEqual(len(public_updates), 1)
        self.assertEqual(len(private_updates), 1)

        self.qmd.search_results = [
            {"file": "qmd://markdown/spr/policy.md", "title": "policy", "score": 1},
        ]
        public_search = self.app.handle(
            "POST",
            "/v1/search",
            self._headers("bob"),
            json.dumps({"query": "policy", "library": "public"}).encode(),
        )
        self.assertEqual(public_search.status, 200)
        public_payload = json.loads(public_search.body)
        self.assertEqual(public_payload["library"], "public")
        search_cwds = [c[1] for c in self.qmd.calls if c[0] == "search"]
        self.assertEqual(Path(search_cwds[-1]), self.data / "public")

        self.qmd.search_results = [
            {"file": "qmd://markdown/notes/secret.md", "title": "secret", "score": 1},
        ]
        private_search = self.app.handle(
            "POST",
            "/v1/search",
            self._headers("bob"),
            json.dumps({"query": "secret", "library": "private"}).encode(),
        )
        self.assertEqual(private_search.status, 200)
        # Bob's private search must run in bob's tree, not alice's and not public.
        search_cwds = [c[1] for c in self.qmd.calls if c[0] == "search"]
        self.assertEqual(Path(search_cwds[-1]), self.data / "users" / "bob")

    def test_bob_cannot_read_alice_private_file_but_can_read_public(self):
        priv = self.app.handle(
            "POST",
            "/v1/files",
            self._headers("alice"),
            _file_body(filename="secret.md", markdown="# secret", relative="notes/secret.md"),
        )
        priv_id = json.loads(priv.body)["file"]["id"]
        pub = self.app.handle(
            "POST",
            "/v1/files",
            self._headers("alice"),
            json.dumps(
                {
                    "filename": "policy.md",
                    "content_base64": __import__("base64").b64encode(b"# policy").decode(),
                    "markdown": "# policy",
                    "relative_path": "spr/policy.md",
                    "library": "public",
                }
            ).encode(),
        )
        pub_id = json.loads(pub.body)["file"]["id"]
        denied = self.app.handle("GET", f"/v1/files/{priv_id}", self._headers("bob"), b"")
        self.assertEqual(denied.status, 404)
        allowed = self.app.handle("GET", f"/v1/files/{pub_id}", self._headers("bob"), b"")
        self.assertEqual(allowed.status, 200)
        self.assertEqual(json.loads(allowed.body)["file"]["library"], "public")
        content = self.app.handle(
            "GET",
            f"/v1/files/{pub_id}/content?tree=markdown",
            self._headers("bob"),
            b"",
        )
        self.assertEqual(content.status, 200)
        self.assertIn(b"# policy", content.body)

    def test_public_tree_visible_to_any_user(self):
        self.app.handle(
            "POST",
            "/v1/folders",
            self._headers("alice"),
            json.dumps({"path": "spr", "library": "public"}).encode(),
        )
        (self.data / "public" / "markdown" / "spr" / "wiki").mkdir(parents=True)
        (self.data / "public" / "markdown" / "spr" / "wiki" / "index.md").write_text(
            "# SPR\n", encoding="utf-8"
        )
        tree = self.app.handle("GET", "/v1/tree?library=public", self._headers("bob"), b"")
        self.assertEqual(tree.status, 200)
        payload = json.loads(tree.body)
        self.assertEqual(payload["library"], "public")
        names = [c["name"] for c in payload["tree"]["children"]]
        self.assertIn("spr", names)
        spr = next(c for c in payload["tree"]["children"] if c["name"] == "spr")
        self.assertTrue(spr["wiki"])

    def test_public_document_read_scoped_to_public_root(self):
        self.app.handle(
            "POST",
            "/v1/files",
            self._headers("alice"),
            json.dumps(
                {
                    "filename": "index.md",
                    "content_base64": __import__("base64").b64encode(b"# SPR index").decode(),
                    "markdown": "# SPR index",
                    "relative_path": "spr/wiki/index.md",
                    "library": "public",
                }
            ).encode(),
        )
        got = self.app.handle(
            "GET",
            "/v1/documents?library=public&path=spr/wiki/index.md",
            self._headers("bob"),
            b"",
        )
        self.assertEqual(got.status, 200)
        body = json.loads(got.body)
        self.assertEqual(body["library"], "public")
        self.assertIn("SPR index", body["document"]["text"])

        # A path that only exists in alice's private tree is not visible via public get.
        self.app.handle(
            "POST",
            "/v1/files",
            self._headers("alice"),
            _file_body(filename="secret.md", markdown="# secret", relative="notes/secret.md"),
        )
        missing = self.app.handle(
            "GET",
            "/v1/documents?library=public&path=notes/secret.md",
            self._headers("bob"),
            b"",
        )
        self.assertEqual(missing.status, 404)
