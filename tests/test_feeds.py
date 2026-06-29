"""Unit tests for the owner-education feed builder (pure function)."""
from __future__ import annotations

import pytest

from adapters.feeds import build_outcome
from shared.models import OfferBaseline

LIST = 40.0
OFFER = 30.0   # 25% off


def _baseline(**kw) -> OfferBaseline:
    base = dict(
        run_id="run-1", store_id="S1", jpin="J1",
        product_title="Spinach", rung="R1",
        from_price=LIST, to_price=OFFER, discount_pct=25.0,
        ts_ist="2024-01-01T10:00:00",
        units_sold_before=10, rate_before=2.0, units_left_before=30,
    )
    base.update(kw)
    return OfferBaseline(**base)


def _outcome(**kw):
    return build_outcome(
        baseline=_baseline(),
        units_sold_now=20, rate_after=4.0,
        window_h=1.5, ts_ist="2024-01-01T11:30:00",
        phase="interim", salvage_ref_price=0.0,
        **kw,
    )


# ---------------------------------------------------------------- happy path --- #

def test_normal_lift_rate():
    o = _outcome()
    assert o.rate_before == pytest.approx(2.0)
    assert o.rate_after == pytest.approx(4.0)
    assert o.lift_pct == pytest.approx(100.0, abs=0.1)


def test_units_sold_after_is_incremental():
    # units_sold_before=10, units_sold_now=20 -> after=10
    o = _outcome()
    assert o.units_sold_after == 10


def test_revenue_recovered():
    # 10 incremental units sold at ₹30
    o = _outcome()
    assert o.revenue_recovered == pytest.approx(300.0)


def test_waste_avoided_capped_at_units_left_before():
    # lift_units = (4-2) * 1.5 = 3; units_left_before=30 -> waste_avoided=3
    o = _outcome()
    assert o.waste_avoided_units == 3


def test_salvage_ref_price_drives_waste_value():
    o = build_outcome(
        baseline=_baseline(),
        units_sold_now=20, rate_after=4.0,
        window_h=1.5, ts_ist="2024-01-01T11:30:00",
        phase="interim", salvage_ref_price=5.0,
    )
    assert o.waste_avoided_value == pytest.approx(3 * 5.0)


# ---------------------------------------------------------------- stalled line --- #

def test_stalled_line_rate_before_zero():
    bl = _baseline(rate_before=0.0, units_sold_before=0, units_left_before=30)
    o = build_outcome(
        baseline=bl,
        units_sold_now=5, rate_after=2.0,
        window_h=1.5, ts_ist="2024-01-01T11:30:00",
        phase="interim", salvage_ref_price=0.0,
    )
    # lift_pct: rate_before=0 but rate_after>0 -> 100%
    assert o.lift_pct == pytest.approx(100.0)
    assert "stalled" in o.headline.lower()


def test_no_improvement_zero_lift():
    o = build_outcome(
        baseline=_baseline(rate_before=3.0),
        units_sold_now=10, rate_after=2.0,   # rate went DOWN
        window_h=1.5, ts_ist="2024-01-01T11:30:00",
        phase="interim", salvage_ref_price=0.0,
    )
    assert o.lift_pct < 0
    assert o.waste_avoided_units == 0   # no incremental lift -> no waste avoided


# ---------------------------------------------------------------- units_left --- #

def test_units_left_decrements_correctly():
    # units_left_before=30, units_sold_after=10 -> left=20
    o = _outcome()
    assert o.units_left == 20


def test_units_left_never_negative():
    bl = _baseline(units_left_before=5)
    o = build_outcome(
        baseline=bl,
        units_sold_now=100, rate_after=50.0,   # extreme sell-through
        window_h=1.0, ts_ist="2024-01-01T11:00:00",
        phase="final", salvage_ref_price=0.0,
    )
    assert o.units_left >= 0


# ---------------------------------------------------------------- phase label --- #

def test_phase_final_propagates():
    o = build_outcome(
        baseline=_baseline(),
        units_sold_now=20, rate_after=4.0,
        window_h=1.5, ts_ist="2024-01-01T21:00:00",
        phase="final", salvage_ref_price=0.0,
    )
    assert o.phase == "final"
