"""
Tests for the lean exit-model library (app.cfd_research.exit_models).

Deterministic hand-built candles verify each model's money-critical behavior:
fixed TP/SL, SL-priority on an ambiguous bar, breakeven protection, ATR-trailing
capture, time-stop, and scale-out. Zero-cost model so RR math is exact.
XAUUSD: $100/point, so a $10 stop at 1% of $100k sizes to exactly 1.0 lot.
"""

from __future__ import annotations

import math

from app.cfd_research.exit_models import (
    AtrTrailing,
    BreakevenAfter1R,
    EntryIntent,
    FixedRR,
    ScaleRunner,
    TimeStop,
    simulate_entry,
)
from app.cfd_execution.base import ExitReason
from app.cfd_risk.costs import COST_MODEL_ZERO
from app.cfd_strategy.base import Direction
from app.core.models import Candle, Timeframe

_MS = 300_000


def _c(i, o, h, l, cl):
    return Candle("ICMARKETS", "CFD", "XAUUSD", Timeframe.M5, i * _MS, o, h, l, cl, 0)


def _entry(entry=2400.0, stop=2390.0):
    return EntryIntent("XAUUSD", Direction.LONG, entry, stop, entry_time_ms=0.0)


def _run(model, future, atr=None):
    return simulate_entry(_entry(), future, model, risk_pct=1.0,
                          ref_balance=100_000.0, cost_model=COST_MODEL_ZERO, atr_at_entry=atr)


def test_fixed_rr_hits_tp():
    t = _run(FixedRR(2.0), [_c(1, 2400, 2425, 2399, 2422)])   # rallies through TP 2420
    assert t.exit_reason is ExitReason.TAKE_PROFIT
    assert math.isclose(t.exit_price, 2420.0)
    assert math.isclose(t.realized_rr, 2.0)
    assert t.lots == 1.0


def test_fixed_rr_hits_sl():
    t = _run(FixedRR(2.0), [_c(1, 2400, 2402, 2388, 2389)])   # dips below SL 2390
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert math.isclose(t.realized_rr, -1.0)


def test_sl_priority_on_ambiguous_bar():
    # One bar covers BOTH SL(2390) and TP(2420) -> stop wins.
    t = _run(FixedRR(2.0), [_c(1, 2400, 2425, 2385, 2400)])
    assert t.exit_reason is ExitReason.STOP_LOSS


def test_breakeven_protects_after_1r():
    # Rise past +1R (2410) arms breakeven; next bar falls back to entry -> exit ~0R.
    future = [_c(1, 2400, 2411, 2400, 2410), _c(2, 2410, 2410, 2400, 2401)]
    t = _run(BreakevenAfter1R(2.0), future)
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert abs(t.realized_rr) < 1e-6           # exited at breakeven, not a loss


def test_breakeven_vs_fixed_difference():
    # Same path: fixed would still be open/lose; breakeven banks 0 instead of -1.
    future = [_c(1, 2400, 2411, 2400, 2410), _c(2, 2410, 2410, 2388, 2389)]
    be = _run(BreakevenAfter1R(2.0), future)
    fixed = _run(FixedRR(2.0), future)
    assert math.isclose(be.realized_rr, 0.0, abs_tol=1e-6)     # stopped at entry (bar2 low 2400 first)
    assert math.isclose(fixed.realized_rr, -1.0)               # rode to original stop -> loss


def test_atr_trailing_captures_beyond_entry():
    # ATR 5, mult 2 -> trail 10. Rally to 2430 (trail->2420), reverse hits 2420 = +2R.
    future = [_c(1, 2400, 2430, 2399, 2429), _c(2, 2429, 2429, 2419, 2420)]
    t = _run(AtrTrailing(2.0), future, atr=5.0)
    assert t.exit_reason is ExitReason.TRAILING_STOP
    assert math.isclose(t.exit_price, 2420.0)
    assert math.isclose(t.realized_rr, 2.0)


def test_time_stop_exits_after_max_bars():
    m = TimeStop(2.0, max_bars=3)
    future = [_c(1, 2400, 2405, 2398, 2402), _c(2, 2402, 2406, 2399, 2404),
              _c(3, 2404, 2405, 2401, 2403)]   # never hits TP/SL
    t = _run(m, future)
    assert t.exit_reason is ExitReason.TIME_STOP
    assert math.isclose(t.exit_price, 2403.0)
    assert t.bars_held == 3


def test_scale_runner_books_half_at_2r():
    # ATR 5, mult 2 -> runner trail 10. Bar1 hits first TP 2420 (book 0.5, breakeven runner,
    # trail from 2422 -> 2412). Bar2 dips to 2411 -> runner out at 2412 (+1.2R).
    future = [_c(1, 2400, 2422, 2399, 2421), _c(2, 2421, 2421, 2411, 2412)]
    t = _run(ScaleRunner(2.0, 0.5, 2.0), future, atr=5.0)
    assert len(t.partials) == 2
    assert t.partials[0].reason is ExitReason.TAKE_PROFIT
    # 0.5 * 2R + 0.5 * 1.2R = 1.6R
    assert math.isclose(t.realized_rr, 1.6, abs_tol=1e-6)
