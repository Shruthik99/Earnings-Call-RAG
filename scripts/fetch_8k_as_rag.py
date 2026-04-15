#!/usr/bin/env python3
"""
scripts/fetch_8k_as_rag.py

For each of the 7 US companies (BBY, INTU, CRM, SNOW, UNH, WDAY, ZM):
  1. Find the most recent 8-K with Item 2.02 via SEC EDGAR submissions API
  2. Download the EX-99.1 press release exhibit HTML
  3. Extract all paragraph text; skip tables that are mostly numeric
  4. Save plain text to data/filings_8k/{TICKER}_8K_{filing_date}.txt
  5. Chunk by paragraphs (max 500 tokens)
  6. Add metadata header: [TICKER | 8-K Press Release | Filing Date]
  7. source_type = 'filing_8k'
  8. Embed each chunk with Ollama nomic-embed-text
  9. Store in pgvector transcript_chunks table
"""

import sys
import os
import re
import time
import json
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))

from backend.app.db.database import SessionLocal
from backend.app.db.models import Company, EarningsCall, TranscriptChunk

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
EDGAR_HEADERS  = {"User-Agent": "CheckitAnalytics shruthi6790@gmail.com"}
EDGAR_DELAY    = 0.2
OLLAMA_HOST    = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL    = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
MAX_TOKENS     = 500
EMBED_BATCH    = 10

TARGET_TICKERS = ["BBY", "INTU", "CRM", "SNOW", "UNH", "WDAY", "ZM"]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DIR_8K   = DATA_DIR / "filings_8k"

# A table cell is "numeric" if it looks like a number, currency, %, or dash
_NUMERIC_CELL = re.compile(
    r"^\s*[\$\(]?[\d,\.\-]+[%\)]?\s*$"
    r"|^\s*[\-\—\–]+\s*$"
    r"|^\s*n/?a\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# EDGAR helpers
# ---------------------------------------------------------------------------
def edgar_get(url: str):
    time.sleep(EDGAR_DELAY)
    try:
        r = requests.get(url, headers=EDGAR_HEADERS, timeout=60)
        if r.ok:
            return r
        print(f"    [WARN] HTTP {r.status_code}: {url}")
    except Exception as e:
        print(f"    [WARN] Request error ({e}): {url}")
    return None


def fetch_submissions(cik: str):
    cik_padded = str(cik).zfill(10)
    r = edgar_get(f"https://data.sec.gov/submissions/CIK{cik_padded}.json")
    return r.json() if r else None


def find_8k_with_202(submissions: dict) -> dict | None:
    """Return the most recent 8-K filing that contains Item 2.02, or None."""
    recent     = submissions.get("filings", {}).get("recent", {})
    forms      = recent.get("form", [])
    dates      = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    items_list = recent.get("items", [])

    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        item_str = items_list[i] if i < len(items_list) else ""
        if "2.02" in item_str:
            return {
                "filing_date": dates[i],
                "accession":   accessions[i],
            }
    return None


def fetch_filing_index(cik: str, accession: str) -> list[dict]:
    """
    Fetch the filing index HTML page and return a list of
    {"name": filename, "type": doc_type, "url": full_url}.
    """
    cik_int  = str(int(cik))
    acc_flat = accession.replace("-", "")
    idx_url  = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{acc_flat}/{accession}-index.htm"
    )
    r = edgar_get(idx_url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    docs = []
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_flat}/"

    # The index page has a table with columns: Seq, Description, Document, Type, Size
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        # Column layout: Seq | Description | Document (link) | Type | Size
        link_cell = cells[2] if len(cells) >= 4 else None
        type_cell = cells[3] if len(cells) >= 4 else None
        if not link_cell or not type_cell:
            continue
        a = link_cell.find("a", href=True)
        if not a:
            continue
        href  = a["href"]
        name  = href.split("/")[-1]
        dtype = type_cell.get_text(strip=True)
        url   = f"https://www.sec.gov{href}" if href.startswith("/") else base + name
        docs.append({"name": name, "type": dtype, "url": url})

    return docs


def find_exhibit_url(docs: list[dict]) -> str | None:
    """
    Return the URL of the best press-release exhibit:
    prefer EX-99.1, fall back to any EX-99.*, then any .htm/.html file
    that is not the index.
    """
    # Priority 1: explicit EX-99.1 type
    for d in docs:
        if d["type"] in ("EX-99.1", "EX-99.01"):
            return d["url"]

    # Priority 2: any EX-99.* type
    for d in docs:
        if d["type"].startswith("EX-99"):
            return d["url"]

    # Priority 3: any htm file that isn't the main filing or index
    for d in docs:
        name = d["name"].lower()
        if name.endswith((".htm", ".html")) and "index" not in name:
            if d["type"] not in ("8-K",):
                return d["url"]

    return None


# ---------------------------------------------------------------------------
# Text extraction from HTML press release
# ---------------------------------------------------------------------------
def is_numeric_table(table_tag) -> bool:
    """True if >60 % of non-empty table cells look like numbers."""
    cells = table_tag.find_all(["td", "th"])
    total, numeric = 0, 0
    for cell in cells:
        text = cell.get_text(strip=True)
        if not text:
            continue
        total += 1
        if _NUMERIC_CELL.match(text):
            numeric += 1
    return total > 3 and (numeric / total) > 0.60


def extract_press_release_text(html: str) -> str:
    """
    Extract readable paragraph text from the press release HTML.
    Removes numeric financial tables; returns newline-separated paragraphs.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove non-content tags
    for tag in soup(["script", "style", "noscript", "head"]):
        tag.decompose()

    # Remove numeric financial tables
    for table in soup.find_all("table"):
        if is_numeric_table(table):
            table.decompose()

    # Collect text from paragraph-like elements; only leaf nodes to avoid dups
    seen, paragraphs = set(), []
    for tag in soup.find_all(["p", "h1", "h2", "h3", "h4", "li"]):
        # Skip if this tag has child block elements (avoid double-counting)
        if tag.find(["p", "h1", "h2", "h3", "h4", "li", "div"]):
            continue
        text = " ".join(tag.get_text(separator=" ").split())
        if len(text) < 30:
            continue
        if text in seen:
            continue
        seen.add(text)
        paragraphs.append(text)

    # If no <p> tags were useful, fall back to full get_text
    if not paragraphs:
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    return "\n\n".join(paragraphs)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def _tok(text: str) -> int:
    return int(len(text.split()) * 1.3)


def chunk_paragraphs(text: str, max_tokens: int = MAX_TOKENS) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    chunks, cur_parts, cur_tok = [], [], 0

    for para in paragraphs:
        pt = _tok(para)

        if pt > max_tokens:
            if cur_parts:
                chunks.append("\n\n".join(cur_parts))
                cur_parts, cur_tok = [], 0
            sents = re.split(r"(?<=[.!?])\s+", para)
            s_parts, s_tok = [], 0
            for s in sents:
                st = _tok(s)
                if s_parts and s_tok + st > max_tokens:
                    chunks.append(" ".join(s_parts))
                    s_parts, s_tok = [], 0
                s_parts.append(s)
                s_tok += st
            if s_parts:
                chunks.append(" ".join(s_parts))
        else:
            if cur_parts and cur_tok + pt > max_tokens:
                chunks.append("\n\n".join(cur_parts))
                cur_parts, cur_tok = [], 0
            cur_parts.append(para)
            cur_tok += pt

    if cur_parts:
        chunks.append("\n\n".join(cur_parts))
    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# Ollama embedding
# ---------------------------------------------------------------------------
def check_ollama() -> bool:
    try:
        return requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5).ok
    except Exception:
        return False


def embed_text(text: str):
    try:
        r = requests.post(
            f"{OLLAMA_HOST}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=60,
        )
        if r.ok:
            return r.json().get("embedding")
        print(f"    [WARN] Ollama {r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"    [WARN] Ollama error: {e}")
    return None


def embed_chunks(chunk_records: list, ollama_up: bool) -> int:
    if not ollama_up or not chunk_records:
        return 0
    n = 0
    for i in range(0, len(chunk_records), EMBED_BATCH):
        batch = chunk_records[i: i + EMBED_BATCH]
        ok = 0
        for ch in batch:
            vec = embed_text(ch["enriched_content"])
            if vec is not None:
                ch["embedding"] = vec
                ok += 1
        print(f"    Embed batch {i // EMBED_BATCH + 1}: {ok}/{len(batch)}")
        n += ok
    return n


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def get_or_create_ec(db, company_id: int, filing_date_str: str):
    filing_date = datetime.strptime(filing_date_str, "%Y-%m-%d").date()
    ec = (
        db.query(EarningsCall)
        .filter_by(
            company_id=company_id,
            fiscal_quarter="8-K",
            call_date=filing_date,
        )
        .first()
    )
    if ec:
        return ec
    ec = EarningsCall(
        company_id=company_id,
        fiscal_year=filing_date.year,
        fiscal_quarter="8-K",
        call_date=filing_date,
        transcript_source="sec_edgar",
        status="pending",
        is_complete=True,
    )
    db.add(ec)
    db.flush()
    return ec


# ---------------------------------------------------------------------------
# Per-company processing
# ---------------------------------------------------------------------------
def process_company(db, company, ollama_up: bool) -> dict:
    ticker = company.ticker
    cik    = company.cik
    result = {"ticker": ticker, "chunks": 0, "embedded": 0, "file": None, "filing_date": None}

    print(f"\n  CIK: {cik}")
    subs = fetch_submissions(cik)
    if not subs:
        print(f"  [SKIP] Could not fetch EDGAR submissions")
        return result

    filing = find_8k_with_202(subs)
    if not filing:
        print(f"  [SKIP] No 8-K with Item 2.02 found")
        return result

    filing_date = filing["filing_date"]
    accession   = filing["accession"]
    date_id     = filing_date.replace("-", "")
    print(f"  8-K filing: {filing_date}  (acc {accession})")
    result["filing_date"] = filing_date

    # Fetch filing index to find EX-99.1
    docs = fetch_filing_index(cik, accession)
    if not docs:
        print(f"  [SKIP] Could not fetch filing index")
        return result

    exhibit_url = find_exhibit_url(docs)
    if not exhibit_url:
        print(f"  [SKIP] No press release exhibit found in filing index")
        print(f"  Available docs: {[d['name'] for d in docs[:8]]}")
        return result

    print(f"  Exhibit URL: {exhibit_url}")
    r = edgar_get(exhibit_url)
    if not r:
        print(f"  [SKIP] Download failed")
        return result

    html = r.text
    print(f"  Downloaded {len(html):,} chars")

    # Extract text
    text = extract_press_release_text(html)
    if len(text) < 200:
        print(f"  [SKIP] Extracted text too short ({len(text)} chars)")
        return result
    print(f"  Extracted {len(text):,} chars of paragraph text")

    # Save text file
    DIR_8K.mkdir(parents=True, exist_ok=True)
    out_path = DIR_8K / f"{ticker}_8K_{filing_date}.txt"
    out_path.write_text(text, encoding="utf-8")
    print(f"  Saved -> {out_path.name}")
    result["file"] = out_path.name

    # EarningsCall record
    ec = get_or_create_ec(db, company.id, filing_date)

    # Skip if chunks already loaded
    if db.query(TranscriptChunk).filter_by(earnings_call_id=ec.id).first():
        print(f"  [SKIP] Chunks already in DB (ec_id={ec.id})")
        return result

    # Build chunk dicts
    chunk_records = []
    for idx, sub_text in enumerate(chunk_paragraphs(text)):
        header   = f"[{ticker} | 8-K Press Release | {filing_date}]"
        enriched = f"{header}\n\n{sub_text}"
        chunk_id = f"{ticker}_8K_{date_id}_chunk_{idx:04d}"
        chunk_records.append({
            "id":               chunk_id,
            "chunk_index":      idx,
            "content":          sub_text,
            "enriched_content": enriched,
            "source_type":      "filing_8k",
            "section":          "Press Release",
            "token_count":      _tok(sub_text),
        })

    print(f"  Built {len(chunk_records)} chunks")

    # Embed
    n_embedded = embed_chunks(chunk_records, ollama_up)

    # Persist
    try:
        for ch in chunk_records:
            tc = TranscriptChunk(
                id               = ch["id"],
                earnings_call_id = ec.id,
                chunk_index      = ch["chunk_index"],
                content          = ch["content"],
                enriched_content = ch["enriched_content"],
                source_type      = ch["source_type"],
                section          = ch["section"],
                token_count      = ch["token_count"],
                embedding        = ch.get("embedding"),
            )
            db.add(tc)
            db.flush()

        ec.status = "embedded" if (ollama_up and n_embedded == len(chunk_records)) else "chunked"
        db.commit()
    except Exception as exc:
        db.rollback()
        print(f"  [ERROR] DB insert failed: {exc}")
        return result

    print(f"  Saved {len(chunk_records)} chunks, {n_embedded} embedded  (ec_id={ec.id})")
    result["chunks"]   = len(chunk_records)
    result["embedded"] = n_embedded
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    db = SessionLocal()

    ollama_up = check_ollama()
    if ollama_up:
        print(f"[OK]   Ollama at {OLLAMA_HOST}, embed model: {EMBED_MODEL}")
    else:
        print(f"[WARN] Ollama not reachable -- chunks saved WITHOUT embeddings")

    summary = []

    for ticker in TARGET_TICKERS:
        print(f"\n{'=' * 64}")
        print(f"  {ticker}")
        print(f"{'=' * 64}")

        company = db.query(Company).filter_by(ticker=ticker).first()
        if not company:
            print(f"  [SKIP] {ticker} not in companies table")
            continue
        if not company.cik:
            print(f"  [SKIP] {ticker} has no CIK")
            continue

        res = process_company(db, company, ollama_up)
        summary.append(res)

    db.close()

    # Summary table
    print(f"\n{'=' * 70}")
    print(f"  {'Ticker':<6} {'Filing Date':<13} {'File':<35} {'Chunks':>6} {'Embed':>6}")
    print(f"  {'-' * 66}")
    for s in summary:
        print(
            f"  {s['ticker']:<6} {(s['filing_date'] or 'N/A'):<13} "
            f"{(s['file'] or '(none)'):<35} {s['chunks']:>6} {s['embedded']:>6}"
        )
    print(f"{'=' * 70}")
    print(f"  Total chunks: {sum(s['chunks'] for s in summary)}   "
          f"Embedded: {sum(s['embedded'] for s in summary)}")


if __name__ == "__main__":
    main()
