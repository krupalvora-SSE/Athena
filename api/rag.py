import os
import re
import time
import logging
import httpx
from langchain_ollama import OllamaLLM
from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma
from langchain.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL    = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL       = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "/chroma_data")
CHROMA_COLLECTION  = "erp_docs"

# Minimum similarity score (0–1) for ChromaDB to consider a doc relevant.
# Below this threshold → return "I don't have docs on this" instead of guessing.
RELEVANCE_THRESHOLD = float(os.getenv("RAG_RELEVANCE_THRESHOLD", "0.35"))

# History longer than this (chars) gets summarised before injection into the prompt.
HISTORY_SUMMARY_THRESHOLD = int(os.getenv("HISTORY_SUMMARY_THRESHOLD", "800"))

# Pronoun / back-reference patterns that signal a follow-up question worth rewriting.
_FOLLOWUP_RE = re.compile(
    r"\b(it|its|that|those|the same|this|they|their|these|him|her"
    r"|what about|and also|also|too|the one|such|which one)\b",
    re.I,
)


class RAGPipeline:
    def __init__(self):
        self._ready = False
        self._setup()

    def _llm_invoke(self, prompt: str, retries: int = 3, delay: float = 3.0) -> str:
        """
        Call self._llm_invoke() with retry-with-backoff.
        The native ollama client occasionally throws ConnectionError on the first
        call after container start — retrying recovers without surfacing a 500.
        """
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return self._llm_invoke(prompt)
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    logger.warning(
                        f"Ollama LLM call failed (attempt {attempt}/{retries}): {e}. "
                        f"Retrying in {delay}s…"
                    )
                    time.sleep(delay)
        raise RuntimeError(f"Ollama LLM unavailable after {retries} attempts: {last_exc}")

    def _setup(self):
        logger.info(f"Connecting to Ollama at {OLLAMA_BASE_URL} with model {OLLAMA_MODEL}")
        self.llm = OllamaLLM(
            base_url=OLLAMA_BASE_URL,
            model=OLLAMA_MODEL,
            temperature=0.1,
            request_timeout=180.0,
        )
        self.embeddings = OllamaEmbeddings(
            base_url=OLLAMA_BASE_URL,
            model=OLLAMA_EMBED_MODEL,
        )
        self.vectorstore = Chroma(
            collection_name=CHROMA_COLLECTION,
            embedding_function=self.embeddings,
            persist_directory=CHROMA_PERSIST_DIR,
        )

        logger.info("Building BM25 index from stored chunks...")
        stored = self.vectorstore.get(include=["documents", "metadatas"])
        from langchain_core.documents import Document as LCDocument
        all_docs = [
            LCDocument(page_content=text, metadata=meta)
            for text, meta in zip(stored["documents"], stored["metadatas"])
        ]
        bm25_retriever = BM25Retriever.from_documents(all_docs, k=12)
        # MMR (Max Marginal Relevance) diversifies results so the top-6 chunks
        # come from different sections rather than all being the same topic.
        vector_retriever = self.vectorstore.as_retriever(
            search_type="mmr",
            search_kwargs={"k": 12, "fetch_k": 24, "lambda_mult": 0.7},
        )
        self.retriever = EnsembleRetriever(
            retrievers=[bm25_retriever, vector_retriever],
            weights=[0.5, 0.5],
        )
        self._ready = True
        logger.info("RAG pipeline initialized.")

    def is_ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------------
    # Relevance gate — prevents answering when no relevant docs exist
    # ------------------------------------------------------------------

    def _check_relevance(self, question: str) -> bool:
        """
        Returns True if ChromaDB has at least one doc above RELEVANCE_THRESHOLD.
        Fail-open: if the check itself errors, allow RAG to proceed.
        """
        try:
            results = self.vectorstore.similarity_search_with_relevance_scores(question, k=1)
            if not results:
                return False
            _, score = results[0]
            logger.info(f"Top relevance score: {score:.3f} (threshold: {RELEVANCE_THRESHOLD})")
            return score >= RELEVANCE_THRESHOLD
        except Exception as e:
            logger.warning(f"Relevance check failed, failing open: {e}")
            return True

    # ------------------------------------------------------------------
    # Query rewriting — converts follow-up questions to standalone ones
    # ------------------------------------------------------------------

    def _rewrite_query(self, question: str, history: str) -> str:
        """
        If the question looks like a follow-up (short, or contains back-references),
        ask the LLM to rewrite it as a self-contained question for better retrieval.
        """
        if not history or len(history.strip()) < 20:
            return question
        # Skip rewriting if the question is already self-contained
        if not _FOLLOWUP_RE.search(question) and len(question.split()) > 6:
            return question

        prompt = (
            "Given the conversation history and a follow-up question, "
            "rewrite the follow-up as a single complete standalone question. "
            "Do NOT answer it. Output ONLY the rewritten question, nothing else.\n\n"
            f"History:\n{history[-600:]}\n\n"
            f"Follow-up: {question}\n"
            "Standalone question:"
        )
        try:
            resp = httpx.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                timeout=30.0,
            )
            resp.raise_for_status()
            rewritten = resp.json().get("response", "").strip().strip('"').strip("'")
            if rewritten and len(rewritten) > 5:
                logger.info(f"Query rewritten: '{question}' → '{rewritten}'")
                return rewritten
        except Exception as e:
            logger.warning(f"Query rewrite failed, using original: {e}")
        return question

    # ------------------------------------------------------------------
    # History summarisation — condenses long sessions
    # ------------------------------------------------------------------

    def _summarize_history(self, history: str) -> str:
        """
        When history exceeds HISTORY_SUMMARY_THRESHOLD chars, summarise it
        with the LLM and keep only the last 2 turns verbatim.
        This preserves early context (doctype, document names) without bloating the prompt.
        """
        if len(history) <= HISTORY_SUMMARY_THRESHOLD:
            return history

        prompt = (
            "Summarise the following ERP support conversation in 2–3 sentences. "
            "Capture key topics, doctypes, document names, and roles mentioned. Be concise.\n\n"
            f"{history}\n\nSummary:"
        )
        try:
            resp = httpx.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                timeout=30.0,
            )
            resp.raise_for_status()
            summary = resp.json().get("response", "").strip()
            if summary:
                turns = history.split("\n\n")
                last_two = "\n\n".join(turns[-2:]) if len(turns) >= 2 else history
                condensed = f"[Earlier conversation summary]\n{summary}\n\n{last_two}"
                logger.info(f"History condensed: {len(history)} → {len(condensed)} chars")
                return condensed
        except Exception as e:
            logger.warning(f"History summarisation failed, hard-truncating: {e}")

        return history[-HISTORY_SUMMARY_THRESHOLD:]

    # ------------------------------------------------------------------
    # Main query entry point
    # ------------------------------------------------------------------

    def query(self, question: str, history: str = "", user_context: str = "") -> dict:
        # 1. Relevance gate — bail early if no relevant docs exist
        if not self._check_relevance(question):
            return {
                "answer": (
                    "I don't have documentation that covers this topic. "
                    "Please check with your ERP administrator or the official Frappe/ERPNext docs."
                ),
                "sources": [],
            }

        # 2. Condense long history
        processed_history = self._summarize_history(history) if history else ""

        # 3. Rewrite follow-up questions for better retrieval accuracy
        retrieval_query = self._rewrite_query(question, processed_history)

        # 4. Retrieve diverse docs via BM25 + MMR ensemble
        docs = self.retriever.invoke(retrieval_query)

        # 5. Cap context at 6 chunks to stay within LLM window
        context = "\n\n".join(doc.page_content for doc in docs[:6])

        # 6. Build the full LLM prompt
        sections = [
            "You are Athena, an internal ERP support assistant created by Krupal Vora.",
            "Answer using only the context provided. If the answer is not in the context, "
            "say you don't know — do not make up information.",
            "If asked about your name or who created you, always say you are Athena, created by Krupal Vora.",
        ]
        if user_context:
            sections.append(f"[User context]\n{user_context}")
        if processed_history:
            sections.append(f"[Conversation history]\n{processed_history}")
        sections.append(f"[Context from docs]\n{context}")
        sections.append(f"Question: {question}\nAnswer:")

        full_prompt = "\n\n".join(sections)

        # 7. Call LLM directly (embedding stays short — history/context not passed to embedder)
        try:
            answer = self._llm_invoke(full_prompt)
        except RuntimeError as e:
            logger.error(f"LLM unavailable: {e}")
            return {
                "answer": "I'm temporarily unavailable — the AI model is not responding. Please try again in a moment.",
                "sources": [],
            }

        sources = []
        for doc in docs:
            src = doc.metadata.get("source", "")
            if src and src not in sources:
                sources.append(src)
        return {"answer": answer, "sources": sources}
