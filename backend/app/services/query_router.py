"""
backend/app/services/query_router.py

Classifies a question into preferred source_types for the RAG retrieval
filter.  Returns an empty list if no clear signal (→ search all sources).
"""


def classify_query(question: str) -> list[str]:
    q = question.lower()
    if any(w in q for w in ["risk", "concern", "threat", "challenge", "headwind", "worry"]):
        return ["filing_risk", "transcript_qa"]
    elif any(w in q for w in ["ceo", "cfo", "management said", "executive", "leadership"]):
        return ["transcript_prepared", "transcript_qa"]
    elif any(w in q for w in ["revenue", "eps", "earnings", "beat", "miss", "financial", "margin", "profit"]):
        return ["filing_mda", "transcript_prepared"]
    elif any(w in q for w in ["business", "strategy", "segment", "product", "service"]):
        return ["filing_business", "filing_mda"]
    elif any(w in q for w in ["outlook", "guidance", "forecast", "next quarter", "future"]):
        return ["transcript_prepared", "filing_mda"]
    else:
        return []
