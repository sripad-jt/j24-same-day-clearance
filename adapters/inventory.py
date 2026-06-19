"""Stage B — Inventory Item Details API shape + synthetic sell-through (design §4, §7).

Real impl calls POST .../space/product/details/for-state-status-facility:
  - active states (SELLABLE/FULFILMENT/INWARDED/UNDER_TRANSFER) -> Q0 (initialQty),
    on-hand (leftQty), received time, listingSellingPrice, expiry (via lotId)
  - OUTWARDED (createdTimeAfter within 2 days) -> units sold since T0

This stub synthesises a deterministic intraday sell-through curve per JPIN so the
projection in the decision engine actually moves: some lines clear on their own
(HOLD), others lag (STEP). A markdown gives a modest demand boost.
"""
from __future__ import annotations

import hashlib


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
