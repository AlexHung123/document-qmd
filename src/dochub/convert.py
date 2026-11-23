from __future__ import annotations

import json
import re


def merge_front_matter(body: str, meta: dict[str, str]) -> str:
    existing: dict[str, str] = {}
    rest = body
    if body.startswith("---"):
        match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", body, re.DOTALL)
        if match:
            for line in match.group(1).splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                existing[key.strip()] = value.strip().strip('"').strip("'")
            rest = match.group(2)
    existing.update(meta)
    lines = ["---"]
    for key, value in existing.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + rest.lstrip("\n")
