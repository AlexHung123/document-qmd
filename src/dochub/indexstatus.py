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
    semantic_ready = update_pending == 0 and embed_pending == 0 and failed == 0
    return {
        "update_pending": update_pending,
        "embed_pending": embed_pending,
        "failed": failed,
        "succeeded": succeeded,
        "last_succeeded_at": last_succeeded_at,
        "keyword_search_ready": keyword_ready,
        "semantic_search_ready": semantic_ready,
    }


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
    if not files and markdown_count == 0:
        summary["keyword_search_ready"] = True
        summary["semantic_search_ready"] = True

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

    semantic = bool(summary["semantic_search_ready"])
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
