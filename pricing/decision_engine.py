"""The deterministic decision engine (design §8).

A PURE function: no I/O, no clock reads, no randomness. Given the inputs it
returns the target rung, the decision, and a one-line reason. This is what makes
the price always explainable from logged inputs and the workflow replay-safe.
"""
from __future__ import annotations

from shared.models import Decision, DecisionResult, RungDef


def price_for_rung(rung: RungDef, list_price: float, token_free_price: float) -> float:
    """Resolve the shelf price at a rung. Token-free rung -> ₹1, else markdown %."""
    if rung.token_free:
        return round(token_free_price, 2)
    return round(list_price * (1.0 - rung.ceiling_pct / 100.0), 2)


def decide(
    *,
    q0: int,
    units_sold: int,
    run_rate: float,
    nominal_remaining_h: float,
    current_rung_index: int,
    ceiling_rung_index: int,
    rungs: list[RungDef],
    list_price: float,
    token_free_price: float,
    theta_hold: float,
    is_rte: bool,
    past_rte_gate: bool,
) -> DecisionResult:
    """Decide whether to hold or step the price down at this checkpoint.

    Sell-through modulation:
        proj  = sold + run_rate * hours_remaining
        ratio = proj / q0
        ratio >= 1.0        -> HOLD (on track to clear)
        ratio >= theta_hold -> step one rung toward the ceiling
        else                -> take the ceiling (lagging badly)
    Price is monotonic non-increasing within the day: target is never below the
    current rung. The ceiling rung's token-free flag (R3) drives AUTO_CLEAR for RTE.
    """
    proj = units_sold + run_rate * max(0.0, nominal_remaining_h)
    residual = max(0.0, float(q0) - proj)
    ratio = proj / q0 if q0 > 0 else 1.0

    ceiling_rung = rungs[ceiling_rung_index]
    is_token_ceiling = ceiling_rung.token_free

    # RTE past the close gate at the token rung auto-clears without consent.
    if is_token_ceiling and is_rte and past_rte_gate:
        target = ceiling_rung_index
        price = price_for_rung(ceiling_rung, list_price, token_free_price)
        return DecisionResult(
            target_rung_index=target,
            decision=Decision.AUTO_CLEAR,
            reason=(
                f"RTE past close gate with {residual:.0f} of {q0} unsold "
                f"(proj {proj:.0f}) — auto-clear to token ₹{price:g}"
            ),
            ratio=ratio,
            projected_clearance=proj,
            residual=residual,
            target_price=price,
            requires_approval=False,
        )

    if ratio >= 1.0:
        target = current_rung_index
        reason = (
            f"on track to clear (proj {proj:.0f} ≥ {q0} on hand) — hold at "
            f"{rungs[current_rung_index].label}"
        )
        decision = Decision.HOLD
    elif ratio >= theta_hold:
        target = min(current_rung_index + 1, ceiling_rung_index)
        decision = Decision.STEP if target > current_rung_index else Decision.HOLD
        reason = (
            f"slightly short (proj {proj:.0f}/{q0}, ratio {ratio:.2f}) — "
            f"step to {rungs[target].label}"
        )
    else:
        target = ceiling_rung_index
        decision = Decision.STEP if target > current_rung_index else Decision.HOLD
        reason = (
            f"lagging badly (proj {proj:.0f}/{q0}, ratio {ratio:.2f}) — "
            f"take ceiling {rungs[target].label}"
        )

    # Monotonic: never step the price back up within the day.
    if target < current_rung_index:
        target = current_rung_index
        decision = Decision.HOLD

    target_rung = rungs[target]
    price = price_for_rung(target_rung, list_price, token_free_price)

    if decision == Decision.HOLD:
        requires_approval = False
    elif target_rung.token_free:
        # Token rung for non-RTE still needs consent (RTE handled above).
        requires_approval = True
    else:
        requires_approval = True

    return DecisionResult(
        target_rung_index=target,
        decision=decision,
        reason=reason,
        ratio=ratio,
        projected_clearance=proj,
        residual=residual,
        target_price=price,
        requires_approval=requires_approval,
    )
