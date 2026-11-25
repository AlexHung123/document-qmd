from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import __version__
from .log import log
from .paths import PathError, parse_user_id
from .scope import constrain_to_pin, parse_document_path, parse_document_ref, parse_folder_scope, path_under_prefix, resolve_scope

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
DEFAULT_URL = "http://127.0.0.1:8787"
DEFAULT_TIMEOUT = 180
JSONRPC_PARSE = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL = -32603


class DochubApiError(Exception):
    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class DochubClient:
    """HTTP client for the Dochub API. Identity is fixed at construction."""

    def __init__(
        self,
        *,
        url: str,
        user_id: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport=None,
    ):
        self.base = url.rstrip("/")
        self.user_id = parse_user_id(user_id)
        self.timeout = timeout
        self.transport = transport or self._http_transport

    def _http_transport(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        query: dict[str, str] | None,
    ) -> tuple[int, Any]:
        url = self.base + path
        if query:
            filtered = {key: value for key, value in query.items() if value is not None and value != ""}
            if filtered:
                url += "?" + urlencode(filtered)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = Request(url, data=data, method=method)
        req.add_header("X-User-Id", self.user_id)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return resp.status, payload
        except HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            payload: Any
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {"message": raw.decode("utf-8", "replace")}
            message = payload.get("message") if isinstance(payload, dict) else str(payload)
            raise DochubApiError(exc.code, str(message or exc.reason), payload) from exc
        except URLError as exc:
            raise DochubApiError(502, f"dochub unreachable: {exc.reason}") from exc

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> Any:
        status, payload = self.transport(method, path, body, query)
        if status >= 400:
            message = payload.get("message") if isinstance(payload, dict) else str(payload)
            raise DochubApiError(status, str(message or f"HTTP {status}"), payload)
        return payload

    def health(self) -> dict[str, Any]:
        return self._call("GET", "/v1/health")

    def search(
        self,
        query: str | None = None,
        *,
        mode: str = "search",
        limit: int = 10,
        path: str | None = None,
        min_score: float | None = None,
        searches: list[dict[str, str]] | None = None,
        intent: str | None = None,
        rerank: bool | None = None,
        candidate_limit: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"mode": mode, "limit": limit}
        if query:
            body["query"] = query
        if path:
            body["path"] = path
        if min_score is not None:
            body["min_score"] = min_score
        if searches:
            body["searches"] = searches
        if intent:
            body["intent"] = intent
        if rerank is not None:
            body["rerank"] = rerank
        if candidate_limit is not None:
            body["candidate_limit"] = candidate_limit
        return self._call("POST", "/v1/search", body)

    def status(self) -> dict[str, Any]:
        return self._call("GET", "/v1/status")

    def context(self) -> dict[str, Any]:
        return self._call("GET", "/v1/context")

    def multi_get(
        self,
        files,
        *,
        from_line: int | None = None,
        max_lines: int | None = None,
        max_bytes: int | None = None,
        line_numbers: bool = True,
        path: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"files": files, "line_numbers": line_numbers}
        if from_line is not None:
            body["from_line"] = from_line
        if max_lines is not None:
            body["max_lines"] = max_lines
        if max_bytes is not None:
            body["max_bytes"] = max_bytes
        if path:
            body["path"] = path
        return self._call("POST", "/v1/documents/multi", body)

    def get_document(
        self,
        path: str,
        *,
        from_line: int | None = None,
        max_lines: int | None = None,
        line_numbers: bool = True,
    ) -> dict[str, Any]:
        query = {
            "path": path,
            "line_numbers": "1" if line_numbers else "0",
        }
        if from_line is not None:
            query["from_line"] = str(from_line)
        if max_lines is not None:
            query["max_lines"] = str(max_lines)
        return self._call("GET", "/v1/documents", query=query)

    def list_files(self) -> dict[str, Any]:
        return self._call("GET", "/v1/files")

    def tree(self, path: str | None = None) -> dict[str, Any]:
        query = {"path": path} if path else None
        return self._call("GET", "/v1/tree", query=query)


def compose_query(args: dict[str, Any]) -> tuple[str, str]:
    """Build a Dochub search query + mode from QMD-style MCP arguments."""
    from .searchreq import compose_qmd_query, parse_typed_searches

    searches = parse_typed_searches(args.get("searches")) if args.get("searches") is not None else []
    return compose_qmd_query(
        query=args.get("query"),
        intent=args.get("intent"),
        searches=searches,
        mode=args.get("mode"),
    )


def tool_definitions(folder: str | None) -> list[dict[str, Any]]:
    pin = f" Session is pinned to folder `{folder}`." if folder else ""
    return [
        {
            "name": "query",
            "description": (
                "Search this user's indexed markdown documents. Always search before answering, "
                "then call get on the best hits. Do not pass user_id."
                + pin
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Plain-text query. Mutually exclusive with searches. Default mode is BM25 search.",
                    },
                    "searches": {
                        "type": "array",
                        "description": "QMD-style typed sub-queries. lex=keywords, vec=semantic, hyde=hypothetical passage.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["lex", "vec", "hyde"]},
                                "query": {"type": "string"},
                            },
                            "required": ["type", "query"],
                        },
                    },
                    "intent": {
                        "type": "string",
                        "description": "What to find and what to avoid. Used when composing a hybrid query.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["search", "vsearch", "query"],
                        "description": "Override retrieval mode. search=BM25, vsearch=semantic, query=hybrid RRF (rerank off unless rerank=true).",
                    },
                    "path": {
                        "type": "string",
                        "description": "Folder or file prefix under this user's markdown tree (further restricted by the session pin).",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "minScore": {"type": "number", "minimum": 0, "maximum": 1},
                    "rerank": {
                        "type": "boolean",
                        "description": "Cross-encoder rerank for query mode. Default false; set true to enable.",
                    },
                    "candidateLimit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        },
        {
            "name": "get",
            "description": (
                "Retrieve a markdown document by dochub_path from search results. "
                "Supports path:from:count (e.g. contracts/note.md:120:40). Do not pass user_id."
                + pin
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "dochub_path or qmd://markdown/... path. Optional :from:count suffix.",
                    },
                    "fromLine": {"type": "integer", "description": "1-indexed start line (overrides suffix)."},
                    "maxLines": {"type": "integer", "description": "Maximum lines to return (overrides suffix)."},
                    "lineNumbers": {
                        "type": "boolean",
                        "default": True,
                        "description": "Prefix lines with N:. On by default.",
                    },
                },
                "required": ["file"],
            },
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        },
        {
            "name": "multi_get",
            "description": "Retrieve several documents by comma-separated paths or a glob under the session folder.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Comma-separated dochub_paths, or a glob such as contracts/*.md.",
                    },
                    "maxLines": {"type": "integer", "description": "Maximum lines per file."},
                    "lineNumbers": {"type": "boolean", "default": True},
                },
                "required": ["pattern"],
            },
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        },
        {
            "name": "tree",
            "description": "List this user's folder tree (inbox). Optional path is relative to the session pin.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Subtree under the user's files / session pin."},
                },
            },
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        },
        {
            "name": "status",
            "description": "Dochub health for this user: queue, folder pin, whether QMD is available.",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        },
    ]


def format_search_summary(payload: dict[str, Any]) -> str:
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    query = payload.get("query") or ""
    path = payload.get("path")
    scope = f" in {path}" if path else ""
    fallback = payload.get("fallback") if isinstance(payload.get("fallback"), dict) else None
    fallback_note = ""
    if fallback and fallback.get("reason"):
        fallback_note = f" (fell back to keyword search: {fallback.get('reason')})"
    if not results:
        return f"No results found for {query!r}{scope}{fallback_note}"
    lines = [f"Found {len(results)} result{'s' if len(results) != 1 else ''} for {query!r}{scope}{fallback_note}:", ""]
    for item in results:
        if not isinstance(item, dict):
            continue
        docid = item.get("docid") or ""
        score = item.get("score")
        rel = item.get("dochub_path") or item.get("file") or ""
        title = item.get("title") or ""
        line = item.get("line")
        snippet = (item.get("snippet") or "").strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:237] + "..."
        score_txt = ""
        if isinstance(score, (int, float)):
            score_txt = f"{score:.2f} " if score <= 1 else f"{score} "
        loc = f"{rel}:{line}" if line else str(rel)
        lines.append(f"{docid} {score_txt}{loc} {title}".strip())
        if snippet:
            lines.append(f"  {snippet}")
    lines.append("")
    lines.append("Fetch full text with get using dochub_path (add :line:80 to read around a hit).")
    return "\n".join(lines)


def _text_result(text: str, *, is_error: bool = False, structured: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return result


class McpSession:
    """JSON-RPC MCP session bound to one user and optional folder pin."""

    def __init__(self, client: DochubClient, folder: str | None = None):
        self.client = client
        self.folder = parse_folder_scope(folder)
        self.tools = {item["name"]: item for item in tool_definitions(self.folder)}

    def _similar_under_pin(self, similar: Any) -> list[str]:
        names = [item for item in similar if isinstance(item, str)] if isinstance(similar, list) else []
        if not self.folder:
            return names
        return [item for item in names if path_under_prefix(item, self.folder)]

    def instructions(self) -> str:
        pin = f" Folder pin: `{self.folder}` (all tools stay inside it)." if self.folder else ""
        return (
            "Dochub search over this user's markdown documents. "
            "Identity is already bound; never pass user_id. "
            "Workflow: query → get/multi_get, then answer from retrieved text citing dochub_path and line numbers. "
            "Prefer query with lex for exact terms; add vec/hyde (mode query) for conceptual recall. "
            "If query/vsearch fails, retry with mode search."
            + pin
        )

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _rpc_error(None, JSONRPC_INVALID_REQUEST, "invalid request")
        if message.get("jsonrpc") != "2.0":
            return _rpc_error(message.get("id"), JSONRPC_INVALID_REQUEST, "jsonrpc must be 2.0")
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params", {})
        if not isinstance(method, str):
            return _rpc_error(msg_id, JSONRPC_INVALID_REQUEST, "method is required")
        is_notification = "id" not in message
        try:
            result = self._dispatch(method, params if isinstance(params, dict) else {})
        except MethodNotFound as exc:
            if is_notification:
                return None
            return _rpc_error(msg_id, JSONRPC_METHOD_NOT_FOUND, f"method not found: {exc.method}")
        except PathError as exc:
            if is_notification:
                return None
            if method == "tools/call":
                return _rpc_result(msg_id, _text_result(str(exc), is_error=True))
            return _rpc_error(msg_id, JSONRPC_INVALID_PARAMS, str(exc))
        except ValueError as exc:
            if is_notification:
                return None
            if method == "tools/call":
                return _rpc_result(msg_id, _text_result(str(exc), is_error=True))
            return _rpc_error(msg_id, JSONRPC_INVALID_PARAMS, str(exc))
        except DochubApiError as exc:
            if is_notification:
                return None
            if method == "tools/call":
                return _rpc_result(msg_id, _text_result(str(exc), is_error=True))
            return _rpc_error(msg_id, JSONRPC_INTERNAL, str(exc))
        except Exception as exc:
            log("mcp handler error", method=method, error=str(exc))
            if is_notification:
                return None
            return _rpc_error(msg_id, JSONRPC_INTERNAL, "internal error")
        if is_notification:
            return None
        return _rpc_result(msg_id, result)

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion") or PROTOCOL_VERSIONS[0]
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "dochub", "version": __version__},
                "instructions": self.instructions(),
            }
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return {}
        if method == "shutdown":
            return {}
        if method == "tools/list":
            return {"tools": list(self.tools.values())}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or name not in self.tools:
                return _text_result(f"unknown tool: {name}", is_error=True)
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            if "user_id" in arguments or "userId" in arguments:
                return _text_result("user_id is not a tool argument; this session is already bound", is_error=True)
            return self._call_tool(name, arguments)
        if method in {"resources/list", "resources/templates/list"}:
            return {"resources": []} if method == "resources/list" else {"resourceTemplates": []}
        raise MethodNotFound(method)

    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "query":
            return self._tool_query(args)
        if name == "get":
            return self._tool_get(args)
        if name == "multi_get":
            return self._tool_multi_get(args)
        if name == "tree":
            return self._tool_tree(args)
        if name == "status":
            return self._tool_status()
        return _text_result(f"unknown tool: {name}", is_error=True)

    def _tool_query(self, args: dict[str, Any]) -> dict[str, Any]:
        _composed, mode = compose_query(args)
        limit = args.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 50:
            raise ValueError("limit must be an integer 1..50")
        min_score = args.get("minScore", args.get("min_score"))
        if min_score is not None and not isinstance(min_score, (int, float)):
            raise ValueError("minScore must be a number")
        candidate_limit = args.get("candidateLimit", args.get("candidate_limit"))
        if candidate_limit is not None and (not isinstance(candidate_limit, int) or isinstance(candidate_limit, bool) or candidate_limit < 1):
            raise ValueError("candidateLimit must be an integer >= 1")
        rerank = args.get("rerank")
        if rerank is not None and not isinstance(rerank, bool):
            raise ValueError("rerank must be a boolean")
        path = resolve_scope(self.folder, args.get("path"))
        searches = args.get("searches") if isinstance(args.get("searches"), list) else None
        intent = args.get("intent") if isinstance(args.get("intent"), str) else None
        raw_query = args.get("query") if isinstance(args.get("query"), str) else None
        payload = self.client.search(
            None if searches else raw_query,
            mode=mode,
            limit=limit,
            path=path,
            min_score=float(min_score) if min_score is not None else None,
            searches=searches,
            intent=intent.strip() if intent and intent.strip() else None,
            rerank=rerank,
            candidate_limit=candidate_limit,
        )
        return _text_result(format_search_summary(payload), structured=payload)

    def _tool_get(self, args: dict[str, Any]) -> dict[str, Any]:
        file = args.get("file")
        if not isinstance(file, str) or not file.strip():
            raise ValueError("file is required")
        from_line = _opt_positive(args.get("fromLine", args.get("from_line")), "fromLine")
        max_lines = _opt_positive(args.get("maxLines", args.get("max_lines")), "maxLines")
        line_numbers = args.get("lineNumbers", args.get("line_numbers", True))
        if not isinstance(line_numbers, bool):
            raise ValueError("lineNumbers must be a boolean")
        rel, docid, suf_from, suf_count = parse_document_ref(file.strip())
        locator = docid or constrain_to_pin(rel, self.folder)
        try:
            payload = self.client.get_document(
                locator,
                from_line=from_line if from_line is not None else suf_from,
                max_lines=max_lines if max_lines is not None else suf_count,
                line_numbers=line_numbers,
            )
        except DochubApiError as exc:
            similar = self._similar_under_pin(exc.payload.get("similar") if isinstance(exc.payload, dict) else None)
            msg = str(exc)
            if similar:
                msg += f" (similar: {', '.join(similar)})"
            return _text_result(msg, is_error=True)
        doc = payload.get("document") if isinstance(payload, dict) else None
        if not isinstance(doc, dict):
            return _text_result("document not found", is_error=True)
        if self.folder and isinstance(doc.get("path"), str) and not path_under_prefix(doc["path"], self.folder):
            return _text_result("path is outside the pinned folder", is_error=True)
        text = doc.get("text") or ""
        path = doc.get("path") or rel or docid
        header = f"{path}"
        if doc.get("from_line"):
            header += f" (from line {doc['from_line']})"
        body = f"{header}\n---\n{text}"
        return _text_result(body, structured=payload)

    def _tool_multi_get(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = args.get("pattern", args.get("files"))
        if isinstance(pattern, list):
            tokens = [part.strip() for part in pattern if isinstance(part, str) and part.strip()]
        elif isinstance(pattern, str) and pattern.strip():
            tokens = [part.strip() for part in pattern.split(",") if part.strip()]
        else:
            raise ValueError("pattern is required")
        if not tokens:
            raise ValueError("pattern is required")
        max_lines = _opt_positive(args.get("maxLines", args.get("max_lines")), "maxLines")
        max_bytes = args.get("maxBytes", args.get("max_bytes"))
        if max_bytes is not None and (not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1):
            raise ValueError("maxBytes must be an integer >= 1")
        line_numbers = args.get("lineNumbers", args.get("line_numbers", True))
        if not isinstance(line_numbers, bool):
            raise ValueError("lineNumbers must be a boolean")
        payload = self.client.multi_get(
            tokens,
            max_lines=max_lines,
            max_bytes=max_bytes,
            line_numbers=line_numbers,
            path=self.folder,
        )
        documents = payload.get("documents") if isinstance(payload, dict) else None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if not isinstance(documents, list):
            documents = []
        if not isinstance(errors, list):
            errors = []
        if self.folder:
            kept = []
            for doc in documents:
                rel = doc.get("path") if isinstance(doc, dict) else None
                if isinstance(rel, str) and path_under_prefix(rel, self.folder):
                    kept.append(doc)
                else:
                    errors.append({"file": rel or "?", "error": "outside_pin", "message": "path is outside the pinned folder"})
            documents = kept
            for item in errors:
                if isinstance(item, dict) and "similar" in item:
                    item["similar"] = self._similar_under_pin(item.get("similar"))
        chunks = [f"{doc.get('path')}\n---\n{doc.get('text') or ''}" for doc in documents if isinstance(doc, dict)]
        err_lines = []
        for item in errors:
            if isinstance(item, dict):
                hint = item.get("similar")
                msg = f"{item.get('file')}: {item.get('message') or item.get('error')}"
                if hint:
                    msg += f" (similar: {', '.join(map(str, hint))})"
                err_lines.append(msg)
            else:
                err_lines.append(str(item))
        text = "\n\n".join(chunks) if chunks else "No documents retrieved"
        if err_lines:
            text = "Errors:\n" + "\n".join(err_lines) + (("\n\n" + text) if chunks else "")
        return _text_result(text, is_error=not chunks, structured={"documents": documents, "errors": errors})

    def _tool_tree(self, args: dict[str, Any]) -> dict[str, Any]:
        path = resolve_scope(self.folder, args.get("path"))
        payload = self.client.tree(path)
        return _text_result(json.dumps(payload, ensure_ascii=False, indent=2), structured=payload)

    def _tool_status(self) -> dict[str, Any]:
        try:
            status = self.client.status()
        except DochubApiError:
            status = self.client.health()
        body = {
            "user_id": self.client.user_id,
            "folder": self.folder,
            "url": self.client.base,
            "health": status,
        }
        lines = [
            "Dochub status:",
            f"  user: {self.client.user_id}",
            f"  folder pin: {self.folder or '(entire markdown tree)'}",
            f"  url: {self.client.base}",
            f"  qmd: {status.get('qmd')}",
        ]
        if status.get("keyword_search_ready") is not None:
            lines.append(f"  keyword_search_ready: {status.get('keyword_search_ready')}")
        if status.get("semantic_search_ready") is not None:
            lines.append(f"  semantic_search_ready: {status.get('semantic_search_ready')}")
        if status.get("hint"):
            lines.append(f"  hint: {status.get('hint')}")
        queue = status.get("queue") or (status.get("index") or {}).get("queue")
        if isinstance(queue, dict):
            lines.append(f"  queue: {queue}")
        return _text_result("\n".join(lines), structured=body)



class MethodNotFound(Exception):
    def __init__(self, method: str):
        super().__init__(method)
        self.method = method


def _opt_positive(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be an integer >= 1")
    return value


def _rpc_result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def read_message(fp) -> dict[str, Any] | None:
    header_line = fp.readline()
    if not header_line:
        return None
    if header_line.lstrip().startswith(b"{"):
        try:
            return json.loads(header_line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON") from exc
    headers: dict[str, str] = {}
    line = header_line
    while line not in (b"\r\n", b"\n"):
        decoded = line.decode("ascii", "replace")
        if ":" not in decoded:
            raise ValueError("invalid MCP header")
        key, value = decoded.split(":", 1)
        headers[key.strip().lower()] = value.strip()
        line = fp.readline()
        if not line:
            break
    if "content-length" not in headers:
        raise ValueError("Content-Length is required")
    n = int(headers["content-length"])
    body = fp.read(n)
    if len(body) != n:
        raise ValueError("truncated MCP body")
    return json.loads(body.decode("utf-8"))


def write_message(fp, message: dict[str, Any]) -> None:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    fp.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
    fp.flush()


def config_from_env(
    *,
    url: str | None = None,
    user_id: str | None = None,
    folder: str | None = None,
    timeout: float | None = None,
) -> tuple[str, str, str | None, float]:
    resolved_url = url or os.environ.get("DOCHUB_URL") or DEFAULT_URL
    resolved_user = user_id or os.environ.get("DOCHUB_USER_ID") or ""
    resolved_folder = folder if folder is not None else os.environ.get("DOCHUB_FOLDER")
    if timeout is not None:
        resolved_timeout = timeout
    else:
        raw = os.environ.get("DOCHUB_TIMEOUT")
        resolved_timeout = float(raw) if raw else DEFAULT_TIMEOUT
    if not resolved_user.strip():
        raise ValueError("DOCHUB_USER_ID is required (set by the host, never by the model)")
    parse_user_id(resolved_user.strip())
    return resolved_url.rstrip("/"), resolved_user.strip(), parse_folder_scope(resolved_folder), resolved_timeout


def run_mcp(
    *,
    url: str | None = None,
    user_id: str | None = None,
    folder: str | None = None,
    timeout: float | None = None,
) -> int:
    try:
        resolved_url, resolved_user, resolved_folder, resolved_timeout = config_from_env(
            url=url, user_id=user_id, folder=folder, timeout=timeout
        )
    except (ValueError, PathError) as exc:
        print(f"dochub mcp: {exc}", file=sys.stderr)
        return 2
    client = DochubClient(url=resolved_url, user_id=resolved_user, timeout=resolved_timeout)
    session = McpSession(client, resolved_folder)
    log("mcp starting", user_id=resolved_user, folder=resolved_folder or "", url=resolved_url)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            message = read_message(stdin)
        except ValueError as exc:
            write_message(stdout, _rpc_error(None, JSONRPC_PARSE, str(exc)))
            continue
        if message is None:
            return 0
        if isinstance(message, dict) and message.get("method") == "exit":
            return 0
        try:
            reply = session.handle(message)
        except MethodNotFound as exc:
            if "id" in message:
                write_message(stdout, _rpc_error(message.get("id"), JSONRPC_METHOD_NOT_FOUND, f"method not found: {exc.method}"))
            continue
        if reply is not None:
            write_message(stdout, reply)
