"""
Tests for trade persistence (app/cfd_research/trade_store.py):
  * round-trip fidelity (every scored field survives save -> load),
  * score PARITY (scoring the loaded trades == scoring the originals),
  * gzip transparency.

If these hold, --score-from re-slicing is guaranteed identical to a fresh run's
scoring — the whole point of persistence.
"""

from __future__ import annotations

import pytest

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_execution.base import ExitReason
from app.cfd_research.challenge_sim import ChallengeRules
from app.cfd_research.slice_scorer import score_slices
from app.cfd_research.trade_store import load_trades, save_trades
from app.cfd_strategy.base import Direction

_SCORED_FIELDS = (
    "instrument", "entry_price", "entry_time_ms", "exit_price", "exit_time_ms",
    "lots", "planned_rr", "realized_rr", "pnl_price", "pnl_usd", "cost_usd",
    "net_pnl_usd", "mfe_price", "mae_price", "bars_held", "closed",
    "strategy_id", "session", "regime", "volatility", "exit_model", "timeframe",
)


def _mk(k, model="fixed_rr2", regime="trend_up", vol="normalVol", pnl=50.0, tf="30m"):
    t0 = 1_500_000_000_000
    return SimulatedTrade(
        instrument="DE40",
        direction=Direction.LONG if k % 2 == 0 else Direction.SHORT,
        entry_price=18000.0 + k, entry_time_ms=t0 + k * 3_600_000,
        exit_price=18010.0 + k, exit_time_ms=t0 + k * 3_600_000 + 300_000,
        exit_reason=ExitReason.TAKE_PROFIT if pnl > 0 else ExitReason.STOP_LOSS,
        lots=1.0, planned_rr=2.0, realized_rr=1.0 if pnl > 0 else -1.0,
        pnl_price=10.0, pnl_usd=pnl + 2, cost_usd=2.0, net_pnl_usd=pnl,
        mfe_price=15.0, mae_price=8.0, bars_held=5, closed=True,
        strategy_id="orb_london_6b", session="london", regime=regime,
        volatility=vol, exit_model=model, timeframe=tf,
    )


def _trades():
    out = []
    for k in range(60):
        out.append(_mk(k, regime=["trend_up", "trend_down", "range"][k % 3],
                       vol=["loVol", "normalVol", "hiVol"][k % 3],
                       pnl=60.0 if k % 3 else -40.0))
    return out


def test_round_trip_fidelity(tmp_path):
    trades = _trades()
    path = str(tmp_path / "t.jsonl")
    meta = save_trades(path, trades, ref_balance=100_000.0, ref_risk_pct=1.0,
                       data_start_ms=1_400_000_000_000, data_end_ms=1_600_000_000_000)
    assert meta["count"] == len(trades)

    loaded, lmeta = load_trades(path)
    assert len(loaded) == len(trades)
    assert lmeta["ref_balance"] == 100_000.0
    assert lmeta["ref_risk_pct"] == 1.0
    assert lmeta["data_start_ms"] == 1_400_000_000_000

    for a, b in zip(trades, loaded):
        for f in _SCORED_FIELDS:
            assert getattr(a, f) == getattr(b, f), f"field {f} mismatch"
        assert a.direction is b.direction        # enum restored by name
        assert a.exit_reason is b.exit_reason
        assert b.partials == []                   # dropped by design


def _score(trades):
    rules = ChallengeRules()
    dims = ("instrument", "strategy_id", "timeframe", "regime", "volatility", "exit_model")
    return score_slices(trades, dims, rules, ref_risk_pct=1.0, risk_levels=(0.5, 1.0),
                        min_trades=5)


def test_score_parity_after_persist(tmp_path):
    trades = _trades()
    path = str(tmp_path / "t.jsonl")
    save_trades(path, trades, ref_balance=100_000.0, ref_risk_pct=1.0)
    loaded, _ = load_trades(path)

    orig = _score(trades)
    rep = _score(loaded)
    assert len(orig) == len(rep)
    # Compare the scored slices field-by-field (deterministic).
    def key(r):
        return (r.label(), r.risk_pct)
    o = {key(r): r for r in orig}
    p = {key(r): r for r in rep}
    assert set(o) == set(p)
    for k in o:
        assert o[k].trade_count == p[k].trade_count
        assert o[k].mc.pass_rate == p[k].mc.pass_rate
        assert o[k].mc.blowup_rate == p[k].mc.blowup_rate
        assert o[k].deploy.deployable == p[k].deploy.deployable


def test_gzip_round_trip(tmp_path):
    trades = _trades()
    path = str(tmp_path / "t.jsonl.gz")
    save_trades(path, trades, ref_balance=100_000.0, ref_risk_pct=1.0)
    loaded, meta = load_trades(path)
    assert len(loaded) == len(trades)
    assert meta["count"] == len(trades)


def test_bad_schema_raises(tmp_path):
    path = str(tmp_path / "bad.jsonl")
    with open(path, "w") as fh:
        fh.write('{"_schema": "something_else"}\n')
    with pytest.raises(ValueError):
        load_trades(path)
