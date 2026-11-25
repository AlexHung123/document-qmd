from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support
from dochub.layout import ensure_public_tree, ensure_user_tree
from dochub.library import (
    LIBRARY_PRIVATE,
    LIBRARY_PUBLIC,
    PUBLIC_OWNER_ID,
    annotate_wiki_flags,
    can_write_public,
    folder_is_wiki,
    parse_curators,
    parse_library,
    public_root,
)
from dochub.paths import PathError, markdown_dir, qmd_dir
from dochub.seed import seed_public_folder


class LibraryParseTests(unittest.TestCase):
    def test_parse_library_defaults_private(self):
        self.assertEqual(parse_library(None), LIBRARY_PRIVATE)
        self.assertEqual(parse_library(""), LIBRARY_PRIVATE)
        self.assertEqual(parse_library("private"), LIBRARY_PRIVATE)
        self.assertEqual(parse_library("PUBLIC"), LIBRARY_PUBLIC)
        self.assertEqual(parse_library("shared"), LIBRARY_PUBLIC)

    def test_parse_library_rejects_unknown(self):
        with self.assertRaises(PathError):
            parse_library("internet")
        with self.assertRaises(PathError):
            parse_library(1)

    def test_parse_curators(self):
        self.assertEqual(parse_curators(None), ())
        self.assertEqual(parse_curators("  "), ())
        self.assertEqual(parse_curators("alice, bob,alice"), ("alice", "bob"))
        with self.assertRaises(PathError):
            parse_curators("../alice")
        with self.assertRaises(PathError):
            parse_curators("__public__")

    def test_can_write_public(self):
        self.assertFalse(can_write_public((), "alice"))
        self.assertTrue(can_write_public(("alice", "1"), "alice"))
        self.assertFalse(can_write_public(("alice",), "bob"))


class PublicLayoutTests(unittest.TestCase):
    def test_public_tree_is_separate_from_user_tree(self):
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            qmd = support.FakeQmd()
            private = ensure_user_tree(data, "alice", qmd)
            public = ensure_public_tree(data, qmd)
            self.assertTrue((qmd_dir(public) / "index.sqlite").is_file())
            self.assertTrue((qmd_dir(private) / "index.sqlite").is_file())
            self.assertNotEqual(qmd_dir(public), qmd_dir(private))
            self.assertEqual(public, public_root(data))
            self.assertFalse(str(public).startswith(str(private)))
            self.assertNotEqual(
                (qmd_dir(public) / "index.sqlite").resolve(),
                (qmd_dir(private) / "index.sqlite").resolve(),
            )
            pub_yml = (qmd_dir(public) / "index.yml").read_text(encoding="utf-8")
            priv_yml = (qmd_dir(private) / "index.yml").read_text(encoding="utf-8")
            self.assertIn(json.dumps(str(markdown_dir(public))), pub_yml)
            self.assertIn(json.dumps(str(markdown_dir(private))), priv_yml)
            self.assertNotIn(json.dumps(str(markdown_dir(public))), priv_yml)


class WikiDetectTests(unittest.TestCase):
    def test_folder_is_wiki_when_index_exists(self):
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            qmd = support.FakeQmd()
            root = ensure_public_tree(data, qmd)
            folder = markdown_dir(root) / "spr" / "wiki"
            folder.mkdir(parents=True)
            self.assertFalse(folder_is_wiki(root, "spr"))
            (folder / "index.md").write_text("# SPR\n", encoding="utf-8")
            self.assertTrue(folder_is_wiki(root, "spr"))
            self.assertFalse(folder_is_wiki(root, "csr"))
            tree = {
                "type": "dir",
                "name": "",
                "path": "",
                "children": [
                    {"type": "dir", "name": "spr", "path": "spr", "children": []},
                    {"type": "dir", "name": "notes", "path": "notes", "children": []},
                ],
            }
            annotate_wiki_flags(tree, root)
            by_name = {c["name"]: c for c in tree["children"]}
            self.assertTrue(by_name["spr"]["wiki"])
            self.assertFalse(by_name["notes"]["wiki"])


class SeedPublicTests(unittest.TestCase):
    def test_seed_copies_and_registers_under_public_owner(self):
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            src = Path(raw) / "src" / "spr"
            (src / "wiki").mkdir(parents=True)
            (src / "wiki" / "index.md").write_text("# index\n", encoding="utf-8")
            (src / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
            app, _worker, qmd = support.make_app(data)
            result = seed_public_folder(data, src, "spr", app.store, qmd)
            self.assertEqual(result["folder"], "spr")
            self.assertEqual(result["copied"], 2)
            self.assertTrue(result["wiki"])
            inbox = data / "public" / "inbox" / "spr" / "wiki" / "index.md"
            md = data / "public" / "markdown" / "spr" / "wiki" / "index.md"
            self.assertTrue(inbox.is_file())
            self.assertTrue(md.is_file())
            rows = app.store.list_files(PUBLIC_OWNER_ID, limit=20)
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(r.user_id == PUBLIC_OWNER_ID for r in rows))
            self.assertFalse((data / "users").exists())
            # idempotent
            again = seed_public_folder(data, src, "spr", app.store, qmd)
            self.assertEqual(again["copied"], 0)
            self.assertEqual(again["skipped"], 2)
            self.assertEqual(len(app.store.list_files(PUBLIC_OWNER_ID, limit=20)), 2)
            app.store.close()
