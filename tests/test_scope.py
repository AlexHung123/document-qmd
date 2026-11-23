from __future__ import annotations

import unittest

import support  # noqa: F401
from dochub.paths import PathError
from dochub.scope import (
    constrain_to_pin,
    filter_results,
    overfetch_limit,
    parse_document_path,
    parse_folder_scope,
    parse_markdown_relpath,
    path_under_prefix,
    resolve_scope,
    split_line_range,
)


class ScopeTests(unittest.TestCase):
    def test_parse_qmd_uri(self):
        self.assertEqual(parse_markdown_relpath("qmd://markdown/contracts/a.md"), "contracts/a.md")
        self.assertEqual(parse_markdown_relpath("markdown/contracts/a.md"), "contracts/a.md")
        self.assertEqual(parse_markdown_relpath("contracts/a.md"), "contracts/a.md")
        self.assertIsNone(parse_markdown_relpath("qmd://markdown"))
        self.assertIsNone(parse_markdown_relpath("#abc123"))

    def test_nested_collection_folder_keeps_inner_markdown(self):
        self.assertEqual(parse_markdown_relpath("qmd://markdown/markdown/a.md"), "markdown/a.md")

    def test_line_suffix(self):
        self.assertEqual(split_line_range("note.md:120:40"), ("note.md", 120, 40))
        self.assertEqual(split_line_range("note.md:120"), ("note.md", 120, None))
        path, start, count = parse_document_path("qmd://markdown/a.md:10:5")
        self.assertEqual(path, "a.md")
        self.assertEqual((start, count), (10, 5))

    def test_docid_rejected_as_folder_and_document(self):
        with self.assertRaises(PathError):
            parse_folder_scope("#abc")
        with self.assertRaises(PathError):
            parse_document_path("#abc")

    def test_traversal_rejected(self):
        with self.assertRaises(PathError):
            parse_folder_scope("../bob")
        with self.assertRaises(PathError):
            parse_document_path("qmd://markdown/../bob/a.md")

    def test_path_under_prefix(self):
        self.assertTrue(path_under_prefix("contracts/a.md", "contracts"))
        self.assertTrue(path_under_prefix("contracts", "contracts"))
        self.assertFalse(path_under_prefix("contract/a.md", "contracts"))
        self.assertFalse(path_under_prefix("contracts-old/a.md", "contracts"))
        self.assertTrue(path_under_prefix("a.md", None))

    def test_resolve_scope_joins_relative_and_keeps_under_pin(self):
        self.assertEqual(resolve_scope("contracts", None), "contracts")
        self.assertEqual(resolve_scope("contracts", "contracts/2024"), "contracts/2024")
        self.assertEqual(resolve_scope("contracts", "2024"), "contracts/2024")
        self.assertEqual(resolve_scope(None, "invoices"), "invoices")
        self.assertEqual(constrain_to_pin("a.md", "contracts"), "contracts/a.md")
        self.assertEqual(constrain_to_pin("contracts/a.md", "contracts"), "contracts/a.md")

    def test_filter_results_keeps_prefix_and_annotates(self):
        raw = [
            {"file": "qmd://markdown/contracts/a.md", "title": "a"},
            {"file": "qmd://markdown/invoices/b.md", "title": "b"},
            {"file": "qmd://markdown/contracts/2024/c.md", "title": "c"},
        ]
        got = filter_results(raw, "contracts", 10)
        paths = [item["dochub_path"] for item in got]
        self.assertEqual(paths, ["contracts/a.md", "contracts/2024/c.md"])
        self.assertEqual(filter_results(raw, "contracts/2024", 10)[0]["dochub_path"], "contracts/2024/c.md")

    def test_overfetch_only_when_scoped(self):
        self.assertEqual(overfetch_limit(10, scoped=False), 10)
        self.assertGreaterEqual(overfetch_limit(10, scoped=True), 40)
        self.assertLessEqual(overfetch_limit(50, scoped=True), 100)
