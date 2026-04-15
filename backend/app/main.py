"""
backend/app/main.py

FastAPI application entry-point.

Features:
  - CORS enabled for all origins (dev mode)
  - slowapi rate limiting (shared Limiter instance)
  - SSE streaming via sse_starlette
  - Routers: /api/companies, /api/earnings, /api/chat
"""

import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from backend.app.limiter import limiter
from backend.app.routes.companies import router as companies_router
from backend.app.routes.earnings import router as earnings_router
from backend.app.routes.chat import router as chat_router
from backend.app.routes.evaluation import router as evaluation_router

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Checkit Analytics — Earnings Call RAG API",
    version="1.0.0",
    description=(
        "Retrieval-Augmented Generation over earnings call transcripts "
        "with pgvector similarity search and cross-encoder reranking."
    ),
)

# ── Middleware ──────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Rate limiting ───────────────────────────────────────────────────────────

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Routers ─────────────────────────────────────────────────────────────────

app.include_router(companies_router)
app.include_router(earnings_router)
app.include_router(chat_router)
app.include_router(evaluation_router)


# ── Health check ────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
