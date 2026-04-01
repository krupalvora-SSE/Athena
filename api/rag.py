import os
import logging
from langchain_ollama import OllamaLLM
from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma
from langchain.prompts import PromptTemplate
from langchain.chains import RetrievalQA
from langchain.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL   = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "/chroma_data")
CHROMA_COLLECTION  = "erp_docs"

PROMPT_TEMPLATE = """You are an internal support assistant for Frappe/ERPNext.
Answer the question using only the context provided. If the answer is not in the context, say you don't know — do not make up information.

Context:
{context}

Question: {question}

Answer:"""


class RAGPipeline:
    def __init__(self):
        self._ready = False
        self._setup()

    def _setup(self):
        logger.info(f"Connecting to Ollama at {OLLAMA_BASE_URL} with model {OLLAMA_MODEL}")
        self.llm = OllamaLLM(
            base_url=OLLAMA_BASE_URL,
            model=OLLAMA_MODEL,
            temperature=0.1,
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
        vector_retriever = self.vectorstore.as_retriever(
            search_type="similarity",
            search_kwargs={"k": 12},
        )
        self.retriever = EnsembleRetriever(
            retrievers=[bm25_retriever, vector_retriever],
            weights=[0.5, 0.5],
        )
        prompt = PromptTemplate(
            template=PROMPT_TEMPLATE,
            input_variables=["context", "question"],
        )
        self.chain = RetrievalQA.from_chain_type(
            llm=self.llm,
            chain_type="stuff",
            retriever=self.retriever,
            return_source_documents=True,
            chain_type_kwargs={"prompt": prompt},
        )
        self._ready = True
        logger.info("RAG pipeline initialized.")

    def is_ready(self) -> bool:
        return self._ready

    def query(self, question: str, history: str = "", user_context: str = "") -> dict:
        # Step 1: retrieve relevant docs using ONLY the current question.
        # Never pass history/user_context to the embedder — it has a short context
        # window and large role lists will exceed it.
        docs = self.retriever.invoke(question)

        # Step 2: build the context block from retrieved docs (cap at 6 to stay within LLM window).
        context = "\n\n".join(doc.page_content for doc in docs[:6])

        # Step 3: build the full LLM prompt — history and user context live here only.
        sections = [
            "You are an internal support assistant for Frappe/ERPNext.",
            "Answer using only the context provided. If the answer is not in the context, "
            "say you don't know — do not make up information.",
        ]
        if user_context:
            sections.append(f"[User context]\n{user_context}")
        if history:
            # Truncate history to last ~1500 chars to stay well within LLM context
            trimmed_history = history[-1500:] if len(history) > 1500 else history
            sections.append(f"[Conversation history]\n{trimmed_history}")
        sections.append(f"[Context from docs]\n{context}")
        sections.append(f"Question: {question}\nAnswer:")

        full_prompt = "\n\n".join(sections)

        # Step 4: call LLM directly (bypasses RetrievalQA so embedding stays short).
        answer = self.llm.invoke(full_prompt)

        sources = []
        for doc in docs:
            src = doc.metadata.get("source", "")
            if src and src not in sources:
                sources.append(src)
        return {"answer": answer, "sources": sources}
