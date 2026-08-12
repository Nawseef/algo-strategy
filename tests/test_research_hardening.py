"""
Tests for the research-pipeline hardening fixes (G1-G9) — the money-critical
methodology fixes that keep strategy findings trustworthy.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_execution.base import ExitReason
from app.cfd_research.challenge_sim import ChallengeRules, MonteCarloResult
from app.cfd_research.deployability import compute_deployability
from app.cfd_research.entry_replay import replay_entries
from app.cfd_research.entry_strategy import EntryContext, EntryStrategy
from app.cfd_research.exit_models import EntryIntent
from app.cfd_research.slice_scorer import _slice_has_overlap, score_slices
from app.cfd_risk.costs import (
    COST_MODEL_INTRADAY,
    COST_MODEL_SESSION_OPEN,
    calculate_trade_cost,
)
from app.core.models import Candle, Timeframe
from app.cfd_strategy.base import Direction
from app.utils import forex_hours


def _ms(y, mo, d, h=12, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000


def _trade(entry_ms, exit_ms, net=100.0, strategy_id="s", exit_model="fixed_rr2",
           instrument="XAUUSD", session="london"):
    return SimulatedTrade(
        instrument=instrument, direction=Direction.LONG,
        entry_price=2000.0, entry_time_ms=entry_ms,
        exit_price=2010.0, exit_time_ms=exit_ms,
        exit_reason=ExitReason.TAKE_PROFIT, lots=1.0,
        planned_rr=2.0, realized_rr=2.0 if net > 0 else -1.0,
        pnl_price=0.0, pnl_usd=net, cost_usd=0.0, net_pnl_usd=net,
        mfe_price=0.0, mae_price=5.0, bars_held=1,
        strategy_id=strategy_id, exit_model=exit_model, session=session,
    )


# ─── G9: Monte-Carlo rates exclude undecided (INCOMPLETE) runs ────

def test_g9_pass_rate_excludes_incompletes():
    mc = MonteCarloResult(runs=10, passed=4, phase1_passed=6,
                          failed_daily=1, failed_max=1, timeouts=0, incompletes=5)
    assert mc.decisive_runs == 5              # 10 - 5 incompletes
    assert mc.pass_rate == pytest.approx(0.8)  # 4 / 5 decisive
    assert mc.blowup_rate == pytest.approx(0.2)  # 1 / 5
    assert mc.incomplete_rate == pytest.approx(0.5)  # 5 / 10


# ─── G7: exit_model MUST be a slice dimension ─────────────────────

def test_g7_requires_exit_model_dimension():
    rules = ChallengeRules()
    with pytest.raises(ValueError, match="exit_model"):
        score_slices([], ("instrument", "session"), rules)


def test_g7_unknown_dimension_rejected():
    with pytest.raises(ValueError, match="unknown slice dimension"):
        score_slices([], ("instrument", "bogus", "exit_model"), ChallengeRules())


# ─── G1: strategy_id is a clean attribution dimension ─────────────

def test_g1_slice_by_strategy_id_separates_variants():
    trades = []
    for i in range(40):
        trades.append(_trade(_ms(2022, 1, 1 + i % 27, 8), _ms(2022, 1, 1 + i % 27, 10),
                             strategy_id="orb_london_6b"))
        trades.append(_trade(_ms(2022, 1, 1 + i % 27, 13), _ms(2022, 1, 1 + i % 27, 15),
                             strategy_id="orb_new_york_6b", session="new_york"))
    results = score_slices(trades, ("strategy_id", "exit_model"), ChallengeRules(),
                           risk_levels=(1.0,), min_trades=1)
    variants = {r.key["strategy_id"] for r in results}
    assert variants == {"orb_london_6b", "orb_new_york_6b"}   # cleanly separated


# ─── G8: overlap detection within a slice ─────────────────────────

def test_g8_overlap_detection():
    # Non-overlapping: each exits before the next enters.
    seq = [_trade(_ms(2022, 1, 1, 8), _ms(2022, 1, 1, 9)),
           _trade(_ms(2022, 1, 1, 10), _ms(2022, 1, 1, 11))]
    assert _slice_has_overlap(seq) is False
    # Overlapping: second enters while the first is still open.
    over = [_trade(_ms(2022, 1, 1, 8), _ms(2022, 1, 1, 12)),
            _trade(_ms(2022, 1, 1, 9), _ms(2022, 1, 1, 10))]
    assert _slice_has_overlap(over) is True


# ─── G3: consistency judged over the FULL data window ─────────────

def test_g3_consistency_fails_on_decayed_edge():
    # Trades only in 2020-2021; data window says 2020-2023 -> 2022/2023 dead.
    trades = []
    for year in (2020, 2021):
        for m in range(1, 13):
            for d in range(1, 7):            # 6 trades/month => active month
                trades.append(_trade(_ms(year, m, d, 8), _ms(year, m, d, 10)))
    m = compute_deployability(
        trades,
        data_start_ms=_ms(2020, 1, 1, 0),
        data_end_ms=_ms(2024, 1, 2, 0),
    )
    assert 2022 in m.full_years and 2023 in m.full_years
    assert m.min_full_year_active_months == 0     # dead years caught
    assert m.pass_consistency is False


# ─── G4/G5: session-open costs > intraday, per-instrument slippage ─

def test_g4_g5_session_open_costs_higher_for_index():
    open_cost = calculate_trade_cost("DE40", lot_size=1.0, cost_model=COST_MODEL_SESSION_OPEN)
    intra_cost = calculate_trade_cost("DE40", lot_size=1.0, cost_model=COST_MODEL_INTRADAY)
    assert open_cost.total_usd > intra_cost.total_usd
    # Slippage should be meaningful for an index under session_open (price-based),
    # not the near-zero flat-pip value.
    assert open_cost.slippage_usd > intra_cost.slippage_usd


# ─── G2: intraday flatten — no holds past the FX day boundary ─────

class _OneShotLong(EntryStrategy):
    strategy_id = "oneshot"
    name = "one shot"
    instruments = ()
    min_history = 2

    def __init__(self, trigger_ms):
        self._trigger_ms = trigger_ms

    def entries(self, ctx: EntryContext):
        if ctx.candle.timestamp_ms == self._trigger_ms:
            return [EntryIntent(
                instrument=ctx.instrument, direction=Direction.LONG,
                entry_price=ctx.close, stop_loss=ctx.close - 10.0,   # R=10, TP at +20
                entry_time_ms=ctx.candle.timestamp_ms, reason="test",
            )]
        return []


def _flat_candles(start_dt, n, price=100.0):
    out = []
    for i in range(n):
        ts = (start_dt.timestamp() + i * 300) * 1000
        out.append(Candle(exchange="CFD", segment="cfd", exchange_token="XAUUSD",
                          timeframe=Timeframe.M5, timestamp_ms=ts,
                          open=price, high=price, low=price, close=price, volume=1))
    return out


def test_g2_intraday_flatten_vs_overnight():
    # Flat price so SL/TP never hit -> the only exit is the day-boundary flatten.
    # Start mid-day UTC; the FX day rolls at 17:00 NY (=22:00 UTC in winter),
    # so a run of ~200 5m bars crosses the boundary.
    start = datetime(2022, 1, 3, 12, 0, tzinfo=timezone.utc)
    candles = _flat_candles(start, 220)
    trigger_ms = candles[5].timestamp_ms
    entry_day = forex_hours.trading_day(datetime.fromtimestamp(trigger_ms / 1000, timezone.utc))

    intraday = replay_entries("XAUUSD", candles, _OneShotLong(trigger_ms),
                              intraday_only=True)
    overnight = replay_entries("XAUUSD", candles, _OneShotLong(trigger_ms),
                               intraday_only=False)

    assert intraday and overnight
    # Every intraday trade exits within the SAME FX trading day it opened.
    for t in intraday:
        exit_day = forex_hours.trading_day(datetime.fromtimestamp(t.exit_time_ms / 1000, timezone.utc))
        assert exit_day == entry_day
    # Overnight holds strictly longer (crosses the boundary).
    assert overnight[0].bars_held > intraday[0].bars_held
