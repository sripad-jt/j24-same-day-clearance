"""Continuous decision engine v2 — demand-gated, not time-gated.

A PURE function: no I/O, no clock reads, no randomness. Given the inputs it
returns the target price, the decision, and full projection diagnostics. This
is what makes the price always explainable from logged inputs and the workflow
replay-safe.

Key differences from the v1 decide():
  1. Decision is driven by the projected end-of-day residual, not by which
     clock rung fired. Markdown depth is demand-gated.
  2. Uses demand elasticity to pick the SMALLEST discount whose projected lift
     clears the residual — never over-discounts when a gentler cut suffices.
  3. Every target is clamped to a price FLOOR (cost / salvage).
  4. Discount moves in small steps with a hysteresis band to prevent flapping.
  5. Price is monotonic non-increasing within the day.

Rungs no longer drive the decision; the workflow keeps them only as display
labels for the store screen and the offer headline.
"""
from __future__ import annotations

from shared.models import Decision, PriceDecisionV2, RungDef


def price_for_rung(rung: RungDef, list_price: float, token_free_price: float) -> float:
    """Resolve the shelf price at a rung label (used for forced overrides)."""
    if rung.token_free:
        return round(token_free_price, 2)
    return round(list_price * (1.0 - rung.ceiling_pct / 100.0), 2)


def _projected_clearance(units_sold: int, rate: float, remaining_h: float) -> float:
    return units_sold + max(0.0, rate) * max(0.0, remaining_h)


def _lift_factor(current_price: float, candidate_price: float, elasticity: float) -> float:
    """Expected demand multiplier moving current_price -> candidate_price.

    Elasticity of 0.6: a 10% cut from the current price lifts demand ~6%.
    Lift is clamped non-negative (a cut never reduces demand in this model).
    """
    if current_price <= 0 or candidate_price >= current_price:
        return 1.0
    rel_cut = (current_price - candidate_price) / current_price
    return 1.0 + max(0.0, elasticity) * rel_cut


def decide_v2(
    *,
    q0: int,
    units_sold: int,
    recent_rate: float,            # trailing units/hour (responsive), NOT cumulative
    remaining_h: float,            # hours to must-clear (close, or terminal day)
    current_price: float,
    list_price: float,
    floor_price: float,            # cost / salvage clamp — never price below this
    elasticity: float = 0.6,
    token_free_price: float = 1.0,
    residual_tolerance: float = 0.0,
    step_pct: float = 5.0,
    max_discount_pct: float = 60.0,
    hysteresis_units: float = 1.0,
    is_rte: bool = False,
    past_rte_gate: bool = False,
    token_eligible: bool = False,  # only true on the terminal day / at close
) -> PriceDecisionV2:
    """Decide whether to hold or cut, and to exactly what price.

    Search: from the current discount, walk discount in step_pct increments up
    to max_discount_pct, applying the elasticity lift to recent_rate, and take
    the FIRST (smallest) discount whose projected clearance covers q0 - tolerance.
    If nothing within the cap clears and the line is token-eligible, drop to
    the token price; otherwise take the deepest allowed price (floored).
    """
    q0 = max(0, q0)
    proj_at_current = _projected_clearance(units_sold, recent_rate, remaining_h)
    ratio = proj_at_current / q0 if q0 else 1.0
    residual_at_current = max(0.0, q0 - proj_at_current)

    # RTE terminal auto-clear — no approval needed
    if token_eligible and is_rte and past_rte_gate:
        price = round(token_free_price, 2)
        return PriceDecisionV2(
            decision=Decision.AUTO_CLEAR,
            target_price=price,
            discount_pct=round((1 - price / list_price) * 100, 1) if list_price else 100.0,
            reason=(
                f"RTE past close gate, {residual_at_current:.0f} of {q0} projected unsold "
                f"— auto-clear to token ₹{price:g}"
            ),
            residual_at_current=residual_at_current,
            residual_at_target=0.0,
            projected_clearance_at_target=float(q0),
            ratio=ratio,
            clears=True,
            floored=False,
            requires_approval=False,
        )

    # On track: hold
    if residual_at_current <= max(residual_tolerance, hysteresis_units):
        return PriceDecisionV2(
            decision=Decision.HOLD,
            target_price=round(current_price, 2),
            discount_pct=round((1 - current_price / list_price) * 100, 1) if list_price else 0.0,
            reason=(
                f"on track (proj {proj_at_current:.0f} of {q0}, ratio {ratio:.2f}) "
                f"— hold at ₹{current_price:g}"
            ),
            residual_at_current=residual_at_current,
            residual_at_target=residual_at_current,
            projected_clearance_at_target=proj_at_current,
            ratio=ratio,
            clears=True,
            floored=False,
            requires_approval=False,
        )

    # Behind: find the smallest discount that clears
    current_discount = (1 - current_price / list_price) * 100 if list_price else 0.0
    floor_discount = (1 - floor_price / list_price) * 100 if list_price else max_discount_pct
    hard_cap = min(max_discount_pct, floor_discount)

    best_price = round(max(floor_price, current_price), 2)
    best_proj = proj_at_current
    chosen = None

    d = current_discount + step_pct
    while d <= hard_cap + 1e-9:
        candidate = round(list_price * (1 - d / 100.0), 2)
        candidate = max(candidate, floor_price)
        if candidate >= current_price:            # monotonic: never step up
            d += step_pct
            continue
        proj = _projected_clearance(
            units_sold,
            recent_rate * _lift_factor(current_price, candidate, elasticity),
            remaining_h,
        )
        best_price, best_proj = candidate, proj
        if proj >= q0 - residual_tolerance:
            chosen = (candidate, proj)
            break
        d += step_pct

    if chosen is not None:
        price, proj = chosen
        residual_target = max(0.0, q0 - proj)
        return PriceDecisionV2(
            decision=Decision.STEP,
            target_price=price,
            discount_pct=round((1 - price / list_price) * 100, 1) if list_price else 0.0,
            reason=(
                f"behind (proj {proj_at_current:.0f}/{q0}, ratio {ratio:.2f}); "
                f"smallest cut to clear → ₹{price:g} "
                f"({round((1 - price / list_price) * 100):.0f}% off)"
            ),
            residual_at_current=residual_at_current,
            residual_at_target=residual_target,
            projected_clearance_at_target=proj,
            ratio=ratio,
            clears=True,
            floored=False,
            requires_approval=True,
        )

    # Nothing within the cap clears
    if token_eligible and past_rte_gate:
        price = round(token_free_price, 2)
        proj_token = _projected_clearance(
            units_sold,
            recent_rate * _lift_factor(current_price, price, elasticity),
            remaining_h,
        )
        return PriceDecisionV2(
            decision=Decision.AUTO_CLEAR if is_rte else Decision.STEP,
            target_price=price,
            discount_pct=round((1 - price / list_price) * 100, 1) if list_price else 100.0,
            reason=(
                f"cannot clear within {hard_cap:.0f}% cap (proj {best_proj:.0f}/{q0}); "
                f"terminal token ₹{price:g}"
            ),
            residual_at_current=residual_at_current,
            residual_at_target=max(0.0, q0 - proj_token),
            projected_clearance_at_target=proj_token,
            ratio=ratio,
            clears=True,
            floored=True,
            requires_approval=not is_rte,
        )

    residual_target = max(0.0, q0 - best_proj)
    return PriceDecisionV2(
        decision=Decision.STEP,
        target_price=best_price,
        discount_pct=round((1 - best_price / list_price) * 100, 1) if list_price else 0.0,
        reason=(
            f"behind and floor-bound (proj {best_proj:.0f}/{q0} at ₹{best_price:g}); "
            f"deepest allowed cut, {residual_target:.0f} still projected unsold"
        ),
        residual_at_current=residual_at_current,
        residual_at_target=residual_target,
        projected_clearance_at_target=best_proj,
        ratio=ratio,
        clears=False,
        floored=True,
        requires_approval=True,
    )


# =========================================================================== #
# v3 — projection-driven decide(). Identical policy to decide_v2 (floor, step
# search, hysteresis, RTE token) but the end-of-day projection is supplied by
# the profile-aware projector (pricing/projection.py) instead of a flat
# recent_rate * remaining_h. `remaining_demand` is the baseline expected units
# from now to close AT THE CURRENT PRICE; the elasticity lift scales it for each
# candidate cut. Still a PURE function: all shape/time inputs arrive as params.
# =========================================================================== #
def decide_v3(
    *,
    q0: int,
    units_sold: int,
    remaining_demand: float,       # baseline units now->close at current price (from projector)
    current_price: float,
    list_price: float,
    floor_price: float,
    elasticity: float = 0.6,
    token_free_price: float = 1.0,
    residual_tolerance: float = 0.0,
    step_pct: float = 5.0,
    max_discount_pct: float = 60.0,
    hysteresis_units: float = 1.0,
    is_rte: bool = False,
    past_rte_gate: bool = False,
    token_eligible: bool = False,
    projection_method: str = "",   # display only; carried into the reason
) -> PriceDecisionV2:
    q0 = max(0, q0)
    base_remaining = max(0.0, remaining_demand)
    proj_at_current = units_sold + base_remaining
    ratio = proj_at_current / q0 if q0 else 1.0
    residual_at_current = max(0.0, q0 - proj_at_current)
    tag = f" [{projection_method}]" if projection_method else ""

    # RTE terminal auto-clear — no approval needed
    if token_eligible and is_rte and past_rte_gate:
        price = round(token_free_price, 2)
        return PriceDecisionV2(
            decision=Decision.AUTO_CLEAR,
            target_price=price,
            discount_pct=round((1 - price / list_price) * 100, 1) if list_price else 100.0,
            reason=(
                f"RTE past close gate, {residual_at_current:.0f} of {q0} projected unsold "
                f"— auto-clear to token ₹{price:g}{tag}"
            ),
            residual_at_current=residual_at_current,
            residual_at_target=0.0,
            projected_clearance_at_target=float(q0),
            ratio=ratio, clears=True, floored=False, requires_approval=False,
        )

    # On track: hold
    if residual_at_current <= max(residual_tolerance, hysteresis_units):
        return PriceDecisionV2(
            decision=Decision.HOLD,
            target_price=round(current_price, 2),
            discount_pct=round((1 - current_price / list_price) * 100, 1) if list_price else 0.0,
            reason=(
                f"on track (proj {proj_at_current:.0f} of {q0}, ratio {ratio:.2f}) "
                f"— hold at ₹{current_price:g}{tag}"
            ),
            residual_at_current=residual_at_current,
            residual_at_target=residual_at_current,
            projected_clearance_at_target=proj_at_current,
            ratio=ratio, clears=True, floored=False, requires_approval=False,
        )

    # Behind: smallest discount whose lifted remaining-demand clears the residual
    current_discount = (1 - current_price / list_price) * 100 if list_price else 0.0
    floor_discount = (1 - floor_price / list_price) * 100 if list_price else max_discount_pct
    hard_cap = min(max_discount_pct, floor_discount)

    best_price = round(max(floor_price, current_price), 2)
    best_proj = proj_at_current
    chosen = None

    d = current_discount + step_pct
    while d <= hard_cap + 1e-9:
        candidate = round(list_price * (1 - d / 100.0), 2)
        candidate = max(candidate, floor_price)
        if candidate >= current_price:            # monotonic: never step up
            d += step_pct
            continue
        proj = units_sold + base_remaining * _lift_factor(current_price, candidate, elasticity)
        best_price, best_proj = candidate, proj
        if proj >= q0 - residual_tolerance:
            chosen = (candidate, proj)
            break
        d += step_pct

    if chosen is not None:
        price, proj = chosen
        return PriceDecisionV2(
            decision=Decision.STEP,
            target_price=price,
            discount_pct=round((1 - price / list_price) * 100, 1) if list_price else 0.0,
            reason=(
                f"behind (proj {proj_at_current:.0f}/{q0}, ratio {ratio:.2f}); "
                f"smallest cut to clear → ₹{price:g} "
                f"({round((1 - price / list_price) * 100):.0f}% off){tag}"
            ),
            residual_at_current=residual_at_current,
            residual_at_target=max(0.0, q0 - proj),
            projected_clearance_at_target=proj,
            ratio=ratio, clears=True, floored=False, requires_approval=True,
        )

    # Nothing within the cap clears
    if token_eligible and past_rte_gate:
        price = round(token_free_price, 2)
        proj_token = units_sold + base_remaining * _lift_factor(current_price, price, elasticity)
        return PriceDecisionV2(
            decision=Decision.AUTO_CLEAR if is_rte else Decision.STEP,
            target_price=price,
            discount_pct=round((1 - price / list_price) * 100, 1) if list_price else 100.0,
            reason=(
                f"cannot clear within {hard_cap:.0f}% cap (proj {best_proj:.0f}/{q0}); "
                f"terminal token ₹{price:g}{tag}"
            ),
            residual_at_current=residual_at_current,
            residual_at_target=max(0.0, q0 - proj_token),
            projected_clearance_at_target=proj_token,
            ratio=ratio, clears=True, floored=True, requires_approval=not is_rte,
        )

    return PriceDecisionV2(
        decision=Decision.STEP,
        target_price=best_price,
        discount_pct=round((1 - best_price / list_price) * 100, 1) if list_price else 0.0,
        reason=(
            f"behind and floor-bound (proj {best_proj:.0f}/{q0} at ₹{best_price:g}); "
            f"deepest allowed cut, {max(0.0, q0 - best_proj):.0f} still projected unsold{tag}"
        ),
        residual_at_current=residual_at_current,
        residual_at_target=max(0.0, q0 - best_proj),
        projected_clearance_at_target=best_proj,
        ratio=ratio, clears=False, floored=True, requires_approval=True,
    )
