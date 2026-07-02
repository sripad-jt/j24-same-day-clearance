"""DeadStockDiscoveryWorkflow — one per store; daily dead-stock discovery.

Each day it pulls the store's dead-stock list from posgateway, upserts a candidate
row per flagged JPIN (for the UI), and — when `auto_start` is on — ensures a
`DeadStockClearanceWorkflow` exists for each item (one durable run per store×jpin,
deduped by workflow id). With `auto_start` off it only discovers; a human starts
each clearance from the UI. Bounds history with `continue_as_new` each day.

Mirrors the j24-pulse DeadStockParent cadence but keeps every pricing decision in
the per-item clearance workflow (this one never decides prices).
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from activities.deadstock import (
        discover_dead_stock,
        persist_deadstock_candidate,
        resolve_sku_meta,
    )
    from activities.persistence import record_run_event
    from shared.models import ClearanceMode  # noqa: F401 (kept for parity)
    from workflows.deadstock import DeadStockClearanceWorkflow

_DISCOVER = dict(start_to_close_timeout=timedelta(seconds=300),
                 retry_policy=RetryPolicy(maximum_attempts=2))
_READ = dict(start_to_close_timeout=timedelta(seconds=120),
             retry_policy=RetryPolicy(maximum_attempts=2))
_DB = dict(start_to_close_timeout=timedelta(seconds=15),
           retry_policy=RetryPolicy(maximum_attempts=5))


@workflow.defn
class DeadStockDiscoveryWorkflow:
    def __init__(self) -> None:
        self._stop = False

    @workflow.run
    async def run(
        self,
        store_id: str,
        auto_start: bool = False,
        shadow_mode: bool = False,
        demo_speed: float = 1800.0,
        _day: int = 0,
    ) -> str:
        parent_run_id = f"deadstock-discovery-{store_id}"
        items = await workflow.execute_activity(
            discover_dead_stock, args=[store_id], **_DISCOVER,
        )
        await workflow.execute_activity(
            record_run_event,
            args=[parent_run_id, "DISCOVERED",
                  f"{len(items)} dead-stock items (day {_day}, "
                  f"{'auto-start' if auto_start else 'discover-only'})"],
            **_DB,
        )

        for it in items:
            meta = await workflow.execute_activity(
                resolve_sku_meta, args=[it.jpin], **_READ,
            )
            child_id = f"deadstock-{store_id}-{it.jpin}"
            status = "FLAGGED"
            run_id = ""
            if auto_start:
                try:
                    await workflow.start_child_workflow(
                        DeadStockClearanceWorkflow.run,
                        args=[store_id, it.jpin, it.days_unsold, shadow_mode, demo_speed,
                              False, True, 100.0],
                        id=child_id,
                        parent_close_policy=ParentClosePolicy.ABANDON,
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    )
                    status, run_id = "ACTIVE", child_id
                except Exception:  # noqa: BLE001 — already running: keep the existing run
                    status, run_id = "ACTIVE", child_id

            await workflow.execute_activity(
                persist_deadstock_candidate,
                args=[store_id, it.jpin, meta.get("product_title", it.jpin),
                      it.days_unsold, int(meta.get("shelf_life_days", 0) or 0), 0, 0,
                      it.rank, status, run_id],
                **_DB,
            )

        # Wait one nominal day, then continue-as-new (bounds history).
        day_seconds = max(1.0, 86400.0 / max(1.0, demo_speed))
        try:
            await workflow.wait_condition(lambda: self._stop,
                                          timeout=timedelta(seconds=day_seconds))
        except asyncio.TimeoutError:
            pass
        if self._stop:
            return f"stopped after {len(items)} items"
        workflow.continue_as_new(
            args=[store_id, auto_start, shadow_mode, demo_speed, _day + 1]
        )

    @workflow.signal
    def stop(self) -> None:
        self._stop = True
