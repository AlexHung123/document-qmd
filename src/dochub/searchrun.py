from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .qmd import QmdError
from .searchreq import SearchRequest, keyword_fallback_query

HYBRID_MODES = frozenset({"query", "vsearch"})
DEFAULT_HYBRID_SLOTS = 1


class HybridLimiter:
    """Process-wide, non-blocking slot for qmd query / vsearch."""

    def __init__(self, size: int = DEFAULT_HYBRID_SLOTS):
        self.size = max(1, int(size))
        self._sema = threading.BoundedSemaphore(self.size)

    def try_acquire(self) -> bool:
        return self._sema.acquire(blocking=False)

    def release(self) -> None:
        try:
            self._sema.release()
        except ValueError:
            pass


def search_with_fallback(
    qmd,
    user_root: Path,
    req: SearchRequest,
    *,
    fetch_limit: int,
    semantic_ready: bool | None,
    limiter: HybridLimiter,
) -> tuple[Any, dict[str, str] | None]:
    """Run the requested search; retry `qmd search` in the same user root on hybrid failure.

    Returns (raw_results, fallback_info). fallback_info is None when the
    requested mode ran successfully. Never opens a global qmd sqlite.
    """

    def run(mode: str, query: str) -> Any:
        return qmd.search(
            user_root,
            query,
            mode=mode,
            limit=fetch_limit,
            min_score=req.min_score,
            full=req.full,
            rerank=req.rerank,
            candidate_limit=req.candidate_limit,
        )

    if req.mode not in HYBRID_MODES:
        return run(req.mode, req.query), None

    if req.fallback and semantic_ready is False:
        raw = run("search", keyword_fallback_query(req))
        return raw, {"reason": "semantic_not_ready", "mode": "search"}

    if not limiter.try_acquire():
        if not req.fallback:
            raise QmdError("hybrid search busy", permanent=False, reason="busy")
        raw = run("search", keyword_fallback_query(req))
        return raw, {"reason": "busy", "mode": "search"}

    try:
        return run(req.mode, req.query), None
    except QmdError as exc:
        if not req.fallback or exc.permanent:
            raise
        hybrid_error = exc
    finally:
        limiter.release()

    try:
        raw = run("search", keyword_fallback_query(req))
    except QmdError as kw_exc:
        raise QmdError(
            f"search failed: hybrid error: {hybrid_error}; keyword error: {kw_exc}",
            permanent=kw_exc.permanent,
            reason=hybrid_error.reason or "qmd_exec_failed",
        ) from kw_exc
    return raw, {"reason": hybrid_error.reason or "qmd_exec_failed", "mode": "search"}
