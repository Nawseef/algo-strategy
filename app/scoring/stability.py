"""
Stability analysis — temporal consistency scoring.

From File 3:
    "Split history into periods (monthly/quarterly).
     Check consistency of expectancy, win rate, profit factor.
     Penalize variants that earn everything in one short period.
     Reward variants that perform across many periods."

The stability score (0-100) measures how consistently a variant
performs across time windows. A variant that earns 100 points
spread evenly over 5 months scores higher than one that earns
100 points all in a single lucky week.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from app.scoring.metrics import compute_metrics, VariantMetrics


@dataclass
class StabilityResult:
    """Result of stability analysis for a variant."""

    stability_score: float = 0.0  # 0-100
    periods_analyzed: int = 0
    periods_profitable: int = 0
    consistency_of_expectancy: float = 0.0  # coefficient of variation (lower = more consistent)
    consistency_of_winrate: float = 0.0
    worst_period_pnl: float = 0.0
    best_period_pnl: float = 0.0
    pnl_concentration: float = 0.0  # what % of total PnL came from best period


def compute_stability(
    trades: list[dict],
    exit_model_key: str,
    period_type: str = "monthly",
) -> StabilityResult:
    """
    Compute stability score by splitting trades into time periods.

    Args:
        trades: List of trade dicts (must have 'entry_time_ms' and exit result columns).
        exit_model_key: Which exit model's PnL to use (e.g. "rr2_result").
        period_type: "monthly" or "weekly".

    Returns:
        StabilityResult with score and breakdown.
    """
    result = StabilityResult()

    if not trades:
        return result

    # Group trades by period
    period_trades: dict[str, list[float]] = defaultdict(list)

    for trade in trades:
        pnl = trade.get(exit_model_key)
        if pnl is None:
            continue

        entry_ms = trade.get("entry_time_ms", 0)
        if entry_ms == 0:
            continue

        dt = datetime.fromtimestamp(entry_ms / 1000)

        if period_type == "monthly":
            period_key = dt.strftime("%Y-%m")
        elif period_type == "weekly":
            iso_year, iso_week, _ = dt.isocalendar()
            period_key = f"{iso_year}-W{iso_week:02d}"
        else:
            period_key = dt.strftime("%Y-%m")

        period_trades[period_key].append(pnl)

    if len(period_trades) < 2:
        # Need at least 2 periods for stability analysis
        result.periods_analyzed = len(period_trades)
        if period_trades:
            only_pnls = list(period_trades.values())[0]
            result.stability_score = 30.0  # Low score for single period
            result.best_period_pnl = sum(only_pnls)
            result.worst_period_pnl = sum(only_pnls)
        return result

    # Compute metrics per period
    period_pnls: list[float] = []
    period_winrates: list[float] = []
    period_expectancies: list[float] = []

    for period_key, pnls in sorted(period_trades.items()):
        total_pnl = sum(pnls)
        period_pnls.append(total_pnl)

        metrics = compute_metrics(pnls)
        period_winrates.append(metrics.win_rate)
        period_expectancies.append(metrics.expectancy)

    result.periods_analyzed = len(period_pnls)
    result.periods_profitable = sum(1 for p in period_pnls if p > 0)
    result.best_period_pnl = max(period_pnls)
    result.worst_period_pnl = min(period_pnls)

    # PnL concentration: what % of total came from best period
    total_pnl = sum(period_pnls)
    if total_pnl > 0:
        result.pnl_concentration = result.best_period_pnl / total_pnl
    elif total_pnl < 0:
        result.pnl_concentration = 1.0  # All periods losing

    # Consistency metrics (coefficient of variation — lower = more consistent)
    result.consistency_of_expectancy = _coefficient_of_variation(period_expectancies)
    result.consistency_of_winrate = _coefficient_of_variation(period_winrates)

    # ─── Compute stability score (0-100) ─────────────────────────────────
    score = 0.0

    # Factor 1: Profitable period ratio (0-30 points)
    profitable_ratio = result.periods_profitable / result.periods_analyzed
    score += profitable_ratio * 30.0

    # Factor 2: Low concentration (0-25 points)
    # Concentration = 1.0 means all profit from one period (bad)
    # Concentration = 0.2 means evenly spread (good for 5 periods)
    ideal_concentration = 1.0 / result.periods_analyzed
    if result.pnl_concentration <= ideal_concentration * 2:
        score += 25.0  # Very well distributed
    elif result.pnl_concentration <= 0.5:
        score += 15.0  # Acceptable
    elif result.pnl_concentration <= 0.75:
        score += 5.0   # Concentrated

    # Factor 3: Consistency of expectancy (0-25 points)
    # CV < 0.5 = very consistent, CV > 2.0 = very inconsistent
    if result.consistency_of_expectancy < 0.5:
        score += 25.0
    elif result.consistency_of_expectancy < 1.0:
        score += 15.0
    elif result.consistency_of_expectancy < 2.0:
        score += 5.0

    # Factor 4: No catastrophic periods (0-20 points)
    # Penalize if worst period lost more than 50% of net profit
    if total_pnl > 0:
        worst_ratio = abs(result.worst_period_pnl) / total_pnl if result.worst_period_pnl < 0 else 0.0
        if worst_ratio < 0.2:
            score += 20.0  # Worst period was small loss
        elif worst_ratio < 0.5:
            score += 10.0  # Moderate worst period
    elif all(p >= 0 for p in period_pnls):
        score += 20.0  # No losing periods at all

    result.stability_score = min(score, 100.0)

    return result


def _coefficient_of_variation(values: list[float]) -> float:
    """
    Coefficient of variation = std / |mean|.
    Lower = more consistent. Returns 0 if mean is 0.
    """
    if not values or len(values) < 2:
        return 0.0

    mean = sum(values) / len(values)
    if abs(mean) < 1e-10:
        return 0.0

    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    std = variance ** 0.5

    return std / abs(mean)
