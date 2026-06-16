"""
Regime-Conditional Scorer — finds the best variant for each market condition.

Instead of finding "best variant overall" (which loses in wrong conditions),
this finds "best variant PER condition" so you have a specialist for every regime.

Conditions (dimensions):
  - instrument: NIFTY, BANKNIFTY, RELIANCE, etc.
  - volatility_regime: HIGH, NORMAL, LOW
  - market_structure: TRENDING, RANGING, TRANSITIONING
  - session: MORNING, MIDDAY, CLOSING

The output is a regime→variant mapping table:
  (instrument, volatility, structure, session) → best variant + exit model

NEW in v2:
  --min-freq 15         Minimum trades per month (hard filter, default 0 = off)
  --instruments bnf     Only score specific instruments:
                          bnf       → BANKNIFTY only (26009)
                          nf        → NIFTY only (26000)
                          indices   → NIFTY + BANKNIFTY
                          stocks    → all 8 stock futures
                          all       → all 10 instruments (default)
                          or pass comma-separated tokens: 26000,26009,2885
  --cost futures        Cost model: none, equity_intraday, futures (default), options

Usage:
    python -m app.scoring.regime_scorer --from 2021-01-01 --to 2024-12-31
    python -m app.scoring.regime_scorer --from 2021-01-01 --to 2024-12-31 \\
        --instruments indices --cost futures --min-freq 15 --top 5
    python -m app.scoring.regime_scorer --from 2021-01-01 --to 2024-12-31 \\
        --validate-from 2025-01-01 --validate-to 2026-06-12 \\
        --instruments bnf --cost futures --min-freq 20
"""

from __future__ import annotations

import sys
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import product

from app.db.research_store import ResearchStore
from app.scoring.metrics import compute_metrics, VariantMetrics
from app.scoring.costs import get_cost_model, CostModel, COST_FUTURES
from app.utils.logger import get_logger

logger = get_logger("regime_scorer")


# ─── Condition dimensions ────────────────────────────────────────────────────

ALL_INSTRUMENTS = {
    "26000": "NIFTY",
    "26009": "BANKNIFTY",
    "2885": "RELIANCE",
    "1333": "HDFCBANK",
    "4963": "ICICIBANK",
    "3045": "SBIN",
    "5900": "AXISBANK",
    "1594": "INFY",
    "11536": "TCS",
    "10604": "BHARTIARTL",
}

# Keep backward-compat alias
INSTRUMENTS = ALL_INSTRUMENTS

# Named instrument presets
INSTRUMENT_PRESETS: dict[str, list[str]] = {
    "bnf":     ["26009"],
    "nf":      ["26000"],
    "indices": ["26000", "26009"],
    "stocks":  ["2885", "1333", "4963", "3045", "5900", "1594", "11536", "10604"],
    "all":     list(ALL_INSTRUMENTS.keys()),
}

# Per-instrument futures cost in points (round-trip)
FUTURES_COST_POINTS: dict[str, float] = {
    "26000": 17.0,   # NIFTY
    "26009": 28.0,   # BANKNIFTY
    "2885": 5.0,     # RELIANCE
    "1333": 5.0,     # HDFCBANK
    "4963": 5.0,     # ICICIBANK
    "3045": 5.0,     # SBIN
    "5900": 5.0,     # AXISBANK
    "1594": 5.0,     # INFY
    "11536": 5.0,    # TCS
    "10604": 5.0,    # BHARTIARTL
}

VOLATILITY_REGIMES = ["HIGH", "NORMAL", "LOW"]
MARKET_STRUCTURES = ["TRENDING", "RANGING", "TRANSITIONING"]
SESSIONS = ["MORNING", "MIDDAY", "CLOSING"]

# Exit model columns
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
    "delayed_chand_2x_result", "delayed_chand_3x_result",
    "delayed_chand_4x_result",
    "vwap_cross_result", "ema9_cross_result", "ema13_cross_result",
    "ema20_cross_result", "ema50_cross_result",
    "rsi_70_exit_result", "rsi_75_exit_result", "rsi_80_exit_result",
    "ema_9_21_xover_result", "ema_9_50_xover_result",
]


# ─── Data classes ────────────────────────────────────────────────────────────


@dataclass
class ConditionKey:
    """A specific market condition combination."""
    instrument: str  # token like "26000"
    volatility: str  # HIGH / NORMAL / LOW
    structure: str   # TRENDING / RANGING / TRANSITIONING
    session: str     # MORNING / MIDDAY / CLOSING

    @property
    def instrument_name(self) -> str:
        return INSTRUMENTS.get(self.instrument, self.instrument)

    def label(self) -> str:
        return f"{self.instrument_name}|{self.volatility}|{self.structure}|{self.session}"

    def __hash__(self):
        return hash((self.instrument, self.volatility, self.structure, self.session))

    def __eq__(self, other):
        return (self.instrument, self.volatility, self.structure, self.session) == \
               (other.instrument, other.volatility, other.structure, other.session)


@dataclass
class ConditionWinner:
    """The best variant for a specific condition."""
    condition: ConditionKey
    variant_id: str
    strategy: str
    timeframe: str
    exit_model: str

    # Performance in this specific condition
    trade_count: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0

    # Frequency
    trades_per_month: float = 0.0  # average monthly trade count

    # Composite score for this condition
    score: float = 0.0

    # After-cost metrics
    net_expectancy: float = 0.0
    profitable_after_costs: bool = True


@dataclass
class ValidationResult:
    """Forward walk validation for a condition winner."""
    condition: ConditionKey
    variant_id: str
    exit_model: str

    # Train period performance
    train_trades: int = 0
    train_expectancy: float = 0.0
    train_win_rate: float = 0.0
    train_pf: float = 0.0

    # Validation period performance
    val_trades: int = 0
    val_expectancy: float = 0.0
    val_win_rate: float = 0.0
    val_pf: float = 0.0

    # Verdict
    passed: bool = False  # Does it hold up in unseen data?
    degradation_pct: float = 0.0  # How much worse in validation vs train


# ─── Core scoring logic ──────────────────────────────────────────────────────


class RegimeScorer:
    """
    Scores variants per market condition and produces a regime→variant map.

    The approach:
    1. For each condition combo (instrument × volatility × structure × session):
       - Query only trades matching that condition
       - Group by variant_id
       - For each variant, find its best exit model on THIS condition's trades
       - Score and rank
    2. Output: top N variants per condition
    3. Optionally: forward walk validate (train vs test period)

    New parameters:
        instruments: list of token strings to score (default: all)
        min_trades_per_month: hard filter on trade frequency (default: 0 = off)
        train_months: number of months in the train window (auto-calculated from dates)
    """

    def __init__(
        self,
        store: ResearchStore,
        min_trades: int = 30,
        top_per_condition: int = 5,
        cost_model_name: str = "futures",
        instruments: list[str] | None = None,
        min_trades_per_month: float = 0.0,
    ):
        self._store = store
        self._min_trades = min_trades
        self._top_n = top_per_condition
        self._cost_model_name = cost_model_name
        self._cost_model = get_cost_model(cost_model_name)
        # Instrument filter — default to all if not specified
        self._instruments = instruments if instruments else list(ALL_INSTRUMENTS.keys())
        self._min_trades_per_month = min_trades_per_month
        self._train_months: float = 0.0  # set when score_all_conditions is called

    def _cost_for_instrument(self, token: str) -> float:
        """Get per-trade cost in points for a specific token."""
        if self._cost_model_name == "futures":
            return FUTURES_COST_POINTS.get(token, 5.0)
        # Fall back to the CostModel's name-based lookup with instrument name
        inst_name = ALL_INSTRUMENTS.get(token, token)
        return self._cost_model.cost_per_trade_points(inst_name)

    def score_all_conditions(
        self,
        start_ms: float,
        end_ms: float,
    ) -> dict[ConditionKey, list[ConditionWinner]]:
        """
        Score every condition combination and find winners.

        Returns dict mapping each condition to its top N variants.
        """
        # Calculate number of months in training window (for frequency filter)
        start_dt = datetime.fromtimestamp(start_ms / 1000)
        end_dt = datetime.fromtimestamp(end_ms / 1000)
        self._train_months = max(
            1.0,
            (end_dt - start_dt).days / 30.44,
        )
        # Derive min_trades from frequency requirement
        effective_min = self._min_trades
        if self._min_trades_per_month > 0:
            freq_min = int(self._min_trades_per_month * self._train_months)
            effective_min = max(self._min_trades, freq_min)

        results: dict[ConditionKey, list[ConditionWinner]] = {}
        total_conditions = (
            len(self._instruments) * len(VOLATILITY_REGIMES)
            * len(MARKET_STRUCTURES) * len(SESSIONS)
        )
        processed = 0
        found = 0

        freq_label = (
            f", min freq={self._min_trades_per_month:.0f}/month"
            f" (={effective_min} trades)"
            if self._min_trades_per_month > 0 else ""
        )
        inst_names = [ALL_INSTRUMENTS.get(t, t) for t in self._instruments]
        print(f"\n  Instruments:  {', '.join(inst_names)}")
        print(f"  Scoring {total_conditions} condition combinations...")
        print(f"  Min trades: {effective_min} ({self._train_months:.1f} months){freq_label}")
        print(f"  Top N per condition: {self._top_n}")
        print(f"  Cost model: {self._cost_model_name}")
        print()

        for inst in self._instruments:
            for vol in VOLATILITY_REGIMES:
                for struct in MARKET_STRUCTURES:
                    for sess in SESSIONS:
                        processed += 1
                        condition = ConditionKey(
                            instrument=inst,
                            volatility=vol,
                            structure=struct,
                            session=sess,
                        )

                        winners = self._score_condition(
                            condition, start_ms, end_ms, effective_min
                        )

                        if winners:
                            results[condition] = winners
                            found += len(winners)

                        if processed % 30 == 0:
                            print(
                                f"    [{processed}/{total_conditions}] "
                                f"conditions scored, {found} winners found"
                            )

        print(f"\n  Done: {len(results)} conditions have winners "
              f"({found} total variant slots)")
        return results

    def _score_condition(
        self,
        condition: ConditionKey,
        start_ms: float,
        end_ms: float,
        effective_min: int | None = None,
    ) -> list[ConditionWinner]:
        """
        Score all variants for a single condition.
        Returns top N winners sorted by composite score.
        """
        min_trades = effective_min if effective_min is not None else self._min_trades

        # Load trades matching this condition
        trades = self._load_condition_trades(condition, start_ms, end_ms)

        if len(trades) < min_trades:
            return []

        # Group by variant_id
        variant_groups: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            vid = t.get("variant_id", "")
            if vid:
                variant_groups[vid].append(t)

        # Score each variant under this condition
        candidates: list[ConditionWinner] = []

        for variant_id, vtrades in variant_groups.items():
            if len(vtrades) < min_trades:
                continue

            winner = self._score_variant_for_condition(
                variant_id, vtrades, condition, min_trades
            )
            if winner is not None:
                candidates.append(winner)

        # Sort by score (best first) and return top N
        candidates.sort(key=lambda w: w.score, reverse=True)
        return candidates[:self._top_n]

    def _score_variant_for_condition(
        self,
        variant_id: str,
        trades: list[dict],
        condition: ConditionKey,
        min_trades: int | None = None,
    ) -> ConditionWinner | None:
        """Score a single variant under a specific condition."""
        mt = min_trades if min_trades is not None else self._min_trades

        # Find best exit model for THIS condition's trades
        best_exit, best_exp = self._find_best_exit(trades, mt)
        if best_exit is None or best_exp <= 0:
            return None

        # Compute metrics using best exit
        pnl_values = [
            t.get(best_exit, 0.0) for t in trades
            if t.get(best_exit) is not None
        ]
        if len(pnl_values) < mt:
            return None

        metrics = compute_metrics(pnl_values)
        if metrics.expectancy <= 0:
            return None

        # Calculate trades per month
        months = self._train_months if self._train_months > 0 else 1.0
        trades_per_month = len(pnl_values) / months

        # Compute composite score (now includes frequency bonus)
        score = self._composite_score(metrics, len(pnl_values), trades_per_month)

        # After-cost metrics using token-based cost
        cost = self._cost_for_instrument(condition.instrument)
        net_exp = metrics.expectancy - cost
        profitable = net_exp > 0

        return ConditionWinner(
            condition=condition,
            variant_id=variant_id,
            strategy=trades[0].get("strategy", ""),
            timeframe=trades[0].get("timeframe", ""),
            exit_model=best_exit.replace("_result", ""),
            trade_count=metrics.trade_count,
            win_rate=metrics.win_rate,
            expectancy=metrics.expectancy,
            profit_factor=metrics.profit_factor,
            net_pnl=metrics.net_pnl,
            max_drawdown=metrics.max_drawdown,
            sharpe_ratio=metrics.sharpe_ratio,
            trades_per_month=trades_per_month,
            score=score,
            net_expectancy=net_exp,
            profitable_after_costs=profitable,
        )

    def _find_best_exit(
        self, trades: list[dict], min_trades: int | None = None
    ) -> tuple[str | None, float]:
        """Find exit model with highest expectancy for these trades."""
        mt = min_trades if min_trades is not None else self._min_trades
        best_col = None
        best_exp = 0.0

        for col in EXIT_MODEL_COLUMNS:
            pnls = [t.get(col) for t in trades if t.get(col) is not None]
            if len(pnls) < mt:
                continue
            metrics = compute_metrics(pnls)
            if metrics.expectancy > best_exp:
                best_exp = metrics.expectancy
                best_col = col

        return best_col, best_exp

    def _composite_score(
        self, metrics: VariantMetrics, trade_count: int,
        trades_per_month: float = 0.0,
    ) -> float:
        """
        Composite score for condition-specific ranking.

        Weights:
          - Expectancy (35%): how much you make per trade
          - Win rate (20%): consistency of winning
          - Profit factor (15%): risk-reward balance
          - Sharpe (10%): risk-adjusted return
          - Monthly income (20%): expectancy × frequency (rewards high-freq strategies)

        Multiplied by confidence factor based on sample size.
        """
        # Normalize expectancy (0-100 pts range — BNF can go high)
        norm_exp = min(metrics.expectancy / 100.0, 1.0) if metrics.expectancy > 0 else 0.0

        # Normalize win rate (50% = break even, 80%+ = excellent)
        norm_wr = max(0, (metrics.win_rate - 0.4) / 0.5)  # 40%→0, 90%→1
        norm_wr = min(norm_wr, 1.0)

        # Normalize profit factor (1.0-5.0 range)
        pf = min(metrics.profit_factor, 5.0) if metrics.profit_factor != float("inf") else 5.0
        norm_pf = max(0, (pf - 1.0) / 4.0)

        # Normalize sharpe (0-2 range)
        norm_sharpe = min(max(metrics.sharpe_ratio, 0) / 2.0, 1.0)

        # Monthly income = expectancy × trades_per_month
        # Normalize: 0 pts/month → 0, 1000 pts/month → 1.0
        monthly_income = max(0, metrics.expectancy * trades_per_month)
        norm_monthly = min(monthly_income / 1000.0, 1.0)

        raw = (
            0.35 * norm_exp
            + 0.20 * norm_wr
            + 0.15 * norm_pf
            + 0.10 * norm_sharpe
            + 0.20 * norm_monthly  # rewards frequency
        )

        # Confidence: sqrt(trades / 100), capped at 1.0
        confidence = min(1.0, math.sqrt(trade_count / 100.0))

        return raw * confidence * 100.0

    def _load_condition_trades(
        self,
        condition: ConditionKey,
        start_ms: float,
        end_ms: float,
    ) -> list[dict]:
        """Load trades matching a specific condition from DB."""
        sql = """
            SELECT t.trade_id, t.variant_id, t.strategy, t.timeframe,
                t.instrument, t.direction, t.entry_time_ms, t.entry_price,
                t.session, t.volatility_regime, t.market_structure,
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
                e.be_atr_trail_result, e.be_tight_trail_result,
                e.be_wide_trail_result, e.be_ema_trail_result,
                e.be_rr2_target_result, e.be_rr3_target_result,
                e.be_rr5_target_result,
                e.chandelier_2x_result, e.chandelier_3x_result,
                e.chandelier_4x_result,
                e.pct_trail_05_result, e.pct_trail_1_result,
                e.pct_trail_15_result, e.pct_trail_2_result,
                e.step_trail_1r_result, e.step_trail_05r_result,
                e.delayed_chand_2x_result, e.delayed_chand_3x_result,
                e.delayed_chand_4x_result,
                e.vwap_cross_result, e.ema9_cross_result,
                e.ema13_cross_result, e.ema20_cross_result,
                e.ema50_cross_result,
                e.rsi_70_exit_result, e.rsi_75_exit_result,
                e.rsi_80_exit_result,
                e.ema_9_21_xover_result, e.ema_9_50_xover_result,
                e.mfe, e.mae
            FROM trades t
            JOIN exit_results e ON t.trade_id = e.trade_id
            WHERE t.entry_time_ms >= %s AND t.entry_time_ms < %s
              AND t.instrument = %s
              AND t.volatility_regime = %s
              AND t.market_structure = %s
              AND t.session = %s
        """
        params = (
            start_ms, end_ms,
            condition.instrument,
            condition.volatility,
            condition.structure,
            condition.session,
        )
        return self._store._query(sql, params)

    def validate_winners(
        self,
        train_results: dict[ConditionKey, list[ConditionWinner]],
        val_start_ms: float,
        val_end_ms: float,
    ) -> list[ValidationResult]:
        """
        Forward walk validation: check if train-period winners hold up
        in an unseen validation period.

        For each condition's #1 winner from training:
        - Load that variant's trades in the validation period
        - Compute metrics using the SAME exit model
        - Compare expectancy: did it survive?

        Returns list of ValidationResults (pass/fail per condition).
        """
        validations: list[ValidationResult] = []
        total = len(train_results)
        passed_count = 0

        print(f"\n  Validating {total} condition winners on unseen data...")

        for i, (condition, winners) in enumerate(train_results.items()):
            if not winners:
                continue

            top = winners[0]  # #1 from training

            # Load this variant's trades in validation period under same condition
            val_trades = self._load_condition_trades(
                condition, val_start_ms, val_end_ms
            )

            # Filter to just this variant
            variant_val_trades = [
                t for t in val_trades if t.get("variant_id") == top.variant_id
            ]

            vr = ValidationResult(
                condition=condition,
                variant_id=top.variant_id,
                exit_model=top.exit_model,
                train_trades=top.trade_count,
                train_expectancy=top.expectancy,
                train_win_rate=top.win_rate,
                train_pf=top.profit_factor,
            )

            if len(variant_val_trades) < 5:
                # Not enough validation trades — inconclusive
                vr.passed = False
                vr.val_trades = len(variant_val_trades)
                validations.append(vr)
                continue

            # Score with same exit model
            exit_col = f"{top.exit_model}_result"
            pnls = [
                t.get(exit_col) for t in variant_val_trades
                if t.get(exit_col) is not None
            ]

            if len(pnls) < 5:
                vr.passed = False
                validations.append(vr)
                continue

            val_metrics = compute_metrics(pnls)
            vr.val_trades = val_metrics.trade_count
            vr.val_expectancy = val_metrics.expectancy
            vr.val_win_rate = val_metrics.win_rate
            vr.val_pf = val_metrics.profit_factor

            # Pass criteria:
            # 1. Still positive expectancy in validation
            # 2. Didn't degrade more than 70% from training
            if top.expectancy > 0:
                vr.degradation_pct = (
                    (top.expectancy - val_metrics.expectancy)
                    / top.expectancy * 100
                )
            else:
                vr.degradation_pct = 100.0

            vr.passed = (
                val_metrics.expectancy > 0
                and vr.degradation_pct < 70.0
            )

            if vr.passed:
                passed_count += 1

            validations.append(vr)

            if (i + 1) % 30 == 0:
                print(f"    [{i+1}/{total}] validated, {passed_count} passed")

        print(f"\n  Validation complete: {passed_count}/{total} conditions passed")
        return validations


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point for regime-conditional scoring."""
    print("=" * 70)
    print("  REGIME-CONDITIONAL SCORER")
    print("  Find best variant per market condition")
    print("=" * 70)

    # Parse args
    args = sys.argv[1:]
    from_date: str | None = None
    to_date: str | None = None
    val_from: str | None = None
    val_to: str | None = None
    top_n = 5
    min_trades = 30
    cost_model_name = "futures"
    instruments_arg: str = "all"
    min_freq: float = 0.0

    i = 0
    while i < len(args):
        if args[i] == "--from" and i + 1 < len(args):
            from_date = args[i + 1]
            i += 2
        elif args[i] == "--to" and i + 1 < len(args):
            to_date = args[i + 1]
            i += 2
        elif args[i] == "--validate-from" and i + 1 < len(args):
            val_from = args[i + 1]
            i += 2
        elif args[i] == "--validate-to" and i + 1 < len(args):
            val_to = args[i + 1]
            i += 2
        elif args[i] == "--top" and i + 1 < len(args):
            top_n = int(args[i + 1])
            i += 2
        elif args[i] == "--min-trades" and i + 1 < len(args):
            min_trades = int(args[i + 1])
            i += 2
        elif args[i] == "--cost" and i + 1 < len(args):
            cost_model_name = args[i + 1]
            i += 2
        elif args[i] == "--instruments" and i + 1 < len(args):
            instruments_arg = args[i + 1]
            i += 2
        elif args[i] == "--min-freq" and i + 1 < len(args):
            min_freq = float(args[i + 1])
            i += 2
        else:
            i += 1

    if not from_date or not to_date:
        print("\n  ERROR: --from and --to are required")
        print("  Usage: python -m app.scoring.regime_scorer "
              "--from 2021-01-01 --to 2024-12-31")
        print()
        print("  Options:")
        print("    --instruments  bnf|nf|indices|stocks|all|26000,26009,...")
        print("    --cost         none|equity_intraday|futures|options")
        print("    --min-freq     N   (minimum trades per month, e.g. 15)")
        print("    --min-trades   N   (minimum total trades per condition, default 30)")
        print("    --top          N   (top N variants per condition, default 5)")
        print("    --validate-from / --validate-to  (forward walk dates)")
        print()
        print("  Examples:")
        print("    # BNF+NF only, futures costs, min 15 trades/month")
        print("    python -m app.scoring.regime_scorer \\")
        print("        --from 2021-01-01 --to 2024-12-31 \\")
        print("        --instruments indices --cost futures --min-freq 15")
        print()
        print("    # All instruments, no cost filter, no freq filter")
        print("    python -m app.scoring.regime_scorer \\")
        print("        --from 2021-01-01 --to 2024-12-31 --cost none")
        return

    # Resolve instrument list
    if instruments_arg in INSTRUMENT_PRESETS:
        instrument_tokens = INSTRUMENT_PRESETS[instruments_arg]
    else:
        # Comma-separated token list
        instrument_tokens = [t.strip() for t in instruments_arg.split(",") if t.strip()]
        # Validate
        unknown = [t for t in instrument_tokens if t not in ALL_INSTRUMENTS]
        if unknown:
            print(f"\n  ERROR: Unknown instrument tokens: {unknown}")
            print(f"  Valid tokens: {list(ALL_INSTRUMENTS.keys())}")
            return

    # Parse dates
    start_dt = datetime.strptime(from_date, "%Y-%m-%d")
    end_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59
    )
    start_ms = start_dt.timestamp() * 1000
    end_ms = end_dt.timestamp() * 1000

    inst_names = [ALL_INSTRUMENTS.get(t, t) for t in instrument_tokens]
    print(f"\n  Train period:    {from_date} → {to_date}")
    print(f"  Instruments:     {', '.join(inst_names)} ({len(instrument_tokens)} total)")
    print(f"  Cost model:      {cost_model_name}")
    print(f"  Min trades:      {min_trades}")
    print(f"  Min freq:        {min_freq:.0f}/month" if min_freq > 0 else "  Min freq:        off")
    print(f"  Top N/cond:      {top_n}")

    if val_from and val_to:
        print(f"  Validate:        {val_from} → {val_to}")

    # Setup
    store = ResearchStore()
    store.start()

    scorer = RegimeScorer(
        store=store,
        min_trades=min_trades,
        top_per_condition=top_n,
        cost_model_name=cost_model_name,
        instruments=instrument_tokens,
        min_trades_per_month=min_freq,
    )

    # Score all conditions
    results = scorer.score_all_conditions(start_ms, end_ms)

    # Print results
    _print_results(results, cost_model_name)

    # Validation if requested
    if val_from and val_to:
        val_start_dt = datetime.strptime(val_from, "%Y-%m-%d")
        val_end_dt = datetime.strptime(val_to, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59
        )
        val_start_ms = val_start_dt.timestamp() * 1000
        val_end_ms = val_end_dt.timestamp() * 1000

        validations = scorer.validate_winners(results, val_start_ms, val_end_ms)
        _print_validations(validations)

    store.stop()
    print(f"\n{'═' * 70}")


def _print_results(
    results: dict[ConditionKey, list[ConditionWinner]],
    cost_model: str,
) -> None:
    """Print the regime→variant mapping table."""
    print(f"\n{'─' * 70}")
    print(f"  REGIME → VARIANT MAPPING ({len(results)} conditions with winners)")
    print(f"{'─' * 70}")

    # Group by instrument for readability
    by_instrument: dict[str, list[tuple[ConditionKey, ConditionWinner]]] = defaultdict(list)

    for condition, winners in sorted(
        results.items(), key=lambda x: x[0].label()
    ):
        if winners:
            by_instrument[condition.instrument_name].append(
                (condition, winners[0])
            )

    for inst_name in sorted(by_instrument.keys()):
        entries = by_instrument[inst_name]
        print(f"\n  ┌─── {inst_name} ({len(entries)} conditions) ───")

        for condition, winner in entries:
            cost_marker = "✅" if winner.profitable_after_costs else "⚠️"
            print(
                f"  │ {condition.volatility:6s} {condition.structure:13s} "
                f"{condition.session:7s} → "
                f"{winner.strategy:5s} {winner.timeframe:3s} "
                f"[{winner.exit_model:20s}] "
                f"WR={winner.win_rate*100:.0f}% "
                f"E={winner.expectancy:.1f} "
                f"net={winner.net_expectancy:.1f} "
                f"freq={winner.trades_per_month:.1f}/mo "
                f"N={winner.trade_count:4d} "
                f"S={winner.score:.0f} "
                f"{cost_marker}"
            )

        print(f"  └───")

    # Summary stats
    all_winners = [ws[0] for ws in results.values() if ws]
    if all_winners:
        print(f"\n  ── Summary ──")
        print(f"  Total conditions with winners: {len(results)}")
        total_possible = (
            len(INSTRUMENTS) * len(VOLATILITY_REGIMES)
            * len(MARKET_STRUCTURES) * len(SESSIONS)
        )
        print(f"  Total possible conditions:     {total_possible}")
        print(f"  Coverage:                      "
              f"{len(results)/total_possible*100:.0f}%")

        # Strategy distribution among winners
        strat_counts: dict[str, int] = defaultdict(int)
        for cond_winners in results.values():
            if cond_winners:
                strat_counts[cond_winners[0].strategy] += 1

        print(f"\n  Strategy distribution (among #1 winners):")
        for strat, cnt in sorted(
            strat_counts.items(), key=lambda x: -x[1]
        ):
            print(f"    {strat:6s}: {cnt} conditions")


def _print_validations(validations: list[ValidationResult]) -> None:
    """Print forward walk validation results."""
    print(f"\n{'─' * 70}")
    print(f"  FORWARD WALK VALIDATION")
    print(f"{'─' * 70}")

    passed = [v for v in validations if v.passed]
    failed = [v for v in validations if not v.passed]

    print(f"\n  ✅ PASSED: {len(passed)} / {len(validations)} conditions")
    print()

    if passed:
        print("  ── Top validated winners (sorted by validation expectancy) ──")
        passed_sorted = sorted(passed, key=lambda v: v.val_expectancy, reverse=True)

        for v in passed_sorted[:30]:
            print(
                f"    {v.condition.label():40s} "
                f"│ {v.variant_id[:8]} "
                f"│ Train: E={v.train_expectancy:5.1f} WR={v.train_win_rate*100:.0f}% "
                f"N={v.train_trades:4d} "
                f"│ Val: E={v.val_expectancy:5.1f} WR={v.val_win_rate*100:.0f}% "
                f"N={v.val_trades:3d} "
                f"│ Deg={v.degradation_pct:.0f}%"
            )

    if failed:
        print(f"\n  ── Failed ({len(failed)}) ──")
        # Show first 10 failures
        for v in failed[:10]:
            reason = "too few trades" if v.val_trades < 5 else (
                f"E={v.val_expectancy:.1f}, deg={v.degradation_pct:.0f}%"
            )
            print(
                f"    {v.condition.label():40s} "
                f"│ {v.variant_id[:8]} "
                f"│ {reason}"
            )
        if len(failed) > 10:
            print(f"    ... and {len(failed) - 10} more")

    # Unique variants that passed
    if passed:
        unique_variants = set(v.variant_id for v in passed)
        print(f"\n  Unique validated variants: {len(unique_variants)}")
        print(f"  (These are your deployment candidates)")

        # Show the actual variant IDs
        print(f"\n  ── Validated variant IDs ──")
        variant_conditions: dict[str, list[str]] = defaultdict(list)
        for v in passed:
            variant_conditions[v.variant_id].append(v.condition.label())

        for vid, conditions in sorted(
            variant_conditions.items(),
            key=lambda x: -len(x[1]),
        ):
            print(f"    {vid}: wins in {len(conditions)} conditions")
            for c in conditions[:5]:
                print(f"      └ {c}")
            if len(conditions) > 5:
                print(f"      └ ... +{len(conditions)-5} more")


if __name__ == "__main__":
    main()
