"""Shelf-life scheduler — once-per-day mode decision for multi-day batches.

The intraday engine (decide_v2) handles ONE day. This scheduler decides,
once per day per batch, whether a line should be in same-day clearance, and
for L>1 lines builds a multi-day discount ramp keyed off days-to-expiry.

It is PURE: given the batch's shelf-life facts and a daily velocity estimate
it returns a plan. For L=1 it collapses to CLEAR_SAMEDAY on the receipt date.

Slow movers are a planning problem before a markdown problem: when daily velocity
is far below what's needed to clear across the remaining shelf life, the right
first lever is to stop reordering (REDUCE_OTB / STOP_REORDER), not to discount.
That signal is emitted alongside the markdown recommendation.
"""
from __future__ import annotations

import math

from shared.models import ClearanceMode, ReorderAction, ShelfLifePlan


def plan_clearance(
    *,
    shelf_life_days: int,
    days_to_expiry: int,               # whole days from today to use-by (0 = expires today)
    remaining_units: int,
    daily_rate: float,                 # units/day, trailing multi-day average
    # tuning
    min_window_days: int = 1,
    max_window_days: int = 5,
    nudge_discount_pct: float = 10.0,
    max_multiday_discount_pct: float = 50.0,
    slow_mover_days_to_clear: float = 14.0,
) -> ShelfLifePlan:
    """Decide today's clearance mode + discount for a (possibly multi-day) batch.

    projected_days_to_clear = remaining / daily_rate
    clearance_window N      = clamp(ceil(projected_days_to_clear), [min,max])

    Modes:
      days_to_expiry <= 1            → CLEAR_SAMEDAY (hand to intraday engine)
      projected clears, far from N   → HOLD
      days_to_expiry == N+1          → NUDGE (gentle early discount)
      days_to_expiry <= N            → CLEAR_MULTIDAY (depth scales with urgency)
      won't clear across shelf life  → + REDUCE_OTB / STOP_REORDER signal
    """
    rate = max(daily_rate, 1e-6)
    projected_days_to_clear = remaining_units / rate

    window = int(min(max_window_days, max(min_window_days, math.ceil(projected_days_to_clear))))

    reorder = ReorderAction.NONE
    if projected_days_to_clear > slow_mover_days_to_clear or projected_days_to_clear > shelf_life_days * 2:
        reorder = ReorderAction.STOP_REORDER
    elif projected_days_to_clear > shelf_life_days:
        reorder = ReorderAction.REDUCE_OTB

    # Terminal day: same-day engine takes over
    if days_to_expiry <= 1:
        return ShelfLifePlan(
            mode=ClearanceMode.CLEAR_SAMEDAY,
            recommended_discount_pct=0.0,
            is_terminal_day=True,
            projected_days_to_clear=projected_days_to_clear,
            days_to_expiry=days_to_expiry,
            clearance_window_days=window,
            reorder_action=reorder,
            reason=(
                f"terminal day (expires within 1d), {remaining_units} on hand at "
                f"{rate:.1f}/day — hand to intraday clearance engine"
            ),
        )

    # On track across remaining runway: hold
    if projected_days_to_clear <= days_to_expiry and days_to_expiry > window + 1:
        return ShelfLifePlan(
            mode=ClearanceMode.HOLD,
            recommended_discount_pct=0.0,
            is_terminal_day=False,
            projected_days_to_clear=projected_days_to_clear,
            days_to_expiry=days_to_expiry,
            clearance_window_days=window,
            reorder_action=reorder,
            reason=(
                f"clears in {projected_days_to_clear:.1f}d, {days_to_expiry}d to expiry "
                f"— no markdown needed"
            ),
        )

    # Early nudge: one day before the window opens
    if days_to_expiry == window + 1:
        return ShelfLifePlan(
            mode=ClearanceMode.NUDGE,
            recommended_discount_pct=nudge_discount_pct,
            is_terminal_day=False,
            projected_days_to_clear=projected_days_to_clear,
            days_to_expiry=days_to_expiry,
            clearance_window_days=window,
            reorder_action=reorder,
            reason=(
                f"{days_to_expiry}d to expiry, {projected_days_to_clear:.1f}d to clear "
                f"— gentle {nudge_discount_pct:.0f}% nudge to get ahead"
            ),
        )

    # Inside the clearance window: escalating multi-day discount
    urgency = max(0.0, min(1.0, 1.0 - (days_to_expiry - 1) / max(1, window)))
    depth = round(nudge_discount_pct + urgency * (max_multiday_discount_pct - nudge_discount_pct), 1)
    mode = ClearanceMode.SUPPRESS_REORDER if reorder == ReorderAction.STOP_REORDER else ClearanceMode.CLEAR_MULTIDAY
    return ShelfLifePlan(
        mode=mode,
        recommended_discount_pct=depth,
        is_terminal_day=False,
        projected_days_to_clear=projected_days_to_clear,
        days_to_expiry=days_to_expiry,
        clearance_window_days=window,
        reorder_action=reorder,
        reason=(
            f"in clearance window ({days_to_expiry}d to expiry, window {window}d); "
            f"urgency {urgency:.2f} → {depth:.0f}% off"
            + (f"; {reorder.value} (won't clear across shelf life)" if reorder != ReorderAction.NONE else "")
        ),
    )
