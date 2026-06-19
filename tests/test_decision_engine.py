"""Unit tests for the pure decision engine — the determinism/audit guarantee."""
from __future__ import annotations

from pricing.decision_engine import decide, price_for_rung
from pricing.ladder import DEFAULT_RUNGS

LIST = 40.0
TOKEN = 1.0


def _decide(**kw):
    base = dict(
        rungs=DEFAULT_RUNGS, list_price=LIST, token_free_price=TOKEN,
        theta_hold=0.85, is_rte=False, past_rte_gate=False,
    )
    base.update(kw)
    return decide(**base)


def test_hold_when_on_track():
    # selling fast enough to clear -> HOLD, no markdown
    r = _decide(q0=40, units_sold=20, run_rate=10, nominal_remaining_h=3,
                current_rung_index=0, ceiling_rung_index=1)
    assert r.decision.value == "HOLD"
    assert r.target_rung_index == 0
    assert not r.requires_approval


def test_step_to_ceiling_when_lagging_badly():
    # barely selling -> take the ceiling rung
    r = _decide(q0=40, units_sold=5, run_rate=1, nominal_remaining_h=3,
                current_rung_index=0, ceiling_rung_index=1)
    assert r.decision.value == "STEP"
    assert r.target_rung_index == 1
    assert r.requires_approval
    assert r.target_price == price_for_rung(DEFAULT_RUNGS[1], LIST, TOKEN)  # 25% off


def test_step_one_rung_when_slightly_short():
    # ratio between theta_hold and 1.0 -> single step toward ceiling
    r = _decide(q0=40, units_sold=28, run_rate=3, nominal_remaining_h=2,
                current_rung_index=0, ceiling_rung_index=2)
    assert r.decision.value == "STEP"
    assert r.target_rung_index == 1  # min(current+1, ceiling)


def test_price_is_monotonic_non_increasing():
    # already at R2; an on-track read must never step back up to R1/R0
    r = _decide(q0=40, units_sold=40, run_rate=5, nominal_remaining_h=2,
                current_rung_index=2, ceiling_rung_index=2)
    assert r.target_rung_index == 2
    assert r.decision.value == "HOLD"


def test_rte_auto_clear_to_token_past_gate():
    r = _decide(q0=40, units_sold=34, run_rate=1, nominal_remaining_h=0,
                current_rung_index=2, ceiling_rung_index=3,
                is_rte=True, past_rte_gate=True)
    assert r.decision.value == "AUTO_CLEAR"
    assert r.target_rung_index == 3
    assert r.target_price == TOKEN
    assert not r.requires_approval


def test_non_rte_token_rung_requires_approval():
    r = _decide(q0=40, units_sold=2, run_rate=0.2, nominal_remaining_h=0,
                current_rung_index=2, ceiling_rung_index=3,
                is_rte=False, past_rte_gate=True)
    assert r.target_rung_index == 3
    assert r.requires_approval
