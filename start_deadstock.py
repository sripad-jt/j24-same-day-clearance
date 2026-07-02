"""CLI: start the per-store DeadStockDiscoveryWorkflow (daily dead-stock sweep).

    python start_deadstock.py --store BZID-1304298141                 # discover only
    python start_deadstock.py --store BZID-1304298141 --auto-start     # auto-clear too
    python start_deadstock.py --store BZID-1304298141 --speed 1800     # fast demo day

Discovers dead stock via posgateway each nominal day and (with --auto-start) fans
out one DeadStockClearanceWorkflow per flagged JPIN. Reuse-safe (ALLOW_DUPLICATE).
"""
from __future__ import annotations

import argparse
import asyncio

from temporalio.common import WorkflowIDReusePolicy

from shared.config import TASK_QUEUE, get_client
from workflows.deadstock_parent import DeadStockDiscoveryWorkflow


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="BZID-1304298141")
    ap.add_argument("--auto-start", action="store_true",
                    help="auto-start a clearance run per flagged JPIN")
    ap.add_argument("--shadow", action="store_true", help="record but never write prices")
    ap.add_argument("--speed", type=float, default=1800.0,
                    help="wall-clock compression for the nominal day")
    args = ap.parse_args()

    client = await get_client()
    wid = f"deadstock-discovery-{args.store}"
    await client.start_workflow(
        DeadStockDiscoveryWorkflow.run,
        args=[args.store, args.auto_start, args.shadow, args.speed],
        id=wid,
        task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )
    print(f"started {wid} (auto_start={args.auto_start}, shadow={args.shadow})")


if __name__ == "__main__":
    asyncio.run(main())
