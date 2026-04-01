"""
One-time script to ingest ERPNextDocs into ChromaDB.
Run this from outside Docker, pointing at the ChromaDB persist directory.

Usage:
    DOCS_DIR=/path/to/ERPNextDocs CHROMA_DIR=/path/to/chroma_data python index_docs.py
"""

import os
import logging
from pathlib import Path

from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter
from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOCS_DIR = os.getenv(
    "DOCS_DIR",
    "/Users/krupalvora/Desktop/frappe_docker/development/frappe-bench/apps/solar_square/ERPNextDocs",
)
CHROMA_DIR = os.getenv("CHROMA_DIR", "/Users/krupalvora/Desktop/chatbot/chroma_data")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
CHROMA_COLLECTION = "erp_docs"


def ingest():
    docs_path = Path(DOCS_DIR)
    if not docs_path.exists():
        raise FileNotFoundError(f"Docs directory not found: {DOCS_DIR}")

    logger.info(f"Loading documents from {DOCS_DIR}")
    loader = DirectoryLoader(
        DOCS_DIR,
        glob="**/*.md",
        loader_cls=TextLoader,
        loader_kwargs={"encoding": "utf-8"},
        show_progress=True,
    )
    docs = loader.load()
    logger.info(f"Loaded {len(docs)} documents")

    # Step 1: extract H1 title from each doc for use as a prefix on every chunk.
    def get_title(doc) -> str:
        for line in doc.page_content.splitlines():
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
        return Path(doc.metadata.get("source", "")).stem

    title_map = {doc.metadata["source"]: get_title(doc) for doc in docs}

    # Step 2: split on ## headers only. ### subsections (states tables, hook
    # descriptions) stay together within their parent section, giving the LLM
    # enough surrounding context to synthesize answers.
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("##", "section")],
        strip_headers=False,
    )

    # Step 3: 900 chars is the precise sweet spot:
    #   - PO states table (all 6 including Finance Approval) = ~890 chars → fits
    #   - PKL validate section lands in its own overlap chunk = ~850 chars → fits
    #   - GRN workflow states table stays intact = ~540 chars → well within limit
    size_splitter = RecursiveCharacterTextSplitter(
        chunk_size=900,
        chunk_overlap=100,
        separators=["\n\n", "\n", " ", ""],
    )

    chunks = []
    for doc in docs:
        source = doc.metadata.get("source", "")
        title = title_map.get(source, "")
        header_chunks = header_splitter.split_text(doc.page_content)
        for hchunk in header_chunks:
            # Carry over source metadata (MarkdownHeaderTextSplitter drops it)
            hchunk.metadata["source"] = source
        # Size-split FIRST, then add prefix — if we prefix before splitting,
        # RecursiveCharacterTextSplitter splits on the "\n\n" in "Title\n\n## Section"
        # and creates a tiny orphan "Source: Title" chunk with no content.
        size_chunks = size_splitter.split_documents(header_chunks)
        for chunk in size_chunks:
            chunk.page_content = f"Source: {title}\n\n{chunk.page_content}"
        chunks.extend(size_chunks)

    logger.info(f"Split into {len(chunks)} chunks")

    logger.info(f"Embedding with {OLLAMA_EMBED_MODEL} via {OLLAMA_BASE_URL}")
    embeddings = OllamaEmbeddings(base_url=OLLAMA_BASE_URL, model=OLLAMA_EMBED_MODEL)

    vectorstore = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        collection_name=CHROMA_COLLECTION,
        persist_directory=CHROMA_DIR,
    )
    logger.info(f"Ingested {len(chunks)} chunks into ChromaDB at {CHROMA_DIR}")


if __name__ == "__main__":
    ingest()
