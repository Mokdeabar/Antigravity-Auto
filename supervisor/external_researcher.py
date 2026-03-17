"""
external_researcher.py — V21 External RAG Synthesiser

Gives the agent internet access. When the Temporal Planner flags knowledge
gaps, this module fetches live documentation via Jina Reader, chunks it
into a per-epic ChromaDB vector store, and retrieves the most relevant
snippets for injection into the Gemini Worker's prompt.

Scraping: Jina Reader (r.jina.ai) bypasses Cloudflare and returns clean Markdown.
Storage:  ChromaDB ephemeral collection, wiped when the epic completes.
Retrieval: Top-3 most relevant chunks per query.
"""

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

logger = logging.getLogger("supervisor.external_researcher")

_MEMORY_DIR = Path(__file__).resolve().parent.parent / ".ag-supervisor"
_RAG_DIR = _MEMORY_DIR / "rag_store"

# Maximum chunks to store per document. Prevents bloat from massive pages.
MAX_CHUNKS_PER_DOC = 30
CHUNK_SIZE = 800  # characters per chunk
CHUNK_OVERLAP = 100


class ExternalResearcher:
    """
    Autonomously fetches, chunks, and vectorises external documentation
    for RAG injection into the Gemini Worker's prompt.
    """

    def __init__(self):
        self._collection = None
        self._chroma_client = None
        self._available = self._init_chromadb()

    def _init_chromadb(self) -> bool:
        """Initialise a persistent ChromaDB client. Returns False if unavailable."""
        try:
            import chromadb
            _RAG_DIR.mkdir(parents=True, exist_ok=True)
            self._chroma_client = chromadb.Client()
            self._collection = self._chroma_client.get_or_create_collection(
                name="epic_docs",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info("🌐 ChromaDB RAG store initialised (ephemeral).")
            return True
        except ImportError:
            logger.warning(
                "🌐 ChromaDB not installed. RAG will use fallback in-memory store. "
                "Install with: pip install chromadb"
            )
            # Fallback: simple in-memory list store
            self._fallback_store: List[Dict] = []
            return False
        except Exception as e:
            logger.error("🌐 ChromaDB init failed: %s. Using fallback.", e)
            self._fallback_store: List[Dict] = []
            return False

    # ────────────────────────────────────────────────
    # Fetch (Jina Reader)
    # ────────────────────────────────────────────────

    async def research_gaps(self, knowledge_gaps: List[str]) -> int:
        """
        Fetch documentation for each knowledge gap query.
        Returns the total number of chunks stored.
        """
        total_chunks = 0
        for query in knowledge_gaps:
            safe_query = query.strip()
            if not safe_query:
                continue

            # Append "latest official documentation" to filter out stale content
            search_query = f"{safe_query} latest official documentation"

            docs = await self._fetch_via_jina(search_query)
            if docs:
                chunks = self._chunk_markdown(docs, source=safe_query)
                self._store_chunks(chunks, source=safe_query)
                total_chunks += len(chunks)
                logger.info("🌐 Fetched %d chunks for: %s", len(chunks), safe_query[:50])
            else:
                logger.warning("🌐 No docs fetched for: %s", safe_query[:50])

        return total_chunks

    async def _fetch_via_jina(self, query: str) -> str:
        """
        Use Jina Reader's search endpoint to fetch markdown documentation.
        URL: https://s.jina.ai/{query}
        Falls back to standard requests if Jina is unavailable.
        """
        import asyncio

        jina_url = f"https://s.jina.ai/{quote_plus(query)}"

        try:
            # Use asyncio subprocess to avoid blocking
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sL",
                "-H", "Accept: text/markdown",
                "-H", "X-No-Cache: true",
                jina_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            content = stdout.decode("utf-8", errors="replace").strip()

            if not content or len(content) < 50:
                return ""

            # Filter: reject obviously non-documentation content
            if self._is_noise(content):
                return ""

            return content[:50000]  # Cap at 50k chars

        except Exception as exc:
            logger.warning("🌐 Jina fetch failed for '%s': %s", query[:30], exc)
            return ""

    @staticmethod
    def _is_noise(content: str) -> bool:
        """Reject forum posts, blog spam, and login walls."""
        noise_signals = [
            "sign in to continue",
            "you need to log in",
            "403 forbidden",
            "page not found",
            "stack overflow",
            "reddit.com",
        ]
        lower = content[:500].lower()
        return any(signal in lower for signal in noise_signals)

    # ────────────────────────────────────────────────
    # Chunk
    # ────────────────────────────────────────────────

    @staticmethod
    def _chunk_markdown(text: str, source: str = "") -> List[Dict]:
        """
        Split markdown documentation into overlapping chunks for vector storage.
        Each chunk carries its source metadata.
        """
        chunks = []
        # First, try to split on markdown headers
        sections = re.split(r'\n(?=#{1,3}\s)', text)

        for section in sections:
            section = section.strip()
            if not section or len(section) < 30:
                continue

            # If section is still too large, split by fixed size
            if len(section) > CHUNK_SIZE:
                for i in range(0, len(section), CHUNK_SIZE - CHUNK_OVERLAP):
                    chunk_text = section[i:i + CHUNK_SIZE]
                    if len(chunk_text) > 30:
                        chunks.append({
                            "text": chunk_text,
                            "source": source,
                        })
                    if len(chunks) >= MAX_CHUNKS_PER_DOC:
                        break
            else:
                chunks.append({
                    "text": section,
                    "source": source,
                })

            if len(chunks) >= MAX_CHUNKS_PER_DOC:
                break

        return chunks

    # ────────────────────────────────────────────────
    # Store
    # ────────────────────────────────────────────────

    def _store_chunks(self, chunks: List[Dict], source: str = ""):
        """Store chunks in ChromaDB or fallback in-memory store."""
        if self._available and self._collection is not None:
            ids = []
            documents = []
            metadatas = []
            for i, chunk in enumerate(chunks):
                chunk_id = hashlib.sha256(
                    f"{source}:{i}:{chunk['text'][:50]}".encode()
                ).hexdigest()[:16]
                ids.append(chunk_id)
                documents.append(chunk["text"])
                metadatas.append({"source": chunk.get("source", ""), "index": i})

            if ids:
                self._collection.add(
                    ids=ids,
                    documents=documents,
                    metadatas=metadatas,
                )
        else:
            # Fallback store
            if not hasattr(self, '_fallback_store'):
                self._fallback_store = []
            self._fallback_store.extend(chunks)

    # ────────────────────────────────────────────────
    # Retrieve (RAG Query)
    # ────────────────────────────────────────────────

    def query_docs(self, objective: str, top_k: int = 3) -> str:
        """
        Retrieve the top-k most relevant documentation chunks for the
        given objective. Returns an injectable prompt block.
        """
        if self._available and self._collection is not None:
            try:
                count = self._collection.count()
                if count == 0:
                    return ""

                results = self._collection.query(
                    query_texts=[objective],
                    n_results=min(top_k, count),
                )
                docs = results.get("documents", [[]])[0]
                if not docs:
                    return ""

                lines = ["[EXTERNAL DOCUMENTATION — Retrieved from live sources]"]
                for i, doc in enumerate(docs, 1):
                    lines.append(f"--- Doc {i} ---")
                    lines.append(doc[:600])
                block = "\n".join(lines)
                logger.info(
                    "🌐 Retrieved %d doc chunks (%d chars) for: %s",
                    len(docs), len(block), objective[:50],
                )
                return block

            except Exception as exc:
                logger.warning("🌐 ChromaDB query failed: %s", exc)
                return self._fallback_query(objective, top_k)
        else:
            return self._fallback_query(objective, top_k)

    def _fallback_query(self, objective: str, top_k: int = 3) -> str:
        """Simple keyword-overlap fallback when ChromaDB is unavailable."""
        if not hasattr(self, '_fallback_store') or not self._fallback_store:
            return ""

        # Score by keyword overlap
        keywords = set(objective.lower().split())
        scored = []
        for chunk in self._fallback_store:
            text = chunk["text"].lower()
            score = sum(1 for kw in keywords if kw in text)
            scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [s[1] for s in scored[:top_k] if s[0] > 0]

        if not top:
            return ""

        lines = ["[EXTERNAL DOCUMENTATION — Retrieved from live sources]"]
        for i, chunk in enumerate(top, 1):
            lines.append(f"--- Doc {i} ---")
            lines.append(chunk["text"][:600])

        return "\n".join(lines)

    # ────────────────────────────────────────────────
    # Teardown
    # ────────────────────────────────────────────────

    def teardown(self):
        """Wipe the RAG store after epic completion. External APIs change constantly."""
        if self._available and self._chroma_client is not None:
            try:
                self._chroma_client.delete_collection("epic_docs")
                logger.info("🌐 ChromaDB epic_docs collection wiped.")
            except Exception:
                pass
        if hasattr(self, '_fallback_store'):
            self._fallback_store.clear()
        logger.info("🌐 RAG store torn down.")

    def get_chunk_count(self) -> int:
        """Return current number of stored chunks."""
        if self._available and self._collection is not None:
            return self._collection.count()
        if hasattr(self, '_fallback_store'):
            return len(self._fallback_store)
        return 0
