"""All DB reads/writes via short-lived sessions. Used by activities and the API."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from db.database import SessionLocal
from db.models import (
    AuditEventRow,
    DecisionRow,
    MarkdownRun,
    OfferRow,
    PriceChangeRow,
    RunEventRow,
    StoreRow,
)
from shared.models import AuditEvent, MarkdownState


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
                "rung": p.rung, "from_price": p.from_price, "to_price": p.to_price,
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
    from_price: float, to_price: float,
) -> bool:
    """Idempotent on (run_id, rung). Returns True if newly inserted."""
    with SessionLocal.begin() as s:
        existing = s.scalars(
            select(PriceChangeRow).where(
                PriceChangeRow.run_id == run_id, PriceChangeRow.rung == rung
            )
        ).first()
        if existing is not None:
            return False
        s.add(PriceChangeRow(
            run_id=run_id, store_id=store_id, jpin=jpin, rung=rung,
            from_price=from_price, to_price=to_price, confirmed=True,
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
        import json
        return [json.loads(r.payload) for r in rows]


def add_offer(run_id: str, rung: str, headline: str, price: float, channel: str) -> None:
    with SessionLocal.begin() as s:
        s.add(OfferRow(run_id=run_id, rung=rung, headline=headline,
                       price=price, channel=channel))


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
