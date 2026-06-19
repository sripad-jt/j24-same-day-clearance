"""FastAPI control plane for the React app — start, monitor, and steer runs.

Thin bridge: reads the Postgres read-model for lists/detail and the live Temporal
query for in-flight state; writes are Temporal signals (owner decision, override,
GRN, sold-out). The workflow remains the source of truth.
"""
from __future__ import annotations

from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError

from adapters.catalog import discover_candidates
from db import repo
from db.database import init_db
from pricing.ladder import default_config
from shared.config import TASK_QUEUE, get_client
from shared.models import (
    AdditionalGrn,
    ManualOverride,
    OwnerDecision,
    SeedRequest,
)
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
    repo.upsert_store("BTMLayout", "BTM Layout J24", 21)
    await client()


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
async def get_config() -> dict:
    return default_config().model_dump()


@app.get("/api/stores")
async def stores() -> list[dict]:
    return repo.list_stores()


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


@app.post("/api/runs/seed")
async def seed(req: SeedRequest) -> dict:
    c = await client()
    today = date.today().isoformat()
    cands = discover_candidates(req.store_id, req.count)
    if not req.include_rte:
        cands = [x for x in cands if not x.is_rte]
    started = []
    for cand in cands[: req.count]:
        wid = f"perish-markdown-{req.store_id}-{cand.jpin}-{today}"
        await c.start_workflow(
            PerishableMarkdownWorkflow.run,
            args=[req.store_id, cand.jpin, today, req.shadow_mode, req.demo_speed],
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


async def _signal(run_id: str, name: str, *args) -> None:
    try:
        handle = (await client()).get_workflow_handle(run_id)
        await handle.signal(name, *args)
    except RPCError as e:
        raise HTTPException(404, f"run not running: {e}")
