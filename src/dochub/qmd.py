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
    def __init__(
        self,
        message: str,
        *,
        permanent: bool = False,
        output: str = "",
        reason: str = "qmd_exec_failed",
    ):
        super().__init__(message)
        self.permanent = permanent
        self.output = output
        self.reason = reason


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
        # Official qmd honors INDEX_PATH, QMD_CONFIG_DIR, and XDG_CONFIG_HOME.
        # Drop host/process values so this user's project-local .qmd is used.
        env.pop("INDEX_PATH", None)
        env.pop("QMD_CONFIG_DIR", None)
        env.pop("XDG_CONFIG_HOME", None)
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
            raise QmdError(f"qmd binary not found: {self.settings.qmd_bin}", permanent=True, reason="qmd_not_found") from exc
        except subprocess.TimeoutExpired as exc:
            raise QmdError(f"qmd {' '.join(args[:2])} timed out after {timeout}s", permanent=False, reason="timeout") from exc
        if proc.returncode != 0:
            detail = truncate((proc.stderr or "") + "\n" + (proc.stdout or ""))
            raise QmdError(f"qmd {' '.join(args[:2])} failed: {detail}", permanent=False, output=detail, reason="qmd_exec_failed")
        return proc

    def _try_run(self, user_root: Path, attempts: list[list[str]], timeout: float) -> subprocess.CompletedProcess[str] | None:
        last: QmdError | None = None
        for args in attempts:
            try:
                return self.run(user_root, args, timeout=timeout)
            except QmdError as exc:
                last = exc
                continue
        if last is not None:
            return None
        return None

    def _parse_json(self, text: str) -> Any:
        text = text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

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
        rerank: bool = False,
        candidate_limit: int | None = None,
    ) -> Any:
        if mode not in {"search", "vsearch", "query"}:
            raise QmdError(f"unknown search mode {mode}", permanent=True, reason="invalid_query")
        args = [mode, "--format", "json", "-n", str(limit), "-c", "markdown"]
        if min_score is not None:
            args += ["--min-score", str(min_score)]
        if full:
            args += ["--full"]
        # --no-rerank and --candidate-limit are qmd query flags (RRF / rerank).
        if mode == "query":
            if not rerank:
                args += ["--no-rerank"]
            if candidate_limit is not None:
                args += ["--candidate-limit", str(candidate_limit)]
        args += ["--", query]
        if mode == "search":
            timeout = self.settings.keyword_search_timeout_seconds
        else:
            timeout = self.settings.search_timeout_seconds
        proc = self.run(user_root, args, timeout=timeout)
        text = proc.stdout.strip() or "[]"
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise QmdError(
                "qmd returned non-JSON search output",
                permanent=False,
                output=truncate(text),
                reason="parse_failed",
            ) from exc

    def status(self, user_root: Path) -> Any:
        """Index health from `qmd status`. JSON if the installed qmd supports it."""
        proc = self._try_run(
            user_root,
            [["status", "--format", "json"], ["status", "--json"], ["status"]],
            timeout=30,
        )
        if proc is None:
            return None
        parsed = self._parse_json(proc.stdout)
        if parsed is not None:
            return parsed
        text = (proc.stdout or "").strip()
        return {"raw": text} if text else None

    def context_list(self, user_root: Path) -> Any:
        """Contexts from `qmd context list`, if the CLI supports JSON output."""
        proc = self._try_run(
            user_root,
            [
                ["context", "list", "--format", "json"],
                ["context", "list", "--json"],
                ["context", "list"],
            ],
            timeout=30,
        )
        if proc is None:
            return None
        parsed = self._parse_json(proc.stdout)
        if parsed is not None:
            return parsed
        text = (proc.stdout or "").strip()
        return {"raw": text} if text else None

    def resolve_docid(self, user_root: Path, docid: str) -> dict[str, Any] | None:
        """Resolve `#abc123` via `qmd multi-get --format json` or `qmd get`.

        qmd get has no documented --format json; multi-get does.
        """
        token = docid if docid.startswith("#") else f"#{docid}"
        proc = self._try_run(
            user_root,
            [
                ["multi-get", "--format", "json", "--", token],
                ["multi-get", "--json", "--", token],
            ],
            timeout=30,
        )
        if proc is not None:
            parsed = self._parse_json(proc.stdout)
            item = _first_doc_payload(parsed)
            if item:
                return item
        proc = self._try_run(user_root, [["get", "--", token]], timeout=30)
        if proc is None:
            return None
        return _parse_get_cli(proc.stdout, token)

    def available(self) -> bool:
        if self.settings.qmd_bin.endswith(".py"):
            return Path(self.settings.qmd_bin).is_file()
        return shutil.which(self.settings.qmd_bin) is not None


def _first_doc_payload(raw: Any) -> dict[str, Any] | None:
    items: list[Any]
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for key in ("documents", "results", "files"):
            value = raw.get(key)
            if isinstance(value, list):
                items = value
                break
        else:
            items = [raw]
    else:
        return None
    for item in items:
        if isinstance(item, dict):
            return item
        if isinstance(item, str) and item.strip():
            return {"file": item.strip()}
    return None


def _parse_get_cli(text: str, docid: str) -> dict[str, Any] | None:
    if not text or not text.strip():
        return None
    first = text.strip().splitlines()[0].strip()
    # Typical header: `qmd://markdown/contracts/a.md  #abc123`
    path = first
    found_id = docid
    if "  " in first:
        left, right = first.split("  ", 1)
        path, found_id = left.strip(), right.strip() or docid
    elif " " in first and first.startswith("qmd://"):
        left, right = first.split(" ", 1)
        path, found_id = left.strip(), right.strip() or docid
    if not path:
        return None
    return {"file": path, "docid": found_id, "raw": text}
