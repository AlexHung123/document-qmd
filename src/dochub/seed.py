from __future__ import annotations

import mimetypes
import os
import shutil
from pathlib import Path
from typing import Any

from .layout import ensure_public_tree
from .library import PUBLIC_OWNER_ID, folder_is_wiki
from .log import log
from .paths import (
    PathError,
    inbox_dir,
    markdown_dir,
    markdown_relpath_for,
    normalize_relative_path,
    safe_join,
    sanitize_filename,
)
from .queue import ControlStore, FileRow
from .util import sha256_bytes

# Skip these names anywhere in a source tree when seeding.
_SKIP_DIR_NAMES = {".git", ".qmd", "__pycache__", "node_modules", ".provided"}


def seed_public_folder(
    data_root: Path,
    source: Path,
    folder: str,
    store: ControlStore,
    qmd,
    *,
    hidden: bool = False,
    run_index: bool = False,
) -> dict[str, Any]:
    """Copy a local document tree into the shared public library.

    Does not delete `source`. Files are written under public/inbox/<folder>/
    and markdown under public/markdown/<folder>/. Existing destinations are
    left in place (idempotent). Each new file is registered against the
    reserved public owner, not the operator's private user_id.
    """
    src = source.expanduser().resolve()
    if not src.is_dir():
        raise PathError(f"seed source is not a directory: {src}")
    folder_rel = normalize_relative_path(folder)
    root = ensure_public_tree(data_root, qmd)
    inbox = inbox_dir(root)
    md_root = markdown_dir(root)
    copied = 0
    skipped = 0
    registered: list[FileRow] = []

    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _SKIP_DIR_NAMES and not name.startswith(".")
        )
        filenames = sorted(name for name in filenames if not name.startswith("."))
        rel_dir = Path(dirpath).resolve().relative_to(src)
        rel_dir_text = "" if str(rel_dir) == "." else str(rel_dir).replace("\\", "/")
        for name in filenames:
            rel = f"{folder_rel}/{name}" if not rel_dir_text else f"{folder_rel}/{rel_dir_text}/{name}"
            rel = normalize_relative_path(rel)
            src_file = Path(dirpath) / name
            dest = safe_join(inbox, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            existed = dest.exists()
            if not existed:
                shutil.copy2(src_file, dest)
                copied += 1
            else:
                skipped += 1
            md_rel = None
            md_size = None
            if dest.suffix.lower() == ".md" or src_file.suffix.lower() == ".md":
                md_rel = markdown_relpath_for(rel)
                md_dest = safe_join(md_root, md_rel)
                md_dest.parent.mkdir(parents=True, exist_ok=True)
                if not md_dest.exists() or not existed:
                    shutil.copy2(src_file, md_dest)
                if md_dest.is_file():
                    md_size = md_dest.stat().st_size
            row = store.get_file_by_inbox(PUBLIC_OWNER_ID, rel)
            if row is None:
                data = dest.read_bytes()
                ctype, _ = mimetypes.guess_type(name)
                row = store.insert_registered_file(
                    user_id=PUBLIC_OWNER_ID,
                    original_filename=sanitize_filename(name),
                    content_type=ctype,
                    sha256=sha256_bytes(data),
                    size_bytes=len(data),
                    inbox_relpath=rel,
                    markdown_relpath=md_rel,
                    markdown_size_bytes=md_size,
                )
            elif md_size is not None and row.markdown_size_bytes is None:
                store.set_markdown_size_bytes(row.id, md_size)
                row = store.get_file(row.id) or row
            registered.append(row)

    indexed = False
    if run_index:
        qmd.update(root)
        qmd.embed(root)
        indexed = True

    log(
        "public folder seeded",
        folder=folder_rel,
        source=str(src),
        copied=copied,
        skipped=skipped,
        registered=len(registered),
        wiki=folder_is_wiki(root, folder_rel),
        hidden=hidden,
        indexed=indexed,
    )
    return {
        "folder": folder_rel,
        "library": "public",
        "source": str(src),
        "copied": copied,
        "skipped": skipped,
        "registered": len(registered),
        "wiki": folder_is_wiki(root, folder_rel),
        "hidden": hidden,
        "indexed": indexed,
    }
