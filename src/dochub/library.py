from __future__ import annotations

from pathlib import Path
from typing import Any

from .paths import PathError, markdown_dir, parse_user_id, user_root

LIBRARY_PRIVATE = "private"
LIBRARY_PUBLIC = "public"
LIBRARIES = (LIBRARY_PRIVATE, LIBRARY_PUBLIC)

# Reserved control-plane owner for the shared public library.
# This is never a users/<id>/ path and must not collide with a JWT user id.
PUBLIC_OWNER_ID = "__public__"

WIKI_INDEX = "wiki/index.md"


class ForbiddenError(RuntimeError):
    """Authenticated user is not allowed to write the public library."""


def parse_library(raw: Any) -> str:
    """Return 'private' or 'public'. Missing/blank defaults to private."""
    if raw is None:
        return LIBRARY_PRIVATE
    if not isinstance(raw, str):
        raise PathError("library must be a string")
    value = raw.strip().lower()
    if value in ("", LIBRARY_PRIVATE, "priv"):
        return LIBRARY_PRIVATE
    if value in (LIBRARY_PUBLIC, "shared"):
        return LIBRARY_PUBLIC
    raise PathError("library must be private or public")


def parse_curators(raw: str | None) -> tuple[str, ...]:
    """Comma-separated user ids allowed to write the public library.

    Empty means nobody can create or upload public folders (safe default).
    """
    if raw is None or not str(raw).strip():
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for part in str(raw).split(","):
        token = part.strip()
        if not token:
            continue
        uid = parse_user_id(token)
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    return tuple(out)


def public_root(data_root: Path) -> Path:
    return (data_root / "public").resolve()


def library_owner(library: str, user_id: str) -> str:
    if library == LIBRARY_PUBLIC:
        return PUBLIC_OWNER_ID
    return parse_user_id(user_id)


def is_public_owner(owner_id: str | None) -> bool:
    return owner_id == PUBLIC_OWNER_ID


def can_write_public(curators: tuple[str, ...] | list[str], user_id: str) -> bool:
    try:
        uid = parse_user_id(user_id)
    except PathError:
        return False
    return uid in set(curators)


def library_root(data_root: Path, library: str, user_id: str) -> Path:
    if library == LIBRARY_PUBLIC:
        return public_root(data_root)
    return user_root(data_root, user_id)


def wiki_index_path(root: Path, folder: str = "") -> Path:
    base = markdown_dir(root)
    if folder:
        return base / folder / "wiki" / "index.md"
    return base / "wiki" / "index.md"


def folder_is_wiki(root: Path, folder: str = "") -> bool:
    """A folder is a wiki library when it contains wiki/index.md."""
    path = wiki_index_path(root, folder)
    return path.is_file()


def annotate_wiki_flags(node: dict[str, Any], root: Path) -> dict[str, Any]:
    """Mark directory nodes that contain wiki/index.md (for UI badges)."""
    if node.get("type") == "dir":
        rel = node.get("path") if isinstance(node.get("path"), str) else ""
        node["wiki"] = folder_is_wiki(root, rel or "")
        for child in node.get("children") or []:
            if isinstance(child, dict):
                annotate_wiki_flags(child, root)
    return node
