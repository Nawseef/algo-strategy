"""
Tests for the generalized research controls:
  * true cost-off model (COST_MODEL_RAW zeroes commission too),
  * strategy registry (multi-select + unknown guard),
  * out-of-sample discover/confirm split (partition + robust join).
"""

from __future__ import annotations

import types

import pytest

from app.cfd_research.challenge_sim import MonteCarloResult
from app.cfd_research.oos import validate_oos
from app.cfd_research.slice_scorer import SliceResult
from app.cfd_research.strategy_registry import available, build_variants
from app.cfd_risk.costs import (
    COST_MODEL_RAW,
    COST_MODEL_ZERO,
    calculate_trade_cost,
)
from app.cfd_risk.instruments import get_instrument
from app.core.models import Timeframe


# ─── Cost: true off vs legacy zero ───────────────────────────────────────────


def test_cost_raw_is_truly_zero():
    raw = calculate_trade_cost("EURUSD", lot_size=1.0, cost_model=COST_MODEL_RAW)
    assert raw.total_usd == 0.0
    assert raw.commission_usd == 0.0
    assert raw.spread_usd == 0.0
    assert raw.slippage_usd == 0.0


def test_cost_zero_still_charges_commission():
    sym = "EURUSD"
    inst = get_instrument(sym)
    z = calculate_trade_cost(sym, lot_size=1.0, cost_model=COST_MODEL_ZERO)
    # 'zero' zeroes spread/slippage but NOT commission (legacy behaviour).
    assert z.spread_usd == 0.0
    assert z.commission_usd == round(inst.commission_per_lot * 1.0, 2)


# ─── Strategy registry ───────────────────────────────────────────────────────


def test_registry_build_orb_variants():
    cfg = {"sessions": ["london", "new_york"],
           "timeframes": [Timeframe.M5, Timeframe.M15],
           "range_bars": 6, "buffer_frac": 0.0, "trend_ema": None}
    variants = build_variants(["orb"], cfg)
    assert len(variants) == 4                       # 2 sessions x 2 timeframes
    assert {v.timeframe for v in variants} == {Timeframe.M5, Timeframe.M15}
    assert all(v.__class__.__name__ == "SessionORB" for v in variants)


def test_registry_unknown_strategy_raises():
    with pytest.raises(ValueError):
        build_variants(["does_not_exist"], {"sessions": ["london"], "timeframes": []})


def test_registry_available():
    assert "orb" in available()


# ─── Out-of-sample split ─────────────────────────────────────────────────────


class _MC:
    def __init__(self, pr=0.7, br=0.0):
        self.pass_rate = pr
        self.blowup_rate = br
        self.account_ending_rate = br


class _Deploy:
    def __init__(self, dep):
        self.deployable = dep

    def flags(self):
        return "F C D Q"


class _Slice:
    """Minimal stand-in for SliceResult (only what validate_oos/format_oos read)."""
    def __init__(self, label, risk, qualifies):
        self._label = label
        self.risk_pct = risk
        self._q = qualifies
        self.mc = _MC()
        self.deploy = _Deploy(qualifies)
        self.passes_challenge = qualifies

    def label(self):
        return self._label

    @property
    def qualifies(self):
        return self._q


def test_oos_partition_and_robust_join():
    split_ms = 1000.0
    trades = [types.SimpleNamespace(entry_time_ms=t) for t in (500, 800, 1000, 1500)]
    captured = {}

    def score_discover(ts, ds, de):
        captured["discover"] = len(ts)
        return [_Slice("DE40 x", 1.0, True), _Slice("US30 x", 1.0, True)]

    def score_confirm(ts, ds, de):
        captured["confirm"] = len(ts)
        # DE40 survives (deployable in confirm); US30 absent (no trades) -> not robust.
        return [_Slice("DE40 x", 1.0, True)]

    rows, n_disc, n_conf = validate_oos(trades, split_ms, score_discover, score_confirm)

    # Partition: entry < split -> discover (500, 800); >= split -> confirm (1000, 1500).
    assert n_disc == 2 and n_conf == 2
    assert captured["discover"] == 2 and captured["confirm"] == 2

    by = {r.discover.label(): r for r in rows}
    assert by["DE40 x"].robust is True             # deployable in BOTH
    assert by["US30 x"].robust is False            # missing from confirm
    assert by["US30 x"].confirm is None
    # Robust row sorts first.
    assert rows[0].discover.label() == "DE40 x"


# ─── Daily-DD breaches count as account loss in the survival gate ────────────


def test_daily_breach_counts_as_account_loss():
    # 65% pass, only 1% max-DD breach, but 30% DAILY-DD breaches. A daily breach
    # terminates the eval too, so account-loss = 31% -> must NOT be deployable.
    mc = MonteCarloResult(runs=100, passed=65, failed_max=1, failed_daily=30,
                          timeouts=4, incompletes=0)
    assert mc.decisive_runs == 100
    assert mc.pass_rate == 0.65
    assert mc.blowup_rate == 0.01               # max-DD only (granular sub-metric)
    assert mc.daily_halt_rate == 0.30
    assert mc.account_ending_rate == 0.31       # max + daily = true account-loss rate

    r = SliceResult(key={}, trade_count=300, risk_pct=1.0, mc=mc)
    # Pre-fix (blowup=1% <= 5%, pass 65% >= 60%) this WRONGLY passed. Now the
    # 31% daily-driven account loss correctly fails the challenge.
    assert r.passes_challenge is False


def test_no_daily_breach_still_passes():
    mc = MonteCarloResult(runs=100, passed=95, failed_max=1, failed_daily=0, incompletes=0)
    r = SliceResult(key={}, trade_count=300, risk_pct=1.0, mc=mc)
    assert mc.account_ending_rate == 0.01
    assert r.passes_challenge is True
