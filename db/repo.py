"""All DB reads/writes via short-lived sessions. Used by activities and the API."""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import select

from db.database import SessionLocal
from db.models import (
    AuditEventRow,
    DeadStockCandidateRow,
    DeadStockRun,
    DecisionRow,
    MarkdownRun,
    OfferBaselineRow,
    OfferOutcomeRow,
    OfferRow,
    PriceChangeRow,
    RunEventRow,
    StoreRow,
)
from shared.models import (
    AuditEvent,
    DeadStockState,
    MarkdownState,
    OfferBaseline,
    OfferOutcome,
)


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
def upsert_run_from_state(run_id: str, state: MarkdownState) -> None:
    with SessionLocal.begin() as s:
        run = s.get(MarkdownRun, run_id)
        if run is None:
            run = MarkdownRun(run_id=run_id)
            s.add(run)
        run.store_id = state.store_id
        run.jpin = state.jpin
        run.receipt_date = state.receipt_date
        run.clearance_date = state.clearance_date
        run.product_title = state.product_title
        run.category = state.category
        run.is_rte = state.is_rte
        run.status = state.status.value
        run.current_rung = state.current_rung
        run.list_price = state.list_price
        run.current_price = state.current_price
        run.q0 = state.q0
        run.units_sold = state.units_sold
        run.awaiting_approval = state.awaiting_approval
        run.shadow_mode = state.shadow_mode
        run.summary = state.last_reason
        # v2 fields
        run.q0_source = state.q0_source
        run.low_confidence = state.low_confidence
        run.clearance_mode = state.clearance_mode
        run.reorder_action = state.reorder_action
        run.floor_price = state.floor_price
        run.standing_rule_pct = state.standing_rule_pct


def list_runs() -> list[dict]:
    with SessionLocal() as s:
        rows = s.scalars(
            select(MarkdownRun).order_by(MarkdownRun.created_at.desc())
        ).all()
        return [_run_summary(r) for r in rows]


def get_run(run_id: str) -> Optional[dict]:
    with SessionLocal() as s:
        run = s.get(MarkdownRun, run_id)
        if run is None:
            return None
        events = s.scalars(
            select(RunEventRow).where(RunEventRow.run_id == run_id)
            .order_by(RunEventRow.id)
        ).all()
        decisions = s.scalars(
            select(DecisionRow).where(DecisionRow.run_id == run_id)
            .order_by(DecisionRow.id)
        ).all()
        prices = s.scalars(
            select(PriceChangeRow).where(PriceChangeRow.run_id == run_id)
            .order_by(PriceChangeRow.id)
        ).all()
        offers = s.scalars(
            select(OfferRow).where(OfferRow.run_id == run_id).order_by(OfferRow.id)
        ).all()
        d = _run_summary(run)
        d["events"] = [
            {"kind": e.kind, "message": e.message, "ts": e.ts.isoformat()}
            for e in events
        ]
        d["decisions"] = [
            {
                "rung": x.rung, "price": x.price, "units_sold": x.units_sold,
                "run_rate": round(x.run_rate, 2), "ratio": round(x.ratio, 3),
                "residual": round(x.residual, 1), "decision": x.decision,
                "approval": x.approval, "reason": x.reason, "ts": x.ts.isoformat(),
            }
            for x in decisions
        ]
        d["price_changes"] = [
            {
                "rung": p.rung, "price_seq": p.price_seq,
                "from_price": p.from_price, "to_price": p.to_price,
                "confirmed": p.confirmed, "ts": p.ts.isoformat(),
            }
            for p in prices
        ]
        d["offers"] = [
            {"rung": o.rung, "headline": o.headline, "price": o.price,
             "channel": o.channel, "ts": o.ts.isoformat()}
            for o in offers
        ]
        return d


def _run_summary(r: MarkdownRun) -> dict:
    return {
        "run_id": r.run_id,
        "store_id": r.store_id,
        "jpin": r.jpin,
        "receipt_date": r.receipt_date,
        "clearance_date": r.clearance_date,
        "product_title": r.product_title,
        "category": r.category,
        "is_rte": r.is_rte,
        "status": r.status,
        "current_rung": r.current_rung,
        "list_price": r.list_price,
        "current_price": r.current_price,
        "q0": r.q0,
        "units_sold": r.units_sold,
        "awaiting_approval": r.awaiting_approval,
        "shadow_mode": r.shadow_mode,
        "summary": r.summary,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


# --------------------------------------------------------------------------- #
# Events / decisions / ledger / offers
# --------------------------------------------------------------------------- #
def add_event(run_id: str, kind: str, message: str) -> None:
    with SessionLocal.begin() as s:
        s.add(RunEventRow(run_id=run_id, kind=kind, message=message))


def add_decision(run_id: str, d: dict) -> None:
    with SessionLocal.begin() as s:
        s.add(DecisionRow(run_id=run_id, **d))


def record_price_change(
    run_id: str, store_id: str, jpin: str, rung: str,
    from_price: float, to_price: float, price_seq: int,
) -> bool:
    """Idempotent on (run_id, price_seq). Returns True if newly inserted."""
    with SessionLocal.begin() as s:
        existing = s.scalars(
            select(PriceChangeRow).where(
                PriceChangeRow.run_id == run_id,
                PriceChangeRow.price_seq == price_seq,
            )
        ).first()
        if existing is not None:
            return False
        s.add(PriceChangeRow(
            run_id=run_id, store_id=store_id, jpin=jpin, rung=rung,
            price_seq=price_seq, from_price=from_price, to_price=to_price,
            confirmed=True,
        ))
        return True


def add_audit(event: AuditEvent) -> None:
    with SessionLocal.begin() as s:
        s.add(AuditEventRow(run_id=event.run_id, payload=event.model_dump_json()))


def list_audit(run_id: str) -> list[dict]:
    with SessionLocal() as s:
        rows = s.scalars(
            select(AuditEventRow).where(AuditEventRow.run_id == run_id)
            .order_by(AuditEventRow.id)
        ).all()
        return [json.loads(r.payload) for r in rows]


def add_offer(run_id: str, rung: str, headline: str, price: float, channel: str) -> None:
    with SessionLocal.begin() as s:
        s.add(OfferRow(run_id=run_id, rung=rung, headline=headline,
                       price=price, channel=channel))


# --------------------------------------------------------------------------- #
# Owner-education feed (offer baselines + outcomes)
# --------------------------------------------------------------------------- #
def add_offer_baseline(b: OfferBaseline) -> None:
    with SessionLocal.begin() as s:
        s.add(OfferBaselineRow(**b.model_dump()))


def add_offer_outcome(o: OfferOutcome) -> None:
    with SessionLocal.begin() as s:
        s.add(OfferOutcomeRow(**o.model_dump()))


def list_outcomes_for_store(store_id: str) -> list[dict]:
    """Powers the My J24 'My Offers' pre/post feed for a Giant."""
    with SessionLocal() as s:
        rows = s.scalars(
            select(OfferOutcomeRow)
            .where(OfferOutcomeRow.store_id == store_id)
            .order_by(OfferOutcomeRow.id.desc())
        ).all()
        return [
            {
                "run_id": r.run_id, "jpin": r.jpin, "product_title": r.product_title,
                "phase": r.phase, "discount_pct": r.discount_pct,
                "rate_before": r.rate_before, "rate_after": r.rate_after,
                "lift_pct": r.lift_pct, "units_sold_after": r.units_sold_after,
                "units_left": r.units_left, "revenue_recovered": r.revenue_recovered,
                "waste_avoided_units": r.waste_avoided_units,
                "waste_avoided_value": r.waste_avoided_value,
                "headline": r.headline, "ts_ist": r.ts_ist,
            }
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #
def upsert_store(store_id: str, name: str = "", close_hour_ist: int = 21) -> None:
    with SessionLocal.begin() as s:
        row = s.get(StoreRow, store_id)
        if row is None:
            row = StoreRow(store_id=store_id)
            s.add(row)
        row.name = name or row.name or store_id
        row.close_hour_ist = close_hour_ist


def list_stores() -> list[dict]:
    with SessionLocal() as s:
        rows = s.scalars(select(StoreRow).order_by(StoreRow.store_id)).all()
        return [
            {"store_id": r.store_id, "name": r.name, "close_hour_ist": r.close_hour_ist}
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# Sell-through snapshots (v3 shared read-model)
# --------------------------------------------------------------------------- #
def upsert_sell_through_snapshot(
    *, facility_id: str, store_id: str, jpin: str, receipt_date: str,
    q0: int, q0_source: str, units_sold_today: int, recent_rate: float,
    window_h: float, low_confidence: bool, fetched_at_ms: int, stale: bool,
) -> None:
    from db.models import SellThroughSnapshotRow
    with SessionLocal.begin() as s:
        row = s.scalars(
            select(SellThroughSnapshotRow).where(
                SellThroughSnapshotRow.facility_id == facility_id,
                SellThroughSnapshotRow.jpin == jpin,
                SellThroughSnapshotRow.receipt_date == receipt_date,
            )
        ).first()
        if row is None:
            row = SellThroughSnapshotRow(
                facility_id=facility_id, jpin=jpin, receipt_date=receipt_date
            )
            s.add(row)
        row.store_id = store_id
        row.q0 = q0
        row.q0_source = q0_source
        row.units_sold_today = units_sold_today
        row.recent_rate = recent_rate
        row.window_h = window_h
        row.low_confidence = low_confidence
        row.fetched_at_ms = fetched_at_ms
        row.stale = stale


def get_sell_through_snapshot(
    facility_id: str, jpin: str, receipt_date: str
) -> Optional[dict]:
    from db.models import SellThroughSnapshotRow
    with SessionLocal() as s:
        row = s.scalars(
            select(SellThroughSnapshotRow).where(
                SellThroughSnapshotRow.facility_id == facility_id,
                SellThroughSnapshotRow.jpin == jpin,
                SellThroughSnapshotRow.receipt_date == receipt_date,
            )
        ).first()
        if row is None:
            return None
        return {
            "q0": row.q0, "q0_source": row.q0_source,
            "units_sold_today": row.units_sold_today,
            "recent_rate": row.recent_rate, "window_h": row.window_h,
            "low_confidence": row.low_confidence,
            "fetched_at_ms": row.fetched_at_ms, "stale": row.stale,
        }


# --------------------------------------------------------------------------- #
# Dead-stock candidates + runs
# --------------------------------------------------------------------------- #
def upsert_dead_stock_candidate(
    *, store_id: str, jpin: str, product_title: str, days_unsold: int,
    shelf_life_days: int, remaining_shelf_life_days: int, on_hand: int,
    rank: int, status: str, run_id: str = "",
) -> None:
    with SessionLocal.begin() as s:
        row = s.scalars(
            select(DeadStockCandidateRow).where(
                DeadStockCandidateRow.store_id == store_id,
                DeadStockCandidateRow.jpin == jpin,
            )
        ).first()
        if row is None:
            row = DeadStockCandidateRow(store_id=store_id, jpin=jpin)
            s.add(row)
        row.product_title = product_title
        row.days_unsold = days_unsold
        row.shelf_life_days = shelf_life_days
        row.remaining_shelf_life_days = remaining_shelf_life_days
        row.on_hand = on_hand
        row.rank = rank
        row.status = status
        if run_id:
            row.run_id = run_id


def list_dead_stock_candidates(store_id: str) -> list[dict]:
    with SessionLocal() as s:
        rows = s.scalars(
            select(DeadStockCandidateRow)
            .where(DeadStockCandidateRow.store_id == store_id)
            .order_by(DeadStockCandidateRow.rank)
        ).all()
        return [
            {
                "store_id": r.store_id, "jpin": r.jpin,
                "product_title": r.product_title, "days_unsold": r.days_unsold,
                "shelf_life_days": r.shelf_life_days,
                "remaining_shelf_life_days": r.remaining_shelf_life_days,
                "on_hand": r.on_hand, "rank": r.rank, "status": r.status,
                "run_id": r.run_id,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]


def _deadstock_run_summary(r: DeadStockRun) -> dict:
    return {
        "run_id": r.run_id, "store_id": r.store_id, "jpin": r.jpin,
        "product_title": r.product_title, "category": r.category, "is_rte": r.is_rte,
        "status": r.status, "shelf_life_days": r.shelf_life_days,
        "days_since_received": r.days_since_received, "days_to_expiry": r.days_to_expiry,
        "remaining_shelf_life_days": r.remaining_shelf_life_days,
        "days_unsold": r.days_unsold, "on_hand": r.on_hand,
        "list_price": r.list_price, "current_price": r.current_price,
        "floor_price": r.floor_price, "current_discount_pct": r.current_discount_pct,
        "mode": r.mode, "reorder_action": r.reorder_action,
        "awaiting_approval": r.awaiting_approval, "standing_rule_pct": r.standing_rule_pct,
        "simulate": r.simulate, "shadow_mode": r.shadow_mode, "summary": r.summary,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def upsert_dead_stock_run(run_id: str, state: DeadStockState) -> None:
    with SessionLocal.begin() as s:
        run = s.get(DeadStockRun, run_id)
        if run is None:
            run = DeadStockRun(run_id=run_id)
            s.add(run)
        run.store_id = state.store_id
        run.jpin = state.jpin
        run.product_title = state.product_title
        run.category = state.category
        run.is_rte = state.is_rte
        run.status = state.status.value
        run.shelf_life_days = state.shelf_life_days
        run.days_since_received = state.days_since_received
        run.days_to_expiry = state.days_to_expiry
        run.remaining_shelf_life_days = state.remaining_shelf_life_days
        run.days_unsold = state.days_unsold
        run.on_hand = state.on_hand
        run.list_price = state.list_price
        run.current_price = state.current_price
        run.floor_price = state.floor_price
        run.current_discount_pct = state.current_discount_pct
        run.mode = state.mode
        run.reorder_action = state.reorder_action
        run.awaiting_approval = state.awaiting_approval
        run.standing_rule_pct = state.standing_rule_pct
        run.simulate = state.simulate
        run.summary = state.last_reason


def list_dead_stock_runs(store_id: str) -> list[dict]:
    with SessionLocal() as s:
        rows = s.scalars(
            select(DeadStockRun)
            .where(DeadStockRun.store_id == store_id)
            .order_by(DeadStockRun.updated_at.desc())
        ).all()
        return [_deadstock_run_summary(r) for r in rows]


def get_dead_stock_run(run_id: str) -> Optional[dict]:
    with SessionLocal() as s:
        run = s.get(DeadStockRun, run_id)
        if run is None:
            return None
        d = _deadstock_run_summary(run)
        events = s.scalars(
            select(RunEventRow).where(RunEventRow.run_id == run_id)
            .order_by(RunEventRow.id)
        ).all()
        decisions = s.scalars(
            select(DecisionRow).where(DecisionRow.run_id == run_id)
            .order_by(DecisionRow.id)
        ).all()
        prices = s.scalars(
            select(PriceChangeRow).where(PriceChangeRow.run_id == run_id)
            .order_by(PriceChangeRow.id)
        ).all()
        d["events"] = [
            {"kind": e.kind, "message": e.message, "ts": e.ts.isoformat()}
            for e in events
        ]
        d["decisions"] = [
            {
                "rung": x.rung, "price": x.price, "units_sold": x.units_sold,
                "run_rate": round(x.run_rate, 2), "ratio": round(x.ratio, 3),
                "residual": round(x.residual, 1), "decision": x.decision,
                "approval": x.approval, "reason": x.reason, "ts": x.ts.isoformat(),
            }
            for x in decisions
        ]
        d["price_changes"] = [
            {
                "rung": p.rung, "price_seq": p.price_seq,
                "from_price": p.from_price, "to_price": p.to_price,
                "confirmed": p.confirmed, "ts": p.ts.isoformat(),
            }
            for p in prices
        ]
        return d
