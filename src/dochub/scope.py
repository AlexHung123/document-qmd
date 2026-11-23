from __future__ import annotations

import re
from typing import Any

from .paths import PathError, normalize_relative_path

COLLECTION = "markdown"
QMD_SCHEME = "qmd://"
LINE_RANGE_RE = re.compile(r":(\d+):(\d+)$")
LINE_FROM_RE = re.compile(r":(\d+)$")
OVERFETCH_FACTOR = 8
OVERFETCH_FLOOR = 40
OVERFETCH_CAP = 100


def split_line_range(spec: str) -> tuple[str, int | None, int | None]:
    """Split `path:from:count` or `path:from` suffixes used by QMD get."""
    if not isinstance(spec, str):
        raise PathError("invalid path")
    text = spec.strip()
    match = LINE_RANGE_RE.search(text)
    if match:
        return text[: match.start()], int(match.group(1)), int(match.group(2))
    match = LINE_FROM_RE.search(text)
    if match:
        return text[: match.start()], int(match.group(1)), None
    return text, None, None


def parse_markdown_relpath(raw: str) -> str | None:
    """Markdown-relative path from a QMD URI, collection-prefixed path, or folder.

    `#docid` returns None. Collection root (`markdown` / `qmd://markdown`) returns None.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PathError("invalid path")
    text, _, _ = split_line_range(raw.strip())
    if text.startswith("#"):
        return None
    if text.startswith(QMD_SCHEME):
        text = text[len(QMD_SCHEME) :]
    text = text.replace("\\", "/").lstrip("/")
    if text.startswith("./"):
        text = text[2:]
    if text == COLLECTION:
        return None
    if text.startswith(COLLECTION + "/"):
        text = text[len(COLLECTION) + 1 :]
    if not text:
        return None
    return normalize_relative_path(text)


def parse_folder_scope(raw: str | None) -> str | None:
    """Optional folder pin under the user's markdown tree. None means the whole tree."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise PathError("path must be a string")
    if not raw.strip():
        return None
    text = raw.strip()
    stripped, _, _ = split_line_range(text)
    if stripped.startswith("#"):
        raise PathError("path must be a folder or file, not a docid")
    return parse_markdown_relpath(text)


def parse_document_path(raw: str) -> tuple[str, int | None, int | None]:
    if not isinstance(raw, str) or not raw.strip():
        raise PathError("document path is required")
    text, from_line, max_lines = split_line_range(raw.strip())
    if text.startswith("#"):
        raise PathError("docid is not supported; use dochub_path from search results")
    rel = parse_markdown_relpath(text)
    if not rel:
        raise PathError("document path is required")
    return rel, from_line, max_lines


def path_under_prefix(rel: str, prefix: str | None) -> bool:
    if not prefix:
        return True
    return rel == prefix or rel.startswith(prefix + "/")


def resolve_scope(pin: str | None, path: str | None) -> str | None:
    """Combine a session pin with a caller path. Both are markdown-relative.

    If `path` is already under `pin`, it wins. Otherwise it is joined under `pin`.
    """
    pin_n = parse_folder_scope(pin)
    extra = parse_folder_scope(path)
    if extra is None:
        return pin_n
    if pin_n is None:
        return extra
    if path_under_prefix(extra, pin_n):
        return extra
    return normalize_relative_path(f"{pin_n}/{extra}")


def constrain_to_pin(rel: str, pin: str | None) -> str:
    """Resolve `rel` so it cannot escape `pin`. `rel` is already markdown-relative."""
    effective = resolve_scope(pin, rel)
    if not effective:
        raise PathError("document path is required")
    pin_n = parse_folder_scope(pin)
    if pin_n and not path_under_prefix(effective, pin_n):
        raise PathError("path is outside the pinned folder")
    return effective


def markdown_relpath_from_result(item: dict[str, Any]) -> str | None:
    for key in ("dochub_path", "path", "file", "filepath", "displayPath"):
        raw = item.get(key)
        if not isinstance(raw, str) or not raw.strip() or raw.strip().startswith("#"):
            continue
        try:
            rel = parse_markdown_relpath(raw)
        except PathError:
            continue
        if rel:
            return rel
    return None


def annotate_result(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    rel = markdown_relpath_from_result(out)
    if rel:
        out["dochub_path"] = rel
    return out


def result_under_prefix(item: dict[str, Any], prefix: str | None) -> bool:
    if not prefix:
        return True
    rel = item.get("dochub_path") if isinstance(item.get("dochub_path"), str) else None
    if not rel:
        rel = markdown_relpath_from_result(item)
    if not rel:
        return False
    return path_under_prefix(rel, prefix)


def normalize_search_payload(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        for key in ("results", "hits", "documents"):
            items = raw.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def filter_results(results: list[dict[str, Any]], prefix: str | None, limit: int) -> list[dict[str, Any]]:
    annotated = [annotate_result(item) for item in results]
    if prefix:
        annotated = [item for item in annotated if result_under_prefix(item, prefix)]
    if limit < 1:
        return []
    return annotated[:limit]


def overfetch_limit(limit: int, *, scoped: bool) -> int:
    if not scoped:
        return limit
    return min(max(limit * OVERFETCH_FACTOR, OVERFETCH_FLOOR), OVERFETCH_CAP)
