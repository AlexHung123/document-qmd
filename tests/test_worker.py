from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

import support
from dochub.paths import markdown_dir


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.app, self.worker, self.qmd = support.make_app(self.data)

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def _upload(self, filename="note.md", content="%PDF-fake", markdown="# Hello search term\n", relative=None):
        body = {
            "filename": filename,
            "content_base64": base64.b64encode(content.encode() if isinstance(content, str) else content).decode(),
            "markdown": markdown,
        }
        if relative is not None:
            body["relative_path"] = relative
        headers = {"X-User-Id": "alice", "Content-Type": "application/json"}
        return self.app.handle("POST", "/v1/files", headers, json.dumps(body).encode())

    def test_upload_writes_markdown_then_worker_indexes(self):
        resp = self._upload(relative="docs/note.pdf")
        self.assertEqual(resp.status, 202)
        payload = json.loads(resp.body)
        self.assertEqual(payload["task"]["status"], "pending")
        self.assertEqual(payload["task"]["stage"], "qmd_update")
        md = markdown_dir(self.data / "users" / "alice") / "docs" / "note.md"
        self.assertTrue(md.is_file())
        self.assertIn("Hello search term", md.read_text(encoding="utf-8"))
        self.assertTrue(self.worker.process_one())
        self.assertEqual([c[0] for c in self.qmd.calls if c[0] != "init"], ["update", "embed"])
        task = self.app.store.get_task(payload["task"]["id"])
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.stage, "qmd_embed")

    def test_resume_from_qmd_embed(self):
        resp = self._upload()
        payload = json.loads(resp.body)
        task_id = payload["task"]["id"]
        claimed = self.app.store.claim()
        self.app.store.set_stage(task_id, "qmd_embed")
        self.app.store.recover_running()
        self.qmd.calls.clear()
        self.assertTrue(self.worker.process_one())
        self.assertEqual([c[0] for c in self.qmd.calls if c[0] != "init"], ["embed"])
        self.assertEqual(self.app.store.get_task(task_id).status, "succeeded")
        self.assertEqual(claimed.stage, "qmd_update")

    def test_retry_on_qmd_failure(self):
        self.qmd.fail_update = True
        self._upload()
        self.assertTrue(self.worker.process_one())
        tasks = self.app.store.list_tasks("alice")
        self.assertEqual(tasks[0].status, "pending")
        self.assertEqual(tasks[0].attempts, 1)
        self.assertEqual(tasks[0].stage, "qmd_update")

    def test_users_do_not_share_qmd_calls(self):
        body = json.dumps(
            {
                "filename": "a.pdf",
                "content_base64": base64.b64encode(b"%PDF").decode(),
                "markdown": "# a",
            }
        ).encode()
        self.app.handle("POST", "/v1/files", {"X-User-Id": "alice", "Content-Type": "application/json"}, body)
        self.app.handle("POST", "/v1/files", {"X-User-Id": "bob", "Content-Type": "application/json"}, body)
        self.worker.process_one()
        self.worker.process_one()
        update_roots = [c[1] for c in self.qmd.calls if c[0] == "update"]
        self.assertEqual(len(set(update_roots)), 2)
