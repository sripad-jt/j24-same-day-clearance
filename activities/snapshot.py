"""Activities for the shared facility sell-through read-model (v3).

Why this exists: OUTWARDED is slow and un-indexed server-side (P0 #2), and inline
per-batch polling means the SAME expensive scan runs once per batch workflow. For
one facility with K candidate leafy JPINs and N batch workflows that is O(N*K)
duplicated slow scans per tick. The poller collapses it to O(K) *batched* scans
per tick, writes a snapshot, and every batch workflow reads the snapshot in ~1ms.

`poll_facility_snapshot` does the batched Bolt read (reusing the existing
`inventory.live_sold_snapshot`, which already fans out the calls in parallel) and
upserts one row per JPIN. `read_snapshot` is the fast path the batch workflow
calls instead of hitting Bolt directly; it returns a freshness flag so the batch
workflow can fall back to a direct read if the snapshot is stale.
"""
from __future__ import annotations

import time

from temporalio import activity

from adapters import inventory
from db import repo
from shared.models import SellThroughV2
from shared.stores import get_store


@activity.defn
async def poll_facility_snapshot(
    facility_id: str,
    store_id: str,
    jpins: list[str],
    receipt_date: str,
    t0_ms: int,
    trailing_window_h: float,
) -> int:
    """Batch-read sell-through for every candidate JPIN and upsert the snapshot.

    Returns the number of JPINs successfully refreshed. Never raises for a single
    JPIN: a failed read marks that row `stale=True` and keeps the last good value.
    """
    now_ms = int(time.time() * 1000)
    trail_start_ms = max(t0_ms, now_ms - int(trailing_window_h * 3600 * 1000))
    elapsed_h = max(0.5, (now_ms - t0_ms) / 3_600_000)
    window_h = max(0.01, (now_ms - trail_start_ms) / 3_600_000)

    # One batched call for on-hand/received + T0 sold; one for the trailing window.
    snap_t0 = await inventory.live_sold_snapshot(jpins, facility_id, t0_ms)
    snap_win = await inventory.live_sold_snapshot(jpins, facility_id, trail_start_ms)

    ok = 0
    for jpin in jpins:
        t0 = snap_t0.get(jpin, {})
        win = snap_win.get(jpin, {})
        sold_today = t0.get("sold_today")
        sold_window = win.get("sold_today")
        q0 = t0.get("received_today") or 0
        stale = sold_today is None

        if stale:
            # keep the last good snapshot; just flag it
            existing = repo.get_sell_through_snapshot(facility_id, jpin, receipt_date)
            if existing:
                repo.upsert_sell_through_snapshot(
                    facility_id=facility_id, store_id=store_id, jpin=jpin,
                    receipt_date=receipt_date, q0=existing["q0"],
                    q0_source=existing["q0_source"],
                    units_sold_today=existing["units_sold_today"],
                    recent_rate=existing["recent_rate"], window_h=existing["window_h"],
                    low_confidence=True, fetched_at_ms=existing["fetched_at_ms"],
                    stale=True,
                )
            continue

        recent_rate = ((sold_window / window_h) if sold_window is not None
                       else sold_today / elapsed_h)
        repo.upsert_sell_through_snapshot(
            facility_id=facility_id, store_id=store_id, jpin=jpin,
            receipt_date=receipt_date, q0=int(q0),
            q0_source="lot_initial_qty" if q0 else "none",
            units_sold_today=int(sold_today), recent_rate=round(recent_rate, 3),
            window_h=round(window_h, 2), low_confidence=sold_window is None,
            fetched_at_ms=now_ms, stale=False,
        )
        ok += 1

    activity.logger.info(
        "facility snapshot %s: %d/%d JPINs refreshed", facility_id, ok, len(jpins)
    )
    return ok


@activity.defn
async def read_snapshot(
    store_id: str,
    jpin: str,
    receipt_date: str,
    fallback_q0: int,
    max_age_s: float = 300.0,
) -> SellThroughV2:
    """Fast read from the shared snapshot for one batch workflow.

    Returns the snapshot as a `SellThroughV2`. If the row is missing or older than
    `max_age_s`, it falls back to a direct Bolt read so a batch never blocks on a
    lagging poller — belt and suspenders.
    """
    facility_id = (get_store(store_id) or {}).get("facility_id") or ""
    snap = repo.get_sell_through_snapshot(facility_id, jpin, receipt_date)
    now_ms = int(time.time() * 1000)
    fresh = (
        snap is not None
        and not snap["stale"]
        and (now_ms - snap["fetched_at_ms"]) <= max_age_s * 1000
    )
    if fresh:
        return SellThroughV2(
            units_sold_today=snap["units_sold_today"],
            recent_rate=snap["recent_rate"],
            q0=snap["q0"] or fallback_q0,
            q0_source=snap["q0_source"],
            window_h=snap["window_h"],
            low_confidence=snap["low_confidence"],
        )

    # Fallback: direct read (reuse the existing single-JPIN path semantics).
    from activities.pipeline import fetch_sellthrough

    t0_ms = _t0_ms(receipt_date)
    return await fetch_sellthrough(store_id, jpin, fallback_q0, t0_ms, 1.5, 0.0)


def _t0_ms(receipt_date: str) -> int:
    import datetime
    dt = datetime.datetime.strptime(receipt_date, "%Y-%m-%d").replace(
        hour=2, minute=30, tzinfo=datetime.timezone.utc
    )
    return int(dt.timestamp() * 1000)
