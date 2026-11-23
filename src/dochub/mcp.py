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
from .scope import constrain_to_pin, parse_document_path, parse_folder_scope, resolve_scope

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
        query: str,
        *,
        mode: str = "search",
        limit: int = 10,
        path: str | None = None,
        min_score: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query, "mode": mode, "limit": limit}
        if path:
            body["path"] = path
        if min_score is not None:
            body["min_score"] = min_score
        return self._call("POST", "/v1/search", body)

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
    query = args.get("query")
    searches = args.get("searches")
    intent = args.get("intent")
    mode = args.get("mode")
    if query is not None and not isinstance(query, str):
        raise ValueError("query must be a string")
    if intent is not None and not isinstance(intent, str):
        raise ValueError("intent must be a string")
    if mode is not None and mode not in {"search", "vsearch", "query"}:
        raise ValueError("mode must be search, vsearch, or query")
    if searches is not None:
        if not isinstance(searches, list) or not searches:
            raise ValueError("searches must be a non-empty array")
        if query:
            raise ValueError("query and searches are mutually exclusive")
        lex: list[str] = []
        vec: list[str] = []
        hyde: list[str] = []
        types: set[str] = set()
        for item in searches:
            if not isinstance(item, dict):
                raise ValueError("each search must be an object")
            kind = item.get("type")
            text = item.get("query")
            if kind not in {"lex", "vec", "hyde"}:
                raise ValueError("search type must be lex, vec, or hyde")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("search query is required")
            types.add(kind)
            if kind == "lex":
                lex.append(text.strip())
            elif kind == "vec":
                vec.append(text.strip())
            else:
                hyde.append(text.strip())
        if mode is None and types <= {"lex"} and not (intent and intent.strip()):
            return " ".join(lex), "search"
        lines: list[str] = []
        if intent and intent.strip():
            lines.append(f"intent: {intent.strip()}")
        if lex:
            lines.append("lex: " + " ".join(lex))
        if vec:
            lines.append("vec: " + " ".join(vec))
        if hyde:
            lines.append("hyde: " + " ".join(hyde))
        return "\n".join(lines), mode or "query"
    if not query or not query.strip():
        raise ValueError("provide query or searches")
    if mode is None:
        mode = "search"
    if intent and intent.strip() and mode == "query":
        return f"intent: {intent.strip()}\nlex: {query.strip()}", mode
    return query.strip(), mode


def _glob_match(path: str, pattern: str) -> bool:
    import fnmatch

    pat = pattern.replace("\\", "/")
    if fnmatch.fnmatch(path, pat):
        return True
    if "/" not in pat.rstrip("/") and fnmatch.fnmatch(path.rsplit("/", 1)[-1], pat):
        return True
    if "**" in pat:
        import re

        relaxed = re.escape(pat).replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", ".")
        return re.fullmatch(relaxed, path) is not None
    return False


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
                        "description": "Override retrieval mode. search=BM25, vsearch=semantic, query=hybrid+rerank.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Folder or file prefix under this user's markdown tree (further restricted by the session pin).",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "minScore": {"type": "number", "minimum": 0, "maximum": 1},
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
    if not results:
        return f"No results found for {query!r}{scope}"
    lines = [f"Found {len(results)} result{'s' if len(results) != 1 else ''} for {query!r}{scope}:", ""]
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
        query, mode = compose_query(args)
        limit = args.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 50:
            raise ValueError("limit must be an integer 1..50")
        min_score = args.get("minScore", args.get("min_score"))
        if min_score is not None and not isinstance(min_score, (int, float)):
            raise ValueError("minScore must be a number")
        path = resolve_scope(self.folder, args.get("path"))
        payload = self.client.search(
            query,
            mode=mode,
            limit=limit,
            path=path,
            min_score=float(min_score) if min_score is not None else None,
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
        rel, suf_from, suf_count = parse_document_path(file.strip())
        rel = constrain_to_pin(rel, self.folder)
        payload = self.client.get_document(
            rel,
            from_line=from_line if from_line is not None else suf_from,
            max_lines=max_lines if max_lines is not None else suf_count,
            line_numbers=line_numbers,
        )
        doc = payload.get("document") if isinstance(payload, dict) else None
        if not isinstance(doc, dict):
            return _text_result("document not found", is_error=True)
        text = doc.get("text") or ""
        path = doc.get("path") or rel
        header = f"{path}"
        if doc.get("from_line"):
            header += f" (from line {doc['from_line']})"
        body = f"{header}\n---\n{text}"
        return _text_result(body, structured=payload)

    def _tool_multi_get(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError("pattern is required")
        max_lines = _opt_positive(args.get("maxLines", args.get("max_lines")), "maxLines")
        line_numbers = args.get("lineNumbers", args.get("line_numbers", True))
        if not isinstance(line_numbers, bool):
            raise ValueError("lineNumbers must be a boolean")
        tokens = [part.strip() for part in pattern.split(",") if part.strip()]
        if not tokens:
            raise ValueError("pattern is required")
        listed = None
        chunks: list[str] = []
        errors: list[str] = []
        documents: list[Any] = []
        for token in tokens:
            if any(ch in token for ch in "*?["):
                if listed is None:
                    listed = self.client.list_files().get("files") or []
                matches = []
                for row in listed:
                    if not isinstance(row, dict):
                        continue
                    rel = row.get("markdown_relpath")
                    if not isinstance(rel, str) or not rel:
                        continue
                    if self.folder and not (
                        rel == self.folder or rel.startswith(self.folder + "/")
                    ):
                        continue
                    if _glob_match(rel, token):
                        matches.append((rel, None, None))
                if not matches:
                    errors.append(f"no files matched {token}")
                    continue
                paths = matches
            else:
                try:
                    rel, suf_from, suf_count = parse_document_path(token)
                    paths = [(constrain_to_pin(rel, self.folder), suf_from, suf_count)]
                except PathError as exc:
                    errors.append(f"{token}: {exc}")
                    continue
            for item in paths:
                if isinstance(item, tuple):
                    rel, suf_from, suf_count = item
                else:
                    rel, suf_from, suf_count = item, None, None
                try:
                    payload = self.client.get_document(
                        rel,
                        from_line=suf_from,
                        max_lines=max_lines if max_lines is not None else suf_count,
                        line_numbers=line_numbers,
                    )
                except DochubApiError as exc:
                    errors.append(f"{rel}: {exc}")
                    continue
                doc = payload.get("document") if isinstance(payload, dict) else None
                if not isinstance(doc, dict):
                    errors.append(f"{rel}: not found")
                    continue
                documents.append(doc)
                chunks.append(f"{doc.get('path')}\n---\n{doc.get('text') or ''}")
        text = "\n\n".join(chunks) if chunks else "No documents retrieved"
        if errors:
            text = "Errors:\n" + "\n".join(errors) + ("\n\n" + text if chunks else "")
        return _text_result(text, is_error=not chunks, structured={"documents": documents, "errors": errors})

    def _tool_tree(self, args: dict[str, Any]) -> dict[str, Any]:
        path = resolve_scope(self.folder, args.get("path"))
        payload = self.client.tree(path)
        return _text_result(json.dumps(payload, ensure_ascii=False, indent=2), structured=payload)

    def _tool_status(self) -> dict[str, Any]:
        health = self.client.health()
        body = {
            "user_id": self.client.user_id,
            "folder": self.folder,
            "url": self.client.base,
            "health": health,
        }
        lines = [
            "Dochub status:",
            f"  user: {self.client.user_id}",
            f"  folder pin: {self.folder or '(entire markdown tree)'}",
            f"  url: {self.client.base}",
            f"  qmd: {health.get('qmd')}",
        ]
        queue = health.get("queue")
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
