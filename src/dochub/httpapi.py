from __future__ import annotations

import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .convert import merge_front_matter
from .layout import ensure_user_tree
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
from .documents import read_markdown
from .qmd import QmdError, QmdRunner
from .queue import ControlStore, FileRow, TaskRow
from .scope import filter_results, normalize_search_payload, overfetch_limit, parse_folder_scope
from .tree import build_inbox_tree, create_folder
from .util import atomic_write_bytes, atomic_write_text, new_id, sha256_bytes, utcnow_iso

MAX_JSON_BODY = 2 * 1024 * 1024


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
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return cls(status, data, headers)


class App:
    def __init__(self, settings: Settings, store: ControlStore, qmd: QmdRunner):
        self.settings = settings
        self.store = store
        self.qmd = qmd

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
                return Response.json(200, {"user_id": user_id})
            if method == "POST" and route == "/v1/files":
                return self._upload(user_id, headers, body)
            if method == "POST" and route == "/v1/folders":
                return self._create_folder(user_id, body)
            if method == "GET" and route == "/v1/tree":
                return self._tree(user_id, query)
            if method == "GET" and route == "/v1/files":
                return self._list_files(user_id)
            if method == "GET" and route.startswith("/v1/files/"):
                return self._file_routes(user_id, route, query)
            if method == "POST" and route == "/v1/search":
                return self._search(user_id, body)
            if method == "GET" and route == "/v1/documents":
                return self._get_document(user_id, query)
            if method == "GET" and route == "/v1/tasks":
                return self._list_tasks(user_id, query)
            if method == "GET" and route.startswith("/v1/tasks/"):
                return self._get_task(user_id, route.rsplit("/", 1)[-1])
            return Response.error(404, "not_found", "unknown route")
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
        return Response.json(200, body)

    def _upload(self, user_id: str, headers: dict[str, str], body: bytes) -> Response:
        content_type = headers.get("Content-Type") or headers.get("content-type") or ""
        filename, file_bytes, markdown_text, relative_path, declared_type = self._parse_upload(content_type, body)
        if not file_bytes:
            return Response.error(400, "empty_file", "upload is empty")
        if not markdown_text or not markdown_text.strip():
            return Response.error(400, "markdown_required", "markdown is required")
        if len(file_bytes) > self.settings.max_upload_bytes:
            return Response.error(413, "too_large", "file exceeds max upload size")
        root = ensure_user_tree(self.settings.data_root, user_id, self.qmd)
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
        dest = safe_join(inbox, rel)
        atomic_write_bytes(dest, file_bytes)
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
        atomic_write_text(safe_join(md_root, md_rel), md_body)
        payload = {
            "inbox_relpath": rel,
            "markdown_relpath": md_rel,
            "original_filename": filename,
            "content_type": declared_type,
        }
        file_row, task_row = self.store.insert_upload(
            user_id=user_id,
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
    ) -> tuple[str, bytes, str | None, str | None, str | None]:
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
            return filename, file_part.data, markdown_text, relative, declared
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
            return filename, file_bytes, markdown_text, relative, declared
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
        root = ensure_user_tree(self.settings.data_root, user_id, self.qmd)
        rel = create_folder(root, path.strip())
        log("folder created", user_id=user_id, path=rel)
        return Response.json(200, {"path": rel})

    def _tree(self, user_id: str, query: dict[str, list[str]]) -> Response:
        root = ensure_user_tree(self.settings.data_root, user_id, self.qmd)
        prefix = (query.get("path") or [None])[0] or None
        files = self.store.list_files(user_id, limit=10000)
        try:
            tree = build_inbox_tree(root, files, prefix=prefix)
        except FileNotFoundError:
            return Response.error(404, "not_found", "folder not found")
        return Response.json(200, {"user_id": user_id, "tree": tree})

    def _list_files(self, user_id: str) -> Response:
        rows = self.store.list_files(user_id)
        return Response.json(200, {"files": [file_public(r) for r in rows]})

    def _file_routes(self, user_id: str, route: str, query: dict[str, list[str]]) -> Response:
        rest = route[len("/v1/files/") :]
        if rest.endswith("/content"):
            file_id = rest[: -len("/content")].rstrip("/")
            return self._file_content(user_id, file_id, query)
        return self._get_file(user_id, rest)

    def _get_file(self, user_id: str, file_id: str) -> Response:
        row = self.store.get_file(file_id)
        if row is None or row.user_id != user_id:
            return Response.error(404, "not_found", "file not found")
        task = self.store.get_task(row.task_id) if row.task_id else None
        return Response.json(200, {"file": file_public(row), "task": task_public(task) if task else None})

    def _file_content(self, user_id: str, file_id: str, query: dict[str, list[str]]) -> Response:
        row = self.store.get_file(file_id)
        if row is None or row.user_id != user_id:
            return Response.error(404, "not_found", "file not found")
        tree = (query.get("tree") or ["inbox"])[0]
        root = ensure_user_tree(self.settings.data_root, user_id, self.qmd)
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
        query = obj.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query is required")
        mode = obj.get("mode") or "search"
        if mode not in {"search", "vsearch", "query"}:
            raise ValueError("mode must be search, vsearch, or query")
        limit = obj.get("limit", 10)
        if not isinstance(limit, int) or limit < 1 or limit > 50:
            raise ValueError("limit must be an integer 1..50")
        min_score = obj.get("min_score")
        if min_score is not None and not isinstance(min_score, (int, float)):
            raise ValueError("min_score must be a number")
        full = bool(obj.get("full", False))
        path_raw = obj.get("path")
        if path_raw is not None and not isinstance(path_raw, str):
            raise ValueError("path must be a string")
        prefix = parse_folder_scope(path_raw)
        root = ensure_user_tree(self.settings.data_root, user_id, self.qmd)
        fetch_limit = overfetch_limit(limit, scoped=prefix is not None)
        raw = self.qmd.search(
            root,
            query.strip(),
            mode=mode,
            limit=fetch_limit,
            min_score=float(min_score) if min_score is not None else None,
            full=full,
        )
        results = filter_results(normalize_search_payload(raw), prefix, limit)
        return Response.json(
            200,
            {
                "user_id": user_id,
                "mode": mode,
                "query": query.strip(),
                "path": prefix,
                "results": results,
            },
        )

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
        line_numbers = _opt_bool(query, "line_numbers", default=True)
        root = ensure_user_tree(self.settings.data_root, user_id, self.qmd)
        try:
            doc = read_markdown(
                root,
                spec.strip(),
                from_line=from_line,
                max_lines=max_lines,
                line_numbers=line_numbers,
            )
        except FileNotFoundError:
            return Response.error(404, "not_found", "document not found")
        except UnicodeDecodeError as exc:
            raise ValueError("document is not utf-8 text") from exc
        return Response.json(200, {"user_id": user_id, "document": doc})

    def _list_tasks(self, user_id: str, query: dict[str, list[str]]) -> Response:
        status = (query.get("status") or [None])[0]
        rows = self.store.list_tasks(user_id=user_id, status=status)
        return Response.json(200, {"tasks": [task_public(r) for r in rows]})

    def _get_task(self, user_id: str, task_id: str) -> Response:
        row = self.store.get_task(task_id)
        if row is None or row.user_id != user_id:
            return Response.error(404, "not_found", "task not found")
        return Response.json(200, {"task": task_public(row)})


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
                self._write(Response.error(413, "too_large", "request body too large"))
                return b""
            return self.rfile.read(length) if length else b""

        def _write(self, response: Response) -> None:
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response.body)

        def _dispatch(self, method: str) -> None:
            try:
                body = self._read_body()
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
