"""E2E: DeadStockClearanceWorkflow multi-day ramp under Temporal time-skipping.

Fakes only plan_deadstock_run (the sole activity needing live Bolt/parquet);
everything else runs for real against a temp SQLite DB. Proves the workflow steps
the price down day over day and clears to the floor at terminal (expiry).

Run standalone:  PYTHONPATH=. python tests/e2e/deadstock_clearance_e2e.py
Or via pytest:    RUN_E2E=1 pytest tests/test_workflows_e2e.py -q
Exits non-zero (assertion) on failure.
"""
import asyncio
import concurrent.futures
import os
import tempfile

_db = os.path.join(tempfile.mkdtemp(), "ds.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db}"
os.environ.pop("INVENTORY_SOURCE", None)

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from db.database import init_db
from db import repo
from pricing.ladder import default_config
from shared.config import TASK_QUEUE
from shared.models import DeadStockPlan
from workflows.deadstock import DeadStockClearanceWorkflow

# real activities except plan_deadstock_run
from activities.deadstock import (
    persist_deadstock_state, read_deadstock_stock, resolve_sku_meta,
    persist_deadstock_candidate,
)
from activities.persistence import persist_decision, record_run_event
from activities.pipeline import (
    apply_price_goldeneye, notify_owner, publish_offer, request_owner_approval,
    shape_offer_llm,
)

STORE, JPIN = "BZID-1304298141", "JPIN-DEAD-1"


@activity.defn(name="plan_deadstock_run")
async def fake_plan(store_id, jpin, days_unsold, shadow_mode, demo_speed):
    cfg = default_config(shadow_mode=shadow_mode, demo_speed=demo_speed)
    return DeadStockPlan(
        store_id=store_id, jpin=jpin, product_title="Paneer 200g (test)",
        category="dairy", shelf_life_days=12, days_since_received=1,
        days_unsold=days_unsold, on_hand=40, list_price=100.0, floor_price=20.0,
        mrp=110.0, config=cfg, eligible=True,
    )


async def main():
    init_db()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            async with Worker(
                env.client, task_queue=TASK_QUEUE,
                workflows=[DeadStockClearanceWorkflow],
                activities=[
                    fake_plan, read_deadstock_stock, persist_deadstock_state,
                    resolve_sku_meta, persist_deadstock_candidate,
                    persist_decision, record_run_event, apply_price_goldeneye,
                    notify_owner, publish_offer, request_owner_approval, shape_offer_llm,
                ],
                activity_executor=ex,
            ):
                wid = f"deadstock-{STORE}-{JPIN}"
                handle = await env.client.start_workflow(
                    DeadStockClearanceWorkflow.run,
                    # sim=True (no Bolt); auto_apply=True; standing 100 => no approval
                    args=[STORE, JPIN, 5, False, 2000.0, True, True, 100.0],
                    id=wid, task_queue=TASK_QUEUE,
                )
                for _ in range(50):
                    st = await handle.query(DeadStockClearanceWorkflow.current_state)
                    if st is not None:
                        print("init: sim=%s price=%s on_hand=%s dte=%s" % (
                            st.simulate, st.current_price, st.on_hand, st.days_to_expiry))
                        break
                    await asyncio.sleep(0.1)
                result = await handle.result()
                print("result:", result)

    row = repo.get_dead_stock_run(wid)
    prices = [(p["price_seq"], p["from_price"], p["to_price"]) for p in row["price_changes"]]
    print("status=%s current_price=%s dte=%s" % (
        row["status"], row["current_price"], row["days_to_expiry"]))
    print("price ramp:", prices)
    assert row["current_price"] <= 20.0 + 1e-9, row["current_price"]
    assert len(prices) >= 2, "expected a multi-day ramp"
    assert prices[0][1] == 100.0 and prices[-1][2] == 20.0, prices
    tos = [p[2] for p in prices]
    assert all(b <= a + 1e-9 for a, b in zip(tos, tos[1:])), tos
    print("PASS: dead-stock ramp walked ₹100 -> ₹20 over %d steps, status=%s" % (
        len(prices), row["status"]))


if __name__ == "__main__":
    asyncio.run(main())
