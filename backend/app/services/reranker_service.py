"""
backend/app/services/reranker_service.py

Cross-encoder reranking using cross-encoder/ms-marco-MiniLM-L-6-v2.
Model is loaded lazily on first call to avoid slowing startup.
"""

from __future__ import annotations
from functools import lru_cache

CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


@lru_cache(maxsize=1)
def _get_model():
    from sentence_transformers import CrossEncoder
    return CrossEncoder(CROSS_ENCODER_MODEL)


def rerank(question: str, chunks: list, top_k: int = 5) -> list:
    """
    Score each chunk against *question* with the cross-encoder and
    return the *top_k* highest-scoring chunks with a 'rerank_score' key.
    """
    if not chunks:
        return []

    model = _get_model()
    pairs = [(question, c["content"]) for c in chunks]
    scores = model.predict(pairs)

    ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)

    return [
        {**chunk, "rerank_score": round(float(score), 4)}
        for score, chunk in ranked[:top_k]
    ]
