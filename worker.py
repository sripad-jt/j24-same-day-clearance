"""Temporal worker — registers the workflow + activities and polls the task queue."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.worker import Worker

from activities.feeds import capture_offer_baseline, measure_offer_outcome
from activities.persistence import persist_decision, persist_state, record_run_event
from activities.pipeline import (
    apply_price_goldeneye,
    fetch_sellthrough,
    notify_owner,
    plan_run,
    publish_offer,
    request_owner_approval,
    shape_offer_llm,
    write_audit,
)
from activities.deadstock import (
    discover_dead_stock,
    persist_deadstock_candidate,
    persist_deadstock_state,
    plan_deadstock_run,
    read_deadstock_stock,
    resolve_sku_meta,
)
from activities.profile import resolve_intraday_profile
from activities.snapshot import poll_facility_snapshot, read_snapshot
from db.database import init_db
from shared.config import TASK_QUEUE, get_client
from workflows.deadstock import DeadStockClearanceWorkflow
from workflows.deadstock_parent import DeadStockDiscoveryWorkflow
from workflows.facility_poller import FacilitySellThroughPoller
from workflows.markdown import PerishableMarkdownWorkflow

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    init_db()
    client = await get_client()
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[
                PerishableMarkdownWorkflow,
                FacilitySellThroughPoller,
                DeadStockClearanceWorkflow,
                DeadStockDiscoveryWorkflow,
            ],
            activities=[
                plan_run,
                fetch_sellthrough,
                request_owner_approval,
                shape_offer_llm,
                apply_price_goldeneye,
                publish_offer,
                write_audit,
                notify_owner,
                persist_state,
                record_run_event,
                persist_decision,
                capture_offer_baseline,
                measure_offer_outcome,
                resolve_intraday_profile,
                poll_facility_snapshot,
                read_snapshot,
                discover_dead_stock,
                plan_deadstock_run,
                read_deadstock_stock,
                resolve_sku_meta,
                persist_deadstock_state,
                persist_deadstock_candidate,
            ],
            activity_executor=executor,
        )
        logging.info("worker polling task queue %s", TASK_QUEUE)
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
