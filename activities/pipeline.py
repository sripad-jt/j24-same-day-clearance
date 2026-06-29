"""Activities — all I/O and non-determinism live here (design §13).

Each delegates to an adapter (stub-backed by synthetic data for the demo).
The workflow stays a pure orchestrator and calls these by name.
"""
from __future__ import annotations

import os
import time

from temporalio import activity

from adapters import catalog, copy_llm, goldeneye, inventory, notify, retailmedia
from db import repo
from pricing.ladder import default_config
from shared.models import (
    AuditEvent,
    Checkpoint,
    ReceiptContext,
    RunPlan,
    SellThrough,
)
from shared.stores import get_store

# Nominal start-of-day / close used to lay out the ladder (IST hours).
T0_HOUR_IST = 8


# Widest-first OUTWARDED window ladder (hours). We try the widest (most complete)
# window first and fall back to narrower, cheaper scans if it times out — the
# OUTWARDED scan is slow for high-volume JPINs. All stay strictly UNDER the
# gateway's 48h (2-day) hard cap on createdTimeAfter (48h itself risks rejection
# from clock skew, so the widest rung is 47h).
_DEFAULT_SELLTHROUGH_WINDOWS_H = (47.0, 36.0, 24.0)
_MAX_WINDOW_H = 47.0  # hard ceiling: gateway rejects createdTimeAfter older than 48h

# Total wall-clock budget (s) for the live read ladder per checkpoint, split
# across the windows. Kept under the _READ activity start_to_close in the workflow.
_LIVE_READ_BUDGET_S = 36.0


def _sellthrough_windows_h() -> list[float]:
    """OUTWARDED window ladder in hours, widest first.

    Override via INVENTORY_SELLTHROUGH_WINDOWS_H (comma-separated, e.g. "47,36,24").
    Values at/below 0 or above the 47h ceiling (gateway rejects >48h) are dropped.
    """
    raw = os.getenv("INVENTORY_SELLTHROUGH_WINDOWS_H")
    if not raw:
        return list(_DEFAULT_SELLTHROUGH_WINDOWS_H)
    out: list[float] = []
    for tok in raw.split(","):
        try:
            h = float(tok.strip())
        except ValueError:
            continue
        if 0 < h <= _MAX_WINDOW_H:
            out.append(h)
    out.sort(reverse=True)
    return out or list(_DEFAULT_SELLTHROUGH_WINDOWS_H)


def _since_ms(window_h: float) -> int:
    """Epoch-ms cutoff `window_h` hours ago (real clock — only called in activities)."""
    return int((time.time() - window_h * 3600) * 1000)


@activity.defn
async def plan_run(
    store_id: str,
    jpin: str,
    receipt_date: str,
    shadow_mode: bool,
    demo_speed: float,
) -> RunPlan:
    """Stage B + planning: build receipt context and the checkpoint timeline.

    Returns checkpoint offsets in *seconds* (scaled by demo_speed) so the
    deterministic workflow only ever sleeps and compares numbers.
    """
    cfg = default_config(shadow_mode=shadow_mode, demo_speed=max(1.0, demo_speed))
    sku = catalog.get_candidate(jpin)
    if sku is None:
        return RunPlan(
            receipt=ReceiptContext(
                store_id=store_id, jpin=jpin, receipt_date=receipt_date,
                product_title=jpin, category="UNKNOWN", is_rte=False,
                shelf_life_days=1, q0=0, list_price=0.0, mrp=0.0,
                received_epoch_ms=0,
            ),
            config=cfg, checkpoints=[], close_offset_s=0.0,
            eligible=False, skip_reason="JPIN not in catalogue",
        )

    # Opening stock (Q0) is synthetic, JPIN-keyed (stable across replays). There is
    # no trustworthy live Q0 from the Inventory API: active `leftQty` is a stale
    # 3-year lot pile (doesn't reflect sales), and OUTWARDED gives movements, not an
    # opening batch. Real sell-through (units sold) IS read live per checkpoint in
    # fetch_sellthrough via the OUTWARDED count.
    q0 = 30 + (sum(ord(c) for c in jpin) % 25)  # 30..54

    # List price (the markdown anchor) IS read live: the details API now returns a
    # real per-JPIN listingSellingPrice (e.g. ₹5–15), so snapshot it at run start.
    # Falls back to the catalogue placeholder on timeout / disabled / no price.
    list_price = sku.list_price
    if inventory.live_enabled():
        facility_id = (get_store(store_id) or {}).get("facility_id")
        if facility_id:
            live_price = await inventory.live_listing_price(jpin, facility_id)
            if live_price and live_price > 0:
                list_price = live_price

    receipt = ReceiptContext(
        store_id=store_id, jpin=jpin, receipt_date=receipt_date,
        product_title=sku.product_title, category=sku.category, is_rte=sku.is_rte,
        shelf_life_days=sku.shelf_life_days, q0=q0,
        list_price=list_price, mrp=sku.mrp,
        received_epoch_ms=0, expiry_date=receipt_date,
    )

    if list_price <= 0:
        return RunPlan(receipt=receipt, config=cfg, checkpoints=[],
                       close_offset_s=0.0, eligible=False,
                       skip_reason="no listing selling price")
    if q0 < cfg.min_q0:
        return RunPlan(receipt=receipt, config=cfg, checkpoints=[],
                       close_offset_s=0.0, eligible=False,
                       skip_reason=f"Q0 {q0} below min {cfg.min_q0}")

    close_h = cfg.store_close_hour
    total_h = float(close_h - T0_HOUR_IST)
    checkpoints: list[Checkpoint] = []
    for r in cfg.rungs:
        # Elapsed hours for this rung = whichever trigger comes first.
        candidates: list[float] = []
        if r.elapsed_hours is not None:
            candidates.append(r.elapsed_hours)
        if r.wallclock_hour_ist is not None:
            candidates.append(float(r.wallclock_hour_ist - T0_HOUR_IST))
        elapsed = min(candidates) if candidates else 0.0
        elapsed = max(0.0, min(elapsed, total_h))
        checkpoints.append(Checkpoint(
            rung_index=r.index,
            label=r.label,
            sleep_offset_s=elapsed * 3600.0 / cfg.demo_speed,
            nominal_elapsed_h=elapsed,
            nominal_remaining_h=max(0.0, total_h - elapsed),
            ceiling_pct=r.ceiling_pct,
            token_free=r.token_free,
            wallclock_hour_ist=int(T0_HOUR_IST + elapsed),
        ))

    return RunPlan(
        receipt=receipt, config=cfg, checkpoints=checkpoints,
        close_offset_s=total_h * 3600.0 / cfg.demo_speed, eligible=True,
    )


@activity.defn
async def fetch_sellthrough(
    store_id: str, jpin: str, q0: int, nominal_elapsed_h: float, total_h: float,
    trailing_window_h: float, markdown_pct: float,
) -> SellThrough:
    # LIVE: real units sold over the window from the OUTWARDED count (sales = outward
    # movements; active leftQty doesn't decrement). Best-effort — the OUTWARDED scan
    # times out for high-volume sellers, so fall back to the synthetic curve with a
    # low_confidence flag when it does.
    if inventory.live_enabled():
        facility_id = (get_store(store_id) or {}).get("facility_id")
        if facility_id:
            windows = _sellthrough_windows_h()
            # Split the read budget across the ladder so all attempts fit within
            # the _READ activity start_to_close timeout.
            per_try = max(8.0, _LIVE_READ_BUDGET_S / len(windows))
            for window_h in windows:
                sold = await inventory.live_units_sold(
                    jpin, facility_id, _since_ms(window_h), timeout=per_try
                )
                if sold is not None:
                    rate = sold / max(nominal_elapsed_h, 0.5)  # units / nominal hour
                    activity.logger.info(
                        "live sell-through %s @ %s: sold=%s/%gh", jpin, facility_id,
                        sold, window_h,
                    )
                    return SellThrough(
                        units_sold=sold, run_rate=rate, low_confidence=False
                    )
            activity.logger.warning(
                "live sell-through %s timed out at all windows %s — synthetic fallback",
                jpin, windows,
            )

    sold, rate = inventory.sell_through(
        jpin, q0, nominal_elapsed_h, total_h, trailing_window_h, markdown_pct
    )
    return SellThrough(
        units_sold=sold, run_rate=rate, low_confidence=inventory.live_enabled()
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
    from_price: float, to_price: float,
) -> bool:
    confirmed = goldeneye.apply_price(store_id, jpin, rung, to_price)
    if confirmed:
        repo.record_price_change(run_id, store_id, jpin, rung, from_price, to_price)
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
