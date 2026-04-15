#!/usr/bin/env python3
"""
scripts/load_boss_transcripts.py

Load boss-provided transcript .txt files from data/transcripts/ into the DB.
Parses ticker and quarter/year from the filename, looks up the company,
and inserts an EarningsCall record.
"""

import sys, os, re, io
from pathlib import Path
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sqlalchemy import text
from backend.app.db.database import SessionLocal, engine
from backend.app.db.models import Company, EarningsCall

# ── Config ───────────────────────────────────────────────────────────────────
TRANSCRIPT_DIR = Path(__file__).parent.parent / "data" / "transcripts"

FILENAME_TO_TICKER = {
    "alibaba":     "BABA",
    "bestbuy":     "BBY",
    "ehang":       "EH",
    "intuit":      "INTU",
    "li":          "LI",
    "lotus":       "LOT",
    "nio":         "NIO",
    "ponyai":      "PONY",
    "salesforce":  "CRM",
    "snowflake":   "SNOW",
    "unitedhealth":"UNH",
    "wday":        "WDAY",
    "webull":      "BULL",
    "weride":      "WRD",
    "zoom":        "ZM",
}

# ── Ensure raw_transcript_path column exists in DB ────────────────────────────
with engine.connect() as conn:
    conn.execute(text(
        "ALTER TABLE earnings_calls "
        "ADD COLUMN IF NOT EXISTS raw_transcript_path TEXT"
    ))
    conn.commit()

# ── Helpers ───────────────────────────────────────────────────────────────────
def parse_filename(stem: str):
    """
    Extract (company_key, quarter_str, fiscal_year) from a filename stem.
    Expected pattern: {company_key}_q{n}_{year}[_transcript]
    e.g.  alibaba_q2_2026_transcript  ->  ('alibaba', 'Q2', 2026)
          weride_q3_2025_transcript   ->  ('weride',  'Q3', 2025)
    """
    # Strip trailing _transcript if present
    stem = re.sub(r'_transcript$', '', stem, flags=re.IGNORECASE)

    # Match: <key>_q<n>_<year>
    m = re.match(r'^(.+?)_(q\d)_(\d{4})$', stem, re.IGNORECASE)
    if not m:
        return None, None, None

    company_key   = m.group(1).lower()
    quarter_str   = m.group(2).upper()   # e.g. 'Q2'
    fiscal_year   = int(m.group(3))

    return company_key, quarter_str, fiscal_year


def estimate_call_date(fiscal_year: int, fiscal_quarter: str, fye: int) -> date:
    """
    Estimate the earnings call date as the 15th of the month after the
    fiscal quarter ends, based on fiscal_year_end_month (fye).

    Quarter-end month formula:
      months_from_fy_end = (4 - q) * 3
      end_month = ((fye - 1 - months_from_fy_end) % 12) + 1
      end_year  = fiscal_year if end_month <= fye else fiscal_year - 1
    """
    q = int(fiscal_quarter[1])                        # 1-4
    months_back = (4 - q) * 3
    end_month = ((fye - 1 - months_back) % 12) + 1
    end_year  = fiscal_year if end_month <= fye else fiscal_year - 1

    # Call is roughly the 15th of the following month
    call_month = end_month % 12 + 1
    call_year  = end_year + (1 if end_month == 12 else 0)
    return date(call_year, call_month, 15)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    db = SessionLocal()

    # Build ticker -> Company lookup
    companies = {c.ticker: c for c in db.query(Company).all()}

    txt_files = sorted(TRANSCRIPT_DIR.glob("*.txt"))
    if not txt_files:
        print(f"No .txt files found in {TRANSCRIPT_DIR}")
        db.close()
        return

    print(f"Found {len(txt_files)} transcript file(s) in {TRANSCRIPT_DIR}\n")
    print(f"{'Filename':<45} {'Ticker':<6} {'Quarter':<8} {'Status'}")
    print("-" * 80)

    loaded = 0
    skipped = 0
    errors = []

    for fpath in txt_files:
        stem = fpath.stem
        company_key, quarter_str, fiscal_year = parse_filename(stem)

        # --- parse error ---
        if not company_key:
            msg = f"cannot parse filename pattern"
            print(f"  {fpath.name:<45} {'?':<6} {'?':<8} ERROR: {msg}")
            errors.append((fpath.name, msg))
            continue

        # --- ticker mapping ---
        ticker = FILENAME_TO_TICKER.get(company_key)
        if not ticker:
            msg = f"no ticker mapping for '{company_key}'"
            print(f"  {fpath.name:<45} {'?':<6} {quarter_str:<8} ERROR: {msg}")
            errors.append((fpath.name, msg))
            continue

        # --- company lookup ---
        company = companies.get(ticker)
        if not company:
            msg = f"{ticker} not found in companies table"
            print(f"  {fpath.name:<45} {ticker:<6} {quarter_str:<8} ERROR: {msg}")
            errors.append((fpath.name, msg))
            continue

        # --- duplicate check ---
        existing = (
            db.query(EarningsCall)
            .filter_by(
                company_id=company.id,
                fiscal_year=fiscal_year,
                fiscal_quarter=quarter_str,
            )
            .first()
        )
        if existing:
            print(f"  {fpath.name:<45} {ticker:<6} {quarter_str} FY{fiscal_year}  SKIP (already in DB, id={existing.id})")
            skipped += 1
            continue

        # --- insert ---
        fye        = company.fiscal_year_end_month or 12
        call_date  = estimate_call_date(fiscal_year, quarter_str, fye)
        abs_path   = str(fpath.resolve())

        ec = EarningsCall(
            company_id          = company.id,
            fiscal_year         = fiscal_year,
            fiscal_quarter      = quarter_str,
            call_date           = call_date,
            transcript_source   = "boss_provided",
            transcript_source_url = None,
            raw_transcript_path = abs_path,
            is_complete         = True,
            status              = "downloaded",
        )
        db.add(ec)
        db.flush()
        loaded += 1
        print(f"  {fpath.name:<45} {ticker:<6} {quarter_str} FY{fiscal_year}  LOADED (id={ec.id}, call_date={call_date})")

    db.commit()
    db.close()

    # ── Summary ───────────────────────────────────────────────────────────
    print("-" * 80)
    print(f"\nLoaded:  {loaded}")
    print(f"Skipped: {skipped} (already existed)")
    if errors:
        print(f"Errors:  {len(errors)}")
        for fname, msg in errors:
            print(f"  - {fname}: {msg}")
    else:
        print(f"Errors:  0")


if __name__ == "__main__":
    main()
