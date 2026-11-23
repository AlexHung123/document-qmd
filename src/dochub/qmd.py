from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import Settings
from .util import truncate


class QmdError(Exception):
    def __init__(self, message: str, *, permanent: bool = False, output: str = ""):
        super().__init__(message)
        self.permanent = permanent
        self.output = output


class QmdRunner:
    """Run the qmd CLI inside a user's root so the project-local .qmd index is used.

    Search is executed here on demand. This process never starts `qmd mcp`.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def _command(self, args: list[str]) -> list[str]:
        binary = self.settings.qmd_bin
        if binary.endswith(".py"):
            import sys

            return [sys.executable, binary, *args]
        return [binary, *args]

    def _env(self, user_root: Path) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("INDEX_PATH", None)
        env["QMD_TRUST_LOCAL_CONFIG"] = "1"
        # qmd init uses process.env.PWD || process.cwd(). Inherit PWD from the
        # parent shell and it writes the index into the repo instead of the user root.
        env["PWD"] = str(user_root)
        return env

    def run(self, user_root: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        cmd = self._command(args)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(user_root),
                env=self._env(user_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise QmdError(f"qmd binary not found: {self.settings.qmd_bin}", permanent=True) from exc
        except subprocess.TimeoutExpired as exc:
            raise QmdError(f"qmd {' '.join(args[:2])} timed out after {timeout}s", permanent=False) from exc
        if proc.returncode != 0:
            detail = truncate((proc.stderr or "") + "\n" + (proc.stdout or ""))
            raise QmdError(f"qmd {' '.join(args[:2])} failed: {detail}", permanent=False, output=detail)
        return proc

    def init_index(self, user_root: Path) -> None:
        self.run(user_root, ["init"], timeout=60)

    def update(self, user_root: Path) -> str:
        proc = self.run(user_root, ["update"], timeout=self.settings.update_timeout_seconds)
        return proc.stdout

    def embed(self, user_root: Path) -> str:
        proc = self.run(
            user_root,
            ["embed", "-c", "markdown"],
            timeout=self.settings.embed_timeout_seconds,
        )
        return proc.stdout

    def search(
        self,
        user_root: Path,
        query: str,
        *,
        mode: str = "search",
        limit: int = 10,
        min_score: float | None = None,
        full: bool = False,
    ) -> Any:
        if mode not in {"search", "vsearch", "query"}:
            raise QmdError(f"unknown search mode {mode}", permanent=True)
        args = [mode, "--format", "json", "-n", str(limit), "-c", "markdown"]
        if min_score is not None:
            args += ["--min-score", str(min_score)]
        if full:
            args += ["--full"]
        args += ["--", query]
        timeout = self.settings.search_timeout_seconds
        if mode == "search":
            timeout = min(timeout, 30)
        proc = self.run(user_root, args, timeout=timeout)
        text = proc.stdout.strip() or "[]"
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise QmdError("qmd returned non-JSON search output", permanent=False, output=truncate(text)) from exc

    def available(self) -> bool:
        if self.settings.qmd_bin.endswith(".py"):
            return Path(self.settings.qmd_bin).is_file()
        return shutil.which(self.settings.qmd_bin) is not None
