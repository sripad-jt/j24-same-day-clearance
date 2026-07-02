"""CLI: start the shared FacilitySellThroughPoller for a store's candidates.

    python start_poller.py --store BZID-1304298141 --speed 1800

One poller per facility per clearance day batch-refreshes the sell-through
snapshot that every markdown run for that facility reads (when
READ_FROM_SNAPSHOT=true). Uses the same pilot leafy-green catalogue as the sweep.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone

from temporalio.common import WorkflowIDReusePolicy

from adapters.catalog import discover_candidates
from shared.config import TASK_QUEUE, get_client
from shared.stores import get_store
from workflows.facility_poller import FacilitySellThroughPoller


def _t0_ms(receipt_date: str) -> int:
    dt = datetime.strptime(receipt_date, "%Y-%m-%d").replace(
        hour=2, minute=30, tzinfo=timezone.utc
    )  # 08:00 IST
    return int(dt.timestamp() * 1000)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="BZID-1304298141")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--interval", type=float, default=120.0, help="poll seconds (nominal)")
    ap.add_argument("--speed", type=float, default=1800.0)
    ap.add_argument("--limit", type=int, default=9)
    args = ap.parse_args()

    store = get_store(args.store) or {}
    facility_id = store.get("facility_id", "")
    if not facility_id:
        raise SystemExit(f"no facility_id for store {args.store}")

    jpins = [c.jpin for c in discover_candidates(args.store, args.limit)]
    poll_interval_s = max(1.0, args.interval / max(1.0, args.speed) * 60.0)

    client = await get_client()
    wid = f"perish-poller-{facility_id}-{args.date}"
    handle = await client.start_workflow(
        FacilitySellThroughPoller.run,
        args=[facility_id, args.store, jpins, args.date, _t0_ms(args.date),
              1.5, poll_interval_s, 0],
        id=wid,
        task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )
    print(f"started poller {handle.id} for {len(jpins)} JPINs @ facility {facility_id}")


if __name__ == "__main__":
    asyncio.run(main())
