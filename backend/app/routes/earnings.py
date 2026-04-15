"""
backend/app/routes/earnings.py

GET /api/earnings/{ticker}/{quarter}/{year}
  -> earnings call details + financial metrics + precomputed insights
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.app.db.database import get_db
from backend.app.db.models import (
    Company, EarningsCall, FinancialMetrics, PrecomputedInsights
)
from backend.app.limiter import limiter

router = APIRouter(prefix="/api", tags=["earnings"])


def _f(v):
    return float(v) if v is not None else None


@router.get("/earnings/{ticker}/{quarter}/{year}")
@limiter.limit("60/minute")
def get_earnings(
    ticker: str,
    quarter: str,
    year: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Return earnings call details, financial metrics, and any precomputed
    insights for the given ticker / quarter (e.g. Q3) / fiscal year.
    """
    company = db.query(Company).filter(Company.ticker == ticker.upper()).first()
    if not company:
        raise HTTPException(status_code=404, detail=f"Company '{ticker}' not found")

    ec = (
        db.query(EarningsCall)
        .filter_by(
            company_id=company.id,
            fiscal_quarter=quarter.upper(),
            fiscal_year=year,
        )
        .first()
    )
    if not ec:
        raise HTTPException(
            status_code=404,
            detail=f"No earnings call found for {ticker} {quarter.upper()} FY{year}",
        )

    result = {
        "id":              ec.id,
        "ticker":          company.ticker,
        "company_name":    company.name,
        "sector":          company.sector,
        "industry":        company.industry,
        "fiscal_year":     ec.fiscal_year,
        "fiscal_quarter":  ec.fiscal_quarter,
        "call_date":       ec.call_date.isoformat() if ec.call_date else None,
        "status":          ec.status,
        "transcript_source": ec.transcript_source,
    }

    # Financial metrics
    fm = db.query(FinancialMetrics).filter_by(earnings_call_id=ec.id).first()
    if fm:
        result["financial_metrics"] = {
            "revenue_actual":          _f(fm.revenue_actual),
            "revenue_consensus":       _f(fm.revenue_consensus),
            "eps_actual":              _f(fm.eps_actual),
            "eps_consensus":           _f(fm.eps_consensus),
            "revenue_yoy_growth":      _f(fm.revenue_yoy_growth),
            "net_income":              _f(fm.net_income),
            "guidance_revenue_low":    _f(fm.guidance_revenue_low),
            "guidance_revenue_high":   _f(fm.guidance_revenue_high),
            "guidance_eps_low":        _f(fm.guidance_eps_low),
            "guidance_eps_high":       _f(fm.guidance_eps_high),
            "stock_price_before":      _f(fm.stock_price_before),
            "stock_price_after_hours": _f(fm.stock_price_after_hours),
            "stock_price_next_day":    _f(fm.stock_price_next_day),
            "source":                  fm.source,
        }
    else:
        result["financial_metrics"] = None

    # Precomputed insights
    pi = db.query(PrecomputedInsights).filter_by(earnings_call_id=ec.id).first()
    if pi:
        result["insights"] = {
            "summary":             pi.summary,
            "key_takeaways":       pi.key_takeaways,
            "suggested_questions": pi.suggested_questions,
            "topics_discussed":    pi.topics_discussed,
            "model_used":          pi.model_used,
        }
    else:
        result["insights"] = None

    return result
