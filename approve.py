"""CLI: send an owner decision to a waiting run.

    python approve.py --workflow <id> --rung R1 --approve
    python approve.py --workflow <id> --rung R1 --reject
"""
from __future__ import annotations

import argparse
import asyncio

from shared.config import get_client
from shared.models import OwnerDecision


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--rung", default="")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", action="store_true")
    args = ap.parse_args()

    client = await get_client()
    handle = client.get_workflow_handle(args.workflow)
    await handle.signal(
        "owner_decision",
        OwnerDecision(rung=args.rung, approve=bool(args.approve)),
    )
    print(f"signalled {args.workflow}: {'approve' if args.approve else 'reject'}")


if __name__ == "__main__":
    asyncio.run(main())
