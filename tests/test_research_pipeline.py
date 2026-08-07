"""
Tests for the research pipeline: Session ORB entries, the entry replay + exit
sweep, and the slice scorer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_execution.base import ExitReason
from app.cfd_research.entries.session_orb import SessionORB
from app.cfd_research.entry_replay import replay_entries
from app.cfd_research.entry_strategy import EntryContext
from app.cfd_research.exit_models import default_exit_models
from app.cfd_research.challenge_sim import ChallengeRules
from app.cfd_research.slice_scorer import score_slices
from app.cfd_risk.costs import COST_MODEL_ZERO
from app.cfd_strategy.base import Direction
from app.core.models import Candle, Timeframe

_MS = 300_000
_BASE = datetime(2024, 6, 3, 8, 0, tzinfo=timezone.utc)  # a Monday, London hours


def _c(i, o, h, l, cl):
    return Candle("ICMARKETS", "CFD", "XAUUSD", Timeframe.M5,
                  (_BASE + timedelta(minutes=5 * i)).timestamp() * 1000, o, h, l, cl, 0)


def _always_london(dt):
    return {"london"}


# ─── Session ORB ─────────────────────────────────────────────────────────────


def _orb_candles():
    # bars 0-2 form the opening range [99, 102]; bar 3 closes above -> long breakout.
    return [
        _c(0, 100, 101, 99, 100),
        _c(1, 100, 102, 99, 101),
        _c(2, 101, 102, 100, 101),
        _c(3, 101, 105, 101, 104),   # close 104 > range_high 102
    ]


def test_orb_emits_long_on_breakout():
    strat = SessionORB(session="london", range_bars=3, session_fn=_always_london)
    candles = _orb_candles()
    entries = []
    for i, c in enumerate(candles):
        entries += strat.entries(EntryContext("XAUUSD", Timeframe.M5, c, candles[: i + 1]))
    assert len(entries) == 1
    e = entries[0]
    assert e.direction is Direction.LONG
    assert e.entry_price == 104.0
    assert e.stop_loss == 99.0          # opposite side of the opening range


def test_orb_one_entry_per_session():
    strat = SessionORB(session="london", range_bars=3, session_fn=_always_london)
    candles = _orb_candles() + [_c(4, 104, 108, 104, 107)]   # another breakout bar
    entries = []
    for i, c in enumerate(candles):
        entries += strat.entries(EntryContext("XAUUSD", Timeframe.M5, c, candles[: i + 1]))
    assert len(entries) == 1                # still only the first breakout


def test_orb_resets_when_session_ends():
    # Session active for the range+breakout, then closes, then re-opens -> new range.
    seq = [{"london"}] * 4 + [set()] + [{"london"}] * 4
    def sess_fn(dt, _seq=iter(seq)):
        return next(_seq)
    strat = SessionORB(session="london", range_bars=3, session_fn=sess_fn)
    candles = _orb_candles() + [
        _c(4, 104, 105, 103, 104),           # session closed here
        _c(5, 100, 101, 99, 100),            # new session opens (range restart)
        _c(6, 100, 102, 99, 101),
        _c(7, 101, 102, 100, 101),
        _c(8, 101, 106, 101, 105),           # new breakout
    ]
    entries = []
    for i, c in enumerate(candles):
        entries += strat.entries(EntryContext("XAUUSD", Timeframe.M5, c, candles[: i + 1]))
    assert len(entries) == 2                 # one per session


# ─── Entry replay + exit sweep ───────────────────────────────────────────────


def test_replay_sweeps_all_exit_models():
    strat = SessionORB(session="london", range_bars=3, session_fn=_always_london)
    # range + breakout, then a rising run so exits resolve.
    candles = _orb_candles() + [
        _c(4, 104, 106, 103, 105), _c(5, 105, 110, 104, 109),
        _c(6, 109, 115, 108, 114), _c(7, 114, 120, 113, 119),
        _c(8, 119, 125, 118, 124), _c(9, 124, 130, 123, 129),
    ]
    trades = replay_entries("XAUUSD", candles, strat,
                            risk_pct=1.0, ref_balance=100_000.0, cost_model=COST_MODEL_ZERO)
    # One entry x 5 exit models = 5 trades, each tagged and distinctly labelled.
    assert len(trades) == len(default_exit_models())
    assert len({t.exit_model for t in trades}) == len(trades)
    for t in trades:
        assert t.session != ""
        assert t.timeframe == "5m"
        assert t.exit_model != ""


# ─── Slice scorer ────────────────────────────────────────────────────────────


def _mk_trade(exit_model, day, net_usd, mae_price, session="london", instrument="XAUUSD"):
    entry = (_BASE + timedelta(days=day)).timestamp() * 1000
    return SimulatedTrade(
        instrument=instrument, direction=Direction.LONG, entry_price=2400.0,
        entry_time_ms=entry, exit_price=2410.0, exit_time_ms=entry + 3_600_000,
        exit_reason=ExitReason.TAKE_PROFIT if net_usd > 0 else ExitReason.STOP_LOSS,
        lots=1.0, planned_rr=2.0, realized_rr=2.0 if net_usd > 0 else -1.0,
        pnl_price=net_usd / 100.0, pnl_usd=net_usd, cost_usd=0.0, net_pnl_usd=net_usd,
        mfe_price=10.0 if net_usd > 0 else 0.0, mae_price=mae_price, bars_held=1,
        session=session, regime="trend_up", volatility="normalVol",
        exit_model=exit_model, timeframe="5m",
    )


def test_score_slices_ranks_good_exit_above_bad():
    good = [_mk_trade("good_exit", d, +2000.0, 0.0) for d in range(40)]
    bad = [_mk_trade("bad_exit", d, -4000.0, 40.0) for d in range(40)]
    results = score_slices(good + bad, ("exit_model",), ChallengeRules(),
                           ref_risk_pct=1.0, risk_levels=(1.0,), min_trades=30)
    assert results
    assert results[0].key["exit_model"] == "good_exit"
    assert results[0].mc.pass_rate > 0.0
    bad_rows = [r for r in results if r.key["exit_model"] == "bad_exit"]
    assert bad_rows and bad_rows[0].mc.blowup_rate > 0.0


def test_score_slices_min_trades_filter_and_bad_dimension():
    few = [_mk_trade("x", d, 2000.0, 0.0) for d in range(10)]
    assert score_slices(few, ("exit_model",), ChallengeRules(), min_trades=30) == []
    with pytest.raises(ValueError):
        score_slices(few, ("not_a_dim",), ChallengeRules())
