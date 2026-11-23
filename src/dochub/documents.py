from __future__ import annotations

import fnmatch
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .paths import PathError, markdown_dir, safe_join
from .scope import parse_document_ref, parse_markdown_relpath, path_under_prefix

DEFAULT_MAX_BYTES = 200 * 1024
MAX_MULTI_FILES = 50
SIMILAR_LIMIT = 5


def slice_lines(
    text: str,
    from_line: int | None,
    max_lines: int | None,
    line_numbers: bool,
    *,
    max_bytes: int | None = None,
) -> tuple[str, int, int | None]:
    lines = text.splitlines()
    start_idx = 0
    if from_line is not None:
        if from_line < 1:
            raise ValueError("from_line must be >= 1")
        start_idx = min(from_line - 1, len(lines))
    chunk = lines[start_idx:]
    if max_lines is not None:
        if max_lines < 1:
            raise ValueError("max_lines must be >= 1")
        chunk = chunk[:max_lines]
    start_no = start_idx + 1
    if max_bytes is not None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        kept: list[str] = []
        used = 0
        for i, line in enumerate(chunk):
            numbered = f"{start_no + i}: {line}" if line_numbers else line
            extra = len(numbered.encode("utf-8")) + (1 if kept else 0)
            if kept and used + extra > max_bytes:
                break
            if not kept and extra > max_bytes:
                # Always return at least the first line, truncated if needed.
                raw = numbered.encode("utf-8")[:max_bytes]
                kept.append(raw.decode("utf-8", "ignore"))
                used = len(raw)
                break
            kept.append(line if not line_numbers else numbered)
            used += extra
        rendered = "\n".join(kept)
        if text.endswith("\n") and kept and used < max_bytes:
            rendered += "\n"
        return rendered, start_no, max_lines

    if line_numbers:
        rendered = "\n".join(f"{i}: {line}" for i, line in enumerate(chunk, start=start_no))
    else:
        rendered = "\n".join(chunk)
    if text.endswith("\n") and chunk:
        rendered += "\n"
    return rendered, start_no, max_lines


def list_markdown_relpaths(user_root: Path, prefix: str | None = None) -> list[str]:
    root = markdown_dir(user_root)
    if not root.is_dir():
        return []
    root_res = root.resolve()
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_res):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            rel = str((Path(dirpath) / name).resolve().relative_to(root_res)).replace("\\", "/")
            if path_under_prefix(rel, prefix):
                out.append(rel)
    return out


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a glob to a regex. `**` matches zero or more path segments."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(path: str, pattern: str) -> bool:
    pat = pattern.replace("\\", "/").lstrip("/")
    if pat.startswith("markdown/"):
        pat = pat[len("markdown/") :]
    if fnmatch.fnmatch(path, pat):
        return True
    if "/" not in pat.rstrip("/") and fnmatch.fnmatch(path.rsplit("/", 1)[-1], pat):
        return True
    return glob_to_regex(pat).fullmatch(path) is not None


def expand_glob(user_root: Path, pattern: str, prefix: str | None = None) -> list[str]:
    return [rel for rel in list_markdown_relpaths(user_root, prefix) if glob_match(rel, pattern)]


def similar_paths(user_root: Path, target: str, *, prefix: str | None = None, limit: int = SIMILAR_LIMIT) -> list[str]:
    try:
        needle = parse_markdown_relpath(target) or target
    except PathError:
        needle = target.strip().lstrip("#")
    needle = needle.replace("\\", "/").lower()
    name = needle.rsplit("/", 1)[-1]
    scored: list[tuple[float, str]] = []
    for rel in list_markdown_relpaths(user_root, prefix):
        low = rel.lower()
        base = low.rsplit("/", 1)[-1]
        if low == needle or base == name:
            score = 1.0
        elif name and name in base:
            score = 0.85
        elif needle and needle in low:
            score = 0.8
        else:
            score = max(SequenceMatcher(None, name, base).ratio(), SequenceMatcher(None, needle, low).ratio())
        if score >= 0.45:
            scored.append((score, rel))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [rel for _, rel in scored[:limit]]


class DocumentNotFound(FileNotFoundError):
    def __init__(self, spec: str, similar: list[str] | None = None):
        super().__init__(spec)
        self.spec = spec
        self.similar = similar or []


def read_markdown(
    user_root: Path,
    spec: str,
    *,
    from_line: int | None = None,
    max_lines: int | None = None,
    line_numbers: bool = True,
    max_bytes: int | None = DEFAULT_MAX_BYTES,
    qmd=None,
) -> dict[str, Any]:
    rel, docid, suf_from, suf_count = parse_document_ref(spec)
    if docid:
        rel = _resolve_docid_path(user_root, docid, qmd)
        if not rel:
            raise DocumentNotFound(docid, similar_paths(user_root, docid))
    if not rel:
        raise PathError("document path is required")
    start = from_line if from_line is not None else suf_from
    count = max_lines if max_lines is not None else suf_count
    path = safe_join(markdown_dir(user_root), rel)
    if path.is_dir():
        raise PathError("path is a directory")
    if not path.is_file():
        raise DocumentNotFound(rel, similar_paths(user_root, rel))
    size = path.stat().st_size
    if max_bytes is not None and start is None and count is None and size > max_bytes:
        # Still return a sliced prefix rather than skipping the file entirely.
        pass
    text = path.read_text(encoding="utf-8")
    rendered, start_no, used_count = slice_lines(
        text,
        start,
        count,
        line_numbers,
        max_bytes=max_bytes,
    )
    return {
        "path": rel,
        "docid": docid,
        "from_line": start_no,
        "max_lines": used_count,
        "line_numbers": line_numbers,
        "line_count": len(text.splitlines()),
        "bytes": size,
        "text": rendered,
    }


def _resolve_docid_path(user_root: Path, docid: str, qmd) -> str | None:
    if qmd is None or not hasattr(qmd, "resolve_docid"):
        return None
    payload = qmd.resolve_docid(user_root, docid)
    if not isinstance(payload, dict):
        return None
    for key in ("dochub_path", "path", "file", "filepath", "displayPath"):
        raw = payload.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            rel = parse_markdown_relpath(raw.strip())
        except PathError:
            continue
        if rel:
            return rel
    return None
