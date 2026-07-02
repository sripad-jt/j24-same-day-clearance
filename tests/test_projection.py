"""Tests for the v3 profile-aware projection + decide_v3.

The headline test is the leafy-greens evening-peak scenario: at ~4pm a store has
sold roughly half its coriander at the sleepy midday rate. Flat extrapolation
(v2) says "you'll fall short, cut the price." The profile knows a large share of
the day's demand is still ahead (the evening rush), so v3 HOLDs. That difference
is the whole point of the redesign.
"""
from __future__ import annotations

from pricing.decision_engine import decide_v2, decide_v3
from pricing.projection import project_remaining_demand
from shared.models import Decision


# --- profile shares for leafy greens at 16:00 (evening peak still ahead) ------
# By 4pm ~45% of the day's demand has happened; ~55% is still to come.
CUM_4PM = 0.45
REM_4PM = 0.55


def test_flat_fallback_matches_v2_projection():
    """With no profile, the projector reproduces v2's flat proj exactly."""
    proj = project_remaining_demand(
        units_sold=20, recent_rate=4.0, remaining_h=5.0,
        cum_share_to_now=0.0, remaining_share=0.0, profile_source="none",
    )
    assert proj.method == "flat_fallback"
    assert proj.remaining_demand == 4.0 * 5.0
    assert proj.projected_total == 20 + 20


def test_evening_peak_profile_holds_where_flat_marks_down():
    """The core fix: flat cuts, profile holds, same inputs."""
    q0, units_sold, list_price, floor = 60, 27, 20.0, 4.0
    # Midday rate is sleepy: 3 units/hr, 5 hours nominally 'remaining'.
    recent_rate, remaining_h = 3.0, 5.0

    # v2 flat: proj = 27 + 3*5 = 42 of 60 -> behind -> STEP (marks down)
    v2 = decide_v2(
        q0=q0, units_sold=units_sold, recent_rate=recent_rate,
        remaining_h=remaining_h, current_price=list_price, list_price=list_price,
        floor_price=floor,
    )
    assert v2.decision == Decision.STEP

    # v3 with the evening-peak profile: D_hat = 27/0.45 = 60, remaining = 60*0.55
    # = 33 -> proj = 27 + 33 = 60 -> on track -> HOLD (protects margin)
    proj = project_remaining_demand(
        units_sold=units_sold, recent_rate=recent_rate, remaining_h=remaining_h,
        cum_share_to_now=CUM_4PM, remaining_share=REM_4PM, profile_source="sku",
    )
    v3 = decide_v3(
        q0=q0, units_sold=units_sold, remaining_demand=proj.remaining_demand,
        current_price=list_price, list_price=list_price, floor_price=floor,
        projection_method=proj.method,
    )
    assert v3.decision == Decision.HOLD, v3.reason
    assert proj.method == "pace"


def test_genuinely_slow_line_still_marks_down():
    """Profile-aware must NOT paper over a real laggard: if even the evening peak
    can't clear it, v3 still steps."""
    q0, units_sold, list_price, floor = 60, 8, 20.0, 4.0
    proj = project_remaining_demand(
        units_sold=units_sold, recent_rate=1.0, remaining_h=5.0,
        cum_share_to_now=CUM_4PM, remaining_share=REM_4PM, profile_source="sku",
    )
    # D_hat = 8/0.45 = 17.8 -> remaining = 9.8 -> proj = ~17.8 of 60: way behind
    v3 = decide_v3(
        q0=q0, units_sold=units_sold, remaining_demand=proj.remaining_demand,
        current_price=list_price, list_price=list_price, floor_price=floor,
    )
    assert v3.decision == Decision.STEP
    assert v3.target_price < list_price


def test_early_morning_leans_on_rate_not_pace():
    """At open, cum_share is tiny -> pace is noisy -> weight favours rate."""
    proj = project_remaining_demand(
        units_sold=1, recent_rate=2.0, remaining_h=10.0,
        cum_share_to_now=0.02, remaining_share=0.98, profile_source="sku",
    )
    assert proj.pace_weight < 0.3       # mostly rate early on
    assert proj.method in ("rate", "blend")


def test_projection_is_deterministic():
    """Same inputs -> identical projection (replay safety)."""
    kw = dict(units_sold=27, recent_rate=3.0, remaining_h=5.0,
              cum_share_to_now=CUM_4PM, remaining_share=REM_4PM, profile_source="sku")
    a = project_remaining_demand(**kw)
    b = project_remaining_demand(**kw)
    assert a == b


def test_monotonic_price_never_steps_up():
    """A cut candidate above the current price is never chosen."""
    proj = project_remaining_demand(
        units_sold=5, recent_rate=1.0, remaining_h=3.0,
        cum_share_to_now=0.6, remaining_share=0.4, profile_source="sku",
    )
    v3 = decide_v3(
        q0=40, units_sold=5, remaining_demand=proj.remaining_demand,
        current_price=10.0, list_price=20.0, floor_price=4.0,
    )
    assert v3.target_price <= 10.0
