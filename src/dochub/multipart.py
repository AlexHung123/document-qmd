from __future__ import annotations

import re
from dataclasses import dataclass


class MultipartError(ValueError):
    pass


@dataclass
class FormPart:
    name: str
    filename: str | None
    content_type: str | None
    data: bytes


_NAME_RE = re.compile(r'name="([^"]*)"')
_FILENAME_RE = re.compile(r'filename="([^"]*)"')


def parse_boundary(content_type: str) -> bytes:
    if not content_type:
        raise MultipartError("missing content type")
    match = re.search(r"boundary=([^;]+)", content_type, re.I)
    if not match:
        raise MultipartError("multipart missing boundary")
    boundary = match.group(1).strip().strip('"')
    if not boundary or len(boundary) > 200:
        raise MultipartError("invalid multipart boundary")
    try:
        return boundary.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MultipartError("invalid multipart boundary") from exc


def parse_multipart(content_type: str, body: bytes) -> dict[str, FormPart]:
    boundary = parse_boundary(content_type)
    sep = b"--" + boundary
    chunks = body.split(sep)
    parts: dict[str, FormPart] = {}
    for raw in chunks:
        if not raw or raw in (b"--", b"--\r\n", b"--\n"):
            continue
        if raw.startswith(b"--"):
            continue
        if raw.startswith(b"\r\n"):
            raw = raw[2:]
        elif raw.startswith(b"\n"):
            raw = raw[1:]
        header_blob, sep_found, data = raw.partition(b"\r\n\r\n")
        if not sep_found:
            header_blob, sep_found, data = raw.partition(b"\n\n")
        if not sep_found:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        elif data.endswith(b"\n"):
            data = data[:-1]
        headers = _parse_headers(header_blob.decode("utf-8", "replace"))
        disposition = headers.get("content-disposition", "")
        if "form-data" not in disposition.lower():
            continue
        name_m = _NAME_RE.search(disposition)
        if not name_m:
            continue
        name = name_m.group(1)
        filename_m = _FILENAME_RE.search(disposition)
        filename = filename_m.group(1) if filename_m else None
        parts[name] = FormPart(
            name=name,
            filename=filename,
            content_type=headers.get("content-type"),
            data=data,
        )
    return parts


def _parse_headers(blob: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in blob.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers
