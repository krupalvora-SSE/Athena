# Athena — Project Prompt for LLMs

This file primes an LLM/agent with everything needed to reason about and modify this codebase. For human setup instructions, see [README.md](README.md).

---

## What we're building

**Athena** — an internal AI support assistant for **Frappe / ERPNext**, used by Supply Chain & Finance teams. Users ask natural-language questions in a chat UI and get answers about roles, permissions, stock balances, documents, pending approvals, ERP processes, or the results of read-only DB queries.

**Status:** MVP for management demo. 2–3 concurrent internal users. Not public-facing.

---

## Hard constraints

- **Read-only against ERP DB.** `GRANT SELECT` only. Never write to the Frappe MariaDB.
- **No changes to the ERPNext dev container** (runs separately at `:8000`).
- **Local LLM only** — Ollama on host (`qwen2.5:7b` for routing/SQL, `qwen2.5:3b` for generation, `nomic-embed-text` for embeddings). No external API calls for inference.
- **Keep it simple.** No speculative abstractions, no features beyond what's asked, no half-finished implementations.

---

## Architecture (one screen)

```
Chat UI ──► FastAPI /chat (api/main.py)
              │
              ├── Regex Router (api/tools.py)         ← first match wins, zero-latency
              │     workflow → howto → list → compound → stock → roles → docs → NL-SQL
              │
              ├── LLM Classifier (Ollama JSON mode)   ← fallback when regex misses
              │
              ├── DB Tools (api/db.py)                ← PyMySQL, raw SQL, read-only
              │     tabHas Role, tabDocPerm, tabBin, tabToDo, tabWorkflow Action, …
              │     NL-to-SQL with live-schema injection (no column hallucination)
              │
              └── RAG Fallback (api/rag.py)           ← ChromaDB + BM25 hybrid
                    relevance gate (<0.35 → "I don't know")
                    query rewriting · history summarisation · MMR diversity
```

**Key files:**

| File | Purpose |
|---|---|
| [api/main.py](api/main.py) | FastAPI app, lifespan (schema sync + RAG init), session history |
| [api/tools.py](api/tools.py) | Intent router — regex → LLM classifier → DB handlers |
| [api/db.py](api/db.py) | Raw SQL connector — all queries + full schema loader |
| [api/rag.py](api/rag.py) | Hybrid RAG pipeline (ChromaDB + BM25 + MMR + Ollama) |
| [ingest/index_docs.py](ingest/index_docs.py) | Ingest markdown/PDF docs into ChromaDB |
| [logs.py](logs.py) | Chat-log analysis CLI (groups by `_route` stamp) |

---

## Routing rules (important)

1. **Regex first, LLM last.** `route_query()` in `api/tools.py` walks an ordered chain. The order is deliberate: `_HOWTO_RE` and `_WORKFLOW_RE` fire **before** `_NL_QUERY_RE` so "how to do X" / "workflow of Y" don't get translated into LLM-generated SQL.
2. **NL-to-SQL is a last resort**, gated by a schema-dump detector. Prefer adding a regex pattern + a small handler that hits `db.py` directly.
3. Each handler returns `{"answer", "sources", "_route"}`. The `_route` string (e.g. `regex/workflow`, `regex/howto`, `regex/compound[a+b]`) is stamped into `tabAI Chat Log.request_payload` for `logs.py --stats` to group by.
4. **`user_roles` from the request body is preferred** over `db.get_user_roles(username)` — Administrator has every role and would drown the workflow handler.

---

## Non-obvious wiring

- `request_payload` on `tabAI Chat Log` is **dual-written**: the Frappe-side wrapper writes the full request body; the FastAPI-side `db.log_chat()` writes a separate row with `{"route": "..."}`. Both rows coexist with different name suffixes (sequential vs UUID).
- `doc_status` from `tabWorkflow Document State` comes back as a **string** (`'0'/'1'/'2'`), not int — must `int()` before lookup.
- Container ships **built code** (no volume mount). `docker restart` reuses the old image — use `docker compose up -d --build api` after any code change.
- Schema is **read live from MariaDB** on every container start (`SHOW TABLES` + `DESCRIBE` + custom fields) — no manual sync needed after `bench migrate`, just restart the api container.
- For chat-log analysis, query the `tabAI Chat Log` table in Frappe MariaDB directly. Do NOT rely on docker logs.

---

## What Athena answers

User roles, role permissions, doctype access, access checks, stock balance (with fuzzy item-name search), open tasks, pending approvals, document lookups (`SO-2024-001`, etc.), live read-only DB queries (gated to manager roles), ERP process docs (RAG), follow-up questions (context-aware rewriting), identity questions.

---

## Conventions when modifying this code

- Adding a new intent → **regex pattern in `tools.py` + small handler in `db.py`**, not a new LLM path.
- Stamp `_route` on every handler return so `logs.py --stats` keeps working.
- Don't expand the LLM-SQL surface area without strong justification — it's the failure-prone path.
- RAG prompts already refuse to answer workflow questions when chunks don't cover them. Don't loosen that.
- No mocks for DB tests — use the real Frappe MariaDB. Tests in `tests/test_chat_log_cases.py` are derived from real chat logs.
- Comments: only when the *why* is non-obvious. Don't narrate what code does.

---

## Non-goals

- Multi-tenant / public deployment.
- Write access to ERP data (creating/updating/submitting documents).
- Replacing Frappe's UI — Athena is a sidecar chat assistant, not a workflow engine.
- Cloud LLMs, paid APIs, or any inference outside the local Ollama instance.
