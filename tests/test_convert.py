from __future__ import annotations

import unittest

import support  # noqa: F401
from dochub.convert import merge_front_matter


class ConvertTests(unittest.TestCase):
    def test_merge_existing_front_matter(self):
        body = "---\ntitle: Old\n---\n\nHello\n"
        out = merge_front_matter(body, {"dochub_file_id": "x"})
        self.assertIn("dochub_file_id", out)
        self.assertIn("title", out)
        self.assertIn("Hello", out)

    def test_adds_front_matter_when_missing(self):
        out = merge_front_matter("# Hello\n", {"dochub_source": "a.pdf"})
        self.assertTrue(out.startswith("---\n"))
        self.assertIn("dochub_source", out)
        self.assertIn("# Hello", out)
