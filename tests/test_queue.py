from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import support
from dochub.queue import ControlStore, backoff_seconds


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ControlStore(Path(self.tmp.name) / "control.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _ingest(self, user: str, name: str = "a.md"):
        return self.store.insert_upload(
            user_id=user,
            original_filename=name,
            content_type="text/markdown",
            sha256="abc",
            size_bytes=3,
            inbox_relpath=f"{user}/{name}",
            markdown_relpath=f"{user}/{Path(name).with_suffix('.md')}",
            max_attempts=3,
            payload={},
        )

    def test_claim_serial_per_user(self):
        self._ingest("alice", "one.md")
        self._ingest("alice", "two.md")
        self._ingest("bob", "one.md")
        first = self.store.claim()
        self.assertIsNotNone(first)
        second = self.store.claim()
        self.assertIsNotNone(second)
        self.assertNotEqual(first.user_id, second.user_id)
        third = self.store.claim()
        self.assertIsNone(third)
        self.store.succeed(first.id)
        third = self.store.claim()
        self.assertIsNotNone(third)
        self.assertEqual(third.user_id, first.user_id)

    def _due_now(self, task_id: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET run_after='2000-01-01T00:00:00Z' WHERE id=?", (task_id,))

    def test_retry_then_fail(self):
        _, task = self._ingest("alice")
        claimed = self.store.claim()
        self.assertEqual(claimed.id, task.id)
        again = self.store.fail(claimed.id, "boom", permanent=False)
        self.assertEqual(again.status, "pending")
        self.assertEqual(again.attempts, 1)
        self._due_now(again.id)
        claimed = self.store.claim()
        self.store.fail(claimed.id, "boom", permanent=False)
        self._due_now(claimed.id)
        claimed = self.store.claim()
        terminal = self.store.fail(claimed.id, "boom", permanent=False)
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.attempts, 3)
        self.assertIsNone(self.store.claim())

    def test_permanent_fail_skips_retry(self):
        self._ingest("alice")
        claimed = self.store.claim()
        terminal = self.store.fail(claimed.id, "unsupported", permanent=True)
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.attempts, 1)

    def test_recover_running_keeps_stage(self):
        self._ingest("alice")
        claimed = self.store.claim()
        self.store.set_stage(claimed.id, "qmd_update")
        n = self.store.recover_running()
        self.assertEqual(n, 1)
        row = self.store.get_task(claimed.id)
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.stage, "qmd_update")
        self.assertEqual(row.attempts, 0)

    def test_backoff(self):
        self.assertEqual(backoff_seconds(1), 5)
        self.assertEqual(backoff_seconds(2), 15)
        self.assertEqual(backoff_seconds(5), 300)

    def test_migrates_markdown_size_bytes_column(self):
        path = Path(self.tmp.name) / "legacy.sqlite"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE files (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              original_filename TEXT NOT NULL,
              content_type TEXT,
              sha256 TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              inbox_relpath TEXT NOT NULL,
              markdown_relpath TEXT,
              provided_markdown_relpath TEXT,
              created_at TEXT NOT NULL,
              task_id TEXT
            );
            """
        )
        db.execute(
            """
            INSERT INTO files(
              id, user_id, original_filename, content_type, sha256, size_bytes,
              inbox_relpath, markdown_relpath, provided_markdown_relpath, created_at, task_id
            ) VALUES ('legacy', 'alice', 'a.md', 'text/markdown', 'abc', 3, 'a.md', 'a.md', NULL, '2024-01-01T00:00:00Z', NULL)
            """
        )
        db.commit()
        db.close()
        store = ControlStore(path)
        self.addCleanup(store.close)
        row = store.get_file("legacy")
        self.assertIsNotNone(row)
        self.assertIsNone(row.markdown_size_bytes)
        store.set_markdown_size_bytes("legacy", 12)
        self.assertEqual(store.get_file("legacy").markdown_size_bytes, 12)

    def test_insert_registered_file_stores_markdown_size(self):
        row = self.store.insert_registered_file(
            user_id="alice",
            original_filename="a.md",
            content_type="text/markdown",
            sha256="abc",
            size_bytes=3,
            inbox_relpath="a.md",
            markdown_relpath="a.md",
            markdown_size_bytes=11,
        )
        self.assertEqual(row.markdown_size_bytes, 11)
        self.assertEqual(self.store.get_file(row.id).markdown_size_bytes, 11)
