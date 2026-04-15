"""
backend/app/routes/chat.py

POST /api/chat
  Body: { ticker, quarter, year, question, session_id? }

Pipeline:
  1. Embed question + pgvector top-10 search  (rag_service)
  2. Cross-encoder rerank to top-7            (reranker_service)
  3. Fetch financial metrics                  (structured_data)
  4. Stream JSON reasoning from Ollama        (reasoning_service)
  5. Log query + result to user_queries table

Events emitted over SSE:
  status  -> { step, message }
  token   -> raw text token from Ollama
  result  -> final parsed JSON analysis
  error   -> { message }
"""

from __future__ import annotations
import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from backend.app.db.database import get_db
from backend.app.db.models import Company, EarningsCall, UserQuery
from backend.app.limiter import limiter
from backend.app.services.rag_service import query_rag
from backend.app.services.reranker_service import rerank
from backend.app.services.structured_data import get_financial_metrics
from backend.app.services.reasoning_service import generate_reasoning
from backend.app.services.query_router import classify_query

import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "qwen2.5:7b-instruct-q4_K_M")

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    ticker:     str
    quarter:    str
    year:       int
    question:   str
    session_id: str | None = None


def _sse(event: str, data: dict | str) -> dict:
    """Helper: build an sse_starlette-compatible event dict."""
    return {
        "event": event,
        "data":  json.dumps(data) if isinstance(data, dict) else data,
    }


@router.post("/chat")
@limiter.limit("20/minute")
async def chat_endpoint(
    request: Request,
    body: ChatRequest,
    db: Session = Depends(get_db),
):
    """Stream an SSE analysis for the given earnings call question."""

    async def event_stream():
        start_time = time.time()
        uq_id: int | None = None

        try:
            # ── 1. Resolve company & earnings call ────────────────────────
            yield _sse("status", {"step": "searching", "message": "Looking up earnings call..."})

            company = (
                db.query(Company)
                .filter(Company.ticker == body.ticker.upper())
                .first()
            )
            if not company:
                yield _sse("error", {"message": f"Company '{body.ticker}' not found."})
                return

            ec = (
                db.query(EarningsCall)
                .filter_by(
                    company_id=company.id,
                    fiscal_quarter=body.quarter.upper(),
                    fiscal_year=body.year,
                )
                .first()
            )
            if not ec:
                yield _sse(
                    "error",
                    {"message": f"No earnings call for {body.ticker} {body.quarter.upper()} FY{body.year}."},
                )
                return

            # ── 2. Create UserQuery record (pre-fill, update after) ───────
            session_id = (
                body.session_id
                or request.headers.get("X-Session-Id")
                or str(uuid.uuid4())
            )
            uq = UserQuery(
                session_id=session_id,
                earnings_call_id=ec.id,
                query_text=body.question,
                query_type="rag_chat",
                model_used=OLLAMA_CHAT_MODEL,
            )
            db.add(uq)
            db.flush()
            uq_id = uq.id

            # ── 3. RAG search ─────────────────────────────────────────────
            yield _sse("status", {"step": "searching", "message": "Searching transcript chunks..."})
            source_types = classify_query(body.question)
            chunks = query_rag(body.ticker, body.quarter, body.year, body.question, db,
                               source_types=source_types or None)

            if not chunks:
                yield _sse("error", {"message": "No transcript chunks found for this earnings call."})
                db.rollback()
                return

            yield _sse(
                "status",
                {"step": "reranking", "message": f"Reranking {len(chunks)} chunks..."},
            )

            # ── 4. Rerank — top 5 ────────────────────────────────────────────
            reranked = await asyncio.to_thread(rerank, body.question, chunks, 7)

            # ── 5. Financial data ─────────────────────────────────────────
            financial_data = get_financial_metrics(ec.id, db)

            # ── 6. Stream reasoning ───────────────────────────────────────
            yield _sse(
                "status",
                {"step": "reasoning", "message": "Generating analysis..."},
            )

            final_result: dict | None = None

            async for event_type, data in generate_reasoning(
                body.ticker,
                body.quarter,
                body.year,
                body.question,
                reranked,
                financial_data,
            ):
                if event_type == "token":
                    yield _sse("token", data)
                elif event_type == "result":
                    final_result = data
                    yield _sse("result", data)
                elif event_type == "error":
                    yield _sse("error", data)

            # ── 7. Update UserQuery with results ──────────────────────────
            if uq_id is not None:
                latency_ms = int((time.time() - start_time) * 1000)
                uq.retrieved_chunk_ids = [c["chunk_id"] for c in chunks]
                uq.rerank_scores = {
                    c["chunk_id"]: c.get("rerank_score") for c in reranked
                }
                uq.latency_ms = latency_ms
                uq.validation_passed = final_result is not None

                if final_result:
                    uq.output_json = final_result
                    uq.confidence  = final_result.get("confidence")
                    evidence = final_result.get("evidence") or []
                    if isinstance(evidence, dict):
                        evidence = [evidence]
                    quoted = sum(1 for e in evidence if isinstance(e, dict) and e.get("quote"))
                    uq.grounding_score = (
                        round(quoted / len(evidence), 2) if evidence else None
                    )

                db.commit()

        except Exception as exc:
            yield _sse("error", {"message": str(exc)})
            try:
                db.rollback()
            except Exception:
                pass

    return EventSourceResponse(event_stream())
