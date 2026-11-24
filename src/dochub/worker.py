from __future__ import annotations

import threading

from .config import Settings
from .layout import ensure_public_tree, ensure_user_tree
from .library import is_public_owner
from .log import log
from .qmd import QmdError, QmdRunner
from .queue import ControlStore, TaskRow


class PermanentError(Exception):
    pass


class Worker:
    def __init__(self, settings: Settings, store: ControlStore, qmd: QmdRunner):
        self.settings = settings
        self.store = store
        self.qmd = qmd

    def recover(self) -> int:
        n = self.store.recover_running()
        if n:
            log("recovered running tasks", count=n)
        return n

    def process_one(self) -> bool:
        task = self.store.claim()
        if task is None:
            return False
        log("task claimed", task_id=task.id, user_id=task.user_id, stage=task.stage)
        try:
            self.run_stages(task)
            self.store.succeed(task.id)
            log("task succeeded", task_id=task.id, user_id=task.user_id)
        except (PermanentError, QmdError) as exc:
            permanent = isinstance(exc, PermanentError) or (isinstance(exc, QmdError) and exc.permanent)
            updated = self.store.fail(task.id, str(exc), permanent=permanent)
            log(
                "task failed",
                task_id=task.id,
                user_id=task.user_id,
                status=updated.status,
                attempts=updated.attempts,
                error=str(exc),
            )
        except Exception as exc:
            updated = self.store.fail(task.id, str(exc), permanent=False)
            log(
                "task crashed",
                task_id=task.id,
                user_id=task.user_id,
                status=updated.status,
                attempts=updated.attempts,
                error=str(exc),
            )
        return True

    def run_stages(self, task: TaskRow) -> None:
        if task.kind != "ingest":
            raise PermanentError(f"unknown task kind {task.kind}")
        if not task.file_id:
            raise PermanentError("ingest task missing file_id")
        file_row = self.store.get_file(task.file_id)
        if file_row is None:
            raise PermanentError(f"file {task.file_id} not found")
        if not file_row.markdown_relpath:
            raise PermanentError("markdown missing; upload must include markdown")
        if is_public_owner(task.user_id):
            root = ensure_public_tree(self.settings.data_root, self.qmd)
        else:
            root = ensure_user_tree(self.settings.data_root, task.user_id, self.qmd)
        if task.stage == "convert":
            self.store.set_stage(task.id, "qmd_update")
            task.stage = "qmd_update"
        if task.stage == "qmd_update":
            self.qmd.update(root)
            self.store.set_stage(task.id, "qmd_embed")
            task.stage = "qmd_embed"
        if task.stage == "qmd_embed":
            self.qmd.embed(root)


def worker_loop(worker: Worker, stop: threading.Event) -> None:
    worker.recover()
    log("worker started")
    while not stop.is_set():
        try:
            worked = worker.process_one()
        except Exception as exc:
            log("worker loop error", error=str(exc))
            worked = False
        if not worked:
            stop.wait(worker.settings.worker_poll_seconds)
    log("worker stopped")
