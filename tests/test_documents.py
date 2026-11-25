from __future__ import annotations

import unittest

import support  # noqa: F401
from dochub.documents import glob_match


class GlobMatchTests(unittest.TestCase):
    def test_single_star_stays_in_one_segment(self):
        self.assertTrue(glob_match("docs/a.md", "docs/*.md"))
        self.assertFalse(glob_match("docs/sub/b.md", "docs/*.md"))

    def test_basename_star_matches_any_depth(self):
        self.assertTrue(glob_match("a.md", "*.md"))
        self.assertTrue(glob_match("docs/a.md", "*.md"))
        self.assertTrue(glob_match("docs/sub/b.md", "*.md"))

    def test_recursive_glob_matches_nested(self):
        self.assertTrue(glob_match("docs/a.md", "docs/**/*.md"))
        self.assertTrue(glob_match("docs/sub/b.md", "docs/**/*.md"))
        self.assertFalse(glob_match("other/a.md", "docs/**/*.md"))
