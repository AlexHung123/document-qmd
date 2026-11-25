from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .config import Settings
from .convert import merge_front_matter
from .layout import ensure_public_tree, ensure_user_tree
from .library import (
    LIBRARY_PRIVATE,
    LIBRARY_PUBLIC,
    PUBLIC_OWNER_ID,
    ForbiddenError,
    annotate_wiki_flags,
    can_write_public,
    is_public_owner,
    parse_library,
)
from .log import log
from .multipart import MultipartError, parse_multipart
from .paths import (
    PathError,
    default_inbox_relpath,
    inbox_dir,
    markdown_dir,
    markdown_relpath_for,
    normalize_relative_path,
    parse_user_id,
    safe_join,
    unique_relpath,
)
from .documents import (
    DEFAULT_MAX_BYTES,
    MAX_MULTI_FILES,
    DocumentNotFound,
    expand_glob,
    read_markdown,
    similar_paths,
)
from .indexstatus import build_index_status
from .qmd import QmdError, QmdRunner
from .queue import ControlStore, DuplicateInboxPath, FileRow, TaskRow
from .scope import (
    filter_results,
    normalize_search_payload,
    overfetch_limit,
    parse_document_ref,
    parse_folder_scope,
    path_under_prefix,
)
from .searchreq import parse_search_body
from .searchrun import HybridLimiter, search_with_fallback
from .tree import build_inbox_tree, create_folder
from .util import atomic_write_bytes, atomic_write_text, new_id, sha256_bytes, utcnow_iso


MAX_JSON_BODY = 2 * 1024 * 1024


class BodyTooLarge(Exception):
    """Request Content-Length exceeds the configured body cap."""


_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*([^']*)''([^;]+)", re.I)
_FILENAME_QUOTED_RE = re.compile(r'filename\s*=\s*"((?:\\.|[^"\\])*)"', re.I)
_FILENAME_TOKEN_RE = re.compile(r"filename\s*=\s*([^;]+)", re.I)


def _header_basename(filename: str) -> str:
    name = str(filename).replace("\\", "/").split("/")[-1]
    name = name.replace("\r", "").replace("\n", "").strip()
    return name or "download"


def _ascii_fallback_filename(name: str) -> str:
    path = Path(name)
    stem = path.stem or name
    suffix = path.suffix

    def clean(part: str, empty: str) -> str:
        chars: list[str] = []
        for ch in part:
            if ch.isascii() and ch not in '"\\/' and 32 <= ord(ch) < 127:
                chars.append(ch)
            else:
                chars.append("_")
        cleaned = re.sub(r"_+", "_", "".join(chars)).strip(" ._")
        return cleaned or empty

    fallback = clean(stem, "download")
    if suffix:
        ext = clean(suffix.lstrip("."), "")
        if ext:
            fallback += "." + ext
    return fallback or "download"


def content_disposition(filename: str) -> str:
    name = _header_basename(filename)
    fallback = _ascii_fallback_filename(name)
    escaped = fallback.replace("\\", "\\\\").replace('"', '\\"')
    value = f'attachment; filename="{escaped}"'
    if name != fallback:
        value += f"; filename*=UTF-8''{quote(name, safe='')}"
    return value


def _filename_from_disposition(value: str) -> str:
    match = _FILENAME_STAR_RE.search(value)
    if match:
        return unquote(match.group(2).strip())
    match = _FILENAME_QUOTED_RE.search(value)
    if match:
        return match.group(1).replace('\\"', '"').replace("\\\\", "\\")
    match = _FILENAME_TOKEN_RE.search(value)
    if match:
        return match.group(1).strip().strip('"')
    return value


def latin1_header_value(key: str, value: str) -> str:
    cleaned = str(value).replace("\r", "").replace("\n", "")
    try:
        cleaned.encode("latin-1")
        return cleaned
    except UnicodeEncodeError:
        if key.lower() == "content-disposition":
            return content_disposition(_filename_from_disposition(value))
        return cleaned.encode("latin-1", "replace").decode("latin-1")


def user_id_from_headers(headers: dict[str, str]) -> str:
    raw = headers.get("X-User-Id") or headers.get("x-user-id")
    if not raw or not str(raw).strip():
        raise ValueError("X-User-Id header is required")
    return parse_user_id(str(raw).strip())


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    @classmethod
    def json(cls, status: int, obj: Any) -> Response:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return cls(status, data, {"Content-Type": "application/json; charset=utf-8"})

    @classmethod
    def error(cls, status: int, code: str, message: str) -> Response:
        return cls.json(status, {"error": code, "message": message})

    @classmethod
    def bytes(cls, status: int, data: bytes, content_type: str, filename: str | None = None) -> Response:
        headers = {"Content-Type": content_type}
        if filename:
            headers["Content-Disposition"] = content_disposition(filename)
        return cls(status, data, headers)


class App:
    def __init__(self, settings: Settings, store: ControlStore, qmd: QmdRunner):
        self.settings = settings
        self.store = store
        self.qmd = qmd
        self.hybrid_limiter = HybridLimiter()

    def _can_write_public(self, user_id: str) -> bool:
        return can_write_public(self.settings.public_curators, user_id)

    def _open_library(self, user_id: str, library: str, *, write: bool) -> tuple[Path, str]:
        """Return (tree root, control-plane owner id) for a library.

        Public writes require the caller to be in DOCHUB_PUBLIC_CURATORS.
        Public files are owned by PUBLIC_OWNER_ID, never the uploader's private id.
        """
        if library == LIBRARY_PUBLIC:
            if write and not self._can_write_public(user_id):
                raise ForbiddenError("public library is read-only for this user")
            return ensure_public_tree(self.settings.data_root, self.qmd), PUBLIC_OWNER_ID
        return ensure_user_tree(self.settings.data_root, user_id, self.qmd), user_id

    def _library_from_query(self, query: dict[str, list[str]]) -> str:
        raw = (query.get("library") or query.get("visibility") or [None])[0]
        return parse_library(raw)

    def _library_from_obj(self, obj: dict[str, Any]) -> str:
        return parse_library(obj.get("library") or obj.get("visibility"))

    def _file_visible(self, row: FileRow, user_id: str) -> bool:
        return row.user_id == user_id or is_public_owner(row.user_id)

    def _root_for_owner(self, owner_id: str) -> Path:
        if is_public_owner(owner_id):
            return ensure_public_tree(self.settings.data_root, self.qmd)
        return ensure_user_tree(self.settings.data_root, owner_id, self.qmd)

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> Response:
        parsed = urlparse(path)
        route = parsed.path
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and route == "/health":
                return Response.json(200, {"ok": True, "service": "dochub"})
            if method == "GET" and route == "/v1/health":
                return self._health(headers)
            user_id = user_id_from_headers(headers)
            if method == "GET" and route == "/v1/me":
                return Response.json(
                    200,
                    {
                        "user_id": user_id,
                        "can_write_public": can_write_public(self.settings.public_curators, user_id),
                        "libraries": [LIBRARY_PRIVATE, LIBRARY_PUBLIC],
                    },
                )
            if method == "GET" and route == "/v1/status":
                return self._status(user_id, query)
            if method == "GET" and route == "/v1/context":
                return self._context(user_id, query)
            if method == "POST" and route == "/v1/files":
                return self._upload(user_id, headers, body)
            if method == "POST" and route == "/v1/folders":
                return self._create_folder(user_id, body)
            if method == "GET" and route == "/v1/tree":
                return self._tree(user_id, query)
            if method == "GET" and route == "/v1/files":
                return self._list_files(user_id, query)
            if method == "GET" and route.startswith("/v1/files/"):
                return self._file_routes(user_id, route, query)
            if method == "POST" and route == "/v1/search":
                return self._search(user_id, body)
            if method == "GET" and route == "/v1/documents":
                return self._get_document(user_id, query)
            if method == "POST" and route == "/v1/documents/multi":
                return self._multi_get(user_id, body)
            if method == "GET" and route == "/v1/tasks":
                return self._list_tasks(user_id, query)
            if method == "GET" and route.startswith("/v1/tasks/"):
                return self._get_task(user_id, route.rsplit("/", 1)[-1])
            return Response.error(404, "not_found", "unknown route")
        except ForbiddenError as exc:
            return Response.error(403, "forbidden", str(exc))
        except PathError as exc:
            return Response.error(400, "bad_path", str(exc))
        except MultipartError as exc:
            return Response.error(400, "bad_multipart", str(exc))
        except ValueError as exc:
            return Response.error(400, "bad_request", str(exc))
        except QmdError as exc:
            return Response.error(502, "qmd_error", str(exc))
        except Exception as exc:
            log("unhandled error", error=str(exc), path=route)
            return Response.error(500, "internal", "internal error")

    def _health(self, headers: dict[str, str]) -> Response:
        body: dict[str, Any] = {"ok": True, "service": "dochub", "qmd": self.qmd.available()}
        raw = headers.get("X-User-Id") or headers.get("x-user-id")
        if raw and raw.strip():
            user_id = parse_user_id(raw.strip())
            body["user_id"] = user_id
            body["queue"] = self.store.counts(user_id)
            try:
                body["index"] = self._index_status(user_id)
            except Exception as exc:
                log("index status failed", error=str(exc), user_id=user_id)
        return Response.json(200, body)

    def _index_status(self, user_id: str, library: str = LIBRARY_PRIVATE) -> dict[str, Any]:
        root, owner = self._open_library(user_id, library, write=False)
        return build_index_status(self.store, owner, root, self.qmd)

    def _status(self, user_id: str, query: dict[str, list[str]] | None = None) -> Response:
        library = self._library_from_query(query or {})
        index = self._index_status(user_id, library)
        body = {
            "ok": True,
            "service": "dochub",
            "user_id": user_id,
            "library": library,
            "qmd": self.qmd.available(),
            "index": index,
            "keyword_search_ready": index.get("keyword_search_ready"),
            "semantic_search_ready": index.get("semantic_search_ready"),
            "hint": index.get("hint"),
            "queue": index.get("queue"),
            "contexts": index.get("contexts") or [],
        }
        return Response.json(200, body)

    def _context(self, user_id: str, query: dict[str, list[str]] | None = None) -> Response:
        library = self._library_from_query(query or {})
        index = self._index_status(user_id, library)
        return Response.json(
            200,
            {"user_id": user_id, "library": library, "contexts": index.get("contexts") or []},
        )

    def _upload(self, user_id: str, headers: dict[str, str], body: bytes) -> Response:
        content_type = headers.get("Content-Type") or headers.get("content-type") or ""
        filename, file_bytes, markdown_text, relative_path, declared_type, library_raw = self._parse_upload(content_type, body)
        library = parse_library(library_raw)
        if not file_bytes:
            return Response.error(400, "empty_file", "upload is empty")
        if not markdown_text or not markdown_text.strip():
            return Response.error(400, "markdown_required", "markdown is required")
        if len(file_bytes) > self.settings.max_upload_bytes:
            return Response.error(413, "too_large", "file exceeds max upload size")
        root, owner = self._open_library(user_id, library, write=True)
        inbox = inbox_dir(root)
        md_root = markdown_dir(root)
        if relative_path:
            rel = normalize_relative_path(relative_path)
        else:
            rel = default_inbox_relpath(filename)
        file_id = new_id()
        dest_probe = safe_join(inbox, rel)
        if dest_probe.exists() and dest_probe.is_dir():
            return Response.error(409, "is_directory", "relative_path is an existing folder")
        rel = unique_relpath(inbox, rel, file_id)
        md_rel = unique_relpath(md_root, markdown_relpath_for(rel), file_id)
        md_body = merge_front_matter(
            markdown_text,
            {
                "dochub_file_id": file_id,
                "dochub_source": rel,
                "dochub_original_filename": filename,
                "dochub_content_type": declared_type or "",
                "dochub_uploaded_at": utcnow_iso(),
            },
        )
        payload = {
            "inbox_relpath": rel,
            "markdown_relpath": md_rel,
            "original_filename": filename,
            "content_type": declared_type,
        }
        try:
            file_row, task_row = self.store.insert_upload(
                user_id=owner,
                original_filename=filename,
                content_type=declared_type,
                sha256=sha256_bytes(file_bytes),
                size_bytes=len(file_bytes),
                inbox_relpath=rel,
                markdown_relpath=md_rel,
                max_attempts=self.settings.max_attempts,
                payload=payload,
                file_id=file_id,
            )
        except DuplicateInboxPath:
            return Response.error(409, "conflict", "file already exists at this relative_path")
        try:
            atomic_write_bytes(safe_join(inbox, rel), file_bytes)
            atomic_write_text(safe_join(md_root, md_rel), md_body)
        except Exception:
            self.store.delete_upload(file_id)
            raise
        log(
            "upload stored",
            user_id=user_id,
            file_id=file_row.id,
            task_id=task_row.id,
            inbox=rel,
            markdown=md_rel,
        )
        return Response.json(
            202,
            {
                "file": file_public(file_row),
                "task": task_public(task_row),
            },
        )

    def _parse_upload(
        self, content_type: str, body: bytes
    ) -> tuple[str, bytes, str | None, str | None, str | None, str | None]:
        lowered = content_type.lower()
        if lowered.startswith("multipart/form-data"):
            parts = parse_multipart(content_type, body)
            file_part = parts.get("file")
            if file_part is None or not file_part.data:
                raise ValueError("multipart field 'file' is required")
            filename = file_part.filename or "file"
            markdown_text = None
            md_part = parts.get("markdown")
            if md_part is not None and md_part.data:
                markdown_text = md_part.data.decode("utf-8")
            relative = None
            if "relative_path" in parts and parts["relative_path"].data:
                relative = parts["relative_path"].data.decode("utf-8").strip() or None
            declared = file_part.content_type
            if "content_type" in parts and parts["content_type"].data:
                declared = parts["content_type"].data.decode("utf-8").strip() or declared
            library_raw = None
            if "library" in parts and parts["library"].data:
                library_raw = parts["library"].data.decode("utf-8").strip() or None
            elif "visibility" in parts and parts["visibility"].data:
                library_raw = parts["visibility"].data.decode("utf-8").strip() or None
            return filename, file_part.data, markdown_text, relative, declared, library_raw
        if lowered.startswith("application/json"):
            try:
                obj = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid JSON body") from exc
            if not isinstance(obj, dict):
                raise ValueError("JSON body must be an object")
            filename = obj.get("filename") or "file"
            if not isinstance(filename, str):
                raise ValueError("filename must be a string")
            raw = obj.get("content_base64")
            if not isinstance(raw, str):
                raise ValueError("content_base64 is required")
            import base64

            try:
                file_bytes = base64.b64decode(raw, validate=False)
            except (ValueError, TypeError) as exc:
                raise ValueError("invalid content_base64") from exc
            markdown_text = obj.get("markdown")
            if markdown_text is not None and not isinstance(markdown_text, str):
                raise ValueError("markdown must be a string")
            md_b64 = obj.get("markdown_base64")
            if md_b64 is not None:
                if not isinstance(md_b64, str):
                    raise ValueError("markdown_base64 must be a string")
                markdown_text = base64.b64decode(md_b64).decode("utf-8")
            relative = obj.get("relative_path")
            if relative is not None and not isinstance(relative, str):
                raise ValueError("relative_path must be a string")
            declared = obj.get("content_type")
            if declared is not None and not isinstance(declared, str):
                raise ValueError("content_type must be a string")
            library_raw = obj.get("library", obj.get("visibility"))
            if library_raw is not None and not isinstance(library_raw, str):
                raise ValueError("library must be a string")
            return filename, file_bytes, markdown_text, relative, declared, library_raw
        raise ValueError("Content-Type must be multipart/form-data or application/json")

    def _create_folder(self, user_id: str, body: bytes) -> Response:
        try:
            obj = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(obj, dict):
            raise ValueError("JSON body must be an object")
        path = obj.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path is required")
        library = self._library_from_obj(obj)
        root, _owner = self._open_library(user_id, library, write=True)
        rel = create_folder(root, path.strip())
        log("folder created", user_id=user_id, library=library, path=rel)
        return Response.json(200, {"path": rel, "library": library, "wiki": False})

    def _tree(self, user_id: str, query: dict[str, list[str]]) -> Response:
        library = self._library_from_query(query)
        root, owner = self._open_library(user_id, library, write=False)
        prefix = (query.get("path") or [None])[0] or None
        files = self.store.list_files(owner, limit=10000)
        try:
            tree = annotate_wiki_flags(build_inbox_tree(root, files, prefix=prefix), root)
        except FileNotFoundError:
            return Response.error(404, "not_found", "folder not found")
        return Response.json(200, {"user_id": user_id, "library": library, "tree": tree})

    def _list_files(self, user_id: str, query: dict[str, list[str]] | None = None) -> Response:
        library = self._library_from_query(query or {})
        _root, owner = self._open_library(user_id, library, write=False)
        rows = self.store.list_files(owner)
        return Response.json(200, {"library": library, "files": [file_public(r) for r in rows]})

    def _file_routes(self, user_id: str, route: str, query: dict[str, list[str]]) -> Response:
        rest = route[len("/v1/files/") :]
        if rest.endswith("/content"):
            file_id = rest[: -len("/content")].rstrip("/")
            return self._file_content(user_id, file_id, query)
        return self._get_file(user_id, rest)

    def _get_file(self, user_id: str, file_id: str) -> Response:
        row = self.store.get_file(file_id)
        if row is None or not self._file_visible(row, user_id):
            return Response.error(404, "not_found", "file not found")
        task = self.store.get_task(row.task_id) if row.task_id else None
        return Response.json(200, {"file": file_public(row), "task": task_public(task) if task else None})

    def _file_content(self, user_id: str, file_id: str, query: dict[str, list[str]]) -> Response:
        row = self.store.get_file(file_id)
        if row is None or not self._file_visible(row, user_id):
            return Response.error(404, "not_found", "file not found")
        tree = (query.get("tree") or ["inbox"])[0]
        root = self._root_for_owner(row.user_id)
        if tree == "inbox":
            path = safe_join(inbox_dir(root), row.inbox_relpath)
            if not path.is_file():
                return Response.error(404, "not_found", "inbox file missing")
            return Response.bytes(200, path.read_bytes(), "application/octet-stream", row.original_filename)
        if tree == "markdown":
            if not row.markdown_relpath:
                return Response.error(409, "not_ready", "markdown not converted yet")
            path = safe_join(markdown_dir(root), row.markdown_relpath)
            if not path.is_file():
                return Response.error(404, "not_found", "markdown file missing")
            return Response.bytes(200, path.read_bytes(), "text/markdown; charset=utf-8", Path(row.markdown_relpath).name)
        return Response.error(400, "bad_request", "tree must be inbox or markdown")

    def _search(self, user_id: str, body: bytes) -> Response:
        try:
            obj = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(obj, dict):
            raise ValueError("JSON body must be an object")
        req = parse_search_body(obj)
        library = self._library_from_obj(obj)
        prefix = parse_folder_scope(req.path)
        root, owner = self._open_library(user_id, library, write=False)
        index = None
        try:
            index = build_index_status(self.store, owner, root, self.qmd)
        except Exception as exc:
            log("index status failed", error=str(exc), user_id=user_id, library=library)
        fetch_limit = overfetch_limit(req.limit, scoped=prefix is not None)
        semantic_ready = None if index is None else bool(index.get("semantic_search_ready"))
        raw, fallback = search_with_fallback(
            self.qmd,
            root,
            req,
            fetch_limit=fetch_limit,
            semantic_ready=semantic_ready,
            limiter=self.hybrid_limiter,
        )
        results = filter_results(normalize_search_payload(raw), prefix, req.limit)
        payload: dict[str, Any] = {
            "user_id": user_id,
            "library": library,
            "mode": req.mode,
            "query": req.query,
            "path": prefix,
            "rerank": req.rerank,
            "min_score": req.min_score,
            "candidate_limit": req.candidate_limit,
            "results": results,
        }
        if req.intent:
            payload["intent"] = req.intent
        if req.searches:
            payload["searches"] = [{"type": item.type, "query": item.query} for item in req.searches]
        if fallback:
            payload["fallback"] = fallback
        if index:
            payload["keyword_search_ready"] = index.get("keyword_search_ready")
            payload["semantic_search_ready"] = index.get("semantic_search_ready")
            uses_semantic = req.mode in {"vsearch", "query"} or any(item.type in {"vec", "hyde"} for item in req.searches)
            if uses_semantic and not index.get("semantic_search_ready"):
                payload["hint"] = index.get("hint")
        return Response.json(200, payload)

    def _get_document(self, user_id: str, query: dict[str, list[str]]) -> Response:
        spec = (query.get("path") or [None])[0]
        if not spec or not spec.strip():
            raise ValueError("path is required")
        from_line = _opt_int(query, "from_line")
        if from_line is None:
            from_line = _opt_int(query, "from")
        max_lines = _opt_int(query, "max_lines")
        if max_lines is None:
            max_lines = _opt_int(query, "count")
        max_bytes = _opt_int(query, "max_bytes")
        if max_bytes is None:
            max_bytes = DEFAULT_MAX_BYTES
        line_numbers = _opt_bool(query, "line_numbers", default=True)
        library = self._library_from_query(query)
        root, _owner = self._open_library(user_id, library, write=False)
        try:
            doc = read_markdown(
                root,
                spec.strip(),
                from_line=from_line,
                max_lines=max_lines,
                line_numbers=line_numbers,
                max_bytes=max_bytes,
                qmd=self.qmd,
            )
        except DocumentNotFound as exc:
            return Response.json(
                404,
                {
                    "error": "not_found",
                    "message": "document not found",
                    "path": exc.spec,
                    "similar": exc.similar,
                },
            )
        except FileNotFoundError:
            return Response.json(
                404,
                {
                    "error": "not_found",
                    "message": "document not found",
                    "similar": similar_paths(root, spec.strip()),
                },
            )
        except UnicodeDecodeError as exc:
            raise ValueError("document is not utf-8 text") from exc
        return Response.json(200, {"user_id": user_id, "library": library, "document": doc})

    def _multi_get(self, user_id: str, body: bytes) -> Response:
        try:
            obj = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(obj, dict):
            raise ValueError("JSON body must be an object")
        raw_files = obj.get("files", obj.get("pattern"))
        tokens = _parse_file_list(raw_files)
        if not tokens:
            raise ValueError("files is required")
        from_line = obj.get("from_line", obj.get("fromLine"))
        max_lines = obj.get("max_lines", obj.get("maxLines"))
        max_bytes = obj.get("max_bytes", obj.get("maxBytes", DEFAULT_MAX_BYTES))
        if from_line is not None and (not isinstance(from_line, int) or from_line < 1):
            raise ValueError("from_line must be an integer >= 1")
        if max_lines is not None and (not isinstance(max_lines, int) or max_lines < 1):
            raise ValueError("max_lines must be an integer >= 1")
        if max_bytes is not None and (not isinstance(max_bytes, int) or max_bytes < 1):
            raise ValueError("max_bytes must be an integer >= 1")
        line_numbers = obj.get("line_numbers", obj.get("lineNumbers", True))
        if not isinstance(line_numbers, bool):
            raise ValueError("line_numbers must be a boolean")
        path_raw = obj.get("path")
        if path_raw is not None and not isinstance(path_raw, str):
            raise ValueError("path must be a string")
        prefix = parse_folder_scope(path_raw)
        library = self._library_from_obj(obj)
        root, _owner = self._open_library(user_id, library, write=False)
        documents: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for token in tokens:
            if len(documents) >= MAX_MULTI_FILES:
                errors.append({"file": token, "error": "too_many", "message": f"truncated at {MAX_MULTI_FILES} files"})
                break
            if any(ch in token for ch in "*?["):
                matches = expand_glob(root, token, prefix)
                if not matches:
                    errors.append(
                        {
                            "file": token,
                            "error": "not_found",
                            "message": "no files matched",
                            "similar": similar_paths(root, token.replace("**", "").replace("*", ""), prefix=prefix),
                        }
                    )
                    continue
                specs = matches
                glob_scope = True
            else:
                specs = [token]
                glob_scope = False
            for spec in specs:
                if len(documents) >= MAX_MULTI_FILES:
                    break
                try:
                    rel, docid, suf_from, suf_count = parse_document_ref(spec)
                except PathError as exc:
                    errors.append({"file": spec, "error": "bad_path", "message": str(exc)})
                    continue
                if rel and prefix and not glob_scope and not path_under_prefix(rel, prefix):
                    # Explicit paths may sit outside path (caller pin + mentions).
                    pass
                if rel and prefix and glob_scope and not path_under_prefix(rel, prefix):
                    continue
                locator = spec if not rel else rel
                if suf_from or suf_count:
                    locator = spec
                try:
                    doc = read_markdown(
                        root,
                        locator,
                        from_line=from_line if from_line is not None else suf_from,
                        max_lines=max_lines if max_lines is not None else suf_count,
                        line_numbers=line_numbers,
                        max_bytes=max_bytes,
                        qmd=self.qmd,
                        prefix=prefix,
                    )
                except DocumentNotFound as exc:
                    errors.append(
                        {
                            "file": exc.spec,
                            "error": "not_found",
                            "message": "document not found",
                            "similar": exc.similar,
                        }
                    )
                    continue
                except (FileNotFoundError, UnicodeDecodeError, ValueError, PathError) as exc:
                    errors.append({"file": locator, "error": "error", "message": str(exc)})
                    continue
                if prefix and doc.get("path") and not path_under_prefix(str(doc["path"]), prefix) and not docid and glob_scope:
                    continue
                documents.append(doc)
        return Response.json(
            200,
            {
                "user_id": user_id,
                "library": library,
                "path": prefix,
                "documents": documents,
                "errors": errors,
            },
        )

    def _list_tasks(self, user_id: str, query: dict[str, list[str]]) -> Response:
        status = (query.get("status") or [None])[0]
        library = self._library_from_query(query)
        _root, owner = self._open_library(user_id, library, write=False)
        rows = self.store.list_tasks(user_id=owner, status=status)
        return Response.json(200, {"library": library, "tasks": [task_public(r) for r in rows]})

    def _get_task(self, user_id: str, task_id: str) -> Response:
        row = self.store.get_task(task_id)
        if row is None:
            return Response.error(404, "not_found", "task not found")
        if row.user_id != user_id and not is_public_owner(row.user_id):
            return Response.error(404, "not_found", "task not found")
        return Response.json(200, {"task": task_public(row)})



def _parse_file_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    raise ValueError("files must be a string or array of strings")

def _opt_int(query: dict[str, list[str]], name: str) -> int | None:
    raw = (query.get(name) or [None])[0]
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _opt_bool(query: dict[str, list[str]], name: str, *, default: bool) -> bool:
    raw = (query.get(name) or [None])[0]
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def file_public(row: FileRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "library": LIBRARY_PUBLIC if is_public_owner(row.user_id) else LIBRARY_PRIVATE,
        "original_filename": row.original_filename,
        "content_type": row.content_type,
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
        "inbox_relpath": row.inbox_relpath,
        "markdown_relpath": row.markdown_relpath,
        "created_at": row.created_at,
        "task_id": row.task_id,
    }


def task_public(row: TaskRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "library": LIBRARY_PUBLIC if is_public_owner(row.user_id) else LIBRARY_PRIVATE,
        "kind": row.kind,
        "file_id": row.file_id,
        "status": row.status,
        "stage": row.stage,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "run_after": row.run_after,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            log("http", client=self.address_string(), request=fmt % args)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0:
                raise ValueError("invalid Content-Length")
            if length > app.settings.max_upload_bytes + MAX_JSON_BODY:
                raise BodyTooLarge
            return self.rfile.read(length) if length else b""

        def _write(self, response: Response) -> None:
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, latin1_header_value(key, value))
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response.body)

        def _dispatch(self, method: str) -> None:
            try:
                body = self._read_body()
            except BodyTooLarge:
                self._write(Response.error(413, "too_large", "request body too large"))
                return
            except Exception:
                self._write(Response.error(400, "bad_request", "cannot read body"))
                return
            headers = {k: v for k, v in self.headers.items()}
            response = app.handle(method, self.path, headers, body)
            self._write(response)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_HEAD(self) -> None:
            self._dispatch("GET")

    return Handler


def serve_http(app: App) -> ThreadingHTTPServer:
    handler = make_handler(app)
    server = ThreadingHTTPServer((app.settings.bind, app.settings.port), handler)
    log("listening", bind=app.settings.bind, port=server.server_address[1], data_root=str(app.settings.data_root))
    return server
