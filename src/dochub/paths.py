from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from .util import utcnow

USER_ID_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,128}$")
CONTROL_CHARS = set(range(32))
# Reserved control-plane owner for the shared public library. Never a users/<id>/.
PUBLIC_OWNER_ID = "__public__"
RESERVED_USER_IDS = frozenset({".", "..", PUBLIC_OWNER_ID})


class PathError(ValueError):
    """Rejected user, filename, or relative path."""


def parse_user_id(raw: str) -> str:
    if not isinstance(raw, str) or not USER_ID_RE.fullmatch(raw):
        raise PathError("invalid user_id")
    if raw in RESERVED_USER_IDS:
        raise PathError("invalid user_id")
    return raw


def user_root(data_root: Path, user_id: str) -> Path:
    uid = parse_user_id(user_id)
    data = data_root.resolve()
    users = (data / "users").resolve()
    root = (users / uid).resolve()
    if root.parent != users or root.name != uid:
        raise PathError("invalid user_id")
    return root


def inbox_dir(root: Path) -> Path:
    return root / "inbox"


def markdown_dir(root: Path) -> Path:
    return root / "markdown"


def qmd_dir(root: Path) -> Path:
    return root / ".qmd"


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name.replace("\\", "/"))
    name = Path(name).name
    out: list[str] = []
    for ch in name:
        if ord(ch) < 32:
            out.append("_")
        elif ch.isalnum() or ch in "._- +":
            out.append(ch)
        else:
            out.append("_")
    cleaned = "".join(out).strip(" .")
    while cleaned.startswith("."):
        cleaned = cleaned[1:].lstrip(" .")
    if not cleaned:
        cleaned = "file"
    return cleaned[:180]


def normalize_relative_path(rel: str) -> str:
    if not isinstance(rel, str) or "\x00" in rel:
        raise PathError("invalid relative_path")
    rel = unicodedata.normalize("NFC", rel.replace("\\", "/")).strip()
    if not rel or rel.startswith("/"):
        raise PathError("invalid relative_path")
    parts: list[str] = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or part.startswith("."):
            raise PathError("invalid relative_path")
        if any(ord(ch) < 32 for ch in part):
            raise PathError("invalid relative_path")
        if len(part) > 180:
            raise PathError("invalid relative_path")
        parts.append(part)
    if not parts or len(parts) > 12:
        raise PathError("invalid relative_path")
    return "/".join(parts)


def safe_join(root: Path, rel: str) -> Path:
    rel = normalize_relative_path(rel)
    root_res = root.resolve()
    candidate = (root_res / rel).resolve()
    if not _is_within(root_res, candidate):
        raise PathError("path escapes root")
    return candidate


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def default_inbox_relpath(filename: str, now: datetime | None = None) -> str:
    stamp = now or utcnow()
    return f"{stamp:%Y}/{stamp:%m}/{sanitize_filename(filename)}"


def unique_relpath(root: Path, rel: str, file_id: str) -> str:
    rel = normalize_relative_path(rel)
    if not safe_join(root, rel).exists():
        return rel
    path = Path(rel)
    suffix = path.suffix
    stem = path.stem
    parent = str(path.parent).replace("\\", "/")
    extra = file_id.replace("-", "")[:8]
    n = 0
    while n < 1000:
        name = f"{stem}-{extra}{suffix}" if n == 0 else f"{stem}-{extra}-{n}{suffix}"
        candidate = name if parent in ("", ".") else f"{parent}/{name}"
        if not safe_join(root, candidate).exists():
            return candidate
        n += 1
    raise PathError("could not allocate a unique relative_path")


def markdown_relpath_for(inbox_relpath: str) -> str:
    rel = normalize_relative_path(inbox_relpath)
    return str(Path(rel).with_suffix(".md")).replace("\\", "/")


def ensure_within_user(user_dir: Path, path: Path) -> Path:
    root = user_dir.resolve()
    real = path.resolve()
    if not _is_within(root, real):
        raise PathError("path escapes user root")
    return real


def chmod_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def ensure_relative_dir(root: Path, rel: str) -> Path:
    """Create `root/rel` and each parent as a private directory."""
    rel = normalize_relative_path(rel)
    root_res = root.resolve()
    chmod_private_dir(root_res)
    current = root_res
    for part in rel.split("/"):
        current = current / part
        if current.exists() and not current.is_dir():
            raise PathError("path exists as a file")
        if not _is_within(root_res, current.resolve() if current.exists() else current):
            raise PathError("path escapes root")
        current.mkdir(exist_ok=True)
        os.chmod(current, 0o700)
    return current.resolve()
