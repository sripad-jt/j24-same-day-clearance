"""Unit tests for the pure decision engine v2 — the determinism/audit guarantee."""
from __future__ import annotations

import pytest

from pricing.decision_engine import decide_v2, price_for_rung
from pricing.ladder import DEFAULT_RUNGS

LIST = 40.0
TOKEN = 1.0


def _decide(**kw):
    base = dict(
        q0=40, units_sold=0, recent_rate=2.0, remaining_h=5.0,
        current_price=LIST, list_price=LIST, floor_price=0.0,
        elasticity=0.6, token_free_price=TOKEN, residual_tolerance=0.0,
        step_pct=5.0, max_discount_pct=60.0, hysteresis_units=0.0,
        is_rte=False, past_rte_gate=False, token_eligible=False,
    )
    base.update(kw)
    return decide_v2(**base)


# ----------------------------------------------------------------- HOLD --- #

def test_hold_when_on_track():
    # rate=4/h * 10h = 40 projected, Q0=40 -> on track -> HOLD
    r = _decide(q0=40, units_sold=0, recent_rate=4.0, remaining_h=10.0)
    assert r.decision.value == "HOLD"
    assert r.target_price == LIST
    assert not r.requires_approval
    assert r.clears


def test_hold_hysteresis_prevents_step_on_tiny_shortfall():
    # residual=1, hysteresis=2 -> still HOLD
    r = _decide(q0=40, units_sold=0, recent_rate=3.9, remaining_h=10.0, hysteresis_units=2.0)
    assert r.decision.value == "HOLD"


# ----------------------------------------------------------------- STEP --- #

def test_step_smallest_cut_that_clears():
    # q0=12, rate=2/h, 5h -> proj=10 < 12, residual=2
    # elasticity at 35% off lifts rate enough to reach 12 projected
    r = _decide(q0=12, units_sold=0, recent_rate=2.0, remaining_h=5.0)
    assert r.decision.value == "STEP"
    assert r.target_price < LIST          # must have cut
    assert r.target_price >= 0.0
    assert r.clears                       # smallest cut that clears


def test_step_does_not_over_discount():
    # if a 5% cut already clears the line, it should not jump to 10 or 20%
    r = _decide(q0=10, units_sold=9, recent_rate=0.5, remaining_h=2.0)
    # residual is very small; a tiny cut should be enough
    assert r.decision.value in ("HOLD", "STEP")
    if r.decision.value == "STEP":
        # confirm it took the shallowest step
        assert round((1 - r.target_price / LIST) * 100, 1) <= 10.0


def test_elasticity_lift_is_applied():
    # with elasticity, a price cut should increase projected clearance vs no lift
    r_with = _decide(q0=40, units_sold=0, recent_rate=2.0, remaining_h=5.0, elasticity=0.6)
    r_none = _decide(q0=40, units_sold=0, recent_rate=2.0, remaining_h=5.0, elasticity=0.0)
    # both should STEP (proj=10 < 40), but r_with should reach a shallower discount
    if r_with.decision.value == r_none.decision.value == "STEP":
        assert r_with.target_price >= r_none.target_price   # lift allows a smaller cut


def test_floor_clamps_target_price():
    floor = LIST * 0.6   # 60% of list
    r = _decide(q0=40, units_sold=0, recent_rate=0.0, remaining_h=1.0,
                floor_price=floor, max_discount_pct=80.0)
    assert r.target_price >= floor - 1e-6


def test_floored_true_when_floor_prevents_clearing():
    # zero rate, floor at 80% of list, max_discount=20% — can't clear
    r = _decide(q0=40, units_sold=0, recent_rate=0.0, remaining_h=1.0,
                floor_price=LIST * 0.8, max_discount_pct=20.0)
    # STEP but floored (can't clear within cap)
    assert r.floored


def test_price_is_monotonic_non_increasing():
    # already discounted 20%; on-track read must never step back up
    current = LIST * 0.8
    r = _decide(q0=40, units_sold=38, recent_rate=5.0, remaining_h=5.0,
                current_price=current)
    assert r.target_price <= current + 1e-6


# ---------------------------------------------------------------- AUTO_CLEAR --- #

def test_rte_auto_clear_past_gate():
    r = _decide(q0=40, units_sold=5, recent_rate=0.1, remaining_h=0.2,
                is_rte=True, past_rte_gate=True, token_eligible=True)
    assert r.decision.value == "AUTO_CLEAR"
    assert r.target_price == pytest.approx(TOKEN)
    assert not r.requires_approval
    assert r.clears


def test_non_rte_terminal_step_requires_approval():
    r = _decide(q0=40, units_sold=5, recent_rate=0.1, remaining_h=0.2,
                is_rte=False, past_rte_gate=True, token_eligible=True)
    # should STEP (not AUTO_CLEAR) and require approval
    assert r.decision.value == "STEP"
    assert r.requires_approval


# ---------------------------------------------------------------- price_for_rung --- #

def test_price_for_rung_token():
    rung = next(r for r in DEFAULT_RUNGS if r.token_free)
    assert price_for_rung(rung, LIST, TOKEN) == pytest.approx(TOKEN)


def test_price_for_rung_percentage():
    rung = next(r for r in DEFAULT_RUNGS if not r.token_free and r.ceiling_pct == 25.0)
    assert price_for_rung(rung, LIST, TOKEN) == pytest.approx(LIST * 0.75)
