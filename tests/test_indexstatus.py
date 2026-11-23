from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support
from dochub.indexstatus import (
    build_index_status,
    qmd_semantic_ready,
    semantic_search_ready,
)
from dochub.queue import ControlStore


class QmdSemanticReadyTests(unittest.TestCase):
    def test_official_ready_payload(self):
        self.assertTrue(
            qmd_semantic_ready(
                {"totalDocuments": 3, "needsEmbedding": 0, "hasVectorIndex": True, "collections": []}
            )
        )

    def test_pending_embeddings_are_unready(self):
        self.assertFalse(
            qmd_semantic_ready({"totalDocuments": 3, "needsEmbedding": 2, "hasVectorIndex": True})
        )

    def test_missing_vector_index_with_docs_is_unready(self):
        self.assertFalse(
            qmd_semantic_ready({"totalDocuments": 3, "needsEmbedding": 0, "hasVectorIndex": False})
        )

    def test_empty_official_index_is_ready(self):
        self.assertTrue(
            qmd_semantic_ready({"totalDocuments": 0, "needsEmbedding": 0, "hasVectorIndex": False})
        )

    def test_snake_case_and_inconclusive(self):
        self.assertTrue(
            qmd_semantic_ready({"total_documents": 1, "needs_embedding": 0, "has_vector_index": True})
        )
        self.assertIsNone(qmd_semantic_ready({"available": True, "collections": [{"name": "markdown"}]}))
        self.assertIsNone(qmd_semantic_ready(None))


class SemanticSearchReadyTests(unittest.TestCase):
    def test_empty_library_is_ready(self):
        self.assertTrue(
            semantic_search_ready(
                empty_library=True,
                update_pending=1,
                embed_pending=1,
                qmd_status={"needsEmbedding": 4, "hasVectorIndex": False},
            )
        )

    def test_pending_queue_blocks_even_if_qmd_ready(self):
        ready_status = {"totalDocuments": 2, "needsEmbedding": 0, "hasVectorIndex": True}
        self.assertFalse(
            semantic_search_ready(
                empty_library=False, update_pending=1, embed_pending=0, qmd_status=ready_status
            )
        )
        self.assertFalse(
            semantic_search_ready(
                empty_library=False, update_pending=0, embed_pending=1, qmd_status=ready_status
            )
        )

    def test_idle_queue_follows_qmd_progress(self):
        self.assertTrue(
            semantic_search_ready(
                empty_library=False,
                update_pending=0,
                embed_pending=0,
                qmd_status={"totalDocuments": 2, "needsEmbedding": 0, "hasVectorIndex": True},
            )
        )
        self.assertFalse(
            semantic_search_ready(
                empty_library=False,
                update_pending=0,
                embed_pending=0,
                qmd_status={"totalDocuments": 2, "needsEmbedding": 1, "hasVectorIndex": True},
            )
        )

    def test_historical_failure_does_not_block_when_qmd_inconclusive(self):
        self.assertTrue(
            semantic_search_ready(
                empty_library=False,
                update_pending=0,
                embed_pending=0,
                qmd_status={"available": True, "collections": [{"name": "markdown"}]},
            )
        )


class IndexStatusBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ControlStore(self.root / "control.sqlite")
        self.user_root = self.root / "users" / "alice"
        self.qmd = support.FakeQmd()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _ingest(self, name: str):
        return self.store.insert_upload(
            user_id="alice",
            original_filename=name,
            content_type="text/markdown",
            sha256=name,
            size_bytes=3,
            inbox_relpath=name,
            markdown_relpath=str(Path(name).with_suffix(".md")),
            max_attempts=3,
            payload={},
        )

    def test_empty_library_is_ready(self):
        status = build_index_status(self.store, "alice", self.user_root, self.qmd)
        self.assertTrue(status["keyword_search_ready"])
        self.assertTrue(status["semantic_search_ready"])
        self.assertIsNone(status["hint"])

    def test_pending_ingest_is_not_semantically_ready(self):
        self._ingest("note.md")
        status = build_index_status(self.store, "alice", self.user_root, self.qmd)
        self.assertFalse(status["keyword_search_ready"])
        self.assertFalse(status["semantic_search_ready"])
        self.assertIn("keyword search", status["hint"].lower())

    def test_historical_failed_task_does_not_pin_library_unready(self):
        _, failed = self._ingest("old.md")
        claimed = self.store.claim()
        self.assertEqual(claimed.id, failed.id)
        self.store.fail(failed.id, "embed boom", permanent=True)

        _, later = self._ingest("new.md")
        claimed = self.store.claim()
        self.assertEqual(claimed.id, later.id)
        self.store.set_stage(later.id, "qmd_embed")
        self.store.succeed(later.id)

        self.qmd.status_payload = {
            "totalDocuments": 2,
            "needsEmbedding": 0,
            "hasVectorIndex": True,
            "collections": [{"name": "markdown", "documents": 2}],
        }
        status = build_index_status(self.store, "alice", self.user_root, self.qmd)
        self.assertTrue(status["keyword_search_ready"])
        self.assertTrue(status["semantic_search_ready"])
        self.assertIsNone(status["hint"])
        self.assertEqual(status["ingest"]["failed"], 1)
        self.assertEqual(status["ingest"]["succeeded"], 1)
        self.assertEqual(status["ingest"]["update_pending"], 0)
        self.assertEqual(status["ingest"]["embed_pending"], 0)

    def test_idle_queue_stays_unready_when_qmd_still_needs_embeddings(self):
        _, failed = self._ingest("old.md")
        claimed = self.store.claim()
        self.store.fail(claimed.id, "embed boom", permanent=True)
        self.qmd.status_payload = {
            "totalDocuments": 1,
            "needsEmbedding": 1,
            "hasVectorIndex": False,
            "collections": [{"name": "markdown", "documents": 1}],
        }
        status = build_index_status(self.store, "alice", self.user_root, self.qmd)
        self.assertTrue(status["keyword_search_ready"])
        self.assertFalse(status["semantic_search_ready"])
        self.assertEqual(status["ingest"]["failed"], 1)
        self.assertIn("keyword search", status["hint"].lower())
