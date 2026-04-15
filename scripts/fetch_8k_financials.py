#!/usr/bin/env python3
"""
scripts/fetch_8k_financials.py

For each earnings_call in the DB, find the matching SEC EDGAR 8-K (Item 2.02),
download the press-release exhibit, parse financial figures, and store in
financial_metrics.

Extracted fields: revenue_actual, eps_actual, eps_consensus (if stated),
                  net_income, revenue_yoy_growth, guidance_revenue_low/high,
                  guidance_eps_low/high.
"""

import sys
import os
import re
import time
import requests
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.app.db.database import SessionLocal
from backend.app.db.models import Company, EarningsCall, FinancialMetrics

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": "CheckitAnalytics shruthi6790@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}
DELAY        = 0.15   # seconds between EDGAR requests
WINDOW_DAYS  = 30     # look for 8-K filings within +/- this many days of call_date


# ---------------------------------------------------------------------------
# EDGAR helpers
# ---------------------------------------------------------------------------
def sec_get(url):
    time.sleep(DELAY)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r


def get_8k_filings(cik: str) -> list:
    """Return all 8-K filings from the submissions API."""
    padded = cik.zfill(10)
    data   = sec_get(f"https://data.sec.gov/submissions/CIK{padded}.json").json()
    recent = data.get("filings", {}).get("recent", {})

    forms   = recent.get("form", [])
    dates   = recent.get("filingDate", [])
    accs    = recent.get("accessionNumber", [])
    items_l = recent.get("items", []) or [""] * len(forms)
    prim    = recent.get("primaryDocument", []) or [""] * len(forms)

    results = []
    for i, form in enumerate(forms):
        if form == "8-K":
            results.append({
                "accession":   accs[i],
                "date":        dates[i],
                "items":       items_l[i] if i < len(items_l) else "",
                "primary_doc": prim[i]    if i < len(prim)    else "",
            })
    return results


def get_filing_docs(cik: str, accession: str) -> list:
    """
    Fetch the filing index page and return list of
    {"name": ..., "type": ..., "size": ...} dicts.
    """
    cik_int   = int(cik)
    acc_clean = accession.replace("-", "")
    url = (f"https://www.sec.gov/Archives/edgar/data/"
           f"{cik_int}/{acc_clean}/{accession}-index.htm")
    try:
        soup = BeautifulSoup(sec_get(url).text, "html.parser")
        docs = []
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            a    = tds[2].find("a")
            href = a["href"] if a else ""
            try:
                size = int(tds[4].get_text(strip=True).replace(",", ""))
            except ValueError:
                size = 0
            docs.append({
                "name": href.split("/")[-1] if href else "",
                "href": href,
                "type": tds[3].get_text(strip=True),
                "size": size,
            })
        return docs
    except Exception:
        return []


def best_exhibit(docs: list) -> dict | None:
    """
    Pick the best press-release exhibit: prefer EX-99.1, then largest .htm file.
    """
    htm_docs = [d for d in docs if d["name"].lower().endswith((".htm", ".html"))]
    if not htm_docs:
        return None
    # Prefer EX-99.1
    for d in htm_docs:
        if d["type"].upper() in ("EX-99.1", "EX-99"):
            return d
    # Fall back to largest htm
    return max(htm_docs, key=lambda d: d["size"])


def download_exhibit(cik: str, accession: str, doc: dict) -> str:
    """Download an exhibit and return plain text (HTML stripped)."""
    href = doc["href"]
    if href.startswith("/"):
        url = f"https://www.sec.gov{href}"
    else:
        cik_int   = int(cik)
        acc_clean = accession.replace("-", "")
        url = (f"https://www.sec.gov/Archives/edgar/data/"
               f"{cik_int}/{acc_clean}/{doc['name']}")
    html = sec_get(url).text
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n")


# ---------------------------------------------------------------------------
# Financial-figure parsing
# ---------------------------------------------------------------------------
# Matches: $1.23B  $4,567M  $12.3 billion  1.23  (0.45)
_MONEY_RE = re.compile(
    r"""
    (?:                         # optional leading $
        \$\s*
    )?
    (                           # capture the number
        \([\d,]+\.?\d*\)        # negative in parens: (123.4)
        |
        [\d,]+\.?\d*            # plain number: 1,234.5
    )
    \s*
    (billion|million|B|M|K)?    # optional scale suffix
    """,
    re.IGNORECASE | re.VERBOSE,
)

def parse_money(text: str) -> Decimal | None:
    """
    Parse the first dollar-like number from a short string.
    Returns value in millions (e.g. '$3.2 billion' -> Decimal('3200')).
    """
    m = _MONEY_RE.search(text)
    if not m:
        return None
    raw   = m.group(1).replace(",", "").strip()
    scale = (m.group(2) or "").lower()

    # Handle negative parens
    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1]

    try:
        val = Decimal(raw)
    except InvalidOperation:
        return None

    if negative:
        val = -val

    if scale in ("b", "billion"):
        val *= 1000
    elif scale in ("k",):
        val /= 1000
    # 'm' / 'million' or no suffix -> already in millions assumed
    return val


def parse_eps(text: str) -> Decimal | None:
    """
    Parse EPS: a small decimal like '$1.23' or '(0.45)'.
    Returns value as-is (not scaled).
    """
    m = re.search(
        r'\$?\s*(\([\d.]+\)|[\d.]+)\s*(?:per\s+(?:diluted\s+)?share)?',
        text, re.IGNORECASE
    )
    if not m:
        return None
    raw = m.group(1).replace(",", "").strip()
    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1]
    try:
        val = Decimal(raw)
        return -val if negative else val
    except InvalidOperation:
        return None


def parse_pct(text: str) -> Decimal | None:
    """Parse a percentage like '12%' or '+5.3%' -> Decimal('12') or Decimal('5.3')."""
    m = re.search(r'([+-]?\s*[\d.]+)\s*%', text)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(" ", ""))
    except InvalidOperation:
        return None


# Paragraph-level search: find lines containing a keyword and extract numbers.
def search_value(lines: list, keywords: list, parser_fn,
                 window: int = 3) -> Decimal | None:
    """
    Scan `lines` for any line containing one of `keywords`.
    When found, extract a value from that line and the `window` lines after it.
    Returns the first successfully parsed value, or None.
    """
    kw_lower = [k.lower() for k in keywords]
    for i, line in enumerate(lines):
        ll = line.lower()
        if any(kw in ll for kw in kw_lower):
            snippet = "\n".join(lines[i: i + window])
            val = parser_fn(snippet)
            if val is not None:
                return val
    return None


def extract_financials(text: str) -> dict:
    """
    Parse revenue, net_income, eps_actual, eps_consensus,
    revenue_yoy_growth and guidance from press-release plain text.
    Returns a dict with Decimal values (or None).
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    results = {}

    # --- Revenue (millions) ---
    results["revenue_actual"] = search_value(
        lines,
        ["total revenue", "net revenue", "total revenues",
         "revenue of", "revenues of", "revenue was", "revenues were"],
        parse_money,
    )
    if results["revenue_actual"] is None:
        results["revenue_actual"] = search_value(
            lines, ["revenue", "revenues"], parse_money
        )

    # --- Net income (millions) ---
    results["net_income"] = search_value(
        lines,
        ["net income attributable", "net income of", "net income was",
         "net income (loss)", "net loss", "net income"],
        parse_money,
    )

    # --- EPS (diluted) ---
    results["eps_actual"] = search_value(
        lines,
        ["diluted earnings per share", "diluted eps", "earnings per diluted share",
         "diluted net income per", "diluted loss per"],
        parse_eps,
    )
    if results["eps_actual"] is None:
        results["eps_actual"] = search_value(
            lines, ["earnings per share", "eps"], parse_eps
        )

    # --- Revenue YoY growth (%) ---
    for kw in ["increased", "grew", "growth", "declined", "decreased"]:
        for i, ln in enumerate(lines):
            if kw in ln.lower() and any(
                rv in ln.lower() for rv in ["revenue", "revenues"]
            ):
                val = parse_pct(ln)
                if val is not None:
                    results["revenue_yoy_growth"] = val
                    break
        else:
            continue
        break
    else:
        results["revenue_yoy_growth"] = None

    # --- Guidance revenue (range) ---
    guidance_lines = [
        ln for ln in lines
        if any(g in ln.lower() for g in ["guidance", "outlook", "expects", "expects revenue",
                                          "full year", "fiscal year", "next quarter"])
    ]
    rev_guidance = None
    for gln in guidance_lines:
        # Look for a range like "$X billion to $Y billion"
        rng = re.search(
            r'\$?\s*([\d,.]+)\s*(billion|million|B|M)?\s*'
            r'(?:to|-)\s*'
            r'\$?\s*([\d,.]+)\s*(billion|million|B|M)?',
            gln, re.IGNORECASE
        )
        if rng:
            def to_m(val_str, scale_str):
                try:
                    v = Decimal(val_str.replace(",", ""))
                    s = (scale_str or "").lower()
                    if s in ("b", "billion"):
                        v *= 1000
                    return v
                except Exception:
                    return None
            lo = to_m(rng.group(1), rng.group(2))
            hi = to_m(rng.group(3), rng.group(4))
            if lo and hi:
                rev_guidance = (lo, hi)
                break
    results["guidance_revenue_low"]  = rev_guidance[0] if rev_guidance else None
    results["guidance_revenue_high"] = rev_guidance[1] if rev_guidance else None

    # --- Guidance EPS range ---
    eps_guidance = None
    for gln in guidance_lines:
        rng = re.search(
            r'\$?\s*([\d.]+)\s*(?:to|-)\s*\$?\s*([\d.]+)\s*(?:per\s+(?:diluted\s+)?share|eps)',
            gln, re.IGNORECASE
        )
        if rng:
            try:
                lo = Decimal(rng.group(1))
                hi = Decimal(rng.group(2))
                eps_guidance = (lo, hi)
                break
            except InvalidOperation:
                pass
    results["guidance_eps_low"]  = eps_guidance[0] if eps_guidance else None
    results["guidance_eps_high"] = eps_guidance[1] if eps_guidance else None

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    db = SessionLocal()

    ec_rows = (
        db.query(EarningsCall)
        .join(Company)
        .order_by(EarningsCall.id)
        .all()
    )
    print(f"Processing {len(ec_rows)} earnings call(s).\n")

    # Track summary
    summary = []  # list of dicts

    for ec in ec_rows:
        company = db.query(Company).get(ec.company_id)
        ticker  = company.ticker
        cik     = company.cik
        label   = f"{ticker} {ec.fiscal_quarter} FY{ec.fiscal_year}"

        print(f"[{label}]")

        # -- skip if financial_metrics already exists --
        existing_fm = (
            db.query(FinancialMetrics)
            .filter_by(earnings_call_id=ec.id)
            .first()
        )
        if existing_fm:
            print(f"  SKIP  financial_metrics already present (id={existing_fm.id})")
            summary.append({"label": label, "status": "skipped"})
            continue

        if not cik:
            print(f"  SKIP  no CIK for {ticker}")
            summary.append({"label": label, "status": "no_cik"})
            continue

        # -- fetch 8-K filings from EDGAR --
        try:
            filings_8k = get_8k_filings(cik)
        except Exception as e:
            print(f"  ERROR fetching submissions: {e}")
            summary.append({"label": label, "status": f"error:{e}"})
            continue

        print(f"  Found {len(filings_8k)} total 8-K filings for {ticker}")

        # -- filter: Item 2.02 within +/- WINDOW_DAYS of call_date --
        call_dt  = ec.call_date
        window   = timedelta(days=WINDOW_DAYS)
        matched  = []
        for f in filings_8k:
            if "2.02" not in f["items"]:
                continue
            try:
                fdate = datetime.strptime(f["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            if abs((fdate - call_dt).days) <= WINDOW_DAYS:
                matched.append(f)

        if not matched:
            print(f"  MISS  no Item-2.02 8-K within {WINDOW_DAYS} days of {call_dt}")
            summary.append({"label": label, "status": "no_8k_found"})
            # Still create a blank financial_metrics row so we know we tried
            fm = FinancialMetrics(earnings_call_id=ec.id, source="sec_edgar_8k")
            db.add(fm)
            db.commit()
            continue

        # Use the closest filing to call_date
        matched.sort(key=lambda f: abs(
            (datetime.strptime(f["date"], "%Y-%m-%d").date() - call_dt).days
        ))
        best_filing = matched[0]
        print(f"  Match 8-K filed {best_filing['date']} "
              f"(acc={best_filing['accession']})")

        # -- get exhibit list --
        try:
            docs = get_filing_docs(cik, best_filing["accession"])
        except Exception as e:
            print(f"  ERROR fetching filing index: {e}")
            summary.append({"label": label, "status": f"error:{e}"})
            continue

        exhibit = best_exhibit(docs)
        if not exhibit:
            print(f"  MISS  no .htm exhibit found in filing")
            summary.append({"label": label, "status": "no_exhibit"})
            fm = FinancialMetrics(earnings_call_id=ec.id, source="sec_edgar_8k")
            db.add(fm)
            db.commit()
            continue

        print(f"  Exhibit: {exhibit['name']} ({exhibit['size']:,} bytes)")

        # -- download and parse --
        try:
            text = download_exhibit(cik, best_filing["accession"], exhibit)
        except Exception as e:
            print(f"  ERROR downloading exhibit: {e}")
            summary.append({"label": label, "status": f"error:{e}"})
            continue

        financials = extract_financials(text)

        found_fields = [k for k, v in financials.items() if v is not None]
        print(f"  Parsed: {', '.join(found_fields) if found_fields else 'nothing'}")
        for k, v in financials.items():
            if v is not None:
                print(f"    {k:<28} = {v}")

        # -- store in financial_metrics --
        fm = FinancialMetrics(
            earnings_call_id      = ec.id,
            revenue_actual        = financials.get("revenue_actual"),
            eps_actual            = financials.get("eps_actual"),
            net_income            = financials.get("net_income"),
            revenue_yoy_growth    = financials.get("revenue_yoy_growth"),
            guidance_revenue_low  = financials.get("guidance_revenue_low"),
            guidance_revenue_high = financials.get("guidance_revenue_high"),
            guidance_eps_low      = financials.get("guidance_eps_low"),
            guidance_eps_high     = financials.get("guidance_eps_high"),
            source                = "sec_edgar_8k",
        )
        db.add(fm)
        db.commit()

        status = "ok" if found_fields else "parsed_nothing"
        summary.append({"label": label, "status": status, "fields": found_fields})
        print()

    db.close()

    # ---------------------------------------------------------------------------
    # Summary table
    # ---------------------------------------------------------------------------
    print("=" * 70)
    print(f"{'Company/Quarter':<28} {'Status':<16} {'Fields extracted'}")
    print("-" * 70)
    for s in summary:
        fields = ", ".join(s.get("fields", [])) or "-"
        print(f"  {s['label']:<26} {s['status']:<16} {fields}")
    ok_count = sum(1 for s in summary if s["status"] == "ok")
    print("=" * 70)
    print(f"Total: {len(summary)}  |  Extracted: {ok_count}  |  "
          f"No data: {len(summary) - ok_count}")


if __name__ == "__main__":
    main()
