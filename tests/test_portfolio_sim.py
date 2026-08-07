"""
Tests for the portfolio (shared-account) challenge simulator.

The headline test is ``test_combined_dd_breach_from_overlap``: two losing trades
that are each safe on their own breach the daily drawdown ONLY because they were
open at the same time. That combined-floating risk is the whole reason this
module exists, so it must be caught.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.cfd_research.challenge_sim import ChallengeRules, Outcome, TradeReturn, simulate_phase
from app.cfd_research.portfolio_sim import (
    PortfolioConstraints,
    PortfolioLeg,
    monte_carlo_portfolio,
    simulate_portfolio_challenge,
    simulate_portfolio_phase,
)

_BASE = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


def _ms(day: int, minute: int) -> float:
    return (_BASE + timedelta(days=day, minutes=minute)).timestamp() * 1000


def _leg(strat, inst, day, ret, mae=None, risk=1.0, start_min=0, dur_min=30):
    if mae is None:
        mae = abs(ret) if ret < 0 else 0.0
    entry = _ms(day, start_min)
    return PortfolioLeg(
        strategy_id=strat, instrument=inst, entry_ms=entry, exit_ms=entry + dur_min * 60_000,
        ret_pct=ret, mae_ret_pct=mae, risk_pct=risk,
        trading_day=(_BASE + timedelta(days=day)).date(),
    )


def _rules(**kw):
    base = dict(phase1_target_pct=8.0, phase2_target_pct=0.0, daily_dd_pct=5.0,
               max_dd_pct=10.0, dd_mode="static", min_trading_days=0)
    base.update(kw)
    return ChallengeRules(**base)


def _loose():
    return PortfolioConstraints(max_trades_per_day=10, max_concurrent_positions=10,
                                max_open_risk_pct=100.0, one_position_per_instrument=False)


# ─── The headline: combined drawdown ─────────────────────────────────────────


def test_combined_dd_breach_from_overlap():
    # Two -3% trades, each safe alone vs the 5% daily limit, but OPEN at the same
    # time -> combined worst floating -6% breaches daily DD.
    a = _leg("s1", "XAUUSD", 0, -3.0, 3.0, start_min=0, dur_min=60)
    b = _leg("s2", "XAGUSD", 0, -3.0, 3.0, start_min=30, dur_min=60)
    r = simulate_portfolio_phase([a, b], a.entry_ms, _rules(), _loose(), target_pct=8.0)
    assert r.outcome is Outcome.FAIL_DAILY_DD


def test_same_trade_alone_is_safe():
    # Control: just one of those -3% trades never breaches.
    a = _leg("s1", "XAUUSD", 0, -3.0, 3.0, start_min=0, dur_min=60)
    r = simulate_portfolio_phase([a], a.entry_ms, _rules(), _loose(), target_pct=8.0)
    assert r.outcome is not Outcome.FAIL_DAILY_DD


# ─── Entry constraints ───────────────────────────────────────────────────────


def test_trades_per_day_cap():
    # 5 sequential (non-overlapping) entries same day, cap 2 -> only 2 taken.
    legs = [_leg("s", "XAUUSD", 0, 0.5, 0.0, start_min=i * 40, dur_min=30) for i in range(5)]
    c = PortfolioConstraints(max_trades_per_day=2, max_concurrent_positions=10,
                             max_open_risk_pct=100.0, one_position_per_instrument=False)
    r = simulate_portfolio_phase(legs, legs[0].entry_ms, _rules(), c, target_pct=8.0)
    assert r.trades_taken == 2
    assert r.trades_skipped == 3


def test_concurrent_positions_cap():
    # 3 overlapping trades, cap 2 concurrent -> 3rd rejected.
    legs = [_leg("s", f"I{i}", 0, 0.5, 0.0, start_min=i * 5, dur_min=60) for i in range(3)]
    c = PortfolioConstraints(max_trades_per_day=10, max_concurrent_positions=2,
                             max_open_risk_pct=100.0, one_position_per_instrument=False)
    r = simulate_portfolio_phase(legs, legs[0].entry_ms, _rules(), c, target_pct=8.0)
    assert r.trades_taken == 2
    assert r.trades_skipped == 1


def test_one_position_per_instrument():
    # Two overlapping trades on the SAME instrument -> 2nd rejected.
    a = _leg("s1", "XAUUSD", 0, 0.5, 0.0, start_min=0, dur_min=60)
    b = _leg("s2", "XAUUSD", 0, 0.5, 0.0, start_min=20, dur_min=60)
    c = PortfolioConstraints(max_trades_per_day=10, max_concurrent_positions=10,
                             max_open_risk_pct=100.0, one_position_per_instrument=True)
    r = simulate_portfolio_phase([a, b], a.entry_ms, _rules(), c, target_pct=8.0)
    assert r.trades_taken == 1
    assert r.trades_skipped == 1


def test_open_risk_cap():
    # 4 overlapping trades at 1% risk each, cap 3% -> only 3 fit.
    legs = [_leg("s", f"I{i}", 0, 0.5, 0.0, risk=1.0, start_min=i * 5, dur_min=60) for i in range(4)]
    c = PortfolioConstraints(max_trades_per_day=10, max_concurrent_positions=10,
                             max_open_risk_pct=3.0, one_position_per_instrument=False)
    r = simulate_portfolio_phase(legs, legs[0].entry_ms, _rules(), c, target_pct=8.0)
    assert r.trades_taken == 3
    assert r.trades_skipped == 1


# ─── Equivalence to the single-stream sim when there is no overlap ────────────


def test_no_overlap_matches_single_stream():
    # Sequential winners (one per day, never overlapping): the shared-account sim
    # should behave like the single-stream challenge sim.
    port_legs = [_leg("s", "XAUUSD", d, 2.0, 0.0, start_min=0, dur_min=10) for d in range(4)]
    pr = simulate_portfolio_phase(port_legs, port_legs[0].entry_ms, _rules(), _loose(), target_pct=8.0)

    single = [TradeReturn(entry_ms=l.entry_ms, exit_ms=l.exit_ms, ret_pct=2.0,
                          mae_ret_pct=0.0, trading_day=l.trading_day) for l in port_legs]
    sr = simulate_phase(single, 0, _rules(), target_pct=8.0)

    assert pr.outcome is Outcome.PASS
    assert sr.outcome is Outcome.PASS
    assert pr.trading_days == sr.trading_days == 4


# ─── Two-phase + Monte-Carlo ─────────────────────────────────────────────────


def test_two_phase_portfolio_pass():
    legs = [_leg("s", "XAUUSD", d, 2.0, 0.0, dur_min=10) for d in range(7)]
    cr = simulate_portfolio_challenge(legs, legs[0].entry_ms,
                                      _rules(phase2_target_pct=5.0), _loose())
    assert cr.passed
    assert cr.phase2 is not None and cr.phase2.passed


def test_monte_carlo_blowup_on_overlapping_losers():
    # Every day, two overlapping -3% trades -> combined daily breach from any start.
    legs = []
    for d in range(40):
        legs.append(_leg("s1", "XAUUSD", d, -3.0, 3.0, start_min=0, dur_min=60))
        legs.append(_leg("s2", "XAGUSD", d, -3.0, 3.0, start_min=30, dur_min=60))
    legs.sort(key=lambda l: l.entry_ms)
    mc = monte_carlo_portfolio(legs, _rules(), _loose(), step_days=7)
    assert mc.runs > 1
    assert mc.pass_rate == 0.0
    assert mc.daily_halt_rate > 0.0
