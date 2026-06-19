"""CLI: create/delete the morning-sweep Temporal Schedule (design §4, §9).

The schedule fans out one markdown run per perishable candidate each morning.
For the demo it starts a run per candidate in the pilot catalogue.

    python schedule.py --store BTMLayout          # create daily sweep
    python schedule.py --store BTMLayout --delete  # remove it
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta

from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
)
from temporalio.common import WorkflowIDReusePolicy

from adapters.catalog import discover_candidates
from shared.config import TASK_QUEUE, get_client
from workflows.markdown import PerishableMarkdownWorkflow


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="BTMLayout")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--speed", type=float, default=1800.0)
    args = ap.parse_args()

    client = await get_client()
    sid = f"perish-sweep-{args.store}"

    if args.delete:
        await client.get_schedule_handle(sid).delete()
        print(f"deleted schedule {sid}")
        return

    # One schedule per first candidate as the representative sweep action.
    # (A production sweep activity would enumerate candidates and signal-with-start
    # each one; Schedules take a single action, so the real impl uses a starter
    # workflow that fans out. Here we register the leading candidate.)
    cand = discover_candidates(args.store, 1)[0]
    today = date.today().isoformat()
    await client.create_schedule(
        sid,
        Schedule(
            action=ScheduleActionStartWorkflow(
                PerishableMarkdownWorkflow.run,
                args=[args.store, cand.jpin, today, False, args.speed],
                id=f"perish-markdown-{args.store}-{cand.jpin}-{today}",
                task_queue=TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            ),
            spec=ScheduleSpec(
                intervals=[ScheduleIntervalSpec(every=timedelta(days=1))]
            ),
        ),
    )
    print(f"created schedule {sid}")


if __name__ == "__main__":
    asyncio.run(main())
