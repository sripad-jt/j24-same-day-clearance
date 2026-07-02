"""E2E: sim-mode same-day run driven by simulate signals, under Temporal time-skipping.

Fakes only plan_run (the sole activity needing live Bolt); everything else runs
for real against a temp SQLite DB. Proves: sim mode skips Bolt, the operator's
simulate signal drives units_sold, and the v3 ladder actually walks (price steps).

Run standalone:  PYTHONPATH=. python tests/e2e/sim_clearance_e2e.py
Or via pytest:    RUN_E2E=1 pytest tests/test_workflows_e2e.py -q
Exits non-zero (assertion) on failure.
"""
import asyncio
import concurrent.futures
import os
import tempfile

_db = os.path.join(tempfile.mkdtemp(), "sim.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db}"
os.environ["PROJECTION_MODE"] = "v3"
os.environ.pop("INVENTORY_SOURCE", None)  # sim mode won't call Bolt anyway

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from db.database import init_db
from shared.config import TASK_QUEUE
from shared.models import ReceiptContext, RunPlan, SimulateRequest
from pricing.ladder import default_config
from workflows.markdown import PerishableMarkdownWorkflow

# real activities (we override plan_run only)
from activities.feeds import capture_offer_baseline, measure_offer_outcome
from activities.persistence import persist_decision, persist_state, record_run_event
from activities.pipeline import (
    apply_price_goldeneye, fetch_sellthrough, notify_owner, publish_offer,
    request_owner_approval, shape_offer_llm, write_audit,
)
from activities.profile import resolve_intraday_profile
from activities.snapshot import poll_facility_snapshot, read_snapshot
from db import repo

STORE = "BZID-1304298141"
JPIN = "JPIN-TEST-1"
DATE = "2026-07-01"


@activity.defn(name="plan_run")
async def fake_plan_run(store_id, jpin, receipt_date, shadow_mode, demo_speed, mock_gateway=False):
    cfg = default_config(demo_speed=demo_speed, projection_mode="v3")
    receipt = ReceiptContext(
        store_id=store_id, jpin=jpin, receipt_date=receipt_date,
        product_title="Coriander (test)", category="leafy", is_rte=False,
        shelf_life_days=1, q0=60, q0_source="lot_initial_qty",
        list_price=40.0, mrp=50.0, received_epoch_ms=0, expiry_date=receipt_date,
    )
    total_h = float(cfg.store_close_hour - 8)
    return RunPlan(receipt=receipt, config=cfg,
                   close_offset_s=total_h * 3600.0 / cfg.demo_speed,
                   floor_price=5.0, eligible=True)


async def main():
    init_db()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            async with Worker(
                env.client, task_queue=TASK_QUEUE,
                workflows=[PerishableMarkdownWorkflow],
                activities=[
                    fake_plan_run, fetch_sellthrough, request_owner_approval,
                    shape_offer_llm, apply_price_goldeneye, publish_offer,
                    write_audit, notify_owner, persist_state, record_run_event,
                    persist_decision, capture_offer_baseline, measure_offer_outcome,
                    resolve_intraday_profile, poll_facility_snapshot, read_snapshot,
                ],
                activity_executor=ex,
            ):
                wid = f"perish-markdown-{STORE}-{JPIN}-{DATE}"
                handle = await env.client.start_workflow(
                    PerishableMarkdownWorkflow.run,
                    args=[STORE, JPIN, DATE, False, 1800.0, True],  # simulate=True
                    id=wid, task_queue=TASK_QUEUE,
                )
                sim_seen = False
                for _ in range(50):
                    st = await handle.query(PerishableMarkdownWorkflow.current_state)
                    if st is not None:
                        sim_seen = st.simulate
                        print("init state: simulate=%s price=%s q0=%s" % (
                            st.simulate, st.current_price, st.q0))
                        break
                    await asyncio.sleep(0.1)
                await handle.signal(PerishableMarkdownWorkflow.simulate,
                                    SimulateRequest(units_sold=1, recent_rate=0.5))
                result = await handle.result()
                print("workflow result:", result)

    row = repo.get_run(wid)
    price_changes = row["price_changes"]
    print("final: status=%s current_price=%s units_sold=%s" % (
        row["status"], row["current_price"], row["units_sold"]))

    assert sim_seen is True, "run should report simulate=True"
    assert row["units_sold"] <= 1, f"sim sell-through unexpectedly high: {row['units_sold']}"
    assert row["current_price"] < 40.0, f"ladder did not walk: price={row['current_price']}"
    assert price_changes, "expected at least one applied price change"
    print("PASS: sim mode drove sell-through and the v3 ladder walked "
          "(₹40 -> ₹%s)" % row["current_price"])


if __name__ == "__main__":
    asyncio.run(main())
