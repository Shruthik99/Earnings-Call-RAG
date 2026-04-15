"""
backend/app/services/rag_service.py

Hybrid retrieval: pgvector cosine similarity + keyword ILIKE search,
fused with Reciprocal Rank Fusion (RRF), then sent to the reranker.

query_rag() accepts an optional source_types filter produced by
query_router.classify_query().
"""

import os
import re
import requests as _requests
from dotenv import load_dotenv
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.app.db.models import TranscriptChunk, EarningsCall, Company

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

OLLAMA_HOST        = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

_RRF_K = 60  # standard RRF constant

# Filing source types live on annual/standalone calls (10-K, 10-Q, 8-K),
# NOT on the quarterly transcript call, so quarter filter must be dropped for them.
_FILING_SOURCE_TYPES = {"filing_risk", "filing_business", "filing_mda", "filing_8k", "filing_10q"}

_STOP_WORDS = {
    "what", "how", "did", "the", "a", "an", "is", "was", "were", "are",
    "in", "of", "to", "for", "and", "or", "on", "at", "by", "from",
    "with", "that", "this", "it", "its", "their", "about", "which",
    "who", "when", "where", "than", "be", "been", "has", "had", "have",
    "do", "does", "not", "no", "per", "as", "they", "we", "our", "you",
    "your", "he", "she", "his", "her", "during", "management", "company",
    "quarter", "year", "fy", "q1", "q2", "q3", "q4",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def embed_text(text: str) -> list:
    """Return a 768-dim embedding for *text* via Ollama."""
    resp = _requests.post(
        f"{OLLAMA_HOST}/api/embeddings",
        json={"model": OLLAMA_EMBED_MODEL, "prompt": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def _extract_keywords(question: str) -> list[str]:
    """Return meaningful tokens from the question for ILIKE matching."""
    words = re.sub(r"[^\w\s]", " ", question.lower()).split()
    return [w for w in words if w not in _STOP_WORDS and len(w) > 2]


def _chunk_to_dict(chunk: TranscriptChunk, similarity: float = 0.0) -> dict:
    return {
        "chunk_id":        chunk.id,
        "content":         chunk.content,
        "enriched_content": chunk.enriched_content,
        "source_type":     chunk.source_type,
        "section":         chunk.section,
        "speaker_name":    chunk.speaker_name,
        "speaker_role":    chunk.speaker_role,
        "chunk_index":     chunk.chunk_index,
        "similarity":      similarity,
    }


# ---------------------------------------------------------------------------
# Main retrieval function
# ---------------------------------------------------------------------------

def _vec_search(
    query_vec: list,
    ticker: str,
    year: int,
    db: Session,
    top_k: int,
    quarter: str | None,
    source_types: list[str] | None,
) -> list:
    """Run pgvector cosine search. If quarter is None, searches the whole fiscal year."""
    q = (
        db.query(
            TranscriptChunk,
            TranscriptChunk.embedding.cosine_distance(query_vec).label("distance"),
        )
        .join(EarningsCall, TranscriptChunk.earnings_call_id == EarningsCall.id)
        .join(Company, EarningsCall.company_id == Company.id)
        .filter(Company.ticker == ticker.upper(), EarningsCall.fiscal_year == int(year))
    )
    if quarter is not None:
        q = q.filter(EarningsCall.fiscal_quarter == quarter.upper())
    if source_types:
        q = q.filter(TranscriptChunk.source_type.in_(source_types))
    return (
        q.order_by(TranscriptChunk.embedding.cosine_distance(query_vec))
        .limit(top_k)
        .all()
    )


def _kw_search(
    keywords: list[str],
    ticker: str,
    year: int,
    db: Session,
    quarter: str | None,
    source_types: list[str] | None,
) -> list:
    """Run keyword ILIKE search. If quarter is None, searches the whole fiscal year."""
    if not keywords:
        return []
    ilike_clauses = [TranscriptChunk.content.ilike(f"%{kw}%") for kw in keywords]
    q = (
        db.query(TranscriptChunk)
        .join(EarningsCall, TranscriptChunk.earnings_call_id == EarningsCall.id)
        .join(Company, EarningsCall.company_id == Company.id)
        .filter(
            Company.ticker == ticker.upper(),
            EarningsCall.fiscal_year == int(year),
            or_(*ilike_clauses),
        )
    )
    if quarter is not None:
        q = q.filter(EarningsCall.fiscal_quarter == quarter.upper())
    if source_types:
        q = q.filter(TranscriptChunk.source_type.in_(source_types))
    return q.limit(10).all()


def query_rag(
    ticker: str,
    quarter: str,
    year: int,
    question: str,
    db: Session,
    top_k: int = 10,
    source_types: list[str] | None = None,
) -> list:
    """
    Hybrid retrieval:
      1. pgvector cosine-similarity search (top_k results)
      2. Keyword ILIKE search on content (top 10 results)
      3. Merge via Reciprocal Rank Fusion, return top_k

    Filing source types (filing_risk, filing_mda, etc.) are stored on annual
    calls (10-K), not on quarterly transcript calls, so they are searched
    year-wide (no fiscal_quarter filter). Transcript types are restricted to
    the requested quarter.

    Each returned dict contains:
      chunk_id, content, enriched_content, source_type, section,
      speaker_name, speaker_role, chunk_index, similarity (float 0-1)
    """
    query_vec = embed_text(question)
    keywords  = _extract_keywords(question)

    # Split source_types into filing (year-wide) vs transcript (quarter-specific)
    if source_types:
        filing_types     = [s for s in source_types if s in _FILING_SOURCE_TYPES]
        transcript_types = [s for s in source_types if s not in _FILING_SOURCE_TYPES]
    else:
        filing_types = transcript_types = []

    # ── 1. Vector search ─────────────────────────────────────────────────────
    vec_rows: list = []

    if source_types:
        if transcript_types:
            vec_rows += _vec_search(query_vec, ticker, year, db, top_k,
                                    quarter=quarter, source_types=transcript_types)
        if filing_types:
            vec_rows += _vec_search(query_vec, ticker, year, db, top_k,
                                    quarter=None, source_types=filing_types)
    else:
        # No routing — search the specific quarter for all source types
        vec_rows = _vec_search(query_vec, ticker, year, db, top_k,
                               quarter=quarter, source_types=None)

    # Fallback: if still nothing, retry the specific quarter with no type filter
    if not vec_rows:
        vec_rows = _vec_search(query_vec, ticker, year, db, top_k,
                               quarter=quarter, source_types=None)

    # Build ranked vector dict: chunk_id → (rank, dict)
    vec_map: dict[str, tuple[int, dict]] = {}
    for rank, (chunk, distance) in enumerate(vec_rows, start=1):
        vec_map[chunk.id] = (rank, _chunk_to_dict(chunk, round(1.0 - float(distance), 4)))

    # ── 2. Keyword search ─────────────────────────────────────────────────────
    kw_map: dict[str, tuple[int, dict]] = {}

    if keywords:
        kw_rows: list = []
        if source_types:
            if transcript_types:
                kw_rows += _kw_search(keywords, ticker, year, db,
                                      quarter=quarter, source_types=transcript_types)
            if filing_types:
                kw_rows += _kw_search(keywords, ticker, year, db,
                                      quarter=None, source_types=filing_types)
        else:
            kw_rows = _kw_search(keywords, ticker, year, db,
                                 quarter=quarter, source_types=None)

        for rank, chunk in enumerate(kw_rows, start=1):
            kw_map[chunk.id] = (rank, _chunk_to_dict(chunk))

    # ── 3. Reciprocal Rank Fusion ────────────────────────────────────────────
    all_ids = set(vec_map) | set(kw_map)
    fused: list[tuple[float, dict]] = []

    for cid in all_ids:
        score = 0.0
        if cid in vec_map:
            score += 1.0 / (_RRF_K + vec_map[cid][0])
        if cid in kw_map:
            score += 1.0 / (_RRF_K + kw_map[cid][0])

        # Prefer the vector dict (has similarity); fall back to keyword dict
        chunk_dict = vec_map[cid][1] if cid in vec_map else kw_map[cid][1]
        fused.append((score, chunk_dict))

    fused.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in fused[:top_k]]
