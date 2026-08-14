"""
Tests for the concurrency-aware challenge sim (#4): when a single slice holds
overlapping trades, their worst-case simultaneous drawdown must STACK — a dip
that neither trade would breach alone can breach together.
"""

from __future__ import annotations

from datetime import date

from app.cfd_research.challenge_sim import (
    ChallengeRules,
    Outcome,
    TradeReturn,
    _has_overlap,
    monte_carlo,
    simulate_challenge,
)

_DAY = date(2020, 1, 1)


def _r(entry_ms, exit_ms, ret_pct, mae_ret_pct):
    return TradeReturn(entry_ms=entry_ms, exit_ms=exit_ms, ret_pct=ret_pct,
                       mae_ret_pct=mae_ret_pct, trading_day=_DAY)


# Two trades open at the same time, each 6% adverse. Alone: 6% < 10% max DD.
# Together: 12% > 10% -> only the concurrency-aware sim catches the blow-up.
_OVERLAP = [_r(0, 200, 1.0, 6.0), _r(100, 300, 1.0, 6.0)]
# daily DD disabled so we isolate the MAX-DD stacking behaviour.
_RULES = ChallengeRules(phase1_target_pct=8.0, phase2_target_pct=0.0,
                        daily_dd_pct=100.0, max_dd_pct=10.0, dd_mode="static")


def test_overlap_detection():
    assert _has_overlap(_OVERLAP) is True
    assert _has_overlap([_r(0, 100, 1.0, 6.0), _r(150, 250, 1.0, 6.0)]) is False


def test_sequential_misses_stacked_drawdown():
    cr = simulate_challenge(_OVERLAP, 0, _RULES, concurrent=False)
    # Sequential sees each 6% dip alone -> no max-DD breach.
    assert cr.outcome is not Outcome.FAIL_MAX_DD
    assert not cr.blew_up


def test_concurrent_catches_stacked_drawdown():
    cr = simulate_challenge(_OVERLAP, 0, _RULES, concurrent=True)
    # Both open at once -> 12% combined dip -> max-DD breach.
    assert cr.outcome is Outcome.FAIL_MAX_DD
    assert cr.blew_up


def test_monte_carlo_auto_enables_concurrency_on_overlap():
    auto = monte_carlo(_OVERLAP, _RULES)                 # auto-detect -> concurrent
    forced_seq = monte_carlo(_OVERLAP, _RULES, concurrent=False)
    assert auto.failed_max >= 1                           # blow-up caught
    assert forced_seq.failed_max == 0                     # sequential misses it


def test_monte_carlo_non_overlap_unaffected():
    # A clean one-at-a-time slice: auto must behave exactly like sequential.
    clean = [_r(0, 100, 1.0, 4.0), _r(150, 250, 1.0, 4.0)]
    assert _has_overlap(clean) is False
    auto = monte_carlo(clean, _RULES)
    forced_seq = monte_carlo(clean, _RULES, concurrent=False)
    assert auto.failed_max == forced_seq.failed_max
    assert auto.passed == forced_seq.passed
    assert auto.runs == forced_seq.runs
