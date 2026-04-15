#!/usr/bin/env python3
"""
scripts/fetch_transcripts.py

Download earnings-call transcripts for all 20 companies from SEC EDGAR.

Strategy (per company):
  1. EDGAR EFTS full-text search  — finds 8-K exhibit docs that contain
     "earnings call" AND "transcript", filtered by company CIK.
  2. edgartools exhibit scan      — fallback: scan recent 8-K exhibit list
     and download EX-99 docs from Reg-FD / Results-of-Ops filings.
"""

import sys, os, re, time, json, io
import requests

# Force UTF-8 output on Windows (avoids cp1252 encoding errors)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from pathlib import Path
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.app.db.database import SessionLocal
from backend.app.db.models import Company, EarningsCall

# ── Config ──────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "CheckitAnalytics shruthi6790@gmail.com",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json, text/html, */*",
}
TRANSCRIPT_DIR = Path(__file__).parent.parent / "data" / "transcripts"
TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

START_DATE    = "2024-01-01"
END_DATE      = "2025-12-31"
MAX_QUARTERS  = 4
DELAY         = 0.15   # seconds between SEC requests


# ── HTTP helper ──────────────────────────────────────────────────────────────
def sec_get(url, **kw):
    time.sleep(DELAY)
    r = requests.get(url, headers=HEADERS, timeout=25, **kw)
    r.raise_for_status()
    return r


# ── EDGAR EFTS (full-text search) ────────────────────────────────────────────
EFTS_BASE = "https://efts.sec.gov/LATEST/search-index"

def efts_search(company_name: str, cik: str, from_: int = 0) -> list[dict]:
    """
    Search EDGAR full-text index for 8-K docs containing
    "earnings call" AND "transcript" filed by this company.
    Returns list of raw EFTS hit dicts.
    """
    params = {
        "q": '"earnings call" "transcript"',
        "forms": "8-K",
        "dateRange": "custom",
        "startdt": START_DATE,
        "enddt": END_DATE,
        "entity": company_name,
        "from": from_,
    }
    try:
        data = sec_get(EFTS_BASE, params=params).json()
    except Exception as e:
        print(f"    [EFTS] request failed: {e}")
        return []

    hits = data.get("hits", {}).get("hits", [])
    # Filter to this company's CIK only (EFTS can return subsidiaries etc.)
    cik_int = str(int(cik))
    return [
        h for h in hits
        if cik_int in [str(int(c)) for c in h["_source"].get("ciks", [])]
    ]


def doc_url_from_hit(hit: dict) -> str:
    """Build the EDGAR document URL from an EFTS hit."""
    src      = hit["_source"]
    adsh     = src.get("adsh", "")
    acc_clean = adsh.replace("-", "")
    ciks     = src.get("ciks", [])
    filename = hit["_id"].split(":", 1)[-1] if ":" in hit["_id"] else ""
    if not (ciks and acc_clean and filename):
        return ""
    cik_int = int(ciks[0])
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{filename}"


def filing_date_from_hit(hit: dict) -> str:
    return hit["_source"].get("file_date", "")


def period_from_hit(hit: dict) -> str:
    return hit["_source"].get("period_ending", "")


# ── edgartools fallback ──────────────────────────────────────────────────────
def setup_edgartools():
    import edgar
    edgar.set_identity("CheckitAnalytics shruthi6790@gmail.com")
    return edgar


def get_recent_8k_from_edgar(ticker: str, edgar_mod):
    """Return list of recent 8-K Filing objects via edgartools."""
    from edgar import Company as EdgarCompany
    co = EdgarCompany(ticker)
    filings = co.get_filings(form="8-K")
    recent = filings.filter(date=f"{START_DATE}:{END_DATE}")
    return list(recent)


# ── Text extraction ──────────────────────────────────────────────────────────
def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_doc_text(url: str) -> str:
    """Download a document URL and return cleaned plain text."""
    try:
        r = sec_get(url)
        content = r.text
        if any(url.lower().endswith(ext) for ext in (".htm", ".html")):
            content = html_to_text(content)
        return content
    except Exception as e:
        print(f"    [WARN] download failed ({e}): {url[:80]}")
        return ""


TRANSCRIPT_SIGNALS = [
    "operator:", "operator :", "good afternoon", "good morning", "good evening",
    "thank you for joining", "question-and-answer session",
    "our next question", "your line is open",
]

def is_valid_transcript(text: str) -> bool:
    """Return True if the text looks like an earnings-call transcript."""
    if len(text) < 3000:
        return False
    low = text.lower()
    # Must have earnings financial terms
    fin_score = sum(1 for w in ["revenue", "earnings", "quarter", "fiscal",
                                 "guidance", "margin", "growth", "eps"] if w in low)
    # Must have call structure signals
    call_score = sum(1 for s in TRANSCRIPT_SIGNALS if s in low)
    return fin_score >= 3 and call_score >= 2


# ── Fiscal calendar ──────────────────────────────────────────────────────────
def fiscal_info(filing_date: str, fye: int) -> tuple[int, str]:
    """
    Given filing_date (YYYY-MM-DD) and fiscal-year-end month,
    return (fiscal_year, 'Q1'|...|'Q4') for the quarter most likely reported.

    Subtracts 45 days to estimate the quarter-end date; earnings calls
    are filed within ~1 week of the call itself per Reg FD.

    Validation examples:
      AAPL fye=9,  filing=2025-01-30 → qend=2024-12-16 → Q1 FY2025  ✓
      MSFT fye=6,  filing=2025-01-29 → qend=2024-12-15 → Q2 FY2025  ✓
      JPM  fye=12, filing=2024-04-12 → qend=2024-02-27 → Q1 FY2024  ✓
    """
    d  = datetime.strptime(filing_date, "%Y-%m-%d").date()
    qe = d - timedelta(days=45)
    m, y = qe.month, qe.year

    # 0-indexed start of fiscal year (e.g. fye=9 → start=Oct=index 9)
    fy_start_0 = fye % 12
    pos = (m - 1 - fy_start_0) % 12   # position in FY, 0-11
    q   = pos // 3 + 1                 # quarter 1-4

    # FY is named for the year the last month (fye) falls in
    fy  = (y + 1) if m > fye else y

    return fy, f"Q{q}"


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    db = SessionLocal()
    companies = db.query(Company).order_by(Company.id).all()

    edgar_mod = None   # lazy-init edgartools only if needed
    summary   = []

    for co in companies:
        ticker = co.ticker
        cik    = co.cik
        fye    = co.fiscal_year_end_month or 12
        name   = co.name

        print(f"\n{'='*62}")
        print(f"[{ticker}]  {name}  |  CIK={cik}  |  FYE month={fye}")

        if not cik:
            print("  No CIK — skipping")
            summary.append({"ticker": ticker, "method": "-", "saved": 0})
            continue

        # Quarters already in DB for this company
        done = {
            (ec.fiscal_year, ec.fiscal_quarter)
            for ec in db.query(EarningsCall).filter_by(company_id=co.id).all()
        }

        saved       = 0
        method_used = "—"

        # ── Method 1: EFTS full-text search ──────────────────────────────
        print(f"  [1] EFTS full-text search …")
        hits = efts_search(name, cik)
        print(f"      {len(hits)} document hit(s) matched CIK={int(cik)}")

        # Sort hits newest → oldest so we capture the latest 4 quarters first
        hits.sort(key=lambda h: h["_source"].get("file_date", ""), reverse=True)

        seen_urls = set()
        for hit in hits:
            if saved >= MAX_QUARTERS:
                break

            url        = doc_url_from_hit(hit)
            fdate      = filing_date_from_hit(hit)
            if not url or not fdate or url in seen_urls:
                continue
            seen_urls.add(url)

            fy, qstr   = fiscal_info(fdate, fye)
            qkey       = (fy, qstr)
            if qkey in done:
                continue

            print(f"      → {fdate}  {qstr} FY{fy}  {url[-60:]}")
            text = fetch_doc_text(url)
            if not is_valid_transcript(text):
                print(f"        ✗ not a valid transcript ({len(text):,} chars)")
                continue

            fname = f"{ticker}_{qstr}_{fy}.txt"
            (TRANSCRIPT_DIR / fname).write_text(text, encoding="utf-8", errors="replace")

            ec = EarningsCall(
                company_id=co.id,
                fiscal_year=fy,
                fiscal_quarter=qstr,
                call_date=datetime.strptime(fdate, "%Y-%m-%d").date(),
                transcript_source="sec_edgar",
                transcript_source_url=url,
                status="downloaded",
                is_complete=True,
            )
            db.add(ec)
            db.flush()
            done.add(qkey)
            saved += 1
            method_used = "EFTS"
            print(f"        ✓ SAVED  {fname}  ({len(text):,} chars, DB id={ec.id})")

        db.commit()

        # ── Method 2: edgartools exhibit scan (fallback) ──────────────────
        if saved < MAX_QUARTERS:
            print(f"  [2] edgartools exhibit scan (still need {MAX_QUARTERS - saved} quarters) …")
            try:
                if edgar_mod is None:
                    edgar_mod = setup_edgartools()
                filings = get_recent_8k_from_edgar(ticker, edgar_mod)
                print(f"      {len(filings)} 8-K filings in range")
                filings.sort(key=lambda f: str(f.filing_date), reverse=True)

                for filing in filings:
                    if saved >= MAX_QUARTERS:
                        break

                    fdate  = str(filing.filing_date)
                    items  = filing.items or ""
                    fy, qstr = fiscal_info(fdate, fye)
                    qkey   = (fy, qstr)
                    if qkey in done:
                        continue

                    # Only scan Reg-FD (7.01) or Results-of-Ops (2.02) filings
                    if not ("7.01" in items or "2.02" in items):
                        continue

                    for ex in filing.exhibits:
                        doc_type = getattr(ex, "document_type", "") or ""
                        desc     = getattr(ex, "description", "") or ""
                        ex_url   = getattr(ex, "url", "") or ""
                        if not ex_url:
                            continue

                        combined = f"{doc_type} {desc} {ex_url}".lower()
                        is_transcript_named = any(
                            kw in combined
                            for kw in ["transcript", "conference call", "earnings call"]
                        )
                        is_ex99 = doc_type.upper().startswith("EX-99")

                        if not (is_transcript_named or is_ex99):
                            continue
                        if ex_url in seen_urls:
                            continue
                        seen_urls.add(ex_url)

                        print(f"      → {fdate}  {qstr} FY{fy}  {ex_url[-55:]}")
                        text = fetch_doc_text(ex_url)
                        if not is_valid_transcript(text):
                            print(f"        ✗ not a valid transcript ({len(text):,} chars)")
                            continue

                        fname = f"{ticker}_{qstr}_{fy}.txt"
                        (TRANSCRIPT_DIR / fname).write_text(
                            text, encoding="utf-8", errors="replace"
                        )
                        ec = EarningsCall(
                            company_id=co.id,
                            fiscal_year=fy,
                            fiscal_quarter=qstr,
                            call_date=datetime.strptime(fdate, "%Y-%m-%d").date(),
                            transcript_source="sec_edgar",
                            transcript_source_url=ex_url,
                            status="downloaded",
                            is_complete=True,
                        )
                        db.add(ec)
                        db.flush()
                        done.add(qkey)
                        saved += 1
                        method_used = "edgartools"
                        print(f"        ✓ SAVED  {fname}  ({len(text):,} chars, DB id={ec.id})")
                        break   # one exhibit per filing is enough

                db.commit()

            except Exception as e:
                print(f"      [WARN] edgartools scan failed: {e}")

        # ── Per-company summary ───────────────────────────────────────────
        missing = [
            f"FY{fy} {q}"
            for fy in (2024, 2025) for q in ("Q1","Q2","Q3","Q4")
            if (fy, q) not in done
        ]
        summary.append({
            "ticker": ticker,
            "method": method_used if saved > 0 else "—",
            "saved": saved,
            "missing_count": len(missing),
        })
        if saved == 0:
            print(f"  → No transcripts found in EDGAR (will need alternative source)")
        else:
            print(f"  → {saved} transcript(s) saved for {ticker}")

    db.close()

    # ── Global summary ────────────────────────────────────────────────────
    print("\n" + "="*62)
    print(f"{'Ticker':<7} {'Method':<12} {'Saved':>6}  {'Still missing':>5}")
    print("-"*62)
    total = 0
    for s in summary:
        miss = f"{s['missing_count']} quarters"
        print(f"  {s['ticker']:<5}  {s['method']:<12} {s['saved']:>6}  {miss}")
        total += s["saved"]
    print("="*62)
    print(f"  Total transcripts downloaded: {total}")
    print(f"\n  Files saved to: {TRANSCRIPT_DIR}")


if __name__ == "__main__":
    main()
