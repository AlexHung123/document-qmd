from __future__ import annotations

import sys
from typing import Any

from .util import utcnow_iso


def log(message: str, **fields: Any) -> None:
    extras = " ".join(f"{key}={_fmt(value)}" for key, value in fields.items())
    line = f"{utcnow_iso()} {message}"
    if extras:
        line += " " + extras
    print(line, file=sys.stderr, flush=True)


def _fmt(value: Any) -> str:
    text = str(value)
    if any(ch.isspace() for ch in text):
        return json_quote(text)
    return text


def json_quote(text: str) -> str:
    import json

    return json.dumps(text, ensure_ascii=False)
