"""
backend/app/services/structured_data.py

Retrieves structured financial metrics for a given earnings call.
"""

from sqlalchemy.orm import Session
from backend.app.db.models import FinancialMetrics


def _f(v):
    """Convert a Decimal/None value to float or None."""
    return float(v) if v is not None else None


def get_financial_metrics(earnings_call_id: int, db: Session) -> dict | None:
    """
    Return a dict of financial metrics for *earnings_call_id*, or None
    if no row exists in the financial_metrics table.
    """
    fm = (
        db.query(FinancialMetrics)
        .filter_by(earnings_call_id=earnings_call_id)
        .first()
    )
    if not fm:
        return None

    return {
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
