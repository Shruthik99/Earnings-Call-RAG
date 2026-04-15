"""
backend/app/routes/evaluation.py

GET /api/evaluation         — all reasoning_output rows with category + ticker
GET /api/evaluation/summary — aggregated metrics + per-category breakdown
"""

from __future__ import annotations
import json
import os

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.app.db.database import get_db
from backend.app.db.models import ReasoningOutput, EarningsCall, Company

router = APIRouter(prefix="/api", tags=["evaluation"])

# ---------------------------------------------------------------------------
# Category lookup — matches query_text against test_questions.json
# ---------------------------------------------------------------------------

_TEST_Q_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "tests", "test_questions.json"
)


def _question_map() -> dict[str, dict]:
    try:
        with open(_TEST_Q_PATH, encoding="utf-8") as f:
            qs = json.load(f)
        return {q["question"]: q for q in qs}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _f(v):
    """Coerce Decimal/None to float/None."""
    return float(v) if v is not None else None


def _avg(lst: list[float]) -> float | None:
    return round(sum(lst) / len(lst), 4) if lst else None


# ---------------------------------------------------------------------------
# GET /api/evaluation/summary
# ---------------------------------------------------------------------------

@router.get("/evaluation/summary")
def evaluation_summary(db: Session = Depends(get_db)):
    rows = (
        db.query(ReasoningOutput, Company.ticker)
        .join(EarningsCall, ReasoningOutput.earnings_call_id == EarningsCall.id)
        .join(Company, EarningsCall.company_id == Company.id)
        .all()
    )

    if not rows:
        return {
            "total_questions": 0,
            "composite_score": None,
            "metrics": {},
            "by_category": {},
            "last_run": None,
        }

    q_map = _question_map()

    buckets: dict[str, list[float]] = {
        "grounding": [],
        "hallucination_rate": [],
        "reasoning_score": [],   # stored as 1-5, normalised to 0-1 here
        "completeness": [],
        "citation_rate": [],
        "composite": [],
    }
    by_category: dict[str, list[float]] = {}

    for ro, _ticker in rows:
        g   = _f(ro.grounding_score)
        h   = _f(ro.hallucination_rate)
        r   = _f(ro.reasoning_score)
        c   = _f(ro.completeness_score)
        cit = _f(ro.citation_rate)
        comp = _f(ro.composite_score)

        if g    is not None: buckets["grounding"].append(g)
        if h    is not None: buckets["hallucination_rate"].append(h)
        if r    is not None: buckets["reasoning_score"].append(r / 5.0)
        if c    is not None: buckets["completeness"].append(c)
        if cit  is not None: buckets["citation_rate"].append(cit)
        if comp is not None: buckets["composite"].append(comp)

        q_info = q_map.get(ro.query_text)
        cat = q_info.get("category", "other") if q_info else "other"
        by_category.setdefault(cat, [])
        if comp is not None:
            by_category[cat].append(comp)

    targets = {
        "grounding":        {"target": 0.75, "higher_is_better": True},
        "hallucination_rate": {"target": 0.20, "higher_is_better": False},
        "reasoning_score":  {"target": 0.80, "higher_is_better": True},
        "completeness":     {"target": 0.70, "higher_is_better": True},
        "citation_rate":    {"target": 0.80, "higher_is_better": True},
    }

    metrics: dict = {}
    for key, cfg in targets.items():
        avg_val = _avg(buckets[key])
        if avg_val is None:
            passed = None
        elif cfg["higher_is_better"]:
            passed = avg_val >= cfg["target"]
        else:
            passed = avg_val <= cfg["target"]
        metrics[key] = {
            "avg":    avg_val,
            "target": cfg["target"],
            "pass":   passed,
            "higher_is_better": cfg["higher_is_better"],
        }

    last_run = max((ro.created_at for ro, _ in rows if ro.created_at), default=None)

    return {
        "total_questions": len(rows),
        "composite_score": _avg(buckets["composite"]),
        "metrics": metrics,
        "by_category": {
            cat: {"composite": _avg(scores), "count": len(scores)}
            for cat, scores in sorted(by_category.items())
        },
        "last_run": str(last_run) if last_run else None,
    }


# ---------------------------------------------------------------------------
# GET /api/evaluation
# ---------------------------------------------------------------------------

@router.get("/evaluation")
def evaluation_results(db: Session = Depends(get_db)):
    rows = (
        db.query(ReasoningOutput, Company.ticker)
        .join(EarningsCall, ReasoningOutput.earnings_call_id == EarningsCall.id)
        .join(Company, EarningsCall.company_id == Company.id)
        .order_by(ReasoningOutput.composite_score.desc())
        .all()
    )

    q_map = _question_map()
    out = []

    for ro, ticker in rows:
        q_info = q_map.get(ro.query_text)
        out.append({
            "id":                 ro.id,
            "ticker":             ticker,
            "question":           ro.query_text,
            "category":           q_info.get("category", "other") if q_info else "other",
            "grounding_score":    _f(ro.grounding_score),
            "hallucination_rate": _f(ro.hallucination_rate),
            "reasoning_score":    int(ro.reasoning_score) if ro.reasoning_score is not None else None,
            "completeness_score": _f(ro.completeness_score),
            "citation_rate":      _f(ro.citation_rate),
            "composite_score":    _f(ro.composite_score),
            "consistency":        ro.consistency_result,
            "created_at":         str(ro.created_at) if ro.created_at else None,
        })

    return out
