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
    # v2 additions
    q0_source: Mapped[str] = mapped_column(String(32), default="synthetic")
    low_confidence: Mapped[bool] = mapped_column(Boolean, default=False)
    clearance_mode: Mapped[str] = mapped_column(String(32), default="CLEAR_SAMEDAY")
    reorder_action: Mapped[str] = mapped_column(String(32), default="NONE")
    floor_price: Mapped[float] = mapped_column(Float, default=0.0)
    standing_rule_pct: Mapped[float] = mapped_column(Float, default=100.0)
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
    """Durable ledger of applied prices — idempotent on (run_id, price_seq)."""

    __tablename__ = "price_changes"
    __table_args__ = (UniqueConstraint("run_id", "price_seq", name="uq_price_run_seq"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), index=True)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    jpin: Mapped[str] = mapped_column(String(64), index=True)
    price_seq: Mapped[int] = mapped_column(Integer)
    rung: Mapped[str] = mapped_column(String(8), default="")  # display label
    from_price: Mapped[float] = mapped_column(Float, default=0.0)
    to_price: Mapped[float] = mapped_column(Float, default=0.0)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), index=True)
    payload: Mapped[str] = mapped_column(Text)
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


class OfferBaselineRow(Base):
    """Snapshot captured at the instant a markdown is applied — the 'before'."""

    __tablename__ = "offer_baselines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), index=True)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    jpin: Mapped[str] = mapped_column(String(64), index=True)
    product_title: Mapped[str] = mapped_column(String(256), default="")
    rung: Mapped[str] = mapped_column(String(8), default="")
    from_price: Mapped[float] = mapped_column(Float, default=0.0)
    to_price: Mapped[float] = mapped_column(Float, default=0.0)
    discount_pct: Mapped[float] = mapped_column(Float, default=0.0)
    ts_ist: Mapped[str] = mapped_column(String(40), default="")
    units_sold_before: Mapped[int] = mapped_column(Integer, default=0)
    rate_before: Mapped[float] = mapped_column(Float, default=0.0)
    units_left_before: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OfferOutcomeRow(Base):
    """The 'after' + computed lift — the education card for the Giant."""

    __tablename__ = "offer_outcomes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), index=True)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    jpin: Mapped[str] = mapped_column(String(64), index=True)
    product_title: Mapped[str] = mapped_column(String(256), default="")
    rung: Mapped[str] = mapped_column(String(8), default="")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    discount_pct: Mapped[float] = mapped_column(Float, default=0.0)
    ts_ist: Mapped[str] = mapped_column(String(40), default="")
    phase: Mapped[str] = mapped_column(String(16), index=True, default="interim")
    rate_before: Mapped[float] = mapped_column(Float, default=0.0)
    rate_after: Mapped[float] = mapped_column(Float, default=0.0)
    lift_pct: Mapped[float] = mapped_column(Float, default=0.0)
    units_sold_after: Mapped[int] = mapped_column(Integer, default=0)
    incremental_units: Mapped[float] = mapped_column(Float, default=0.0)
    units_left: Mapped[int] = mapped_column(Integer, default=0)
    revenue_recovered: Mapped[float] = mapped_column(Float, default=0.0)
    waste_avoided_units: Mapped[int] = mapped_column(Integer, default=0)
    waste_avoided_value: Mapped[float] = mapped_column(Float, default=0.0)
    headline: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SellThroughSnapshotRow(Base):
    """Shared per-facility sell-through read-model (v3 poller writes; batch
    workflows read). Decouples the ONE slow OUTWARDED scan per JPIN from the N
    batch workflows that need it, and gives a fast, cache-friendly source. Keyed
    on (facility_id, jpin, receipt_date) — last write wins.
    """

    __tablename__ = "sell_through_snapshots"
    __table_args__ = (
        UniqueConstraint("facility_id", "jpin", "receipt_date",
                         name="uq_snapshot_fac_jpin_date"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_id: Mapped[str] = mapped_column(String(64), index=True)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    jpin: Mapped[str] = mapped_column(String(64), index=True)
    receipt_date: Mapped[str] = mapped_column(String(16), index=True)
    q0: Mapped[int] = mapped_column(Integer, default=0)
    q0_source: Mapped[str] = mapped_column(String(32), default="none")
    units_sold_today: Mapped[int] = mapped_column(Integer, default=0)
    recent_rate: Mapped[float] = mapped_column(Float, default=0.0)
    window_h: Mapped[float] = mapped_column(Float, default=0.0)
    low_confidence: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at_ms: Mapped[int] = mapped_column(Integer, default=0)   # epoch-ms of the read
    stale: Mapped[bool] = mapped_column(Boolean, default=False)      # last poll failed
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class DeadStockCandidateRow(Base):
    """Discovered dead-stock candidates per store (from posgateway). One row per
    (store_id, jpin) — last write wins; `run_id` links to an active clearance run."""

    __tablename__ = "dead_stock_candidates"
    __table_args__ = (
        UniqueConstraint("store_id", "jpin", name="uq_deadstock_store_jpin"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    jpin: Mapped[str] = mapped_column(String(64), index=True)
    product_title: Mapped[str] = mapped_column(String(256), default="")
    days_unsold: Mapped[int] = mapped_column(Integer, default=0)
    shelf_life_days: Mapped[int] = mapped_column(Integer, default=0)
    remaining_shelf_life_days: Mapped[int] = mapped_column(Integer, default=0)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    rank: Mapped[int] = mapped_column(Integer, default=1_000_000)
    status: Mapped[str] = mapped_column(String(24), default="FLAGGED", index=True)
    run_id: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class DeadStockRun(Base):
    """Read-model for a multi-day dead-stock clearance run (mirrors MarkdownRun
    but with shelf-life fields). Ledgers (decisions/price_changes/events/audit) are
    the generic tables keyed by run_id."""

    __tablename__ = "dead_stock_runs"
    run_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    jpin: Mapped[str] = mapped_column(String(64), index=True)
    product_title: Mapped[str] = mapped_column(String(256), default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    is_rte: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="STARTED")
    shelf_life_days: Mapped[int] = mapped_column(Integer, default=0)
    days_since_received: Mapped[int] = mapped_column(Integer, default=0)
    days_to_expiry: Mapped[int] = mapped_column(Integer, default=0)
    remaining_shelf_life_days: Mapped[int] = mapped_column(Integer, default=0)
    days_unsold: Mapped[int] = mapped_column(Integer, default=0)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    list_price: Mapped[float] = mapped_column(Float, default=0.0)
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    floor_price: Mapped[float] = mapped_column(Float, default=0.0)
    current_discount_pct: Mapped[float] = mapped_column(Float, default=0.0)
    mode: Mapped[str] = mapped_column(String(32), default="HOLD")
    reorder_action: Mapped[str] = mapped_column(String(32), default="NONE")
    awaiting_approval: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    standing_rule_pct: Mapped[float] = mapped_column(Float, default=100.0)
    simulate: Mapped[bool] = mapped_column(Boolean, default=False)
    shadow_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
