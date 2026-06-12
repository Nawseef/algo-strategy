"""
Core performance metrics for variant scoring.

From File 3:
    - Trade count (minimum threshold)
    - Win rate
    - Average win / average loss
    - Expectancy
    - Profit factor
    - Net PnL
    - Max drawdown
    - Recovery factor

All metrics are computed from a list of PnL values (one per trade).
Pure functions — no state, no DB access.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VariantMetrics:
    """Complete metrics for a variant (or variant + filter combination)."""

    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0

    avg_win: float = 0.0
    avg_loss: float = 0.0

    expectancy: float = 0.0  # (WR × avg_win) - (LR × avg_loss)
    profit_factor: float = 0.0  # gross profit / gross loss
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    recovery_factor: float = 0.0  # net_pnl / max_drawdown

    gross_profit: float = 0.0
    gross_loss: float = 0.0

    # Risk-adjusted
    sharpe_ratio: float = 0.0  # mean / std of PnL
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0

    # Best exit model analysis
    best_exit_model: str = ""
    best_exit_expectancy: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dict for DB storage."""
        return {
            "trade_count": self.trade_count,
            "win_rate": self.win_rate,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "expectancy": self.expectancy,
            "profit_factor": self.profit_factor,
            "net_pnl": self.net_pnl,
            "max_drawdown": self.max_drawdown,
            "recovery_factor": self.recovery_factor,
            "best_exit_model": self.best_exit_model,
            "best_exit_expectancy": self.best_exit_expectancy,
        }


def compute_metrics(pnl_values: list[float]) -> VariantMetrics:
    """
    Compute all performance metrics from a list of trade PnL values.

    Args:
        pnl_values: List of PnL (in points) for each trade. Positive = win.

    Returns:
        VariantMetrics with all fields populated.
    """
    metrics = VariantMetrics()

    if not pnl_values:
        return metrics

    metrics.trade_count = len(pnl_values)

    # Separate wins and losses
    wins = [p for p in pnl_values if p > 0]
    losses = [p for p in pnl_values if p < 0]
    breakeven = [p for p in pnl_values if p == 0]

    metrics.win_count = len(wins)
    metrics.loss_count = len(losses)
    metrics.win_rate = metrics.win_count / metrics.trade_count if metrics.trade_count > 0 else 0.0

    # Averages
    metrics.avg_win = sum(wins) / len(wins) if wins else 0.0
    metrics.avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0  # stored as positive

    # Gross profit/loss
    metrics.gross_profit = sum(wins)
    metrics.gross_loss = abs(sum(losses))

    # Net PnL
    metrics.net_pnl = sum(pnl_values)

    # Expectancy: (WR × avg_win) - (LR × avg_loss)
    loss_rate = metrics.loss_count / metrics.trade_count if metrics.trade_count > 0 else 0.0
    metrics.expectancy = (metrics.win_rate * metrics.avg_win) - (loss_rate * metrics.avg_loss)

    # Profit factor
    metrics.profit_factor = (
        metrics.gross_profit / metrics.gross_loss
        if metrics.gross_loss > 0
        else float("inf") if metrics.gross_profit > 0
        else 0.0
    )

    # Max drawdown (from cumulative equity curve)
    metrics.max_drawdown = _compute_max_drawdown(pnl_values)

    # Recovery factor
    metrics.recovery_factor = (
        metrics.net_pnl / metrics.max_drawdown
        if metrics.max_drawdown > 0
        else float("inf") if metrics.net_pnl > 0
        else 0.0
    )

    # Sharpe ratio (simplified: mean / std of trade PnLs)
    if len(pnl_values) > 1:
        mean_pnl = metrics.net_pnl / metrics.trade_count
        variance = sum((p - mean_pnl) ** 2 for p in pnl_values) / (metrics.trade_count - 1)
        std_pnl = variance ** 0.5
        metrics.sharpe_ratio = mean_pnl / std_pnl if std_pnl > 0 else 0.0

    # Consecutive wins/losses
    metrics.max_consecutive_wins = _max_consecutive(pnl_values, positive=True)
    metrics.max_consecutive_losses = _max_consecutive(pnl_values, positive=False)

    return metrics


def compute_metrics_for_exit_model(
    trades_with_exits: list[dict], exit_model_key: str
) -> VariantMetrics:
    """
    Compute metrics using a specific exit model's PnL for each trade.

    Args:
        trades_with_exits: List of joined trade+exit_result dicts.
        exit_model_key: Column name like "rr2_result", "be_atr_trail_result", etc.

    Returns:
        VariantMetrics for that exit model.
    """
    pnl_values = []
    for t in trades_with_exits:
        pnl = t.get(exit_model_key)
        if pnl is not None:
            pnl_values.append(pnl)

    return compute_metrics(pnl_values)


def _compute_max_drawdown(pnl_values: list[float]) -> float:
    """
    Compute max drawdown from a series of trade PnLs.
    Drawdown = peak equity - current equity.
    """
    if not pnl_values:
        return 0.0

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0

    for pnl in pnl_values:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    return max_dd


def _max_consecutive(pnl_values: list[float], positive: bool) -> int:
    """Count max consecutive wins (positive=True) or losses (positive=False)."""
    max_streak = 0
    current_streak = 0

    for pnl in pnl_values:
        if (positive and pnl > 0) or (not positive and pnl < 0):
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    return max_streak
