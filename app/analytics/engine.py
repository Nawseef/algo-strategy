"""
Analytics engine for trading performance evaluation.

Computes key metrics from closed positions:
- Win rate
- Expectancy
- Profit factor
- Max drawdown
- Average win / average loss
- Sharpe-like ratio
- Time-based performance
- Per-strategy breakdown

Can operate on:
1. In-memory positions (from PaperTradingEngine)
2. Historical positions (from TradeStore/SQLite)
"""

from dataclasses import dataclass, field

from app.core.models import Position
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PerformanceReport:
    """Complete performance report for a set of trades."""

    # Basic counts
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    breakeven: int = 0

    # Rates
    win_rate: float = 0.0  # percentage

    # PnL
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    # Ratios
    profit_factor: float = 0.0  # gross profit / gross loss
    expectancy: float = 0.0  # avg $ per trade
    reward_risk_ratio: float = 0.0  # avg win / avg loss

    # Drawdown
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0

    # Time
    avg_hold_time_minutes: float = 0.0
    total_duration_minutes: float = 0.0

    # Streaks
    max_win_streak: int = 0
    max_loss_streak: int = 0

    # Per-strategy breakdown
    by_strategy: dict[str, "PerformanceReport"] = field(default_factory=dict)

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            "═" * 50,
            "PERFORMANCE REPORT",
            "═" * 50,
            f"Total trades:      {self.total_trades}",
            f"Winners:           {self.winners} | Losers: {self.losers} | Breakeven: {self.breakeven}",
            f"Win rate:          {self.win_rate:.1f}%",
            "",
            f"Total PnL:         ₹{self.total_pnl:.2f}",
            f"Avg PnL/trade:     ₹{self.avg_pnl:.2f}",
            f"Avg win:           ₹{self.avg_win:.2f}",
            f"Avg loss:          ₹{self.avg_loss:.2f}",
            f"Largest win:       ₹{self.largest_win:.2f}",
            f"Largest loss:      ₹{self.largest_loss:.2f}",
            "",
            f"Profit factor:     {self.profit_factor:.2f}",
            f"Expectancy:        ₹{self.expectancy:.2f}",
            f"Reward/Risk:       {self.reward_risk_ratio:.2f}",
            "",
            f"Max drawdown:      ₹{self.max_drawdown:.2f} ({self.max_drawdown_pct:.1f}%)",
            f"Avg hold time:     {self.avg_hold_time_minutes:.1f} min",
            "",
            f"Max win streak:    {self.max_win_streak}",
            f"Max loss streak:   {self.max_loss_streak}",
            "═" * 50,
        ]
        return "\n".join(lines)


class AnalyticsEngine:
    """
    Computes performance metrics from a list of closed positions.

    Usage:
        analytics = AnalyticsEngine()
        report = analytics.analyze(closed_positions)
        print(report.summary())
    """

    def analyze(
        self,
        positions: list[Position],
        include_strategy_breakdown: bool = True,
    ) -> PerformanceReport:
        """
        Analyze a list of closed positions and return a performance report.

        Args:
            positions: List of closed Position objects.
            include_strategy_breakdown: If True, include per-strategy reports.
        """
        # Filter to closed only
        closed = [p for p in positions if not p.is_open]

        if not closed:
            return PerformanceReport()

        report = self._compute_metrics(closed)

        # Per-strategy breakdown
        if include_strategy_breakdown:
            strategies: dict[str, list[Position]] = {}
            for p in closed:
                strategies.setdefault(p.strategy_name, []).append(p)

            for strat_name, strat_positions in strategies.items():
                report.by_strategy[strat_name] = self._compute_metrics(strat_positions)

        return report

    def _compute_metrics(self, positions: list[Position]) -> PerformanceReport:
        """Compute all metrics for a set of positions."""
        report = PerformanceReport()
        report.total_trades = len(positions)

        pnls = [p.pnl for p in positions]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        breakevens = [pnl for pnl in pnls if pnl == 0]

        report.winners = len(wins)
        report.losers = len(losses)
        report.breakeven = len(breakevens)
        report.win_rate = (report.winners / report.total_trades) * 100

        # PnL metrics
        report.total_pnl = sum(pnls)
        report.avg_pnl = report.total_pnl / report.total_trades
        report.avg_win = sum(wins) / len(wins) if wins else 0.0
        report.avg_loss = sum(losses) / len(losses) if losses else 0.0
        report.largest_win = max(wins) if wins else 0.0
        report.largest_loss = min(losses) if losses else 0.0

        # Profit factor
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        report.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Expectancy
        report.expectancy = report.avg_pnl

        # Reward/Risk ratio
        report.reward_risk_ratio = (
            report.avg_win / abs(report.avg_loss) if report.avg_loss != 0 else float("inf")
        )

        # Drawdown
        report.max_drawdown, report.max_drawdown_pct = self._compute_drawdown(pnls)

        # Hold time
        hold_times = []
        for p in positions:
            if p.exit_time_ms > 0 and p.entry_time_ms > 0:
                hold_ms = p.exit_time_ms - p.entry_time_ms
                hold_times.append(hold_ms / 60_000)  # convert to minutes
        report.avg_hold_time_minutes = (
            sum(hold_times) / len(hold_times) if hold_times else 0.0
        )
        report.total_duration_minutes = sum(hold_times)

        # Streaks
        report.max_win_streak, report.max_loss_streak = self._compute_streaks(pnls)

        return report

    @staticmethod
    def _compute_drawdown(pnls: list[float]) -> tuple[float, float]:
        """
        Compute maximum drawdown from a sequence of PnLs.
        Returns (max_drawdown_absolute, max_drawdown_percentage).
        """
        if not pnls:
            return 0.0, 0.0

        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0

        for pnl in pnls:
            cumulative += pnl
            if cumulative > peak:
                peak = cumulative
            drawdown = peak - cumulative
            if drawdown > max_dd:
                max_dd = drawdown

        max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0.0
        return max_dd, max_dd_pct

    @staticmethod
    def _compute_streaks(pnls: list[float]) -> tuple[int, int]:
        """Compute max consecutive win and loss streaks."""
        max_win = 0
        max_loss = 0
        current_win = 0
        current_loss = 0

        for pnl in pnls:
            if pnl > 0:
                current_win += 1
                current_loss = 0
                max_win = max(max_win, current_win)
            elif pnl < 0:
                current_loss += 1
                current_win = 0
                max_loss = max(max_loss, current_loss)
            else:
                current_win = 0
                current_loss = 0

        return max_win, max_loss
