"""
Integration test for the CFD backtest pipeline.

Feeds hand-built candles through a tiny deterministic strategy and asserts the
exit simulator + replay produce the correct RR outcomes with costs applied.
"""

from __future__ import annotations

import math
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.cfd_backtest.exit_simulator import simulate_exit
from app.cfd_backtest.replay import BacktestConfig, CFDBacktestReplay
from app.cfd_risk.costs import COST_MODEL_ZERO
from app.cfd_strategy.base import (
    CFDSignal,
    CFDStrategy,
    Direction,
    EntryMode,
    StrategyContext,
    build_rr_exit_plan,
)
from app.core.models import Candle, Timeframe

_MS_5M = 300_000


def _c(ts, o, h, l, cl):
    return Candle("ICMARKETS", "CFD", "XAUUSD", Timeframe.M5, ts, o, h, l, cl, 0)


# ─── Exit simulator unit tests ───────────────────────────────────────────────


def test_simulate_exit_hits_tp():
    plan = build_rr_exit_plan(Direction.LONG, 2400.0, 2390.0, rr_targets=[2.0])  # TP 2420
    # Next bar rallies through TP.
    future = [_c(_MS_5M, 2400, 2425, 2399, 2422)]
    trade = simulate_exit(
        "XAUUSD", Direction.LONG, 2400.0, 0.0, lots=1.0,
        exit_plan=plan, future_candles=future, cost_model=COST_MODEL_ZERO,
    )
    assert trade.exit_reason.value == "TAKE_PROFIT"
    assert math.isclose(trade.exit_price, 2420.0)
    assert math.isclose(trade.realized_rr, 2.0)
    # 20 * 100 * 1 lot = 2000 gross, less $7 gold commission.
    assert math.isclose(trade.net_pnl_usd, 1993.0)


def test_simulate_exit_hits_sl():
    plan = build_rr_exit_plan(Direction.LONG, 2400.0, 2390.0, rr_targets=[2.0])
    future = [_c(_MS_5M, 2400, 2402, 2388, 2389)]  # dips to 2388 < SL 2390
    trade = simulate_exit(
        "XAUUSD", Direction.LONG, 2400.0, 0.0, lots=1.0,
        exit_plan=plan, future_candles=future, cost_model=COST_MODEL_ZERO,
    )
    assert trade.exit_reason.value == "STOP_LOSS"
    assert math.isclose(trade.exit_price, 2390.0)
    assert math.isclose(trade.realized_rr, -1.0)


def test_simulate_exit_sl_priority_when_both_in_bar():
    """A bar that contains BOTH SL and TP must resolve as the stop (conservative)."""
    plan = build_rr_exit_plan(Direction.LONG, 2400.0, 2390.0, rr_targets=[2.0])  # TP 2420
    future = [_c(_MS_5M, 2400, 2425, 2385, 2400)]  # range covers SL(2390) and TP(2420)
    trade = simulate_exit(
        "XAUUSD", Direction.LONG, 2400.0, 0.0, lots=1.0,
        exit_plan=plan, future_candles=future, cost_model=COST_MODEL_ZERO,
    )
    assert trade.exit_reason.value == "STOP_LOSS"


def test_simulate_exit_short_tp():
    plan = build_rr_exit_plan(Direction.SHORT, 2400.0, 2410.0, rr_targets=[2.0])  # TP 2380
    future = [_c(_MS_5M, 2400, 2401, 2378, 2380)]
    trade = simulate_exit(
        "XAUUSD", Direction.SHORT, 2400.0, 0.0, lots=1.0,
        exit_plan=plan, future_candles=future, cost_model=COST_MODEL_ZERO,
    )
    assert trade.exit_reason.value == "TAKE_PROFIT"
    assert math.isclose(trade.realized_rr, 2.0)


# ─── Replay integration ──────────────────────────────────────────────────────


class _AlwaysLongAtClose(CFDStrategy):
    """Emits a single LONG candle-close signal on the Nth bar only."""

    strategy_id = "test_long_close"
    name = "Test Long"
    timeframe = Timeframe.M5
    instruments = ("XAUUSD",)
    min_history = 3
    variants = ("default",)

    def __init__(self, fire_on_index: int = 3):
        self._fire_on_index = fire_on_index

    def evaluate(self, ctx: StrategyContext) -> list[CFDSignal]:
        # Fire exactly once, when history length hits the target.
        if len(ctx.history) != self._fire_on_index:
            return []
        entry = ctx.close
        sl = entry - 10.0
        plan = build_rr_exit_plan(Direction.LONG, entry, sl, rr_targets=[2.0])
        return [CFDSignal(
            strategy_id=self.strategy_id, variant_id="default", instrument=ctx.instrument,
            direction=Direction.LONG, entry_mode=EntryMode.CANDLE_CLOSE,
            entry_price=entry, exit_plan=plan, timestamp_ms=ctx.candle.timestamp_ms,
        )]


def _seed_candles(store, n=10, start_price=2400.0):
    """Write n rising 5m candles for XAUUSD into cfd_historical_candles."""
    rows = []
    price = start_price
    base_ms = _date_to_ms_utc(2026, 3, 2)  # a Monday
    for i in range(n):
        o = price
        c = price + 5.0     # each bar rises 5
        h = c + 2.0
        l = o - 1.0
        ts = base_ms + i * _MS_5M
        sd = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
        rows.append((ts, o, h, l, c, 0, sd, "london"))
        price = c
    store.write_cfd_historical_candles_batch(rows, "XAUUSD", "5m")


def _date_to_ms_utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000


def test_replay_end_to_end_win():
    tmp = tempfile.mktemp(suffix=".db")
    from app.db.research_store import ResearchStore
    store = ResearchStore(db_path=Path(tmp))
    store.start()
    try:
        # Rising market: a long at bar 3 close should reach its 2R TP.
        _seed_candles(store, n=12, start_price=2400.0)

        strat = _AlwaysLongAtClose(fire_on_index=3)
        cfg = BacktestConfig(
            starting_balance=100_000.0, risk_pct=1.0,
            cost_model=COST_MODEL_ZERO, compound=False, persist=True,
        )
        replay = CFDBacktestReplay([strat], store, cfg)
        result = replay.run(["XAUUSD"], date(2026, 3, 2), date(2026, 3, 3))

        assert result.total_trades == 1
        t = result.trades[0]
        assert t.exit_reason.value == "TAKE_PROFIT"
        assert math.isclose(t.realized_rr, 2.0, rel_tol=1e-6)
        assert result.net_pnl_usd > 0
        assert result.win_rate == 100.0

        # Persisted?
        persisted = store.get_cfd_paper_trades(account_id="backtest")
        assert len(persisted) == 1
        assert persisted[0]["mode"] == "BACKTEST"
    finally:
        store.stop()
        os.remove(tmp)


class _FiresEveryBarInRange(CFDStrategy):
    """Fires a LONG on every bar while history length is in [3, 7] (5 signals)."""

    strategy_id = "test_fires_many"
    name = "Test Fires Many"
    timeframe = Timeframe.M5
    instruments = ("XAUUSD",)
    min_history = 3
    variants = ("default",)

    def evaluate(self, ctx: StrategyContext) -> list[CFDSignal]:
        if not (3 <= len(ctx.history) <= 7):
            return []
        entry = ctx.close
        plan = build_rr_exit_plan(Direction.LONG, entry, entry - 10.0, rr_targets=[2.0])
        return [CFDSignal(
            strategy_id=self.strategy_id, variant_id="default", instrument=ctx.instrument,
            direction=Direction.LONG, entry_mode=EntryMode.CANDLE_CLOSE,
            entry_price=entry, exit_plan=plan, timestamp_ms=ctx.candle.timestamp_ms,
        )]


def _run_fires_many(record_all_signals: bool) -> int:
    tmp = tempfile.mktemp(suffix=".db")
    from app.db.research_store import ResearchStore
    store = ResearchStore(db_path=Path(tmp))
    store.start()
    try:
        _seed_candles(store, n=20, start_price=2400.0)   # steady rise
        cfg = BacktestConfig(
            starting_balance=100_000.0, risk_pct=1.0, cost_model=COST_MODEL_ZERO,
            compound=False, record_all_signals=record_all_signals,
        )
        replay = CFDBacktestReplay([_FiresEveryBarInRange()], store, cfg)
        result = replay.run(["XAUUSD"], date(2026, 3, 2), date(2026, 3, 3))
        return result.total_trades
    finally:
        store.stop()
        os.remove(tmp)


def test_record_all_signals_captures_every_entry():
    # Record-all (research default): all 5 overlapping signals become trades.
    n_all = _run_fires_many(record_all_signals=True)
    # Guard on: overlapping signals for the same key are collapsed -> fewer trades.
    n_guard = _run_fires_many(record_all_signals=False)
    assert n_all == 5
    assert n_guard < n_all


def test_trades_are_tagged_with_session_regime_tf():
    tmp = tempfile.mktemp(suffix=".db")
    from app.db.research_store import ResearchStore
    store = ResearchStore(db_path=Path(tmp))
    store.start()
    try:
        # 60 rising bars so the regime classifier has enough history (needs ~51).
        _seed_candles(store, n=60, start_price=2400.0)
        strat = _AlwaysLongAtClose(fire_on_index=55)   # fire late, plenty of history
        cfg = BacktestConfig(starting_balance=100_000.0, risk_pct=1.0,
                             cost_model=COST_MODEL_ZERO, compound=False)
        replay = CFDBacktestReplay([strat], store, cfg)
        result = replay.run(["XAUUSD"], date(2026, 3, 2), date(2026, 3, 4))
        assert result.total_trades == 1
        t = result.trades[0]
        assert t.session != ""                      # tagged from entry time
        assert t.regime in ("trend_up", "trend_down", "range")
        assert t.volatility in ("loVol", "normalVol", "hiVol")
        assert t.timeframe == "5m"
    finally:
        store.stop()
        os.remove(tmp)


def test_replay_no_trades_when_history_insufficient():
    tmp = tempfile.mktemp(suffix=".db")
    from app.db.research_store import ResearchStore
    store = ResearchStore(db_path=Path(tmp))
    store.start()
    try:
        _seed_candles(store, n=12)
        # Strategy needs 50 bars but we only have 12 -> no signals.
        strat = _AlwaysLongAtClose(fire_on_index=3)
        strat.min_history = 50
        replay = CFDBacktestReplay([strat], store, BacktestConfig(cost_model=COST_MODEL_ZERO))
        result = replay.run(["XAUUSD"], date(2026, 3, 2), date(2026, 3, 3))
        assert result.total_trades == 0
    finally:
        store.stop()
        os.remove(tmp)
