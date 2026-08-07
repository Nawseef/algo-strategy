"""
Slice scorer — "which slice actually passes the prop challenge?"

Takes tagged trades (from the entry replay) and groups them by any combination of
tag dimensions — instrument, session, regime, volatility, exit_model, timeframe —
then runs the challenge simulator on each slice and ranks by pass-rate / blow-up.
This is what turns raw trades into the answer you want:

    "ORB on XAUUSD, London, trend_up, breakeven exit  -> pass 71%, blowup 2%"

Each slice is scored with the SINGLE-STREAM challenge sim (a slice is a filtered
set of that strategy's own trades). To evaluate a chosen SET of slices as one
shared account, feed the winners to the portfolio simulator separately.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_research.challenge_sim import (
    ChallengeRules,
    MonteCarloResult,
    from_simulated_trades,
    monte_carlo,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

# The dimensions you can slice by (attributes on SimulatedTrade).
TAG_DIMENSIONS = ("instrument", "session", "regime", "volatility", "exit_model", "timeframe")


@dataclass
class SliceResult:
    key: dict[str, str]        # dimension -> value for this slice
    trade_count: int
    risk_pct: float
    mc: MonteCarloResult

    @property
    def sort_key(self) -> tuple:
        return (-self.mc.pass_rate, self.mc.blowup_rate, -self.mc.phase1_pass_rate)

    def label(self) -> str:
        return " ".join(f"{k}={v}" for k, v in self.key.items())

    def row(self) -> str:
        return (
            f"{self.label():55s} risk={self.risk_pct:>4.2f}% | n={self.trade_count:<5d} "
            f"pass={self.mc.pass_rate*100:5.1f}% P1={self.mc.phase1_pass_rate*100:5.1f}% "
            f"blowup={self.mc.blowup_rate*100:5.1f}% medDays={self.mc.median_days_to_pass:>3.0f} "
            f"worstDD={self.mc.worst_dd_pct:4.1f}%"
        )


def score_slices(
    trades: list[SimulatedTrade],
    dimensions: tuple[str, ...],
    rules: ChallengeRules,
    *,
    ref_balance: float = 100_000.0,
    ref_risk_pct: float = 1.0,               # risk the trades were sized at (in replay)
    risk_levels: tuple[float, ...] = (0.5, 1.0),
    step_days: int = 7,
    min_trades: int = 30,
    reset_utc_offset_hours: float = 0.0,
) -> list[SliceResult]:
    """Group trades by ``dimensions`` and challenge-score each slice at each risk."""
    for d in dimensions:
        if d not in TAG_DIMENSIONS:
            raise ValueError(f"unknown slice dimension {d!r}; valid: {TAG_DIMENSIONS}")

    groups: dict[tuple, list[SimulatedTrade]] = defaultdict(list)
    for t in trades:
        groups[tuple(getattr(t, d) for d in dimensions)].append(t)

    results: list[SliceResult] = []
    for key_vals, group in groups.items():
        if len(group) < min_trades:
            continue
        returns = from_simulated_trades(group, ref_balance, reset_utc_offset_hours)
        key = {d: v for d, v in zip(dimensions, key_vals)}
        for level in risk_levels:
            scale = level / ref_risk_pct if ref_risk_pct else 1.0
            mc = monte_carlo(returns, rules, risk_scale=scale, step_days=step_days)
            results.append(SliceResult(key=key, trade_count=len(group), risk_pct=level, mc=mc))

    results.sort(key=lambda r: r.sort_key)
    return results


def format_slices(results: list[SliceResult], top: int | None = 30) -> str:
    rows = results[:top] if top else results
    if not rows:
        return "(no slices with enough trades)"
    return "\n".join(r.row() for r in rows)
