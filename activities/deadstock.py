"""Activities for dead-stock multi-day clearance — all I/O lives here.

- discover_dead_stock: posgateway dead-stock list for a store (adapters/deadstock).
- plan_deadstock_run: join the posgateway flag + SKU-master shelf life + live Bolt
  stock detail into a snapshotted DeadStockPlan (ineligible if not actionable).
- read_deadstock_stock: daily refresh of on-hand / days-since-received during a run.
- persist_deadstock_state / persist_deadstock_candidate: read-model writes.

Price application, decisions, events and audit reuse the same-day activities
(apply_price_goldeneye, persist_decision, record_run_event, write_audit).
"""
from __future__ import annotations

import time

from temporalio import activity

from adapters import _bolt, deadstock, inventory, sku_master
from db import repo
from pricing.ladder import default_config
from shared.models import DeadStockItem, DeadStockPlan, DeadStockState
from shared.stores import get_store

_DAY_MS = 86_400_000


@activity.defn
async def discover_dead_stock(store_id: str) -> list[DeadStockItem]:
    """Dead-stock candidates for a store, most-urgent first (posgateway)."""
    items = await deadstock.fetch_dead_stock(store_id)
    return [DeadStockItem(**it) for it in items]


def _days_since(ms: int | None) -> int:
    if not ms:
        return 0
    return max(0, int((int(time.time() * 1000) - int(ms)) // _DAY_MS))


@activity.defn
async def plan_deadstock_run(
    store_id: str,
    jpin: str,
    days_unsold: int,
    shadow_mode: bool,
    demo_speed: float,
    mock_gateway: bool = False,
) -> DeadStockPlan:
    """Snapshot the facts for a dead-stock clearance run. Ineligible when we can't
    price it (no shelf life, no live stock, or no list price)."""
    if mock_gateway:
        _bolt.use_mock_gateway()
    cfg = default_config(shadow_mode=shadow_mode, demo_speed=max(1.0, demo_speed))
    sku = sku_master.resolve_sku(jpin)

    def _ineligible(reason: str) -> DeadStockPlan:
        return DeadStockPlan(
            store_id=store_id, jpin=jpin,
            product_title=(sku or {}).get("product_title", jpin),
            category=(sku or {}).get("category", ""),
            shelf_life_days=(sku or {}).get("shelf_life_days", 0),
            days_unsold=days_unsold, config=cfg,
            eligible=False, skip_reason=reason,
        )

    if not sku or not sku.get("shelf_life_days"):
        return _ineligible("no shelf life in SKU master for JPIN")
    if not inventory.live_enabled():
        return _ineligible("Bolt Gateway not configured (INVENTORY_SOURCE != live)")

    facility_id = (get_store(store_id) or {}).get("facility_id")
    if not facility_id:
        return _ineligible(f"no facility_id for store {store_id}")

    detail = await inventory.live_stock_detail(jpin, facility_id)
    on_hand = detail.get("on_hand") or 0
    if on_hand <= 0:
        return _ineligible("no live on-hand stock")

    list_price = detail.get("list_price") or (sku.get("mrp") or 0.0)
    if not list_price or list_price <= 0:
        return _ineligible("no live list price / MRP")

    days_since_received = _days_since(detail.get("oldest_created_ms"))
    floor_price = round(0.2 * list_price, 2)   # 80%-off clearance floor (tunable)

    return DeadStockPlan(
        store_id=store_id, jpin=jpin,
        product_title=sku.get("product_title", jpin),
        category=sku.get("category", ""),
        is_rte=False,
        shelf_life_days=int(sku["shelf_life_days"]),
        days_since_received=days_since_received,
        days_unsold=days_unsold,
        on_hand=int(on_hand),
        list_price=float(list_price),
        floor_price=floor_price,
        mrp=float(sku.get("mrp") or 0.0),
        config=cfg,
        eligible=True,
    )


@activity.defn
async def read_deadstock_stock(store_id: str, jpin: str, mock_gateway: bool = False) -> dict:
    """Daily refresh: current on-hand + days-since-received from live Bolt.
    Returns {"on_hand": int|None, "days_since_received": int|None}."""
    if mock_gateway:
        _bolt.use_mock_gateway()
    facility_id = (get_store(store_id) or {}).get("facility_id") or ""
    detail = await inventory.live_stock_detail(jpin, facility_id)
    return {
        "on_hand": detail.get("on_hand"),
        "days_since_received": _days_since(detail.get("oldest_created_ms")),
    }


@activity.defn
async def resolve_sku_meta(jpin: str) -> dict:
    """Light SKU-master lookup for the candidate list (shelf life + title). Empty
    dict when the JPIN is absent from the master."""
    return sku_master.resolve_sku(jpin) or {}


@activity.defn
async def persist_deadstock_state(run_id: str, state: DeadStockState) -> None:
    repo.upsert_dead_stock_run(run_id, state)


@activity.defn
async def persist_deadstock_candidate(
    store_id: str, jpin: str, product_title: str, days_unsold: int,
    shelf_life_days: int, remaining_shelf_life_days: int, on_hand: int,
    rank: int, status: str, run_id: str = "",
) -> None:
    repo.upsert_dead_stock_candidate(
        store_id=store_id, jpin=jpin, product_title=product_title,
        days_unsold=days_unsold, shelf_life_days=shelf_life_days,
        remaining_shelf_life_days=remaining_shelf_life_days, on_hand=on_hand,
        rank=rank, status=status, run_id=run_id,
    )
