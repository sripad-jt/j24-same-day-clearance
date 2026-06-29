"""Activities for the owner-education feed (pre/post sell-through of an offer).

Flow in the workflow:
  - The instant a markdown is APPLIED, call `capture_offer_baseline` (records the
    'before' velocity).
  - Schedule a measurement checkpoint `measure_window_h` later (a durable timer)
    and at close; at each, call `measure_offer_outcome` which reads the post-offer
    sell-through, builds the pre/post card, persists it, and pushes it to the Giant.
"""
from __future__ import annotations

from temporalio import activity

from adapters import feeds, inventory
from db import repo
from shared.models import OfferBaseline, OfferOutcome


@activity.defn
async def capture_offer_baseline(baseline: OfferBaseline) -> None:
    repo.add_offer_baseline(baseline)


@activity.defn
async def measure_offer_outcome(
    baseline: OfferBaseline,
    store_id: str,
    jpin: str,
    facility_id: str,
    t0_ms: int,
    window_h: float,
    ts_ist: str,
    phase: str,
    salvage_ref_price: float,
) -> OfferOutcome:
    """Read post-offer sell-through, build the pre/post card, persist + push."""
    units_now = None
    rate_after = baseline.rate_before

    if inventory.live_enabled() and facility_id:
        units_now = await inventory.live_units_sold(jpin, facility_id, t0_ms, timeout=12.0)

    if units_now is None:
        # Live read timed out — use baseline as best-effort
        units_now = baseline.units_sold_before
    else:
        sold_in_window = max(0, units_now - baseline.units_sold_before)
        rate_after = sold_in_window / max(window_h, 0.5)

    outcome = feeds.build_outcome(
        baseline=baseline,
        units_sold_now=units_now,
        rate_after=rate_after,
        window_h=window_h,
        ts_ist=ts_ist,
        phase=phase,
        salvage_ref_price=salvage_ref_price,
    )
    repo.add_offer_outcome(outcome)
    feeds.push_offer_outcome_card(outcome)
    return outcome
