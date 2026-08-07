"""
Tests for the CFD strategy framework — focused on the money-safety invariants:
mandatory SL/TP and the enforced 1:2 R:R floor.
"""

from __future__ import annotations

import math

import pytest

from app.cfd_strategy.base import (
    MIN_RR,
    CFDSignal,
    Direction,
    EntryMode,
    ExitPlan,
    TakeProfit,
    build_rr_exit_plan,
)


# ─── ExitPlan RR enforcement ─────────────────────────────────────────────────


def test_long_exit_plan_valid_1to2():
    """A clean 1:2 long plan is accepted and reports RR 2.0."""
    plan = ExitPlan(
        direction=Direction.LONG,
        entry_price=2400.0,
        stop_loss=2390.0,                       # 10 risk
        take_profits=(TakeProfit(2420.0),),     # 20 reward => 2R
    )
    assert math.isclose(plan.risk_distance, 10.0)
    assert math.isclose(plan.max_rr, 2.0)


def test_short_exit_plan_valid_1to2():
    plan = ExitPlan(
        direction=Direction.SHORT,
        entry_price=2400.0,
        stop_loss=2410.0,                       # 10 risk
        take_profits=(TakeProfit(2380.0),),     # 20 reward => 2R
    )
    assert math.isclose(plan.max_rr, 2.0)


def test_rr_below_minimum_rejected():
    """A plan that only reaches 1.5R must be rejected (below 1:2 floor)."""
    with pytest.raises(ValueError, match="below the minimum"):
        ExitPlan(
            direction=Direction.LONG,
            entry_price=2400.0,
            stop_loss=2390.0,                   # 10 risk
            take_profits=(TakeProfit(2415.0),), # 15 reward => 1.5R
        )


def test_stop_on_wrong_side_long_rejected():
    """LONG stop above entry is invalid."""
    with pytest.raises(ValueError, match="wrong side"):
        ExitPlan(
            direction=Direction.LONG,
            entry_price=2400.0,
            stop_loss=2410.0,                   # above entry — wrong side
            take_profits=(TakeProfit(2440.0),),
        )


def test_tp_on_wrong_side_short_rejected():
    """SHORT take-profit above entry is invalid."""
    with pytest.raises(ValueError, match="wrong side"):
        ExitPlan(
            direction=Direction.SHORT,
            entry_price=2400.0,
            stop_loss=2410.0,
            take_profits=(TakeProfit(2420.0),),  # above entry — wrong side for short
        )


def test_missing_tp_rejected():
    """TP is mandatory."""
    with pytest.raises(ValueError, match="at least one take-profit"):
        ExitPlan(
            direction=Direction.LONG,
            entry_price=2400.0,
            stop_loss=2390.0,
            take_profits=(),
        )


def test_partial_fractions_over_one_rejected():
    with pytest.raises(ValueError, match="cannot close more than"):
        ExitPlan(
            direction=Direction.LONG,
            entry_price=2400.0,
            stop_loss=2390.0,
            take_profits=(
                TakeProfit(2420.0, close_fraction=0.7),
                TakeProfit(2430.0, close_fraction=0.7),  # sums to 1.4
            ),
        )


def test_furthest_tp_defines_max_rr():
    """With multiple TPs, max_rr uses the furthest target."""
    plan = ExitPlan(
        direction=Direction.LONG,
        entry_price=2400.0,
        stop_loss=2390.0,                       # 10 risk
        take_profits=(
            TakeProfit(2420.0, close_fraction=0.5),  # 2R
            TakeProfit(2430.0, close_fraction=0.5),  # 3R (furthest)
        ),
    )
    assert math.isclose(plan.max_rr, 3.0)


def test_blended_rr_weighted():
    """Blended RR is fraction-weighted across TPs."""
    plan = ExitPlan(
        direction=Direction.LONG,
        entry_price=2400.0,
        stop_loss=2390.0,                       # 10 risk
        take_profits=(
            TakeProfit(2420.0, close_fraction=0.5),  # 2R on half
            TakeProfit(2430.0, close_fraction=0.5),  # 3R on half
        ),
    )
    # 0.5*2 + 0.5*3 = 2.5
    assert math.isclose(plan.blended_rr, 2.5)


# ─── build_rr_exit_plan helper ───────────────────────────────────────────────


def test_build_rr_exit_plan_single_target_long():
    plan = build_rr_exit_plan(Direction.LONG, 2400.0, 2390.0, rr_targets=[2.0])
    assert math.isclose(plan.take_profits[0].price, 2420.0)
    assert math.isclose(plan.max_rr, 2.0)
    assert math.isclose(plan.take_profits[0].close_fraction, 1.0)


def test_build_rr_exit_plan_single_target_short():
    plan = build_rr_exit_plan(Direction.SHORT, 100.0, 102.0, rr_targets=[2.5])
    # risk = 2, reward = 5 => TP at 95
    assert math.isclose(plan.take_profits[0].price, 95.0)
    assert math.isclose(plan.max_rr, 2.5)


def test_build_rr_exit_plan_multi_target_even_split():
    plan = build_rr_exit_plan(
        Direction.LONG, 2400.0, 2390.0, rr_targets=[2.0, 3.0, 5.0]
    )
    fractions = [tp.close_fraction for tp in plan.take_profits]
    assert math.isclose(sum(fractions), 1.0)
    assert math.isclose(plan.max_rr, 5.0)


def test_build_rr_exit_plan_below_min_rejected():
    """Even the helper enforces the floor: 1.5R single target is rejected."""
    with pytest.raises(ValueError, match="below the minimum"):
        build_rr_exit_plan(Direction.LONG, 2400.0, 2390.0, rr_targets=[1.5])


def test_build_rr_exit_plan_zero_risk_rejected():
    with pytest.raises(ValueError, match="risk distance is zero"):
        build_rr_exit_plan(Direction.LONG, 2400.0, 2400.0, rr_targets=[2.0])


# ─── CFDSignal consistency guards ────────────────────────────────────────────


def test_signal_entry_mismatch_rejected():
    plan = build_rr_exit_plan(Direction.LONG, 2400.0, 2390.0)
    with pytest.raises(ValueError, match="does not match"):
        CFDSignal(
            strategy_id="s", variant_id="v", instrument="XAUUSD",
            direction=Direction.LONG, entry_mode=EntryMode.CANDLE_CLOSE,
            entry_price=2401.0,  # mismatch vs plan's 2400
            exit_plan=plan, timestamp_ms=0.0,
        )


def test_signal_direction_mismatch_rejected():
    plan = build_rr_exit_plan(Direction.LONG, 2400.0, 2390.0)
    with pytest.raises(ValueError, match="does not match"):
        CFDSignal(
            strategy_id="s", variant_id="v", instrument="XAUUSD",
            direction=Direction.SHORT,  # mismatch vs plan
            entry_mode=EntryMode.CANDLE_CLOSE,
            entry_price=2400.0, exit_plan=plan, timestamp_ms=0.0,
        )


def test_signal_valid_construction():
    plan = build_rr_exit_plan(Direction.LONG, 2400.0, 2390.0, rr_targets=[2.0, 3.0])
    sig = CFDSignal(
        strategy_id="gold_orb", variant_id="default", instrument="XAUUSD",
        direction=Direction.LONG, entry_mode=EntryMode.INTRABAR,
        entry_price=2400.0, exit_plan=plan, timestamp_ms=1000.0,
        expiry_candles=3, reason="test",
    )
    assert sig.stop_loss == 2390.0
    assert len(sig.take_profits) == 2
    assert sig.expiry_candles == 3


def test_signal_bad_expiry_rejected():
    plan = build_rr_exit_plan(Direction.LONG, 2400.0, 2390.0)
    with pytest.raises(ValueError, match="expiry_candles must be"):
        CFDSignal(
            strategy_id="s", variant_id="v", instrument="XAUUSD",
            direction=Direction.LONG, entry_mode=EntryMode.INTRABAR,
            entry_price=2400.0, exit_plan=plan, timestamp_ms=0.0,
            expiry_candles=0,
        )
