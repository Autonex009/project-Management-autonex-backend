"""
Knowledge service for policy document retrieval (RAG pipeline).

Uses Gemini text-embedding-004 for embedding generation and cosine similarity
for retrieval. At the current document scale (~5 policy files), in-memory search
is more than sufficient — no external vector DB required.
"""
import os
import logging
import re
from pathlib import Path
from typing import Optional

import numpy as np
from google import genai

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────
KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
EMBEDDING_MODEL = "gemini-embedding-001"
CHUNK_SIZE = 500  # characters per chunk
CHUNK_OVERLAP = 100
TOP_K = 5

# ── In-memory store ─────────────────────────────────────────────────
_chunks: list[dict] = []  # [{text, source, section, embedding}]
_initialized = False


def _get_client() -> genai.Client:
    """Lazily create a Gemini client."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment")
    return genai.Client(api_key=api_key)


def _split_into_chunks(text: str, source: str) -> list[dict]:
    """Split a document into overlapping chunks, preserving section headers."""
    chunks = []
    # Split by markdown headers first
    sections = re.split(r'(?=^## )', text, flags=re.MULTILINE)

    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Extract section header if present
        header_match = re.match(r'^## (.+)', section)
        section_title = header_match.group(1).strip() if header_match else ""

        # If section is small enough, keep it whole
        if len(section) <= CHUNK_SIZE:
            chunks.append({
                "text": section,
                "source": source,
                "section": section_title,
            })
        else:
            # Split long sections into overlapping chunks
            words = section.split()
            current_chunk = []
            current_len = 0

            for word in words:
                current_chunk.append(word)
                current_len += len(word) + 1

                if current_len >= CHUNK_SIZE:
                    chunk_text = " ".join(current_chunk)
                    chunks.append({
                        "text": chunk_text,
                        "source": source,
                        "section": section_title,
                    })
                    # Keep overlap
                    overlap_words = current_chunk[-(CHUNK_OVERLAP // 5):]
                    current_chunk = overlap_words
                    current_len = sum(len(w) + 1 for w in current_chunk)

            if current_chunk:
                chunk_text = " ".join(current_chunk)
                chunks.append({
                    "text": chunk_text,
                    "source": source,
                    "section": section_title,
                })

    return chunks


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def _keyword_score(query: str, text: str) -> float:
    """Simple BM25-like keyword matching score."""
    query_words = set(query.lower().split())
    text_lower = text.lower()
    matches = sum(1 for w in query_words if w in text_lower)
    return matches / max(len(query_words), 1)


def initialize_knowledge_base():
    """Load all policy documents, chunk them, and compute embeddings."""
    global _chunks, _initialized

    if _initialized:
        return

    if not KNOWLEDGE_DIR.exists():
        logger.warning("Knowledge directory not found: %s", KNOWLEDGE_DIR)
        _initialized = True
        return

    # Load and chunk all markdown files
    all_chunks = []
    for md_file in sorted(KNOWLEDGE_DIR.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
            source_name = md_file.stem.replace("_", " ").title()
            file_chunks = _split_into_chunks(content, source_name)
            all_chunks.extend(file_chunks)
            logger.info("Loaded %d chunks from %s", len(file_chunks), md_file.name)
        except Exception as e:
            logger.error("Failed to load %s: %s", md_file.name, e)

    if not all_chunks:
        logger.warning("No knowledge documents found")
        _initialized = True
        return

    # Generate embeddings
    try:
        client = _get_client()
        texts = [c["text"] for c in all_chunks]

        # Batch embed (API supports up to 100 texts per call)
        batch_size = 50
        all_embeddings = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            result = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=batch,
            )
            for embedding in result.embeddings:
                all_embeddings.append(np.array(embedding.values, dtype=np.float32))

        for chunk, embedding in zip(all_chunks, all_embeddings):
            chunk["embedding"] = embedding

        _chunks = all_chunks
        _initialized = True
        logger.info("Knowledge base initialized: %d chunks with embeddings", len(_chunks))

    except Exception as e:
        logger.error("Failed to generate embeddings: %s", e)
        # Fall back to keyword-only search
        _chunks = all_chunks
        _initialized = True
        logger.info("Knowledge base initialized (keyword-only mode): %d chunks", len(_chunks))


def search_policy(query: str, top_k: int = TOP_K) -> list[dict]:
    """
    Hybrid search: embedding similarity + keyword matching.
    Returns top_k most relevant chunks with scores.
    """
    if not _initialized:
        initialize_knowledge_base()

    if not _chunks:
        return []

    results = []

    # Check if embeddings are available
    has_embeddings = _chunks[0].get("embedding") is not None

    if has_embeddings:
        try:
            client = _get_client()
            query_result = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=[query],
            )
            query_embedding = np.array(query_result.embeddings[0].values, dtype=np.float32)

            for chunk in _chunks:
                # Hybrid score: 70% embedding similarity + 30% keyword
                emb_score = _cosine_similarity(query_embedding, chunk["embedding"])
                kw_score = _keyword_score(query, chunk["text"])
                combined = 0.7 * emb_score + 0.3 * kw_score

                results.append({
                    "text": chunk["text"],
                    "source": chunk["source"],
                    "section": chunk["section"],
                    "score": combined,
                })
        except Exception as e:
            logger.error("Embedding search failed, falling back to keyword: %s", e)
            has_embeddings = False

    if not has_embeddings:
        # Keyword-only fallback
        for chunk in _chunks:
            kw_score = _keyword_score(query, chunk["text"])
            results.append({
                "text": chunk["text"],
                "source": chunk["source"],
                "section": chunk["section"],
                "score": kw_score,
            })

    # Sort by score descending, return top_k
    results.sort(key=lambda x: x["score"], reverse=True)
    # Filter out very low scores
    results = [r for r in results if r["score"] > 0.1]
    return results[:top_k]
