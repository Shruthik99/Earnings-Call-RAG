"""
backend/app/services/reasoning_service.py

Streams a structured JSON analysis from Cerebras (llama-3.3-70b) given
transcript chunks and financial data.

Yields (event_type, data) tuples:
  ("token",  str)           -- streaming token from Cerebras
  ("result", dict)          -- final parsed JSON object
  ("error",  dict)          -- {"message": str, "raw": str}
"""

from __future__ import annotations
import os
import json
import re
import asyncio
import threading
from dotenv import load_dotenv
from cerebras.cloud.sdk import Cerebras

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL   = "qwen-3-235b-a22b-instruct-2507"

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a financial analyst. Given transcript excerpts and financial data, provide a thorough analysis. "
    "Include specific numbers, percentages, and speaker names when available. "
    "Compare management's statements to actual financial results when data is available. "
    "Reply with ONLY a JSON object — no preamble, no markdown. Start with { end with }.\n\n"
    "{\n"
    '  "summary": "1-2 sentences answering the question",\n'
    '  "key_points": ["point with specific number/name", "point 2", "point 3"],\n'
    '  "consistency": "aligned"|"mixed"|"conflict",\n'
    '  "risk_flags": ["flag if any"],\n'
    '  "confidence": "high"|"medium"|"low",\n'
    '  "evidence": [{"chunk_id":"EXACT_ID_FROM_BRACKETS","speaker":"","quote":"verbatim words from excerpt","relevance":"1 sentence"}]\n'
    "}\n\n"
    "CRITICAL RULES:\n"
    "- quote field: Copy EXACT words from the excerpts above. Do not paraphrase. Use verbatim text from the speaker.\n"
    "- summary: Every claim must be directly supported by your evidence quotes. Do not add information not in the excerpts.\n"
    "- key_points: Cover ALL major topics from the excerpts — include specific numbers, percentages, dollar amounts, and speaker names when available.\n"
    "- If excerpts mention revenue, include the exact figure. If they mention growth rates, include the percentage. If they mention guidance, include the range.\n"
    "- evidence chunk_id: Use the EXACT chunk ID shown in brackets like [1] id=XXXX. Copy that ID exactly.\n\n"
    "Max 1 evidence item. "
    "consistency: aligned=narrative matches numbers, mixed=partial, conflict=contradiction. "
    "confidence: high=clear evidence, medium=partial, low=weak. "
    "If the topic is not discussed in the provided excerpts at all, set consistency to 'n/a' and confidence to 'low'. "
    "Do not hallucinate information that is not in the excerpts."
)


def _build_user_prompt(
    ticker: str,
    quarter: str,
    year: int,
    question: str,
    chunks: list,
    financial_data: dict | None,
) -> str:
    lines = []
    lines.append(f"COMPANY: {ticker}  |  PERIOD: {quarter} FY{year}")
    lines.append(f"\nQUESTION: {question}")

    lines.append("\n--- FINANCIAL DATA ---")
    if financial_data:
        def fmt(label, val, unit=""):
            if val is not None:
                lines.append(f"  {label}: {val}{unit}")

        fmt("Revenue (actual)",    financial_data.get("revenue_actual"),    "M")
        fmt("Revenue (consensus)", financial_data.get("revenue_consensus"), "M")
        fmt("EPS (actual)",        financial_data.get("eps_actual"),        "")
        fmt("EPS (consensus)",     financial_data.get("eps_consensus"),     "")
        fmt("Revenue YoY growth",  financial_data.get("revenue_yoy_growth"),"%")
        fmt("Net income",          financial_data.get("net_income"),        "M")
        fmt("Guidance rev low",    financial_data.get("guidance_revenue_low"),  "M")
        fmt("Guidance rev high",   financial_data.get("guidance_revenue_high"), "M")
        fmt("Guidance EPS low",    financial_data.get("guidance_eps_low"),  "")
        fmt("Guidance EPS high",   financial_data.get("guidance_eps_high"), "")
        fmt("Stock price before",  financial_data.get("stock_price_before"), "")
        fmt("Stock after-hours",   financial_data.get("stock_price_after_hours"), "")
        fmt("Stock next day",      financial_data.get("stock_price_next_day"), "")
        if not any(v is not None for v in financial_data.values() if v != financial_data.get("source")):
            lines.append("  (no numeric data available)")
    else:
        lines.append("  Not available for this company/period.")

    lines.append("\n--- TRANSCRIPT EXCERPTS (ranked by relevance) ---")
    for i, chunk in enumerate(chunks, 1):
        speaker = chunk.get("speaker_name") or "Unknown"
        cid     = chunk.get("chunk_id") or ""
        lines.append(f"\n[{i}] id={cid} | {speaker}")
        lines.append(chunk["content"])

    lines.append(
        "\nRespond with ONLY the JSON object. "
        "Do not add any text before or after the JSON."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    text = text.strip()

    # 1. Strip <think>...</think> blocks (qwen3 chain-of-thought)
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()

    # 2. Direct parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 3. Strip markdown code fences
    stripped = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    stripped = re.sub(r"\s*```\s*$", "", stripped, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 4. Find all { ... } blocks and return the first that parses as a dict
    for match in re.finditer(r"\{[\s\S]*?\}", text):
        try:
            obj = json.loads(match.group())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # 5. Greedy last-resort: outermost { ... }
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            obj = json.loads(match.group())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in model response")


# ---------------------------------------------------------------------------
# Main async generator
# ---------------------------------------------------------------------------

async def generate_reasoning(
    ticker: str,
    quarter: str,
    year: int,
    question: str,
    chunks: list,
    financial_data: dict | None,
):
    """
    Async generator that streams Cerebras tokens and yields:
      ("token",  str)
      ("result", dict)
      ("error",  dict)
    """
    user_prompt = _build_user_prompt(
        ticker, quarter, year, question, chunks, financial_data
    )

    loop        = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    full_response: list[str] = []

    def _cerebras_worker():
        import time as _time
        client = Cerebras(api_key=CEREBRAS_API_KEY)
        last_err = None
        for attempt in range(4):  # up to 3 retries on 429
            if attempt:
                _time.sleep(attempt * 5)  # 5s, 10s, 15s back-off
                full_response.clear()    # reset tokens on retry
            try:
                stream = client.chat.completions.create(
                    model=CEREBRAS_MODEL,
                    messages=[
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user",   "content": user_prompt},
                    ],
                    max_tokens=800,
                    temperature=0.1,
                    stream=True,
                )
                for chunk in stream:
                    token = (chunk.choices[0].delta.content or "")
                    if token:
                        full_response.append(token)
                        asyncio.run_coroutine_threadsafe(
                            queue.put(("token", token)), loop
                        )
                asyncio.run_coroutine_threadsafe(queue.put(("done", None)), loop)
                return
            except Exception as exc:
                last_err = exc
                if "429" not in str(exc):
                    break  # non-retryable error
        asyncio.run_coroutine_threadsafe(
            queue.put(("error", str(last_err))), loop
        )

    thread = threading.Thread(target=_cerebras_worker, daemon=True)
    thread.start()

    while True:
        event_type, data = await queue.get()
        if event_type == "done":
            break
        elif event_type == "error":
            yield "error", {
                "message": f"Cerebras error: {data}",
                "raw":     "".join(full_response)[:400],
            }
            return
        else:
            yield "token", data

    # Parse final JSON
    raw = "".join(full_response)
    try:
        result = _extract_json(raw)
        yield "result", result
    except ValueError as exc:
        yield "error", {
            "message": str(exc),
            "raw":     raw[:800],
        }
