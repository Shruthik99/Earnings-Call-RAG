import sys
import os

# Add project root so that 'backend.app.db.*' imports resolve correctly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.app.db.database import engine, Base
from backend.app.db.models import (
    Company,
    EarningsCall,
    FinancialMetrics,
    TranscriptChunk,
    PrecomputedInsights,
    UserQuery,
    ReasoningOutput,
)
from sqlalchemy import text

# Ensure pgvector extension exists before creating tables
with engine.connect() as conn:
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    conn.commit()

Base.metadata.create_all(engine)
print("All tables created successfully!")
