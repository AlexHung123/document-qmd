from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dochub.config import Settings  # noqa: E402
from dochub.httpapi import App  # noqa: E402
from dochub.queue import ControlStore  # noqa: E402
from dochub.worker import Worker  # noqa: E402


class FakeQmd:
    def __init__(self):
        self.calls: list[tuple] = []
        self.search_results = [{"file": "qmd://markdown/note.md", "title": "note", "snippet": "hi", "score": 1}]
        self.fail_update = False
        self.fail_embed = False

    def init_index(self, user_root: Path) -> None:
        self.calls.append(("init", str(user_root)))
        qmd = user_root / ".qmd"
        qmd.mkdir(parents=True, exist_ok=True)
        sqlite = qmd / "index.sqlite"
        if not sqlite.exists():
            sqlite.write_bytes(b"")

    def update(self, user_root: Path) -> str:
        self.calls.append(("update", str(user_root)))
        if self.fail_update:
            from dochub.qmd import QmdError

            raise QmdError("update failed", permanent=False)
        return "updated"

    def embed(self, user_root: Path) -> str:
        self.calls.append(("embed", str(user_root)))
        if self.fail_embed:
            from dochub.qmd import QmdError

            raise QmdError("embed failed", permanent=False)
        return "embedded"

    def search(self, user_root: Path, query: str, **kwargs):
        self.calls.append(("search", str(user_root), query, kwargs))
        mode = kwargs.get("mode", "search")
        errors = getattr(self, "search_errors", None) or {}
        if mode in errors:
            err = errors[mode]
            raise err() if callable(err) else err
        results = (getattr(self, "search_results_by_mode", None) or {}).get(mode, self.search_results)
        return [{**item, "cwd": user_root.name, "query": query, "mode": mode} for item in results]

    def status(self, user_root: Path):
        self.calls.append(("status", str(user_root)))
        return getattr(self, "status_payload", {"available": True, "collections": [{"name": "markdown"}]})

    def context_list(self, user_root: Path):
        self.calls.append(("context_list", str(user_root)))
        return getattr(self, "contexts", None)

    def resolve_docid(self, user_root: Path, docid: str):
        self.calls.append(("resolve_docid", str(user_root), docid))
        mapping = getattr(self, "docids", {}) or {}
        key = docid if docid.startswith("#") else f"#{docid}"
        return mapping.get(key) or mapping.get(docid)

    def available(self) -> bool:
        return True


def test_settings(data_root: Path, **overrides) -> Settings:
    values = dict(
        data_root=data_root,
        bind="127.0.0.1",
        port=0,
        qmd_bin="qmd",
        max_upload_bytes=1024 * 1024,
        max_attempts=3,
        worker_poll_seconds=0.01,
        update_timeout_seconds=5,
        embed_timeout_seconds=5,
        search_timeout_seconds=5,
    )
    values.update(overrides)
    return Settings(**values)


def make_app(data_root: Path, qmd=None, **overrides) -> tuple[App, Worker, FakeQmd]:
    settings = test_settings(data_root, **overrides)
    store = ControlStore(settings.queue_db)
    fake = qmd if qmd is not None else FakeQmd()
    app = App(settings, store, fake)
    worker = Worker(settings, store, fake)
    return app, worker, fake
