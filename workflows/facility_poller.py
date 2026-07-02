"""FacilitySellThroughPoller — one durable workflow per facility per clearance day.

It owns the shared sell-through read-model: on a durable timer it batch-refreshes
the snapshot for all candidate JPINs at the facility, then every
`PerishableMarkdownWorkflow` for that facility reads the snapshot (via
`read_snapshot`) instead of scanning Bolt itself.

Design:
  * ONE poller per facility, not per batch — this is the whole point (collapses
    O(N*K) duplicated OUTWARDED scans to O(K) batched scans per tick).
  * The candidate JPIN set can change intraday (a new GRN adds a line); the
    `add_jpins` signal folds new JPINs into the polled set without a restart.
  * `continue_as_new` after a bounded number of ticks keeps history small over a
    ~13-hour day (Temporal best practice for long pollers).
  * Decisions do NOT live here — the poller only refreshes data. Every markdown
    decision stays in the per-batch workflow, so correlation/audit is preserved.

Determinism boundary: the Bolt read + DB write are in `poll_facility_snapshot`
(an activity). The workflow only sleeps, loops, and folds signals.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities.snapshot import poll_facility_snapshot
    from shared.models import AddJpinsRequest

_POLL = dict(
    start_to_close_timeout=timedelta(seconds=90),
    retry_policy=RetryPolicy(maximum_attempts=2),
)
_MAX_TICKS_BEFORE_CAN = 24   # continue-as-new after this many ticks


@workflow.defn
class FacilitySellThroughPoller:
    def __init__(self) -> None:
        self._jpins: set[str] = set()
        self._stop: bool = False

    @workflow.run
    async def run(
        self,
        facility_id: str,
        store_id: str,
        jpins: list[str],
        receipt_date: str,
        t0_ms: int,
        trailing_window_h: float = 1.5,
        poll_interval_s: float = 120.0,
        ticks_done: int = 0,
    ) -> str:
        self._jpins = set(jpins)
        ticks = 0

        while not self._stop and ticks < _MAX_TICKS_BEFORE_CAN:
            if self._jpins:
                await workflow.execute_activity(
                    poll_facility_snapshot,
                    args=[facility_id, store_id, sorted(self._jpins),
                          receipt_date, t0_ms, trailing_window_h],
                    **_POLL,
                )
            ticks += 1
            try:
                await workflow.wait_condition(
                    lambda: self._stop, timeout=timedelta(seconds=poll_interval_s)
                )
            except asyncio.TimeoutError:
                pass

        if self._stop:
            return f"poller stopped after {ticks_done + ticks} ticks"

        # Keep history bounded over the selling day.
        workflow.continue_as_new(args=[
            facility_id, store_id, sorted(self._jpins), receipt_date, t0_ms,
            trailing_window_h, poll_interval_s, ticks_done + ticks,
        ])

    @workflow.signal
    def add_jpins(self, req: AddJpinsRequest) -> None:
        self._jpins.update(req.jpins)

    @workflow.signal
    def stop(self) -> None:
        self._stop = True

    @workflow.query
    def polled_jpins(self) -> list[str]:
        return sorted(self._jpins)
