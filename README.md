# dochub — Mac 檔案同索引中心

前端負責登入同權限，亦負責把原檔轉成 Markdown。本服務跑喺 Mac，按轉發過來嘅用戶身份落盤、維護每位用戶自己嘅 QMD 索引，同代查搜尋。唔會開 `qmd mcp`，前端唔好直連 QMD。

## 固定目錄

預設資料根：`~/Documents/dochub`（可用 `DOCHUB_DATA_ROOT` 改）。

```
~/Documents/dochub/
  queue/control.sqlite          # 上傳登記 + 落盤任務隊列（同 QMD 庫分開）
  users/<user_id>/
    inbox/                      # 原檔
    markdown/                   # 前端交嚟嘅 .md；QMD 只索引呢度
    .qmd/index.yml              # 呢個用戶自己嘅 QMD 設定
    .qmd/index.sqlite           # 呢個用戶自己嘅 QMD 庫（唔用 ~/.cache/qmd/index.sqlite）
```

## 流程

1. 前端完成登入同權限檢查，轉發請求到本服務，帶 `X-User-Id`。
2. 前端資料夾樹用 `POST /v1/folders` 喺 Mac 建同一路徑（`inbox/` 同 `markdown/` 兩邊一齊建）。上傳檔案時用 `relative_path` 對齊，例如 `contracts/2024/report.pdf`。
3. `POST /v1/files` 必須同時交原檔同 markdown。即時寫入 `inbox/` 同 `markdown/`，然後入隊列。
4. Worker 按 `user_id` 串行：`qmd update` → `qmd embed`。失敗可重試；行程重啟會把 `running` 改回 `pending`，從上次 stage 繼續。
5. 搜尋唔入隊列。`POST /v1/search` 即時喺該用戶根目錄跑 `qmd`，只搜 `markdown` collection。可用 `path` 再限資料夾。
6. 每位用戶嘅 Pi session 用 `python -m dochub mcp`（stdio），由 host 注入 `DOCHUB_USER_ID` 同可選 `DOCHUB_FOLDER`。唔好開 `qmd mcp`。

`qmd update` / `qmd embed` 仍然用隊列，因為 embed 會載入本地模型，可以好慢。上傳唔使等呢兩步完成。

本服務唔驗 token、唔做 scope。用戶身份只來自 `X-User-Id`。

## 啟動

```bash
export DOCHUB_DATA_ROOT="$HOME/Documents/dochub"
# 內網可改 bind；預設只聽 127.0.0.1
# export DOCHUB_BIND=127.0.0.1
# export DOCHUB_PORT=8787

PYTHONPATH=src python3 -m dochub serve
```

`serve` 預設連 worker 一齊開。分開跑：

```bash
PYTHONPATH=src python3 -m dochub serve --no-worker
PYTHONPATH=src python3 -m dochub worker
```

## 前端點轉發

登入成功之後，把請求轉去 `http://127.0.0.1:8787`，每個請求加：

```
X-User-Id: alice
```

`user_id` 只可以係 `A-Za-z0-9._@+-`，最長 128。檔案永遠寫入 `users/<X-User-Id>/`。

## HTTP

| 方法 | 路徑 | `X-User-Id` | 說明 |
|------|------|-------------|------|
| GET | `/health` | 唔使 | liveness |
| GET | `/v1/health` | 可選 | 有 header 就附帶該用戶隊列數字 |
| GET | `/v1/me` | 要 | 回傳 `user_id` |
| POST | `/v1/folders` | 要 | 建立資料夾（inbox 同 markdown 同步） |
| GET | `/v1/tree` | 要 | 該用戶資料夾樹；`?path=` 可睇子樹 |
| POST | `/v1/files` | 要 | 落盤 + 入隊列 |
| GET | `/v1/files` | 要 | 該用戶檔案列表 |
| GET | `/v1/files/{id}` | 要 | 檔案 + 任務 |
| GET | `/v1/files/{id}/content?tree=inbox\|markdown` | 要 | 原檔或已轉 md |
| POST | `/v1/search` | 要 | 即時代查，唔入隊列；可選 `path` 限資料夾 |
| GET | `/v1/documents?path=` | 要 | 讀該用戶 `markdown/` 下一個檔（可加 `:from:count`） |
| GET | `/v1/tasks` | 要 | 該用戶任務 |
| GET | `/v1/tasks/{id}` | 要 | 單一任務 |

HTTP 沒有 MCP 路由。Pi / pi-agent-core 用 stdio：`python -m dochub mcp`。唔好把 `qmd mcp` 暴露給前端或 agent。

### 資料夾樹

前端建立資料夾之後，同步去 Mac：

```bash
curl -s http://127.0.0.1:8787/v1/folders \
  -H "X-User-Id: alice" \
  -H "Content-Type: application/json" \
  -d '{"path":"contracts/2024"}'
```

空資料夾都會留喺 `inbox/contracts/2024/` 同 `markdown/contracts/2024/`。重複建立同一路徑係安全嘅。

睇樹：

```bash
curl -s http://127.0.0.1:8787/v1/tree -H "X-User-Id: alice"
curl -s "http://127.0.0.1:8787/v1/tree?path=contracts" -H "X-User-Id: alice"
```

上傳入呢個資料夾時，`relative_path` 用完整相對路徑（連檔名）：

```
contracts/2024/report.pdf
```

唔事先 `POST /v1/folders` 都可以：上傳會自動建缺少嘅父目錄。資料夾 API 用嚟保留空資料夾，同前端樹對齊。

### 上傳

```bash
curl -s http://127.0.0.1:8787/v1/files \
  -H "X-User-Id: alice" \
  -F "file=@./report.pdf;type=application/pdf" \
  -F "markdown=@./report.md;type=text/markdown" \
  -F "relative_path=contracts/report.pdf"
```

JSON：

```json
{
  "filename": "report.pdf",
  "content_base64": "...",
  "relative_path": "contracts/report.pdf",
  "content_type": "application/pdf",
  "markdown": "# 前端已轉好的正文"
}
```

`markdown` 必填。成功回 `202`，body 含 `file` 同 `task`。原檔同 markdown 已經落盤；worker 稍後跑 `qmd update` / `qmd embed`。

### 搜尋

```bash
curl -s http://127.0.0.1:8787/v1/search \
  -H "X-User-Id: alice" \
  -H "Content-Type: application/json" \
  -d '{"query":"季度規劃","mode":"search","limit":10}'
```

`mode`：`search`（預設，BM25，快）、`vsearch`、`query`（hybrid + rerank，會載入本地 LLM）。只查該用戶嘅庫。

限資料夾（markdown 相對路徑，前綴匹配）：

```bash
curl -s http://127.0.0.1:8787/v1/search \
  -H "X-User-Id: alice" \
  -H "Content-Type: application/json" \
  -d '{"query":"季度規劃","mode":"search","path":"contracts/2024","limit":10}'
```

每條結果會帶 `dochub_path`（例如 `contracts/2024/report.md`）。QMD 本身冇 folder filter，本服務會 over-fetch 再按路徑前綴篩。

讀正文（MCP `get` 用呢條）：

```bash
curl -s "http://127.0.0.1:8787/v1/documents?path=contracts/2024/report.md" \
  -H "X-User-Id: alice"
curl -s "http://127.0.0.1:8787/v1/documents?path=contracts/2024/report.md:120:40" \
  -H "X-User-Id: alice"
```

## Per-user MCP（pi-agent-core）

每位登入用戶開一個 stdio MCP 進程，身份同資料夾由 **host** 寫入環境變數，唔經模型：

```bash
PYTHONPATH=src \
DOCHUB_URL=http://127.0.0.1:8787 \
DOCHUB_USER_ID=alice \
DOCHUB_FOLDER=contracts/2024 \
python3 -m dochub mcp
```

工具對齊 QMD MCP：`query`、`get`、`multi_get`，另外有 `tree`、`status`。`user_id` 唔係 tool 參數。`DOCHUB_FOLDER` 可選，有就所有搜尋／讀檔都唔可以走出呢個資料夾。

Skill（指導 agent 點 search-then-get）喺：

`contrib/pi-agent/skills/dochub/SKILL.md`

Host 接線例子：`contrib/pi-agent/README.md`、`mcp.json.example`、`session.mjs.example`。Pi CLI 把 skill 放到 `~/.pi/agent/skills/dochub/`；pi-agent-core 要把 `SKILL.md` 放進該 session 嘅 system prompt，並按用戶 spawn MCP。

## 環境變數

| 變數 | 預設 | 說明 |
|------|------|------|
| `DOCHUB_DATA_ROOT` | `~/Documents/dochub` | 資料根 |
| `DOCHUB_BIND` | `127.0.0.1` | 監聽地址 |
| `DOCHUB_PORT` | `8787` | 埠 |
| `DOCHUB_QMD_BIN` | `qmd` | qmd CLI |
| `DOCHUB_MAX_UPLOAD_BYTES` | 50MiB | 上傳上限 |
| `DOCHUB_MAX_ATTEMPTS` | 5 | 任務重試次數 |
| `DOCHUB_URL` | `http://127.0.0.1:8787` | MCP 進程連 HTTP API |
| `DOCHUB_USER_ID` | （必填，僅 MCP） | 該 MCP 綁定嘅用戶 |
| `DOCHUB_FOLDER` | （可選，僅 MCP） | 資料夾 pin |
| `DOCHUB_TIMEOUT` | 180 | MCP 打 HTTP 超時（秒） |

跑 `qmd` 時會設 `QMD_TRUST_LOCAL_CONFIG=1`（設定檔由本服務寫），並以該用戶根目錄為 cwd，令 QMD 用 `.qmd/index.sqlite` 而唔係預設庫。

## 測試

```bash
make test
```

## launchd

見 `contrib/launchd/com.dochub.plist`。記住改 Python / qmd 路徑。
