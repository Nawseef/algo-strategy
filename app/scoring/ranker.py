"""
Variant Ranker — composite scoring and ranking.

From File 3:
    "Composite score: weighted(expectancy, profit_factor, stability, trade_count).
     Filter: minimum trade count, positive expectancy, stability > threshold.
     Output: ranked list of top N candidate variants."

The ranker:
1. Loads all trades + exit results for a period
2. Groups by variant_id
3. For each variant, finds the best exit model
4. Computes metrics, stability, regime analysis
5. Produces a ranked list sorted by composite score
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.db.research_store import ResearchStore
from app.scoring.costs import CostModel, get_cost_model, COST_NONE
from app.scoring.metrics import compute_metrics, compute_metrics_for_exit_model, VariantMetrics
from app.scoring.stability import compute_stability, StabilityResult
from app.scoring.regime import compute_regime_analysis, RegimeAnalysis
from app.utils.logger import get_logger

logger = get_logger(__name__)


# Exit model columns in the exit_results table (the 57 models)
EXIT_MODEL_COLUMNS = [
    "rr1_result", "rr1_5_result", "rr2_result", "rr2_5_result",
    "rr3_result", "rr5_result", "rr10_result",
    "atr_stop_result", "swing_stop_result", "fixed_stop_result",
    "atr_trail_result", "ema_trail_result", "swing_trail_result",
    "partial_a_result", "partial_b_result", "partial_c_result",
    "time_15m_result", "time_30m_result", "time_1h_result",
    "time_2h_result", "time_4h_result",
    "session_morning_result", "session_midday_result",
    "session_afternoon_result", "session_preclose_result",
    "dead_30m_result", "dead_1h_result", "dead_2h_result",
    "be_atr_trail_result", "be_tight_trail_result", "be_wide_trail_result",
    "be_ema_trail_result", "be_rr2_target_result", "be_rr3_target_result",
    "be_rr5_target_result",
    "chandelier_2x_result", "chandelier_3x_result", "chandelier_4x_result",
    "pct_trail_05_result", "pct_trail_1_result", "pct_trail_15_result",
    "pct_trail_2_result",
    "step_trail_1r_result", "step_trail_05r_result",
    "delayed_chand_2x_result", "delayed_chand_3x_result", "delayed_chand_4x_result",
    "vwap_cross_result", "ema9_cross_result", "ema13_cross_result",
    "ema20_cross_result", "ema50_cross_result",
    "rsi_70_exit_result", "rsi_75_exit_result", "rsi_80_exit_result",
    "ema_9_21_xover_result", "ema_9_50_xover_result",
]


@dataclass
class ExitModelScore:
    """Score for a single exit model applied to a variant."""

    model_name: str = ""
    expectancy: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    net_pnl: float = 0.0
    trade_count: int = 0


@dataclass
class RankedVariant:
    """A scored and ranked variant with full research detail."""

    rank: int = 0
    variant_id: str = ""
    strategy: str = ""
    timeframe: str = ""

    # Best exit model for this variant
    best_exit_model: str = ""
    best_exit_expectancy: float = 0.0

    # Top 3 exit models (alternatives to best)
    top_exit_models: list[ExitModelScore] = field(default_factory=list)

    # Core metrics (using best exit model)
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    max_drawdown: float = 0.0
    recovery_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    # Edge analysis (MFE/MAE)
    avg_mfe: float = 0.0  # Average max favorable excursion
    avg_mae: float = 0.0  # Average max adverse excursion
    edge_ratio: float = 0.0  # avg_mfe / abs(avg_mae) — how much room trades have

    # Stability
    stability_score: float = 0.0
    periods_profitable: int = 0
    periods_analyzed: int = 0

    # Composite
    composite_score: float = 0.0

    # After-cost metrics (same variant scored with transaction costs deducted)
    cost_model_name: str = ""
    cost_per_trade: float = 0.0
    net_expectancy: float = 0.0  # expectancy AFTER costs
    net_profit_factor: float = 0.0
    net_net_pnl: float = 0.0
    net_win_rate: float = 0.0
    profitable_after_costs: bool = True

    # Regime
    best_regime: str = ""
    worst_regime: str = ""
    regime_details: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class RankingConfig:
    """Configuration for the ranking process."""

    # Minimum requirements
    min_trade_count: int = 30  # Minimum trades to be considered (was 10, increased for statistical confidence)
    min_expectancy: float = 0.0  # Must be positive
    min_stability: float = 20.0  # Out of 100

    # Composite score weights
    weight_expectancy: float = 0.35
    weight_profit_factor: float = 0.20
    weight_stability: float = 0.25
    weight_sharpe: float = 0.10
    weight_recovery: float = 0.10

    # Transaction cost model name: "none", "equity_intraday", "futures", "conservative"
    cost_model: str = "equity_intraday"

    # Sample size confidence: variants with fewer trades get penalized
    # confidence = min(1.0, sqrt(trade_count / confidence_target_trades))
    confidence_target_trades: int = 100  # Full confidence at 100+ trades

    # Output
    top_n: int = 50


class VariantRanker:
    """
    Ranks variants by composite performance score.

    Pipeline:
    1. Load trades + exit results
    2. Group by variant_id
    3. Find best exit model per variant
    4. Compute metrics, stability, regime
    5. Compute composite score
    6. Filter and rank
    """

    def __init__(self, store: ResearchStore, config: RankingConfig | None = None) -> None:
        self._store = store
        self._config = config or RankingConfig()
        self._cost_model = get_cost_model(self._config.cost_model)

    def rank_variants(
        self,
        period_start_ms: float,
        period_end_ms: float,
        period_label: str = "",
    ) -> list[RankedVariant]:
        """
        Rank all variants for a given time period.

        Args:
            period_start_ms: Start of period (epoch ms).
            period_end_ms: End of period (epoch ms).
            period_label: Label for DB storage (e.g. "2026-06").

        Returns:
            Sorted list of RankedVariant (top N, best first).
        """
        # Load all trades with exit results for the period
        trades_with_exits = self._load_trades_with_exits(period_start_ms, period_end_ms)

        if not trades_with_exits:
            logger.info("No trades found for ranking period")
            return []

        logger.info("Ranking: %d trades loaded for period", len(trades_with_exits))

        # Group by variant_id
        variant_groups: dict[str, list[dict]] = defaultdict(list)
        for trade in trades_with_exits:
            vid = trade.get("variant_id", "")
            if vid:
                variant_groups[vid].append(trade)

        logger.info("Ranking: %d unique variants to score", len(variant_groups))

        # Score each variant
        candidates: list[RankedVariant] = []

        for variant_id, trades in variant_groups.items():
            ranked = self._score_variant(variant_id, trades)
            if ranked is not None:
                candidates.append(ranked)

        # Sort by composite score (highest first)
        candidates.sort(key=lambda r: r.composite_score, reverse=True)

        # Assign ranks and limit to top N
        top = candidates[:self._config.top_n]
        for i, rv in enumerate(top, 1):
            rv.rank = i

        # Store to DB if period label provided
        if period_label:
            self._save_scores(top, period_label)

        logger.info(
            "Ranking complete: %d candidates passed filters, returning top %d",
            len(candidates), len(top),
        )

        return top

    def _score_variant(self, variant_id: str, trades: list[dict]) -> RankedVariant | None:
        """
        Score a single variant. Returns None if it doesn't meet minimum criteria.
        """
        # Check minimum trade count
        if len(trades) < self._config.min_trade_count:
            return None

        # Find the best exit model (and top 3)
        best_exit, best_expectancy, top_exits = self._find_best_exit_models(trades)
        if best_exit is None:
            return None

        # Check minimum expectancy
        if best_expectancy <= self._config.min_expectancy:
            return None

        # Compute full metrics using best exit model
        pnl_values = [t.get(best_exit, 0.0) for t in trades if t.get(best_exit) is not None]
        if not pnl_values:
            return None

        metrics = compute_metrics(pnl_values)

        # Compute stability
        stability = compute_stability(trades, best_exit, "weekly")

        # Check minimum stability
        if stability.stability_score < self._config.min_stability:
            return None

        # Compute regime analysis
        regime = compute_regime_analysis(trades, best_exit)

        # Compute edge ratio from MFE/MAE
        avg_mfe, avg_mae, edge_ratio = self._compute_edge_ratio(trades)

        # Compute regime summary for storage
        from app.scoring.regime import compute_regime_summary
        regime_details = compute_regime_summary(trades, best_exit)

        # Compute composite score
        composite = self._composite_score(metrics, stability.stability_score)

        # Compute after-cost metrics
        # Use token directly for cost lookup to handle numeric tokens (e.g. "26000")
        # before falling back to name-based lookup which only works for string names
        from app.scoring.costs import COST_FUTURES
        from app.scoring.regime_scorer import FUTURES_COST_POINTS as _FCP
        instrument = trades[0].get("instrument", "NIFTY")
        cost_per_trade = _FCP.get(instrument, self._cost_model.cost_per_trade_points(instrument))
        adjusted_pnls = self._cost_model.apply(pnl_values, instrument)
        net_metrics = compute_metrics(adjusted_pnls)

        # Build result
        rv = RankedVariant(
            variant_id=variant_id,
            strategy=trades[0].get("strategy", ""),
            timeframe=trades[0].get("timeframe", ""),
            best_exit_model=best_exit.replace("_result", ""),
            best_exit_expectancy=best_expectancy,
            top_exit_models=top_exits,
            trade_count=metrics.trade_count,
            win_count=metrics.win_count,
            loss_count=metrics.loss_count,
            win_rate=metrics.win_rate,
            avg_win=metrics.avg_win,
            avg_loss=metrics.avg_loss,
            expectancy=metrics.expectancy,
            profit_factor=metrics.profit_factor,
            net_pnl=metrics.net_pnl,
            gross_profit=metrics.gross_profit,
            gross_loss=metrics.gross_loss,
            max_drawdown=metrics.max_drawdown,
            recovery_factor=metrics.recovery_factor,
            sharpe_ratio=metrics.sharpe_ratio,
            max_consecutive_wins=metrics.max_consecutive_wins,
            max_consecutive_losses=metrics.max_consecutive_losses,
            avg_mfe=avg_mfe,
            avg_mae=avg_mae,
            edge_ratio=edge_ratio,
            stability_score=stability.stability_score,
            periods_profitable=stability.periods_profitable,
            periods_analyzed=stability.periods_analyzed,
            composite_score=composite,
            cost_model_name=self._cost_model.name,
            cost_per_trade=cost_per_trade,
            net_expectancy=net_metrics.expectancy,
            net_profit_factor=net_metrics.profit_factor,
            net_net_pnl=net_metrics.net_pnl,
            net_win_rate=net_metrics.win_rate,
            profitable_after_costs=net_metrics.expectancy > 0,
            best_regime=(
                f"{regime.best_regime.regime_name}={regime.best_regime.regime_value}"
                if regime.best_regime else ""
            ),
            worst_regime=(
                f"{regime.worst_regime.regime_name}={regime.worst_regime.regime_value}"
                if regime.worst_regime else ""
            ),
            regime_details=regime_details,
        )

        return rv

    def _find_best_exit_models(self, trades: list[dict]) -> tuple[str | None, float, list[ExitModelScore]]:
        """
        Find the exit models with highest expectancy for this variant's trades.
        Returns (best_column_name, best_expectancy, top_3_scores).
        """
        model_scores: list[ExitModelScore] = []

        for col in EXIT_MODEL_COLUMNS:
            pnl_values = [t.get(col) for t in trades if t.get(col) is not None]
            if len(pnl_values) < self._config.min_trade_count:
                continue

            metrics = compute_metrics(pnl_values)
            if metrics.expectancy > 0:
                model_scores.append(ExitModelScore(
                    model_name=col.replace("_result", ""),
                    expectancy=metrics.expectancy,
                    win_rate=metrics.win_rate,
                    profit_factor=metrics.profit_factor,
                    net_pnl=metrics.net_pnl,
                    trade_count=metrics.trade_count,
                ))

        if not model_scores:
            return None, 0.0, []

        # Sort by expectancy (best first)
        model_scores.sort(key=lambda s: s.expectancy, reverse=True)

        best = model_scores[0]
        top_3 = model_scores[:3]

        return f"{best.model_name}_result", best.expectancy, top_3

    def _compute_edge_ratio(self, trades: list[dict]) -> tuple[float, float, float]:
        """
        Compute average MFE, MAE, and edge ratio from trade data.
        Edge ratio = avg_mfe / abs(avg_mae). Higher = trades have more room to run.
        """
        mfe_values = [t.get("mfe", 0) for t in trades if t.get("mfe") is not None]
        mae_values = [t.get("mae", 0) for t in trades if t.get("mae") is not None]

        avg_mfe = sum(mfe_values) / len(mfe_values) if mfe_values else 0.0
        avg_mae = sum(mae_values) / len(mae_values) if mae_values else 0.0

        edge_ratio = avg_mfe / abs(avg_mae) if avg_mae != 0 else 0.0

        return avg_mfe, avg_mae, edge_ratio

    def _composite_score(self, metrics: VariantMetrics, stability_score: float) -> float:
        """
        Compute weighted composite score with sample-size confidence adjustment.

        Normalizes each metric to a 0-1 scale, applies weights,
        then multiplies by confidence factor based on trade count.
        """
        cfg = self._config

        # Normalize expectancy (assume typical range 0-50 points)
        norm_exp = min(metrics.expectancy / 50.0, 1.0) if metrics.expectancy > 0 else 0.0

        # Normalize profit factor (1.0 = break even, 3.0+ = excellent)
        pf = min(metrics.profit_factor, 5.0) if metrics.profit_factor != float("inf") else 5.0
        norm_pf = max(0, (pf - 1.0) / 4.0)  # 1.0→0, 5.0→1.0

        # Normalize stability (already 0-100)
        norm_stab = stability_score / 100.0

        # Normalize Sharpe (typical range 0-2)
        norm_sharpe = min(max(metrics.sharpe_ratio, 0) / 2.0, 1.0)

        # Normalize recovery factor (typical range 0-5)
        rf = min(metrics.recovery_factor, 5.0) if metrics.recovery_factor != float("inf") else 5.0
        norm_rf = rf / 5.0

        raw_composite = (
            cfg.weight_expectancy * norm_exp
            + cfg.weight_profit_factor * norm_pf
            + cfg.weight_stability * norm_stab
            + cfg.weight_sharpe * norm_sharpe
            + cfg.weight_recovery * norm_rf
        )

        # Sample size confidence factor: sqrt(trades / target)
        # 30 trades → 0.55 confidence, 100 trades → 1.0, 200 trades → 1.0 (capped)
        import math
        confidence = min(1.0, math.sqrt(metrics.trade_count / cfg.confidence_target_trades))

        return raw_composite * confidence * 100.0  # Scale to 0-100

    def _load_trades_with_exits(self, start_ms: float, end_ms: float) -> list[dict]:
        """Load trades joined with exit results for a period."""
        # Use explicit column selection to avoid t.id / e.id collision
        if self._store.is_postgres:
            sql = """
                SELECT t.trade_id, t.variant_id, t.strategy, t.timeframe, t.instrument,
                    t.direction, t.entry_time_ms, t.entry_price,
                    t.atr_entry, t.adx_entry, t.rsi_entry, t.vix_entry,
                    t.volume_ratio_entry, t.vwap_entry,
                    t.gap_size, t.gap_direction, t.session, t.day_of_week, t.month,
                    t.market_structure, t.volatility_regime, t.htf_trend_1h,
                    t.ema_20_slope, t.ema_50_slope, t.opening_range_size,
                    e.rr1_result, e.rr1_5_result, e.rr2_result, e.rr2_5_result,
                    e.rr3_result, e.rr5_result, e.rr10_result,
                    e.atr_stop_result, e.swing_stop_result, e.fixed_stop_result,
                    e.atr_trail_result, e.ema_trail_result, e.swing_trail_result,
                    e.partial_a_result, e.partial_b_result, e.partial_c_result,
                    e.time_15m_result, e.time_30m_result, e.time_1h_result,
                    e.time_2h_result, e.time_4h_result,
                    e.session_morning_result, e.session_midday_result,
                    e.session_afternoon_result, e.session_preclose_result,
                    e.dead_30m_result, e.dead_1h_result, e.dead_2h_result,
                    e.be_atr_trail_result, e.be_tight_trail_result, e.be_wide_trail_result,
                    e.be_ema_trail_result, e.be_rr2_target_result, e.be_rr3_target_result,
                    e.be_rr5_target_result,
                    e.chandelier_2x_result, e.chandelier_3x_result, e.chandelier_4x_result,
                    e.pct_trail_05_result, e.pct_trail_1_result, e.pct_trail_15_result,
                    e.pct_trail_2_result, e.step_trail_1r_result, e.step_trail_05r_result,
                    e.delayed_chand_2x_result, e.delayed_chand_3x_result, e.delayed_chand_4x_result,
                    e.vwap_cross_result, e.ema9_cross_result, e.ema13_cross_result,
                    e.ema20_cross_result, e.ema50_cross_result,
                    e.rsi_70_exit_result, e.rsi_75_exit_result, e.rsi_80_exit_result,
                    e.ema_9_21_xover_result, e.ema_9_50_xover_result,
                    e.mfe, e.mae, e.best_exit_model, e.best_pnl
                FROM trades t
                LEFT JOIN exit_results e ON t.trade_id = e.trade_id
                WHERE t.entry_time_ms >= %s AND t.entry_time_ms < %s
                ORDER BY t.entry_time_ms
            """
        else:
            sql = """
                SELECT t.trade_id, t.variant_id, t.strategy, t.timeframe, t.instrument,
                    t.direction, t.entry_time_ms, t.entry_price,
                    t.atr_entry, t.adx_entry, t.rsi_entry, t.vix_entry,
                    t.volume_ratio_entry, t.vwap_entry,
                    t.gap_size, t.gap_direction, t.session, t.day_of_week, t.month,
                    t.market_structure, t.volatility_regime, t.htf_trend_1h,
                    t.ema_20_slope, t.ema_50_slope, t.opening_range_size,
                    e.rr1_result, e.rr1_5_result, e.rr2_result, e.rr2_5_result,
                    e.rr3_result, e.rr5_result, e.rr10_result,
                    e.atr_stop_result, e.swing_stop_result, e.fixed_stop_result,
                    e.atr_trail_result, e.ema_trail_result, e.swing_trail_result,
                    e.partial_a_result, e.partial_b_result, e.partial_c_result,
                    e.time_15m_result, e.time_30m_result, e.time_1h_result,
                    e.time_2h_result, e.time_4h_result,
                    e.session_morning_result, e.session_midday_result,
                    e.session_afternoon_result, e.session_preclose_result,
                    e.dead_30m_result, e.dead_1h_result, e.dead_2h_result,
                    e.be_atr_trail_result, e.be_tight_trail_result, e.be_wide_trail_result,
                    e.be_ema_trail_result, e.be_rr2_target_result, e.be_rr3_target_result,
                    e.be_rr5_target_result,
                    e.chandelier_2x_result, e.chandelier_3x_result, e.chandelier_4x_result,
                    e.pct_trail_05_result, e.pct_trail_1_result, e.pct_trail_15_result,
                    e.pct_trail_2_result, e.step_trail_1r_result, e.step_trail_05r_result,
                    e.delayed_chand_2x_result, e.delayed_chand_3x_result, e.delayed_chand_4x_result,
                    e.vwap_cross_result, e.ema9_cross_result, e.ema13_cross_result,
                    e.ema20_cross_result, e.ema50_cross_result,
                    e.rsi_70_exit_result, e.rsi_75_exit_result, e.rsi_80_exit_result,
                    e.ema_9_21_xover_result, e.ema_9_50_xover_result,
                    e.mfe, e.mae, e.best_exit_model, e.best_pnl
                FROM trades t
                LEFT JOIN exit_results e ON t.trade_id = e.trade_id
                WHERE t.entry_time_ms >= ? AND t.entry_time_ms < ?
                ORDER BY t.entry_time_ms
            """
        return self._store._query(sql, (start_ms, end_ms))

    def _save_scores(self, ranked: list[RankedVariant], period_label: str) -> None:
        """Save top variant scores and regime breakdowns to DB."""
        for rv in ranked:
            self._store.write_variant_score(
                variant_id=rv.variant_id,
                period=period_label,
                metrics={
                    "trade_count": rv.trade_count,
                    "win_rate": rv.win_rate,
                    "avg_win": rv.avg_win,
                    "avg_loss": rv.avg_loss,
                    "expectancy": rv.expectancy,
                    "profit_factor": rv.profit_factor,
                    "net_pnl": rv.net_pnl,
                    "max_drawdown": rv.max_drawdown,
                    "recovery_factor": rv.recovery_factor,
                    "stability_score": rv.stability_score,
                    "best_exit_model": rv.best_exit_model,
                    "best_exit_expectancy": rv.best_exit_expectancy,
                },
            )

            # Save regime breakdown if available
            if rv.regime_details:
                # Convert regime_details to the format expected by write_regime_scores
                # Current format: {"session": {"MORNING": 35.2, "MIDDAY": -8.5}}
                # Need: {"session": {"MORNING": {"expectancy": 35.2, ...}}}
                # The regime_details from compute_regime_summary only has expectancy
                # We'll store what we have
                regime_data: dict[str, dict[str, dict]] = {}
                for dim, values in rv.regime_details.items():
                    regime_data[dim] = {}
                    for dim_value, expectancy in values.items():
                        regime_data[dim][dim_value] = {
                            "trade_count": 0,  # Not available in summary format
                            "win_rate": 0,
                            "expectancy": expectancy,
                            "profit_factor": 0,
                            "net_pnl": 0,
                        }

                self._store.write_regime_scores(
                    variant_id=rv.variant_id,
                    scoring_period=period_label,
                    exit_model=rv.best_exit_model,
                    regime_data=regime_data,
                )
