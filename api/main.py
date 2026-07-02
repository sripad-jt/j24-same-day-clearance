"""FastAPI control plane for the React app — start, monitor, and steer runs.

Thin bridge: reads the Postgres read-model for lists/detail and the live Temporal
query for in-flight state; writes are Temporal signals (owner decision, override,
GRN, sold-out). The workflow remains the source of truth.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError

from adapters import inventory
from adapters.catalog import discover_candidates, get_candidate
from db import repo
from db.database import init_db
from pricing.ladder import default_config
from shared.config import TASK_QUEUE, get_client
from workflows.deadstock import DeadStockClearanceWorkflow
from workflows.deadstock_parent import DeadStockDiscoveryWorkflow
from shared.models import (
    AdditionalGrn,
    DeadStockDiscoverRequest,
    DeadStockSeedRequest,
    ManualOverride,
    OwnerDecision,
    SeedRequest,
    SimulateRequest,
    StandingRuleRequest,
)
from shared.stores import DEFAULT_STORE_ID, STORE_DIRECTORY, get_store
from workflows.markdown import PerishableMarkdownWorkflow

app = FastAPI(title="Perishables Markdown Control Plane")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

_client: Client | None = None


async def client() -> Client:
    global _client
    if _client is None:
        _client = await get_client()
    return _client


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    await client()


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
async def get_config() -> dict:
    return default_config().model_dump()


@app.get("/api/stores")
async def stores() -> list[dict]:
    """Full J24 store directory for the picker (bzid + facility + org ids)."""
    return STORE_DIRECTORY


@app.get("/api/candidates")
async def candidates(store_id: str = DEFAULT_STORE_ID) -> dict:
    """Selectable leafy-green candidates for a store (product-level catalogue)."""
    store = get_store(store_id)
    cands = discover_candidates(store_id, 1000)
    return {
        "store": store,
        "candidates": [
            {
                "jpin": c.jpin,
                "product_title": c.product_title,
                "category": c.category,
                "is_rte": c.is_rte,
                "shelf_life_days": c.shelf_life_days,
                "list_price": c.list_price,
                "mrp": c.mrp,
            }
            for c in cands
        ],
    }


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
@app.get("/api/runs")
async def list_runs() -> list[dict]:
    return repo.list_runs()


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    row = repo.get_run(run_id)
    if row is None:
        raise HTTPException(404, "run not found")
    # Best-effort live state from the running workflow.
    try:
        handle = (await client()).get_workflow_handle(run_id)
        live = await handle.query(PerishableMarkdownWorkflow.current_state)
        if live is not None:
            row["live"] = live.model_dump()
    except (RPCError, Exception):
        row["live"] = None
    return row


@app.get("/api/runs/{run_id}/audit")
async def get_audit(run_id: str) -> list[dict]:
    return repo.list_audit(run_id)


# In-memory inventory cache: store_id → snapshot dict.
# The Bolt fetch runs in a background task so the HTTP response returns
# immediately — avoiding the upstream nginx proxy timeout. The scan itself can
# take minutes for high-volume sellers; the timeout is env-tunable (default 3 min).
_inv_cache: dict[str, dict] = {}
_inv_fetching: set[str] = set()
_BOLT_FETCH_TIMEOUT = float(os.getenv("BOLT_SELLTHROUGH_TIMEOUT_S", "180"))
_CACHE_TTL = 300.0            # seconds — treat cache as fresh for 5 min


async def _fetch_inventory_bg(
    store_id: str, facility_id: str, jpins: list[str], titles: dict[str, str]
) -> None:
    """Background task: run Bolt calls for up to 2 min, then update cache."""
    if store_id in _inv_fetching:
        return  # already in flight
    _inv_fetching.add(store_id)
    t0_ms = inventory.t0_today_ms()
    try:
        snap = await inventory.live_sold_snapshot(
            jpins, facility_id, t0_ms, timeout=_BOLT_FETCH_TIMEOUT
        )
        any_null = any(
            v.get("sold_today") is None or v.get("inventory_at_t0") is None
            for v in snap.values()
        )
        source = "partial" if any_null else "live"
    except Exception:  # noqa: BLE001
        snap, source = {}, "error"
    finally:
        _inv_fetching.discard(store_id)

    items = [
        {
            "jpin": j,
            "product_title": titles[j],
            "inventory_at_t0": (snap.get(j) or {}).get("inventory_at_t0"),
            "received_today": (snap.get(j) or {}).get("received_today"),
            "sold_today": (snap.get(j) or {}).get("sold_today"),
            "t0_ms": t0_ms,
        }
        for j in jpins
    ]
    _inv_cache[store_id] = {
        "source": source, "t0_ms": t0_ms,
        "items": items, "fetched_at": time.time(),
    }


@app.get("/api/inventory")
async def inventory_snapshot(
    store_id: str = DEFAULT_STORE_ID,
    refresh: bool = False,
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
) -> dict:
    """Live inventory snapshot per leafy-green JPIN since T0 today (05:00 IST).

    Returns immediately from cache (source: live/partial/error) or with
    source: "loading" on the first call. The actual Bolt fetch runs in the
    background (up to 120s) — the frontend polls until source changes.
    Pass ?refresh=true to force a new background fetch even when the cache
    is fresh.
    """
    store = get_store(store_id)
    cands = discover_candidates(store_id, 1000)
    titles = {c.jpin: c.product_title for c in cands}
    jpins = list(titles)
    facility_id = (store or {}).get("facility_id")
    t0_ms = inventory.t0_today_ms()

    cached = _inv_cache.get(store_id)
    is_fetching = store_id in _inv_fetching
    cache_stale = not cached or (time.time() - cached.get("fetched_at", 0)) > _CACHE_TTL

    # Kick off a background fetch when: no cache yet, cache is stale, or forced.
    if inventory.live_enabled() and facility_id and not is_fetching:
        if cache_stale or refresh:
            background_tasks.add_task(
                _fetch_inventory_bg, store_id, facility_id, jpins, titles
            )
            is_fetching = True

    if cached:
        return {
            **cached,
            "store": store,
            "facility_id": facility_id,
            "loading": is_fetching,
        }

    # First-ever load — nothing cached yet, fetch just kicked off.
    empty_items = [
        {"jpin": j, "product_title": titles[j],
         "inventory_at_t0": None, "received_today": None, "sold_today": None,
         "t0_ms": t0_ms}
        for j in jpins
    ]
    return {
        "store": store, "facility_id": facility_id,
        "source": "loading" if is_fetching else "error",
        "t0_ms": t0_ms, "items": empty_items, "loading": is_fetching,
    }


@app.post("/api/runs/seed")
async def seed(req: SeedRequest) -> dict:
    c = await client()
    today = date.today().isoformat()

    # Record the chosen store (display name from the directory) for the read-model.
    store = get_store(req.store_id)
    repo.upsert_store(req.store_id, store["name"] if store else req.store_id, 21)

    if req.jpins:
        # UI multi-select: start exactly the chosen JPINs (validated against catalogue).
        jpins = [j for j in req.jpins if get_candidate(j) is not None]
    else:
        # Legacy demo seeding: first `count` catalogue candidates.
        cands = discover_candidates(req.store_id, req.count)
        if not req.include_rte:
            cands = [x for x in cands if not x.is_rte]
        jpins = [c.jpin for c in cands[: req.count]]

    started = []
    for jpin in jpins:
        wid = f"perish-markdown-{req.store_id}-{jpin}-{today}"
        await c.start_workflow(
            PerishableMarkdownWorkflow.run,
            args=[req.store_id, jpin, today, req.shadow_mode, req.demo_speed,
                  req.simulate, req.mock],
            id=wid,
            task_queue=TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
        started.append(wid)
    return {"started": started}


@app.post("/api/runs/{run_id}/decision")
async def decision(run_id: str, body: OwnerDecision) -> dict:
    await _signal(run_id, "owner_decision", body)
    return {"ok": True}


@app.post("/api/runs/{run_id}/override")
async def override(run_id: str, body: ManualOverride) -> dict:
    await _signal(run_id, "manual_override", body)
    return {"ok": True}


@app.post("/api/runs/{run_id}/grn")
async def grn(run_id: str, body: AdditionalGrn) -> dict:
    await _signal(run_id, "additional_grn", body)
    return {"ok": True}


@app.post("/api/runs/{run_id}/soldout")
async def soldout(run_id: str) -> dict:
    await _signal(run_id, "sold_out")
    return {"ok": True}


@app.post("/api/runs/{run_id}/simulate")
async def simulate(run_id: str, body: SimulateRequest) -> dict:
    await _signal(run_id, "simulate", body)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Dead-stock multi-day clearance (separate workflow + UI page). Steering reuses
# the generic signal endpoints above (/decision, /override, /soldout, /simulate,
# /standing-rule) since the dead-stock workflow shares those signal names.
# --------------------------------------------------------------------------- #
@app.get("/api/deadstock")
async def list_deadstock(store_id: str = DEFAULT_STORE_ID) -> dict:
    return {
        "candidates": repo.list_dead_stock_candidates(store_id),
        "runs": repo.list_dead_stock_runs(store_id),
    }


@app.get("/api/deadstock/runs/{run_id}")
async def get_deadstock_run(run_id: str) -> dict:
    row = repo.get_dead_stock_run(run_id)
    if row is None:
        raise HTTPException(404, "dead-stock run not found")
    try:
        handle = (await client()).get_workflow_handle(run_id)
        live = await handle.query(DeadStockClearanceWorkflow.current_state)
        if live is not None:
            row["live"] = live.model_dump()
    except (RPCError, Exception):
        row["live"] = None
    return row


@app.post("/api/deadstock/discover")
async def deadstock_discover(req: DeadStockDiscoverRequest) -> dict:
    c = await client()
    store = get_store(req.store_id)
    repo.upsert_store(req.store_id, store["name"] if store else req.store_id, 21)
    wid = f"deadstock-discovery-{req.store_id}"
    await c.start_workflow(
        DeadStockDiscoveryWorkflow.run,
        args=[req.store_id, req.auto_start, req.shadow_mode, req.demo_speed, req.mock],
        id=wid, task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )
    return {"started": wid}


@app.post("/api/deadstock/seed")
async def deadstock_seed(req: DeadStockSeedRequest) -> dict:
    """Manually start clearance runs for chosen dead-stock JPINs — human-gated
    (auto_apply off, standing rule 0 → every markdown asks for approval)."""
    c = await client()
    started = []
    for jpin in req.jpins:
        wid = f"deadstock-{req.store_id}-{jpin}"
        await c.start_workflow(
            DeadStockClearanceWorkflow.run,
            args=[req.store_id, jpin, 0, req.shadow_mode, req.demo_speed,
                  req.simulate, False, 0.0, req.mock],
            id=wid, task_queue=TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
        started.append(wid)
    return {"started": started}


@app.post("/api/runs/{run_id}/standing-rule")
async def set_standing_rule(run_id: str, body: StandingRuleRequest) -> dict:
    await _signal(run_id, "set_standing_rule", body)
    return {"ok": True}


@app.get("/api/stores/{store_id}/offers")
async def list_store_offers(store_id: str) -> list[dict]:
    return repo.list_outcomes_for_store(store_id)


async def _signal(run_id: str, name: str, *args) -> None:
    try:
        handle = (await client()).get_workflow_handle(run_id)
        await handle.signal(name, *args)
    except RPCError as e:
        raise HTTPException(404, f"run not running: {e}")
