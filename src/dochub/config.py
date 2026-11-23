from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATA_ROOT = Path.home() / "Documents" / "dochub"
DEFAULT_PORT = 8787
DEFAULT_BIND = "127.0.0.1"
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class Settings:
    data_root: Path
    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    qmd_bin: str = "qmd"
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_attempts: int = 5
    worker_poll_seconds: float = 0.4
    update_timeout_seconds: int = 300
    embed_timeout_seconds: int = 1800
    search_timeout_seconds: int = 180

    @property
    def users_root(self) -> Path:
        return self.data_root / "users"

    @property
    def queue_db(self) -> Path:
        return self.data_root / "queue" / "control.sqlite"

    @classmethod
    def from_env(cls) -> Settings:
        data_root = Path(os.environ.get("DOCHUB_DATA_ROOT") or DEFAULT_DATA_ROOT).expanduser()
        return cls(
            data_root=data_root.resolve() if data_root.exists() else data_root,
            bind=os.environ.get("DOCHUB_BIND") or DEFAULT_BIND,
            port=_env_int("DOCHUB_PORT", DEFAULT_PORT),
            qmd_bin=os.environ.get("DOCHUB_QMD_BIN") or "qmd",
            max_upload_bytes=_env_int("DOCHUB_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES),
            max_attempts=_env_int("DOCHUB_MAX_ATTEMPTS", 5),
            worker_poll_seconds=_env_float("DOCHUB_WORKER_POLL_SECONDS", 0.4),
            update_timeout_seconds=_env_int("DOCHUB_UPDATE_TIMEOUT", 300),
            embed_timeout_seconds=_env_int("DOCHUB_EMBED_TIMEOUT", 1800),
            search_timeout_seconds=_env_int("DOCHUB_SEARCH_TIMEOUT", 180),
        )
