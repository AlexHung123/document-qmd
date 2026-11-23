from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import support
from dochub.layout import ensure_user_tree
from dochub.paths import markdown_dir
from dochub.qmd import QmdRunner
from dochub.util import atomic_write_text


@unittest.skipUnless(shutil.which("qmd"), "qmd CLI is not on PATH")
class LiveQmdTests(unittest.TestCase):
    def test_local_index_is_not_the_default_db(self):
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            settings = support.test_settings(data)
            qmd = QmdRunner(settings)
            root = ensure_user_tree(data, "alice", qmd)
            sqlite = root / ".qmd" / "index.sqlite"
            self.assertTrue(sqlite.is_file())
            home_default = Path.home() / ".cache" / "qmd" / "index.sqlite"
            self.assertNotEqual(sqlite.resolve(), home_default.resolve())
            atomic_write_text(markdown_dir(root) / "note.md", "# Hello\n\nLive search about authentication flow.\n")
            qmd.update(root)
            results = qmd.search(root, "authentication", mode="search", limit=5)
            self.assertTrue(results)
            self.assertTrue(any("authentication" in json.dumps(item).lower() for item in results))
            self.assertTrue(str(results[0].get("file", "")).startswith("qmd://markdown/"))
