---
name: dochub
description: Search and retrieve this user's indexed documents through Dochub MCP tools (query, get, multi_get, tree, status). Use when the user asks about their files, folders, notes, uploads, contracts, or knowledge base, or wants an answer from their documents.
compatibility: Requires the Dochub MCP server for this session (`python -m dochub mcp`). Identity and optional folder pin come from the host environment.
---

# Dochub — this user's documents

Search the user's indexed markdown, then read the source, then answer. Snippets are leads, not evidence.

Tools (names may appear as `query` or `mcp__dochub__query` depending on Pi exposure): `query`, `get`, `multi_get`, `tree`, `status`.

This session is already bound to one user (and may be pinned to a folder). Never pass `user_id`. Never run the `qmd` CLI or `qmd mcp`. Never curl Dochub yourself.

## Workflow

1. `query` for candidate documents.
2. `get` or `multi_get` the best hits. Use `path:from:count` around a hit line (for example `contracts/note.md:120:40`).
3. Answer from retrieved text. Cite `dochub_path` and line numbers.

Typical loop:

```text
query  { "searches": [{ "type": "lex", "query": "quarterly plan OKR" }], "limit": 5 }
# leads: contracts/2024/plan.md:42  invoices/acme.md:18
get    { "file": "contracts/2024/plan.md:22:80" }
```

When reporting what you retrieved:

```text
Retrieved:
- contracts/2024/plan.md
- invoices/acme.md
```

## Pick the search mode

**Exact words, titles, names, rare phrases** — `query` with `searches: [{ "type": "lex", "query": "..." }]` (BM25, fast, works after index update even before embeddings).

**Idea / paraphrase / conceptual recall** — default to a structured query you write yourself:

```json
{
  "intent": "Find the note about metrics as instruments; avoid generic KPI dashboards.",
  "searches": [
    { "type": "lex", "query": "cockpit OKR Goodhart metrics judgment" },
    { "type": "vec", "query": "data informed not metric driven product judgment" },
    { "type": "hyde", "query": "A note says metrics are useful like cockpit instruments, but leaders should stay data-informed rather than metric-driven." }
  ],
  "limit": 10,
  "minScore": 0,
  "candidateLimit": 40
}
```

These fields are sent through to Dochub / qmd. `lex` / `vec` / `hyde` stay on separate channels and are fused with RRF. Rerank is off by default; set `rerank: true` to enable the slower cross-encoder. Do not collapse them into one string.

- `intent` — what to find and what to avoid.
- `lex` — exact terms, aliases, titles you expect in the source.
- `vec` — the idea in source-like wording.
- `hyde` — a short passage that would satisfy the request.

A bare `query: "<the user's sentence>"` throws away context only you have. Prefer structured `searches`. If the user gave a single rare token, lex-only is enough.

A bare `query` with `mode: query` is expanded by the server into `lex:` + `vec:`. If hybrid search times out, is busy, fails, or embeddings are not ready, Dochub retries keyword search automatically and returns `fallback.reason` (`timeout`, `busy`, `qmd_exec_failed`, `semantic_not_ready`, or `parse_failed`). You usually do not need to retry. Set `fallback: false` only if you must fail instead of falling back.

## Folder scope

If the user names a folder, pass it as `path` on `query` / `tree` (markdown-relative, for example `contracts/2024`). The host may already pin the session with `DOCHUB_FOLDER`; extra `path` stays inside that pin.

`tree` lists the folder tree when you need to see what exists before searching.

## Retrieve

`get` / `multi_get` take `dochub_path` values from search (also `qmd://markdown/...`). They return line-numbered text by default.

- `contracts/note.md:120:40` — 40 lines starting at 120.
- `contracts/note.md:120` — from 120 to end.
- `get` args `fromLine` / `maxLines` override a suffix.

Do not slice documents with `sed`, `head`, `tail`, or `awk`.

`get` also accepts `#abc123` docids from search. If a path is missing, the tool returns similar path hints.

`multi_get` accepts comma-separated paths, docids, or globs, including recursive `contracts/**/*.md`. Optional `maxBytes` caps each file.

## Pitfalls

- Do not stop at snippets. Fetch before claiming facts, quotes, or decisions.
- Do not invent `user_id`, paths outside the pin, or a second MCP server.
- Do not mutate indexes. Upload and `qmd update` / `embed` are the Mac worker's job.
- Call `status` when search looks empty or stale. Keyword search works after ingest `qmd update`. Semantic `vec` / `hyde` / `mode: query` need embeddings; if `semantic_search_ready` is false, use lex / `mode: search`.
