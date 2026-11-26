from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .util import json_dumps, new_id, utcnow_iso

STATUSES = ("pending", "running", "succeeded", "failed")
STAGES = ("qmd_update", "qmd_embed")


class DuplicateInboxPath(Exception):
    """(user_id, inbox_relpath) is already registered."""

    def __init__(self, user_id: str, inbox_relpath: str):
        super().__init__(f"file already exists at {inbox_relpath}")
        self.user_id = user_id
        self.inbox_relpath = inbox_relpath


@dataclass
class FileRow:
    id: str
    user_id: str
    original_filename: str
    content_type: str | None
    sha256: str
    size_bytes: int
    inbox_relpath: str
    markdown_relpath: str | None
    provided_markdown_relpath: str | None
    markdown_size_bytes: int | None
    created_at: str
    task_id: str | None


@dataclass
class TaskRow:
    id: str
    user_id: str
    kind: str
    file_id: str | None
    status: str
    stage: str
    attempts: int
    max_attempts: int
    run_after: str
    last_error: str | None
    payload: dict[str, Any]
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


def backoff_seconds(attempts: int) -> int:
    return min(300, 5 * (3 ** max(0, attempts - 1)))


def _row_file(row: sqlite3.Row) -> FileRow:
    return FileRow(
        id=row["id"],
        user_id=row["user_id"],
        original_filename=row["original_filename"],
        content_type=row["content_type"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        inbox_relpath=row["inbox_relpath"],
        markdown_relpath=row["markdown_relpath"],
        provided_markdown_relpath=row["provided_markdown_relpath"],
        markdown_size_bytes=row["markdown_size_bytes"] if "markdown_size_bytes" in row.keys() else None,
        created_at=row["created_at"],
        task_id=row["task_id"],
    )


def _row_task(row: sqlite3.Row) -> TaskRow:
    payload = json.loads(row["payload"] or "{}")
    return TaskRow(
        id=row["id"],
        user_id=row["user_id"],
        kind=row["kind"],
        file_id=row["file_id"],
        status=row["status"],
        stage=row["stage"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        run_after=row["run_after"],
        last_error=row["last_error"],
        payload=payload,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class ControlStore:
    """Service control plane: upload registry + ingest task queue.

    Independent of each user's QMD SQLite index.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              original_filename TEXT NOT NULL,
              content_type TEXT,
              sha256 TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              inbox_relpath TEXT NOT NULL,
              markdown_relpath TEXT,
              provided_markdown_relpath TEXT,
              markdown_size_bytes INTEGER,
              created_at TEXT NOT NULL,
              task_id TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS files_user_inbox
              ON files(user_id, inbox_relpath);
            CREATE INDEX IF NOT EXISTS files_user_created
              ON files(user_id, created_at);

            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              kind TEXT NOT NULL DEFAULT 'ingest',
              file_id TEXT,
              status TEXT NOT NULL,
              stage TEXT NOT NULL DEFAULT 'convert',
              attempts INTEGER NOT NULL DEFAULT 0,
              max_attempts INTEGER NOT NULL DEFAULT 5,
              run_after TEXT NOT NULL,
              last_error TEXT,
              payload TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT,
              FOREIGN KEY(file_id) REFERENCES files(id)
            );
            CREATE INDEX IF NOT EXISTS tasks_claim
              ON tasks(status, run_after, created_at);
            CREATE INDEX IF NOT EXISTS tasks_user_status
              ON tasks(user_id, status);
            """
        )
        self._ensure_column("files", "markdown_size_bytes", "INTEGER")
        self._db.commit()

    def _ensure_column(self, table: str, name: str, decl: str) -> None:
        cols = {info[1] for info in self._db.execute(f"PRAGMA table_info({table})")}
        if name not in cols:
            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        begin = "BEGIN IMMEDIATE" if immediate else "BEGIN"
        with self._lock:
            self._db.execute(begin)
            try:
                yield self._db
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def recover_running(self) -> int:
        """On process start, unfinished running tasks become pending again at the same stage."""
        now = utcnow_iso()
        with self.transaction() as db:
            cur = db.execute(
                "UPDATE tasks SET status='pending', started_at=NULL, updated_at=? WHERE status='running'",
                (now,),
            )
            return cur.rowcount

    def insert_upload(
        self,
        *,
        user_id: str,
        original_filename: str,
        content_type: str | None,
        sha256: str,
        size_bytes: int,
        inbox_relpath: str,
        markdown_relpath: str,
        max_attempts: int,
        payload: dict[str, Any],
        file_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple[FileRow, TaskRow]:
        file_id = file_id or new_id()
        task_id = task_id or new_id()
        now = utcnow_iso()
        try:
            with self.transaction() as db:
                db.execute(
                    """
                    INSERT INTO files(
                      id, user_id, original_filename, content_type, sha256, size_bytes,
                      inbox_relpath, markdown_relpath, provided_markdown_relpath, created_at, task_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        file_id,
                        user_id,
                        original_filename,
                        content_type,
                        sha256,
                        size_bytes,
                        inbox_relpath,
                        markdown_relpath,
                        now,
                        task_id,
                    ),
                )
                db.execute(
                    """
                    INSERT INTO tasks(
                      id, user_id, kind, file_id, status, stage, attempts, max_attempts,
                      run_after, last_error, payload, created_at, updated_at, started_at, finished_at
                    ) VALUES (?, ?, 'ingest', ?, 'pending', 'qmd_update', 0, ?, ?, NULL, ?, ?, ?, NULL, NULL)
                    """,
                    (task_id, user_id, file_id, max_attempts, now, json_dumps(payload), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateInboxPath(user_id, inbox_relpath) from exc
        file_row = self.get_file(file_id)
        task_row = self.get_task(task_id)
        assert file_row and task_row
        return file_row, task_row

    def delete_upload(self, file_id: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM tasks WHERE file_id=?", (file_id,))
            db.execute("DELETE FROM files WHERE id=?", (file_id,))

    def insert_registered_file(
        self,
        *,
        user_id: str,
        original_filename: str,
        content_type: str | None,
        sha256: str,
        size_bytes: int,
        inbox_relpath: str,
        markdown_relpath: str | None,
        markdown_size_bytes: int | None = None,
        file_id: str | None = None,
    ) -> FileRow:
        """Register a file already on disk, without enqueueing an ingest task.

        Used by public-library seed so a large tree can be indexed once
        rather than once per file.
        """
        file_id = file_id or new_id()
        now = utcnow_iso()
        with self.transaction() as db:
            db.execute(
                """
                INSERT INTO files(
                  id, user_id, original_filename, content_type, sha256, size_bytes,
                  inbox_relpath, markdown_relpath, provided_markdown_relpath,
                  markdown_size_bytes, created_at, task_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)
                """,
                (
                    file_id,
                    user_id,
                    original_filename,
                    content_type,
                    sha256,
                    size_bytes,
                    inbox_relpath,
                    markdown_relpath,
                    markdown_size_bytes,
                    now,
                ),
            )
        row = self.get_file(file_id)
        assert row
        return row

    def get_file_by_inbox(self, user_id: str, inbox_relpath: str) -> FileRow | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM files WHERE user_id=? AND inbox_relpath=?",
                (user_id, inbox_relpath),
            ).fetchone()
        return _row_file(row) if row else None

    def get_file(self, file_id: str) -> FileRow | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return _row_file(row) if row else None

    def get_task(self, task_id: str) -> TaskRow | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _row_task(row) if row else None

    def list_files(self, user_id: str, limit: int = 100) -> list[FileRow]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM files WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [_row_file(r) for r in rows]

    def list_tasks(self, user_id: str | None = None, status: str | None = None, limit: int = 100) -> list[TaskRow]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        args: list[Any] = []
        if user_id:
            sql += " AND user_id=?"
            args.append(user_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [_row_task(r) for r in rows]

    def set_markdown_relpath(self, file_id: str, relpath: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE files SET markdown_relpath=? WHERE id=?", (relpath, file_id))

    def set_markdown_size_bytes(self, file_id: str, size_bytes: int) -> None:
        with self.transaction() as db:
            db.execute("UPDATE files SET markdown_size_bytes=? WHERE id=?", (size_bytes, file_id))

    def set_stage(self, task_id: str, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage}")
        now = utcnow_iso()
        with self.transaction() as db:
            db.execute("UPDATE tasks SET stage=?, updated_at=? WHERE id=?", (stage, now, task_id))

    def claim(self) -> TaskRow | None:
        """Oldest due pending task whose user has no running task."""
        now = utcnow_iso()
        with self.transaction(immediate=True) as db:
            row = db.execute(
                """
                SELECT * FROM tasks
                WHERE status='pending'
                  AND run_after<=?
                  AND user_id NOT IN (SELECT user_id FROM tasks WHERE status='running')
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE tasks SET status='running', started_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
            claimed = db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
        return _row_task(claimed) if claimed else None

    def succeed(self, task_id: str) -> None:
        now = utcnow_iso()
        with self.transaction() as db:
            db.execute(
                """
                UPDATE tasks
                SET status='succeeded', finished_at=?, updated_at=?, last_error=NULL
                WHERE id=?
                """,
                (now, now, task_id),
            )

    def fail(self, task_id: str, error: str, *, permanent: bool) -> TaskRow:
        now = utcnow_iso()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            attempts = int(row["attempts"]) + 1
            max_attempts = int(row["max_attempts"])
            terminal = permanent or attempts >= max_attempts
            if terminal:
                db.execute(
                    """
                    UPDATE tasks
                    SET status='failed', attempts=?, last_error=?, finished_at=?, updated_at=?, started_at=NULL
                    WHERE id=?
                    """,
                    (attempts, error, now, now, task_id),
                )
            else:
                delay = backoff_seconds(attempts)
                from datetime import datetime, timedelta, timezone

                run_after = (datetime.now(timezone.utc) + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
                db.execute(
                    """
                    UPDATE tasks
                    SET status='pending', attempts=?, last_error=?, run_after=?, updated_at=?, started_at=NULL
                    WHERE id=?
                    """,
                    (attempts, error, run_after, now, task_id),
                )
            updated = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _row_task(updated)

    def counts(self, user_id: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM tasks"
        args: list[Any] = []
        if user_id:
            sql += " WHERE user_id=?"
            args.append(user_id)
        sql += " GROUP BY status"
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = {s: 0 for s in STATUSES}
        for row in rows:
            out[row["status"]] = int(row["n"])
        return out
