from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import support

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dochub.qmd import QmdError, QmdRunner  # noqa: E402
from dochub.searchreq import (  # noqa: E402
    compose_qmd_query,
    keyword_fallback_query,
    parse_search_body,
    parse_typed_searches,
)
from dochub.searchrun import HybridLimiter, search_with_fallback  # noqa: E402


class ComposeQmdQueryTests(unittest.TestCase):
    def test_bare_query_defaults_to_search(self):
        self.assertEqual(compose_qmd_query(query="hello"), ("hello", "search"))

    def test_bare_query_mode_query_fills_lex_and_vec(self):
        query, mode = compose_qmd_query(query="quarterly plan", mode="query")
        self.assertEqual(mode, "query")
        self.assertEqual(query, "lex: quarterly plan\nvec: quarterly plan")

    def test_bare_query_mode_query_includes_intent_and_flattens_newlines(self):
        query, mode = compose_qmd_query(
            query="hello\nworld",
            intent="Find the\nOKR note",
            mode="query",
        )
        self.assertEqual(mode, "query")
        self.assertEqual(query, "intent: Find the OKR note\nlex: hello world\nvec: hello world")

    def test_intent_plus_bare_query_includes_vec(self):
        query, mode = compose_qmd_query(query="OKR", intent="Find the note", mode="query")
        self.assertIn("vec: OKR", query)
        self.assertTrue(query.startswith("intent: Find the note\n"))
        self.assertEqual(mode, "query")

    def test_typed_searches_are_not_collapsed(self):
        searches = parse_typed_searches(
            [
                {"type": "lex", "query": "OKR"},
                {"type": "hyde", "query": "A note about quarterly OKRs"},
            ]
        )
        query, mode = compose_qmd_query(intent="Find the OKR note", searches=searches)
        self.assertEqual(mode, "query")
        self.assertEqual(
            query,
            "intent: Find the OKR note\nlex: OKR\nhyde: A note about quarterly OKRs",
        )
        self.assertNotIn("vec:", query)

    def test_lex_only_without_intent_uses_search(self):
        searches = parse_typed_searches([{"type": "lex", "query": "cockpit OKR"}])
        self.assertEqual(compose_qmd_query(searches=searches), ("cockpit OKR", "search"))

    def test_parse_body_rerank_defaults_false_and_keeps_raw_query(self):
        req = parse_search_body({"query": "hello", "mode": "query"})
        self.assertFalse(req.rerank)
        self.assertTrue(req.fallback)
        self.assertEqual(req.raw_query, "hello")
        self.assertEqual(req.query, "lex: hello\nvec: hello")
        self.assertEqual(keyword_fallback_query(req), "hello")

    def test_parse_body_rerank_true_is_opt_in(self):
        req = parse_search_body({"query": "hello", "mode": "query", "rerank": True})
        self.assertTrue(req.rerank)

    def test_fallback_opt_out(self):
        req = parse_search_body({"query": "hello", "mode": "query", "fallback": False})
        self.assertFalse(req.fallback)


class SearchFallbackTests(unittest.TestCase):
    def test_semantic_not_ready_skips_hybrid_and_uses_keyword(self):
        qmd = support.FakeQmd()
        req = parse_search_body({"query": "hello", "mode": "query"})
        raw, fallback = search_with_fallback(
            qmd,
            Path("/tmp/users/alice"),
            req,
            fetch_limit=10,
            semantic_ready=False,
            limiter=HybridLimiter(),
        )
        self.assertEqual(fallback, {"reason": "semantic_not_ready", "mode": "search"})
        self.assertEqual(len(qmd.calls), 1)
        self.assertEqual(qmd.calls[0][2], "hello")
        self.assertEqual(qmd.calls[0][3]["mode"], "search")
        self.assertEqual(raw[0]["query"], "hello")

    def test_timeout_retries_keyword_in_same_root(self):
        qmd = support.FakeQmd()
        qmd.search_errors = {"query": QmdError("timed out", reason="timeout")}
        root = Path("/tmp/users/alice")
        req = parse_search_body({"query": "hello", "mode": "query"})
        raw, fallback = search_with_fallback(
            qmd, root, req, fetch_limit=10, semantic_ready=True, limiter=HybridLimiter()
        )
        self.assertEqual(fallback, {"reason": "timeout", "mode": "search"})
        modes = [c[3]["mode"] for c in qmd.calls if c[0] == "search"]
        self.assertEqual(modes, ["query", "search"])
        self.assertEqual(qmd.calls[0][1], str(root))
        self.assertEqual(qmd.calls[1][1], str(root))
        self.assertEqual(qmd.calls[1][2], "hello")
        self.assertEqual(raw[0]["mode"], "search")

    def test_busy_fail_fast_to_keyword(self):
        limiter = HybridLimiter()
        self.assertTrue(limiter.try_acquire())
        qmd = support.FakeQmd()
        req = parse_search_body({"query": "hello", "mode": "query"})
        raw, fallback = search_with_fallback(
            qmd,
            Path("/tmp/users/alice"),
            req,
            fetch_limit=10,
            semantic_ready=True,
            limiter=limiter,
        )
        self.assertEqual(fallback, {"reason": "busy", "mode": "search"})
        self.assertEqual([c[3]["mode"] for c in qmd.calls], ["search"])
        self.assertEqual(raw[0]["query"], "hello")
        limiter.release()

    def test_opt_out_does_not_fallback(self):
        qmd = support.FakeQmd()
        qmd.search_errors = {"query": QmdError("boom", reason="qmd_exec_failed")}
        req = parse_search_body({"query": "hello", "mode": "query", "fallback": False})
        with self.assertRaises(QmdError) as ctx:
            search_with_fallback(
                qmd,
                Path("/tmp/users/alice"),
                req,
                fetch_limit=10,
                semantic_ready=True,
                limiter=HybridLimiter(),
            )
        self.assertEqual(ctx.exception.reason, "qmd_exec_failed")
        self.assertEqual([c[3]["mode"] for c in qmd.calls], ["query"])


class QmdSearchArgsTests(unittest.TestCase):
    def test_query_uses_hybrid_timeout_and_no_rerank_by_default(self):
        with tempfile.TemporaryDirectory() as raw:
            settings = support.test_settings(
                Path(raw), search_timeout_seconds=20, keyword_search_timeout_seconds=30
            )
            runner = QmdRunner(settings)
            seen: dict = {}

            class Proc:
                stdout = "[]"
                stderr = ""
                returncode = 0

            def fake_run(user_root, args, timeout):
                seen["args"] = args
                seen["timeout"] = timeout
                return Proc()

            runner.run = fake_run  # type: ignore[method-assign]
            runner.search(Path(raw), "lex: a\nvec: a", mode="query", limit=10)
            self.assertEqual(seen["args"][0], "query")
            self.assertIn("--no-rerank", seen["args"])
            self.assertEqual(seen["timeout"], 20)

    def test_rerank_true_omits_no_rerank(self):
        with tempfile.TemporaryDirectory() as raw:
            settings = support.test_settings(Path(raw))
            runner = QmdRunner(settings)
            seen: dict = {}

            class Proc:
                stdout = "[]"
                stderr = ""
                returncode = 0

            def fake_run(user_root, args, timeout):
                seen["args"] = args
                return Proc()

            runner.run = fake_run  # type: ignore[method-assign]
            runner.search(Path(raw), "lex: a\nvec: a", mode="query", rerank=True, limit=10)
            self.assertNotIn("--no-rerank", seen["args"])

    def test_keyword_search_uses_keyword_timeout(self):
        with tempfile.TemporaryDirectory() as raw:
            settings = support.test_settings(
                Path(raw), search_timeout_seconds=20, keyword_search_timeout_seconds=30
            )
            runner = QmdRunner(settings)
            seen: dict = {}

            class Proc:
                stdout = "[]"
                stderr = ""
                returncode = 0

            def fake_run(user_root, args, timeout):
                seen["timeout"] = timeout
                seen["args"] = args
                return Proc()

            runner.run = fake_run  # type: ignore[method-assign]
            runner.search(Path(raw), "hello", mode="search", limit=10)
            self.assertEqual(seen["timeout"], 30)
            self.assertNotIn("--no-rerank", seen["args"])
