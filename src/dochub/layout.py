from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .library import public_root
from .paths import (
    chmod_private_dir,
    inbox_dir,
    markdown_dir,
    parse_user_id,
    qmd_dir,
    user_root,
)
from .util import atomic_write_text

COLLECTIONS_BLOCK = """collections:
  markdown:
    path: {path}
    pattern: "**/*.md"
    context:
      "/": User markdown documents
"""

PATH_RE = re.compile(
    r"(collections:\n  markdown:\n    path: ).*",
    re.MULTILINE,
)


class LayoutError(RuntimeError):
    pass


def ensure_user_tree(data_root: Path, user_id: str, qmd) -> Path:
    """Create the fixed per-user root and a project-local QMD index.

    Layout:
      users/<id>/inbox/      original uploads
      users/<id>/markdown/   uploaded .md (the only tree QMD indexes)
      users/<id>/.qmd/       this user's index.yml + index.sqlite
    """
    uid = parse_user_id(user_id)
    root = user_root(data_root, uid)
    chmod_private_dir(root)
    chmod_private_dir(inbox_dir(root))
    chmod_private_dir(markdown_dir(root))
    chmod_private_dir(qmd_dir(root))
    _ensure_index_yml(qmd_dir(root) / "index.yml", markdown_dir(root))
    qmd.init_index(root)
    sqlite = qmd_dir(root) / "index.sqlite"
    if not sqlite.exists():
        raise LayoutError(f"qmd init did not create {sqlite}")
    return root


def ensure_public_tree(data_root: Path, qmd) -> Path:
    """Create the shared public library root and its own QMD index.

    Layout (sibling of users/, never mixed into a user tree):
      public/inbox/      original shared uploads
      public/markdown/   shared .md (the only tree this index covers)
      public/.qmd/       public index.yml + index.sqlite
    """
    root = public_root(data_root)
    chmod_private_dir(root)
    chmod_private_dir(inbox_dir(root))
    chmod_private_dir(markdown_dir(root))
    chmod_private_dir(qmd_dir(root))
    _ensure_index_yml(qmd_dir(root) / "index.yml", markdown_dir(root))
    qmd.init_index(root)
    sqlite = qmd_dir(root) / "index.sqlite"
    if not sqlite.exists():
        raise LayoutError(f"qmd init did not create {sqlite}")
    return root


def _ensure_index_yml(yml: Path, markdown_path: Path) -> None:
    quoted = json.dumps(str(markdown_path))
    if not yml.exists():
        atomic_write_text(yml, COLLECTIONS_BLOCK.format(path=quoted), mode=0o600)
        return
    text = yml.read_text(encoding="utf-8")
    if re.search(r"^collections:\s*\{\s*\}\s*$", text, re.MULTILINE):
        text = re.sub(r"^collections:\s*\{\s*\}\s*$", COLLECTIONS_BLOCK.format(path=quoted).rstrip(), text, count=1, flags=re.MULTILINE)
        atomic_write_text(yml, text if text.endswith("\n") else text + "\n", mode=0o600)
        return
    match = PATH_RE.search(text)
    if not match:
        # Keep models / other keys; put our collection on top.
        text = COLLECTIONS_BLOCK.format(path=quoted) + "\n" + text.lstrip()
        atomic_write_text(yml, text, mode=0o600)
        return
    updated, n = PATH_RE.subn(lambda m: m.group(1) + quoted, text, count=1)
    if n and updated != text:
        atomic_write_text(yml, updated, mode=0o600)


def list_user_ids(data_root: Path) -> list[str]:
    users = data_root / "users"
    if not users.is_dir():
        return []
    names = []
    for entry in sorted(users.iterdir()):
        if entry.is_dir() and not entry.name.startswith("."):
            names.append(entry.name)
    return names


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".provided"]
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total
