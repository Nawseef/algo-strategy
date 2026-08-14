"""
Tests for the MAE cap (money-safe correctness fix): a trade's recorded max
adverse excursion must not exceed the stop distance R — once price hits the stop
you're flat, so a bar spiking past the stop can't be charged as floating loss you
held. Sub-R excursions must pass through unchanged.
"""

from __future__ import annotations

from app.cfd_execution.base import ExitReason
from app.cfd_research.exit_models import EntryIntent, FixedRR, simulate_entry
from app.cfd_risk.costs import COST_MODEL_ZERO
from app.cfd_strategy.base import Direction
from app.core.models import Candle, Timeframe


def _c(o, h, l, cl):
    return Candle(exchange="ICMARKETS", segment="CFD", exchange_token="XAUUSD",
                  timeframe=Timeframe.M5, timestamp_ms=1_500_000_000_000,
                  open=o, high=h, low=l, close=cl, volume=0)


def test_beyond_stop_spike_capped_at_R():
    # Long entry 2000, stop 1999 -> R = 1. A bar spikes down to 1997 (3R past the
    # stop) and stops the trade out. MAE must be capped at R (1.0), not 3.0.
    intent = EntryIntent(instrument="XAUUSD", direction=Direction.LONG,
                         entry_price=2000.0, stop_loss=1999.0,
                         entry_time_ms=1_500_000_000_000, reason="test")
    spike = _c(o=1999.5, h=2000.0, l=1997.0, cl=1998.0)   # low 1997 hits stop 1999
    t = simulate_entry(intent, [spike], FixedRR(2.0), risk_pct=1.0,
                       ref_balance=100_000.0, cost_model=COST_MODEL_ZERO)
    assert t is not None
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.exit_price == 1999.0            # realized at the stop level
    assert t.mae_price == 1.0                # capped at R, NOT 3.0


def test_sub_R_excursion_unchanged():
    # Long entry 2000, stop 1990 -> R = 10. Price dips only 2 (0.2R) then hits TP
    # at 2020 (2R). MAE (2.0) is below R so it must pass through unchanged.
    intent = EntryIntent(instrument="XAUUSD", direction=Direction.LONG,
                         entry_price=2000.0, stop_loss=1990.0,
                         entry_time_ms=1_500_000_000_000, reason="test")
    winner = _c(o=2000.0, h=2021.0, l=1998.0, cl=2019.0)  # dips to 1998, tags TP 2020
    t = simulate_entry(intent, [winner], FixedRR(2.0), risk_pct=1.0,
                       ref_balance=100_000.0, cost_model=COST_MODEL_ZERO)
    assert t is not None
    assert t.exit_reason is ExitReason.TAKE_PROFIT
    assert t.mae_price == 2.0                # < R (10), unchanged
