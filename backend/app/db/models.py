from datetime import datetime, date, time
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, Date, Time,
    Numeric, ForeignKey, UniqueConstraint, Index,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector

from backend.app.db.database import Base


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    sector = Column(String)
    industry = Column(String)
    cik = Column(String)
    fiscal_year_end_month = Column(Integer)
    logo_url = Column(String)
    created_at = Column(
        "created_at",
        type_=String,
        insert_default=func.now(),
        server_default=func.now(),
    )

    earnings_calls = relationship("EarningsCall", back_populates="company")


class EarningsCall(Base):
    __tablename__ = "earnings_calls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    fiscal_year = Column(Integer, nullable=False)
    fiscal_quarter = Column(String, nullable=False)
    call_date = Column(Date, nullable=False)
    call_time = Column(Time, nullable=True)
    transcript_source = Column(String)
    transcript_source_url = Column(Text)
    raw_transcript_path = Column(Text, nullable=True)
    is_complete = Column(Boolean, default=True, server_default="true")
    status = Column(String, default="pending", server_default="pending")
    created_at = Column("created_at", type_=String, server_default=func.now())
    updated_at = Column(
        "updated_at",
        type_=String,
        server_default=func.now(),
        onupdate=func.now(),
    )

    company = relationship("Company", back_populates="earnings_calls")
    financial_metrics = relationship(
        "FinancialMetrics", back_populates="earnings_call", uselist=False
    )
    transcript_chunks = relationship("TranscriptChunk", back_populates="earnings_call")
    precomputed_insights = relationship(
        "PrecomputedInsights", back_populates="earnings_call", uselist=False
    )
    user_queries = relationship("UserQuery", back_populates="earnings_call")
    reasoning_outputs = relationship("ReasoningOutput", back_populates="earnings_call")


class FinancialMetrics(Base):
    __tablename__ = "financial_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    earnings_call_id = Column(
        Integer, ForeignKey("earnings_calls.id"), unique=True, nullable=False
    )
    revenue_actual = Column(Numeric, nullable=True)
    revenue_consensus = Column(Numeric, nullable=True)
    eps_actual = Column(Numeric, nullable=True)
    eps_consensus = Column(Numeric, nullable=True)
    revenue_yoy_growth = Column(Numeric, nullable=True)
    net_income = Column(Numeric, nullable=True)
    guidance_revenue_low = Column(Numeric, nullable=True)
    guidance_revenue_high = Column(Numeric, nullable=True)
    guidance_eps_low = Column(Numeric, nullable=True)
    guidance_eps_high = Column(Numeric, nullable=True)
    stock_price_before = Column(Numeric, nullable=True)
    stock_price_after_hours = Column(Numeric, nullable=True)
    stock_price_next_day = Column(Numeric, nullable=True)
    source = Column(String)
    created_at = Column("created_at", type_=String, server_default=func.now())

    earnings_call = relationship("EarningsCall", back_populates="financial_metrics")


class TranscriptChunk(Base):
    __tablename__ = "transcript_chunks"

    # Format: AAPL_Q1_2026_chunk_007
    id = Column(String, primary_key=True)
    earnings_call_id = Column(
        Integer, ForeignKey("earnings_calls.id"), nullable=False
    )
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    enriched_content = Column(Text)
    # Allowed values: transcript_prepared, transcript_qa,
    #                 filing_mda, filing_risk, filing_business
    source_type = Column(String, nullable=False)
    section = Column(String)
    speaker_name = Column(String, nullable=True)
    speaker_role = Column(String, nullable=True)
    speaker_firm = Column(String, nullable=True)
    token_count = Column(Integer)
    embedding = Column(Vector(768))
    created_at = Column("created_at", type_=String, server_default=func.now())

    __table_args__ = (
        Index("ix_transcript_chunks_earnings_call_id", "earnings_call_id"),
        Index("ix_transcript_chunks_source_type", "source_type"),
    )

    earnings_call = relationship("EarningsCall", back_populates="transcript_chunks")


class PrecomputedInsights(Base):
    __tablename__ = "precomputed_insights"

    id = Column(Integer, primary_key=True, autoincrement=True)
    earnings_call_id = Column(
        Integer, ForeignKey("earnings_calls.id"), unique=True, nullable=False
    )
    summary = Column(Text)
    key_takeaways = Column(JSONB)
    suggested_questions = Column(JSONB)
    topics_discussed = Column(JSONB)
    model_used = Column(String)
    prompt_version = Column(String)
    created_at = Column("created_at", type_=String, server_default=func.now())

    earnings_call = relationship(
        "EarningsCall", back_populates="precomputed_insights"
    )


class UserQuery(Base):
    __tablename__ = "user_queries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, nullable=False)
    earnings_call_id = Column(Integer, ForeignKey("earnings_calls.id"), nullable=False)
    query_text = Column(Text, nullable=False)
    query_type = Column(String)
    retrieved_chunk_ids = Column(JSONB)
    rerank_scores = Column(JSONB)
    output_json = Column(JSONB)
    grounding_score = Column(Numeric, nullable=True)
    confidence = Column(String, nullable=True)
    model_used = Column(String)
    latency_ms = Column(Integer)
    validation_passed = Column(Boolean)
    created_at = Column("created_at", type_=String, server_default=func.now())

    __table_args__ = (
        Index("ix_user_queries_session_id", "session_id"),
        Index("ix_user_queries_earnings_call_id", "earnings_call_id"),
    )

    earnings_call = relationship("EarningsCall", back_populates="user_queries")


class ReasoningOutput(Base):
    __tablename__ = "reasoning_outputs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    earnings_call_id = Column(Integer, ForeignKey("earnings_calls.id"), nullable=False)
    query_text = Column(Text, nullable=False)
    output_json = Column(JSONB)
    grounding_score = Column(Numeric, nullable=True)
    hallucination_rate = Column(Numeric, nullable=True)
    consistency_result = Column(String, nullable=True)
    reasoning_score = Column(Integer, nullable=True)
    completeness_score = Column(Numeric, nullable=True)
    citation_rate = Column(Numeric, nullable=True)
    composite_score = Column(Numeric, nullable=True)
    created_at = Column("created_at", type_=String, server_default=func.now())

    __table_args__ = (
        Index("ix_reasoning_outputs_earnings_call_id", "earnings_call_id"),
    )

    earnings_call = relationship("EarningsCall", back_populates="reasoning_outputs")
