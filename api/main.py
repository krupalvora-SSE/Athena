from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager
import logging
import sqlite3
import os

from rag import RAGPipeline
from tools import route_query, wants_sources
from schema_retriever import SchemaRetriever
import index_schema as _index_schema

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

rag: RAGPipeline | None = None
schema_retriever: SchemaRetriever | None = None

# SQLite is kept only as a history fallback if MariaDB is unreachable.
CHAT_LOG_DB   = os.getenv("CHAT_LOG_DB", "/chroma_data/chat_logs.db")
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "15"))


# ---------------------------------------------------------------------------
# SQLite — fallback history store only
# ---------------------------------------------------------------------------

def _init_sqlite():
    with sqlite3.connect(CHAT_LOG_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP,
                username   TEXT NOT NULL,
                session_id TEXT,
                message    TEXT NOT NULL,
                answer     TEXT NOT NULL,
                used_db    INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session ON chat_log(session_id, id)"
        )


def _sqlite_log(username: str, session_id: str, message: str, answer: str, used_db: bool):
    try:
        with sqlite3.connect(CHAT_LOG_DB) as conn:
            conn.execute(
                "INSERT INTO chat_log (username, session_id, message, answer, used_db) VALUES (?, ?, ?, ?, ?)",
                (username, session_id, message, answer, int(used_db)),
            )
    except Exception as e:
        logger.warning(f"SQLite fallback log failed: {e}")


def _sqlite_history(session_id: str, n: int) -> str:
    try:
        with sqlite3.connect(CHAT_LOG_DB) as conn:
            rows = conn.execute(
                "SELECT message, answer FROM chat_log "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
        if not rows:
            return ""
        return "\n\n".join(
            f"User: {msg}\nAssistant: {ans}"
            for msg, ans in reversed(rows)
        )
    except Exception as e:
        logger.warning(f"SQLite history fetch failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# Logging — primary: tabAI Chat Log in MariaDB; fallback: SQLite
# ---------------------------------------------------------------------------

def _log_chat(
    username: str,
    session_id: str,
    message: str,
    answer: str,
    used_db: bool,
    sources: list[str],
    current_doctype: str,
    current_doc: str,
):
    """Write to tabAI Chat Log in MariaDB. Falls back to SQLite on failure."""
    try:
        import db
        db.log_chat(
            user=username,
            question=message,
            answer=answer,
            session_id=session_id,
            sources=sources,
            current_doctype=current_doctype or "",
            current_doc=current_doc or "",
            used_db=used_db,
        )
    except Exception as e:
        logger.warning(f"MariaDB log failed, writing to SQLite fallback: {e}")
        _sqlite_log(username, session_id, message, answer, used_db)


# ---------------------------------------------------------------------------
# Session history — primary: tabAI Chat Log; fallback: SQLite
# ---------------------------------------------------------------------------

def _fetch_history(session_id: str, n: int = HISTORY_TURNS) -> str:
    """Return last n turns as plain text. Tries MariaDB first, falls back to SQLite."""
    try:
        import db
        rows = db.get_chat_history(session_id, n)
        if rows:
            return "\n\n".join(
                f"User: {r['question']}\nAssistant: {r['answer']}"
                for r in rows
            )
    except Exception as e:
        logger.warning(f"MariaDB history fetch failed, trying SQLite: {e}")
    return _sqlite_history(session_id, n)


# ---------------------------------------------------------------------------
# User context
# ---------------------------------------------------------------------------

def _build_user_context(username: str) -> str:
    if username == "anonymous":
        return ""
    try:
        import db
        roles = db.get_user_roles(username)
        if not roles:
            return f"User: {username}"
        return f"User: {username}\nRoles: {', '.join(roles)}"
    except Exception as e:
        logger.warning(f"User context fetch failed: {e}")
        return f"User: {username}"


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global rag, schema_retriever
    _init_sqlite()

    logger.info("Indexing DB schema into ChromaDB...")
    try:
        _index_schema.index_schema()
    except Exception as e:
        logger.warning(f"Schema indexing failed (NL queries degraded): {e}")

    logger.info("Initialising SchemaRetriever...")
    schema_retriever = SchemaRetriever()

    logger.info("Loading RAG pipeline...")
    rag = RAGPipeline()
    logger.info("RAG pipeline ready.")
    yield
    logger.info("Shutting down.")


app = FastAPI(title="ERP Chatbot API", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    username: str = "anonymous"
    session_id: str = "default"
    current_doctype: str | None = None
    current_doc: str | None = None
    user_roles: list[str] | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "chroma_ready": rag is not None and rag.is_ready(),
        "schema_ready": schema_retriever is not None and schema_retriever.is_ready(),
    }


@app.post("/admin/sync-schema")
def sync_schema():
    """Re-index DB schema. Called by Frappe post-migrate hook."""
    global schema_retriever
    try:
        count = _index_schema.index_schema()
        if schema_retriever:
            schema_retriever.reload()
        else:
            schema_retriever = SchemaRetriever()
        return {"status": "ok", "tables_indexed": count}
    except Exception as e:
        logger.error(f"Schema sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if rag is None:
        raise HTTPException(status_code=503, detail="RAG pipeline not initialized")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")

    history = _fetch_history(req.session_id)

    if req.user_roles:
        user_context = f"User: {req.username}\nRoles: {', '.join(req.user_roles)}"
    else:
        user_context = _build_user_context(req.username)

    if req.current_doctype:
        user_context += f"\nCurrently viewing doctype: {req.current_doctype}"
    if req.current_doc:
        user_context += f"\nCurrently open document: {req.current_doc}"

    result = route_query(req.message, req.username, history=history, schema_retriever=schema_retriever)
    used_db = result is not None

    if result is None:
        result = rag.query(req.message, history=history, user_context=user_context)

    answer = result["answer"]
    sources = result.get("sources", [])
    visible_sources = sources if wants_sources(req.message) else []

    _log_chat(
        username=req.username,
        session_id=req.session_id,
        message=req.message,
        answer=answer,
        used_db=used_db,
        sources=sources,
        current_doctype=req.current_doctype or "",
        current_doc=req.current_doc or "",
    )

    return ChatResponse(answer=answer, sources=visible_sources)
