"""SQLAlchemy engine + session factory. Mirrors j24-store-vision/db/database.py.

No migrations — tables are auto-created from the ORM models via init_db(),
which is idempotent and safe to call on every worker/api start.
"""
from __future__ import annotations

import os

import shared.env  # noqa: F401 — side effect: load .env before reading DATABASE_URL

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://perishable:perishable@localhost:5432/perishable",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables if they don't exist (idempotent)."""
    from db.models import Base

    Base.metadata.create_all(engine)
