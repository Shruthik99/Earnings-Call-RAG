#!/usr/bin/env python3
"""
scripts/fetch_10q_10k.py

Downloads 10-Q and 10-K filings from SEC EDGAR for 7 US companies,
extracts key sections (MD&A, Risk Factors, Business), saves text files,
and stores paragraph chunks with Ollama embeddings in pgvector.

Target tickers: BBY, INTU, CRM, SNOW, UNH, WDAY, ZM
"""

import sys
import os
import re
import time
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
EDGAR_HEADERS = {"User-Agent": "CheckitAnalytics shruthi6790@gmail.com"}
EDGAR_DELAY   = 0.2          # seconds between EDGAR requests
OLLAMA_HOST   = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL   = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
MAX_TOKENS    = 500
EMBED_BATCH   = 10

TARGET_TICKERS = ["BBY", "INTU", "CRM", "SNOW", "UNH", "WDAY", "ZM"]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DIR_10Q  = DATA_DIR / "filings_10q"
DIR_10K  = DATA_DIR / "filings_10k"

# ---------------------------------------------------------------------------
# EDGAR item boundary patterns
# Each section has a "start" regex and an "end" regex.
# We scan the full plain-text of the filing and extract the slice between them.
# We skip very short matches (table-of-contents links) by requiring >= 500 chars.
#
# Separator after "Item N" varies by company:
#   Standard:  "Item 2. Management..."
#   Intuit:    "ITEM 2 - MANAGEMENT..."  or  "ITEM 2: MANAGEMENT..."
# Apostrophe in "Management's" may be rendered as straight ', curly \u2019,
# or as \uFFFD (Unicode replacement char) after HTML decode.
# ---------------------------------------------------------------------------
_SEP   = r"[\.\s:\-]*"          # separator: dot, colon, dash, spaces
_APOS  = r"[\s\'\u2019\ufffd]*s\s+"   # 's followed by space (various apostrophes)

ITEM_PATTERNS = {
    "10-Q": {
        "mda": {
            "start": re.compile(
                rf"item\s+2{_SEP}management{_APOS}discussion",
                re.IGNORECASE,
            ),
            "end": re.compile(rf"item\s+3{_SEP}\S", re.IGNORECASE),
        },
    },
    "10-K": {
        "business": {
            "start": re.compile(rf"item\s+1{_SEP}business\b", re.IGNORECASE),
            "end":   re.compile(rf"item\s+1a{_SEP}\S", re.IGNORECASE),
        },
        "risk_factors": {
            "start": re.compile(
                rf"item\s+1a{_SEP}risk\s+factors", re.IGNORECASE
            ),
            "end": re.compile(
                rf"item\s+1b{_SEP}\S|item\s+2{_SEP}\S", re.IGNORECASE
            ),
        },
        "mda": {
            "start": re.compile(
                rf"item\s+7{_SEP}management{_APOS}discussion",
                re.IGNORECASE,
            ),
            "end": re.compile(rf"item\s+7a{_SEP}\S|item\s+8{_SEP}\S", re.IGNORECASE),
        },
    },
}

SECTION_LABEL  = {"mda": "MD&A", "risk_factors": "Risk Factors", "business": "Business"}
SOURCE_TYPE    = {"mda": "filing_mda", "risk_factors": "filing_risk", "business": "filing_business"}
FILING_SECTIONS = {"10-Q": ["mda"], "10-K": ["business", "risk_factors", "mda"]}


# ---------------------------------------------------------------------------
# EDGAR network helpers
# ---------------------------------------------------------------------------
def edgar_get(url: str):
    """GET with polite delay and project User-Agent. Returns Response or None."""
    time.sleep(EDGAR_DELAY)
    try:
        r = requests.get(url, headers=EDGAR_HEADERS, timeout=60)
        if r.ok:
            return r
        print(f"    [WARN] HTTP {r.status_code}: {url}")
        return None
    except Exception as e:
        print(f"    [WARN] Request error ({e}): {url}")
        return None


def fetch_submissions(cik: str):
    """Return EDGAR submissions JSON dict for CIK, or None."""
    cik_padded = str(cik).zfill(10)
    r = edgar_get(f"https://data.sec.gov/submissions/CIK{cik_padded}.json")
    return r.json() if r else None


def recent_filings_of_type(submissions: dict, form_type: str, n: int) -> list:
    """Return up to n most-recent filings matching form_type."""
    recent       = submissions.get("filings", {}).get("recent", {})
    forms        = recent.get("form", [])
    dates        = recent.get("filingDate", [])
    accessions   = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    results = []
    for i, form in enumerate(forms):
        if form == form_type:
            results.append({
                "filing_date":    dates[i],
                "accession":      accessions[i],
                "primary_doc":    primary_docs[i] if i < len(primary_docs) else "",
            })
            if len(results) >= n:
                break
    return results


def primary_doc_url(cik: str, accession: str, doc_filename: str) -> str:
    """Build the direct URL to a primary filing document."""
    cik_int  = str(int(cik))
    acc_flat = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{acc_flat}/{doc_filename}"
    )


def find_primary_doc_from_index(cik: str, accession: str) -> str:
    """
    Fallback: fetch the filing index page and return the URL of the first
    .htm document that is not named 'index'.
    """
    cik_int  = str(int(cik))
    acc_flat = accession.replace("-", "")
    idx_url  = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{acc_flat}/{accession}-index.htm"
    )
    r = edgar_get(idx_url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        if "index" not in href and (href.endswith(".htm") or href.endswith(".html")):
            full = a["href"]
            if full.startswith("/"):
                return f"https://www.sec.gov{full}"
            return (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_int}/{acc_flat}/{full}"
            )
    return None


# ---------------------------------------------------------------------------
# Section extraction from HTML
# ---------------------------------------------------------------------------
def html_to_plain(html: str) -> str:
    """Convert filing HTML to clean plain text (newline-separated)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def extract_section(html: str, filing_type: str, section_key: str):
    """
    Find and return the plain-text content of a named section.
    Skips short (table-of-contents) matches and returns the first
    substantial block (>= 500 chars).  Returns None if not found.
    """
    try:
        plain = html_to_plain(html)
    except Exception as e:
        print(f"    [WARN] HTML parse error: {e}")
        return None

    patterns = ITEM_PATTERNS.get(filing_type, {}).get(section_key)
    if not patterns:
        return None

    start_re = patterns["start"]
    end_re   = patterns["end"]

    search_from = 0
    while True:
        m_start = start_re.search(plain, search_from)
        if not m_start:
            return None

        m_end = end_re.search(plain, m_start.start() + 300)
        if m_end:
            candidate = plain[m_start.start() : m_end.start()]
        else:
            candidate = plain[m_start.start() : m_start.start() + 80_000]

        lines   = [l.strip() for l in candidate.split("\n") if l.strip()]
        cleaned = "\n\n".join(lines)

        if len(cleaned) >= 500:
            return cleaned

        # Too short -> probably a TOC entry; advance past this match
        search_from = m_start.end()


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------
def _tok(text: str) -> int:
    return int(len(text.split()) * 1.3)


def chunk_paragraphs(text: str, max_tokens: int = MAX_TOKENS) -> list:
    """
    Split text into chunks by paragraphs up to max_tokens.
    Oversized paragraphs are split further by sentence.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    chunks, cur_parts, cur_tok = [], [], 0

    for para in paragraphs:
        pt = _tok(para)

        if pt > max_tokens:
            # Flush current accumulation
            if cur_parts:
                chunks.append("\n\n".join(cur_parts))
                cur_parts, cur_tok = [], 0
            # Split oversize paragraph by sentence
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
    """Return embedding vector (list of floats) or None on error."""
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
    """Embed enriched_content for each chunk dict in-place. Returns count embedded."""
    if not ollama_up or not chunk_records:
        return 0
    n = 0
    for i in range(0, len(chunk_records), EMBED_BATCH):
        batch = chunk_records[i : i + EMBED_BATCH]
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
def get_or_create_ec(db, company_id: int, filing_type: str, filing_date_str: str):
    """
    Return an EarningsCall row for this (company, filing_type, date).
    Creates one if it does not exist.
    """
    filing_date = datetime.strptime(filing_date_str, "%Y-%m-%d").date()

    ec = (
        db.query(EarningsCall)
        .filter_by(
            company_id=company_id,
            fiscal_quarter=filing_type,
            call_date=filing_date,
        )
        .first()
    )
    if ec:
        return ec

    ec = EarningsCall(
        company_id=company_id,
        fiscal_year=filing_date.year,
        fiscal_quarter=filing_type,
        call_date=filing_date,
        transcript_source="sec_edgar",
        status="pending",
        is_complete=True,
    )
    db.add(ec)
    db.flush()          # get auto-assigned id
    return ec


# ---------------------------------------------------------------------------
# Per-filing processing
# ---------------------------------------------------------------------------
def process_filing(db, company, filing: dict, filing_type: str, ollama_up: bool) -> dict:
    """
    Download one filing, extract sections, chunk + embed, persist to DB.
    Returns a summary dict.
    """
    ticker      = company.ticker
    cik         = company.cik
    filing_date = filing["filing_date"]
    accession   = filing["accession"]
    primary_doc = filing["primary_doc"]

    ft_label = filing_type.replace("-", "")          # "10Q" / "10K"
    date_id  = filing_date.replace("-", "")           # "20240131"

    print(f"\n  [{ticker}] {filing_type}  {filing_date}  (acc {accession})")

    # Resolve document URL
    if primary_doc:
        doc_url = primary_doc_url(cik, accession, primary_doc)
    else:
        print(f"    No primary_document -> trying index page")
        doc_url = find_primary_doc_from_index(cik, accession)
        if not doc_url:
            print(f"    [SKIP] Cannot locate document")
            return _empty_result(filing_date)

    print(f"    URL: {doc_url}")
    r = edgar_get(doc_url)
    if not r:
        print(f"    [SKIP] Download failed")
        return _empty_result(filing_date)
    html = r.text
    print(f"    Downloaded {len(html):,} chars")

    # Extract sections
    out_dir = DIR_10Q if filing_type == "10-Q" else DIR_10K
    out_dir.mkdir(parents=True, exist_ok=True)

    extracted     = {}          # section_key -> plain text
    all_text_parts = []

    for sec_key in FILING_SECTIONS[filing_type]:
        label = SECTION_LABEL[sec_key]
        text  = extract_section(html, filing_type, sec_key)
        if text:
            extracted[sec_key] = text
            all_text_parts.append(f"=== {label} ===\n\n{text}")
            print(f"    Section '{label}': {len(text):,} chars")
        else:
            print(f"    [WARN] Section '{label}' not found")

    if not extracted:
        print(f"    [SKIP] No sections extracted")
        return _empty_result(filing_date)

    # Save text file
    out_path = out_dir / f"{ticker}_{ft_label}_{filing_date}.txt"
    out_path.write_text("\n\n".join(all_text_parts), encoding="utf-8")
    print(f"    Saved -> {out_path.name}")

    # EarningsCall record (reuse if already exists)
    ec = get_or_create_ec(db, company.id, filing_type, filing_date)

    # Skip if chunks already loaded for this filing
    if db.query(TranscriptChunk).filter_by(earnings_call_id=ec.id).first():
        print(f"    [SKIP] Chunks already in DB (ec_id={ec.id})")
        return {
            "filing_date":    filing_date,
            "sections_found": list(extracted.keys()),
            "chunks_created": 0,
            "embedded":       0,
        }

    # Build chunk dicts
    chunk_records = []
    global_idx    = 0

    for sec_key, text in extracted.items():
        src_type = SOURCE_TYPE[sec_key]
        sec_label = SECTION_LABEL[sec_key]

        for sub_text in chunk_paragraphs(text):
            header   = f"[{ticker} | {filing_type} | {sec_label} | {filing_date}]"
            enriched = f"{header}\n\n{sub_text}"
            chunk_id = f"{ticker}_{ft_label}_{date_id}_chunk_{global_idx:04d}"

            chunk_records.append({
                "id":              chunk_id,
                "chunk_index":     global_idx,
                "content":         sub_text,
                "enriched_content": enriched,
                "source_type":     src_type,
                "section":         sec_label,
                "token_count":     _tok(sub_text),
            })
            global_idx += 1

    print(f"    Built {len(chunk_records)} chunks across {len(extracted)} section(s)")

    # Embed
    n_embedded = embed_chunks(chunk_records, ollama_up)

    # Persist to DB one row at a time to avoid SQLAlchemy 2.x insertmanyvalues
    # batching issue with pgvector's Vector column type.
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
            db.flush()   # write each row individually; avoids executemany batching

        ec.status = "embedded" if (ollama_up and n_embedded == len(chunk_records)) else "chunked"
        db.commit()
    except Exception as exc:
        db.rollback()
        print(f"    [ERROR] DB insert failed: {exc}")
        return _empty_result(filing_date)

    print(f"    Saved {len(chunk_records)} chunks, {n_embedded} embedded  (ec_id={ec.id})")
    return {
        "filing_date":    filing_date,
        "sections_found": list(extracted.keys()),
        "chunks_created": len(chunk_records),
        "embedded":       n_embedded,
    }


def _empty_result(filing_date: str) -> dict:
    return {"filing_date": filing_date, "sections_found": [], "chunks_created": 0, "embedded": 0}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    db = SessionLocal()

    ollama_up = check_ollama()
    if ollama_up:
        print(f"[OK]   Ollama at {OLLAMA_HOST}, embed model: {EMBED_MODEL}")
    else:
        print(f"[WARN] Ollama not reachable at {OLLAMA_HOST} -- chunks saved WITHOUT embeddings")

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

        print(f"  CIK: {company.cik}")
        subs = fetch_submissions(company.cik)
        if not subs:
            print(f"  [SKIP] Could not fetch EDGAR submissions")
            continue

        # 3 most recent 10-Qs
        filings_q = recent_filings_of_type(subs, "10-Q", 3)
        print(f"  Found {len(filings_q)} recent 10-Q filing(s)")
        for f in filings_q:
            res = process_filing(db, company, f, "10-Q", ollama_up)
            summary.append({"ticker": ticker, "filing_type": "10-Q", **res})

        # 1 most recent 10-K
        filings_k = recent_filings_of_type(subs, "10-K", 1)
        print(f"  Found {len(filings_k)} recent 10-K filing(s)")
        for f in filings_k:
            res = process_filing(db, company, f, "10-K", ollama_up)
            summary.append({"ticker": ticker, "filing_type": "10-K", **res})

    db.close()

    # Summary table
    print(f"\n{'=' * 74}")
    print(
        f"  {'Ticker':<5} {'Type':<5} {'Date':<12} "
        f"{'Sections':<28} {'Chunks':>6} {'Embed':>6}"
    )
    print(f"  {'-' * 70}")
    for s in summary:
        secs = ", ".join(SECTION_LABEL.get(k, k) for k in s["sections_found"]) or "(none)"
        print(
            f"  {s['ticker']:<5} {s['filing_type']:<5} {s['filing_date']:<12} "
            f"{secs:<28} {s['chunks_created']:>6} {s['embedded']:>6}"
        )
    total_c = sum(s["chunks_created"] for s in summary)
    total_e = sum(s["embedded"]       for s in summary)
    print(f"{'=' * 74}")
    print(f"  Total chunks: {total_c}   Embedded: {total_e}")


if __name__ == "__main__":
    main()
