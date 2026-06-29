"""Owner-education feed — pre/post sell-through of an offer.

When an offer is rolled out, the Giant (store owner) is shown — with the push
notification — a card that proves the offer worked: sell-through BEFORE vs AFTER,
extra units moved, and waste avoided. Two phases:

  - "interim": a measurement checkpoint ~measure_window_h after the markdown,
    so the owner sees the lift in near-real-time;
  - "final": end-of-day reconciliation against actual leftover (write-off avoided).

`build_outcome` is PURE (unit-testable and replay-safe). `push_offer_outcome_card`
is the only side effect — a stub that logs today; the real impl POSTs to
notification.prod.jumbotail.com (same channel as the approval card).
"""
from __future__ import annotations

import logging

from shared.models import OfferBaseline, OfferOutcome

log = logging.getLogger("feeds")


def build_outcome(
    *,
    baseline: OfferBaseline,
    units_sold_now: int,
    rate_after: float,
    window_h: float,
    ts_ist: str,
    phase: str = "interim",
    salvage_ref_price: float = 0.0,
) -> OfferOutcome:
    """Compute the pre/post lift card from a baseline snapshot + a later reading.

    incremental_units = max(0, rate_after - rate_before) * window_h
        → units attributable to the offer (counterfactual: had the pre-offer rate
          held, this many fewer would have sold in the window).
    waste_avoided = the incremental units, capped at what was actually left to lose.
    revenue_recovered = units actually sold in the window × the offer price.
    """
    rate_before = max(0.0, baseline.rate_before)
    units_sold_after = max(0, units_sold_now - baseline.units_sold_before)
    lift_units = max(0.0, rate_after - rate_before) * max(0.0, window_h)
    lift_pct = (
        (rate_after - rate_before) / rate_before * 100.0
    ) if rate_before > 0 else (100.0 if rate_after > 0 else 0.0)
    units_left = max(0, baseline.units_left_before - units_sold_after)
    waste_avoided = int(round(min(lift_units, baseline.units_left_before)))
    revenue_recovered = round(units_sold_after * baseline.to_price, 2)
    waste_avoided_value = round(waste_avoided * salvage_ref_price, 2)

    if rate_before <= 0 and rate_after > 0:
        headline = (
            f"{baseline.product_title}: sales had stalled before the "
            f"{baseline.discount_pct:.0f}% offer — since then you've moved "
            f"{units_sold_after} more, clearing stock that would've been wasted."
        )
    else:
        headline = (
            f"{baseline.product_title}: selling ~{rate_before:.1f}/hr before the "
            f"{baseline.discount_pct:.0f}% offer, ~{rate_after:.1f}/hr after "
            f"(+{lift_pct:.0f}%). ~{waste_avoided} units cleared that would've "
            f"been written off; ₹{revenue_recovered:g} recovered."
        )

    return OfferOutcome(
        run_id=baseline.run_id,
        store_id=baseline.store_id,
        jpin=baseline.jpin,
        product_title=baseline.product_title,
        rung=baseline.rung,
        price=baseline.to_price,
        discount_pct=baseline.discount_pct,
        ts_ist=ts_ist,
        phase=phase,
        rate_before=round(rate_before, 2),
        rate_after=round(rate_after, 2),
        lift_pct=round(lift_pct, 1),
        units_sold_after=units_sold_after,
        incremental_units=round(lift_units, 1),
        units_left=units_left,
        revenue_recovered=revenue_recovered,
        waste_avoided_units=waste_avoided,
        waste_avoided_value=waste_avoided_value,
        headline=headline,
    )


def push_offer_outcome_card(outcome: OfferOutcome) -> None:
    """Push the education card to the Giant. STUB — logs only.

    Real impl: POST to notification.prod.jumbotail.com (card payload mirrors the
    approval card route) AND mirror onto the POS second screen / My J24 'My Offers'
    feed so the owner can browse pre/post performance per product over time.
    """
    log.info(
        "My J24 offer-outcome [%s] %s %s: before %.1f/h → after %.1f/h (+%.0f%%), "
        "%d cleared, ₹%g recovered",
        outcome.phase, outcome.store_id, outcome.product_title,
        outcome.rate_before, outcome.rate_after, outcome.lift_pct,
        outcome.waste_avoided_units, outcome.revenue_recovered,
    )
