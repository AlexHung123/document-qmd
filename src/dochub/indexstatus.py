from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .documents import list_markdown_relpaths
from .paths import qmd_dir as _qmd_dir
from .queue import ControlStore, TaskRow

SEMANTIC_HINT = (
    "Embeddings are not ready. Use keyword search (mode=search or lex-only). "
    "Do not use vsearch, vec, hyde, or mode=query until embed finishes."
)


def read_index_yml_contexts(user_root: Path) -> list[dict[str, str]]:
    """Read collection context already stored in the user's .qmd/index.yml."""
    yml = _qmd_dir(user_root) / "index.yml"
    if not yml.is_file():
        return []
    try:
        text = yml.read_text(encoding="utf-8")
    except OSError:
        return []
    contexts: list[dict[str, str]] = []
    in_context = False
    for raw in text.splitlines():
        if re.match(r"^    context:\s*$", raw):
            in_context = True
            continue
        if in_context:
            match = re.match(r'^      ("(?:\\.|[^"])+"|[^:]+):\s*(.*)$', raw)
            if match:
                path = match.group(1).strip()
                if path.startswith('"') and path.endswith('"'):
                    try:
                        path = bytes(path, "utf-8").decode("unicode_escape")
                        path = path[1:-1]
                    except Exception:
                        path = path.strip('"')
                value = match.group(2).strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                contexts.append({"path": path, "text": value})
                continue
            if raw.startswith("    ") and not raw.startswith("      "):
                in_context = False
            elif raw and not raw.startswith(" "):
                in_context = False
    return contexts


def _normalize_contexts(raw: Any) -> list[dict[str, str]]:
    items: list[Any]
    if raw is None:
        return []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        if all(isinstance(v, str) for v in raw.values()):
            return [{"path": str(k), "text": v} for k, v in raw.items()]
        for key in ("contexts", "items", "results"):
            value = raw.get(key)
            if isinstance(value, list):
                items = value
                break
        else:
            items = [raw]
    else:
        return []
    out: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            path = item.get("path") or item.get("uri") or item.get("file") or item.get("collection") or ""
            text = item.get("text") or item.get("context") or item.get("description") or ""
            if path or text:
                out.append({"path": str(path), "text": str(text)})
        elif isinstance(item, str) and item.strip():
            out.append({"path": "", "text": item.strip()})
    return out


def merge_contexts(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for group in groups:
        for item in group:
            key = (item.get("path") or "", item.get("text") or "")
            if key in seen or not key[1]:
                continue
            seen.add(key)
            out.append({"path": key[0], "text": key[1]})
    return out


def _task_summary(tasks: list[TaskRow]) -> dict[str, Any]:
    update_pending = 0
    embed_pending = 0
    failed = 0
    succeeded = 0
    last_succeeded_at = None
    for task in tasks:
        if task.status in {"pending", "running"}:
            if task.stage == "qmd_update":
                update_pending += 1
            else:
                embed_pending += 1
        elif task.status == "failed":
            failed += 1
        elif task.status == "succeeded":
            succeeded += 1
            if task.finished_at and (last_succeeded_at is None or task.finished_at > last_succeeded_at):
                last_succeeded_at = task.finished_at
    keyword_ready = update_pending == 0
    # Current queue only: a historical failed ingest must not flip the library
    # to unready after later work has finished (or qmd reports vectors ready).
    queue_idle = update_pending == 0 and embed_pending == 0
    return {
        "update_pending": update_pending,
        "embed_pending": embed_pending,
        "failed": failed,
        "succeeded": succeeded,
        "last_succeeded_at": last_succeeded_at,
        "keyword_search_ready": keyword_ready,
        "queue_idle": queue_idle,
    }


def _first_present(obj: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in obj and obj[key] is not None:
            return obj[key]
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return None


def _unwrap_qmd_status(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    status_keys = (
        "needsEmbedding",
        "needs_embedding",
        "pendingEmbeddings",
        "pending_embeddings",
        "hasVectorIndex",
        "has_vector_index",
        "totalDocuments",
        "total_documents",
        "embeddings",
        "vectors",
    )
    current = raw
    for key in ("status", "index", "qmd"):
        inner = current.get(key)
        if isinstance(inner, dict) and any(item in inner for item in status_keys):
            current = inner
            break
    return current


def qmd_vector_progress(qmd_status: Any) -> dict[str, Any]:
    """Read vector/embed progress from official `qmd status` JSON.

    Canonical fields are ``needsEmbedding``, ``hasVectorIndex``, and
    ``totalDocuments``. Snake_case and a few nested aliases are accepted so
    older or wrapped payloads still work.
    """
    payload = _unwrap_qmd_status(qmd_status)
    if payload is None:
        return {
            "conclusive": False,
            "needs_embedding": None,
            "has_vector_index": None,
            "total_documents": None,
        }

    needs = _as_int(
        _first_present(
            payload,
            "needsEmbedding",
            "needs_embedding",
            "pendingEmbeddings",
            "pending_embeddings",
        )
    )
    has_vec = _as_bool(
        _first_present(
            payload,
            "hasVectorIndex",
            "has_vector_index",
            "vectorIndex",
            "vector_index",
        )
    )
    total_docs = _as_int(
        _first_present(
            payload,
            "totalDocuments",
            "total_documents",
            "totalDocs",
            "total_docs",
        )
    )

    for nest_key in ("embeddings", "embedding", "vectors", "vector"):
        nest = payload.get(nest_key)
        if not isinstance(nest, dict):
            continue
        if needs is None:
            needs = _as_int(
                _first_present(nest, "pending", "needsEmbedding", "needs_embedding", "remaining")
            )
        if has_vec is None:
            has_vec = _as_bool(_first_present(nest, "ready", "hasIndex", "has_index", "present"))

    collections = payload.get("collections")
    if needs is None and isinstance(collections, list):
        collected: list[int] = []
        for item in collections:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("collection") or "")
            col_needs = _as_int(
                _first_present(
                    item,
                    "needsEmbedding",
                    "needs_embedding",
                    "pendingEmbeddings",
                    "pending_embeddings",
                    "pending",
                )
            )
            if col_needs is None:
                continue
            if name in {"", "markdown"} or name.endswith("/markdown"):
                collected.append(col_needs)
        if collected:
            needs = sum(collected)

    return {
        "conclusive": needs is not None or has_vec is not None,
        "needs_embedding": needs,
        "has_vector_index": has_vec,
        "total_documents": total_docs,
    }


def qmd_semantic_ready(qmd_status: Any) -> bool | None:
    """True/False when qmd status clearly reports embed progress, else None."""
    progress = qmd_vector_progress(qmd_status)
    if not progress["conclusive"]:
        return None
    needs = progress["needs_embedding"]
    has_vec = progress["has_vector_index"]
    total = progress["total_documents"]
    if needs is not None and needs > 0:
        return False
    if has_vec is False:
        # Official empty index: 0 documents, 0 pending, no vector table yet.
        if total == 0 or (total is None and needs == 0):
            return True if total == 0 else None
        return False
    if has_vec is True and (needs is None or needs == 0):
        return True
    if needs == 0:
        return True
    return None


def semantic_search_ready(
    *,
    empty_library: bool,
    update_pending: int,
    embed_pending: int,
    qmd_status: Any,
) -> bool:
    """Ready when current ingest is idle and qmd does not report pending vectors.

    Historical failed tasks are ignored here. If qmd status is missing or
    inconclusive, an idle queue is treated as ready so a permanent failure
    cannot pin the library to keyword-only after later embeds succeed.
    """
    if empty_library:
        return True
    if update_pending > 0 or embed_pending > 0:
        return False
    ready = qmd_semantic_ready(qmd_status)
    if ready is False:
        return False
    return True


def build_index_status(
    store: ControlStore,
    user_id: str,
    user_root: Path,
    qmd,
) -> dict[str, Any]:
    queue = store.counts(user_id)
    tasks = store.list_tasks(user_id=user_id, limit=10000)
    files = store.list_files(user_id, limit=10000)
    summary = _task_summary(tasks)
    markdown_count = len(list_markdown_relpaths(user_root))
    empty_library = not files and markdown_count == 0
    if empty_library:
        summary["keyword_search_ready"] = True

    qmd_status = None
    qmd_contexts: list[dict[str, str]] = []
    if hasattr(qmd, "status"):
        try:
            qmd_status = qmd.status(user_root)
        except Exception:
            qmd_status = None
    if hasattr(qmd, "context_list"):
        try:
            qmd_contexts = _normalize_contexts(qmd.context_list(user_root))
        except Exception:
            qmd_contexts = []
    yml_contexts = read_index_yml_contexts(user_root)
    contexts = merge_contexts(yml_contexts, qmd_contexts)

    embedding_model = None
    collections = None
    if isinstance(qmd_status, dict):
        models = qmd_status.get("models")
        embedding_model = qmd_status.get("embeddingModel") or qmd_status.get("embedding_model")
        if embedding_model is None and isinstance(models, dict):
            embedding_model = models.get("embedding") or models.get("embed")
        collections = qmd_status.get("collections")

    semantic = semantic_search_ready(
        empty_library=empty_library,
        update_pending=int(summary["update_pending"]),
        embed_pending=int(summary["embed_pending"]),
        qmd_status=qmd_status,
    )
    keyword = bool(summary["keyword_search_ready"])
    return {
        "keyword_search_ready": keyword,
        "semantic_search_ready": semantic,
        "hint": None if semantic else SEMANTIC_HINT,
        "queue": queue,
        "ingest": {
            "files": len(files),
            "markdown_files": markdown_count,
            "update_pending": summary["update_pending"],
            "embed_pending": summary["embed_pending"],
            "failed": summary["failed"],
            "succeeded": summary["succeeded"],
            "last_succeeded_at": summary["last_succeeded_at"],
        },
        "embedding_model": embedding_model,
        "collections": collections,
        "contexts": contexts,
        "qmd": qmd_status if qmd_status is not None else {"available": bool(getattr(qmd, "available", lambda: False)())},
    }
