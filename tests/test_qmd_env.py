from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import support
from dochub.qmd import QmdRunner


class QmdEnvTests(unittest.TestCase):
    def test_pwd_points_at_user_root(self):
        with tempfile.TemporaryDirectory() as raw:
            settings = support.test_settings(Path(raw))
            runner = QmdRunner(settings)
            user_root = Path(raw) / "users" / "alice"
            env = runner._env(user_root)
            self.assertEqual(env["PWD"], str(user_root))
            self.assertEqual(env["QMD_TRUST_LOCAL_CONFIG"], "1")
            self.assertNotIn("INDEX_PATH", env)

    def test_parent_index_path_is_stripped(self):
        with tempfile.TemporaryDirectory() as raw:
            settings = support.test_settings(Path(raw))
            runner = QmdRunner(settings)
            old = os.environ.get("INDEX_PATH")
            os.environ["INDEX_PATH"] = "/tmp/global.sqlite"
            try:
                env = runner._env(Path(raw) / "users" / "alice")
            finally:
                if old is None:
                    os.environ.pop("INDEX_PATH", None)
                else:
                    os.environ["INDEX_PATH"] = old
            self.assertNotIn("INDEX_PATH", env)
