"""
SchemaRetriever — semantic search over the erp_schema ChromaDB collection.

At NL-to-SQL time, retrieves only the k most relevant table schemas for the
user's question instead of injecting the full database schema into the prompt.

Example:
  "how many submitted delivery notes this month?"
  → returns only tabDelivery Note schema (not all 100+ tables)
  → LLM generates correct SQL using real columns only
"""

import os
import logging

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL    = os.getenv("OLLAMA_BASE_URL",    "http://host.docker.internal:11434")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "/chroma_data")
SCHEMA_COLLECTION  = "erp_schema"


class SchemaRetriever:
    """
    Wraps the erp_schema ChromaDB collection and exposes a single method:
      get_relevant_schemas(question, k) → schema string for SQL prompt injection.
    """

    def __init__(self):
        self._ready = False
        self._setup()

    def _setup(self):
        try:
            from langchain_ollama import OllamaEmbeddings
            from langchain_chroma import Chroma

            embeddings = OllamaEmbeddings(
                base_url=OLLAMA_BASE_URL,
                model=OLLAMA_EMBED_MODEL,
            )
            self._vectorstore = Chroma(
                collection_name=SCHEMA_COLLECTION,
                embedding_function=embeddings,
                persist_directory=CHROMA_PERSIST_DIR,
            )
            count = self._vectorstore._collection.count()
            if count == 0:
                logger.warning(
                    "erp_schema collection is empty — run index_schema.py first. "
                    "NL-to-SQL will fall back to no schema context."
                )
            else:
                logger.info(f"SchemaRetriever ready: {count} tables indexed.")
                self._ready = True
        except Exception as e:
            logger.warning(f"SchemaRetriever init failed: {e}")

    def is_ready(self) -> bool:
        return self._ready

    def get_relevant_schemas(self, question: str, k: int = 4) -> str:
        """
        Semantic search: return the schema strings for the k most relevant
        tables to the question. Returns empty string if not ready.

        The returned string is injected directly into the SQL generation prompt,
        so the LLM only sees real column names — not hallucinated ones.
        """
        if not self._ready:
            return ""
        try:
            docs = self._vectorstore.similarity_search(question, k=k)
            return "\n\n".join(doc.page_content for doc in docs)
        except Exception as e:
            logger.warning(f"Schema retrieval failed: {e}")
            return ""

    def reload(self):
        """Re-initialise after index_schema.py has re-indexed the collection."""
        self._ready = False
        self._setup()
