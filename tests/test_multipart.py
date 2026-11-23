from __future__ import annotations

import unittest

import support  # noqa: F401
from dochub.multipart import parse_multipart


class MultipartTests(unittest.TestCase):
    def test_file_and_fields(self):
        boundary = "----BoundaryXYZ"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="note.md"\r\n'
            "Content-Type: text/markdown\r\n"
            "\r\n"
            "# Hello\n"
            "\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="relative_path"\r\n'
            "\r\n"
            "docs/note.md"
            "\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="markdown"\r\n'
            "\r\n"
            "# Converted"
            "\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        parts = parse_multipart(f"multipart/form-data; boundary={boundary}", body)
        self.assertEqual(parts["file"].filename, "note.md")
        self.assertEqual(parts["file"].data, b"# Hello\n")
        self.assertEqual(parts["relative_path"].data, b"docs/note.md")
        self.assertEqual(parts["markdown"].data, b"# Converted")

    def test_binary_payload(self):
        boundary = "----Bin"
        payload = b"\x00\xffPDF"
        body = (
            b"--" + boundary.encode() + b"\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.bin"\r\n'
            b"Content-Type: application/octet-stream\r\n"
            b"\r\n" + payload + b"\r\n--" + boundary.encode() + b"--\r\n"
        )
        parts = parse_multipart(f'multipart/form-data; boundary="{boundary}"', body)
        self.assertEqual(parts["file"].data, payload)
