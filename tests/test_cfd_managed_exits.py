"""
Tests for the shared managed-exit layer (breakeven / trailing / time-stop) used
identically by the paper executor and the cTrader live executor.

Covers:
  * ExitPolicy validation + is_dynamic()
  * apply_dynamic_stop: breakeven pull-up, trailing ratchet (never loosens),
    both LONG and SHORT
  * PaperExecutor behaviour: breakeven protects a runner, trailing banks profit,
    time-stop closes at market
  * build_rr_exit_plan passes the policy through
  * Regression: a plan with NO policy behaves exactly like a fixed SL
"""

from __future__ import annotations

import math

import pytest

from app.cfd_execution.account import AccountConfig, PropFirmRules
from app.cfd_execution.base import (
    ManagedPosition,
    PositionStatus,
    apply_dynamic_stop,
    time_stop_reached,
)
from app.cfd_execution.paper_executor import PaperExecutor
from app.cfd_risk.costs import COST_MODEL_ZERO
from app.cfd_strategy.base import (
    CFDSignal,
    Direction,
    EntryMode,
    ExitPolicy,
    build_rr_exit_plan,
)


# ─── ExitPolicy ──────────────────────────────────────────────────

def test_exit_policy_validation_and_is_dynamic():
    assert not ExitPolicy().is_dynamic()
    assert ExitPolicy(breakeven_at_r=1.0).is_dynamic()
    assert ExitPolicy(trail_r=2.0).is_dynamic()
    assert ExitPolicy(time_stop_bars=5).is_dynamic()
    for bad in [
        dict(breakeven_at_r=0),
        dict(trail_r=-1),
        dict(trail_distance=0),
        dict(time_stop_bars=0),
    ]:
        with pytest.raises(ValueError):
            ExitPolicy(**bad)


def _pos(direction, entry, stop, policy, rr=3.0):
    plan = build_rr_exit_plan(direction, entry, stop, rr_targets=[rr], exit_policy=policy)
    return ManagedPosition(
        position_id="t", strategy_id="s", variant_id="v", instrument="XAUUSD",
        direction=direction, entry_price=entry, entry_time_ms=0.0, lots=1.0,
        exit_plan=plan,
    )


# ─── apply_dynamic_stop ──────────────────────────────────────────

def test_breakeven_moves_stop_to_entry_long():
    pos = _pos(Direction.LONG, 100.0, 90.0, ExitPolicy(breakeven_at_r=1.0))  # R=10
    assert pos.current_stop == 90.0
    pos.update_excursion(110.0)                 # +1R
    assert apply_dynamic_stop(pos, 110.0) is True
    assert pos.current_stop == 100.0            # breakeven
    # Never loosens on a pullback.
    pos.update_excursion(105.0)
    assert apply_dynamic_stop(pos, 105.0) is False
    assert pos.current_stop == 100.0


def test_breakeven_moves_stop_to_entry_short():
    pos = _pos(Direction.SHORT, 100.0, 110.0, ExitPolicy(breakeven_at_r=1.0))  # R=10
    pos.update_excursion(90.0)                  # +1R favourable for a short
    assert apply_dynamic_stop(pos, 90.0) is True
    assert pos.current_stop == 100.0


def test_trailing_ratchets_and_never_loosens():
    pos = _pos(Direction.LONG, 100.0, 90.0, ExitPolicy(trail_r=1.0), rr=10.0)  # trail 10 behind
    pos.update_excursion(115.0)
    apply_dynamic_stop(pos, 115.0)
    assert pos.current_stop == 105.0
    pos.update_excursion(120.0)
    apply_dynamic_stop(pos, 120.0)
    assert pos.current_stop == 110.0
    # Pullback: best stays 120, stop must not drop.
    pos.update_excursion(112.0)
    assert apply_dynamic_stop(pos, 112.0) is False
    assert pos.current_stop == 110.0


def test_no_policy_is_inert():
    pos = _pos(Direction.LONG, 100.0, 90.0, None)
    pos.update_excursion(150.0)
    assert apply_dynamic_stop(pos, 150.0) is False
    assert pos.current_stop == 90.0
    assert time_stop_reached(pos) is False


# ─── PaperExecutor parity behaviour ──────────────────────────────

def _make_executor(balance=100_000.0, risk_pct=1.0):
    account = AccountConfig(
        account_id="test", initial_balance=balance,
        rules=PropFirmRules(max_risk_per_trade_pct=risk_pct, daily_dd_pct=50, max_dd_pct=90),
        risk_per_trade_pct=risk_pct,
    )
    return PaperExecutor(account, store=None, notifier=None, cost_model=COST_MODEL_ZERO)


def _signal(entry, sl, rr, policy, mode=EntryMode.CANDLE_CLOSE):
    plan = build_rr_exit_plan(Direction.LONG, entry, sl, rr_targets=rr, exit_policy=policy)
    return CFDSignal(
        strategy_id="g", variant_id="v", instrument="XAUUSD", direction=Direction.LONG,
        entry_mode=mode, entry_price=entry, exit_plan=plan, timestamp_ms=1000.0,
    )


def test_paper_breakeven_exits_flat_not_full_loss():
    ex = _make_executor()
    ex.on_signal(_signal(2400.0, 2390.0, [3.0], ExitPolicy(breakeven_at_r=1.0)))
    ex.on_tick("XAUUSD", 2410.0, 2410.2, 2000)   # +1R -> stop to breakeven (2400)
    assert len(ex.open_positions()) == 1
    ex.on_tick("XAUUSD", 2400.0, 2400.2, 3000)   # back to entry -> breakeven stop hit
    assert len(ex.open_positions()) == 0
    # Closed at entry: 0 gross PnL, less $7 gold commission (ZERO model still
    # charges instrument commission) -> -$7.
    assert math.isclose(ex.risk_guard.balance, 99_993.0, rel_tol=1e-9)


def test_paper_trailing_banks_profit():
    ex = _make_executor()
    ex.on_signal(_signal(2400.0, 2390.0, [10.0], ExitPolicy(trail_r=1.0)))  # trail $10
    ex.on_tick("XAUUSD", 2420.0, 2420.2, 2000)   # best 2420 -> stop 2410
    ex.on_tick("XAUUSD", 2430.0, 2430.2, 3000)   # best 2430 -> stop 2420
    assert len(ex.open_positions()) == 1
    ex.on_tick("XAUUSD", 2415.0, 2415.2, 4000)   # pullback -> stop 2420 hit
    assert len(ex.open_positions()) == 0
    # Exit at 2420 = +$20 * $100/pt * 1 lot = +$2000, less $7 commission.
    assert math.isclose(ex.risk_guard.balance, 101_993.0, rel_tol=1e-9)


def test_paper_time_stop_closes_at_market():
    ex = _make_executor()
    ex.on_signal(_signal(2400.0, 2390.0, [2.0], ExitPolicy(time_stop_bars=3)))
    ex.on_tick("XAUUSD", 2405.0, 2405.2, 2000)   # sets last price, no exit
    ex.on_candle_close("XAUUSD", 2100)           # bar 1
    ex.on_candle_close("XAUUSD", 2200)           # bar 2
    assert len(ex.open_positions()) == 1
    ex.on_candle_close("XAUUSD", 2300)           # bar 3 -> time stop closes at 2405
    assert len(ex.open_positions()) == 0
    # +$5 * $100 * 1 lot = +$500, less $7 commission.
    assert math.isclose(ex.risk_guard.balance, 100_493.0, rel_tol=1e-9)
