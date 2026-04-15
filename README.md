# Checkit Analytics - Earnings Call RAG Module
# Checkit Analytics

**Earnings Intelligence Platform** — Transforms earnings call transcripts and SEC filings into structured, evidence-backed investment analysis.

---

## What It Does

Ask a question about any company → get a grounded, cited, structured answer with consistency checks, risk flags, and confidence scoring.

The system searches across **5 data sources** per company (earnings transcripts, 8-K press releases, 10-Q quarterly filings, 10-K annual reports, and financial metrics), validates information across sources, and produces structured analysis — not free-text chat.

---

## Architecture

```
User Question
    ↓
Query Classification (risk / earnings / financial / strategy routing)
    ↓
Hybrid Search (vector similarity + keyword matching + rank fusion)
    ↓
Cross-Encoder Reranking (top 7 passages)
    ↓
Financial Metrics Injection (revenue, EPS, guidance from database)
    ↓
LLM Reasoning (Qwen 3 235B via Cerebras → structured JSON)
    ↓
Structured Output: summary, key_points, consistency, risk_flags, confidence, evidence
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | FastAPI (Python) |
| Frontend | Next.js 16 + Tailwind CSS |
| Database | PostgreSQL + pgvector (Supabase) |
| Embeddings | nomic-embed-text via Ollama (local) |
| Reranker | cross-encoder/ms-marco-MiniLM-L-6-v2 |
| LLM | Qwen 3 235B via Cerebras Cloud |
| Search | Hybrid vector + BM25 with RRF fusion |

## Data Sources

| Source | Type | Origin |
|--------|------|--------|
| Earnings Transcripts | CEO/CFO remarks + analyst Q&A | Company-provided |
| 8-K Press Releases | Official earnings announcements | SEC EDGAR |
| 10-Q Quarterly Filings | Management Discussion & Analysis | SEC EDGAR |
| 10-K Annual Reports | Risk Factors, Business, MD&A | SEC EDGAR |
| Financial Metrics | Revenue, EPS, YoY growth, guidance | Extracted from 8-K |

**7 companies covered:** BBY, CRM, INTU, SNOW, UNH, WDAY, ZM  
**~2,800 embedded passages** across all sources

---

## Evaluation

50-question automated evaluation with strict scoring:

| Metric | Score | Target |
|--------|-------|--------|
| Composite | 86.5% | ≥ 75% |
| Grounding | 81.8% | ≥ 75% |
| Hallucination | 5.5% | ≤ 20% |
| Reasoning | 100% | ≥ 80% |
| Citation | 100% | ≥ 80% |
| Completeness | 59.1% | ≥ 70% |

**Scoring is strict by design:**
- Grounding checks word overlap between evidence and answer (not just existence)
- Citation verifies quotes against actual database content (not just presence)
- Completeness checks expected keyword coverage per question

---

## Setup

### Prerequisites
- Python 3.11+
- Node.js 18+
- Ollama with `nomic-embed-text` model
- Supabase project with pgvector enabled
- Cerebras Cloud API key ([free at cloud.cerebras.ai](https://cloud.cerebras.ai))

### Backend
```bash
cd backend
pip install -r requirements.txt
# Configure .env with DATABASE_URL, CEREBRAS_API_KEY, OLLAMA_HOST
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8001
```

### Frontend
```bash
cd frontend
npm install
npm run dev
```

### Data Ingestion (run once, in order)
```bash
python scripts/seed_companies.py
python scripts/load_transcripts.py
python scripts/fetch_8k_financials.py
python scripts/chunk_and_embed.py
python scripts/fetch_10q_10k.py
python scripts/fetch_8k_as_rag.py
```

### Run Evaluation
```bash
python -m backend.tests.eval_runner --api-url http://localhost:8001
```

---

## Project Structure

```
checkitanalytics/
├── backend/
│   ├── app/
│   │   ├── main.py                    # Application entry point
│   │   ├── routes/                    # API endpoint handlers
│   │   ├── services/                  # RAG, reranking, routing, reasoning
│   │   └── db/                        # Database models and sessions
│   ├── tests/                         # Evaluation suite (50 questions)
│   └── .env                           # Configuration (not committed)
├── frontend/
│   ├── app/                           # Next.js pages
│   └── components/                    # UI components
├── scripts/                           # Data ingestion scripts
└── data/                              # Transcripts and filing extracts
```

---

## Structured Output Format

Every answer returns strict JSON:

```json
{
  "summary": "Direct answer with specific numbers",
  "key_points": ["Point 1", "Point 2", "Point 3"],
  "consistency": "aligned | mixed | conflict | N/A",
  "risk_flags": ["Identified risk"],
  "confidence": "high | medium | low",
  "evidence": [{
    "chunk_id": "source reference",
    "speaker": "speaker name",
    "quote": "verbatim text from source",
    "relevance": "why this supports the answer"
  }]
}
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/companies` | List all companies |
| GET | `/api/companies/{ticker}` | Company details |
| GET | `/api/earnings/{ticker}/{quarter}/{year}` | Financial data |
| POST | `/api/chat` | Submit question, receive streamed answer |
| GET | `/api/evaluation/summary` | Evaluation metrics |

---

## License

Internal use only. Financial data sourced from SEC EDGAR (US government public filings).
