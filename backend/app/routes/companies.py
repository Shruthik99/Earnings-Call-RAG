"""
backend/app/routes/companies.py

GET /api/companies         -> list all companies
GET /api/companies/{ticker} -> company details + earnings call list
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.app.db.database import get_db
from backend.app.db.models import Company, EarningsCall
from backend.app.limiter import limiter

router = APIRouter(prefix="/api", tags=["companies"])


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _company_dict(c: Company) -> dict:
    return {
        "id":                   c.id,
        "ticker":               c.ticker,
        "name":                 c.name,
        "sector":               c.sector,
        "industry":             c.industry,
        "cik":                  c.cik,
        "fiscal_year_end_month": c.fiscal_year_end_month,
        "logo_url":             c.logo_url,
    }


def _call_dict(ec: EarningsCall) -> dict:
    return {
        "id":              ec.id,
        "fiscal_year":     ec.fiscal_year,
        "fiscal_quarter":  ec.fiscal_quarter,
        "call_date":       ec.call_date.isoformat() if ec.call_date else None,
        "status":          ec.status,
        "transcript_source": ec.transcript_source,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/companies")
@limiter.limit("60/minute")
def get_companies(request: Request, db: Session = Depends(get_db)):
    """Return every company ordered by ticker."""
    companies = db.query(Company).order_by(Company.ticker).all()
    return [_company_dict(c) for c in companies]


@router.get("/companies/{ticker}")
@limiter.limit("60/minute")
def get_company(ticker: str, request: Request, db: Session = Depends(get_db)):
    """Return a single company and its list of earnings calls."""
    company = db.query(Company).filter(Company.ticker == ticker.upper()).first()
    if not company:
        raise HTTPException(status_code=404, detail=f"Company '{ticker}' not found")

    calls = (
        db.query(EarningsCall)
        .filter_by(company_id=company.id)
        .order_by(EarningsCall.fiscal_year.desc(), EarningsCall.fiscal_quarter.desc())
        .all()
    )

    result = _company_dict(company)
    result["earnings_calls"] = [_call_dict(ec) for ec in calls]
    return result
