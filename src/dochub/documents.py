from __future__ import annotations

from pathlib import Path
from typing import Any

from .paths import PathError, markdown_dir, safe_join
from .scope import parse_document_path


def slice_lines(
    text: str,
    from_line: int | None,
    max_lines: int | None,
    line_numbers: bool,
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
    if line_numbers:
        rendered = "\n".join(f"{i}: {line}" for i, line in enumerate(chunk, start=start_no))
    else:
        rendered = "\n".join(chunk)
    if text.endswith("\n") and chunk:
        rendered += "\n"
    return rendered, start_no, max_lines


def read_markdown(
    user_root: Path,
    spec: str,
    *,
    from_line: int | None = None,
    max_lines: int | None = None,
    line_numbers: bool = True,
) -> dict[str, Any]:
    rel, suf_from, suf_count = parse_document_path(spec)
    start = from_line if from_line is not None else suf_from
    count = max_lines if max_lines is not None else suf_count
    path = safe_join(markdown_dir(user_root), rel)
    if path.is_dir():
        raise PathError("path is a directory")
    if not path.is_file():
        raise FileNotFoundError(rel)
    text = path.read_text(encoding="utf-8")
    rendered, start_no, used_count = slice_lines(text, start, count, line_numbers)
    return {
        "path": rel,
        "from_line": start_no,
        "max_lines": used_count,
        "line_numbers": line_numbers,
        "line_count": len(text.splitlines()),
        "text": rendered,
    }
