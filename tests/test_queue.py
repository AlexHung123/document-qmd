from __future__ import annotations

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
