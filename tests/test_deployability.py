"""
Tests for the deployability gates (frequency / consistency / concentration /
quality) applied to a slice's trade list before it's considered deployable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_execution.base import ExitReason
from app.cfd_research.deployability import (
    compute_deployability,
    portfolio_trades_per_month,
)
from app.cfd_strategy.base import Direction


def _ms(y, mo, d, h=12):
    return datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp() * 1000


def _trade(entry_ms, net=100.0):
    return SimulatedTrade(
        instrument="XAUUSD", direction=Direction.LONG,
        entry_price=2000.0, entry_time_ms=entry_ms,
        exit_price=2010.0, exit_time_ms=entry_ms + 3_600_000,
        exit_reason=ExitReason.TAKE_PROFIT, lots=1.0,
        planned_rr=2.0, realized_rr=2.0 if net > 0 else -1.0,
        pnl_price=0.0, pnl_usd=net, cost_usd=0.0, net_pnl_usd=net,
        mfe_price=0.0, mae_price=0.0, bars_held=1,
    )


# ─── Frequency ───────────────────────────────────────────────────

def test_frequency_pass_and_fail():
    # 6 trades across ~1 month -> ~6/mo -> pass.
    dense = [_trade(_ms(2022, 1, d)) for d in range(1, 25, 4)]  # 6 trades in Jan
    assert compute_deployability(dense).pass_frequency is True

    # 3 trades across 3 months -> ~1/mo -> fail.
    sparse = [_trade(_ms(2022, 1, 5)), _trade(_ms(2022, 2, 5)), _trade(_ms(2022, 3, 5))]
    assert compute_deployability(sparse).pass_frequency is False


# ─── Consistency (>=10 active months per full year) ──────────────

def _year_trades(year, months_with_enough, per_month=6):
    """Give `months_with_enough` months >=per_month trades; the rest get 1."""
    trades = []
    for m in range(1, 13):
        n = per_month if m <= months_with_enough else 1
        for d in range(1, n + 1):
            trades.append(_trade(_ms(year, m, d)))
    return trades


def test_consistency_pass():
    # Bound 2022 & 2023 as full years; both have all 12 months active.
    trades = [_trade(_ms(2021, 6, 1))]
    trades += _year_trades(2022, 12)
    trades += _year_trades(2023, 12)
    trades += [_trade(_ms(2024, 6, 1))]
    m = compute_deployability(trades)
    assert 2022 in m.full_years and 2023 in m.full_years
    assert m.min_full_year_active_months == 12
    assert m.pass_consistency is True


def test_consistency_fail_when_a_year_has_too_few_active_months():
    trades = [_trade(_ms(2021, 6, 1))]
    trades += _year_trades(2022, 9)     # only 9 active months in 2022
    trades += _year_trades(2023, 12)
    trades += [_trade(_ms(2024, 6, 1))]
    m = compute_deployability(trades)
    assert m.min_full_year_active_months == 9
    assert m.pass_consistency is False


def test_consistency_not_judged_without_a_full_year():
    # Only a partial year of data -> can't judge consistency -> don't fail on it.
    trades = [_trade(_ms(2022, m, d)) for m in range(1, 4) for d in range(1, 7)]
    m = compute_deployability(trades)
    assert m.full_years == []
    assert m.pass_consistency is True


# ─── Day concentration (<=30% of a month on one day) ─────────────

def test_concentration_fail_on_event_day_cluster():
    # 10 trades in a month, 4 of them on the same day -> 40% -> fail.
    trades = [_trade(_ms(2022, 1, d)) for d in range(1, 7)]           # 6 distinct days
    trades += [_trade(_ms(2022, 1, 10, h)) for h in range(9, 13)]     # 4 on Jan 10
    m = compute_deployability(trades)
    assert m.worst_month_day_share >= 0.4 - 1e-9
    assert m.pass_concentration is False


def test_concentration_pass_when_spread():
    # 10 trades in a month, max 2 on any day -> 20% -> pass.
    trades = []
    for d in range(1, 6):
        trades.append(_trade(_ms(2022, 1, d, 9)))
        trades.append(_trade(_ms(2022, 1, d, 15)))
    m = compute_deployability(trades)
    assert m.worst_month_day_share <= 0.2 + 1e-9
    assert m.pass_concentration is True


# ─── Quality (WR>=40% OR expectancy>0) ───────────────────────────

def test_quality_pass_on_win_rate():
    trades = [_trade(_ms(2022, 1, d), net=100.0) for d in range(1, 6)]     # 5 wins
    trades += [_trade(_ms(2022, 1, d), net=-50.0) for d in range(6, 11)]   # 5 losses
    m = compute_deployability(trades)          # WR = 50%
    assert m.pass_quality is True


def test_quality_pass_on_expectancy_despite_low_wr():
    # 3 wins of +500, 7 losses of -100 -> WR 30% but expectancy +80 -> pass.
    trades = [_trade(_ms(2022, 1, d), net=500.0) for d in range(1, 4)]
    trades += [_trade(_ms(2022, 1, d), net=-100.0) for d in range(4, 11)]
    m = compute_deployability(trades)
    assert m.win_rate < 0.40
    assert m.expectancy_usd > 0
    assert m.pass_quality is True


def test_quality_fail_low_wr_and_negative_expectancy():
    trades = [_trade(_ms(2022, 1, d), net=100.0) for d in range(1, 4)]     # 3 wins
    trades += [_trade(_ms(2022, 1, d), net=-100.0) for d in range(4, 11)]  # 7 losses
    m = compute_deployability(trades)
    assert m.win_rate < 0.40
    assert m.expectancy_usd < 0
    assert m.pass_quality is False


# ─── Portfolio frequency helper ──────────────────────────────────

def test_portfolio_trades_per_month():
    from app.cfd_research.portfolio_sim import PortfolioLeg
    from datetime import date

    legs = [
        PortfolioLeg("s", "XAUUSD", _ms(2022, 1, d), _ms(2022, 1, d) + 3600_000,
                     1.0, 0.5, 0.5, date(2022, 1, d))
        for d in range(1, 25)  # 24 legs in ~1 month
    ]
    assert portfolio_trades_per_month(legs) > 12.0
