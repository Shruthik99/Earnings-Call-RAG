#!/usr/bin/env python3
"""
scripts/generate_summaries.py

For every earnings_call with status='embedded', retrieves the top 3 chunks
(prepared remarks / MD&A prioritised), makes ONE streaming Ollama call per EC
to generate a JSON object with:
  - summary (3-4 sentences, revenue + highlights + outlook)
  - key_takeaways (5 items)
  - suggested_questions (3 items)
Results are stored in precomputed_insights.

Uses streaming to avoid request timeouts on slow CPU inference.
"""

import sys
import os
import re
import json
import time
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))

from backend.app.db.database import SessionLocal
from backend.app.db.models import Company, EarningsCall, TranscriptChunk, PrecomputedInsights
from sqlalchemy import func

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "http://localhost:11434")
CHAT_MODEL      = os.getenv("OLLAMA_CHAT_MODEL", "qwen2.5:7b-instruct")
NUM_PREDICT     = 400   # enough for summary + 5 takeaways + 3 questions as JSON
PROMPT_VER      = "v1"
CHUNK_LIMIT     = 3     # 3 chunks x ~150 tokens + prompt ~= 600 token input
CHUNK_MAX_CHARS = 600   # truncate each chunk so total input stays under num_ctx default

# Source priority: most informative first
SOURCE_PRIORITY = [
    "transcript_prepared",
    "filing_mda",
    "filing_business",
    "transcript_qa",
    "filing_risk",
]


# ---------------------------------------------------------------------------
# Ollama streaming helper
# ---------------------------------------------------------------------------
def check_ollama() -> bool:
    try:
        return requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5).ok
    except Exception:
        return False


def ollama_stream(prompt: str, num_predict: int = NUM_PREDICT) -> str:
    """
    POST to /api/generate with stream=True and collect the full response.
    Streaming avoids HTTP read timeouts during slow CPU inference — each
    token arrives as a separate JSON line keeping the connection alive.
    Returns the complete response text, or '' on error.
    """
    try:
        r = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model":   CHAT_MODEL,
                "prompt":  prompt,
                "stream":  True,
                "options": {"num_predict": num_predict, "temperature": 0.3},
            },
            stream=True,
            timeout=(10, 600),   # (connect timeout, read timeout per chunk)
        )
        if not r.ok:
            print(f"    [WARN] Ollama {r.status_code}: {r.text[:120]}")
            return ""

        parts = []
        for raw_line in r.iter_lines():
            if not raw_line:
                continue
            try:
                chunk = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            parts.append(chunk.get("response", ""))
            if chunk.get("done"):
                break

        return "".join(parts).strip()

    except Exception as e:
        print(f"    [WARN] Ollama error: {e}")
        return ""


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def parse_json_list(text: str, key: str, expected: int) -> list:
    """
    Extract a JSON array from `text` by key name, or from a bare array.
    Falls back to splitting on numbered/bulleted lines.
    """
    text = text.strip()

    # Try full JSON object  {"key": [...], ...}
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and key in obj:
            val = obj[key]
            if isinstance(val, list):
                return [str(x).strip() for x in val[:expected]]
    except Exception:
        pass

    # Try to find  "key": [...]  inline
    m = re.search(rf'"{re.escape(key)}"\s*:\s*(\[.*?\])', text, re.DOTALL)
    if m:
        try:
            val = json.loads(m.group(1))
            if isinstance(val, list):
                return [str(x).strip() for x in val[:expected]]
        except Exception:
            pass

    # Try bare JSON array
    m = re.search(r'\[.*?\]', text, re.DOTALL)
    if m:
        try:
            val = json.loads(m.group())
            if isinstance(val, list):
                return [str(x).strip() for x in val[:expected]]
        except Exception:
            pass

    # Line-by-line fallback
    lines = []
    for line in text.split('\n'):
        line = re.sub(r'^[\d\.\-\*\s"]+', '', line).rstrip('",').strip()
        if line and len(line) > 10:
            lines.append(line)
    return lines[:expected]


def parse_summary(text: str) -> str:
    """Extract the summary field from JSON or return the raw text."""
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "summary" in obj:
            return str(obj["summary"]).strip()
    except Exception:
        pass
    m = re.search(r'"summary"\s*:\s*"(.*?)"(?=\s*[,}])', text, re.DOTALL)
    if m:
        return m.group(1).replace('\\n', ' ').strip()
    return text   # raw text is already the summary


# ---------------------------------------------------------------------------
# Chunk retrieval
# ---------------------------------------------------------------------------
def get_top_chunks(db, ec_id: int, limit: int = CHUNK_LIMIT) -> list:
    """Return up to `limit` chunks, source-priority ordered."""
    collected, seen = [], set()
    for src in SOURCE_PRIORITY:
        rows = (
            db.query(TranscriptChunk)
            .filter_by(earnings_call_id=ec_id, source_type=src)
            .order_by(TranscriptChunk.chunk_index)
            .limit(limit)
            .all()
        )
        for ch in rows:
            if ch.id not in seen:
                collected.append(ch)
                seen.add(ch.id)
        if len(collected) >= limit:
            break
    return collected[:limit]


def build_context(chunks: list, ticker: str, fq: str, fy: int) -> str:
    """Compact context: header + each chunk truncated to CHUNK_MAX_CHARS."""
    parts = [f"{ticker} | {fq} FY{fy}"]
    for ch in chunks:
        text = ch.content[:CHUNK_MAX_CHARS]
        if len(ch.content) > CHUNK_MAX_CHARS:
            text += "..."
        parts.append(text)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Single combined prompt → JSON
# ---------------------------------------------------------------------------
COMBINED_PROMPT = """\
You are a financial analyst. Analyze the following {fq} earnings material for {ticker} \
and respond with a single valid JSON object — no markdown, no explanation, JSON only.

Required format:
{{
  "summary": "3-4 sentence summary including revenue result with specific numbers, \
key business highlights, and management outlook",
  "key_takeaways": [
    "takeaway 1",
    "takeaway 2",
    "takeaway 3",
    "takeaway 4",
    "takeaway 5"
  ],
  "suggested_questions": [
    "question 1",
    "question 2",
    "question 3"
  ]
}}

Earnings material:
{context}

JSON:"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    db = SessionLocal()

    if not check_ollama():
        print(f"[ERROR] Ollama not reachable at {OLLAMA_HOST}")
        db.close()
        return
    print(f"[OK] Ollama at {OLLAMA_HOST}, model: {CHAT_MODEL}")

    existing = {pi.earnings_call_id for pi in db.query(PrecomputedInsights).all()}
    ec_rows  = (
        db.query(EarningsCall)
        .filter(EarningsCall.status == "embedded")
        .order_by(EarningsCall.company_id, EarningsCall.call_date)
        .all()
    )
    todo = [ec for ec in ec_rows if ec.id not in existing]
    print(f"Found {len(ec_rows)} embedded calls, {len(todo)} need insights.\n")

    log = []

    for ec in todo:
        company = db.get(Company, ec.company_id)
        ticker  = company.ticker
        fq      = ec.fiscal_quarter
        fy      = ec.fiscal_year
        label   = f"{ticker} {fq} FY{fy} ({ec.call_date})"

        print(f"--- {label} ---", flush=True)

        chunks = get_top_chunks(db, ec.id)
        if not chunks:
            print(f"  [SKIP] No chunks found", flush=True)
            continue

        srcs    = ', '.join(sorted(set(c.source_type for c in chunks)))
        context = build_context(chunks, ticker, fq, fy)
        prompt  = COMBINED_PROMPT.format(ticker=ticker, fq=fq, context=context)
        print(f"  Chunks: {len(chunks)} ({srcs})  prompt: {len(prompt)} chars", flush=True)

        t0 = time.time()
        print(f"  Calling Ollama (streaming) ...", flush=True)
        raw = ollama_stream(prompt, num_predict=NUM_PREDICT)
        elapsed = time.time() - t0
        print(f"  Response: {len(raw)} chars in {elapsed:.0f}s", flush=True)

        if not raw:
            print(f"  [SKIP] Empty response", flush=True)
            continue

        # Parse
        summary    = parse_summary(raw)
        takeaways  = parse_json_list(raw, "key_takeaways",       5)
        questions  = parse_json_list(raw, "suggested_questions", 3)

        if not summary or len(summary) < 20:
            print(f"  [SKIP] Could not parse summary from response", flush=True)
            print(f"  Raw (first 200): {raw[:200]}", flush=True)
            continue

        pi = PrecomputedInsights(
            earnings_call_id    = ec.id,
            summary             = summary,
            key_takeaways       = takeaways,
            suggested_questions = questions,
            topics_discussed    = None,
            model_used          = CHAT_MODEL,
            prompt_version      = PROMPT_VER,
        )
        db.add(pi)
        db.commit()

        print(f"  Saved. Takeaways: {len(takeaways)}  Questions: {len(questions)}", flush=True)
        print(f"  Summary: {summary[:160]}...", flush=True)
        print(flush=True)

        log.append({
            "label":       label,
            "chunks":      len(chunks),
            "elapsed":     elapsed,
            "summary_len": len(summary),
            "takeaways":   len(takeaways),
            "questions":   len(questions),
        })

    db.close()

    # Final table
    print("=" * 74)
    print("%-38s %5s %6s %6s %4s %3s" % ("Label","Chnks","Secs","SumCh","KTs","Qs"))
    print("-" * 74)
    for s in log:
        print("  %-36s %5d %6.0f %6d %4d %3d" % (
            s["label"], s["chunks"], s["elapsed"],
            s["summary_len"], s["takeaways"], s["questions"]
        ))
    total_t = sum(s["elapsed"] for s in log)
    print("=" * 74)
    print("  Total processed: %d   Total time: %.0fs (%.1f min)" % (
        len(log), total_t, total_t / 60
    ))


if __name__ == "__main__":
    main()
