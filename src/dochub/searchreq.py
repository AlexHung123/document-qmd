from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SEARCH_TYPES = ("lex", "vec", "hyde")
SEARCH_MODES = ("search", "vsearch", "query")
MAX_SEARCHES = 10


@dataclass
class TypedSearch:
    type: str
    query: str


@dataclass
class SearchRequest:
    query: str
    mode: str
    limit: int = 10
    path: str | None = None
    min_score: float | None = None
    candidate_limit: int | None = None
    rerank: bool = False
    full: bool = False
    intent: str | None = None
    searches: list[TypedSearch] = field(default_factory=list)
    raw_query: str | None = None
    fallback: bool = True


def flatten_query_field(text: str) -> str:
    """Keep a typed query line on one row, matching Duffel's newline flattening."""
    return text.replace("\r", " ").replace("\n", " ").strip()


def keyword_fallback_query(req: SearchRequest) -> str:
    """Plain keyword text for `qmd search` after a hybrid failure.

    Never send a structured `lex:` / `vec:` document to BM25.
    """
    lex = [flatten_query_field(item.query) for item in req.searches if item.type == "lex"]
    lex = [text for text in lex if text]
    if lex:
        return " ".join(lex)
    other = [flatten_query_field(item.query) for item in req.searches]
    other = [text for text in other if text]
    if other:
        return " ".join(other)
    raw = flatten_query_field(req.raw_query or "")
    if raw:
        return raw
    return flatten_query_field(req.query)


def _as_bool(value: Any, name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _as_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    return float(value)


def _as_int(value: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer {lo}..{hi}")
    if value < lo or value > hi:
        raise ValueError(f"{name} must be an integer {lo}..{hi}")
    return value


def parse_typed_searches(raw: Any) -> list[TypedSearch]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ValueError("searches must be a non-empty array")
    if len(raw) > MAX_SEARCHES:
        raise ValueError(f"searches must have at most {MAX_SEARCHES} items")
    out: list[TypedSearch] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each search must be an object")
        kind = item.get("type")
        text = item.get("query")
        if kind not in SEARCH_TYPES:
            raise ValueError("search type must be lex, vec, or hyde")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("search query is required")
        out.append(TypedSearch(type=str(kind), query=text.strip()))
    return out


def compose_qmd_query(
    *,
    query: str | None = None,
    intent: str | None = None,
    searches: list[TypedSearch] | None = None,
    mode: str | None = None,
) -> tuple[str, str]:
    """Build the qmd CLI query string and mode.

    Typed searches are kept as a query document (`intent:` / `lex:` / `vec:` /
    `hyde:` lines) and sent to `qmd query`, except lex-only without intent,
    which uses `qmd search` with the raw keyword text (works before embed).
    An explicit mode of search/vsearch uses the raw channel text so type
    prefixes are not fed to BM25 or vsearch as literals.

    A bare `mode=query` string (no typed searches) is expanded into `lex:` plus
    `vec:` (and `intent:` when set) so qmd skips LLM query expansion. Newlines
    in those fields are flattened to spaces.
    """
    if mode is not None and mode not in SEARCH_MODES:
        raise ValueError("mode must be search, vsearch, or query")
    if query is not None and not isinstance(query, str):
        raise ValueError("query must be a string")
    if intent is not None and not isinstance(intent, str):
        raise ValueError("intent must be a string")
    intent_text = intent.strip() if isinstance(intent, str) and intent.strip() else None
    typed = list(searches or [])

    if typed:
        if query and query.strip():
            raise ValueError("query and searches are mutually exclusive")
        types = {item.type for item in typed}
        if mode in {"search", "vsearch"}:
            wanted = "lex" if mode == "search" else "vec"
            texts = [item.query for item in typed if item.type == wanted] or [item.query for item in typed]
            return " ".join(texts), mode
        if mode is None and types <= {"lex"} and not intent_text:
            return " ".join(item.query for item in typed), "search"
        lines: list[str] = []
        if intent_text:
            lines.append(f"intent: {intent_text}")
        for item in typed:
            lines.append(f"{item.type}: {item.query}")
        return "\n".join(lines), mode or "query"

    if not query or not query.strip():
        raise ValueError("provide query or searches")
    text = query.strip()
    if mode is None:
        mode = "search"
    if mode == "query":
        return _bare_hybrid_document(text, intent_text), mode
    return text, mode


def _bare_hybrid_document(query: str, intent: str | None) -> str:
    lines: list[str] = []
    intent_text = flatten_query_field(intent) if intent else ""
    if intent_text:
        lines.append(f"intent: {intent_text}")
    text = flatten_query_field(query)
    lines.append(f"lex: {text}")
    lines.append(f"vec: {text}")
    return "\n".join(lines)


def parse_search_body(obj: dict[str, Any]) -> SearchRequest:
    if not isinstance(obj, dict):
        raise ValueError("JSON body must be an object")
    raw_query = obj.get("query")
    if raw_query is not None and not isinstance(raw_query, str):
        raise ValueError("query must be a string")
    intent = obj.get("intent")
    if intent is not None and not isinstance(intent, str):
        raise ValueError("intent must be a string")
    mode = obj.get("mode")
    if mode is not None and mode not in SEARCH_MODES:
        raise ValueError("mode must be search, vsearch, or query")
    searches = parse_typed_searches(obj.get("searches")) if obj.get("searches") is not None else []
    query_text, resolved_mode = compose_qmd_query(
        query=raw_query,
        intent=intent,
        searches=searches,
        mode=mode if isinstance(mode, str) else None,
    )
    limit = obj.get("limit", 10)
    min_score = obj.get("min_score", obj.get("minScore"))
    candidate_limit = obj.get("candidate_limit", obj.get("candidateLimit"))
    path_raw = obj.get("path")
    if path_raw is not None and not isinstance(path_raw, str):
        raise ValueError("path must be a string")
    fallback_raw = obj.get("fallback")
    if fallback_raw is None:
        fallback_raw = obj.get("allow_fallback")
    return SearchRequest(
        query=query_text,
        mode=resolved_mode,
        limit=_as_int(limit, "limit", 1, 50),
        path=path_raw.strip() if isinstance(path_raw, str) and path_raw.strip() else None,
        min_score=_as_number(min_score, "min_score") if min_score is not None else None,
        candidate_limit=_as_int(candidate_limit, "candidate_limit", 1, 200) if candidate_limit is not None else None,
        rerank=_as_bool(obj.get("rerank"), "rerank", default=False),
        full=_as_bool(obj.get("full"), "full", default=False),
        intent=intent.strip() if isinstance(intent, str) and intent.strip() else None,
        searches=searches,
        raw_query=raw_query.strip() if isinstance(raw_query, str) and raw_query.strip() else None,
        fallback=_as_bool(fallback_raw, "fallback", default=True),
    )
