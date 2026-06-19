"""CLI: start one markdown run for a synthetic batch.

    python starter.py --store BTMLayout --jpin JPIN-PALAK-001 [--shadow] [--speed 1800]
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date

from temporalio.common import WorkflowIDReusePolicy

from shared.config import TASK_QUEUE, get_client
from workflows.markdown import PerishableMarkdownWorkflow


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="BTMLayout")
    ap.add_argument("--jpin", default="JPIN-PALAK-001")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--shadow", action="store_true")
    ap.add_argument("--speed", type=float, default=1800.0)
    args = ap.parse_args()

    client = await get_client()
    wid = f"perish-markdown-{args.store}-{args.jpin}-{args.date}"
    handle = await client.start_workflow(
        PerishableMarkdownWorkflow.run,
        args=[args.store, args.jpin, args.date, args.shadow, args.speed],
        id=wid,
        task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )
    print(f"started {handle.id}")


if __name__ == "__main__":
    asyncio.run(main())
