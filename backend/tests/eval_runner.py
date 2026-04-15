"""
backend/tests/eval_runner.py

Evaluation runner for the CheckIt Analytics RAG pipeline.
Reads test_questions.json, calls /api/chat for each question via SSE,
scores each response, saves results to reasoning_outputs, and prints
a summary dashboard.

Usage:
    python -m backend.tests.eval_runner
    python -m backend.tests.eval_runner --limit 5
    python -m backend.tests.eval_runner --limit 30 --api-url http://localhost:8001

Composite scoring formula:
    0.30 * grounding_score
  + 0.20 * (reasoning_score / 5)
  + 0.15 * consistency_score
  + 0.15 * completeness_score
  + 0.10 * citation_rate
  + 0.10 * (1 - hallucination_rate)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

QUESTIONS_FILE = Path(__file__).parent / "test_questions.json"
API_BASE = os.getenv("API_BASE", "http://localhost:8001")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _verify_citation(e: dict, db) -> bool:
    """
    Return True if the first 30 chars of the evidence quote appear as a
    substring in the actual chunk content fetched from the database.
    """
    from backend.app.db.models import TranscriptChunk

    chunk_id = e.get("chunk_id") or ""
    quote    = (e.get("quote") or "").strip()

    if not chunk_id or not quote:
        return False

    chunk = db.query(TranscriptChunk).filter(TranscriptChunk.id == chunk_id).first()
    if not chunk or not chunk.content:
        return False

    needle = quote[:30].lower()
    return needle in chunk.content.lower()


def score_result(
    result: dict,
    db=None,
    expected_keywords: list[str] | None = None,
) -> dict:
    """Compute all evaluation metrics from a parsed result JSON.

    db               — SQLAlchemy Session; enables DB-backed citation verification.
    expected_keywords — list of keywords that a complete answer should cover;
                        when provided, completeness_score = found / total instead
                        of the key_points-count heuristic.
    """

    # grounding_score: ratio of evidence items whose quote has ≥3 word overlap
    # with the summary (proxy for relevance, not just quote existence).
    evidence = result.get("evidence") or []
    if isinstance(evidence, dict):   # model sometimes returns {} instead of [{}]
        evidence = [evidence]

    summary_text = result.get("summary", "")
    _STOP = {
        "the", "a", "an", "is", "was", "were", "are", "in", "of", "to",
        "for", "and", "or", "on", "at", "by", "from", "with", "that",
        "this", "it", "its", "be", "has", "had", "have", "not", "as",
        "we", "they", "our", "but", "so", "if", "about",
    }
    summary_words = {
        w.lower().strip(".,;:\"'!?") for w in summary_text.split()
        if w.lower().strip(".,;:\"'!?") not in _STOP and len(w) > 2
    }

    def _is_supported(e: dict) -> bool:
        quote = e.get("quote", "") or ""
        if not quote:
            return False
        quote_words = {
            w.lower().strip(".,;:\"'!?") for w in quote.split()
            if w.lower().strip(".,;:\"'!?") not in _STOP and len(w) > 2
        }
        return len(quote_words & summary_words) >= 3

    supported = sum(1 for e in evidence if isinstance(e, dict) and _is_supported(e))
    grounding_score = round(supported / len(evidence), 2) if evidence else 0.0

    # hallucination_rate: proxy using stated confidence level
    confidence = result.get("confidence", "low")
    hallucination_rate = {"high": 0.0, "medium": 0.3, "low": 0.6}.get(confidence, 0.5)

    # consistency_result: raw string from model
    consistency_result = result.get("consistency") or ""
    consistency_score = {"aligned": 1.0, "mixed": 0.5, "conflict": 0.0}.get(
        consistency_result, 0.0
    )

    # reasoning_score (1–5): count of populated structured fields
    reasoning_score = sum(
        1
        for field in ("summary", "key_points", "consistency", "evidence", "confidence")
        if result.get(field)
    )

    # completeness_score (0–1):
    # With expected_keywords: fraction of keywords found in summary + key_points.
    # Fallback: key_points count heuristic (3+ = full credit).
    if expected_keywords:
        combined = (result.get("summary") or "") + " " + " ".join(result.get("key_points") or [])
        combined_lower = combined.lower()
        found = sum(1 for kw in expected_keywords if kw.lower() in combined_lower)
        completeness_score = round(found / len(expected_keywords), 2)
    else:
        kp_count = len(result.get("key_points") or [])
        completeness_score = round(min(kp_count / 3.0, 1.0), 2)

    # citation_rate: ratio of evidence items whose quote verifies against the DB chunk.
    # Falls back to bare quote-existence check when no db session is provided.
    if db is not None:
        verified  = sum(1 for e in evidence if isinstance(e, dict) and _verify_citation(e, db))
        citation_rate = round(verified / len(evidence), 2) if evidence else 0.0
    else:
        citation_rate = 1.0 if any(e.get("quote") for e in evidence if isinstance(e, dict)) else 0.0

    # composite
    composite_score = round(
        0.30 * grounding_score
        + 0.20 * (reasoning_score / 5)
        + 0.15 * consistency_score
        + 0.15 * completeness_score
        + 0.10 * citation_rate
        + 0.10 * (1.0 - hallucination_rate),
        4,
    )

    return {
        "grounding_score":    grounding_score,
        "hallucination_rate": round(hallucination_rate, 2),
        "consistency_result": consistency_result,
        "reasoning_score":    reasoning_score,
        "completeness_score": completeness_score,
        "citation_rate":      citation_rate,
        "composite_score":    composite_score,
    }


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------

async def call_chat_api(
    ticker: str,
    quarter: str,
    year: int,
    question: str,
) -> tuple[dict | None, dict | None]:
    """
    POST to /api/chat and parse the SSE stream.
    Returns (result_dict, error_dict); one of them will be None.
    """
    body = {"ticker": ticker, "quarter": quarter, "year": year, "question": question}
    timeout = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)

    result: dict | None = None
    error: dict | None = None
    current_event = ""

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{API_BASE}/api/chat", json=body
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if line.startswith("event: "):
                        current_event = line[7:].strip()
                    elif line.startswith("data: "):
                        data_str = line[6:].strip()
                        if not data_str or data_str.startswith(":"):
                            continue
                        try:
                            parsed = json.loads(data_str)
                            if current_event == "result":
                                result = parsed
                            elif current_event == "error":
                                error = parsed
                        except json.JSONDecodeError:
                            pass
    except Exception as exc:
        error = {"message": f"{type(exc).__name__}: {exc}"}

    if result is None and error is None:
        error = {"message": "No result or error received from API"}

    return result, error


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def save_to_db(
    db,
    ticker: str,
    quarter: str,
    year: int,
    question: str,
    result: dict,
    scores: dict,
) -> bool:
    """Insert a ReasoningOutput row. Returns True on success."""
    try:
        from backend.app.db.models import Company, EarningsCall, ReasoningOutput

        company = (
            db.query(Company).filter(Company.ticker == ticker.upper()).first()
        )
        if not company:
            return False

        ec = (
            db.query(EarningsCall)
            .filter_by(
                company_id=company.id,
                fiscal_quarter=quarter.upper(),
                fiscal_year=year,
            )
            .first()
        )
        if not ec:
            return False

        ro = ReasoningOutput(
            earnings_call_id=ec.id,
            query_text=question,
            output_json=result,
            grounding_score=scores["grounding_score"],
            hallucination_rate=scores["hallucination_rate"],
            consistency_result=scores["consistency_result"],
            reasoning_score=scores["reasoning_score"],
            completeness_score=scores["completeness_score"],
            citation_rate=scores["citation_rate"],
            composite_score=scores["composite_score"],
        )
        db.add(ro)
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        print(f"  DB save failed: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Eval loop
# ---------------------------------------------------------------------------

async def run_eval(questions: list[dict], db) -> list[dict]:
    records = []

    for i, q in enumerate(questions, 1):
        ticker   = q["ticker"]
        quarter  = q["quarter"]
        year     = q["year"]
        question = q["question"]
        category = q.get("category", "unknown")

        print(f"\n[{i}/{len(questions)}] {ticker} {quarter} FY{year}  [{category}]")
        print(f"  Q: {question[:90]}{'...' if len(question) > 90 else ''}")

        if i > 1:
            await asyncio.sleep(3)
        result, error = await call_chat_api(ticker, quarter, year, question)

        if error:
            msg = error.get("message", "unknown error")
            print(f"  ERROR: {msg[:120]}")
            records.append({"q": q, "error": error, "scores": None})
            continue

        scores = score_result(result, db, expected_keywords=q.get("expected_keywords"))
        saved  = save_to_db(db, ticker, quarter, year, question, result, scores)

        print(
            f"  composite={scores['composite_score']:.3f}  "
            f"grounding={scores['grounding_score']:.2f}  "
            f"reasoning={scores['reasoning_score']}/5  "
            f"confidence={result.get('confidence', '?')}  "
            f"db={'saved' if saved else 'SKIP'}"
        )
        records.append({"q": q, "result": result, "scores": scores})

    return records


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def print_dashboard(records: list[dict]) -> None:
    scored = [r for r in records if r["scores"] is not None]
    errors = [r for r in records if r.get("error")]

    sep = "=" * 62
    print(f"\n{sep}")
    print("  EVALUATION DASHBOARD")
    print(sep)
    print(f"  Total questions  : {len(records)}")
    print(f"  Successful       : {len(scored)}")
    print(f"  Errors / skipped : {len(errors)}")

    if not scored:
        print("\n  No successful results to report.")
        print(sep)
        return

    def avg(key: str) -> float:
        return sum(r["scores"][key] for r in scored) / len(scored)

    print(f"\n  Average scores ({len(scored)} responses):")
    print(f"    Composite        : {avg('composite_score'):.3f}")
    print(f"    Grounding        : {avg('grounding_score'):.3f}")
    print(f"    Completeness     : {avg('completeness_score'):.3f}")
    print(f"    Citation rate    : {avg('citation_rate'):.3f}")
    print(f"    Hallucination    : {avg('hallucination_rate'):.3f}")
    print(f"    Reasoning score  : {avg('reasoning_score'):.2f} / 5")

    # Per-category breakdown
    by_cat: dict[str, list[float]] = defaultdict(list)
    for r in scored:
        by_cat[r["q"].get("category", "unknown")].append(
            r["scores"]["composite_score"]
        )

    print("\n  By category:")
    for cat in sorted(by_cat):
        scores = by_cat[cat]
        cat_avg = sum(scores) / len(scores)
        print(f"    {cat:<15}  avg={cat_avg:.3f}  n={len(scores)}")

    # Lowest-scoring questions
    sorted_records = sorted(scored, key=lambda r: r["scores"]["composite_score"])
    print("\n  Lowest-scoring questions:")
    for r in sorted_records[:5]:
        q = r["q"]
        print(
            f"    [{r['scores']['composite_score']:.3f}] "
            f"{q['ticker']} {q['quarter']} FY{q['year']}  "
            f"{q['question'][:55]}..."
        )

    # Highest-scoring questions
    print("\n  Highest-scoring questions:")
    for r in sorted_records[-5:][::-1]:
        q = r["q"]
        print(
            f"    [{r['scores']['composite_score']:.3f}] "
            f"{q['ticker']} {q['quarter']} FY{q['year']}  "
            f"{q['question'][:55]}..."
        )

    # Error summary
    if errors:
        print(f"\n  Failed questions ({len(errors)}):")
        for r in errors:
            q = r["q"]
            msg = r["error"].get("message", "")[:60]
            print(f"    {q['ticker']} {q['quarter']} FY{q['year']}  {msg}")

    print(sep)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global API_BASE

    parser = argparse.ArgumentParser(
        description="Eval runner for CheckIt Analytics RAG pipeline"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        metavar="N",
        help="Run only the first N questions (default: 30 = full suite)",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        default=API_BASE,
        metavar="URL",
        help=f"API base URL (default: {API_BASE})",
    )
    args = parser.parse_args()

    API_BASE = args.api_url

    if not QUESTIONS_FILE.exists():
        print(f"ERROR: {QUESTIONS_FILE} not found.", file=sys.stderr)
        sys.exit(1)

    with open(QUESTIONS_FILE) as f:
        questions: list[dict] = json.load(f)

    if args.limit:
        questions = questions[: args.limit]

    print(f"CheckIt Analytics — Evaluation Runner")
    print(f"API          : {API_BASE}")
    print(f"Questions    : {len(questions)}")
    print(f"Questions file: {QUESTIONS_FILE}")

    from backend.app.db.database import SessionLocal

    db = SessionLocal()
    try:
        records = asyncio.run(run_eval(questions, db))
    finally:
        db.close()

    print_dashboard(records)


if __name__ == "__main__":
    main()
