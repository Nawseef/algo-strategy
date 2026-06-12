"""
Regime analysis — find which market conditions each variant performs best in.

From File 3:
    "Using stored metadata: Find best/worst regime for each strategy.
     Examples: Gap > 1%, 1H Bullish, Morning Session."

Groups trades by metadata dimensions and computes metrics per group.
Answers: "In what conditions does this variant make money?"
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.scoring.metrics import compute_metrics, VariantMetrics


@dataclass
class RegimePerformance:
    """Performance of a variant in a specific regime."""

    regime_name: str = ""
    regime_value: str = ""
    trade_count: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0
    net_pnl: float = 0.0
    profit_factor: float = 0.0


@dataclass
class RegimeAnalysis:
    """Full regime analysis for a variant."""

    best_regime: RegimePerformance | None = None
    worst_regime: RegimePerformance | None = None
    all_regimes: list[RegimePerformance] = field(default_factory=list)

    # Summary
    regime_count: int = 0
    profitable_regimes: int = 0


# Metadata dimensions to analyze
REGIME_DIMENSIONS = [
    "session",           # MORNING / MIDDAY / CLOSING
    "day_of_week",       # MON / TUE / ...
    "gap_direction",     # UP / DOWN / FLAT
    "volatility_regime", # LOW / NORMAL / HIGH
    "htf_trend_1h",      # BULLISH / BEARISH / NEUTRAL
    "market_structure",  # TRENDING / RANGING / TRANSITIONING
    "instrument",        # NIFTY / BANKNIFTY / RELIANCE / etc.
]


def compute_regime_analysis(
    trades: list[dict],
    exit_model_key: str,
) -> RegimeAnalysis:
    """
    Analyze variant performance across all metadata dimensions.

    For each dimension (session, day, gap, volatility, trend, structure),
    groups trades by the dimension value and computes metrics.

    Args:
        trades: List of joined trade+exit dicts.
        exit_model_key: Which exit column to use for PnL.

    Returns:
        RegimeAnalysis with best/worst regimes identified.
    """
    analysis = RegimeAnalysis()
    all_performances: list[RegimePerformance] = []

    for dimension in REGIME_DIMENSIONS:
        # Group trades by dimension value
        groups: dict[str, list[float]] = defaultdict(list)

        for trade in trades:
            pnl = trade.get(exit_model_key)
            dim_value = trade.get(dimension, "")

            if pnl is None or not dim_value:
                continue

            groups[dim_value].append(pnl)

        # Compute metrics per group
        for dim_value, pnls in groups.items():
            if len(pnls) < 10:  # Minimum 10 trades for statistical validity
                continue

            metrics = compute_metrics(pnls)

            perf = RegimePerformance(
                regime_name=dimension,
                regime_value=dim_value,
                trade_count=metrics.trade_count,
                win_rate=metrics.win_rate,
                expectancy=metrics.expectancy,
                net_pnl=metrics.net_pnl,
                profit_factor=metrics.profit_factor,
            )
            all_performances.append(perf)

    if not all_performances:
        return analysis

    # Sort by expectancy
    all_performances.sort(key=lambda p: p.expectancy, reverse=True)

    analysis.all_regimes = all_performances
    analysis.regime_count = len(all_performances)
    analysis.profitable_regimes = sum(1 for p in all_performances if p.net_pnl > 0)

    # Best and worst
    analysis.best_regime = all_performances[0] if all_performances else None
    analysis.worst_regime = all_performances[-1] if all_performances else None

    return analysis


def compute_regime_summary(trades: list[dict], exit_model_key: str) -> dict[str, dict[str, float]]:
    """
    Compact regime summary — returns nested dict for quick lookup.

    Returns:
        {
            "session": {"MORNING": 2.5, "MIDDAY": -1.0, "CLOSING": 1.2},
            "volatility_regime": {"LOW": 3.0, "HIGH": -0.5},
            ...
        }
    Where values are expectancy per regime.
    """
    summary: dict[str, dict[str, float]] = {}

    for dimension in REGIME_DIMENSIONS:
        groups: dict[str, list[float]] = defaultdict(list)

        for trade in trades:
            pnl = trade.get(exit_model_key)
            dim_value = trade.get(dimension, "")
            if pnl is not None and dim_value:
                groups[dim_value].append(pnl)

        dim_summary: dict[str, float] = {}
        for dim_value, pnls in groups.items():
            if len(pnls) >= 3:
                metrics = compute_metrics(pnls)
                dim_summary[dim_value] = metrics.expectancy

        if dim_summary:
            summary[dimension] = dim_summary

    return summary
