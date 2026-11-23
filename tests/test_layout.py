from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support
from dochub.layout import ensure_user_tree
from dochub.paths import inbox_dir, markdown_dir, qmd_dir


class LayoutTests(unittest.TestCase):
    def test_creates_fixed_tree_and_local_index(self):
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            qmd = support.FakeQmd()
            root = ensure_user_tree(data, "alice", qmd)
            self.assertTrue(inbox_dir(root).is_dir())
            self.assertTrue(markdown_dir(root).is_dir())
            self.assertTrue((qmd_dir(root) / "index.yml").is_file())
            self.assertTrue((qmd_dir(root) / "index.sqlite").is_file())
            text = (qmd_dir(root) / "index.yml").read_text(encoding="utf-8")
            self.assertIn("markdown:", text)
            self.assertIn(json.dumps(str(markdown_dir(root))), text)
            self.assertEqual(qmd.calls[0][0], "init")
            self.assertEqual(Path(qmd.calls[0][1]), root)

    def test_updates_collection_path(self):
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            qmd = support.FakeQmd()
            root = ensure_user_tree(data, "alice", qmd)
            yml = qmd_dir(root) / "index.yml"
            yml.write_text(
                'collections:\n  markdown:\n    path: "/old/path"\n    pattern: "**/*.md"\nmodels:\n  embed: x\n',
                encoding="utf-8",
            )
            ensure_user_tree(data, "alice", qmd)
            text = yml.read_text(encoding="utf-8")
            self.assertIn(json.dumps(str(markdown_dir(root))), text)
            self.assertIn("embed: x", text)
            self.assertNotIn("/old/path", text)
