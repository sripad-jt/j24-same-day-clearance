"""Stage B — Inventory Item Details API shape + synthetic sell-through (design §4, §7).

Real impl calls POST .../space/product/details/for-state-status-facility:
  - active states (SELLABLE/FULFILMENT/INWARDED/UNDER_TRANSFER) -> Q0 (initialQty),
    on-hand (leftQty), received time, listingSellingPrice, expiry (via lotId)
  - OUTWARDED (createdTimeAfter within 2 days) -> units sold since T0

The LIVE functions (`live_units_sold`, `live_sold_snapshot`) call the real gateway
via `adapters/_bolt`. They read sell-through as the COUNT of OUTWARDED movements
over a window (active leftQty doesn't decrement on sale). Best-effort: callers
fall back to the synthetic curve below on timeout. Gated by INVENTORY_SOURCE=live.

The synthetic stub below synthesises a deterministic intraday sell-through curve
per JPIN so the projection in the decision engine actually moves: some lines
clear on their own (HOLD), others lag (STEP). A markdown gives a demand boost.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from adapters import _bolt

log = logging.getLogger("inventory")


# --------------------------------------------------------------------------- #
# LIVE — real reads from the Inventory Item Details API
# --------------------------------------------------------------------------- #
def live_enabled() -> bool:
    return _bolt.configured()


async def live_listing_price(
    jpin: str, facility_id: str, timeout: float = 100.0
) -> float | None:
    """Real `listingSellingPrice` for a JPIN (the live markdown anchor).

    Pulled from the active-state details query (SELLABLE/etc.), which is fast and
    naturally bounded by `leftQty > 0` — unlike the OUTWARDED scan. Price is
    uniform across a JPIN's active rows, so we cap at one row. Returns None on
    timeout/error or when the listing has no selling price, so callers fall back
    to the catalogue placeholder.
    """
    try:
        rows = await _bolt.details(
            [jpin], facility_id, _bolt.ACTIVE_STATES, _bolt.ACTIVE_STATUSES,
            max_results=1, timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001 - best-effort live read
        log.warning("live_listing_price(%s) failed: %s", jpin, e)
        return None
    if not rows:
        return None
    price = rows[0].get("listingSellingPrice")
    return float(price) if price else None


async def live_units_sold(
    jpin: str, facility_id: str, since_ms: int, timeout: float = 100.0
) -> int | None:
    """Real units sold since `since_ms` (epoch ms) = sell-through over the window.

    Sales are recorded as OUTWARDED movements (active `leftQty` does NOT decrement
    on sale), so sell-through is the COUNT of OUTWARDED units, not a stock figure.
    Uses the lightweight count endpoint. Best-effort: returns None on timeout/error
    (the OUTWARDED scan is slow server-side and times out for high-volume sellers)
    so callers can fall back. `since_ms` must be >= now - 2 days (gateway rule).
    """
    try:
        data = await _bolt.counts(
            [jpin], facility_id, _bolt.OUTWARDED_STATES, _bolt.OUTWARDED_STATUSES,
            created_after_ms=since_ms, timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001 - best-effort live read
        log.warning("live_units_sold(%s) failed: %s", jpin, e)
        return None
    return int(data.get(jpin) or 0)


async def live_q0_from_lots(
    jpin: str, facility_id: str, t0_today_ms: int, timeout: float = 100.0
) -> int | None:
    """Sum of initialQty for today's inwarded lots — the live Q0 source (§5.1).

    Filters active-state rows by inventoryItemCreatedTime >= t0_today_ms and sums
    initialQty across matching rows. Returns None on timeout/error or when no
    matching rows are found (so callers fall back to the synthetic Q0).

    Response shape: each element is {inventoryItem: {jpin, initialQty, leftQty, ...},
    listingSellingPrice, inventoryItemCreatedTime} — initialQty is nested.
    """
    try:
        rows = await _bolt.details(
            [jpin], facility_id, _bolt.ACTIVE_STATES, _bolt.ACTIVE_STATUSES,
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("live_q0_from_lots(%s) failed: %s", jpin, e)
        return None
    total = sum(
        int((r.get("inventoryItem") or {}).get("initialQty") or 0)
        for r in rows
        if int(r.get("inventoryItemCreatedTime") or 0) >= t0_today_ms
    )
    return total if total > 0 else None


def t0_today_ms() -> int:
    """Epoch-ms for today's T0 = 05:00 IST = 23:30 UTC of the previous calendar day.

    Leafy greens arrive at the store from ~05:00 IST; any sales before that belong
    to the prior day's lot. If the current UTC time is before 23:30 UTC (i.e. before
    05:00 IST next day), we use today's 23:30 UTC — which is in the future, so we
    subtract one day to get the most recent 05:00 IST boundary.
    """
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    # 05:00 IST = 23:30 UTC previous calendar day
    t0 = now.replace(hour=23, minute=30, second=0, microsecond=0)
    if t0 > now:
        t0 -= datetime.timedelta(days=1)
    return int(t0.timestamp() * 1000)


async def live_active_batch(
    jpins: list[str], facility_id: str, t0_ms: int, timeout: float = 22.0
) -> dict[str, dict]:
    """Batched ACTIVE-state details → on_hand + received_today per JPIN.

    Single Bolt call for all JPINs. Returns:
      {jpin: {"on_hand": int|None, "received_today": int|None}}
    - on_hand        = sum of leftQty across all active rows
    - received_today = sum of initialQty for rows where inventoryItemCreatedTime >= t0_ms

    Response shape: each element is
      {inventoryItem: {jpin, initialQty, leftQty, ...}, inventoryItemCreatedTime, ...}
    jpin/initialQty/leftQty are nested under inventoryItem; createdTime is top-level.
    """
    try:
        rows = await _bolt.details(
            jpins, facility_id, _bolt.ACTIVE_STATES, _bolt.ACTIVE_STATUSES,
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("live_active_batch(%s…) failed: %s", jpins[:2], e)
        return {j: {"on_hand": None, "received_today": None} for j in jpins}

    on_hand: dict[str, int] = {}
    received: dict[str, int] = {}
    for r in rows:
        item = r.get("inventoryItem") or {}
        j = item.get("jpin") or ""
        if j not in jpins:
            continue
        on_hand[j] = on_hand.get(j, 0) + int(item.get("leftQty") or 0)
        if int(r.get("inventoryItemCreatedTime") or 0) >= t0_ms:
            received[j] = received.get(j, 0) + int(item.get("initialQty") or 0)

    return {
        j: {
            "on_hand": on_hand.get(j),
            "received_today": received.get(j),
        }
        for j in jpins
    }


async def live_sold_snapshot(
    jpins: list[str], facility_id: str, t0_ms: int, timeout: float = 22.0
) -> dict[str, dict]:
    """Full day-start snapshot per JPIN — two parallel Bolt calls.

    Returns {jpin: {"on_hand": int|None, "received_today": int|None,
                     "sold_today": int|None, "inventory_at_t0": int|None}}
    - inventory_at_t0 = on_hand + sold_today  (reconstructed stock at 05:00 IST)
    - received_today  = GRN lots inwarded since T0
    - sold_today      = OUTWARDED units since T0
    """
    active_task = live_active_batch(jpins, facility_id, t0_ms, timeout=timeout)
    sold_results = asyncio.gather(
        *(live_units_sold(j, facility_id, t0_ms, timeout) for j in jpins),
        return_exceptions=True,
    )
    active_map, sold_list = await asyncio.gather(active_task, sold_results)

    out: dict[str, dict] = {}
    for j, r in zip(jpins, sold_list):
        sold = None if isinstance(r, Exception) else r
        on_hand = active_map[j]["on_hand"]
        received = active_map[j]["received_today"]
        at_t0 = (on_hand + sold) if (on_hand is not None and sold is not None) else None
        out[j] = {
            "inventory_at_t0": at_t0,
            "received_today": received,
            "sold_today": sold,
        }
    return out


# --------------------------------------------------------------------------- #
# SYNTHETIC — deterministic fallback for demos/tests
# --------------------------------------------------------------------------- #


def _demand_strength(jpin: str) -> float:
    """Stable per-JPIN demand multiplier in ~[0.45, 1.25].

    < 1.0 means the line will fall short at list price (needs a markdown);
    >= 1.0 means it clears on its own and should be held.
    """
    h = int(hashlib.sha256(jpin.encode()).hexdigest(), 16)
    return 0.45 + (h % 80) / 100.0  # 0.45 .. 1.24


def units_sold(jpin: str, q0: int, elapsed_h: float, total_h: float,
               markdown_pct: float) -> int:
    """Cumulative units sold by `elapsed_h`, front-loaded, demand- and price-aware."""
    if total_h <= 0 or q0 <= 0:
        return 0
    frac_time = min(1.0, max(0.0, elapsed_h / total_h))
    # Mild front-loading: sales come a little faster early in the day.
    curve = frac_time ** 0.85
    boost = 1.0 + markdown_pct / 100.0 * 0.6   # 25% off -> +15% demand, etc.
    sold = q0 * _demand_strength(jpin) * curve * boost
    return int(min(q0, round(sold)))


def sell_through(jpin: str, q0: int, elapsed_h: float, total_h: float,
                 trailing_window_h: float, markdown_pct: float) -> tuple[int, float]:
    """Return (units_sold_since_T0, run_rate_units_per_hour) over a trailing window."""
    sold_now = units_sold(jpin, q0, elapsed_h, total_h, markdown_pct)
    prev_t = max(0.0, elapsed_h - trailing_window_h)
    sold_prev = units_sold(jpin, q0, prev_t, total_h, markdown_pct)
    window = max(0.5, elapsed_h - prev_t)
    rate = max(0.0, (sold_now - sold_prev) / window)
    if rate == 0.0 and elapsed_h > 0:           # fallback: all-day average
        rate = sold_now / elapsed_h
    return sold_now, rate
