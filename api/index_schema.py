"""
Schema indexer — reads the full live MariaDB schema and stores it in a
ChromaDB collection (`erp_schema`), separate from the docs collection.

Run modes:
  - At container startup via start.sh (automatic on every docker build/restart)
  - Via POST /admin/sync-schema after bench migrate (triggered by Frappe hook)
  - Standalone: python index_schema.py

One document per DocType table is stored. Each doc contains:
  - All real column names (from DESCRIBE)
  - Custom field names (from tabCustom Field)
  - Searchable metadata: table name, doctype name

This lets SchemaRetriever do a semantic search ("which tables have supplier
columns?") rather than injecting the entire schema into every SQL prompt.
"""

import os
import sys
import logging

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL    = os.getenv("OLLAMA_BASE_URL",    "http://host.docker.internal:11434")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "/chroma_data")
SCHEMA_COLLECTION  = "erp_schema"


def _build_documents(schema: dict[str, list[str]]):
    """
    Convert the raw schema dict into LangChain Documents suitable for embedding.
    Content is written as natural language so semantic search works well.
    """
    from langchain_core.documents import Document

    docs = []
    for table, cols in schema.items():
        doctype = table[3:]          # strip leading "tab"
        col_str = ", ".join(cols)
        content = (
            f"DocType: {doctype}\n"
            f"Table: {table}\n"
            f"Columns: {col_str}"
        )
        docs.append(Document(
            page_content=content,
            metadata={"table": table, "doctype": doctype},
        ))
    return docs


def index_schema() -> int:
    """
    Full pipeline: read schema from DB → embed → store in ChromaDB.
    Returns the number of tables indexed. Raises on fatal errors.
    """
    from langchain_ollama import OllamaEmbeddings
    from langchain_chroma import Chroma
    import db

    logger.info("Reading live schema from MariaDB...")
    schema = db.get_all_table_schemas()
    if not schema:
        logger.warning("No tables found — schema collection not updated.")
        return 0

    docs = _build_documents(schema)
    logger.info(f"Building embeddings for {len(docs)} tables...")

    embeddings = OllamaEmbeddings(
        base_url=OLLAMA_BASE_URL,
        model=OLLAMA_EMBED_MODEL,
    )

    # Drop the old collection so stale tables/columns don't linger after migrate
    try:
        old = Chroma(
            collection_name=SCHEMA_COLLECTION,
            embedding_function=embeddings,
            persist_directory=CHROMA_PERSIST_DIR,
        )
        old.delete_collection()
        logger.info("Dropped old erp_schema collection.")
    except Exception:
        pass  # collection didn't exist yet — fine

    Chroma.from_documents(
        documents=docs,
        embedding=embeddings,
        collection_name=SCHEMA_COLLECTION,
        persist_directory=CHROMA_PERSIST_DIR,
    )
    logger.info(f"Schema indexed: {len(docs)} tables stored in '{SCHEMA_COLLECTION}'.")
    return len(docs)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    count = index_schema()
    sys.exit(0 if count > 0 else 1)
