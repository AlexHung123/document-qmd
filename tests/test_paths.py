from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from dochub.paths import (
    PathError,
    markdown_relpath_for,
    normalize_relative_path,
    parse_user_id,
    safe_join,
    sanitize_filename,
    unique_relpath,
    user_root,
)


class PathTests(unittest.TestCase):
    def test_user_id(self):
        self.assertEqual(parse_user_id("alice"), "alice")
        self.assertEqual(parse_user_id("a@b.com"), "a@b.com")
        with self.assertRaises(PathError):
            parse_user_id("../x")
        with self.assertRaises(PathError):
            parse_user_id("a/b")
        with self.assertRaises(PathError):
            parse_user_id("")
        with self.assertRaises(PathError):
            parse_user_id(".")
        with self.assertRaises(PathError):
            parse_user_id("..")
        with self.assertRaises(PathError):
            parse_user_id("__public__")

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("report.pdf"), "report.pdf")
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("初步書面.md"), "初步書面.md")
        self.assertFalse(sanitize_filename(".hidden").startswith("."))

    def test_relative_path_rejects_traversal(self):
        with self.assertRaises(PathError):
            normalize_relative_path("../x")
        with self.assertRaises(PathError):
            normalize_relative_path("/abs")
        with self.assertRaises(PathError):
            normalize_relative_path("a/.hidden/b")
        self.assertEqual(normalize_relative_path("contracts/a.pdf"), "contracts/a.pdf")

    def test_safe_join(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            joined = safe_join(root, "a/b.md")
            self.assertTrue(str(joined).startswith(str(root.resolve())))
            with self.assertRaises(PathError):
                safe_join(root, "../outside")

    def test_unique_relpath(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "note.md").write_text("x")
            rel = unique_relpath(root, "note.md", "abcd1234-xxxx")
            self.assertTrue(rel.startswith("note-"))
            self.assertTrue(rel.endswith(".md"))
            self.assertNotEqual(rel, "note.md")

    def test_unique_relpath_skips_occupied_suffix(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "note.md").write_text("x")
            (root / "note-abcd1234.md").write_text("y")
            rel = unique_relpath(root, "note.md", "abcd1234-xxxx")
            self.assertNotEqual(rel, "note.md")
            self.assertNotEqual(rel, "note-abcd1234.md")
            self.assertFalse((root / rel).exists())

    def test_markdown_relpath(self):
        self.assertEqual(markdown_relpath_for("2026/11/a.pdf"), "2026/11/a.md")
        self.assertEqual(markdown_relpath_for("a.md"), "a.md")

    def test_user_root_stays_under_data(self):
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            root = user_root(data, "alice")
            self.assertEqual(root, (data / "users" / "alice").resolve())
            self.assertEqual(root.parent, (data / "users").resolve())
            for bad in (".", "..", "__public__"):
                with self.assertRaises(PathError):
                    user_root(data, bad)

    def test_ensure_relative_dir(self):
        from dochub.paths import ensure_relative_dir

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            created = ensure_relative_dir(root, "a/b/c")
            self.assertTrue(created.is_dir())
            self.assertTrue((root / "a" / "b" / "c").is_dir())
            (root / "a" / "file").write_text("x")
            with self.assertRaises(PathError):
                ensure_relative_dir(root, "a/file")
