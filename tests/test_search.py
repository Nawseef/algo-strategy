"""
Tests for the strategy search harness (app.cfd_research.search).

A fake backtest function returns canned synthetic trades per (strategy,
instrument), so the harness is exercised end-to-end without a database:
ranking, risk sweep, min-trades filter, applies_to filter, and portfolio
assembly of selected winners.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_execution.base import ExitReason
from app.cfd_research.portfolio_sim import PortfolioConstraints
from app.cfd_research.search import SearchConfig, assemble_portfolio, run_search
from app.cfd_strategy.base import CFDStrategy, Direction
from app.core.models import Timeframe

_BASE = datetime(2023, 1, 2, 12, 0, tzinfo=timezone.utc)


def _trade(instrument, day, net_usd, mae_price):
    entry = (_BASE + timedelta(days=day)).timestamp() * 1000
    return SimulatedTrade(
        instrument=instrument, direction=Direction.LONG, entry_price=2400.0,
        entry_time_ms=entry, exit_price=2410.0, exit_time_ms=entry + 3_600_000,
        exit_reason=ExitReason.TAKE_PROFIT if net_usd > 0 else ExitReason.STOP_LOSS,
        lots=1.0, planned_rr=2.0, realized_rr=2.0 if net_usd > 0 else -1.0,
        pnl_price=net_usd / 100.0, pnl_usd=net_usd, cost_usd=0.0, net_pnl_usd=net_usd,
        mfe_price=10.0 if net_usd > 0 else 0.0, mae_price=mae_price, bars_held=1,
    )


def _series(instrument, n_days, net_usd, mae_price):
    return [_trade(instrument, d, net_usd, mae_price) for d in range(n_days)]


class _Strat(CFDStrategy):
    """Stub strategy: identity only; the fake backtest supplies the trades."""

    timeframe = Timeframe.M5

    def __init__(self, sid, instruments=()):
        self.strategy_id = sid
        self.instruments = instruments

    def evaluate(self, ctx):
        return []


def _fake_backtest():
    """Return a BacktestFn dispatching canned trades by strategy id."""
    def fn(strategy, instrument, start, end, ref_risk):
        sid = strategy.strategy_id
        if sid == "good":
            return _series(instrument, 60, +2000.0, 0.0)     # +2%/day winners
        if sid == "bad":
            return _series(instrument, 60, -4000.0, 40.0)    # -4%/day, 4% MAE -> blows up
        if sid == "few":
            return _series(instrument, 5, +2000.0, 0.0)      # below min_trades
        return []
    return fn


def _config():
    return SearchConfig(
        start_date=date(2023, 1, 1), end_date=date(2023, 12, 31),
        ref_risk_pct=1.0, risk_levels=(0.5, 1.0), step_days=7, min_trades=30,
    )


def test_ranks_good_above_bad():
    strats = [_Strat("bad"), _Strat("good")]
    results, cache = run_search(strats, ["XAUUSD"], _config(), _fake_backtest())
    assert results, "expected results"
    # Best row should be the profitable strategy.
    assert results[0].strategy_id == "good"
    assert results[0].mc.pass_rate > 0.0
    # The losing strategy shows up with blow-ups and no passes.
    bad_rows = [r for r in results if r.strategy_id == "bad"]
    assert bad_rows and all(r.mc.pass_rate == 0.0 for r in bad_rows)
    assert any(r.mc.blowup_rate > 0.0 for r in bad_rows)


def test_risk_sweep_produces_row_per_level_and_blowup_monotonic():
    results, _ = run_search([_Strat("bad")], ["XAUUSD"], _config(), _fake_backtest())
    by_risk = {r.risk_pct: r for r in results}
    assert set(by_risk) == {0.5, 1.0}
    # Lower per-trade risk can never blow up MORE than higher risk (safety invariant).
    assert by_risk[0.5].mc.blowup_rate <= by_risk[1.0].mc.blowup_rate


def test_min_trades_filter_skips_thin_combos():
    results, cache = run_search([_Strat("few")], ["XAUUSD"], _config(), _fake_backtest())
    assert results == []
    assert ("few", "XAUUSD") not in cache


def test_applies_to_filter():
    # Strategy restricted to XAUUSD must not be scored on EURUSD.
    strats = [_Strat("good", instruments=("XAUUSD",))]
    results, cache = run_search(strats, ["XAUUSD", "EURUSD"], _config(), _fake_backtest())
    instruments_scored = {r.instrument for r in results}
    assert instruments_scored == {"XAUUSD"}
    assert ("good", "EURUSD") not in cache


def test_assemble_portfolio_of_winners():
    strats = [_Strat("good")]
    _, cache = run_search(strats, ["XAUUSD", "EURUSD"], _config(), _fake_backtest())
    constraints = PortfolioConstraints(
        max_trades_per_day=5, max_concurrent_positions=5,
        max_open_risk_pct=10.0, one_position_per_instrument=True,
    )
    mc = assemble_portfolio(
        [("good", "XAUUSD"), ("good", "EURUSD")], cache, _config(), constraints, risk_pct=0.5
    )
    assert mc.runs > 1
    assert mc.blowup_rate == 0.0
    assert mc.pass_rate > 0.0
