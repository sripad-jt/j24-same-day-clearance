"""Activities — all I/O and non-determinism live here.

All live data comes from the Bolt Gateway (Inventory Item Details API).
There is no synthetic fallback: if the API is unavailable, plan_run returns
ineligible and fetch_sellthrough raises so Temporal retries per its policy.
"""
from __future__ import annotations

import asyncio
import time

from temporalio import activity

from adapters import catalog, copy_llm, goldeneye, inventory, notify, retailmedia
from db import repo
from pricing.ladder import default_config
from shared.models import (
    AuditEvent,
    ReceiptContext,
    RunPlan,
    SellThroughV2,
)
from shared.stores import get_store

T0_HOUR_IST = 8   # nominal start-of-day (IST)


def _t0_today_ms(receipt_date: str) -> int:
    """Epoch-ms for T0 = receipt_date at 08:00 IST (02:30 UTC)."""
    import datetime
    dt = datetime.datetime.strptime(receipt_date, "%Y-%m-%d").replace(
        hour=2, minute=30, tzinfo=datetime.timezone.utc
    )
    return int(dt.timestamp() * 1000)


@activity.defn
async def plan_run(
    store_id: str,
    jpin: str,
    receipt_date: str,
    shadow_mode: bool,
    demo_speed: float,
) -> RunPlan:
    """Build receipt context from live Bolt data. Ineligible if Bolt unavailable."""
    cfg = default_config(shadow_mode=shadow_mode, demo_speed=max(1.0, demo_speed))
    sku = catalog.get_candidate(jpin)

    def _ineligible(reason: str, q0: int = 0, list_price: float = 0.0) -> RunPlan:
        return RunPlan(
            receipt=ReceiptContext(
                store_id=store_id, jpin=jpin, receipt_date=receipt_date,
                product_title=sku.product_title if sku else jpin,
                category=sku.category if sku else "UNKNOWN",
                is_rte=sku.is_rte if sku else False,
                shelf_life_days=sku.shelf_life_days if sku else 1,
                q0=q0, q0_source="none",
                list_price=list_price, mrp=sku.mrp if sku else 0.0,
                received_epoch_ms=0,
            ),
            config=cfg, close_offset_s=0.0,
            eligible=False, skip_reason=reason,
        )

    if sku is None:
        return _ineligible("JPIN not in catalogue")

    if not inventory.live_enabled():
        return _ineligible("Bolt Gateway not configured (INVENTORY_SOURCE != live)")

    facility_id = (get_store(store_id) or {}).get("facility_id")
    if not facility_id:
        return _ineligible(f"no facility_id for store {store_id}")

    t0_ms = _t0_today_ms(receipt_date)

    # Fetch list price and opening Q0 in parallel
    live_price, live_q0 = await asyncio.gather(
        inventory.live_listing_price(jpin, facility_id),
        inventory.live_q0_from_lots(jpin, facility_id, t0_ms),
        return_exceptions=True,
    )

    if isinstance(live_price, Exception) or not live_price or live_price <= 0:
        return _ineligible(f"Bolt: no listing selling price for {jpin} — {live_price!r}")

    if isinstance(live_q0, Exception) or not live_q0 or live_q0 <= 0:
        return _ineligible(f"Bolt: no inwarded stock (Q0) for {jpin} today — {live_q0!r}")

    receipt = ReceiptContext(
        store_id=store_id, jpin=jpin, receipt_date=receipt_date,
        product_title=sku.product_title, category=sku.category, is_rte=sku.is_rte,
        shelf_life_days=sku.shelf_life_days, q0=live_q0, q0_source="lot_initial_qty",
        list_price=live_price, mrp=sku.mrp,
        received_epoch_ms=0, expiry_date=receipt_date,
    )

    if live_q0 < cfg.min_q0:
        return RunPlan(receipt=receipt, config=cfg, close_offset_s=0.0,
                       eligible=False, skip_reason=f"Q0 {live_q0} below min {cfg.min_q0}")

    total_h = float(cfg.store_close_hour - T0_HOUR_IST)
    return RunPlan(
        receipt=receipt, config=cfg,
        close_offset_s=total_h * 3600.0 / cfg.demo_speed,
        floor_price=0.0,
        eligible=True,
    )


@activity.defn
async def fetch_sellthrough(
    store_id: str,
    jpin: str,
    fallback_q0: int,
    t0_ms: int,
    trailing_window_h: float,
    current_discount_pct: float = 0.0,
) -> SellThroughV2:
    """Fetch today-bounded sell-through from Bolt. Raises on failure (Temporal retries)."""
    if not inventory.live_enabled():
        raise RuntimeError("Bolt Gateway not configured — cannot fetch sell-through")

    facility_id = (get_store(store_id) or {}).get("facility_id")
    if not facility_id:
        raise RuntimeError(f"no facility_id for store {store_id}")

    now_ms = int(time.time() * 1000)
    trail_start_ms = max(t0_ms, now_ms - int(trailing_window_h * 3600 * 1000))
    elapsed_h = max(0.5, (now_ms - t0_ms) / 3_600_000)

    # Fire all three Bolt calls in parallel
    live_q0, units_today, units_in_window = await asyncio.gather(
        inventory.live_q0_from_lots(jpin, facility_id, t0_ms),
        inventory.live_units_sold(jpin, facility_id, t0_ms),
        inventory.live_units_sold(jpin, facility_id, trail_start_ms),
        return_exceptions=True,
    )

    if isinstance(units_today, Exception) or units_today is None:
        raise RuntimeError(
            f"Bolt: units_sold unavailable for {jpin} — {units_today!r}"
        )

    q0 = (live_q0 if (not isinstance(live_q0, Exception) and live_q0 and live_q0 > 0)
          else fallback_q0)
    q0_source = "lot_initial_qty" if q0 != fallback_q0 else "plan_q0"

    cumulative_rate = units_today / elapsed_h
    window_h = max(0.01, (now_ms - trail_start_ms) / 3_600_000)
    if isinstance(units_in_window, Exception) or units_in_window is None:
        recent_rate = cumulative_rate
    else:
        recent_rate = units_in_window / window_h

    activity.logger.info(
        "sell-through %s: q0=%s(%s) today=%s trail=%s/%gh rate=%.2f/h",
        jpin, q0, q0_source, units_today, units_in_window, trailing_window_h, recent_rate,
    )
    return SellThroughV2(
        units_sold_today=units_today,
        recent_rate=round(recent_rate, 3),
        cumulative_rate=round(cumulative_rate, 3),
        q0=q0,
        q0_source=q0_source,
        window_h=round(trailing_window_h, 2),
        low_confidence=isinstance(units_in_window, Exception) or units_in_window is None,
    )


@activity.defn
async def request_owner_approval(
    store_id: str, jpin: str, product: str, from_price: float,
    to_price: float, units_left: int, reason: str,
) -> str:
    return notify.push_approval_card(
        store_id, jpin, product, from_price, to_price, units_left, reason
    )


@activity.defn
async def shape_offer_llm(
    product: str, pct_off: float, token_free: bool, enable_llm: bool
) -> str:
    return copy_llm.offer_copy(product, pct_off, token_free, enable_llm)


@activity.defn
async def apply_price_goldeneye(
    run_id: str, store_id: str, jpin: str, rung: str,
    from_price: float, to_price: float, price_seq: int,
) -> bool:
    confirmed = goldeneye.apply_price(store_id, jpin, rung, to_price)
    if confirmed:
        repo.record_price_change(run_id, store_id, jpin, rung, from_price, to_price, price_seq)
    return confirmed


@activity.defn
async def publish_offer(
    run_id: str, store_id: str, jpin: str, headline: str, price: float, rung: str,
) -> None:
    retailmedia.publish_offer(store_id, jpin, headline, price)
    repo.add_offer(run_id, rung, headline, price, "retail_media")


@activity.defn
async def write_audit(event: AuditEvent) -> None:
    repo.add_audit(event)


@activity.defn
async def notify_owner(store_id: str, message: str) -> None:
    notify.notify_owner(store_id, message)
