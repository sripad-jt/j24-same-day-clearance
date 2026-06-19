"""ORM tables — the Postgres read-model + durable price ledger.

The Temporal workflow is the source of truth; these tables make run state
queryable by the React app and give us an immutable, idempotent record of
applied prices and decisions.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class StoreRow(Base):
    __tablename__ = "stores"
    store_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    close_hour_ist: Mapped[int] = mapped_column(Integer, default=21)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MarkdownRun(Base):
    __tablename__ = "markdown_runs"
    run_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    jpin: Mapped[str] = mapped_column(String(64), index=True)
    receipt_date: Mapped[str] = mapped_column(String(16))
    clearance_date: Mapped[str] = mapped_column(String(16), default="")
    product_title: Mapped[str] = mapped_column(String(256), default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    is_rte: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="STARTED")
    current_rung: Mapped[str] = mapped_column(String(8), default="R0")
    list_price: Mapped[float] = mapped_column(Float, default=0.0)
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    q0: Mapped[int] = mapped_column(Integer, default=0)
    units_sold: Mapped[int] = mapped_column(Integer, default=0)
    awaiting_approval: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    shadow_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class RunEventRow(Base):
    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DecisionRow(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), index=True)
    rung: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float, default=0.0)
    units_sold: Mapped[int] = mapped_column(Integer, default=0)
    run_rate: Mapped[float] = mapped_column(Float, default=0.0)
    projected_clearance: Mapped[float] = mapped_column(Float, default=0.0)
    residual: Mapped[float] = mapped_column(Float, default=0.0)
    ratio: Mapped[float] = mapped_column(Float, default=0.0)
    decision: Mapped[str] = mapped_column(String(16))
    approval: Mapped[str] = mapped_column(String(16), default="NOT_REQUIRED")
    reason: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PriceChangeRow(Base):
    """Durable ledger of applied prices — idempotent on (run_id, rung)."""

    __tablename__ = "price_changes"
    __table_args__ = (UniqueConstraint("run_id", "rung", name="uq_price_run_rung"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), index=True)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    jpin: Mapped[str] = mapped_column(String(64), index=True)
    rung: Mapped[str] = mapped_column(String(8))
    from_price: Mapped[float] = mapped_column(Float, default=0.0)
    to_price: Mapped[float] = mapped_column(Float, default=0.0)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), index=True)
    payload: Mapped[str] = mapped_column(Text)  # serialised AuditEvent JSON
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OfferRow(Base):
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), index=True)
    rung: Mapped[str] = mapped_column(String(8))
    headline: Mapped[str] = mapped_column(String(256), default="")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    channel: Mapped[str] = mapped_column(String(32), default="retail_media")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
