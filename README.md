# Athena — Internal ERP Support Chatbot

Athena is an AI-powered internal support assistant for **Frappe / ERPNext**, built by **Krupal Vora**. It answers questions about roles, permissions, stock balances, ERP processes, and can run live read-only database queries — all through a simple chat interface.

---

## Architecture

```
Chat UI  ──►  FastAPI (/chat)
                  │
                  ├── Regex Router  (fast, zero-latency intent matching)
                  ├── LLM Classifier  (Ollama qwen2.5:7b, JSON mode)
                  │       ↓ if intent matched
                  ├── DB Tools  (PyMySQL → Frappe MariaDB)
                  │       • Roles & permissions (tabHas Role, tabDocPerm)
                  │       • Stock balances (tabBin)
                  │       • Open tasks (tabToDo)
                  │       • Natural language → SELECT queries
                  │
                  └── RAG Fallback  (ChromaDB + BM25 hybrid retrieval)
                          • nomic-embed-text embeddings (Ollama)
                          • qwen2.5:7b answer generation
```

**Key files:**

| File | Purpose |
|---|---|
| `api/main.py` | FastAPI app, chat endpoint, session history, user context |
| `api/tools.py` | Intent router — regex → LLM classifier → DB handlers |
| `api/db.py` | Raw SQL connector for Frappe/ERPNext MariaDB |
| `api/rag.py` | Hybrid RAG pipeline (ChromaDB + BM25 + Ollama) |
| `ingest/index_docs.py` | Ingest PDFs/docs into ChromaDB vector store |
| `db/config.json` | DB credentials (**not committed** — see below) |
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

Create `db/config.json` (this file is gitignored — never commit it):

```json
{
  "host": "frappe_docker_devcontainer-mariadb-1",
  "port": 3306,
  "user": "your_db_user",
  "password": "your_db_password",
  "database": "your_frappe_site_db"
}
```

> **Tip:** The `host` should be the MariaDB container name if running in Docker, or `localhost` if running bare-metal.

### 3. Grant DB access

Run this on your MariaDB instance so the chatbot user can connect from the Docker network:

```sql
GRANT SELECT ON `your_frappe_site_db`.* TO 'your_db_user'@'172.18.%';
FLUSH PRIVILEGES;
```

> Use `GRANT SELECT` only — Athena never writes to the ERP database.

### 4. Configure Docker networking

If your Frappe stack runs in Docker, add the Frappe network to `docker-compose.yml` so Athena can reach MariaDB:

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
docker-compose up --build -d
```

The API will be available at `http://localhost:7001`.

### 6. Ingest documents (optional but recommended)

Place PDF or markdown files in an `ingest/docs/` folder, then run:

```bash
python3 ingest/index_docs.py
```

This populates the ChromaDB vector store used by the RAG fallback.

---

## API Usage

### Health check

```bash
curl http://localhost:7001/health
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
| `session_id` | No | Session ID for conversation history |
| `current_doctype` | No | Doctype the user has open in the UI |
| `current_doc` | No | Specific document name open in the UI |
| `user_roles` | No | Roles from the frontend (skips a DB lookup if provided) |

---

## What Athena Can Answer

| Question type | Example |
|---|---|
| User roles | "What roles do I have?" |
| Role permissions | "What can Stock Manager do on Purchase Order?" |
| Doctype access | "Who can access Delivery Note?" |
| Users with a role | "List users with role System Manager" |
| Stock balance | "What is the stock of item MDCR-0025 in Nagpur warehouse?" |
| Open tasks | "Show my open tasks" |
| Live DB queries* | "How many sales orders were submitted this month?" |
| ERP process docs | "How do I create a Material Transfer?" |
| Identity | "What is your name?" |

> *Live DB queries require the user to have one of: `System Manager`, `Stock Manager`, `Accounts Manager`, `Purchase Manager`, or `Sales Manager`.

---

## Running Tests

Tests are derived from real chat logs in the `tabAI Chat Log` table.

```bash
python3 -m pytest tests/test_chat_log_cases.py -v
```

Expected: **26 passed, 1 xfailed**

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama endpoint |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Model for generation + classification |
| `DB_CONFIG_PATH` | `/db/config.json` | Path to DB credentials file |
| `DB_HOST` | _(from config.json)_ | Override MariaDB hostname |
| `CHAT_LOG_DB` | `/chroma_data/chat_logs.db` | SQLite path for conversation logs |
| `HISTORY_TURNS` | `6` | Number of past turns to inject into context |

---

## Built With

- [FastAPI](https://fastapi.tiangolo.com/)
- [Ollama](https://ollama.com/) — local LLM inference (qwen2.5:7b, nomic-embed-text)
- [ChromaDB](https://www.trychroma.com/) — vector store
- [LangChain](https://langchain.com/) — RAG pipeline + BM25 hybrid retrieval
- [PyMySQL](https://pymysql.readthedocs.io/) — direct MariaDB access
- [Frappe / ERPNext](https://frappeframework.com/)

---

## Author

**Krupal Vora**
