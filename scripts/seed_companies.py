import sys
import os
import time
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.app.db.database import SessionLocal
from backend.app.db.models import Company

# ---------------------------------------------------------------------------
# Hardcoded company data (fallback / source of truth for all non-CIK fields)
# ---------------------------------------------------------------------------
COMPANIES = [
    {"ticker": "AAPL",  "name": "Apple Inc.",                    "sector": "Technology",              "industry": "Consumer Electronics",    "fiscal_year_end_month": 9},
    {"ticker": "MSFT",  "name": "Microsoft Corporation",         "sector": "Technology",              "industry": "Software Infrastructure", "fiscal_year_end_month": 6},
    {"ticker": "GOOGL", "name": "Alphabet Inc.",                  "sector": "Technology",              "industry": "Internet Content",        "fiscal_year_end_month": 12},
    {"ticker": "AMZN",  "name": "Amazon.com Inc.",               "sector": "Consumer Cyclical",       "industry": "Internet Retail",         "fiscal_year_end_month": 12},
    {"ticker": "NVDA",  "name": "NVIDIA Corporation",            "sector": "Technology",              "industry": "Semiconductors",          "fiscal_year_end_month": 1},
    {"ticker": "TSLA",  "name": "Tesla Inc.",                    "sector": "Consumer Cyclical",       "industry": "Auto Manufacturers",      "fiscal_year_end_month": 12},
    {"ticker": "META",  "name": "Meta Platforms Inc.",           "sector": "Technology",              "industry": "Internet Content",        "fiscal_year_end_month": 12},
    {"ticker": "JPM",   "name": "JPMorgan Chase & Co.",          "sector": "Financial Services",      "industry": "Banks",                   "fiscal_year_end_month": 12},
    {"ticker": "JNJ",   "name": "Johnson & Johnson",             "sector": "Healthcare",              "industry": "Drug Manufacturers",      "fiscal_year_end_month": 12},
    {"ticker": "UNH",   "name": "UnitedHealth Group Inc.",       "sector": "Healthcare",              "industry": "Healthcare Plans",        "fiscal_year_end_month": 12},
    {"ticker": "V",     "name": "Visa Inc.",                     "sector": "Financial Services",      "industry": "Credit Services",         "fiscal_year_end_month": 9},
    {"ticker": "PG",    "name": "Procter & Gamble Co.",          "sector": "Consumer Defensive",      "industry": "Household Products",      "fiscal_year_end_month": 6},
    {"ticker": "HD",    "name": "Home Depot Inc.",               "sector": "Consumer Cyclical",       "industry": "Home Improvement",        "fiscal_year_end_month": 1},
    {"ticker": "XOM",   "name": "Exxon Mobil Corporation",       "sector": "Energy",                  "industry": "Oil & Gas",               "fiscal_year_end_month": 12},
    {"ticker": "COST",  "name": "Costco Wholesale Corp.",        "sector": "Consumer Defensive",      "industry": "Discount Stores",         "fiscal_year_end_month": 8},
    {"ticker": "CRM",   "name": "Salesforce Inc.",               "sector": "Technology",              "industry": "Software Application",    "fiscal_year_end_month": 1},
    {"ticker": "AMD",   "name": "Advanced Micro Devices Inc.",   "sector": "Technology",              "industry": "Semiconductors",          "fiscal_year_end_month": 12},
    {"ticker": "NFLX",  "name": "Netflix Inc.",                  "sector": "Communication Services",  "industry": "Entertainment",           "fiscal_year_end_month": 12},
    {"ticker": "DIS",   "name": "Walt Disney Co.",               "sector": "Communication Services",  "industry": "Entertainment",           "fiscal_year_end_month": 9},
    {"ticker": "PFE",   "name": "Pfizer Inc.",                   "sector": "Healthcare",              "industry": "Drug Manufacturers",      "fiscal_year_end_month": 12},
]

# ---------------------------------------------------------------------------
# Step 1 — fetch CIK map from SEC EDGAR
# ---------------------------------------------------------------------------
def fetch_cik_map():
    url = "https://www.sec.gov/files/company_tickers.json"
    headers = {"User-Agent": "CheckitAnalytics shruthi6790@gmail.com"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # data is {index: {cik_str, ticker, title}, ...}
        return {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}
    except Exception as e:
        print(f"  [WARN] SEC EDGAR fetch failed: {e}")
        return {}

# ---------------------------------------------------------------------------
# Step 2 — fetch profile from FMP (sector / industry / fiscal month)
#          We use the demo key; if it fails we keep the hardcoded values.
# ---------------------------------------------------------------------------
FMP_API_KEY = "demo"

def fetch_fmp_profile(ticker):
    url = f"https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={FMP_API_KEY}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data or not isinstance(data, list):
            return {}
        p = data[0]
        # FMP fiscal year end is a month name like "September" — convert to int
        month_map = {
            "January": 1, "February": 2, "March": 3, "April": 4,
            "May": 5, "June": 6, "July": 7, "August": 8,
            "September": 9, "October": 10, "November": 11, "December": 12,
        }
        fy_end = p.get("ipoDate")  # not what we need — use sector/industry only
        return {
            "sector": p.get("sector") or None,
            "industry": p.get("industry") or None,
        }
    except Exception as e:
        print(f"  [WARN] FMP fetch failed for {ticker}: {e}")
        return {}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("Fetching CIK map from SEC EDGAR...")
    cik_map = fetch_cik_map()
    print(f"  Got CIKs for {len(cik_map)} tickers from EDGAR.\n")

    session = SessionLocal()
    added = 0
    skipped = 0

    try:
        for co in COMPANIES:
            ticker = co["ticker"]

            # Skip if already in DB
            existing = session.query(Company).filter_by(ticker=ticker).first()
            if existing:
                print(f"  [SKIP] {ticker} already exists (id={existing.id})")
                skipped += 1
                continue

            # Start with hardcoded values
            sector   = co["sector"]
            industry = co["industry"]
            fiscal_month = co["fiscal_year_end_month"]
            cik = cik_map.get(ticker)

            # Try to enrich with FMP (sector / industry only)
            print(f"  Fetching FMP profile for {ticker}...")
            fmp = fetch_fmp_profile(ticker)
            time.sleep(0.3)   # polite rate-limiting for free tier
            if fmp.get("sector"):
                sector = fmp["sector"]
            if fmp.get("industry"):
                industry = fmp["industry"]

            company = Company(
                ticker=ticker,
                name=co["name"],
                sector=sector,
                industry=industry,
                fiscal_year_end_month=fiscal_month,
                cik=cik,
            )
            session.add(company)
            session.flush()   # get the auto-assigned id for logging
            print(f"  [ADD]  {ticker} — {co['name']} | CIK={cik} | sector={sector} | id={company.id}")
            added += 1

        session.commit()
        print(f"\nDone. Added {added} companies, skipped {skipped} already-existing.")
    except Exception as e:
        session.rollback()
        raise
    finally:
        session.close()

if __name__ == "__main__":
    main()
