"""Intraday demand projection — PURE, profile-aware, replay-safe.

This is the modeling fix at the heart of v3. The v2 decision engine projected
end-of-day clearance with a *flat* extrapolation:

    proj = units_sold + recent_rate * remaining_h        # assumes rate is constant

For leafy greens that is wrong in a way that costs margin: vegetable demand is
strongly evening-peaked (people buy on the way home). A flat extrapolation using
the sleepy 3-4pm rate *understates* the demand still to come, so the agent marks
down right before the rush that would have cleared the stock at list price.

`project_remaining_demand` blends two estimators of the units that will sell from
now to close, and lets the caller (decision_engine) scale that baseline by the
elasticity lift of a candidate price:

  * RATE estimator   — recent_rate * remaining_h. Robust early in the day when
                       little has sold, but blind to the shape of the day.
  * PACE estimator   — uses the intraday profile's cumulative share elapsed to
                       infer today's *level* from what has already sold, then the
                       profile's remaining share to say how much is still ahead:
                           D_hat            = units_sold / cum_share_to_now
                           remaining_pace   = D_hat * remaining_share
                       This carries the evening peak: at 4pm the profile knows a
                       large share of the day is still to come, so it holds.

The blend weight `w` grows with how much of the day's demand has elapsed AND how
many units have actually been observed — so we lean on RATE at open (pace is
noisy when cum_share ~ 0) and on PACE from mid-morning onward. The function is a
pure function of its inputs: no clock reads, no I/O, deterministic on replay.
Time-of-day and the profile shares arrive as computed parameters (resolved in an
activity), exactly like `remaining_h` does today.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DayProjection:
    """Diagnostics for one projection — logged so every decision is explainable."""

    remaining_demand: float        # expected units now->close at CURRENT price
    projected_total: float         # units_sold + remaining_demand
    method: str                    # "pace" | "rate" | "blend" | "flat_fallback"
    pace_weight: float             # w in [0,1]; share of the pace estimator used
    remaining_demand_pace: float
    remaining_demand_rate: float
    cum_share_to_now: float        # fraction of the day's demand already elapsed
    remaining_share: float         # fraction still ahead (profile)
    profile_source: str            # "sku" | "category" | "store" | "synthetic" | "none"


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def project_remaining_demand(
    *,
    units_sold: int,
    recent_rate: float,            # trailing units/hour (the v2 signal)
    remaining_h: float,            # hours from now to must-clear (close)
    cum_share_to_now: float,       # profile: demand fraction elapsed by now (0..1)
    remaining_share: float,        # profile: demand fraction still ahead (0..1)
    profile_source: str = "none",  # provenance of the shares
    # blend tuning (config-driven; snapshotted at run start)
    share_ref: float = 0.12,       # cum_share at which pace is fully trusted
    units_ref: float = 5.0,        # units observed at which pace is fully trusted
    min_profile_conf: float = 0.0, # floor on pace weight when a real profile exists
) -> DayProjection:
    """Estimate baseline units that will sell from now to close at the current price.

    The caller scales `remaining_demand` by an elasticity lift for candidate cuts.
    """
    remaining_h = max(0.0, remaining_h)
    recent_rate = max(0.0, recent_rate)
    cum_share_to_now = _clip(cum_share_to_now, 0.0, 1.0)
    remaining_share = _clip(remaining_share, 0.0, 1.0)

    rate_est = recent_rate * remaining_h

    # No usable profile -> degrade cleanly to the v2 flat behaviour.
    if profile_source in ("none", "") or cum_share_to_now <= 1e-6 or remaining_share <= 0.0:
        proj_total = units_sold + rate_est
        return DayProjection(
            remaining_demand=rate_est,
            projected_total=proj_total,
            method="flat_fallback",
            pace_weight=0.0,
            remaining_demand_pace=0.0,
            remaining_demand_rate=rate_est,
            cum_share_to_now=cum_share_to_now,
            remaining_share=remaining_share,
            profile_source=profile_source or "none",
        )

    # PACE: infer today's level from what has sold, project the remainder by shape.
    d_hat = units_sold / cum_share_to_now
    pace_est = d_hat * remaining_share

    # Confidence in pace: BOTH enough of the day elapsed AND enough units seen.
    w = min(_clip(cum_share_to_now / share_ref, 0.0, 1.0),
            _clip(units_sold / units_ref, 0.0, 1.0))
    if profile_source != "synthetic":
        w = max(w, min_profile_conf)

    remaining = w * pace_est + (1.0 - w) * rate_est
    if w >= 0.999:
        method = "pace"
    elif w <= 0.001:
        method = "rate"
    else:
        method = "blend"

    return DayProjection(
        remaining_demand=remaining,
        projected_total=units_sold + remaining,
        method=method,
        pace_weight=round(w, 3),
        remaining_demand_pace=round(pace_est, 2),
        remaining_demand_rate=round(rate_est, 2),
        cum_share_to_now=round(cum_share_to_now, 4),
        remaining_share=round(remaining_share, 4),
        profile_source=profile_source,
    )
