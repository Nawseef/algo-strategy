"""
Tests for the prop-firm challenge simulator (app.cfd_research.challenge_sim).

Uses deterministic synthetic %-return streams so every pass/fail path is exact:
passing run, daily-DD breach, max-DD breach (static + trailing), min-trading-days
gate, risk scaling changing the outcome, a 2-phase challenge, and Monte-Carlo
aggregation. Plus one check of the SimulatedTrade -> % reduction.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_execution.base import ExitReason
from app.cfd_strategy.base import Direction
from app.cfd_research.challenge_sim import (
    ChallengeRules,
    Outcome,
    TradeReturn,
    from_simulated_trades,
    monte_carlo,
    simulate_challenge,
    simulate_phase,
)

_BASE = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


def _tr(day_offset: int, ret_pct: float, mae_ret_pct: float | None = None, intraday_i: int = 0):
    """Build a TradeReturn on a given day. mae defaults to |ret| for losers, else 0."""
    if mae_ret_pct is None:
        mae_ret_pct = abs(ret_pct) if ret_pct < 0 else 0.0
    dt = _BASE + timedelta(days=day_offset, minutes=intraday_i * 10)
    ms = dt.timestamp() * 1000
    return TradeReturn(
        entry_ms=ms, exit_ms=ms + 60_000, ret_pct=ret_pct,
        mae_ret_pct=mae_ret_pct, trading_day=dt.date(),
    )


def _rules(**kw):
    base = dict(phase1_target_pct=8.0, phase2_target_pct=5.0, daily_dd_pct=5.0,
               max_dd_pct=10.0, dd_mode="static", min_trading_days=0)
    base.update(kw)
    return ChallengeRules(**base)


# ─── Phase outcomes ──────────────────────────────────────────────────────────


def test_phase_passes_on_target():
    # +2% per day for 4 days -> +8% hits the phase-1 target.
    returns = [_tr(d, 2.0) for d in range(4)]
    r = simulate_phase(returns, 0, _rules(), target_pct=8.0)
    assert r.outcome is Outcome.PASS
    assert r.trading_days == 4
    assert r.end_return_pct >= 8.0 - 1e-9


def test_daily_dd_breach():
    # Two -3% trades same day -> intratrade low -6% breaches the 5% daily limit.
    returns = [_tr(0, -3.0, 3.0, intraday_i=0), _tr(0, -3.0, 3.0, intraday_i=1)]
    r = simulate_phase(returns, 0, _rules(), target_pct=8.0)
    assert r.outcome is Outcome.FAIL_DAILY_DD


def test_max_dd_breach_static():
    # -4%/day across days: daily never breaches (floor resets), cumulative hits -10%.
    returns = [_tr(d, -4.0, 4.0) for d in range(3)]
    r = simulate_phase(returns, 0, _rules(daily_dd_pct=6.0), target_pct=8.0)
    assert r.outcome is Outcome.FAIL_MAX_DD


def test_max_dd_breach_trailing():
    # Peak +6%, then a big adverse trade drops equity to -5% => 11% below peak.
    returns = [_tr(0, 6.0, 0.0), _tr(1, -11.0, 11.0)]
    r = simulate_phase(returns, 0, _rules(dd_mode="trailing", daily_dd_pct=20.0), target_pct=8.0)
    assert r.outcome is Outcome.FAIL_MAX_DD


def test_min_trading_days_gate():
    # Hits +2% target on day 0, but min_trading_days=3 forces it to keep going.
    returns = [_tr(0, 3.0, 0.0), _tr(1, 0.1, 0.0), _tr(2, 0.1, 0.0)]
    r = simulate_phase(returns, 0, _rules(min_trading_days=3), target_pct=2.0)
    assert r.outcome is Outcome.PASS
    assert r.trading_days == 3


def test_risk_scale_changes_outcome():
    # At full risk this blows the max DD; at half risk it does not.
    returns = [_tr(d, -4.0, 4.0) for d in range(3)]
    rules = _rules(daily_dd_pct=6.0)
    full = simulate_phase(returns, 0, rules, target_pct=8.0, risk_scale=1.0)
    half = simulate_phase(returns, 0, rules, target_pct=8.0, risk_scale=0.5)
    assert full.outcome is Outcome.FAIL_MAX_DD
    assert half.outcome is not Outcome.FAIL_MAX_DD   # survives (INCOMPLETE)


# ─── Full 2-phase challenge ──────────────────────────────────────────────────


def test_two_phase_challenge_pass():
    # 4 days +2% (=+8% P1), then 3 days +2% (=+6% >= 5% P2).
    returns = [_tr(d, 2.0) for d in range(4)] + [_tr(d, 2.0) for d in range(4, 7)]
    cr = simulate_challenge(returns, 0, _rules())
    assert cr.passed
    assert cr.phase2 is not None and cr.phase2.passed
    assert cr.total_trading_days == 7


def test_challenge_fails_if_phase1_blows():
    returns = [_tr(d, -4.0, 4.0) for d in range(3)]
    cr = simulate_challenge(returns, 0, _rules(daily_dd_pct=6.0))
    assert not cr.passed
    assert cr.blew_up
    assert cr.phase2 is None   # never reached phase 2


def test_one_step_challenge_when_phase2_zero():
    returns = [_tr(d, 2.0) for d in range(4)]
    cr = simulate_challenge(returns, 0, _rules(phase2_target_pct=0.0))
    assert cr.passed
    assert cr.phase2 is None


# ─── Monte-Carlo ─────────────────────────────────────────────────────────────


def test_monte_carlo_all_pass_when_always_profitable():
    # A long steady +2%/day stream: every start date should pass eventually.
    returns = [_tr(d, 2.0) for d in range(60)]
    mc = monte_carlo(returns, _rules(), step_days=7)
    assert mc.runs > 1
    assert mc.blowup_rate == 0.0
    assert mc.failed_daily == 0
    # Every run with enough forward data to finish passed; only late starts
    # (too few trades left to complete both phases) are INCOMPLETE.
    assert mc.passed == mc.runs - mc.incompletes
    assert mc.avg_days_to_pass > 0


def test_monte_carlo_all_blowup_when_always_losing():
    returns = [_tr(d, -4.0, 4.0) for d in range(60)]
    mc = monte_carlo(returns, _rules(daily_dd_pct=6.0), step_days=7)
    assert mc.runs > 1
    assert mc.pass_rate == 0.0
    assert mc.blowup_rate > 0.0


# ─── Reduction from SimulatedTrade ───────────────────────────────────────────


def test_from_simulated_trades_reduction():
    ms = _BASE.timestamp() * 1000
    t = SimulatedTrade(
        instrument="XAUUSD", direction=Direction.LONG, entry_price=2400.0,
        entry_time_ms=ms, exit_price=2420.0, exit_time_ms=ms + 60_000,
        exit_reason=ExitReason.TAKE_PROFIT, lots=1.0, planned_rr=2.0, realized_rr=2.0,
        pnl_price=20.0, pnl_usd=2000.0, cost_usd=7.0, net_pnl_usd=1993.0,
        mfe_price=20.0, mae_price=1.0, bars_held=3,
    )
    out = from_simulated_trades([t], ref_balance=100_000.0)
    assert len(out) == 1
    # XAUUSD point value $100/lot: net 1993 / 100k = 1.993%.
    assert abs(out[0].ret_pct - 1.993) < 1e-6
    # MAE 1.0 price * 100 * 1 lot + $7 cost = $107 -> 0.107%.
    assert abs(out[0].mae_ret_pct - 0.107) < 1e-6
    assert out[0].trading_day == _BASE.date()
