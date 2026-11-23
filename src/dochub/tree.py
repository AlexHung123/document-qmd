from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .paths import inbox_dir, markdown_dir, ensure_relative_dir, normalize_relative_path
from .queue import FileRow


def create_folder(user_root: Path, rel: str) -> str:
    """Create the same relative folder under inbox/ and markdown/."""
    rel = normalize_relative_path(rel)
    ensure_relative_dir(inbox_dir(user_root), rel)
    ensure_relative_dir(markdown_dir(user_root), rel)
    return rel


def build_inbox_tree(user_root: Path, files: list[FileRow], prefix: str | None = None) -> dict[str, Any]:
    """Nested directory tree from inbox/, with file rows attached by inbox_relpath."""
    inbox = inbox_dir(user_root)
    by_rel = {row.inbox_relpath: row for row in files}
    root_node: dict[str, Any] = {"type": "dir", "name": "", "path": "", "children": []}
    if not inbox.is_dir():
        return _subtree(root_node, prefix)

    nodes: dict[str, dict[str, Any]] = {"": root_node}
    inbox_res = inbox.resolve()
    for dirpath, dirnames, filenames in os.walk(inbox_res):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        filenames = sorted(name for name in filenames if not name.startswith("."))
        rel_dir = _rel_to(inbox_res, Path(dirpath))
        parent = _dir_node(nodes, rel_dir)
        for name in dirnames:
            child_rel = name if not rel_dir else f"{rel_dir}/{name}"
            child = _dir_node(nodes, child_rel)
            if child not in parent["children"]:
                parent["children"].append(child)
        for name in filenames:
            child_rel = name if not rel_dir else f"{rel_dir}/{name}"
            row = by_rel.get(child_rel)
            parent["children"].append(
                {
                    "type": "file",
                    "name": name,
                    "path": child_rel,
                    "file": _file_brief(row) if row else None,
                }
            )
        parent["children"].sort(key=lambda n: (0 if n["type"] == "dir" else 1, n["name"]))
    return _subtree(root_node, prefix)


def _rel_to(root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(root)
    text = str(rel).replace("\\", "/")
    return "" if text == "." else text


def _dir_node(nodes: dict[str, dict[str, Any]], rel: str) -> dict[str, Any]:
    if rel in nodes:
        return nodes[rel]
    name = rel.rsplit("/", 1)[-1] if rel else ""
    node = {"type": "dir", "name": name, "path": rel, "children": []}
    nodes[rel] = node
    if rel:
        parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
        parent = _dir_node(nodes, parent_rel)
        if node not in parent["children"]:
            parent["children"].append(node)
    return node


def _file_brief(row: FileRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "original_filename": row.original_filename,
        "inbox_relpath": row.inbox_relpath,
        "markdown_relpath": row.markdown_relpath,
        "size_bytes": row.size_bytes,
        "task_id": row.task_id,
        "created_at": row.created_at,
    }


def _subtree(root: dict[str, Any], prefix: str | None) -> dict[str, Any]:
    if not prefix:
        return root
    rel = normalize_relative_path(prefix)
    node = root
    for part in rel.split("/"):
        match = next((c for c in node.get("children", []) if c["type"] == "dir" and c["name"] == part), None)
        if match is None:
            raise FileNotFoundError(rel)
        node = match
    return node
