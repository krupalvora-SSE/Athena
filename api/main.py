from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager
import logging
import sqlite3
import os

from rag import RAGPipeline
from tools import route_query, wants_sources, load_schema_from_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

rag: RAGPipeline | None = None

CHAT_LOG_DB = os.getenv("CHAT_LOG_DB", "/chroma_data/chat_logs.db")
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "15"))  # number of past turns to inject


# ---------------------------------------------------------------------------
# Chat log
# ---------------------------------------------------------------------------

def _init_chat_log():
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
        # Index for fast history lookups per session
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session ON chat_log(session_id, id)"
        )


def _log_chat(username: str, session_id: str, message: str, answer: str, used_db: bool):
    try:
        with sqlite3.connect(CHAT_LOG_DB) as conn:
            conn.execute(
                "INSERT INTO chat_log (username, session_id, message, answer, used_db) VALUES (?, ?, ?, ?, ?)",
                (username, session_id, message, answer, int(used_db)),
            )
    except Exception as e:
        logger.warning(f"Chat log write failed: {e}")


# ---------------------------------------------------------------------------
# Phase 2.5 — Session history
# ---------------------------------------------------------------------------

def _fetch_history(session_id: str, n: int = HISTORY_TURNS) -> str:
    """
    Return the last n turns for this session as a plain-text block.
    Format: alternating User / Assistant lines, oldest first.
    """
    try:
        with sqlite3.connect(CHAT_LOG_DB) as conn:
            rows = conn.execute(
                "SELECT message, answer FROM chat_log "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
        if not rows:
            return ""
        turns = [
            f"User: {msg}\nAssistant: {ans}"
            for msg, ans in reversed(rows)
        ]
        return "\n\n".join(turns)
    except Exception as e:
        logger.warning(f"History fetch failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# Phase 2.3 — User context
# ---------------------------------------------------------------------------

def _build_user_context(username: str) -> str:
    """
    Return a short context block with the user's roles.
    Injected into the RAG prompt so the LLM knows who is asking.
    Empty string for anonymous users or if DB is unavailable.
    """
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
    global rag
    _init_chat_log()
    logger.info("Syncing DB schema...")
    load_schema_from_db()
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
    # Optional fields the chat UI can pass for richer context
    current_doctype: str | None = None   # e.g. "Purchase Order" — doctype the user has open
    current_doc: str | None = None       # e.g. "PO-2024-00123" — specific document open
    user_roles: list[str] | None = None  # roles already known by the frontend (avoids extra DB call)


class ChatResponse(BaseModel):
    answer: str
    sources: list[str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "chroma_ready": rag is not None and rag.is_ready()}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if rag is None:
        raise HTTPException(status_code=503, detail="RAG pipeline not initialized")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")

    # Phase 2.5 — fetch conversation history for this session
    history = _fetch_history(req.session_id)

    # Phase 2.3 — build user context (use frontend-provided roles if available)
    if req.user_roles:
        user_context = f"User: {req.username}\nRoles: {', '.join(req.user_roles)}"
    else:
        user_context = _build_user_context(req.username)

    # Append document context if the UI tells us what the user has open
    doc_context_parts = []
    if req.current_doctype:
        doc_context_parts.append(f"Currently viewing doctype: {req.current_doctype}")
    if req.current_doc:
        doc_context_parts.append(f"Currently open document: {req.current_doc}")
    if doc_context_parts:
        user_context += "\n" + "\n".join(doc_context_parts)

    # Phase 2.1 — try DB tools (regex first, LLM classifier as fallback)
    result = route_query(req.message, req.username, history=history)
    used_db = result is not None

    # RAG fallback
    if result is None:
        result = rag.query(req.message, history=history, user_context=user_context)

    answer = result["answer"]
    sources = result["sources"] if wants_sources(req.message) else []

    _log_chat(req.username, req.session_id, req.message, answer, used_db)

    return ChatResponse(answer=answer, sources=sources)
