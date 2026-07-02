"""Dead-stock decision — PURE, multi-day, shelf-life aware.

The intraday engine (decide_v2/v3) clears ONE day. Dead / slow-moving stock needs
a multi-day markdown ramp keyed to remaining shelf life. We do NOT reinvent that
ramp — `pricing/shelf_life_scheduler.plan_clearance` already turns
`(shelf_life_days, days_to_expiry, remaining_units, daily_rate)` into a mode +
escalating discount + reorder signal. This module supplies the two derived inputs
`plan_clearance` needs and turns its recommended discount into an actionable,
floor-clamped, monotonic price.

Everything here is pure: no I/O, no clock, no randomness. The activity supplies the
facts (on-hand, days-unsold, shelf life, days-since-received, list price); the
workflow calls these and passes the result to the ledger. Same inputs → same price.

Remaining-runway assumption (from the pilot): a dead-stock lot was typically
**received at half its shelf life already gone**, and we know the last-sale date
since. So today's remaining shelf life is approximated as
`round(shelf_life_days/2) - days_since_received`, and `days_to_expiry` is that,
floored at 0.
"""
from __future__ import annotations

from dataclasses import dataclass

from pricing.shelf_life_scheduler import plan_clearance
from shared.models import DeadStockDecision


@dataclass
class DeadStockContext:
    """Derived inputs for plan_clearance + diagnostics — pure."""

    days_to_expiry: int
    remaining_shelf_life_days: int
    daily_rate: float


def deadstock_context(
    *,
    shelf_life_days: int,
    days_since_received: int,
    days_unsold: int,
    on_hand: int,
    observed_daily_rate: float | None = None,
) -> DeadStockContext:
    """Estimate remaining runway + velocity for a dead-stock line.

    remaining_shelf_life = round(shelf_life_days/2) - days_since_received  (half-life
    assumption), floored at 0. daily_rate uses the observed rate when supplied, else
    it is inferred from `days_unsold`: an item unsold for a while is treated as ~0/day
    so plan_clearance escalates the markdown (and flags reorder). A brand-new laggard
    (days_unsold 0) with stock on hand gets a tiny nominal rate so the ramp still
    engages rather than dividing by zero.
    """
    half = round(max(0, shelf_life_days) / 2.0)
    remaining = max(0, int(half) - max(0, days_since_received))

    if observed_daily_rate is not None:
        rate = max(0.0, float(observed_daily_rate))
    elif days_unsold >= 3 or on_hand <= 0:
        rate = 0.0                       # genuinely not moving
    else:
        rate = 0.0                       # default dead-stock assumption: not selling
    return DeadStockContext(
        days_to_expiry=remaining,
        remaining_shelf_life_days=remaining,
        daily_rate=rate,
    )


def decide_deadstock(
    *,
    on_hand: int,
    days_unsold: int,
    shelf_life_days: int,
    days_since_received: int,
    list_price: float,
    floor_price: float,
    current_price: float,
    observed_daily_rate: float | None = None,
    nudge_discount_pct: float = 10.0,
    max_multiday_discount_pct: float = 50.0,
) -> DeadStockDecision:
    """Decide today's dead-stock price. Pure. Monotonic non-increasing, floor-clamped.

    Delegates the mode + discount to plan_clearance; converts the recommended
    discount to a price, never above the current price, never below the floor.
    """
    ctx = deadstock_context(
        shelf_life_days=shelf_life_days,
        days_since_received=days_since_received,
        days_unsold=days_unsold,
        on_hand=on_hand,
        observed_daily_rate=observed_daily_rate,
    )
    plan = plan_clearance(
        shelf_life_days=max(1, shelf_life_days),
        days_to_expiry=ctx.days_to_expiry,
        remaining_units=max(0, on_hand),
        daily_rate=ctx.daily_rate,
        nudge_discount_pct=nudge_discount_pct,
        max_multiday_discount_pct=max_multiday_discount_pct,
    )

    # Terminal day: plan_clearance defers to the intraday engine (CLEAR_SAMEDAY, 0%),
    # but a standalone dead-stock run has no intraday sub-engine — clear to the floor.
    if plan.is_terminal_day or ctx.days_to_expiry <= 1:
        target = round(max(floor_price, 0.0), 2)
        target = min(target, round(current_price, 2)) if current_price else target
        eff_discount = round((1 - target / list_price) * 100, 1) if list_price else 100.0
        return DeadStockDecision(
            mode=plan.mode.value,
            target_price=target,
            discount_pct=eff_discount,
            days_to_expiry=ctx.days_to_expiry,
            remaining_shelf_life_days=ctx.remaining_shelf_life_days,
            projected_days_to_clear=round(plan.projected_days_to_clear, 2),
            reorder_action=plan.reorder_action.value,
            clears=True,
            requires_approval=True,
            reason=(f"terminal day ({ctx.days_to_expiry}d to expiry), {on_hand} on hand "
                    f"— clear to floor ₹{target:g}"),
        )

    discount = max(0.0, plan.recommended_discount_pct)
    raw_price = list_price * (1 - discount / 100.0) if list_price else current_price
    target = round(max(floor_price, raw_price), 2)
    # monotonic: never step the price up within the clearance
    target = min(target, round(current_price, 2)) if current_price else target
    floored = target <= floor_price + 1e-9 and discount > 0
    eff_discount = round((1 - target / list_price) * 100, 1) if list_price else discount

    return DeadStockDecision(
        mode=plan.mode.value,
        target_price=target,
        discount_pct=eff_discount,
        days_to_expiry=ctx.days_to_expiry,
        remaining_shelf_life_days=ctx.remaining_shelf_life_days,
        projected_days_to_clear=round(plan.projected_days_to_clear, 2),
        reorder_action=plan.reorder_action.value,
        clears=not floored,
        requires_approval=discount > 0,        # any markdown asks; workflow may auto-approve
        reason=plan.reason + (f" → ₹{target:g} ({eff_discount:.0f}% off)" if discount > 0
                              else " → hold at list"),
    )
