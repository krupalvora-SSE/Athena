# Athena — Internal ERP Support Chatbot

Athena is an AI-powered internal support assistant for **Frappe / ERPNext**, built by **Krupal Vora**. It answers questions about roles, permissions, stock balances, ERP processes, document details, pending approvals, and can run live read-only database queries — all through a simple chat interface.

---

## Architecture

```
Chat UI  ──►  FastAPI (/chat)
                  │
                  ├── Regex Router  (fast, zero-latency intent matching)
                  │       • Roles & permissions
                  │       • Stock balance (with fuzzy item name search)
                  │       • Open tasks & pending approvals
                  │       • Document lookup (SO-2024-001, DN-2024-005, etc.)
                  │       • Natural language → SELECT queries
                  │
                  ├── LLM Classifier  (Ollama qwen2.5:7b, JSON mode, fallback)
                  │       ↓ if intent matched
                  ├── DB Tools  (PyMySQL → Frappe MariaDB, read-only)
                  │       • tabHas Role, tabDocPerm, tabCustom DocPerm
                  │       • tabBin (stock), tabToDo, tabWorkflow Action
                  │       • NL-to-SQL with live schema injection (no hallucination)
                  │
                  └── RAG Fallback  (ChromaDB + BM25 hybrid retrieval)
                          • Relevance gate (score < 0.35 → "I don't know")
                          • Query rewriting for follow-up questions
                          • History summarisation for long sessions
                          • nomic-embed-text embeddings (Ollama)
                          • qwen2.5:3b answer generation (MMR diversity)
```

**Key files:**

| File | Purpose |
|---|---|
| `api/main.py` | FastAPI app, lifespan (schema sync + RAG init), session history |
| `api/tools.py` | Intent router — regex → LLM classifier → DB handlers |
| `api/db.py` | Raw SQL connector — all queries + full schema loader |
| `api/rag.py` | Hybrid RAG pipeline (ChromaDB + BM25 + MMR + Ollama) |
| `ingest/index_docs.py` | Ingest markdown/PDF docs into ChromaDB vector store |
| `db/config.json` | DB credentials (**not committed** — see Setup) |
| `docker-compose.yml` | Service definition, network config, volume mounts |

---

## Prerequisites

- **Docker** + **Docker Compose**
- **Ollama** running locally with these models pulled:
  ```bash
  ollama pull qwen2.5:7b
  ollama pull nomic-embed-text
  ```
- Access to the **Frappe/ERPNext MariaDB** container or host

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/krupalvora-SSE/Athena.git
cd Athena
```

### 2. Configure database credentials

Create `db/config.json` (gitignored — never commit this file):

```json
{
  "db_host": "frappe_docker_devcontainer-mariadb-1",
  "db_port": 3306,
  "db_user": "your_db_user",
  "db_password": "your_db_password",
  "db_name": "your_frappe_site_db"
}
```

> **Tip:** `db_host` should be the MariaDB container name if running in Docker, or `localhost` for bare-metal.

### 3. Grant DB access

```sql
GRANT SELECT ON `your_frappe_site_db`.* TO 'your_db_user'@'172.18.%';
FLUSH PRIVILEGES;
```

> `GRANT SELECT` only — Athena never writes to the ERP database.

### 4. Configure Docker networking

If your Frappe stack runs in Docker, the Athena container must be on the same network:

```yaml
networks:
  frappe_network:
    external: true
    name: frappe_docker_devcontainer_default   # replace with your network name
```

Find your Frappe network name with:
```bash
docker network ls
```

### 5. Build and start

```bash
docker compose up --build -d
```

On every build, Athena automatically:
1. Reads the full live schema from MariaDB (`SHOW TABLES` + `DESCRIBE` + custom fields)
2. Loads it into memory for accurate NL-to-SQL generation
3. Initialises the RAG pipeline (ChromaDB + BM25)

The API will be available at `http://localhost:7001`.

### 6. Ingest documents (optional but recommended)

Place markdown or PDF files in `ingest/docs/`, then run:

```bash
python3 ingest/index_docs.py
```

This populates the ChromaDB vector store used by the RAG fallback.

> Re-run this whenever documentation is updated.

### 7. Syncing schema after `bench migrate`

When ERPNext schema changes (new fields, custom fields, doctypes):

```bash
docker compose restart api
```

The container restart re-reads the live schema from MariaDB automatically — no manual steps needed.

---

## API Usage

### Health check

```bash
curl http://localhost:7001/health
```

```json
{ "status": "ok", "chroma_ready": true }
```

### Send a message

```bash
curl -X POST http://localhost:7001/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "what roles do I have?",
    "username": "john.doe@example.com",
    "session_id": "sess-abc123",
    "current_doctype": "Delivery Note",
    "current_doc": "DN-2024-00456",
    "user_roles": ["Stock User", "Delivery User"]
  }'
```

**Request fields:**

| Field | Required | Description |
|---|---|---|
| `message` | Yes | The user's question |
| `username` | No | Logged-in user's email (defaults to `anonymous`) |
| `session_id` | No | Session ID for conversation history (last 15 turns) |
| `current_doctype` | No | Doctype the user has open in the UI |
| `current_doc` | No | Specific document name open in the UI |
| `user_roles` | No | Roles from the frontend (skips a DB lookup if provided) |

**Response:**

```json
{
  "answer": "You have 3 roles: Stock User, Delivery User, Employee.",
  "sources": []
}
```

---

## What Athena Can Answer

| Question type | Example |
|---|---|
| User roles | "What roles do I have?" |
| Role permissions | "What can Stock Manager do on Purchase Order?" |
| Doctype access | "Who can access Delivery Note?" |
| Users with a role | "List users with role System Manager" |
| Access check | "Can I submit a Purchase Invoice?" |
| Stock balance | "Stock of item SLR-100W in Nagpur warehouse" |
| Fuzzy stock search | "Stock of Solar Panel 100W" _(searches by name)_ |
| Open tasks | "Show my open tasks" |
| Pending approvals | "What's pending my approval?" |
| Document details | "Show me PO-2024-00123" |
| Live DB queries* | "How many submitted delivery notes this month?" |
| ERP process docs | "How do I create a Material Transfer?" |
| Follow-up questions | "What's the next step?" _(context-aware rewriting)_ |
| Identity | "What is your name?" |

> *Live DB queries require one of: `System Manager`, `Stock Manager`, `Accounts Manager`, `Purchase Manager`, or `Sales Manager`.

---

## RAG Precision Features

| Feature | How it works |
|---|---|
| **Relevance gate** | If ChromaDB top score < 0.35, returns "I don't have docs on this" instead of guessing |
| **Query rewriting** | Follow-up questions ("what about that?") are rewritten into standalone questions before retrieval |
| **MMR retrieval** | Max Marginal Relevance ensures the top-6 chunks come from diverse sections, not the same topic |
| **History summarisation** | Sessions > 800 chars are condensed by the LLM before injection — early context is preserved |
| **Live schema injection** | NL-to-SQL uses real column names from MariaDB — column hallucination eliminated |

---

## Running Tests

Tests are derived from real chat logs in the `tabAI Chat Log` Frappe table.

```bash
python3 -m pytest tests/test_chat_log_cases.py -v
```

Expected: **26 passed, 1 xfailed**

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama endpoint |
| `OLLAMA_MODEL` | `qwen2.5:3b` | LLM for generation + classification |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model |
| `DB_CONFIG_PATH` | `/db/config.json` | Path to DB credentials file |
| `DB_HOST` | _(from config.json)_ | Override MariaDB hostname |
| `CHROMA_PERSIST_DIR` | `/chroma_data` | ChromaDB persistence directory |
| `CHAT_LOG_DB` | `/chroma_data/chat_logs.db` | SQLite path for conversation history |
| `HISTORY_TURNS` | `15` | Past turns injected into context |
| `RAG_RELEVANCE_THRESHOLD` | `0.35` | Minimum score to attempt RAG answer |
| `HISTORY_SUMMARY_THRESHOLD` | `800` | Chars before history is summarised |

---

## Timeout Reference

| Layer | Timeout |
|---|---|
| MariaDB connect | 10s |
| LLM classifier (intent routing) | 30s |
| Query rewrite + history summarise | 30s each |
| NL-to-SQL generation | 120s |
| OllamaLLM answer generation | 180s |
| Uvicorn keep-alive | 300s |

---

## Built With

- [FastAPI](https://fastapi.tiangolo.com/)
- [Ollama](https://ollama.com/) — local LLM inference (qwen2.5:3b, nomic-embed-text)
- [ChromaDB](https://www.trychroma.com/) — vector store for docs
- [LangChain](https://langchain.com/) — RAG pipeline, BM25, MMR retrieval
- [PyMySQL](https://pymysql.readthedocs.io/) — direct MariaDB access
- [Frappe / ERPNext](https://frappeframework.com/)

---

## Author

**Krupal Vora**
