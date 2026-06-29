"""Unit tests for the shelf-life scheduler (pure function)."""
from __future__ import annotations

from pricing.shelf_life_scheduler import plan_clearance


def _plan(**kw):
    base = dict(
        shelf_life_days=5,
        days_to_expiry=5,
        remaining_units=40,
        daily_rate=10.0,
    )
    base.update(kw)
    return plan_clearance(**base)


# ---------------------------------------------------------------- CLEAR_SAMEDAY --- #

def test_terminal_day_zero_expiry():
    p = _plan(days_to_expiry=0)
    assert p.mode.value == "CLEAR_SAMEDAY"
    assert p.is_terminal_day


def test_terminal_day_one_expiry():
    p = _plan(days_to_expiry=1)
    assert p.mode.value == "CLEAR_SAMEDAY"
    assert p.is_terminal_day


# ---------------------------------------------------------------- HOLD --- #

def test_hold_when_on_track_and_far_from_window():
    # 10 units, rate=5/day -> 2 days to clear; expires in 10 days -> far ahead
    p = _plan(shelf_life_days=10, days_to_expiry=10, remaining_units=10, daily_rate=5.0)
    assert p.mode.value == "HOLD"
    assert p.recommended_discount_pct == 0.0


# ---------------------------------------------------------------- NUDGE --- #

def test_nudge_one_day_before_window_opens():
    # 30 units at 5/day -> 6 days to clear; window=5; nudge triggers when
    # days_to_expiry == window + 1 == 6
    p = _plan(shelf_life_days=10, days_to_expiry=6, remaining_units=30, daily_rate=5.0)
    assert p.mode.value == "NUDGE"
    assert p.recommended_discount_pct == 10.0   # default nudge_discount_pct


# ---------------------------------------------------------------- CLEAR_MULTIDAY --- #

def test_clear_multiday_inside_window():
    # Expires in 3d, window would be ~5d -> inside window
    p = _plan(shelf_life_days=10, days_to_expiry=3, remaining_units=40, daily_rate=4.0)
    assert p.mode.value in ("CLEAR_MULTIDAY", "SUPPRESS_REORDER")
    assert p.recommended_discount_pct > 0.0


def test_multiday_discount_scales_with_urgency():
    # Closer to expiry = higher urgency = deeper discount
    p_near = _plan(shelf_life_days=10, days_to_expiry=2, remaining_units=40, daily_rate=4.0)
    p_far = _plan(shelf_life_days=10, days_to_expiry=4, remaining_units=40, daily_rate=4.0)
    if (p_near.mode.value in ("CLEAR_MULTIDAY", "SUPPRESS_REORDER")
            and p_far.mode.value in ("CLEAR_MULTIDAY", "SUPPRESS_REORDER")):
        assert p_near.recommended_discount_pct >= p_far.recommended_discount_pct


# ---------------------------------------------------------------- REORDER SIGNALS --- #

def test_stop_reorder_for_very_slow_mover():
    # 200 units, rate=1/day -> 200 days to clear; shelf life=5 -> STOP_REORDER
    p = _plan(shelf_life_days=5, days_to_expiry=2, remaining_units=200, daily_rate=1.0)
    assert p.reorder_action.value == "STOP_REORDER"


def test_reduce_otb_when_slightly_over_shelf_life():
    # 12 units, rate=2/day -> 6 days to clear; shelf life=5 -> REDUCE_OTB
    p = _plan(shelf_life_days=5, days_to_expiry=3, remaining_units=12, daily_rate=2.0)
    assert p.reorder_action.value == "REDUCE_OTB"


def test_no_reorder_signal_when_on_track():
    p = _plan(shelf_life_days=5, days_to_expiry=5, remaining_units=10, daily_rate=10.0)
    assert p.reorder_action.value == "NONE"


# ---------------------------------------------------------------- SUPPRESS_REORDER --- #

def test_suppress_reorder_mode_when_stop_inside_window():
    # Slow mover (rate tiny) inside the clearance window -> SUPPRESS_REORDER mode
    p = _plan(shelf_life_days=5, days_to_expiry=2, remaining_units=300, daily_rate=0.5)
    # projected_days_to_clear = 600 >> slow_mover_days_to_clear=14 -> STOP_REORDER
    assert p.reorder_action.value == "STOP_REORDER"
    assert p.mode.value == "SUPPRESS_REORDER"


# ---------------------------------------------------------------- edge cases --- #

def test_zero_rate_does_not_crash():
    p = _plan(remaining_units=10, daily_rate=0.0)
    assert p.mode.value in ("CLEAR_SAMEDAY", "HOLD", "NUDGE", "CLEAR_MULTIDAY",
                            "SUPPRESS_REORDER")
