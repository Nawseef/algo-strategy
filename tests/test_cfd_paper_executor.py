"""
Tests for the CFD paper executor — verifies fills, SL/TP exits, sizing, and
that the risk guard / PnL math are correct. Uses an in-memory SQLite store.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import pytest

from app.cfd_execution.account import AccountConfig, PropFirmRules
from app.cfd_execution.base import ExitReason
from app.cfd_execution.paper_executor import PaperExecutor
from app.cfd_risk.costs import COST_MODEL_ZERO
from app.cfd_strategy.base import (
    CFDSignal,
    Direction,
    EntryMode,
    build_rr_exit_plan,
)


def _make_executor(balance=100_000.0, risk_pct=1.0):
    account = AccountConfig(
        account_id="test",
        initial_balance=balance,
        rules=PropFirmRules(max_risk_per_trade_pct=risk_pct, daily_dd_pct=50, max_dd_pct=90),
        risk_per_trade_pct=risk_pct,
    )
    # Zero-cost model so PnL math is exact and easy to assert.
    return PaperExecutor(account, store=None, notifier=None, cost_model=COST_MODEL_ZERO)


def _gold_long_signal(entry=2400.0, sl=2390.0, rr=(2.0,), mode=EntryMode.CANDLE_CLOSE):
    plan = build_rr_exit_plan(Direction.LONG, entry, sl, rr_targets=list(rr))
    return CFDSignal(
        strategy_id="gold", variant_id="v1", instrument="XAUUSD",
        direction=Direction.LONG, entry_mode=mode, entry_price=entry,
        exit_plan=plan, timestamp_ms=1000.0, expiry_candles=3,
    )


def test_candle_close_fill_then_tp_hit():
    ex = _make_executor()
    ex.on_signal(_gold_long_signal())          # entry 2400, SL 2390, TP 2420 (2R)
    assert len(ex.open_positions()) == 1

    # Price rises but not to TP.
    ex.on_tick("XAUUSD", bid=2410.0, ask=2410.2, timestamp_ms=2000)
    assert len(ex.open_positions()) == 1

    # Price reaches TP -> closes.
    ex.on_tick("XAUUSD", bid=2420.0, ask=2420.2, timestamp_ms=3000)
    assert len(ex.open_positions()) == 0

    # Sizing: 1% of 100k = $1000 risk, gold point value $100/lot, SL 10 => $1000/lot.
    # => 1.0 lot. TP at +20 => 20 * 100 * 1.0 = $2000 gross. Less $7 gold commission.
    assert math.isclose(ex.risk_guard.balance, 101_993.0, rel_tol=1e-6)


def test_candle_close_fill_then_sl_hit():
    ex = _make_executor()
    ex.on_signal(_gold_long_signal())
    # Price drops to SL -> loss of 1R = $1000, plus $7 commission = -$1007.
    ex.on_tick("XAUUSD", bid=2390.0, ask=2390.2, timestamp_ms=2000)
    assert len(ex.open_positions()) == 0
    assert math.isclose(ex.risk_guard.balance, 98_993.0, rel_tol=1e-6)


def test_sl_wins_when_both_in_same_tick_is_not_possible_but_bar_range_conservative():
    """A single tick can't hit both; but confirm SL priority on a wide move down."""
    ex = _make_executor()
    ex.on_signal(_gold_long_signal())
    # A tick straight to below SL closes at SL price (2390), not lower.
    ex.on_tick("XAUUSD", bid=2380.0, ask=2380.2, timestamp_ms=2000)
    assert len(ex.open_positions()) == 0
    # Loss booked at SL (1R = $1000) + $7 commission = -$1007, not at 2380.
    assert math.isclose(ex.risk_guard.balance, 98_993.0, rel_tol=1e-6)


def test_intrabar_arm_fills_on_trigger():
    ex = _make_executor()
    sig = _gold_long_signal(entry=2405.0, sl=2395.0, mode=EntryMode.INTRABAR)
    ex.on_signal(sig)
    assert len(ex.open_positions()) == 0  # armed, not filled

    # Price below trigger — still armed.
    ex.on_tick("XAUUSD", bid=2400.0, ask=2400.2, timestamp_ms=2000)
    assert len(ex.open_positions()) == 0

    # Price reaches trigger 2405 -> fills.
    ex.on_tick("XAUUSD", bid=2405.0, ask=2405.2, timestamp_ms=3000)
    assert len(ex.open_positions()) == 1


def test_intrabar_arm_expires():
    ex = _make_executor()
    sig = _gold_long_signal(entry=2405.0, sl=2395.0, mode=EntryMode.INTRABAR)
    sig.expiry_candles = 2
    ex.on_signal(sig)

    # Age two candles without the trigger being hit.
    ex.on_candle_close("XAUUSD", 2000)
    ex.on_candle_close("XAUUSD", 3000)

    # Now price reaches trigger but the arm has expired.
    ex.on_tick("XAUUSD", bid=2405.0, ask=2405.2, timestamp_ms=4000)
    assert len(ex.open_positions()) == 0


def test_dedup_no_double_open():
    ex = _make_executor()
    ex.on_signal(_gold_long_signal())
    ex.on_signal(_gold_long_signal())  # same key -> ignored
    assert len(ex.open_positions()) == 1


def test_partial_tp_then_sl_on_remainder():
    """Half closes at 2R TP; remainder rides and then stops out."""
    ex = _make_executor()
    plan = build_rr_exit_plan(
        Direction.LONG, 2400.0, 2390.0,
        rr_targets=[2.0, 4.0], close_fractions=[0.5, 0.5],
    )
    sig = CFDSignal(
        strategy_id="gold", variant_id="v1", instrument="XAUUSD",
        direction=Direction.LONG, entry_mode=EntryMode.CANDLE_CLOSE,
        entry_price=2400.0, exit_plan=plan, timestamp_ms=1000.0,
    )
    ex.on_signal(sig)  # 1 lot (1% risk, $10 SL)

    # Hit first TP (2420) -> close half.
    ex.on_tick("XAUUSD", bid=2420.0, ask=2420.2, timestamp_ms=2000)
    assert len(ex.open_positions()) == 1  # half still open

    # Remainder stops out at 2390.
    ex.on_tick("XAUUSD", bid=2390.0, ask=2390.2, timestamp_ms=3000)
    assert len(ex.open_positions()) == 0

    # PnL: half at +2R (+$1000 on 0.5 lot: 20*100*0.5=$1000),
    #      half at -1R (-$500 on 0.5 lot: -10*100*0.5=-$500). Net +$500 gross.
    #      Less $7 gold commission (charged once on full 1 lot round-trip) = +$493.
    assert math.isclose(ex.risk_guard.balance, 100_493.0, rel_tol=1e-6)


def test_flatten_all():
    ex = _make_executor()
    ex.on_signal(_gold_long_signal())
    ex.on_tick("XAUUSD", bid=2405.0, ask=2405.2, timestamp_ms=2000)
    ex.flatten_all()
    assert len(ex.open_positions()) == 0


def test_persist_to_store():
    tmp = tempfile.mktemp(suffix=".db")
    from app.db.research_store import ResearchStore
    store = ResearchStore(db_path=Path(tmp))
    store.start()
    account = AccountConfig(
        account_id="acc1", initial_balance=100_000.0,
        rules=PropFirmRules(max_risk_per_trade_pct=1.0, daily_dd_pct=50, max_dd_pct=90),
    )
    ex = PaperExecutor(account, store=store, notifier=None, cost_model=COST_MODEL_ZERO)
    ex.on_signal(_gold_long_signal())
    ex.on_tick("XAUUSD", bid=2420.0, ask=2420.2, timestamp_ms=3000)

    trades = store.get_cfd_paper_trades(account_id="acc1")
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "TAKE_PROFIT"
    # +$2000 gross less $7 gold commission = $1993 net.
    assert math.isclose(trades[0]["net_pnl_usd"], 1993.0, rel_tol=1e-6)
    store.stop()
    import os
    os.remove(tmp)
