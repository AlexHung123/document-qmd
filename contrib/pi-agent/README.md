# Dochub + pi-agent-core

Per-user document search for a hosted Pi agent. The Mac service stays the only process that talks to QMD. Each Pi session gets a Dochub MCP stdio child bound to one `X-User-Id` and an optional folder pin.

Do not start `qmd mcp`. That server has no user isolation.

## Layout

```
contrib/pi-agent/
  README.md
  mcp.json.example
  session.mjs.example
  skills/dochub/SKILL.md
```

## Host rules

1. After your frontend authenticates the user, spawn MCP with `DOCHUB_USER_ID` from that session. Never take `user_id` from the model.
2. To pin a folder (the UI's current directory), set `DOCHUB_FOLDER` to a markdown-relative path such as `contracts/2024`.
3. Point `DOCHUB_URL` at the Mac Dochub HTTP API (`http://<mac>:8787` when Pi runs on another host).
4. Load `skills/dochub/SKILL.md` into the agent's system prompt (pi-agent-core has no skill loader of its own). Pi CLI can discover the same file from a skills directory.

## Environment

| Variable | Required | Meaning |
|----------|----------|---------|
| `DOCHUB_URL` | no | HTTP API, default `http://127.0.0.1:8787` |
| `DOCHUB_USER_ID` | yes | Bound user; same value as `X-User-Id` |
| `DOCHUB_FOLDER` | no | Folder pin under that user's `markdown/` |
| `DOCHUB_TIMEOUT` | no | HTTP timeout seconds, default `180` |
| `PYTHONPATH` | if not installed | Path to `documents/src` |

```bash
PYTHONPATH=/path/to/documents/src \
DOCHUB_URL=http://127.0.0.1:8787 \
DOCHUB_USER_ID=alice \
DOCHUB_FOLDER=contracts/2024 \
python3 -m dochub mcp
```

CLI flags override env: `python3 -m dochub mcp --url ... --user alice --folder contracts/2024`.

## Pi CLI (`mcp.json`)

Copy `mcp.json.example`. Use `"exposure": "direct"` so `query` / `get` are declared to the model. Put the skill at `~/.pi/agent/skills/dochub/SKILL.md` or `.pi/skills/dochub/SKILL.md`.

A static `mcp.json` cannot isolate users. For a multi-user server, spawn MCP per session as in `session.mjs.example`.

## pi-agent-core

`session.mjs.example` shows:

1. `StdioClientTransport` with per-session `env`
2. MCP tools wrapped as `AgentTool`
3. `SKILL.md` prepended to `systemPrompt`

The child speaks MCP stdio (Content-Length framing, also accepts one JSON object per line). Logs go to stderr.

## Tools

| Tool | Role |
|------|------|
| `query` | Search (BM25 / semantic / hybrid). Optional `path` further scopes inside the pin. |
| `get` | Read one markdown file by `dochub_path` (`file:from:count` supported) |
| `multi_get` | Several paths or a glob |
| `tree` | Inbox folder tree |
| `status` | Health, bound user, pin |

Search still hits Dochub `POST /v1/search` (not the ingest queue). `get` reads `GET /v1/documents`.
