"""Unit tests for the pure dead-stock decision engine."""
from __future__ import annotations

from pricing.deadstock_engine import decide_deadstock, deadstock_context


def test_half_shelf_life_context():
    # 12d shelf life => 6d remaining at receipt; received 2d ago => 4d to expiry.
    ctx = deadstock_context(
        shelf_life_days=12, days_since_received=2, days_unsold=5, on_hand=40
    )
    assert ctx.days_to_expiry == 4
    assert ctx.remaining_shelf_life_days == 4
    assert ctx.daily_rate == 0.0  # not selling


def test_near_expiry_dead_clears_to_floor():
    # 6d shelf life => 3d at receipt; received 3d ago => 0d to expiry => terminal.
    d = decide_deadstock(
        on_hand=40, days_unsold=10, shelf_life_days=6, days_since_received=3,
        list_price=100, floor_price=20, current_price=100,
    )
    assert d.days_to_expiry == 0
    assert d.target_price == 20.0            # cleared to floor
    assert d.discount_pct == 80.0


def test_runway_item_marks_down_partially():
    # Plenty of runway but not selling → escalating multi-day discount, not floor.
    d = decide_deadstock(
        on_hand=40, days_unsold=1, shelf_life_days=12, days_since_received=1,
        list_price=100, floor_price=20, current_price=100,
    )
    assert 0 < d.discount_pct < 100
    assert d.target_price > 20.0             # not yet at floor
    assert d.days_to_expiry == 5


def test_monotonic_never_steps_up():
    # current already below the plan's target → price must not increase.
    d = decide_deadstock(
        on_hand=40, days_unsold=1, shelf_life_days=12, days_since_received=1,
        list_price=100, floor_price=20, current_price=50,
    )
    assert d.target_price <= 50.0


def test_deterministic():
    kw = dict(on_hand=40, days_unsold=4, shelf_life_days=8, days_since_received=2,
              list_price=100, floor_price=20, current_price=100)
    assert decide_deadstock(**kw) == decide_deadstock(**kw)
